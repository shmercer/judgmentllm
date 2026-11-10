"""Private Gemini executor: explicit consent, one-shot forwarding, retained evidence.

Adapts the Anthropic qualification controls without changing its historical rules.
No general execution interface or qualification-registry grant is added here.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import importlib.metadata
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import field_validator

from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.gemini_qualification import (
    GeminiQualificationCallV1,
    GeminiQualificationPlanV1,
    QualificationError,
    _load_plan,
    _source_commit,
    _specification,
    report_plan,
)
from judgmentllm.gemini_transport import (
    GeminiProductionContext,
    PreparedRequestV6,
    _qualification_context,
    production_context_digest,
)
from judgmentllm.persistence.artifacts import ArtifactStore, _fsync_directory
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import SHA256, NotFoundError, PersistenceError
from judgmentllm.persistence.receipts import read_receipt
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.persistence.transport_receipts import read_transport_commitment
from judgmentllm.providers.protocol import PreparedRequest, ProviderIntegrityError
from judgmentllm.qualification import _immutable

_EXECUTOR_TOKEN = object()
_CEILING = Decimal("0.02")


class _Authorization(EvidenceModel):
    """Separate immutable consent; never alters the zero-authority proposal."""

    authorization_version: Literal[1] = 1
    executor: Literal["gemini-qualification-v1"] = "gemini-qualification-v1"
    plan_sha256: SHA256
    source_commit: str
    billable_calls_authorized: Literal[True] = True
    full_synthetic_assessment_capture_authorized: Literal[True] = True
    maximum_physical_requests: Literal[4] = 4
    maximum_output_tokens_per_call: Literal[512] = 512
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
    _fsync_directory(path.parent)


def _authorization(plan: GeminiQualificationPlanV1, digest: str) -> bytes:
    return canonical_bytes(
        _Authorization(plan_sha256=digest, source_commit=plan.source_commit)
    )


def _intent(
    plan: GeminiQualificationPlanV1, digest: str, case: GeminiQualificationCallV1
) -> bytes:
    return canonical_bytes(
        {
            "consumption_version": 1,
            "provider": "gemini",
            "plan_sha256": digest,
            "source_commit": plan.source_commit,
            "case_id": case.case_id,
            "submission_id": case.submission_id,
            "correlation_id": case.correlation_id,
            "payload_sha256": case.payload_sha256,
            "prompt_sha256": case.prompt_sha256,
        }
    )


def _authorize(directory: Path, plan: GeminiQualificationPlanV1, digest: str) -> None:
    path = directory / "execution-authorization.json"
    raw = _authorization(plan, digest)
    if path.exists():
        if path.read_bytes() != raw:
            raise QualificationError()
        # Existing authorization never allows reconstruction of missing state.
        _consumed(directory, plan, digest)
        return
    if any(
        (directory / name).exists()
        for name in (
            "qualification.sqlite3",
            "forward-intents",
            "ledger-created",
            "execution",
        )
    ):
        raise QualificationError()
    _durable(path, raw)
    (directory / "forward-intents").mkdir()
    _fsync_directory(directory)
    _durable(directory / "ledger-created", digest.encode())
    with closing(sqlite3.connect(directory / "qualification.sqlite3")) as connection:
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
            [(c.case_id, c.submission_id, c.payload_sha256) for c in plan.calls],
        )
        connection.commit()
    _fsync_directory(directory)


def _ledger_rows(
    connection: sqlite3.Connection, plan: GeminiQualificationPlanV1, digest: str
) -> list[tuple[Any, ...]]:
    if connection.execute(
        "SELECT plan_sha256,source_commit,plan_bytes FROM authorization"
    ).fetchall() != [(digest, plan.source_commit, canonical_bytes(plan))]:
        raise QualificationError()
    rows = connection.execute(
        "SELECT case_id,submission_id,payload_sha256,consumed_at FROM calls"
    ).fetchall()
    expected = {c.case_id: (c.submission_id, c.payload_sha256) for c in plan.calls}
    if len(rows) != 4 or any(expected.get(r[0]) != (r[1], r[2]) for r in rows):
        raise QualificationError()
    return rows


def _consumed(
    directory: Path, plan: GeminiQualificationPlanV1, digest: str
) -> set[str]:
    authorization = directory / "execution-authorization.json"
    ledger = directory / "qualification.sqlite3"
    intents = directory / "forward-intents"
    marker = directory / "ledger-created"
    if not authorization.exists():
        if any(p.exists() for p in (ledger, intents, marker, directory / "execution")):
            raise QualificationError()
        return set()
    if (
        authorization.read_bytes() != _authorization(plan, digest)
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
    prepared: PreparedRequest, case: GeminiQualificationCallV1
) -> None:
    if (
        type(prepared) is not PreparedRequestV6
        or (prepared.provider, prepared.endpoint, prepared.model)
        != ("gemini", "models.generate_content", "gemini-3.1-flash-lite")
        or prepared.request_uid != case.request_uid
        or prepared.task_ids != case.task_ids
        or prepared.payload != case.request_body_utf8.encode()
        or prepared.payload_sha256 != case.payload_sha256
        or bytes_digest(prepared.payload) != case.payload_sha256
        or prepared.prompt_sha256 != case.prompt_sha256
        or prepared.dependency_sha256 != case.dependency_sha256
    ):
        raise QualificationError()
    receipt = prepared.transport_commitment
    if (
        receipt.commitment_sha256 != case.transport_commitment_sha256
        or (
            receipt.identity.run_id,
            receipt.identity.attempt_id,
            receipt.identity.submission_id,
            receipt.identity.correlation_id,
        )
        != (case.run_id, case.attempt_id, case.submission_id, case.correlation_id)
        or receipt.mechanism != case.mechanism
    ):
        raise QualificationError()


class _QualificationSubmissionGuard:
    """Gemini-only executor authority, consumed once at actual HTTP forwarding."""

    def __init__(
        self,
        directory: Path,
        plan: GeminiQualificationPlanV1,
        case: GeminiQualificationCallV1,
        *,
        token: object,
    ) -> None:
        if token is not _EXECUTOR_TOKEN or case not in plan.calls:
            raise QualificationError()
        self.directory = directory
        self.plan = GeminiQualificationPlanV1.model_validate_json(canonical_bytes(plan))
        self.case = case
        self.digest = bytes_digest(canonical_bytes(plan))

    def verify_context(self, context: GeminiProductionContext) -> None:
        expected = _qualification_context(
            _specification(
                self.directory,
                self.case.task_kind,
                self.case.mode,
                self.plan.max_output_tokens,
            ),
            source_commit=self.plan.source_commit,
        )
        if type(context) is not GeminiProductionContext or production_context_digest(
            context
        ) != production_context_digest(expected):
            raise ProviderIntegrityError("Gemini qualification context is incompatible")

    def __call__(self, prepared: PreparedRequest) -> None:
        try:
            current, digest = _load_plan(self.directory)
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
            if _estimated_exposure(self.plan, prior) > self.plan.proposed_budget_usd:
                raise QualificationError()
            with closing(
                sqlite3.connect(self.directory / "qualification.sqlite3")
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
                    self.directory / "forward-intents" / (self.case.case_id + ".json"),
                    _intent(self.plan, digest, self.case),
                )
                connection.commit()
        except Exception:
            # Do not let filesystem, database or raw validation messages escape
            # through SDK exception wrapping or ordinary transport uncertainty.
            raise ProviderIntegrityError(
                "Gemini qualification forwarding denied"
            ) from None


async def execute_plan(
    directory: Path,
    *,
    authorized_plan_sha256: str,
    authorized_source_commit: str,
    accept_billable_calls: bool,
    accept_full_synthetic_capture: bool,
) -> dict[str, object]:
    """Sequential, at most four original calls; recovery never grants replacements."""
    from judgmentllm.execution import execute_run
    from judgmentllm.execution.retry import RetryPolicy
    from judgmentllm.preparation import open_prepared_run, prepare_run
    from judgmentllm.providers.gemini import GeminiAdapter
    from judgmentllm.transport import physical_identity

    plan, digest = _load_plan(directory)
    directory = directory.resolve()
    if (
        accept_billable_calls is not True
        or accept_full_synthetic_capture is not True
        or authorized_plan_sha256 != digest
        or authorized_source_commit != plan.source_commit
        or _source_commit() != plan.source_commit
        or importlib.metadata.version("google-genai") != plan.sdk_version
        or plan.proposed_budget_usd > _CEILING
    ):
        raise QualificationError()
    _authorize(directory, plan, digest)
    # Verify all four prepared commitments before any credential access. This
    # also admits retained source/transport artifacts via the existing engine.
    for case in plan.calls:
        spec = _specification(
            directory, case.task_kind, case.mode, plan.max_output_tokens
        )
        store = directory / "execution" / case.case_id
        context = _qualification_context(spec, source_commit=plan.source_commit)
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
                    physical_identity(case.run_id, case.request_uid, case.attempt_id),
                ),
                case,
            )
    for case in plan.calls:
        rows = cast(list[dict[str, Any]], report_plan(directory)["calls"])
        if _estimated_exposure(plan, rows) > plan.proposed_budget_usd:
            break
        row = next(r for r in rows if r["case_id"] == case.case_id)
        if row["assessment_continuation_allowed"]:
            continue
        if row["evidence_integrity_failure"]:
            break
        spec = _specification(
            directory, case.task_kind, case.mode, plan.max_output_tokens
        )
        adapter = GeminiAdapter(
            plan.endpoint,
            plan.model,
            context=_qualification_context(spec, source_commit=plan.source_commit),
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
                store=directory / "execution" / case.case_id,
                spec=spec,
                adapter=adapter,
                retry_policy=RetryPolicy(max_attempts=1),
            )
        finally:
            await adapter.aclose()
        row = next(
            r
            for r in cast(list[dict[str, Any]], report_plan(directory)["calls"])
            if r["case_id"] == case.case_id
        )
        if result.recovery_required or not row["assessment_continuation_allowed"]:
            break
    return report_plan(directory)


_COUNTERS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
    "cache_read_tokens",
)


def _case_report(
    directory: Path,
    plan: GeminiQualificationPlanV1,
    case: GeminiQualificationCallV1,
    consumed: bool,
) -> dict[str, Any]:
    from judgmentllm.preparation import _load_manifest, _verify_metadata

    row: dict[str, Any] = {
        "case_id": case.case_id,
        "run_id": case.run_id,
        "request_uid": case.request_uid,
        "attempt_id": case.attempt_id,
        "submission_id": case.submission_id,
        "correlation_id": case.correlation_id,
        "task_ids": list(case.task_ids),
        "mechanism": case.mechanism,
        "payload_sha256": case.payload_sha256,
        "prompt_sha256": case.prompt_sha256,
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
        "live_qualified": False,
    }
    store = directory / "execution" / case.case_id
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
            transport = read_transport_commitment(artifacts, case.attempt_id)
            if (
                transport is None
                or transport.commitment_sha256 != case.transport_commitment_sha256
            ):
                raise QualificationError()
            # Verify complete retained request and prompt bytes, not only hashes
            # named in mutable registry rows.
            for digest in (case.payload_sha256, case.prompt_sha256):
                artifacts.read(repository.get_artifact(case.run_id, digest))
            try:
                submission = repository.get_submission(case.run_id, case.submission_id)
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
                        case.run_id, case.submission_id
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
                case.request_uid,
                case.attempt_id,
                case.submission_id,
                case.correlation_id,
                case.payload_sha256,
            ):
                raise QualificationError()
            usage = receipt.usage
            row.update(
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
                return row
            response = read_receipt(artifacts, case.submission_id)
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
                or diagnostic.diagnostic_version != 3
                or diagnostic.reported_model != usage.reported_model
            ):
                raise QualificationError()
            row.update(
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
                and len(results) == len(case.task_ids)
                and {r.task_id for r in results} == set(case.task_ids)
                and all(
                    (r.run_id, r.request_uid, r.attempt_id)
                    == (case.run_id, case.request_uid, case.attempt_id)
                    and r.status in {"complete", "invalid"}
                    and repository.get_task(case.run_id, r.task_id).state == r.status
                    for r in results
                )
                and summary.tasks == len(case.task_ids)
                and summary.attempts == 1
                and summary.results == len(case.task_ids)
                and summary.unresolved_submissions == 0
                and summary.state
                == (
                    "completed"
                    if all(r.status == "complete" for r in results)
                    else "partial"
                )
                and repository.get_attempt(case.run_id, case.attempt_id).state
                == "normalized"
                and repository.attempt_task_ids(case.run_id, case.attempt_id)
                == case.task_ids
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
        )
    return row


def _estimated_exposure(
    plan: GeminiQualificationPlanV1, rows: list[dict[str, Any]]
) -> Decimal:
    """Keep reservations for unobserved/unknown costs; never infer a zero bill."""
    return sum(
        (
            max(case.reserved_estimated_usd, Decimal(row["estimated_actual_usd"]))
            if row["estimated_actual_usd"] is not None
            else case.reserved_estimated_usd
            for case, row in zip(plan.calls, rows, strict=True)
        ),
        Decimal(0),
    )


def report_execution(
    directory: Path, plan: GeminiQualificationPlanV1, digest: str
) -> dict[str, object]:
    """Deterministic read-only JSON; missing evidence never counts as qualified."""
    consumed = _consumed(directory, plan, digest)
    rows = [
        _case_report(directory, plan, case, case.case_id in consumed)
        for case in plan.calls
    ]
    qualified = all(row["assessment_valid"] for row in rows)
    attempted = [row for row in rows if row["allocation_consumed"]]
    estimate = (
        sum((Decimal(row["estimated_actual_usd"]) for row in attempted), Decimal(0))
        if all(row["estimated_actual_usd"] is not None for row in attempted)
        else None
    )
    totals = {
        name: sum(row["usage"][name] for row in attempted)
        if all(
            row["usage"] is not None and row["usage"][name] is not None
            for row in attempted
        )
        else None
        for name in _COUNTERS
    }
    return {
        "report_version": 1,
        "calls": rows,
        "requested_model": plan.model,
        "endpoint": plan.endpoint,
        "api_version": plan.api_version,
        "sdk_package": plan.sdk_package,
        "sdk_version": plan.sdk_version,
        "execution_enabled": (directory / "execution-authorization.json").exists(),
        "executed_requests": len(consumed),
        "physical_send_attempts": len(consumed),
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
        "overall_qualification_result": "qualified"
        if qualified
        else "incomplete"
        if any(r["status"] in {"outstanding", "unresolved"} for r in rows)
        else "not_qualified",
        "model_identity_policy": "exact_modelVersion_only_no_reviewed_alias_mapping",
        "spending_limitations": "Conservative estimates are authorization policy, not provider-enforced billing caps. Thinking is billed as output; unknown usage or cache pricing remains unknown.",
    }
