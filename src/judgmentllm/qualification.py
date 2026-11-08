"""Explicitly authorized, synthetic OpenAI qualification; never ordinary execution.

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
from typing import TYPE_CHECKING, Annotated, Literal, cast

from pydantic import ConfigDict, Field, model_validator

from judgmentllm.config import RunSpecV3
from judgmentllm.models import StrictModel
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import SHA256, NotFoundError
from judgmentllm.planning import stable_id
from judgmentllm.providers.protocol import PreparedRequest

if TYPE_CHECKING:
    from judgmentllm.production_transport import ProductionContext

# Private APIs deliberately do not enter the top-level public package.
# pyright: reportPrivateUsage=false

_COMMIT = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
_ENDPOINT = Literal["responses", "chat.completions"]
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


class _PricingProfile(_Frozen):
    """Reviewed assumptions, not provider capability or billing authority.

    Reserving the complete documented context as input avoids an unverified
    tokenizer/framing estimate. No cache discount or special tier is assumed.
    """

    profile_version: Literal[1] = 1
    provider: Literal["openai"] = "openai"
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
            set(self.endpoints) != {"responses", "chat.completions"}
            or len(self.endpoints) != 2
            or self.reviewed_on > datetime.now(UTC).date()
            or self.input_usd_per_million <= 0
            or self.output_usd_per_million <= 0
            or any(
                not url.startswith(
                    ("https://developers.openai.com/", "https://openai.com/")
                )
                for url in self.documentation_urls
            )
        ):
            raise QualificationError()
        return self


class _PlannedCall(_Frozen):
    case_id: str
    run_id: str
    provider: Literal["openai"] = "openai"
    endpoint: _ENDPOINT
    model: str
    task_kind: Literal["rubric", "pairwise"]
    mode: Literal["text", "native"]
    mechanism: Literal[
        "text", "openai.responses.text.format", "openai.chat.response_format"
    ]
    request_uid: str
    task_ids: tuple[str, ...]
    attempt_id: str
    submission_id: str
    correlation_id: str
    dependency_sha256: SHA256
    prompt_sha256: SHA256
    payload_sha256: SHA256
    request_body_utf8: str = Field(repr=False)
    max_output_tokens: int = Field(gt=0)
    reserved_tokens: int = Field(gt=0)
    reserved_estimated_usd: _MONEY

    @model_validator(mode="after")
    def exact_body(self) -> _PlannedCall:
        if bytes_digest(self.request_body_utf8.encode("utf-8")) != self.payload_sha256:
            raise QualificationError()
        if self.mode == "text":
            expected = "text"
        else:
            expected = (
                "openai.responses.text.format"
                if self.endpoint == "responses"
                else "openai.chat.response_format"
            )
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
    plan_version: Literal[1] = 1
    source_commit: _COMMIT
    sdk_package: Literal["openai"] = "openai"
    sdk_version: Literal["3.42.1"] = "3.42.1"
    created_at: str
    provider: Literal["openai"] = "openai"
    model: str
    endpoints: tuple[_ENDPOINT, ...]
    synthetic_items: tuple[dict[str, str], ...] = Field(repr=False)
    capture: Literal["full"] = "full"
    store: Literal[False] = False
    reasoning_retention: Literal["none"] = "none"
    automatic_retries: Literal[0] = 0
    max_requests: int = Field(gt=0, le=8)
    max_output_tokens: int = Field(gt=0)
    max_total_tokens: int = Field(gt=0)
    max_estimated_usd: _MONEY
    pricing: _PricingProfile
    calls: tuple[_PlannedCall, ...]

    @model_validator(mode="after")
    def exact_initial_matrix(self) -> _QualificationPlan:
        expected = {
            (endpoint, task, mode)
            for endpoint in ("responses", "chat.completions")
            for task in ("rubric", "pairwise")
            for mode in ("text", "native")
        }
        if (
            len(self.calls) != 8
            or self.max_requests != 8
            or set(self.endpoints) != {"responses", "chat.completions"}
            or len(self.endpoints) != 2
            or tuple(self.synthetic_items) != _SYNTHETIC_ITEMS
            or {(c.endpoint, c.task_kind, c.mode) for c in self.calls} != expected
            or len({c.case_id for c in self.calls}) != 8
            or len({c.submission_id for c in self.calls}) != 8
            or self.model != self.pricing.model
            or self.max_output_tokens > self.pricing.maximum_output_tokens
        ):
            raise QualificationError()
        for call in self.calls:
            if (
                call.case_id != f"{call.endpoint}-{call.task_kind}-{call.mode}"
                or call.run_id != "qualification-" + call.case_id
                or call.model != self.model
                or call.max_output_tokens != self.max_output_tokens
                or call.reserved_tokens
                != self.pricing.context_tokens + self.max_output_tokens
                or call.reserved_estimated_usd
                != _reservation(self.pricing, self.max_output_tokens)
            ):
                raise QualificationError()
        if (
            sum(c.reserved_tokens for c in self.calls) > self.max_total_tokens
            or sum((c.reserved_estimated_usd for c in self.calls), Decimal(0))
            > self.max_estimated_usd
        ):
            raise QualificationError()
        return self


def _reservation(profile: _PricingProfile, output_tokens: int) -> Decimal:
    return (
        Decimal(profile.context_tokens) * profile.input_usd_per_million
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
        "provider": "openai",
        "endpoint": endpoint,
        "model": model,
        "generation": {"max_output_tokens": output_tokens},
        "execution": {"mode": "live", "concurrency": 1, "explanations": False},
        "prompt": {
            "structured_output": mode,
            "native_mechanism": "openai.responses.text.format"
            if endpoint == "responses"
            else "openai.chat.response_format",
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
    from judgmentllm.preparation import prepare_run
    from judgmentllm.production_transport import _qualification_context
    from judgmentllm.transport import physical_identity

    if (
        provider != "openai"
        or set(endpoints) != {"responses", "chat.completions"}
        or len(endpoints) != 2
    ):
        raise QualificationError()
    if (
        importlib.metadata.version("openai") != "3.42.1"
        or source_commit != _source_commit()
    ):
        raise QualificationError()
    if max_requests != 8 or max_output_tokens > pricing.maximum_output_tokens:
        raise QualificationError()
    if (
        pricing.model != model
        or 8 * (pricing.context_tokens + max_output_tokens) > max_total_tokens
        or 8 * _reservation(pricing, max_output_tokens) > max_estimated_usd
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
                run_id = "qualification-" + case_id
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
                                    "openai.responses.text.format",
                                    "openai.chat.response_format",
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
                            reserved_tokens=pricing.context_tokens + max_output_tokens,
                            reserved_estimated_usd=_reservation(
                                pricing, max_output_tokens
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

    def verify_context(self, context: ProductionContext) -> None:
        from judgmentllm.production_transport import ProductionContext

        if (
            type(context) is not ProductionContext
            or context.kind != "openai-qualification-v1"
            or context.qualification_plan_sha256 != self.plan_sha256
            or context.source_commit != self.source_commit
            or (context.scope.provider, context.scope.endpoint, context.scope.model)
            != ("openai", self.case.endpoint, self.case.model)
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
    from judgmentllm.execution import execute_run, recover_run
    from judgmentllm.execution.retry import RetryPolicy
    from judgmentllm.lifecycle import export_run_results
    from judgmentllm.preparation import prepare_run
    from judgmentllm.production_transport import _qualification_context
    from judgmentllm.providers.openai import OpenAIAdapter

    directory = directory.resolve()
    plan, digest = _load_plan(directory)
    if (
        not accept_billable_calls
        or authorized_plan_sha256 != digest
        or authorized_source_commit != plan.source_commit
        or _source_commit() != plan.source_commit
        or importlib.metadata.version("openai") != plan.sdk_version
    ):
        raise QualificationError()
    _verify_inputs(directory)
    for case in plan.calls:
        _verify_case(directory, case)
    _authorize(directory, plan, digest)
    for case in plan.calls:
        spec = _verify_case(directory, case)
        _consumed(directory)
        context = _qualification_context(
            spec, source_commit=plan.source_commit, plan_sha256=digest
        )
        guard = _QualificationSubmissionGuard(
            directory, plan, case, token=_EXECUTOR_TOKEN
        )
        adapter = OpenAIAdapter(
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
            if result.recovery_required or result.summary.state != "completed":
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
        "total_tokens": 0,
    }
    for case in plan.calls:
        item: dict[str, object] = {
            "case_id": case.case_id,
            "endpoint": case.endpoint,
            "requested_model": case.model,
            "mechanism": case.mechanism,
            "request_uid": case.request_uid,
            "payload_sha256": case.payload_sha256,
            "actual_request_body_digest_verified": case.case_id in consumed,
            "max_output_tokens": case.max_output_tokens,
            "reserved_tokens": case.reserved_tokens,
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
        }
        store = directory / "execution" / case.case_id
        if (store / "registry.sqlite3").is_file():
            with ReadOnlyRepository(store) as repository:
                results = tuple(repository.iter_results(case.run_id))
                receipts = tuple(repository.iter_evidence(case.run_id))
                item["outcome"] = repository.get_run(case.run_id).state
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
                item["durable_evidence_consistent"] = (
                    len(receipts) == 1
                    and receipts[0].payload_sha256 == case.payload_sha256
                )
                if receipts:
                    usage = receipts[0].usage
                    item["reported_model"] = usage.reported_model
                    item["provider_response_observed"] = (
                        receipts[0].identity.observation_id is not None
                    )
                    item["invocation_acceptance"] = receipts[0].invocation_acceptance
                    item["provider_outcome"] = usage.provider_outcome
                    item["invocation_failure_code"] = usage.invocation_failure_code
                    item["completion_status"] = usage.provider_outcome or "not_observed"
                    item["observation_kind"] = usage.observation_kind
                    item["observed_order"] = (
                        receipts[0].order.model_dump(mode="json")
                        if receipts[0].order is not None
                        else None
                    )
                    if (
                        usage.input_tokens is not None
                        and usage.output_tokens is not None
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
                            None if value is None or prior is None else prior + value
                        )
        # An acknowledged executor observation is not itself successful
        # assessment acceptance. Keep explicit rejection, unresolved invocation,
        # failed provider response and ordinary completion outcomes distinct.
        if item["invocation_failure_code"] is not None:
            item["provider_accepted"] = False
            item["acceptance_outcome"] = "rejected"
        elif item["invocation_acceptance"] == "unresolved":
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
        if case.case_id in consumed and "estimated_usd" not in item:
            estimated_total = None
            for name in totals:
                totals[name] = None
        item["assessment_behavior_verified"] = (
            item.get("provider_accepted", False) is True
            and item.get("local_assessment_valid", False) is True
        )
        reports.append(item)
    return {
        "report_version": 1,
        "source_commit": plan.source_commit,
        "plan_sha256": digest,
        "sdk_version": plan.sdk_version,
        "planned_at": plan.created_at,
        "report_generated_at": datetime.now(UTC).isoformat(),
        "pricing": plan.pricing.model_dump(mode="json"),
        "estimated_usd": str(estimated_total) if estimated_total is not None else None,
        "provider": "openai",
        "model": plan.model,
        "store": False,
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
        "independent_review_required": True,
        "recovery_limitations": "store=false; retained response recovery only; uncertain or consumed requests are never replaced.",
        "spending_limitations": "Conservative estimate under cited rates; account terms, pricing changes and taxes may differ.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser(
        "plan", help="Compile the eight synthetic requests offline."
    )
    plan.add_argument("--provider", choices=["openai"], required=True)
    plan.add_argument(
        "--endpoint",
        action="append",
        choices=["responses", "chat.completions"],
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
