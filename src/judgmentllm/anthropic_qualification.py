"""Explicitly authorized, synthetic Anthropic qualification; never ordinary execution.

This development harness is separate from the public run/resume interfaces. A
plan grants no authority. Only execute with the exact plan and source digests can
create the private HTTP-boundary guard. Reporting opens retained evidence read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from pydantic import ConfigDict, Field, field_validator, model_validator

from judgmentllm.config import RunSpecV3
from judgmentllm.models import StrictModel
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import SHA256, NotFoundError, PersistenceError
from judgmentllm.planning import stable_id
from judgmentllm.providers.protocol import PreparedRequest

if TYPE_CHECKING:
    from judgmentllm.anthropic_transport import AnthropicProductionContext

# Private APIs deliberately do not enter the top-level public package.
# pyright: reportPrivateUsage=false

_COMMIT = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
_ENDPOINT = Literal["messages"]
_MONEY = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
_EXECUTOR_TOKEN = object()
_SYNTHETIC_ITEMS = (
    {"item_id": "001", "text": "The seed grew because water helped it."},
    {"item_id": "002", "text": "Water helped the seed grow."},
)


class QualificationError(ValueError):
    """Content-free admission failure; never render raw exception details."""

    def __init__(self) -> None:
        super().__init__(
            "Qualification plan, authorization, or evidence is incompatible."
        )


class _Frozen(StrictModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    @field_validator(
        "profile_version",
        "plan_version",
        "automatic_retries",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def exact_integer_versions(cls, value: object) -> object:
        if type(value) is not int:
            raise QualificationError()
        return value


class _PricingProfile(_Frozen):
    """Reviewed assumptions, not provider capability or billing authority.

    The context is descriptive model evidence. Per-case input reservations are
    explicitly estimated from complete sealed request bodies and overhead;
    neither a provider-enforced input limit nor a guaranteed monetary cap.
    """

    profile_version: Literal[1] = 1
    provider: Literal["anthropic"] = "anthropic"
    model: str = Field(min_length=1, max_length=200)
    endpoints: tuple[_ENDPOINT, ...]
    reviewed_on: date
    context_tokens: int = Field(gt=0)
    maximum_output_tokens: int = Field(gt=0)
    input_usd_per_million: _MONEY
    output_usd_per_million: _MONEY
    documentation_urls: tuple[str, ...] = Field(min_length=1)
    output_bound_includes_reasoning: Literal[True]

    @model_validator(mode="after")
    def supported_scope(self) -> _PricingProfile:
        if (
            set(self.endpoints) != {"messages"}
            or len(self.endpoints) != 1
            or self.reviewed_on > datetime.now(UTC).date()
            or self.input_usd_per_million <= 0
            or self.output_usd_per_million <= 0
            or any(
                not url.startswith(
                    ("https://platform.claude.com/", "https://www.anthropic.com/")
                )
                for url in self.documentation_urls
            )
        ):
            raise QualificationError()
        return self


class _PlannedCall(_Frozen):
    case_id: str
    run_id: str
    provider: Literal["anthropic"] = "anthropic"
    endpoint: _ENDPOINT
    model: str
    task_kind: Literal["rubric", "pairwise"]
    mode: Literal["text", "native"]
    mechanism: Literal["text", "anthropic.messages.output_config.format"]
    request_uid: str
    task_ids: tuple[str, ...]
    attempt_id: str
    submission_id: str
    correlation_id: str
    dependency_sha256: SHA256
    prompt_sha256: SHA256
    payload_sha256: SHA256
    request_body_utf8: str = Field(repr=False)
    max_output_tokens: int = Field(gt=0, le=512)
    reserved_input_tokens: int = Field(gt=0)
    input_allowance_evidence: dict[str, int | str | bool]
    reserved_tokens: int = Field(gt=0)
    reserved_estimated_usd: _MONEY

    @model_validator(mode="after")
    def exact_body(self) -> _PlannedCall:
        if bytes_digest(self.request_body_utf8.encode("utf-8")) != self.payload_sha256:
            raise QualificationError()
        if self.mode == "text":
            expected = "text"
        else:
            expected = "anthropic.messages.output_config.format"
        if self.mechanism != expected or self.attempt_id != stable_id(
            "attempt", self.run_id, self.request_uid, 1
        ):
            raise QualificationError()
        from judgmentllm.transport import physical_identity

        identity = physical_identity(self.run_id, self.request_uid, self.attempt_id)
        if (self.submission_id, self.correlation_id) != (
            identity.submission_id,
            identity.correlation_id,
        ):
            raise QualificationError()
        return self


class _QualificationPlan(_Frozen):
    plan_version: Literal[1, 2] = 2
    source_commit: _COMMIT
    sdk_package: Literal["anthropic"] = "anthropic"
    sdk_version: Literal["2.3.0"] = "2.3.0"
    created_at: str
    provider: Literal["anthropic"] = "anthropic"
    model: str
    endpoints: tuple[_ENDPOINT, ...]
    synthetic_items: tuple[dict[str, str], ...] = Field(repr=False)
    capture: Literal["full"] = "full"
    api_version: Literal["2026-10-21"] = "2026-10-21"
    reasoning_retention: Literal["none"] = "none"
    automatic_retries: Literal[0] = 0
    max_requests: int = Field(gt=0, le=4)
    max_output_tokens: int = Field(gt=0, le=512)
    max_total_tokens: int = Field(gt=0)
    max_estimated_usd: _MONEY
    pricing: _PricingProfile
    calls: tuple[_PlannedCall, ...]

    @model_validator(mode="after")
    def exact_initial_matrix(self) -> _QualificationPlan:
        expected = {
            (endpoint, task, mode)
            for endpoint in ("messages",)
            for task in ("rubric", "pairwise")
            for mode in ("text", "native")
        }
        if (
            len(self.calls) != 4
            or self.max_requests != 4
            or set(self.endpoints) != {"messages"}
            or len(self.endpoints) != 1
            or tuple(self.synthetic_items) != _SYNTHETIC_ITEMS
            or {(c.endpoint, c.task_kind, c.mode) for c in self.calls} != expected
            or len({c.case_id for c in self.calls}) != 4
            or len({c.submission_id for c in self.calls}) != 4
            or self.model != self.pricing.model
            or self.max_output_tokens > self.pricing.maximum_output_tokens
        ):
            raise QualificationError()
        for call in self.calls:
            if (
                call.case_id != f"{call.endpoint}-{call.task_kind}-{call.mode}"
                or call.run_id
                != (
                    "qualification-" + call.case_id
                    if self.plan_version == 1
                    else f"qualification-{self.source_commit}-{call.case_id}"
                )
                or call.model != self.model
                or call.max_output_tokens != self.max_output_tokens
                or call.reserved_tokens
                != call.reserved_input_tokens + self.max_output_tokens
                or call.reserved_estimated_usd
                != _reservation(
                    self.pricing, self.max_output_tokens, call.reserved_input_tokens
                )
                or (call.reserved_input_tokens, call.input_allowance_evidence)
                != _input_allowance(call.request_body_utf8)
            ):
                raise QualificationError()
        if (
            sum(c.reserved_tokens for c in self.calls) > self.max_total_tokens
            or sum((c.reserved_estimated_usd for c in self.calls), Decimal(0))
            > self.max_estimated_usd
        ):
            raise QualificationError()
        return self


def _input_allowance(body_utf8: str) -> tuple[int, dict[str, int | str | bool]]:
    """Reviewable allowance, explicitly not an exact tokenizer or API input cap.

    Count the complete serialized request, including JSON framing and controls.
    Count the schema again to allow for provider-side format instructions, then
    reserve 4096 additional tokens for framing/injected instructions. A one-token
    per UTF-8-byte assumption deliberately overcounts ordinary synthetic prose;
    provider tokenization and injected overhead have not been independently
    proven. The floor is 8192 and larger requests round up to 1024-token units.
    No request-side input-limit field exists and no count API is invoked.
    """
    from judgmentllm.output_commitments import ordered_json_bytes
    from judgmentllm.output_contracts import decode_assessment_output

    body = decode_assessment_output(body_utf8)
    if not isinstance(body, dict):
        raise QualificationError()
    values = cast(dict[str, Any], body)
    schema_bytes = (
        len(ordered_json_bytes(values["output_config"]))
        if "output_config" in values
        else 0
    )
    body_bytes = len(body_utf8.encode("utf-8"))
    allowance = max(8192, ((body_bytes + schema_bytes + 4096 + 1023) // 1024) * 1024)
    return allowance, {
        "method": "anthropic-synthetic-byte-allowance-v1",
        "complete_body_utf8_bytes": body_bytes,
        "additional_schema_utf8_bytes": schema_bytes,
        "framing_and_injected_instruction_allowance": 4096,
        "estimated_input_token_allowance": allowance,
        "provider_enforced_input_limit": False,
        "exact_provider_tokenizer_verified": False,
        "assumption": "one token per UTF-8 byte plus schema repetition and fixed overhead; conservative estimate, not a proved billing bound",
    }


def _reservation(
    profile: _PricingProfile, output_tokens: int, input_tokens: int = 8192
) -> Decimal:
    return (
        Decimal(input_tokens) * profile.input_usd_per_million
        + Decimal(output_tokens) * profile.output_usd_per_million
    ) / Decimal(1_000_000)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _immutable(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _read_model(
    path: Path, model: type[_QualificationPlan] | type[_PricingProfile]
) -> _QualificationPlan | _PricingProfile:
    raw = path.read_bytes()
    # Reuse strict duplicate-key admission before Pydantic's JSON-mode date and
    # Decimal conversions. A rehashed duplicate document is never admissible.
    decode_assessment_output(raw)
    return model.model_validate_json(raw)


def _source_commit() -> str:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    dirty = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    if dirty.stdout:
        raise QualificationError()
    return result.stdout.strip()


def _specification(
    directory: Path,
    endpoint: _ENDPOINT,
    model: str,
    task: str,
    mode: str,
    output_tokens: int,
) -> RunSpecV3:
    values: dict[str, object] = {
        "schema_version": 3,
        "task_kind": task,
        "input": str(
            directory
            / ("rubric-responses.jsonl" if task == "rubric" else "responses.jsonl")
        ),
        "provider": "anthropic",
        "endpoint": endpoint,
        "model": model,
        "generation": {"max_output_tokens": output_tokens},
        "execution": {
            "mode": "live",
            "concurrency": 1,
            "explanations": False,
            "tier": "standard_only",
            "cache_policy": "off",
        },
        "prompt": {
            "structured_output": mode,
            "native_mechanism": "anthropic.messages.output_config.format",
        },
        "capture": "full",
        "output_path": str(directory / "runs"),
    }
    if task == "rubric":
        values["rubric"] = {
            "rubric_id": "001",
            "name": "Synthetic clarity rubric",
            "traits": [
                {
                    "trait_id": "01",
                    "name": "Clarity",
                    "description": "Assess whether the short response clearly states its idea.",
                    "levels": [
                        {"label": "low", "numeric_value": 1},
                        {"label": "middle", "numeric_value": 3},
                        {"label": "high", "numeric_value": 8},
                    ],
                }
            ],
        }
    else:
        values["criteria"] = [
            {
                "trait_id": "01",
                "name": "Clarity",
                "description": "Choose the response that states its idea more clearly.",
            }
        ]
        values["pairs"] = str(directory / "pairs.jsonl")
    return RunSpecV3.model_validate(values)


def _verify_inputs(directory: Path) -> None:
    expected = {
        "responses.jsonl": b"".join(
            _json_bytes(item) + b"\n" for item in _SYNTHETIC_ITEMS
        ),
        "rubric-responses.jsonl": _json_bytes(_SYNTHETIC_ITEMS[0]) + b"\n",
        "pairs.jsonl": _json_bytes(
            {
                "pair_uid": "001",
                "pair_id": "001",
                "left_item_id": "001",
                "right_item_id": "002",
                "trait_id": "01",
            }
        )
        + b"\n",
    }
    for name, content in expected.items():
        path = directory / name
        if path.is_symlink() or path.read_bytes() != content:
            raise QualificationError()


def _verify_case(directory: Path, case: _PlannedCall) -> RunSpecV3:
    expected = _specification(
        directory,
        case.endpoint,
        case.model,
        case.task_kind,
        case.mode,
        case.max_output_tokens,
    )
    path = directory / (case.case_id + ".json")
    if path.is_symlink() or path.read_bytes() != canonical_bytes(expected):
        raise QualificationError()
    return expected


def _materialize_inputs(directory: Path) -> None:
    _immutable(
        directory / "responses.jsonl",
        b"".join(_json_bytes(item) + b"\n" for item in _SYNTHETIC_ITEMS),
    )
    _immutable(
        directory / "rubric-responses.jsonl", _json_bytes(_SYNTHETIC_ITEMS[0]) + b"\n"
    )
    _immutable(
        directory / "pairs.jsonl",
        _json_bytes(
            {
                "pair_uid": "001",
                "pair_id": "001",
                "left_item_id": "001",
                "right_item_id": "002",
                "trait_id": "01",
            }
        )
        + b"\n",
    )


def build_plan(
    *,
    directory: Path,
    source_commit: str,
    provider: str,
    endpoints: tuple[_ENDPOINT, ...],
    model: str,
    pricing: _PricingProfile,
    max_requests: int,
    max_output_tokens: int,
    max_total_tokens: int,
    max_estimated_usd: Decimal,
) -> _QualificationPlan:
    """Prepare all exact synthetic bodies offline; create no execution authority."""
    from judgmentllm.anthropic_transport import _qualification_context
    from judgmentllm.preparation import prepare_run
    from judgmentllm.transport import physical_identity

    if provider != "anthropic" or set(endpoints) != {"messages"} or len(endpoints) != 1:
        raise QualificationError()
    if (
        importlib.metadata.version("anthropic") != "2.3.0"
        or source_commit != _source_commit()
    ):
        raise QualificationError()
    if max_requests != 4 or max_output_tokens > min(512, pricing.maximum_output_tokens):
        raise QualificationError()
    if (
        pricing.model != model
        or 4 * (8192 + max_output_tokens) > max_total_tokens
        or 4 * _reservation(pricing, max_output_tokens) > max_estimated_usd
    ):
        raise QualificationError()
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    _materialize_inputs(directory)
    calls: list[_PlannedCall] = []
    for endpoint in endpoints:
        for task in ("rubric", "pairwise"):
            for mode in ("text", "native"):
                case_id = f"{endpoint}-{task}-{mode}"
                run_id = f"qualification-{source_commit}-{case_id}"
                spec = _specification(
                    directory, endpoint, model, task, mode, max_output_tokens
                )
                _immutable(directory / (case_id + ".json"), canonical_bytes(spec))
                context = _qualification_context(spec, source_commit=source_commit)
                with prepare_run(
                    spec,
                    store=directory / "offline-preparation" / case_id,
                    run_id=run_id,
                    transport_context=context,
                ) as prepared:
                    dependencies = tuple(prepared.iter_request_dependencies())
                    if len(dependencies) != 1:
                        raise QualificationError()
                    dependency = dependencies[0]
                    identity = physical_identity(
                        run_id,
                        dependency.request.request_uid,
                        stable_id("attempt", run_id, dependency.request.request_uid, 1),
                    )
                    wire = prepared.prepare_transport_attempt(dependency, identity)
                    input_allowance, allowance_evidence = _input_allowance(
                        wire.payload.decode("utf-8")
                    )
                    calls.append(
                        _PlannedCall(
                            case_id=case_id,
                            run_id=run_id,
                            endpoint=endpoint,
                            model=model,
                            task_kind=task,
                            mode=mode,
                            mechanism=cast(
                                Literal[
                                    "text",
                                    "anthropic.messages.output_config.format",
                                ],
                                "text"
                                if mode == "text"
                                else spec.prompt.native_mechanism,
                            ),
                            request_uid=wire.request_uid,
                            task_ids=wire.task_ids,
                            attempt_id=identity.attempt_id,
                            submission_id=identity.submission_id,
                            correlation_id=identity.correlation_id,
                            dependency_sha256=wire.dependency_sha256,
                            prompt_sha256=wire.prompt_sha256,
                            payload_sha256=wire.payload_sha256,
                            request_body_utf8=wire.payload.decode("utf-8"),
                            max_output_tokens=max_output_tokens,
                            reserved_input_tokens=input_allowance,
                            input_allowance_evidence=allowance_evidence,
                            reserved_tokens=input_allowance + max_output_tokens,
                            reserved_estimated_usd=_reservation(
                                pricing, max_output_tokens, input_allowance
                            ),
                        )
                    )
    plan = _QualificationPlan(
        source_commit=source_commit,
        created_at=datetime.now(UTC).isoformat(),
        model=model,
        endpoints=endpoints,
        synthetic_items=_SYNTHETIC_ITEMS,
        pricing=pricing,
        max_requests=max_requests,
        max_output_tokens=max_output_tokens,
        max_total_tokens=max_total_tokens,
        max_estimated_usd=max_estimated_usd,
        calls=tuple(calls),
    )
    _immutable(directory / "plan.json", canonical_bytes(plan))
    return plan


def _load_plan(directory: Path) -> tuple[_QualificationPlan, str]:
    if directory.is_symlink() or (directory / "plan.json").is_symlink():
        raise QualificationError()
    plan = cast(
        _QualificationPlan, _read_model(directory / "plan.json", _QualificationPlan)
    )
    raw = (directory / "plan.json").read_bytes()
    if raw != canonical_bytes(plan):
        raise QualificationError()
    return plan, bytes_digest(raw)


def _authorize(directory: Path, plan: _QualificationPlan, digest: str) -> None:
    authorization = canonical_bytes(
        {
            "authorization_version": 1,
            "plan_sha256": digest,
            "source_commit": plan.source_commit,
            "billable_calls_authorized": True,
            "max_requests": plan.max_requests,
            "max_output_tokens": plan.max_output_tokens,
            "max_total_tokens": plan.max_total_tokens,
            "max_estimated_usd": str(plan.max_estimated_usd),
        }
    )
    path = directory / "execution-authorization.json"
    if path.exists():
        if path.is_symlink() or path.read_bytes() != authorization:
            raise QualificationError()
    else:
        _immutable(path, authorization)
    intents = directory / "forward-intents"
    if intents.is_symlink():
        raise QualificationError()
    intents.mkdir(exist_ok=True)
    ledger = directory / "qualification.sqlite3"
    # The authorization file is immutable. A missing ledger after authorization
    # may represent deletion of prior counters; never reconstruct it.
    fresh = not ledger.exists()
    if fresh and (directory / "ledger-created").exists():
        raise QualificationError()
    if ledger.is_symlink():
        raise QualificationError()
    with closing(sqlite3.connect(ledger)) as connection:
        connection.execute("PRAGMA synchronous=FULL")
        if fresh:
            _immutable(directory / "ledger-created", digest.encode("ascii"))
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
        stored = connection.execute(
            "SELECT plan_sha256,source_commit,plan_bytes FROM authorization"
        ).fetchall()
        if stored != [(digest, plan.source_commit, canonical_bytes(plan))]:
            raise QualificationError()


class _QualificationSubmissionGuard:
    """Private executor authority consumed exactly once at the HTTP boundary."""

    def __init__(
        self,
        directory: Path,
        plan: _QualificationPlan,
        case: _PlannedCall,
        *,
        token: object,
    ) -> None:
        if token is not _EXECUTOR_TOKEN or case not in plan.calls:
            raise QualificationError()
        self.directory = directory
        self.plan = _QualificationPlan.model_validate_json(canonical_bytes(plan))
        self.case = case
        self.plan_sha256 = bytes_digest(canonical_bytes(plan))
        self.source_commit = plan.source_commit

    def verify_context(self, context: AnthropicProductionContext) -> None:
        from judgmentllm.anthropic_transport import AnthropicProductionContext

        if (
            type(context) is not AnthropicProductionContext
            or context.kind != "anthropic-qualification-v1"
            or context.qualification_plan_sha256 != self.plan_sha256
            or context.source_commit != self.source_commit
            or (context.scope.provider, context.scope.endpoint, context.scope.model)
            != ("anthropic", self.case.endpoint, self.case.model)
        ):
            raise QualificationError()

    def __call__(self, prepared: PreparedRequest) -> None:
        current, digest = _load_plan(self.directory)
        if (
            digest != self.plan_sha256
            or current != self.plan
            or _source_commit() != self.source_commit
        ):
            raise QualificationError()
        case = self.case
        if (
            (prepared.provider, prepared.endpoint, prepared.model)
            != (case.provider, case.endpoint, case.model)
            or prepared.request_uid != case.request_uid
            or prepared.task_ids != case.task_ids
            or prepared.payload_sha256 != case.payload_sha256
            or prepared.payload != case.request_body_utf8.encode("utf-8")
            or prepared.prompt_sha256 != case.prompt_sha256
            or prepared.dependency_sha256 != case.dependency_sha256
        ):
            raise QualificationError()
        receipt = getattr(prepared, "transport_commitment", None)
        if (
            receipt is None
            or receipt.identity.submission_id != case.submission_id
            or receipt.identity.attempt_id != case.attempt_id
            or receipt.identity.correlation_id != case.correlation_id
            or receipt.mechanism != case.mechanism
        ):
            raise QualificationError()
        path = self.directory / "qualification.sqlite3"
        if not path.is_file() or path.is_symlink():
            raise QualificationError()
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            stored = connection.execute(
                "SELECT plan_sha256,source_commit,plan_bytes FROM authorization"
            ).fetchall()
            if stored != [(digest, self.source_commit, canonical_bytes(self.plan))]:
                raise QualificationError()
            rows = connection.execute(
                "SELECT case_id,submission_id,payload_sha256,consumed_at FROM calls"
            ).fetchall()
            if (
                len(rows) != len(self.plan.calls)
                or sum(r[3] is not None for r in rows) >= self.plan.max_requests
            ):
                raise QualificationError()
            expected = {
                c.case_id: (c.submission_id, c.payload_sha256) for c in self.plan.calls
            }
            if any(expected.get(r[0]) != (r[1], r[2]) for r in rows):
                raise QualificationError()
            changed = connection.execute(
                "UPDATE calls SET consumed_at=? WHERE case_id=? AND submission_id=? AND payload_sha256=? AND consumed_at IS NULL",
                (
                    datetime.now(UTC).isoformat(),
                    case.case_id,
                    case.submission_id,
                    case.payload_sha256,
                ),
            ).rowcount
            if changed != 1:
                raise QualificationError()
            # A separate exclusive intent prevents a rehashed/reset SQLite row
            # from replaying an already authorized forward. A crash here consumes
            # the allocation conservatively even if the transaction rolls back.
            _immutable(
                self.directory / "forward-intents" / (case.case_id + ".json"),
                canonical_bytes(
                    {
                        "plan_sha256": digest,
                        "source_commit": self.source_commit,
                        "submission_id": case.submission_id,
                        "payload_sha256": case.payload_sha256,
                    }
                ),
            )
            connection.commit()


def _consumed(directory: Path) -> set[str]:
    ledger = directory / "qualification.sqlite3"
    if not ledger.exists():
        if (directory / "ledger-created").exists():
            raise QualificationError()
        return set()
    if ledger.is_symlink():
        raise QualificationError()
    plan, digest = _load_plan(directory)
    with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as connection:
        stored = connection.execute(
            "SELECT plan_sha256,source_commit,plan_bytes FROM authorization"
        ).fetchall()
        if stored != [(digest, plan.source_commit, canonical_bytes(plan))]:
            raise QualificationError()
        rows = connection.execute(
            "SELECT case_id,submission_id,payload_sha256,consumed_at FROM calls"
        ).fetchall()
        expected_rows = {
            c.case_id: (c.submission_id, c.payload_sha256) for c in plan.calls
        }
        if len(rows) != len(plan.calls) or any(
            expected_rows.get(row[0]) != (row[1], row[2]) for row in rows
        ):
            raise QualificationError()
        consumed = {str(row[0]) for row in rows if row[3] is not None}
    intents = directory / "forward-intents"
    if intents.is_symlink():
        raise QualificationError()
    for call in plan.calls:
        intent = intents / (call.case_id + ".json")
        if intent.exists():
            expected = canonical_bytes(
                {
                    "plan_sha256": digest,
                    "source_commit": plan.source_commit,
                    "submission_id": call.submission_id,
                    "payload_sha256": call.payload_sha256,
                }
            )
            if intent.is_symlink() or intent.read_bytes() != expected:
                raise QualificationError()
            consumed.add(call.case_id)
        elif call.case_id in consumed:
            raise QualificationError()
    return consumed


async def execute_plan(
    directory: Path,
    *,
    authorized_plan_sha256: str,
    authorized_source_commit: str,
    accept_billable_calls: bool,
) -> dict[str, object]:
    """Execute only untouched approved requests; continuation never replaces calls."""
    from judgmentllm.anthropic_transport import _qualification_context
    from judgmentllm.execution import execute_run, recover_run
    from judgmentllm.execution.retry import RetryPolicy
    from judgmentllm.lifecycle import export_run_results
    from judgmentllm.preparation import prepare_run
    from judgmentllm.providers.anthropic import AnthropicAdapter

    directory = directory.resolve()
    plan, digest = _load_plan(directory)
    if (
        plan.plan_version != 2
        or accept_billable_calls is not True
        or authorized_plan_sha256 != digest
        or authorized_source_commit != plan.source_commit
        or _source_commit() != plan.source_commit
        or importlib.metadata.version("anthropic") != plan.sdk_version
    ):
        raise QualificationError()
    _verify_inputs(directory)
    for case in plan.calls:
        _verify_case(directory, case)
    _authorize(directory, plan, digest)
    for case in plan.calls:
        spec = _verify_case(directory, case)
        _consumed(directory)
        existing = report_plan(directory)
        prior = next(
            item
            for item in cast(list[dict[str, object]], existing["calls"])
            if item["case_id"] == case.case_id
        )
        if prior["evidence_integrity_failure"]:
            break
        context = _qualification_context(
            spec, source_commit=plan.source_commit, plan_sha256=digest
        )
        guard = _QualificationSubmissionGuard(
            directory, plan, case, token=_EXECUTOR_TOKEN
        )
        adapter = AnthropicAdapter(
            case.endpoint,
            case.model,
            context=context,
            concurrency=1,
            submission_guard=guard,
        )
        store = directory / "execution" / case.case_id
        try:
            if (store / "registry.sqlite3").exists():
                # Recovery may consume retained evidence; the same one-shot guard
                # rejects every attempted replacement of a consumed call.
                result = await recover_run(
                    case.run_id,
                    store=store,
                    adapter=adapter,
                    spec=spec,
                    retry_policy=RetryPolicy(max_attempts=1),
                )
            else:
                with prepare_run(
                    spec, store=store, run_id=case.run_id, transport_context=context
                ):
                    pass
                result = await execute_run(
                    case.run_id,
                    store=store,
                    adapter=adapter,
                    spec=spec,
                    retry_policy=RetryPolicy(max_attempts=1),
                )
            export_run_results(case.run_id, store=store)
            # Execution state alone cannot authorize the next physical call.
            # A completed provider observation with a terminal invalid assessment
            # is useful qualification evidence, but unresolved work is not.
            retained = report_plan(directory)
            row = next(
                item
                for item in cast(list[dict[str, object]], retained["calls"])
                if item["case_id"] == case.case_id
            )
            if result.recovery_required or not row["assessment_continuation_allowed"]:
                break
        finally:
            await adapter.aclose()
    return report_plan(directory)


def report_plan(directory: Path) -> dict[str, object]:
    """Inspect plan, counters and retained receipts only; no mutation or clients."""
    from judgmentllm.persistence.repository import ReadOnlyRepository

    directory = directory.resolve()
    plan, digest = _load_plan(directory)
    consumed = _consumed(directory)
    reports: list[dict[str, object]] = []
    estimated_total: Decimal | None = Decimal(0)
    totals: dict[str, int | None] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": None,
    }
    for case in plan.calls:
        item: dict[str, object] = {
            "case_id": case.case_id,
            "run_id": case.run_id,
            "request_uid": case.request_uid,
            "attempt_id": case.attempt_id,
            "submission_id": case.submission_id,
            "correlation_id": case.correlation_id,
            "task_ids": list(case.task_ids),
            "endpoint": case.endpoint,
            "requested_model": case.model,
            "mechanism": case.mechanism,
            "payload_sha256": case.payload_sha256,
            "actual_request_body_digest_verified": case.case_id in consumed,
            "max_output_tokens": case.max_output_tokens,
            "reserved_tokens": case.reserved_tokens,
            "reserved_input_tokens": case.reserved_input_tokens,
            "input_allowance_evidence": case.input_allowance_evidence,
            "reserved_estimated_usd": str(case.reserved_estimated_usd),
            "physical_send_attempted": case.case_id in consumed,
            "outcome": "pending",
            "live_qualified": False,
            "provider_accepted": None,
            "acceptance_outcome": "not_attempted",
            "provider_response_observed": False,
            "submission_certainty": "not_started",
            "invocation_acceptance": None,
            "provider_outcome": None,
            "invocation_failure_code": None,
            "completion_status": "not_attempted",
            "http_response_received": False,
            "durable_evidence_consistent": False,
            "evidence_integrity_failure": False,
            "evidence_error_code": None,
            "local_assessment_valid": False,
            "assessment_terminal": False,
            "assessment_diagnostic_available": False,
            "parser_diagnostic": None,
            "canonical_valid": None,
            "assessment_source": None,
            "assessment_extraction_status": None,
            "assessment_retention": None,
            "assessment_sha256": None,
            "assessment_byte_length": None,
            "assessment_artifact": None,
            "assessment_component_metadata": None,
            "assessment_continuation_allowed": False,
            "recovery_required": case.case_id in consumed,
        }
        terminal_assessment_observed = False
        store = directory / "execution" / case.case_id
        if (store / "registry.sqlite3").is_file():
            try:
                with ReadOnlyRepository(store, preserve_files=True) as repository:
                    results = tuple(repository.iter_results(case.run_id))
                    receipts = tuple(repository.iter_evidence(case.run_id))
                    summary = repository.summary(case.run_id)
                    item["outcome"] = summary.state
                    item["recovery_required"] = bool(
                        summary.unresolved_submissions
                        or summary.state in {"created", "submitted", "running"}
                    )
                    try:
                        submission = repository.get_submission(
                            case.run_id, case.submission_id
                        )
                    except NotFoundError:
                        submission = None
                    if submission is not None:
                        item["submission_certainty"] = submission.certainty
                    item["normalized_outcomes"] = [r.status for r in results]
                    item["local_assessment_valid"] = bool(results) and all(
                        r.status == "complete" for r in results
                    )
                    item["durable_evidence_consistent"] = len(receipts) == 1 and (
                        receipts[0].identity.run_id,
                        receipts[0].identity.request_uid,
                        receipts[0].identity.attempt_id,
                        receipts[0].identity.submission_id,
                        receipts[0].correlation_id,
                        receipts[0].payload_sha256,
                    ) == (
                        case.run_id,
                        case.request_uid,
                        case.attempt_id,
                        case.submission_id,
                        case.correlation_id,
                        case.payload_sha256,
                    )
                    if receipts:
                        diagnostic = receipts[0].assessment_diagnostic
                        if diagnostic is not None:
                            item.update(
                                assessment_diagnostic_available=True,
                                parser_diagnostic=diagnostic.parser_diagnostic,
                                canonical_valid=diagnostic.canonical_valid,
                                assessment_source=diagnostic.component.source,
                                assessment_extraction_status=diagnostic.component.extraction_status,
                                assessment_retention=diagnostic.retention,
                                assessment_sha256=diagnostic.content_sha256,
                                assessment_byte_length=diagnostic.byte_length,
                                assessment_artifact=(
                                    diagnostic.artifact.model_dump(mode="json")
                                    if diagnostic.artifact is not None
                                    else None
                                ),
                                assessment_component_metadata=diagnostic.component.model_dump(
                                    mode="json"
                                ),
                            )
                        usage = receipts[0].usage
                        item["reported_model"] = usage.reported_model
                        item["provider_response_observed"] = (
                            receipts[0].identity.observation_id is not None
                        )
                        item["http_response_received"] = (
                            receipts[0].identity.observation_id is not None
                        )
                        item["invocation_acceptance"] = receipts[
                            0
                        ].invocation_acceptance
                        item["provider_outcome"] = usage.provider_outcome
                        item["invocation_failure_code"] = usage.invocation_failure_code
                        item["completion_status"] = (
                            usage.provider_outcome or "not_observed"
                        )
                        item["observation_kind"] = usage.observation_kind
                        item["observed_order"] = (
                            receipts[0].order.model_dump(mode="json")
                            if receipts[0].order is not None
                            else None
                        )
                        if (
                            usage.input_tokens is not None
                            and usage.output_tokens is not None
                            and usage.cache_read_tokens == 0
                            and usage.cache_write_tokens == 0
                        ):
                            estimate = (
                                Decimal(usage.input_tokens)
                                * plan.pricing.input_usd_per_million
                                + Decimal(usage.output_tokens)
                                * plan.pricing.output_usd_per_million
                            ) / Decimal(1_000_000)
                            item["estimated_usd"] = str(estimate)
                            if estimated_total is not None:
                                estimated_total += estimate
                        elif case.case_id in consumed:
                            estimated_total = None
                        item["usage"] = {
                            name: getattr(usage, name)
                            for name in (
                                "usage_source",
                                "input_tokens",
                                "output_tokens",
                                "total_tokens",
                                "reasoning_tokens",
                                "cache_read_tokens",
                                "cache_write_tokens",
                            )
                        }
                        for name in totals:
                            value = getattr(usage, name)
                            prior = totals[name]
                            totals[name] = (
                                None
                                if value is None or prior is None
                                else prior + value
                            )
                    if results and not item["durable_evidence_consistent"]:
                        raise QualificationError()
                    # Positive terminal evidence is required. A partial run, HTTP
                    # success, or a saved receipt by itself never grants continuation.
                    terminal = (
                        bool(results)
                        and len(results) == len(case.task_ids)
                        and {record.task_id for record in results} == set(case.task_ids)
                        and all(
                            (record.run_id, record.request_uid, record.attempt_id)
                            == (case.run_id, case.request_uid, case.attempt_id)
                            and record.status in {"complete", "invalid"}
                            and repository.get_task(case.run_id, record.task_id).state
                            == record.status
                            for record in results
                        )
                        and summary.tasks == len(case.task_ids)
                        and summary.attempts == 1
                        and summary.results == len(case.task_ids)
                        and summary.retry_operations == 0
                        and summary.unresolved_submissions == 0
                        and summary.state
                        == (
                            "completed"
                            if all(record.status == "complete" for record in results)
                            else "partial"
                        )
                        and repository.get_attempt(case.run_id, case.attempt_id).state
                        == "normalized"
                        and repository.attempt_task_ids(case.run_id, case.attempt_id)
                        == case.task_ids
                    )
                    item["assessment_terminal"] = terminal
                    terminal_assessment_observed = bool(
                        terminal
                        and case.case_id in consumed
                        and item["durable_evidence_consistent"]
                        and item["provider_response_observed"]
                        and submission is not None
                        and submission.certainty == "accepted"
                        and submission.state == "resolved"
                        and item["provider_outcome"] == "completed"
                        and item["invocation_acceptance"] is None
                        and item["invocation_failure_code"] is None
                        and not item["recovery_required"]
                    )
                    item["assessment_continuation_allowed"] = bool(
                        plan.plan_version == 2
                        and terminal_assessment_observed
                        and item["assessment_diagnostic_available"]
                        and item["canonical_valid"] == item["local_assessment_valid"]
                    )
            except (PersistenceError, ValueError, OSError, sqlite3.Error):
                terminal_assessment_observed = False
                # Local integrity failures are reportable, never continuation
                # authority. Exception messages may contain untrusted bytes.
                item.update(
                    outcome="evidence_integrity_failure",
                    evidence_integrity_failure=True,
                    evidence_error_code="retained_evidence_unavailable_or_inconsistent",
                    durable_evidence_consistent=False,
                    local_assessment_valid=False,
                    assessment_terminal=False,
                    assessment_continuation_allowed=False,
                    provider_accepted=None,
                    provider_response_observed=False,
                    http_response_received=None,
                    invocation_acceptance=None,
                    provider_outcome=None,
                    invocation_failure_code=None,
                    completion_status="not_observed",
                    recovery_required=True,
                )
                item.pop("usage", None)
                item.pop("estimated_usd", None)
                estimated_total = None
                for name in totals:
                    totals[name] = None
        # An acknowledged executor observation is not itself successful
        # assessment acceptance. Keep explicit rejection, unresolved invocation,
        # failed provider response and ordinary completion outcomes distinct.
        if item["evidence_integrity_failure"]:
            item["acceptance_outcome"] = "evidence_integrity_failure"
        elif item["invocation_failure_code"] is not None:
            item["http_response_received"] = True
            item["provider_accepted"] = False
            item["acceptance_outcome"] = "rejected"
        elif item["invocation_acceptance"] == "unresolved":
            item["http_response_received"] = None
            item["acceptance_outcome"] = "unresolved"
        elif item["provider_outcome"] == "provider_failure":
            item["acceptance_outcome"] = "provider_failure"
        elif (
            item["provider_response_observed"] is True
            and item["provider_outcome"] is not None
        ):
            item["provider_accepted"] = True
            item["acceptance_outcome"] = "accepted"
        elif item["submission_certainty"] == "not_submitted":
            item["provider_accepted"] = False
            item["acceptance_outcome"] = "not_submitted"
        elif item["submission_certainty"] == "accepted":
            item["acceptance_outcome"] = "acknowledged"
        elif item["submission_certainty"] == "unknown" or case.case_id in consumed:
            item["acceptance_outcome"] = "unresolved"
        if (
            item["acceptance_outcome"] != "not_attempted"
            and item["completion_status"] == "not_attempted"
        ):
            item["completion_status"] = "not_observed"
        if case.case_id in consumed and not item.get("usage"):
            estimated_total = None
            for name in totals:
                totals[name] = None
        if item["evidence_integrity_failure"]:
            status = "evidence_integrity_failure"
        elif item["acceptance_outcome"] == "rejected":
            status = "provider_rejected_request"
        elif item["acceptance_outcome"] == "unresolved":
            status = "submitted_acceptance_unresolved"
        elif item["acceptance_outcome"] == "not_submitted":
            status = "not_submitted"
        elif item["provider_outcome"] in {"refusal", "content_filter"}:
            status = "provider_refused"
        elif item["provider_outcome"] == "output_limit":
            status = "provider_truncated_output"
        elif terminal_assessment_observed:
            status = (
                "provider_completed_assessment_valid"
                if item["local_assessment_valid"]
                else "provider_completed_assessment_invalid"
            )
        elif item["submission_certainty"] == "accepted":
            status = "provider_accepted_no_usable_observation"
        else:
            status = "not_attempted"
        item["case_status"] = status
        item["assessment_behavior_verified"] = (
            terminal_assessment_observed and item["local_assessment_valid"] is True
        )
        reports.append(item)
    valid_count = sum(
        row["case_status"] == "provider_completed_assessment_valid" for row in reports
    )
    invalid_count = sum(
        row["case_status"] == "provider_completed_assessment_invalid" for row in reports
    )
    return {
        "report_version": 1,
        "source_commit": plan.source_commit,
        "plan_sha256": digest,
        "sdk_version": plan.sdk_version,
        "planned_at": plan.created_at,
        "report_generated_at": datetime.now(UTC).isoformat(),
        "pricing": plan.pricing.model_dump(mode="json"),
        "estimated_usd": str(estimated_total) if estimated_total is not None else None,
        "provider": "anthropic",
        "model": plan.model,
        "api_version": "2026-10-21",
        "input_budget_kind": "conservative_offline_estimate_not_provider_enforced",
        "capture": "full",
        "reasoning_retention": "none",
        "physical_send_attempts": len(consumed),
        "physical_call_count_certainty": "conservative; includes the durable intent to HTTP forwarding crash window",
        "max_requests": plan.max_requests,
        "reserved_tokens": sum(c.reserved_tokens for c in plan.calls),
        "reserved_estimated_usd": str(
            sum((c.reserved_estimated_usd for c in plan.calls), Decimal(0))
        ),
        "reported_usage_totals": totals,
        "calls": reports,
        "matrix_execution_complete": valid_count + invalid_count == len(plan.calls),
        "all_assessments_valid": valid_count == len(plan.calls),
        "valid_assessment_count": valid_count,
        "invalid_assessment_count": invalid_count,
        "independent_review_required": True,
        "recovery_limitations": "No Messages retrieval API is assumed; retained response recovery only; uncertain or consumed requests are never replaced.",
        "spending_limitations": "Reserved input is an offline estimate, not a provider-enforced input limit or guaranteed monetary cap; unknown caching costs remain unknown; account terms, pricing changes, taxes and injected overhead may differ.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser(
        "plan", help="Compile four synthetic Messages requests offline."
    )
    plan.add_argument("--provider", choices=["anthropic"], required=True)
    plan.add_argument(
        "--endpoint",
        action="append",
        choices=["messages"],
        required=True,
    )
    plan.add_argument("--model", required=True)
    plan.add_argument("--source-commit", required=True)
    plan.add_argument("--pricing-profile", required=True, type=Path)
    plan.add_argument("--max-requests", required=True, type=int)
    plan.add_argument("--max-output-tokens", required=True, type=int)
    plan.add_argument("--max-total-tokens", required=True, type=int)
    plan.add_argument("--max-estimated-usd", required=True, type=Decimal)
    plan.add_argument("--evidence-dir", required=True, type=Path)
    execute = commands.add_parser(
        "execute",
        help="Billable: execute only the explicitly authorized immutable plan.",
    )
    execute.add_argument("--evidence-dir", required=True, type=Path)
    execute.add_argument("--authorize-plan-sha256", required=True)
    execute.add_argument("--authorize-source-commit", required=True)
    execute.add_argument("--accept-billable-calls", action="store_true", required=True)
    report = commands.add_parser(
        "report", help="Inspect retained qualification evidence read-only."
    )
    report.add_argument("--evidence-dir", required=True, type=Path)
    options = parser.parse_args()
    try:
        if options.command == "plan":
            pricing = cast(
                _PricingProfile, _read_model(options.pricing_profile, _PricingProfile)
            )
            build_plan(
                directory=options.evidence_dir,
                source_commit=options.source_commit,
                provider=options.provider,
                endpoints=tuple(options.endpoint),
                model=options.model,
                pricing=pricing,
                max_requests=options.max_requests,
                max_output_tokens=options.max_output_tokens,
                max_total_tokens=options.max_total_tokens,
                max_estimated_usd=options.max_estimated_usd,
            )
            result = report_plan(options.evidence_dir)
        elif options.command == "execute":
            result = asyncio.run(
                execute_plan(
                    options.evidence_dir,
                    authorized_plan_sha256=options.authorize_plan_sha256,
                    authorized_source_commit=options.authorize_source_commit,
                    accept_billable_calls=options.accept_billable_calls,
                )
            )
        else:
            result = report_plan(options.evidence_dir)
    except Exception:
        parser.exit(
            2,
            "Qualification operation failed; inspect the approved plan and retained evidence.\n",
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
