"""Offline four-case Developer API proposal. Live execution is deliberately absent.

This repository harness reuses clean-source admission, immutable file publication,
physical identities and production preparation. A plan/report cannot grant native
production authority, read credentials, or submit requests.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import argparse
import importlib.metadata
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import Field, field_validator, model_validator

from judgmentllm.config import RunSpecV3
from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.gemini_transport import (
    PreparedRequestV6,
    _qualification_context,
    prepare_production_request,
)
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import SHA256
from judgmentllm.planning import plan_record, stable_id
from judgmentllm.preparation import prepare_run
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.qualification import _immutable, _source_commit
from judgmentllm.transport import physical_identity

_MODEL = "gemini-3.1-flash-lite"
_COMMIT = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
_MONEY = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
_MECHANISM = "gemini.generate_content.response_json_schema"


class QualificationError(ValueError):
    def __init__(self) -> None:
        super().__init__(
            "Gemini qualification proposal, source or evidence is incompatible."
        )


class GeminiQualificationCallV1(EvidenceModel):
    case_id: str
    run_id: str
    task_kind: Literal["rubric", "pairwise"]
    mode: Literal["text", "native"]
    mechanism: Literal["text", "gemini.generate_content.response_json_schema"]
    request_uid: str
    task_ids: tuple[str, ...]
    attempt_id: str
    submission_id: str
    correlation_id: str
    dependency_sha256: SHA256
    prompt_sha256: SHA256
    payload_sha256: SHA256
    transport_commitment_sha256: SHA256
    request_body_utf8: str = Field(repr=False)
    reserved_input_tokens: int = Field(ge=8192)
    input_allowance_evidence: dict[str, int | str | bool]
    reserved_estimated_usd: _MONEY


class GeminiQualificationPlanV1(EvidenceModel):
    plan_version: Literal[1] = 1
    execution_enabled: Literal[False] = False
    source_commit: _COMMIT
    sdk_package: Literal["google-genai"] = "google-genai"
    sdk_version: Literal["2.34.0"] = "2.34.0"
    api_version: Literal["v1beta"] = "v1beta"
    provider: Literal["gemini"] = "gemini"
    endpoint: Literal["models.generate_content"] = "models.generate_content"
    base_url: Literal["https://generativelanguage.googleapis.com/"] = (
        "https://generativelanguage.googleapis.com/"
    )
    model: Literal["gemini-3.1-flash-lite"] = "gemini-3.1-flash-lite"
    created_at: datetime
    capture: Literal["full"] = "full"
    assessment_capture_permission: Literal[
        "synthetic_prompts_and_exact_assessment_text"
    ] = "synthetic_prompts_and_exact_assessment_text"
    reasoning_retention: Literal["none"] = "none"
    automatic_continuation: Literal[False] = False
    automatic_function_calling: Literal[False] = False
    automatic_retries: Literal[0] = 0
    concurrency: Literal[1] = 1
    maximum_authorized_requests: Literal[4] = 4
    requests_authorized_now: Literal[0] = 0
    max_output_tokens: int = Field(gt=0, le=512)
    max_total_reserved_tokens: int = Field(gt=0)
    proposed_budget_usd: _MONEY
    input_usd_per_million: Literal["0.25"] = "0.25"
    output_usd_per_million_including_thinking: Literal["1.50"] = "1.50"
    pricing_reviewed_on: Literal["2026-11-10"] = "2026-11-10"
    pricing_sources: tuple[str, ...] = (
        "https://ai.google.dev/gemini-api/docs/pricing",
        "https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite",
        "https://ai.google.dev/api/generate-content",
    )
    authorization_required: tuple[
        Literal[
            "exact_plan_sha256",
            "exact_source_commit",
            "billable_four_requests",
            "full_synthetic_assessment_capture",
        ],
        ...,
    ] = (
        "exact_plan_sha256",
        "exact_source_commit",
        "billable_four_requests",
        "full_synthetic_assessment_capture",
    )
    executor_status: Literal[
        "deferred_requires_reviewed_source_bound_executor_and_human_authorization"
    ] = "deferred_requires_reviewed_source_bound_executor_and_human_authorization"
    calls: tuple[GeminiQualificationCallV1, ...]

    @field_validator(
        "plan_version",
        "automatic_retries",
        "concurrency",
        "maximum_authorized_requests",
        "requests_authorized_now",
        mode="before",
    )
    @classmethod
    def exact_integers(cls, value: object) -> object:
        if type(value) is not int:
            raise QualificationError()
        return value

    @model_validator(mode="after")
    def exact_matrix(self) -> GeminiQualificationPlanV1:
        expected = [
            (task, mode)
            for task in ("rubric", "pairwise")
            for mode in ("text", "native")
        ]
        if [(c.task_kind, c.mode) for c in self.calls] != expected or len(
            {c.submission_id for c in self.calls}
        ) != 4:
            raise QualificationError()
        for call in self.calls:
            ident = physical_identity(call.run_id, call.request_uid, call.attempt_id)
            body = cast(
                dict[str, Any], decode_assessment_output(call.request_body_utf8)
            )
            config = body.get("generationConfig", {})
            if (
                call.case_id != f"{call.task_kind}-{call.mode}"
                or call.run_id
                != f"gemini-qualification-{self.source_commit}-{call.case_id}"
                or call.attempt_id
                != stable_id("attempt", call.run_id, call.request_uid, 1)
                or (call.submission_id, call.correlation_id)
                != (ident.submission_id, ident.correlation_id)
                or call.payload_sha256 != bytes_digest(call.request_body_utf8.encode())
                or (call.reserved_input_tokens, call.input_allowance_evidence)
                != _input_allowance(call.request_body_utf8)
                or call.reserved_estimated_usd
                != _reservation(call.reserved_input_tokens, self.max_output_tokens)
                or config.get("maxOutputTokens") != self.max_output_tokens
                or config.get("candidateCount") != 1
                or call.mechanism != ("text" if call.mode == "text" else _MECHANISM)
            ):
                raise QualificationError()
        if (
            sum(c.reserved_input_tokens + self.max_output_tokens for c in self.calls)
            > self.max_total_reserved_tokens
            or sum((c.reserved_estimated_usd for c in self.calls), Decimal(0))
            > self.proposed_budget_usd
        ):
            raise QualificationError()
        return self


def _input_allowance(body_utf8: str) -> tuple[int, dict[str, int | str | bool]]:
    body = cast(dict[str, Any], decode_assessment_output(body_utf8))
    config = body["generationConfig"]
    schema = config.get("responseJsonSchema")
    size = len(body_utf8.encode())
    schema_size = 0 if schema is None else len(json.dumps(schema).encode())
    reservation = max(8192, ((size + schema_size + 4096 + 1023) // 1024) * 1024)
    return reservation, {
        "method": "gemini-developer-synthetic-byte-allowance-v1",
        "complete_body_utf8_bytes": size,
        "additional_schema_utf8_bytes": schema_size,
        "framing_and_injected_instruction_allowance": 4096,
        "estimated_input_token_allowance": reservation,
        "provider_enforced_input_limit": False,
        "exact_provider_tokenizer_verified": False,
        "assumption": "one token per UTF-8 byte plus schema repetition and fixed overhead; estimated, not a proved billing bound",
    }


def _reservation(input_tokens: int, output_tokens: int) -> Decimal:
    return (
        Decimal(input_tokens) * Decimal("0.25")
        + Decimal(output_tokens) * Decimal("1.50")
    ) / Decimal(1_000_000)


def _specification(
    root: Path, task: str, mode: str, max_output_tokens: int
) -> RunSpecV3:
    data: dict[str, object] = {
        "schema_version": 3,
        "task_kind": task,
        "input": str(
            root / ("rubric-responses.jsonl" if task == "rubric" else "responses.jsonl")
        ),
        "provider": "gemini",
        "endpoint": "models.generate_content",
        "model": _MODEL,
        "generation": {"max_output_tokens": max_output_tokens},
        "capture": "full",
        "execution": {
            "mode": "live",
            "concurrency": 1,
            "cache_policy": "off",
            "explanations": False,
        },
        "prompt": {"structured_output": mode, "native_mechanism": _MECHANISM},
    }
    if task == "rubric":
        data["rubric"] = {
            "rubric_id": "001",
            "name": "Synthetic clarity",
            "traits": [
                {
                    "trait_id": "01",
                    "name": "Clarity",
                    "description": "Assess how clearly the response states its idea.",
                    "levels": [
                        {"label": "low", "numeric_value": 1},
                        {"label": "middle", "numeric_value": 3},
                        {"label": "high", "numeric_value": 8},
                    ],
                }
            ],
        }
    else:
        data["pairs"] = str(root / "pairs.jsonl")
        data["criteria"] = [
            {
                "trait_id": "01",
                "name": "Clarity",
                "description": "Choose the response that states its idea more clearly.",
            }
        ]
    return RunSpecV3.model_validate(data)


def build_plan(
    *,
    directory: Path,
    source_commit: str,
    max_output_tokens: int = 512,
    max_total_reserved_tokens: int = 34816,
    proposed_budget_usd: Decimal = Decimal("0.02"),
) -> GeminiQualificationPlanV1:
    """Require a clean committed checkout; never import the SDK or resolve a key."""
    if (
        importlib.metadata.version("google-genai") != "2.34.0"
        or source_commit != _source_commit()
    ):
        raise QualificationError()
    if (
        type(max_output_tokens) is not int
        or not 1 <= max_output_tokens <= 512
        or max_total_reserved_tokens < 4 * (8192 + max_output_tokens)
        or proposed_budget_usd < 4 * _reservation(8192, max_output_tokens)
    ):
        raise QualificationError()
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    items = [
        {
            "schema_version": 1,
            "item_id": "001",
            "text": "The seed grew because water helped it.",
        },
        {"schema_version": 1, "item_id": "002", "text": "Water helped the seed grow."},
    ]
    _immutable(
        directory / "responses.jsonl",
        b"".join(canonical_bytes(item) + b"\n" for item in items),
    )
    _immutable(directory / "rubric-responses.jsonl", canonical_bytes(items[0]) + b"\n")
    _immutable(
        directory / "pairs.jsonl",
        canonical_bytes(
            {
                "schema_version": 1,
                "pair_id": "001",
                "pair_uid": "001",
                "left_item_id": "001",
                "right_item_id": "002",
                "presented_order": ["002", "001"],
                "trait_id": "01",
            }
        )
        + b"\n",
    )
    calls: list[GeminiQualificationCallV1] = []
    for task in ("rubric", "pairwise"):
        for mode in ("text", "native"):
            case_id = f"{task}-{mode}"
            run_id = f"gemini-qualification-{source_commit}-{case_id}"
            spec = _specification(directory, task, mode, max_output_tokens)
            _immutable(directory / (case_id + ".json"), canonical_bytes(spec))
            with prepare_run(
                spec,
                store=directory / "offline-preparation" / case_id,
                run_id=run_id,
                transport_context=_qualification_context(
                    spec, source_commit=source_commit
                ),
            ) as prepared:
                dependencies = tuple(prepared.iter_request_dependencies())
                if len(dependencies) != 1:
                    raise QualificationError()
                dep = dependencies[0]
                identity = physical_identity(
                    run_id,
                    dep.request.request_uid,
                    stable_id("attempt", run_id, dep.request.request_uid, 1),
                )
                wire = prepared.prepare_transport_attempt(dep, identity)
                if not isinstance(wire, PreparedRequestV6):
                    raise QualificationError()
                allowance, evidence = _input_allowance(wire.payload.decode())
                calls.append(
                    GeminiQualificationCallV1(
                        case_id=case_id,
                        run_id=run_id,
                        task_kind=cast(Any, task),
                        mode=cast(Any, mode),
                        mechanism=cast(Any, wire.transport_commitment.mechanism),
                        request_uid=wire.request_uid,
                        task_ids=wire.task_ids,
                        attempt_id=identity.attempt_id,
                        submission_id=identity.submission_id,
                        correlation_id=identity.correlation_id,
                        dependency_sha256=wire.dependency_sha256,
                        prompt_sha256=wire.prompt_sha256,
                        payload_sha256=wire.payload_sha256,
                        transport_commitment_sha256=wire.transport_commitment.commitment_sha256,
                        request_body_utf8=wire.payload.decode(),
                        reserved_input_tokens=allowance,
                        input_allowance_evidence=evidence,
                        reserved_estimated_usd=_reservation(
                            allowance, max_output_tokens
                        ),
                    )
                )
    plan = GeminiQualificationPlanV1(
        source_commit=source_commit,
        created_at=datetime.now(UTC),
        max_output_tokens=max_output_tokens,
        max_total_reserved_tokens=max_total_reserved_tokens,
        proposed_budget_usd=proposed_budget_usd,
        calls=tuple(calls),
    )
    raw = canonical_bytes(plan)
    _immutable(directory / "plan.json", raw)
    _immutable(directory / "plan.sha256", (bytes_digest(raw) + "\n").encode())
    return plan


def report_plan(directory: Path) -> dict[str, object]:
    """Read only; the exact plan digest is external consent's review identity."""
    raw = (directory / "plan.json").read_bytes()
    digest = bytes_digest(raw)
    decode_assessment_output(raw)
    plan = GeminiQualificationPlanV1.model_validate_json(raw)
    if (
        canonical_bytes(plan) != raw
        or (directory / "plan.sha256").read_text().strip() != digest
    ):
        raise QualificationError()
    for call in plan.calls:
        spec = _specification(
            directory.resolve(), call.task_kind, call.mode, plan.max_output_tokens
        )
        if (directory / (call.case_id + ".json")).read_bytes() != canonical_bytes(spec):
            raise QualificationError()
        responses = {
            "001": ResponseItem(
                item_id="001", text="The seed grew because water helped it."
            ),
            "002": ResponseItem(item_id="002", text="Water helped the seed grow."),
        }
        record = (
            responses["001"]
            if call.task_kind == "rubric"
            else PairTask(
                pair_id="001",
                pair_uid="001",
                left_item_id="001",
                right_item_id="002",
                presented_order=["002", "001"],
                trait_id="01",
            )
        )
        request = next(plan_record(spec, record))
        wire = prepare_production_request(
            spec,
            RequestDependency(
                request=request, dependency_sha256=call.dependency_sha256
            ),
            physical_identity(call.run_id, call.request_uid, call.attempt_id),
            _qualification_context(spec, source_commit=plan.source_commit),
            lookup=lambda kind, identifier, responses=responses, record=record: (
                record if kind == "pair" else responses[identifier]
            ),
        )
        if (
            wire.payload != call.request_body_utf8.encode()
            or wire.prompt_sha256 != call.prompt_sha256
            or wire.transport_commitment.commitment_sha256
            != call.transport_commitment_sha256
            or wire.task_ids != call.task_ids
        ):
            raise QualificationError()
    return {
        "provider": "gemini",
        "plan_sha256": digest,
        "source_commit": plan.source_commit,
        "execution_enabled": False,
        "executed_requests": 0,
        "live_qualified": False,
        "maximum_proposed_requests": plan.maximum_authorized_requests,
        "reserved_input_tokens": sum(c.reserved_input_tokens for c in plan.calls),
        "reserved_output_tokens_including_thinking": 4 * plan.max_output_tokens,
        "reserved_estimated_usd": str(
            sum((c.reserved_estimated_usd for c in plan.calls), Decimal(0))
        ),
        "proposed_budget_usd": str(plan.proposed_budget_usd),
        "executor_status": plan.executor_status,
        "authorization_required": list(plan.authorization_required),
        "limitations": "Estimated input allowances are not enforced input or monetary caps; serving, pricing and account eligibility need review before future authorization.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline Gemini qualification proposal; no executor is provided"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--source-commit", required=True)
    plan.add_argument("--max-output-tokens", type=int, default=512)
    plan.add_argument("--max-total-reserved-tokens", type=int, default=34816)
    plan.add_argument("--proposed-budget-usd", type=Decimal, default=Decimal("0.02"))
    plan.add_argument("--evidence-dir", type=Path, required=True)
    report = sub.add_parser("report")
    report.add_argument("--evidence-dir", type=Path, required=True)
    options = parser.parse_args()
    try:
        if options.command == "plan":
            build_plan(
                directory=options.evidence_dir,
                source_commit=options.source_commit,
                max_output_tokens=options.max_output_tokens,
                max_total_reserved_tokens=options.max_total_reserved_tokens,
                proposed_budget_usd=options.proposed_budget_usd,
            )
        print(json.dumps(report_plan(options.evidence_dir), sort_keys=True))
    except Exception:
        parser.exit(
            2,
            "Gemini qualification proposal is incompatible; no provider operation was authorized.\n",
        )
