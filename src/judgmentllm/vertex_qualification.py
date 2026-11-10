"""Vertex four-case immutable offline candidate, independent of live authority.

Reuse source admission, synthetic assessment contracts, physical identities and
production preparation from the existing qualification harnesses. There is no
executor, authorization/grant factory, credential resolution or registry change.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from pydantic import Field, field_validator, model_validator

from judgmentllm.config import RunSpecV3
from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import SHA256
from judgmentllm.planning import plan_record, stable_id
from judgmentllm.preparation import prepare_run
from judgmentllm.preparation.manifest import configuration_digest
from judgmentllm.preparation.records import record_digest
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.qualification import _immutable, _source_commit
from judgmentllm.transport import physical_identity
from judgmentllm.vertex_config import VertexRuntimeConfiguration
from judgmentllm.vertex_qualification_sdk import (
    ROOT,
    installed_sdk_evidence,
    sdk_request,
)
from judgmentllm.vertex_transport import (
    PreparedRequestV7,
    TransportCommitmentV5,
    _qualification_context,
    prepare_production_request,
)

MODEL = "gemini-3.1-flash-lite"
MECHANISM = "vertex.generate_content.response_json_schema"
_COMMIT = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
_MONEY = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class QualificationError(ValueError):
    """Safe error: no private project, path, credentials or SDK error prose."""

    def __init__(self) -> None:
        super().__init__("Vertex offline plan, source or evidence is incompatible.")


class ReservationRevisionRequired(ValueError):
    """The fixed proposed ceiling cannot admit these reservations."""

    def __init__(self, required: Decimal) -> None:
        super().__init__(
            f"Conservative reservation requires ${required} USD; revise the proposal before sealing. The $0.02 ceiling was not increased."
        )


class InputAllowanceV1(EvidenceModel):
    method: Literal["vertex-complete-body-byte-allowance-v1"] = (
        "vertex-complete-body-byte-allowance-v1"
    )
    complete_body_utf8_bytes: int = Field(gt=0)
    additional_schema_utf8_bytes: int = Field(ge=0)
    framing_and_injected_instruction_allowance: Literal[4096] = 4096
    rounding_unit: Literal[1024] = 1024
    estimated_input_tokens: int = Field(gt=0)
    exact_provider_tokenizer_verified: Literal[False] = False
    provider_enforced_input_limit: Literal[False] = False
    assumption: Literal[
        "one token per complete UTF-8 body byte plus repeated native schema and 4096 framing tokens, rounded up to 1024; estimate only"
    ] = "one token per complete UTF-8 body byte plus repeated native schema and 4096 framing tokens, rounded up to 1024; estimate only"


def _input_allowance(body: bytes) -> InputAllowanceV1:
    values = cast(dict[str, Any], decode_assessment_output(body))
    schema = values["generationConfig"].get("responseJsonSchema")
    repeated = 0 if schema is None else len(json.dumps(schema).encode("utf-8"))
    return InputAllowanceV1(
        complete_body_utf8_bytes=len(body),
        additional_schema_utf8_bytes=repeated,
        estimated_input_tokens=((len(body) + repeated + 4096 + 1023) // 1024) * 1024,
    )


def _reservation(input_tokens: int, output_tokens: int) -> Decimal:
    return (
        Decimal(input_tokens) * Decimal("0.25")
        + Decimal(output_tokens) * Decimal("1.50")
    ) / Decimal(1_000_000)


class VertexQualificationCallV2(EvidenceModel):
    case_id: str
    run_id: str
    task_kind: Literal["rubric", "pairwise"]
    mode: Literal["text", "native"]
    configuration_file_sha256: SHA256
    rendered_prompt_utf8: str = Field(repr=False)
    request_body_utf8: str = Field(repr=False)
    request_url_sha256: SHA256
    transport: TransportCommitmentV5
    input_allowance: InputAllowanceV1
    reserved_output_tokens_including_thinking: int = Field(gt=0, le=512)
    reserved_estimated_usd: _MONEY


class VertexQualificationPlanV2(EvidenceModel):
    """Private content-bearing seal; zero submission or native authority."""

    plan_version: Literal[2] = 2
    plan_kind: Literal["vertex-offline-executable-candidate-v2"] = (
        "vertex-offline-executable-candidate-v2"
    )
    source_commit: _COMMIT
    created_at: datetime
    provider: Literal["vertex"] = "vertex"
    endpoint: Literal["models.generate_content"] = "models.generate_content"
    http_method: Literal["POST"] = "POST"
    sdk_package: Literal["google-genai"] = "google-genai"
    sdk_version: Literal["2.34.0"] = "2.34.0"
    api_version: Literal["v1beta1"] = "v1beta1"
    installed_sdk_source_sha256: dict[str, SHA256]
    artifact_sha256: dict[str, SHA256]
    pricing_evidence_sha256: SHA256
    authentication_mode: Literal["api_key", "adc"]
    credential_source: Literal["JUDGMENTLLM_VERTEX_API_KEY", "google_adc"]
    account_eligibility: Literal["unverified"] = "unverified"
    key_type_and_project_association: Literal["unverified"] = "unverified"
    express_mode_compatible: Literal[False] = False
    project: str = Field(repr=False)
    location: Literal["global"] = "global"
    base_url: Literal["https://aiplatform.googleapis.com/"] = (
        "https://aiplatform.googleapis.com/"
    )
    model: Literal["gemini-3.1-flash-lite"] = "gemini-3.1-flash-lite"
    publisher_model_resource: str = Field(repr=False)
    route_sha256: SHA256
    capture: Literal["full_synthetic_assessment_requires_future_consent"] = (
        "full_synthetic_assessment_requires_future_consent"
    )
    reasoning_retention: Literal["none"] = "none"
    planned_requests: Literal[4] = 4
    maximum_proposed_requests: Literal[4] = 4
    authorized_requests: Literal[0] = 0
    execution_enabled: Literal[False] = False
    executor_status: Literal[
        "absent_requires_separate_review_merge_and_authorization"
    ] = "absent_requires_separate_review_merge_and_authorization"
    production_native_authority: Literal[False] = False
    concurrency: Literal[1] = 1
    candidate_count: Literal[1] = 1
    automatic_retries: Literal[0] = 0
    automatic_continuation: Literal[False] = False
    automatic_function_calling: Literal[False] = False
    max_output_tokens_per_case: int = Field(gt=0, le=512)
    reserved_input_tokens: int = Field(gt=0)
    reserved_output_tokens_including_thinking: int = Field(gt=0)
    total_reserved_tokens: int = Field(gt=0)
    reserved_estimated_usd: _MONEY
    proposed_estimated_authorization_ceiling_usd: Literal["0.02"] = "0.02"
    input_usd_per_million: Literal["0.25"] = "0.25"
    output_usd_per_million_including_thinking: Literal["1.50"] = "1.50"
    pricing_class: Literal["vertex_standard_global_text_no_discounts"] = (
        "vertex_standard_global_text_no_discounts"
    )
    actual_requests: Literal[None] = None
    actual_input_tokens: Literal[None] = None
    actual_output_tokens_including_thinking: Literal[None] = None
    actual_cost_usd: Literal[None] = None
    calls: tuple[VertexQualificationCallV2, ...]

    @field_validator(
        "plan_version",
        "planned_requests",
        "maximum_proposed_requests",
        "authorized_requests",
        "concurrency",
        "candidate_count",
        "automatic_retries",
        mode="before",
    )
    @classmethod
    def exact_integers(cls, value: object) -> object:
        if type(value) is not int:
            raise QualificationError()
        return value

    @model_validator(mode="after")
    def exact_matrix_and_budget(self) -> VertexQualificationPlanV2:
        runtime = self.runtime()
        expected = [
            (task, mode)
            for task in ("rubric", "pairwise")
            for mode in ("text", "native")
        ]
        if (
            [(c.task_kind, c.mode) for c in self.calls] != expected
            or len({c.transport.identity.submission_id for c in self.calls}) != 4
            or len({c.transport.identity.correlation_id for c in self.calls}) != 4
            or self.publisher_model_resource != runtime.resource_path(MODEL)
            or self.route_sha256 != runtime.route_sha256
            or self.credential_source
            != (
                "JUDGMENTLLM_VERTEX_API_KEY"
                if self.authentication_mode == "api_key"
                else "google_adc"
            )
        ):
            raise QualificationError()
        for call in self.calls:
            receipt = call.transport
            identity = receipt.identity
            body = cast(
                dict[str, Any], decode_assessment_output(call.request_body_utf8)
            )
            if (
                call.case_id != f"{call.task_kind}-{call.mode}"
                or call.run_id != _run_id(self.source_commit, runtime, call.case_id)
                or identity
                != physical_identity(
                    call.run_id,
                    identity.request_uid,
                    stable_id("attempt", call.run_id, identity.request_uid, 1),
                )
                or receipt.context_kind != "vertex-qualification-candidate-v1"
                or receipt.route_sha256 != self.route_sha256
                or receipt.scope.model != self.model
                or receipt.mechanism != ("text" if call.mode == "text" else MECHANISM)
                or receipt.payload_sha256
                != bytes_digest(call.request_body_utf8.encode())
                or receipt.prompt_sha256
                != bytes_digest(call.rendered_prompt_utf8.encode())
                or call.input_allowance
                != _input_allowance(call.request_body_utf8.encode())
                or call.reserved_output_tokens_including_thinking
                != self.max_output_tokens_per_case
                or call.reserved_estimated_usd
                != _reservation(
                    call.input_allowance.estimated_input_tokens,
                    self.max_output_tokens_per_case,
                )
                or body["generationConfig"]["maxOutputTokens"]
                != self.max_output_tokens_per_case
                or body["generationConfig"]["candidateCount"] != 1
            ):
                raise QualificationError()
        inputs = sum(c.input_allowance.estimated_input_tokens for c in self.calls)
        outputs = 4 * self.max_output_tokens_per_case
        cost = sum((c.reserved_estimated_usd for c in self.calls), Decimal(0))
        if (
            self.reserved_input_tokens != inputs
            or self.reserved_output_tokens_including_thinking != outputs
            or self.total_reserved_tokens != inputs + outputs
            or self.reserved_estimated_usd != cost
            or cost > Decimal("0.02")
        ):
            raise QualificationError()
        return self

    def runtime(self) -> VertexRuntimeConfiguration:
        return VertexRuntimeConfiguration(
            project=self.project,
            location=self.location,
            auth_mode=self.authentication_mode,
        )


def _run_id(source: str, runtime: VertexRuntimeConfiguration, case: str) -> str:
    return f"vertex-qualification-{source}-{runtime.route_sha256}-{case}"


def _inputs() -> dict[str, bytes]:
    items = [
        dict(schema_version=1, item_id=i, text=t)
        for i, t in (
            ("001", "The seed grew because water helped it."),
            ("002", "Water helped the seed grow."),
        )
    ]
    return {
        "responses.jsonl": b"".join(canonical_bytes(item) + b"\n" for item in items),
        "rubric-responses.jsonl": canonical_bytes(items[0]) + b"\n",
        "pairs.jsonl": canonical_bytes(
            dict(
                schema_version=1,
                pair_id="001",
                pair_uid="001",
                left_item_id="001",
                right_item_id="002",
                presented_order=["002", "001"],
                trait_id="01",
            )
        )
        + b"\n",
    }


def _specification(directory: Path, task: str, mode: str, tokens: int) -> RunSpecV3:
    data: dict[str, object] = {
        "schema_version": 3,
        "task_kind": task,
        "input": str(
            directory
            / ("rubric-responses.jsonl" if task == "rubric" else "responses.jsonl")
        ),
        "provider": "vertex",
        "endpoint": "models.generate_content",
        "model": MODEL,
        "generation": {"max_output_tokens": tokens},
        "capture": "full",
        "execution": {
            "mode": "live",
            "concurrency": 1,
            "cache_policy": "off",
            "explanations": False,
        },
        "prompt": {"structured_output": mode, "native_mechanism": MECHANISM},
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
                        {"label": label, "numeric_value": number}
                        for label, number in (("low", 1), ("middle", 3), ("high", 8))
                    ],
                }
            ],
        }
    else:
        data["pairs"] = str(directory / "pairs.jsonl")
        data["criteria"] = [
            {
                "trait_id": "01",
                "name": "Clarity",
                "description": "Choose the response that states its idea more clearly.",
            }
        ]
    return RunSpecV3.model_validate(data)


def _pricing_digest() -> str:
    return bytes_digest(
        (ROOT / "docs/vertex-qualification-price-evidence.json").read_bytes()
    )


def _external(directory: Path) -> Path:
    # A private seal must never be generated in Git, including through symlinks.
    resolved = directory.resolve()
    if resolved.is_relative_to(ROOT.resolve()) or directory.is_symlink():
        raise QualificationError()
    return resolved


def build_plan(
    *,
    directory: Path,
    source_commit: str,
    project: str,
    authentication_mode: Literal["api_key", "adc"],
    location: str = "global",
    model: str = MODEL,
    max_output_tokens: int = 512,
) -> VertexQualificationPlanV2:
    """Prepare once from clean source; only explicit nonsecret routing is used."""
    if (
        source_commit != _source_commit()
        or authentication_mode not in {"api_key", "adc"}
        or location != "global"
        or model != MODEL
        or type(max_output_tokens) is not int
        or not 1 <= max_output_tokens <= 512
    ):
        raise QualificationError()
    runtime = VertexRuntimeConfiguration(
        project=project, location=location, auth_mode=authentication_mode
    )
    sdk_evidence = installed_sdk_evidence()
    directory = _external(directory)
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    for name, raw in _inputs().items():
        _immutable(directory / name, raw)
    calls: list[VertexQualificationCallV2] = []
    for task in ("rubric", "pairwise"):
        for mode in ("text", "native"):
            case = f"{task}-{mode}"
            run_id = _run_id(source_commit, runtime, case)
            spec = _specification(directory, task, mode, max_output_tokens)
            _immutable(directory / (case + ".json"), canonical_bytes(spec))
            with prepare_run(
                spec,
                store=directory / "offline-preparation" / case,
                run_id=run_id,
                transport_context=_qualification_context(
                    spec, source_commit=source_commit, runtime=runtime
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
                if not isinstance(wire, PreparedRequestV7):
                    raise QualificationError()
                calls.append(
                    _call(case, run_id, spec, wire, runtime, max_output_tokens)
                )
    inputs = sum(c.input_allowance.estimated_input_tokens for c in calls)
    cost = sum((c.reserved_estimated_usd for c in calls), Decimal(0))
    if cost > Decimal("0.02"):
        raise ReservationRevisionRequired(cost)
    plan = VertexQualificationPlanV2(
        source_commit=source_commit,
        authentication_mode=authentication_mode,
        credential_source="JUDGMENTLLM_VERTEX_API_KEY"
        if authentication_mode == "api_key"
        else "google_adc",
        created_at=datetime.now(UTC),
        installed_sdk_source_sha256=sdk_evidence,
        artifact_sha256=_artifact_hashes(directory),
        pricing_evidence_sha256=_pricing_digest(),
        project=project,
        publisher_model_resource=runtime.resource_path(MODEL),
        route_sha256=runtime.route_sha256,
        max_output_tokens_per_case=max_output_tokens,
        reserved_input_tokens=inputs,
        reserved_output_tokens_including_thinking=4 * max_output_tokens,
        total_reserved_tokens=inputs + 4 * max_output_tokens,
        reserved_estimated_usd=cost,
        calls=tuple(calls),
    )
    if source_commit != _source_commit():
        raise QualificationError()
    raw = canonical_bytes(plan)
    _immutable(directory / "plan.json", raw)
    _immutable(directory / "plan.sha256", (bytes_digest(raw) + "\n").encode())
    # The whole directory is private on POSIX; Windows operators must select an
    # equivalently private ACL. Never claim POSIX modes establish Windows ACLs.
    for path in directory.rglob("*"):
        if path.is_file():
            os.chmod(path, 0o600)
    return plan


def _call(
    case: str,
    run_id: str,
    spec: RunSpecV3,
    wire: PreparedRequestV7,
    runtime: VertexRuntimeConfiguration,
    tokens: int,
) -> VertexQualificationCallV2:
    url, body = sdk_request(wire, runtime)
    if (
        body != wire.payload
        or url
        != runtime.base_url
        + "v1beta1/"
        + runtime.resource_path(MODEL)
        + ":generateContent"
    ):
        raise QualificationError()
    return VertexQualificationCallV2(
        case_id=case,
        run_id=run_id,
        task_kind=spec.task_kind,
        mode=cast(Any, spec.prompt.structured_output),
        configuration_file_sha256=bytes_digest(canonical_bytes(spec)),
        rendered_prompt_utf8=wire.rendered_text,
        request_body_utf8=body.decode(),
        request_url_sha256=bytes_digest(url.encode()),
        transport=wire.transport_commitment,
        input_allowance=_input_allowance(body),
        reserved_output_tokens_including_thinking=tokens,
        reserved_estimated_usd=_reservation(
            _input_allowance(body).estimated_input_tokens, tokens
        ),
    )


def _reconstruct(
    directory: Path, plan: VertexQualificationPlanV2, call: VertexQualificationCallV2
) -> VertexQualificationCallV2:
    """Regenerate from synthetic source records, without opening mutable stores."""
    spec = _specification(
        directory, call.task_kind, call.mode, plan.max_output_tokens_per_case
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
    dependency = semantic_digest(
        "request-dependencies",
        {
            "request_uid": request.request_uid,
            "configuration_sha256": configuration_digest(spec),
            "record_sha256": record_digest(record),
            "response_sha256": record_digest(record)
            if call.task_kind == "rubric"
            else None,
            "left_sha256": record_digest(responses["001"])
            if call.task_kind == "pairwise"
            else None,
            "right_sha256": record_digest(responses["002"])
            if call.task_kind == "pairwise"
            else None,
        },
    )
    runtime = plan.runtime()
    wire = prepare_production_request(
        spec,
        RequestDependency(request=request, dependency_sha256=dependency),
        physical_identity(
            call.run_id,
            request.request_uid,
            stable_id("attempt", call.run_id, request.request_uid, 1),
        ),
        _qualification_context(spec, source_commit=plan.source_commit, runtime=runtime),
        lookup=lambda kind, identifier: (
            record if kind == "pair" else responses[identifier]
        ),
    )
    return _call(
        call.case_id, call.run_id, spec, wire, runtime, plan.max_output_tokens_per_case
    )


def verify_plan(directory: Path, *, expected_plan_sha256: str) -> dict[str, object]:
    """Read-only independent reconstruction; an external digest anchors admission.

    No store is reopened, no report is written into evidence, and a current clean
    exact-source checkout is mandatory. Rehashing a changed plan cannot replace
    the independently retained expected digest.
    """
    directory = _external(directory)
    if any(path.is_symlink() for path in directory.rglob("*")):
        raise QualificationError()
    raw = (directory / "plan.json").read_bytes()
    digest = bytes_digest(raw)
    if (
        digest != expected_plan_sha256
        or (directory / "plan.sha256").read_bytes() != (digest + "\n").encode()
    ):
        raise QualificationError()
    decode_assessment_output(raw)
    plan = VertexQualificationPlanV2.model_validate_json(raw)
    if (
        canonical_bytes(plan) != raw
        or plan.source_commit != _source_commit()
        or plan.installed_sdk_source_sha256 != installed_sdk_evidence()
        or plan.pricing_evidence_sha256 != _pricing_digest()
        or plan.artifact_sha256 != _artifact_hashes(directory)
    ):
        raise QualificationError()
    for name, body in _inputs().items():
        if (directory / name).read_bytes() != body:
            raise QualificationError()
    for call in plan.calls:
        if _reconstruct(directory, plan, call) != call:
            raise QualificationError()
    if plan.source_commit != _source_commit():
        raise QualificationError()
    return {
        "provider": plan.provider,
        "plan_version": plan.plan_version,
        "plan_sha256": digest,
        "source_commit": plan.source_commit,
        "sdk_version": plan.sdk_version,
        "api_version": plan.api_version,
        "endpoint": plan.endpoint,
        "model": plan.model,
        "authentication_mode": plan.authentication_mode,
        "location": plan.location,
        "redacted_route": plan.base_url
        + "v1beta1/projects/[redacted]/locations/global/publishers/google/models/"
        + MODEL
        + ":generateContent",
        "route_sha256": plan.route_sha256,
        "installed_sdk_source_sha256": plan.installed_sdk_source_sha256,
        "pricing_evidence_sha256": plan.pricing_evidence_sha256,
        "artifact_inventory_sha256": bytes_digest(
            canonical_bytes(plan.artifact_sha256)
        ),
        "account_eligibility": "unverified",
        "key_type_and_project_association": "unverified",
        "planned_requests": 4,
        "authorized_requests": 0,
        "execution_enabled": False,
        "production_native_authority": False,
        "executor_status": plan.executor_status,
        "live_qualified": False,
        "reserved_input_tokens": plan.reserved_input_tokens,
        "reserved_output_tokens_including_thinking": plan.reserved_output_tokens_including_thinking,
        "total_reserved_tokens": plan.total_reserved_tokens,
        "reserved_estimated_usd": str(plan.reserved_estimated_usd),
        "proposed_estimated_authorization_ceiling_usd": "0.02",
        "actual_usage_and_cost": None,
        "cases": [
            {
                "case_id": c.case_id,
                "submission_id": c.transport.identity.submission_id,
                "correlation_id": c.transport.identity.correlation_id,
                "configuration_sha256": c.configuration_file_sha256,
                "prompt_sha256": c.transport.prompt_sha256,
                "payload_sha256": c.transport.payload_sha256,
                "transport_commitment_sha256": c.transport.commitment_sha256,
                "request_url_sha256": c.request_url_sha256,
                "input_allowance": c.input_allowance.model_dump(mode="json"),
                "reserved_estimated_usd": str(c.reserved_estimated_usd),
            }
            for c in plan.calls
        ],
        "limitations": "Offline bytes do not establish serving or account eligibility. Reservations are estimates, not verified tokenization or billing caps. Key type, project association, billing, enabled API, IAM and restrictions require independent approved cloud setup. Express Mode keys are incompatible; no route or authentication fallback is allowed. A separate reviewed, merged executor and exact-plan/source billable and full synthetic capture consent are required.",
    }


def _artifact_hashes(directory: Path) -> dict[str, str]:
    """Commit every preparation artifact; reject later edits or injected grants."""
    return {
        p.relative_to(directory).as_posix(): bytes_digest(p.read_bytes())
        for p in directory.rglob("*")
        if p.is_file()
        and p.relative_to(directory).as_posix() not in {"plan.json", "plan.sha256"}
    }
