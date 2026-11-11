"""Private Vertex executor: explicit consent, one-shot forwarding, retained evidence.

Adapts the Gemini/Anthropic qualification controls and uses the existing execution,
ownership, receipts, assessment and recovery services. Historical plans and other
providers are unchanged. This harness never publishes production qualifications.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import os
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, field_validator

from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.models import PairTask
from judgmentllm.output_contracts import (
    PairwiseOutputContract,
    RubricOutputContract,
    validate_pairwise_contract_output,
    validate_rubric_contract_output,
)
from judgmentllm.persistence.artifacts import ArtifactStore, _fsync_directory
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    SHA256,
    ArtifactIdentity,
    NotFoundError,
    PersistenceError,
)
from judgmentllm.persistence.ownership import ExecutorOwnership, _lock, _unlock
from judgmentllm.persistence.receipts import read_receipt
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.persistence.transport_receipts import read_transport_commitment
from judgmentllm.providers.protocol import PreparedRequest, ProviderIntegrityError
from judgmentllm.qualification import _immutable
from judgmentllm.results import (
    CategoricalOutput,
    PairwiseOutput,
    PairwiseResult,
    RubricResult,
)
from judgmentllm.vertex_config import VertexRuntimeConfiguration
from judgmentllm.vertex_qualification import (
    QualificationError,
    VertexQualificationCallV2,
    VertexQualificationPlanV2,
    _source_commit,
    _specification,
    verify_plan,
)
from judgmentllm.vertex_transport import (
    PreparedRequestV7,
    VertexProductionContext,
    _qualification_context,
    production_context_digest,
)

_EXECUTOR_TOKEN = object()
_CEILING = Decimal("0.02")


class _Authorization(EvidenceModel):
    """Separate immutable consent; never alters the zero-authority proposal."""

    authorization_version: Literal[1] = 1
    executor: Literal["vertex-qualification-v1"] = "vertex-qualification-v1"
    plan_sha256: SHA256
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    authentication_mode: Literal["api_key", "adc"]
    credential_source: Literal["JUDGMENTLLM_VERTEX_API_KEY", "google_adc"]
    route_sha256: SHA256
    provider: Literal["vertex"] = "vertex"
    endpoint: Literal["models.generate_content"] = "models.generate_content"
    model: Literal["gemini-3.1-flash-lite"] = "gemini-3.1-flash-lite"
    location: Literal["global"] = "global"
    api_version: Literal["v1beta1"] = "v1beta1"
    sdk_version: Literal["2.34.0"] = "2.34.0"
    billable_calls_authorized: Literal[True] = True
    full_synthetic_assessment_capture_authorized: Literal[True] = True
    maximum_physical_requests: Literal[4] = 4
    maximum_output_tokens_per_call: int = Field(gt=0, le=512)
    reserved_input_tokens: int = Field(gt=0)
    reserved_output_tokens_including_thinking: int = Field(gt=0)
    reserved_estimated_usd: str
    estimated_authorization_ceiling_usd: Literal["0.02"] = "0.02"
    monetary_cap_provider_enforced: Literal[False] = False
    reasoning_retention: Literal["none"] = "none"

    @field_validator(
        "authorization_version",
        "maximum_physical_requests",
        "maximum_output_tokens_per_call",
        mode="before",
    )
    @classmethod
    def exact_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise QualificationError()
        return value


def _durable(path: Path, raw: bytes) -> None:
    # Exclusive creation deliberately leaves a partial file on failure. It must
    # block future forwarding, including death before the SQLite commit.
    _immutable(path, raw)
    os.chmod(path, 0o600)
    _fsync_directory(path.parent)


def _authorization(plan: VertexQualificationPlanV2, digest: str) -> bytes:
    return canonical_bytes(
        _Authorization(
            plan_sha256=digest,
            source_commit=plan.source_commit,
            authentication_mode=plan.authentication_mode,
            credential_source=plan.credential_source,
            route_sha256=plan.route_sha256,
            maximum_output_tokens_per_call=plan.max_output_tokens_per_case,
            reserved_input_tokens=plan.reserved_input_tokens,
            reserved_output_tokens_including_thinking=plan.reserved_output_tokens_including_thinking,
            reserved_estimated_usd=str(plan.reserved_estimated_usd),
        )
    )


def _state_directory(directory: Path) -> Path:
    """One fixed sidecar; never add files to the immutable plan inventory."""
    directory = directory.resolve()
    state = directory.with_name(directory.name + ".execution")
    if state.is_symlink() or any(p.is_symlink() for p in state.rglob("*")):
        raise QualificationError()
    return state


def _authorization_anchor(directory: Path) -> Path:
    """Independent tombstone: losing the entire execution directory cannot reset it."""
    directory = directory.resolve()
    anchor = directory.with_name(directory.name + ".authorization.json")
    if anchor.is_symlink():
        raise QualificationError()
    return anchor


@contextmanager
def _plan_ownership(directory: Path) -> Generator[None]:
    """Reuse permanent, nonblocking cross-platform OS locks; never unlink a lock."""
    state = _state_directory(directory)
    state.mkdir(mode=0o700, exist_ok=True)
    stream = ExecutorOwnership._open_file(state / "executor.lock")
    try:
        try:
            _lock(stream)
        except OSError:
            raise QualificationError() from None
        try:
            _fsync_directory(state.parent)
            yield
        finally:
            _unlock(stream)
    finally:
        stream.close()


def _load_plan(
    directory: Path, expected_plan_sha256: str
) -> tuple[VertexQualificationPlanV2, str]:
    try:
        verify_plan(directory, expected_plan_sha256=expected_plan_sha256)
        raw = (directory / "plan.json").read_bytes()
        if bytes_digest(raw) != expected_plan_sha256:
            raise QualificationError()
        return VertexQualificationPlanV2.model_validate_json(raw), expected_plan_sha256
    except (ValueError, OSError):
        raise QualificationError() from None


def _intent(
    plan: VertexQualificationPlanV2, digest: str, case: VertexQualificationCallV2
) -> bytes:
    return canonical_bytes(
        {
            "consumption_version": 1,
            "provider": "vertex",
            "authentication_mode": plan.authentication_mode,
            "route_sha256": plan.route_sha256,
            "plan_sha256": digest,
            "source_commit": plan.source_commit,
            "case_id": case.case_id,
            "submission_id": case.transport.identity.submission_id,
            "correlation_id": case.transport.identity.correlation_id,
            "payload_sha256": case.transport.payload_sha256,
            "prompt_sha256": case.transport.prompt_sha256,
        }
    )


def _authorize(directory: Path, plan: VertexQualificationPlanV2, digest: str) -> None:
    state = _state_directory(directory)
    state.mkdir(mode=0o700, exist_ok=True)
    path = state / "execution-authorization.json"
    anchor = _authorization_anchor(directory)
    raw = _authorization(plan, digest)
    if path.exists():
        if path.read_bytes() != raw:
            raise QualificationError()
        # Existing authorization never allows reconstruction of missing state.
        _consumed(directory, plan, digest)
        return
    if anchor.exists() or any(p.name != "executor.lock" for p in state.iterdir()):
        raise QualificationError()
    _durable(anchor, raw)
    _durable(path, raw)
    (_state_directory(directory) / "forward-intents").mkdir()
    _fsync_directory(state)
    _durable(_state_directory(directory) / "ledger-created", digest.encode())
    with closing(
        sqlite3.connect(_state_directory(directory) / "qualification.sqlite3")
    ) as connection:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE authorization(plan_sha256 TEXT PRIMARY KEY, source_commit TEXT NOT NULL, plan_bytes BLOB NOT NULL)"
        )
        connection.execute(
            "INSERT INTO authorization VALUES(?,?,?)",
            (digest, plan.source_commit, canonical_bytes(plan)),
        )
        connection.execute(
            "CREATE TABLE calls(case_id TEXT PRIMARY KEY, submission_id TEXT UNIQUE NOT NULL, payload_sha256 TEXT NOT NULL, consumed_at TEXT)"
        )
        connection.executemany(
            "INSERT INTO calls VALUES(?,?,?,NULL)",
            [
                (
                    c.case_id,
                    c.transport.identity.submission_id,
                    c.transport.payload_sha256,
                )
                for c in plan.calls
            ],
        )
        connection.commit()
    os.chmod(state / "qualification.sqlite3", 0o600)
    _fsync_directory(state)


def _ledger_rows(
    connection: sqlite3.Connection, plan: VertexQualificationPlanV2, digest: str
) -> list[tuple[Any, ...]]:
    if connection.execute(
        "SELECT plan_sha256,source_commit,plan_bytes FROM authorization"
    ).fetchall() != [(digest, plan.source_commit, canonical_bytes(plan))]:
        raise QualificationError()
    rows = connection.execute(
        "SELECT case_id,submission_id,payload_sha256,consumed_at FROM calls"
    ).fetchall()
    expected = {
        c.case_id: (c.transport.identity.submission_id, c.transport.payload_sha256)
        for c in plan.calls
    }
    if len(rows) != 4 or any(expected.get(r[0]) != (r[1], r[2]) for r in rows):
        raise QualificationError()
    return rows


def _consumed(
    directory: Path, plan: VertexQualificationPlanV2, digest: str
) -> set[str]:
    authorization = _state_directory(directory) / "execution-authorization.json"
    anchor = _authorization_anchor(directory)
    ledger = _state_directory(directory) / "qualification.sqlite3"
    intents = _state_directory(directory) / "forward-intents"
    marker = _state_directory(directory) / "ledger-created"
    if not authorization.exists():
        state = _state_directory(directory)
        if anchor.exists() or (
            state.exists() and any(p.name != "executor.lock" for p in state.iterdir())
        ):
            raise QualificationError()
        return set()
    if (
        authorization.read_bytes() != _authorization(plan, digest)
        or not anchor.is_file()
        or anchor.read_bytes() != _authorization(plan, digest)
        or not ledger.is_file()
        or marker.read_bytes() != digest.encode()
        or not intents.is_dir()
        or any(
            p.name not in {c.case_id + ".json" for c in plan.calls}
            for p in intents.iterdir()
        )
    ):
        raise QualificationError()
    with closing(
        sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)
    ) as connection:
        rows = _ledger_rows(connection, plan, digest)
    consumed = {r[0] for r in rows if r[3] is not None}
    for case in plan.calls:
        path = intents / (case.case_id + ".json")
        if path.exists():
            if path.read_bytes() != _intent(plan, digest, case):
                raise QualificationError()
            consumed.add(case.case_id)
        elif case.case_id in consumed:
            raise QualificationError()
    return consumed


def _verify_prepared(
    prepared: PreparedRequest, case: VertexQualificationCallV2
) -> None:
    if (
        type(prepared) is not PreparedRequestV7
        or (prepared.provider, prepared.endpoint, prepared.model)
        != ("vertex", "models.generate_content", "gemini-3.1-flash-lite")
        or prepared.request_uid != case.transport.identity.request_uid
        or semantic_digest("attempt-membership", list(prepared.task_ids))
        != case.transport.membership_sha256
        or prepared.payload != case.request_body_utf8.encode()
        or prepared.payload_sha256 != case.transport.payload_sha256
        or bytes_digest(prepared.payload) != case.transport.payload_sha256
        or prepared.prompt_sha256 != case.transport.prompt_sha256
        or prepared.dependency_sha256 != case.transport.dependency_sha256
    ):
        raise QualificationError()
    receipt = prepared.transport_commitment
    if (
        receipt != case.transport
        or prepared.rendered_text != case.rendered_prompt_utf8
        or (
            receipt.identity.run_id,
            receipt.identity.attempt_id,
            receipt.identity.submission_id,
            receipt.identity.correlation_id,
        )
        != (
            case.run_id,
            case.transport.identity.attempt_id,
            case.transport.identity.submission_id,
            case.transport.identity.correlation_id,
        )
        or receipt.mechanism != case.transport.mechanism
    ):
        raise QualificationError()


class _QualificationSubmissionGuard:
    """Vertex-only executor authority, consumed once at actual HTTP forwarding."""

    def __init__(
        self,
        directory: Path,
        plan: VertexQualificationPlanV2,
        case: VertexQualificationCallV2,
        *,
        token: object,
    ) -> None:
        if token is not _EXECUTOR_TOKEN or case not in plan.calls:
            raise QualificationError()
        self.directory = directory
        self.plan = VertexQualificationPlanV2.model_validate_json(canonical_bytes(plan))
        self.case = case
        self.digest = bytes_digest(canonical_bytes(plan))

    def verify_context(self, context: VertexProductionContext) -> None:
        expected = _qualification_context(
            _specification(
                self.directory,
                self.case.task_kind,
                self.case.mode,
                self.plan.max_output_tokens_per_case,
            ),
            source_commit=self.plan.source_commit,
            runtime=self.plan.runtime(),
        )
        if type(context) is not VertexProductionContext or production_context_digest(
            context
        ) != production_context_digest(expected):
            raise ProviderIntegrityError("Vertex qualification context is incompatible")

    def __call__(self, prepared: PreparedRequest) -> None:
        try:
            current, digest = _load_plan(self.directory, self.digest)
            if (
                current != self.plan
                or digest != self.digest
                or _source_commit() != self.plan.source_commit
            ):
                raise QualificationError()
            _verify_prepared(prepared, self.case)
            consumed = _consumed(self.directory, self.plan, digest)
            if self.case.case_id in consumed or len(consumed) >= 4:
                raise QualificationError()
            # A guard cannot authorize out-of-order execution or stepping past
            # uncertain, refused, model-mismatched or damaged prior evidence.
            prior = cast(
                list[dict[str, Any]],
                report_execution(self.directory, self.plan, digest)["calls"],
            )
            index = list(self.plan.calls).index(self.case)
            if any(
                not row["assessment_continuation_allowed"] for row in prior[:index]
            ) or any(row["physical_send_attempted"] for row in prior[index:]):
                raise QualificationError()
            if _estimated_exposure(self.plan, prior) > _CEILING:
                raise QualificationError()
            with closing(
                sqlite3.connect(
                    _state_directory(self.directory) / "qualification.sqlite3"
                )
            ) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                _ledger_rows(connection, self.plan, digest)
                changed = connection.execute(
                    "UPDATE calls SET consumed_at=? WHERE case_id=? AND consumed_at IS NULL",
                    (datetime.now(UTC).isoformat(), self.case.case_id),
                ).rowcount
                if changed != 1:
                    raise QualificationError()
                _durable(
                    _state_directory(self.directory)
                    / "forward-intents"
                    / (self.case.case_id + ".json"),
                    _intent(self.plan, digest, self.case),
                )
                connection.commit()
        except Exception:
            # Do not let filesystem, database or raw validation messages escape
            # through SDK exception wrapping or ordinary transport uncertainty.
            raise ProviderIntegrityError(
                "Vertex qualification forwarding denied"
            ) from None


async def execute_plan(
    directory: Path,
    *,
    authorized_plan_sha256: str,
    authorized_source_commit: str,
    authorized_project: str,
    authorized_authentication_mode: Literal["api_key", "adc"],
    authorized_location: str,
    reviewed_request_count: int,
    reviewed_reserved_estimated_usd: Decimal,
    reviewed_estimated_ceiling_usd: Decimal,
    accept_billable_calls: bool,
    accept_full_synthetic_capture: bool,
) -> dict[str, object]:
    """Sequential, at most four original calls; recovery never grants replacements."""
    plan, digest = _load_plan(directory, authorized_plan_sha256)
    runtime = VertexRuntimeConfiguration(
        project=authorized_project,
        auth_mode=authorized_authentication_mode,
        location=authorized_location,
    )
    if (
        accept_billable_calls is not True
        or accept_full_synthetic_capture is not True
        or authorized_source_commit != plan.source_commit
        or _source_commit() != plan.source_commit
        or runtime != plan.runtime()
        or type(reviewed_request_count) is not int
        or reviewed_request_count != 4
        or type(reviewed_reserved_estimated_usd) is not Decimal
        or reviewed_reserved_estimated_usd != plan.reserved_estimated_usd
        or type(reviewed_estimated_ceiling_usd) is not Decimal
        or reviewed_estimated_ceiling_usd != _CEILING
    ):
        raise QualificationError()
    directory = directory.resolve()
    with _plan_ownership(directory):
        _authorize(directory, plan, digest)
        return await _execute_authorized(directory, plan, digest)


async def _execute_authorized(
    directory: Path, plan: VertexQualificationPlanV2, digest: str
) -> dict[str, object]:
    from judgmentllm.execution import execute_run
    from judgmentllm.execution.retry import RetryPolicy
    from judgmentllm.preparation import open_prepared_run, prepare_run
    from judgmentllm.providers.vertex import VertexAdapter
    from judgmentllm.transport import physical_identity

    # Verify all four prepared commitments before any credential access. This
    # also admits retained source/transport artifacts via the existing engine.
    for case in plan.calls:
        spec = _specification(
            directory, case.task_kind, case.mode, plan.max_output_tokens_per_case
        )
        store = _state_directory(directory) / "execution" / case.case_id
        context = _qualification_context(
            spec, source_commit=plan.source_commit, runtime=plan.runtime()
        )
        prepared_run = (
            open_prepared_run(
                case.run_id, spec=spec, store=store, transport_context=context
            )
            if (store / "registry.sqlite3").exists()
            else prepare_run(
                spec, run_id=case.run_id, store=store, transport_context=context
            )
        )
        with prepared_run as prepared:
            dependencies = tuple(prepared.iter_request_dependencies())
            if len(dependencies) != 1:
                raise QualificationError()
            _verify_prepared(
                prepared.prepare_transport_attempt(
                    dependencies[0],
                    physical_identity(
                        case.run_id,
                        case.transport.identity.request_uid,
                        case.transport.identity.attempt_id,
                    ),
                ),
                case,
            )
    for case in plan.calls:
        rows = cast(
            list[dict[str, Any]], report_execution(directory, plan, digest)["calls"]
        )
        if _estimated_exposure(plan, rows) > _CEILING:
            break
        row = next(r for r in rows if r["case_id"] == case.case_id)
        if row["assessment_continuation_allowed"]:
            continue
        if row["evidence_integrity_failure"]:
            break
        spec = _specification(
            directory, case.task_kind, case.mode, plan.max_output_tokens_per_case
        )
        adapter = VertexAdapter(
            plan.endpoint,
            plan.model,
            context=_qualification_context(
                spec, source_commit=plan.source_commit, runtime=plan.runtime()
            ),
            concurrency=1,
            submission_guard=_QualificationSubmissionGuard(
                directory, plan, case, token=_EXECUTOR_TOKEN
            ),
        )
        try:
            # execute_run reopens under ownership and recovers retained receipts.
            # A consumed identity always fails its guard if recovery tries to send.
            result = await execute_run(
                case.run_id,
                store=_state_directory(directory) / "execution" / case.case_id,
                spec=spec,
                adapter=adapter,
                retry_policy=RetryPolicy(max_attempts=1),
            )
        finally:
            await adapter.aclose()
        row = next(
            r
            for r in cast(
                list[dict[str, Any]], report_execution(directory, plan, digest)["calls"]
            )
            if r["case_id"] == case.case_id
        )
        if result.recovery_required or not row["assessment_continuation_allowed"]:
            break
    return report_execution(directory, plan, digest)


_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "cache_read_tokens",
)


def _case_task_ids(
    directory: Path, plan: VertexQualificationPlanV2, case: VertexQualificationCallV2
) -> tuple[str, ...]:
    from judgmentllm.io import read_pairs, read_responses
    from judgmentllm.planning import plan_record

    spec = _specification(
        directory, case.task_kind, case.mode, plan.max_output_tokens_per_case
    )
    if spec.pairs is not None:
        record = next(read_pairs(spec.pairs))
    else:
        record = next(read_responses(spec.input))
    return tuple(task.task_id for task in next(plan_record(spec, record)).tasks)


def _verify_assessment(
    directory: Path,
    plan: VertexQualificationPlanV2,
    case: VertexQualificationCallV2,
    raw: bytes,
    results: tuple[RubricResult | PairwiseResult, ...],
) -> bool:
    """Revalidate retained text independently; never repair or replace a result."""
    spec = _specification(
        directory, case.task_kind, case.mode, plan.max_output_tokens_per_case
    )
    pair = PairTask(
        pair_id="001",
        pair_uid="001",
        trait_id="01",
        left_item_id="001",
        right_item_id="002",
        presented_order=["002", "001"],
    )
    try:
        if spec.rubric is not None:
            output = validate_rubric_contract_output(
                raw, RubricOutputContract(traits=tuple(spec.rubric.traits))
            )
        else:
            output = validate_pairwise_contract_output(
                raw, PairwiseOutputContract(pair=pair)
            )
    except ValueError:
        return False
    if len(results) != 1:
        raise QualificationError()
    result = results[0]
    if (
        isinstance(result, RubricResult)
        and spec.rubric is not None
        and isinstance(output, CategoricalOutput)
    ):
        if (
            result.item_id != "001"
            or result.trait_id != "01"
            or result.category_label != output.scores[0].category_label
            or result.resolved_levels != spec.rubric.traits[0].resolved_levels
            or result.probabilities is not None
            or result.expected_value is not None
        ):
            raise QualificationError()
    elif isinstance(result, PairwiseResult) and isinstance(output, PairwiseOutput):
        if (
            result.pair_uid != pair.pair_uid
            or result.trait_id != pair.trait_id
            or result.original_order != pair.original_order
            or result.presented_order != pair.presented_order
            or result.winner_id != output.winner_id
            or result.tie
            or any(p is not None for p in (result.p_left, result.p_right, result.p_tie))
        ):
            raise QualificationError()
    else:
        raise QualificationError()
    if (
        result.model != plan.model
        or result.provider != plan.provider
        or result.endpoint != plan.endpoint
        or result.explanation is not None
        or result.confidence is not None
    ):
        raise QualificationError()
    return True


def _case_report(
    directory: Path,
    plan: VertexQualificationPlanV2,
    case: VertexQualificationCallV2,
    consumed: bool,
) -> dict[str, Any]:
    from judgmentllm.preparation import _load_manifest, _verify_metadata

    task_ids = _case_task_ids(directory, plan, case)
    row: dict[str, Any] = {
        "case_id": case.case_id,
        "run_id": case.run_id,
        "request_uid": case.transport.identity.request_uid,
        "attempt_id": case.transport.identity.attempt_id,
        "submission_id": case.transport.identity.submission_id,
        "correlation_id": case.transport.identity.correlation_id,
        "task_ids": list(task_ids),
        "mechanism": case.transport.mechanism,
        "mode": case.mode,
        "authentication_mode": plan.authentication_mode,
        "route_sha256": plan.route_sha256,
        "transport_commitment_sha256": case.transport.commitment_sha256,
        "payload_sha256": case.transport.payload_sha256,
        "prompt_sha256": case.transport.prompt_sha256,
        "requested_model": plan.model,
        "reported_model": None,
        "response_model_identity_verified": False,
        "response_model_diagnostic": "response_not_observed",
        "physical_send_attempted": consumed,
        "allocation_consumed": consumed,
        "provider_outcome": None,
        "http_response_observed": False,
        "invocation_failure_code": None,
        "invocation_acceptance": None,
        "submission_certainty": "not_started",
        "assessment_valid": False,
        "canonical_valid": None,
        "parser_diagnostic": None,
        "assessment_continuation_allowed": False,
        "assessment_terminal": False,
        "diagnostic": None,
        "durable_evidence_consistent": False,
        "evidence_integrity_failure": False,
        "usage": None,
        "estimated_actual_usd": None,
        "reserved_estimated_usd": str(case.reserved_estimated_usd),
        "status": "unresolved" if consumed else "outstanding",
        "terminal_state": "uncertain_submission" if consumed else "not_started",
        "response_receipt_sha256": None,
        "retained_response_sha256": None,
        "assessment_sha256": None,
        "evidence_receipt_sha256": None,
        "normalized_results_sha256": None,
        "original_order": ["001", "002"] if case.task_kind == "pairwise" else None,
        "presented_order": ["002", "001"] if case.task_kind == "pairwise" else None,
        "live_qualified": False,
    }
    store = _state_directory(directory) / "execution" / case.case_id
    if not (store / "registry.sqlite3").exists():
        return row
    try:
        with ReadOnlyRepository(store, preserve_files=True) as repository:
            summary = repository.summary(case.run_id)
            results = tuple(repository.iter_results(case.run_id))
            receipts = tuple(repository.iter_evidence(case.run_id))
            artifacts = ArtifactStore(
                repository.run_directory(case.run_id), create=False
            )
            manifest = _load_manifest(repository, case.run_id, unsealed=False)
            _verify_metadata(repository, manifest)
            artifacts.read(manifest.specification)
            for source in manifest.sources:
                artifacts.read(repository.get_artifact(case.run_id, source.raw_sha256))
            transport = read_transport_commitment(
                artifacts, case.transport.identity.attempt_id
            )
            if (
                transport is None
                or transport.commitment_sha256 != case.transport.commitment_sha256
            ):
                raise QualificationError()
            # Verify complete retained request and prompt bytes, not only hashes
            # named in mutable registry rows.
            for digest in (case.transport.payload_sha256, case.transport.prompt_sha256):
                artifacts.read(repository.get_artifact(case.run_id, digest))
            try:
                submission = repository.get_submission(
                    case.run_id, case.transport.identity.submission_id
                )
            except NotFoundError:
                submission = None
            row["submission_certainty"] = (
                submission.certainty if submission else "not_started"
            )
            if len(receipts) > 1 or summary.attempts > 1 or summary.retry_operations:
                raise QualificationError()
            if not receipts:
                if (
                    results
                    or repository.get_observation_for_submission(
                        case.run_id, case.transport.identity.submission_id
                    )
                    is not None
                ):
                    raise QualificationError()
                return row
            receipt = receipts[0]
            if not consumed or (
                receipt.identity.run_id,
                receipt.identity.request_uid,
                receipt.identity.attempt_id,
                receipt.identity.submission_id,
                receipt.correlation_id,
                receipt.payload_sha256,
            ) != (
                case.run_id,
                case.transport.identity.request_uid,
                case.transport.identity.attempt_id,
                case.transport.identity.submission_id,
                case.transport.identity.correlation_id,
                case.transport.payload_sha256,
            ):
                raise QualificationError()
            usage = receipt.usage
            if (
                usage.provider != "vertex"
                or usage.requested_model != plan.model
                or usage.endpoint != plan.endpoint
                or usage.sdk_version not in (None, plan.sdk_version)
            ):
                raise QualificationError()
            row.update(
                evidence_receipt_sha256=bytes_digest(canonical_bytes(receipt)),
                normalized_results_sha256=bytes_digest(canonical_bytes(list(results))),
                provider_outcome=usage.provider_outcome,
                invocation_failure_code=usage.invocation_failure_code,
                invocation_acceptance=receipt.invocation_acceptance,
                reported_model=usage.reported_model,
                usage={
                    name: getattr(usage, name) for name in ("usage_source", *_COUNTERS)
                }
                | {"breakdown": [b.model_dump(mode="json") for b in usage.breakdown]},
            )
            observation_id = receipt.identity.observation_id
            if observation_id is None:
                row["http_response_observed"] = (
                    usage.invocation_failure_code is not None
                )
                row["status"] = (
                    "unsuccessful" if usage.invocation_failure_code else "unresolved"
                )
                row["terminal_state"] = (
                    "provider_rejected"
                    if usage.invocation_failure_code
                    else "uncertain_submission"
                )
                return row
            response = read_receipt(artifacts, case.transport.identity.submission_id)
            if response is None or submission is None:
                raise QualificationError()
            response.verify_submission(submission)
            artifacts.read(response.raw)
            observation = repository.get_observation(case.run_id, observation_id)
            if (
                response.observation() != observation
                or receipt.response_receipt_sha256
                != bytes_digest(canonical_bytes(response))
            ):
                raise QualificationError()
            diagnostic = receipt.assessment_diagnostic
            if (
                diagnostic is None
                or diagnostic.diagnostic_version != 4
                or diagnostic.reported_model != usage.reported_model
            ):
                raise QualificationError()
            row.update(
                response_receipt_sha256=bytes_digest(canonical_bytes(response)),
                retained_response_sha256=response.raw.sha256,
                assessment_sha256=diagnostic.content_sha256,
                http_response_observed=True,
                diagnostic=diagnostic.model_dump(mode="json"),
                canonical_valid=diagnostic.canonical_valid,
                parser_diagnostic=diagnostic.parser_diagnostic,
                durable_evidence_consistent=True,
                response_model_identity_verified=usage.reported_model == plan.model,
                response_model_diagnostic="reported_model_missing"
                if usage.reported_model is None
                else "exact_model_version_match"
                if usage.reported_model == plan.model
                else "reported_model_mismatch",
            )
            terminal = (
                bool(results)
                and len(results) == len(task_ids)
                and {r.task_id for r in results} == set(task_ids)
                and all(
                    (r.run_id, r.request_uid, r.attempt_id)
                    == (
                        case.run_id,
                        case.transport.identity.request_uid,
                        case.transport.identity.attempt_id,
                    )
                    and r.status in {"complete", "invalid"}
                    and repository.get_task(case.run_id, r.task_id).state == r.status
                    for r in results
                )
                and summary.tasks == len(task_ids)
                and summary.attempts == 1
                and summary.results == len(task_ids)
                and summary.unresolved_submissions == 0
                and summary.state
                == (
                    "completed"
                    if all(r.status == "complete" for r in results)
                    else "partial"
                )
                and repository.get_attempt(
                    case.run_id, case.transport.identity.attempt_id
                ).state
                == "normalized"
                and repository.attempt_task_ids(
                    case.run_id, case.transport.identity.attempt_id
                )
                == task_ids
                and submission.certainty == "accepted"
                and submission.state == "resolved"
                and usage.provider_outcome == "completed"
                and receipt.invocation_acceptance is None
                and usage.invocation_failure_code is None
            )
            supported = (
                diagnostic.retention == "retained"
                and diagnostic.capture == "full"
                and diagnostic.component.finish_reason == "STOP"
                and not diagnostic.component.refusal_present
                and not diagnostic.component.tool_present
                and not diagnostic.component.unsupported_present
                and diagnostic.component.extraction_status == "extracted"
                and diagnostic.component.text_component_count
                == diagnostic.component.item_count
                == 1
            )
            valid = all(r.status == "complete" for r in results)
            if supported:
                # The strict retained/full diagnostic contract requires an
                # artifact; receipt canonicalization revalidates that invariant.
                independently_valid = _verify_assessment(
                    directory,
                    plan,
                    case,
                    artifacts.read(cast(ArtifactIdentity, diagnostic.artifact)),
                    results,
                )
                if independently_valid != diagnostic.canonical_valid:
                    raise QualificationError()
            row.update(
                assessment_terminal=terminal,
                assessment_continuation_allowed=bool(
                    terminal
                    and supported
                    and row["response_model_identity_verified"]
                    and diagnostic.canonical_valid == valid
                ),
                assessment_valid=bool(
                    terminal
                    and supported
                    and row["response_model_identity_verified"]
                    and diagnostic.canonical_valid
                    and valid
                ),
                status="completed"
                if submission.state == "resolved"
                and summary.unresolved_submissions == 0
                else "unresolved",
            )
            if (
                usage.input_tokens is not None
                and usage.output_tokens is not None
                and usage.reasoning_tokens is not None
                and usage.cache_read_tokens == 0
                and all(
                    b.value in (None, 0)
                    for b in usage.breakdown
                    if b.field == "toolUsePromptTokenCount"
                )
            ):
                row["estimated_actual_usd"] = str(
                    (
                        Decimal(usage.input_tokens)
                        * Decimal(plan.input_usd_per_million)
                        + Decimal(usage.output_tokens + usage.reasoning_tokens)
                        * Decimal(plan.output_usd_per_million_including_thinking)
                    )
                    / Decimal(1_000_000)
                )
            row["live_qualified"] = row["assessment_valid"]
            row["terminal_state"] = (
                "valid_assessment"
                if row["assessment_valid"]
                else "terminal_invalid"
                if row["status"] == "completed"
                else "incomplete_local_evidence"
            )
    except (PersistenceError, ValueError, OSError, sqlite3.Error):
        row.update(
            status="unresolved",
            evidence_integrity_failure=True,
            durable_evidence_consistent=False,
            assessment_valid=False,
            assessment_terminal=False,
            assessment_continuation_allowed=False,
            response_model_identity_verified=False,
            http_response_observed=False,
            live_qualified=False,
            usage=None,
            estimated_actual_usd=None,
            terminal_state="incomplete_local_evidence",
        )
    return row


def _estimated_exposure(
    plan: VertexQualificationPlanV2, rows: list[dict[str, Any]]
) -> Decimal:
    """Keep reservations for unobserved/unknown costs; never infer a zero bill."""
    exposure = Decimal(0)
    for case, row in zip(plan.calls, rows, strict=True):
        # Known components still constrain authorization when other counters or
        # cache prices are unknown. Use standard rates without any discount;
        # never turn this partial estimate into a reported counter or charge.
        known = Decimal(0)
        usage = row["usage"]
        if usage is not None:
            for name, rate in (
                ("input_tokens", plan.input_usd_per_million),
                ("output_tokens", plan.output_usd_per_million_including_thinking),
                ("reasoning_tokens", plan.output_usd_per_million_including_thinking),
            ):
                if usage[name] is not None:
                    known += Decimal(usage[name]) * Decimal(rate) / Decimal(1_000_000)
        observed = (
            Decimal(row["estimated_actual_usd"])
            if row["estimated_actual_usd"] is not None
            else Decimal(0)
        )
        exposure += max(case.reserved_estimated_usd, known, observed)
    return exposure


def report_execution(
    directory: Path, plan: VertexQualificationPlanV2, digest: str
) -> dict[str, object]:
    """Deterministic read-only JSON; missing evidence never counts as qualified."""
    consumed = _consumed(directory, plan, digest)
    rows = [
        _case_report(directory, plan, case, case.case_id in consumed)
        for case in plan.calls
    ]
    for row in rows:
        row["provenance_sha256"] = bytes_digest(canonical_bytes(row))
    qualified = all(row["assessment_valid"] for row in rows)
    attempted = [row for row in rows if row["allocation_consumed"]]
    estimate = (
        sum((Decimal(row["estimated_actual_usd"]) for row in attempted), Decimal(0))
        if attempted
        and all(row["estimated_actual_usd"] is not None for row in attempted)
        else None
    )
    totals = {
        name: sum(row["usage"][name] for row in attempted)
        if attempted
        and all(
            row["usage"] is not None and row["usage"][name] is not None
            for row in attempted
        )
        else None
        for name in _COUNTERS
    }
    report: dict[str, object] = {
        "report_version": 1,
        "provider": "vertex",
        "plan_sha256": digest,
        "source_commit": plan.source_commit,
        "authentication_mode": plan.authentication_mode,
        "credential_source": plan.credential_source,
        "route_sha256": plan.route_sha256,
        "location": plan.location,
        "authorization_sha256": bytes_digest(_authorization(plan, digest))
        if (_state_directory(directory) / "execution-authorization.json").exists()
        else None,
        "planned_requests": 4,
        "authorized_requests": 4
        if (_state_directory(directory) / "execution-authorization.json").exists()
        else 0,
        "calls": rows,
        "requested_model": plan.model,
        "endpoint": plan.endpoint,
        "api_version": plan.api_version,
        "sdk_package": plan.sdk_package,
        "sdk_version": plan.sdk_version,
        "execution_enabled": (
            _state_directory(directory) / "execution-authorization.json"
        ).exists(),
        "executed_requests": len(consumed),
        "physical_send_attempts": len(consumed),
        "attempted_requests": len(consumed),
        "confirmed_accepted_requests": sum(
            row["submission_certainty"] == "accepted"
            and row["durable_evidence_consistent"]
            for row in attempted
        ),
        "successful_assessments": sum(r["assessment_valid"] for r in rows),
        "invalid_assessments": sum(
            r["terminal_state"] == "terminal_invalid" for r in rows
        ),
        "uncertain_outcomes": sum(r["status"] == "unresolved" for r in rows),
        "exact_model_matches": sum(r["response_model_identity_verified"] for r in rows),
        "successful_text_cases": sum(
            r["assessment_valid"] and r["mode"] == "text" for r in rows
        ),
        "successful_native_cases": sum(
            r["assessment_valid"] and r["mode"] == "native" for r in rows
        ),
        "consumed_allocations": [
            c.case_id for c in plan.calls if c.case_id in consumed
        ],
        "physical_call_count_certainty": "conservative_upper_bound_includes_consumed_before_forwarding_crash_window",
        "actual_http_forwards": len(attempted)
        if all(row["http_response_observed"] for row in attempted)
        else None,
        "confirmed_http_response_count": sum(
            row["http_response_observed"] for row in attempted
        ),
        "successful_cases": [r["case_id"] for r in rows if r["assessment_valid"]],
        "unsuccessful_cases": [
            r["case_id"] for r in attempted if not r["assessment_valid"]
        ],
        "outstanding_cases": [
            r["case_id"] for r in rows if r["status"] == "outstanding"
        ],
        "unresolved_cases": [r["case_id"] for r in rows if r["status"] == "unresolved"],
        "completed_cases": [
            r["case_id"] for r in rows if r["status"] in {"completed", "unsuccessful"}
        ],
        "reported_usage_totals": totals,
        "estimated_actual_usd": str(estimate) if estimate is not None else None,
        "estimated_authorization_ceiling_usd": "0.02",
        "estimated_exposure_usd": str(_estimated_exposure(plan, rows)),
        "cost_is_invoiced": False,
        "live_qualified": qualified,
        "production_native_authority": False,
        "overall_qualification_result": "qualified"
        if qualified
        else "incomplete"
        if any(r["status"] in {"outstanding", "unresolved"} for r in rows)
        else "not_qualified",
        "model_identity_policy": "exact_modelVersion_only_no_reviewed_alias_mapping",
        "spending_limitations": "Conservative estimates are authorization policy, not provider-enforced billing caps. Thinking is billed as output; unknown usage or cache pricing remains unknown.",
    }
    report["report_sha256"] = bytes_digest(canonical_bytes(report))
    return report


def report_plan(directory: Path, *, expected_plan_sha256: str) -> dict[str, object]:
    """Verify a separately supplied digest and inspect retained evidence read-only."""
    plan, digest = _load_plan(directory, expected_plan_sha256)
    report = report_execution(directory.resolve(), plan, digest)
    if _source_commit() != plan.source_commit:
        raise QualificationError()
    return report


def _verify_quota_project(  # pyright: ignore[reportUnusedFunction]
    context: VertexProductionContext, quota_project: str | None
) -> None:
    """Qualification's ADC quota project must match the explicitly reviewed project.

    Inspect the transient SDK header only. No ADC identity/path is resolved or
    retained here; ordinary production routing remains unchanged.
    """
    if context.runtime.auth_mode == "adc" and quota_project != context.runtime.project:
        raise ProviderIntegrityError(
            "Vertex qualification quota project differs from the sealed project"
        )
