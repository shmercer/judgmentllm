"""Exact Vertex production bodies, distinct from sealed synthetic V3 packets.

No credentials, SDK clients or network operations belong in this module. Every
physical body is committed before submission; SDK serialization must reproduce
these bytes exactly or the authenticated transport must refuse to forward it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import Field, model_validator

from judgmentllm.config import NativeTransportMechanism, RunSpecV3, validate_run_spec
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.native_schema import NativeProjectionScopeV1
from judgmentllm.native_schema_v2 import (
    NativeSchemaProjectionV2,
    project_native_schema_v2,
)
from judgmentllm.native_selection import NativeFeature, _features
from judgmentllm.output_commitments import (
    MemberPropertyOrder,
    _property_orders,
)
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    PairwiseOutputContract,
    decode_assessment_output,
    generate_output_schema,
    serialize_output_schema,
)
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import SHA256
from judgmentllm.planning import PlannedRequest, validate_generation
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.providers.protocol import PreparedRequest
from judgmentllm.transport import (
    PhysicalRequestIdentityV1,
    TransportIntegrityError,
    TransportPlanV1,
    _contract,
    _preparation_token,
    _TransportModel,
)
from judgmentllm.vertex_config import VertexRuntimeConfiguration, runtime_configuration
from judgmentllm.vertex_registry import (
    VERTEX_API_SCOPE,
    VERTEX_API_VERSION,
    VERTEX_PRODUCTION_SDK_VERSION,
    VertexMechanism,
    production_generation_capabilities,
)

# Reuse immutable identity and protected rendering primitives, never synthetic
# authority or its payload parser.
# pyright: reportPrivateUsage=false


def project_vertex_schema(
    source: AssessmentOutputContract,
    *,
    scope: NativeProjectionScopeV1,
    mechanism: VertexMechanism,
) -> NativeSchemaProjectionV2:
    return project_native_schema_v2(source, scope=scope, mechanism=mechanism)


VertexProductionContextKind = Literal[
    "vertex-production-v1",
    "vertex-qualification-candidate-v1",
]


@dataclass(frozen=True, repr=False)
class VertexProductionContext:
    """Descriptive scoped authority; qualification context is never call consent.

    Ordinary execution accepts only the production kind. Candidate contexts
    alone compile bodies offline only. The separate repository qualification
    executor must supply exact-plan one-shot authority at the HTTP boundary.
    """

    scope: NativeProjectionScopeV1
    runtime: VertexRuntimeConfiguration
    kind: VertexProductionContextKind = "vertex-production-v1"
    source_commit: str | None = None

    @property
    def api_scope(self) -> Literal["vertex-generate-content-v1beta1-v1"]:
        return "vertex-generate-content-v1beta1-v1"

    @property
    def trusted_synthetic(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "VertexProductionContext(<scoped production authority>)"


def _checked_context(context: VertexProductionContext) -> VertexProductionContext:
    if type(context) is not VertexProductionContext or set(vars(context)) != set(
        VertexProductionContext.__dataclass_fields__
    ):
        raise TransportIntegrityError()
    scope = NativeProjectionScopeV1.model_validate(dict(context.scope))
    if (
        scope.provider != "vertex"
        or scope.endpoint != "models.generate_content"
        or scope.model_scope != "exact"
        or scope.model is None
        or scope.model.startswith("judgmentllm-synthetic-")
        or scope.sdk_package != "google-genai"
        or scope.sdk_version != VERTEX_PRODUCTION_SDK_VERSION
        or scope.api_version != VERTEX_API_VERSION
        or context.kind
        not in {
            "vertex-production-v1",
            "vertex-qualification-candidate-v1",
        }
    ):
        raise TransportIntegrityError()
    qualifier = context.kind != "vertex-production-v1"
    if qualifier != (context.source_commit is not None):
        raise TransportIntegrityError()
    if context.source_commit is not None and not re.fullmatch(
        r"[0-9a-f]{40}", context.source_commit
    ):
        raise TransportIntegrityError()
    if type(context.runtime) is not VertexRuntimeConfiguration:
        raise TransportIntegrityError()
    runtime = VertexRuntimeConfiguration(**vars(context.runtime))
    runtime.resource_path(scope.model)
    return VertexProductionContext(
        scope=scope,
        runtime=runtime,
        kind=context.kind,
        source_commit=context.source_commit,
    )


def production_context(spec: RunSpecV3) -> VertexProductionContext:
    """Create a credential-free exact scope for ordinary production preparation."""
    checked = validate_run_spec(spec)
    if not isinstance(checked, RunSpecV3):
        raise TransportIntegrityError()
    return production_context_for_identity(
        checked.provider, checked.endpoint, checked.model
    )


def production_context_for_identity(
    provider: str,
    endpoint: str,
    model: str,
    *,
    runtime: VertexRuntimeConfiguration | None = None,
) -> VertexProductionContext:
    """Create exact credential-free scope before reopening frozen sources."""
    return _checked_context(
        VertexProductionContext(
            runtime=runtime or runtime_configuration(),
            scope=NativeProjectionScopeV1(
                provider=provider,
                endpoint=endpoint,
                model=model,
                model_scope="exact",
                sdk_package="google-genai",
                sdk_version=VERTEX_PRODUCTION_SDK_VERSION,
                api_version=VERTEX_API_VERSION,
            ),
        )
    )


def _qualification_context(  # pyright: ignore[reportUnusedFunction]
    spec: RunSpecV3,
    *,
    source_commit: str,
    runtime: VertexRuntimeConfiguration | None = None,
) -> VertexProductionContext:
    """Private offline probe compilation; this does not grant API execution."""
    original = (
        production_context(spec)
        if runtime is None
        else production_context_for_identity(
            spec.provider, spec.endpoint, spec.model, runtime=runtime
        )
    )
    scope = original.scope
    return _checked_context(
        VertexProductionContext(
            scope=scope,
            runtime=original.runtime,
            kind="vertex-qualification-candidate-v1",
            source_commit=source_commit,
        )
    )


def _authority_sha256(context: VertexProductionContext) -> str:
    return semantic_digest(
        "vertex-production-authority-v1",
        {"kind": context.kind, "profiles": [], "source_commit": context.source_commit},
    )


def production_context_digest(context: VertexProductionContext) -> str:
    """Immutable exact-scope registry/context identity, containing no secrets."""
    try:
        checked = _checked_context(context)
        return semantic_digest(
            "vertex-production-context-v1",
            {
                "scope": checked.scope,
                "api_scope": VERTEX_API_SCOPE,
                "route_sha256": checked.runtime.route_sha256,
                "authority_sha256": _authority_sha256(checked),
            },
        )
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise TransportIntegrityError() from None


class VertexSelectionV1(_TransportModel):
    """Frozen Vertex choice; the empty registry grants no production native route."""

    selection_version: Literal[1] = 1
    kind: Literal["text", "qualification_probe", "inherited"]
    policy: Literal["auto", "native", "text"]
    requested_mechanism: NativeTransportMechanism | None
    mechanism: VertexMechanism | Literal["text"]
    native_support: Literal["unknown"] = "unknown"
    authority_kind: Literal["unqualified", "qualification_probe"]
    features: tuple[NativeFeature, ...]
    authority_sha256: SHA256
    parent_commitment_sha256: SHA256 | None = None
    local_validation_required: Literal[True] = True

    @model_validator(mode="after")
    def exact_choice(self) -> VertexSelectionV1:
        native = self.mechanism != "text"
        if (
            len(set(self.features)) != len(self.features)
            or not self.features
            or (self.kind == "inherited") != (self.parent_commitment_sha256 is not None)
            or (self.policy == "native" and not native)
            or (self.policy == "text" and native)
            or (native and self.requested_mechanism != self.mechanism)
            or (native and self.authority_kind != "qualification_probe")
            or (self.kind == "text" and native)
            or (self.kind == "qualification_probe" and not native)
        ):
            raise ValueError("Vertex selection conflicts")
        return self


def _verify_selection(
    selection: VertexSelectionV1,
    spec: RunSpecV3,
    contract: AssessmentOutputContract,
    context: VertexProductionContext,
    parent: TransportCommitmentV5 | None,
) -> None:
    qualifier = context.kind == "vertex-qualification-candidate-v1"
    native = selection.mechanism != "text"
    if (
        selection.policy != spec.prompt.structured_output
        or selection.requested_mechanism != spec.prompt.native_mechanism
        or selection.features != _features(contract)
        or selection.authority_sha256 != _authority_sha256(context)
        or selection.authority_kind
        != ("qualification_probe" if qualifier else "unqualified")
        or selection.mechanism not in {"text", spec.prompt.native_mechanism}
        or (native and not qualifier)
        or (not native and selection.policy == "auto" and spec.strict_capabilities)
        or (parent is None) != (selection.parent_commitment_sha256 is None)
        or (
            parent is not None
            and (
                selection.parent_commitment_sha256 != parent.commitment_sha256
                or selection.mechanism != parent.mechanism
                or selection.policy != parent.selection.policy
                or selection.requested_mechanism != parent.requested_mechanism
            )
        )
        or selection.kind
        != (
            "inherited"
            if parent is not None
            else "qualification_probe"
            if native
            else "text"
        )
    ):
        raise TransportIntegrityError()


def _selection(
    spec: RunSpecV3,
    contract: AssessmentOutputContract,
    context: VertexProductionContext,
    parent: TransportCommitmentV5 | None,
) -> VertexSelectionV1:
    policy = spec.prompt.structured_output
    qualifier = context.kind == "vertex-qualification-candidate-v1"
    if parent is not None:
        mechanism = parent.mechanism
    elif policy == "text":
        mechanism = "text"
    elif qualifier:
        mechanism = cast(VertexMechanism, spec.prompt.native_mechanism)
    elif policy == "native" or spec.strict_capabilities:
        raise ValueError(
            "Vertex native output requires independently reviewed exact-scope authority"
        )
    else:
        mechanism = "text"
    result = VertexSelectionV1(
        kind="inherited"
        if parent is not None
        else "qualification_probe"
        if mechanism != "text"
        else "text",
        policy=policy,
        requested_mechanism=spec.prompt.native_mechanism,
        mechanism=mechanism,
        authority_kind="qualification_probe" if qualifier else "unqualified",
        features=_features(contract),
        authority_sha256=_authority_sha256(context),
        parent_commitment_sha256=None if parent is None else parent.commitment_sha256,
    )
    _verify_selection(result, spec, contract, context, parent)
    return result


class VertexRequestPolicyV1(_TransportModel):
    """Endpoint-owned single-attempt policy, separate from requested generation."""

    policy_version: Literal[1] = 1
    candidate_count: Literal[1] = 1
    automatic_continuation: Literal[False] = False
    automatic_function_calling: Literal[False] = False
    sdk_retries: Literal[0] = 0
    connect_write_pool_timeout_seconds: Literal[10] = 10
    read_timeout_seconds: Literal[60] = 60
    operation_deadline_seconds: Literal[90] = 90
    reasoning_retention: Literal["none"] = "none"
    cache: Literal["off", "auto_implicit_only"]


def _request_policy(spec: RunSpecV3) -> VertexRequestPolicyV1:
    if spec.execution.mode != "live":
        raise ValueError("Vertex batch and managed bulk-live belong to Milestone 5")
    if spec.execution.tier is not None or spec.execution.cache_policy == "explicit":
        raise ValueError("Vertex AI tier and explicit caching are unavailable")
    return VertexRequestPolicyV1(
        cache="off" if spec.execution.cache_policy == "off" else "auto_implicit_only"
    )


class OutputCommitmentV7(_TransportModel):
    """Production output commitment; canonical contract and projection stay V2/V2."""

    commitment_version: Literal[7] = 7
    renderer_version: Literal[3] = 3
    scope: NativeProjectionScopeV1 = Field(repr=False)
    route_sha256: SHA256
    contract: AssessmentOutputContract = Field(repr=False)
    selection: VertexSelectionV1
    request_policy: VertexRequestPolicyV1
    schema_text: str = Field(repr=False)
    schema_sha256: SHA256
    neutral_semantic_sha256: SHA256
    projection: NativeSchemaProjectionV2 | None = Field(default=None, repr=False)
    sdk_config_fragment_text: str | None = Field(default=None, repr=False)
    repetition: Literal["canonical_neutral", "omit"]
    context_sha256: SHA256
    local_validation_required: Literal[True] = True

    @model_validator(mode="after")
    def exact_representation(self) -> OutputCommitmentV7:
        raw = serialize_output_schema(self.contract)
        native = self.selection.mechanism != "text"
        if (
            self.schema_text.encode("utf-8") != raw
            or self.schema_sha256 != bytes_digest(raw)
            or self.neutral_semantic_sha256
            != semantic_digest(
                "neutral-schema-semantics-v1", generate_output_schema(self.contract)
            )
            or self.selection.features != _features(self.contract)
            or native != (self.projection is not None)
            or self.repetition != ("omit" if native else "canonical_neutral")
        ):
            raise ValueError("production output representation differs")
        if self.projection is None:
            if self.sdk_config_fragment_text is not None:
                raise ValueError("text output cannot contain native configuration")
        elif (
            self.projection
            != project_vertex_schema(
                self.contract,
                scope=self.scope,
                mechanism=cast(VertexMechanism, self.selection.mechanism),
            )
            or self.projection.mechanism != self.selection.mechanism
            or self.sdk_config_fragment_text != self.projection.sdk_config_fragment_text
        ):
            raise ValueError("production native representation differs")
        return self

    @property
    def root_properties(self) -> tuple[str, ...]:
        return _property_orders(self.contract)[0]

    @property
    def member_properties(self) -> tuple[MemberPropertyOrder, ...]:
        return _property_orders(self.contract)[1]

    @property
    def commitment_sha256(self) -> str:
        return semantic_digest("vertex-output-commitment-v7", self)


def _build_output_commitment_v7(
    spec: RunSpecV3,
    request: PlannedRequest,
    context: VertexProductionContext,
    *,
    inherited_commitment: TransportCommitmentV5 | None = None,
    frozen_selection: VertexSelectionV1 | None = None,
) -> OutputCommitmentV7:
    """Select once, or verify an original frozen choice without resolving auto."""
    context = _checked_context(context)
    _validate_scope(spec, context)
    contract = _contract(spec, request)
    selection = (
        _selection(spec, contract, context, inherited_commitment)
        if frozen_selection is None
        else VertexSelectionV1.model_validate(dict(frozen_selection))
    )
    _verify_selection(selection, spec, contract, context, inherited_commitment)
    projection = (
        None
        if selection.mechanism == "text"
        else project_vertex_schema(
            contract,
            scope=context.scope,
            mechanism=selection.mechanism,
        )
    )
    raw = serialize_output_schema(contract)
    return OutputCommitmentV7(
        scope=context.scope,
        route_sha256=context.runtime.route_sha256,
        contract=contract,
        selection=selection,
        request_policy=_request_policy(spec),
        schema_text=raw.decode("utf-8"),
        schema_sha256=bytes_digest(raw),
        neutral_semantic_sha256=semantic_digest(
            "neutral-schema-semantics-v1", generate_output_schema(contract)
        ),
        projection=projection,
        sdk_config_fragment_text=None
        if projection is None
        else projection.sdk_config_fragment_text,
        repetition="canonical_neutral" if projection is None else "omit",
        context_sha256=production_context_digest(context),
    )


def build_output_commitment_v7(
    spec: RunSpecV3,
    request: PlannedRequest,
    context: VertexProductionContext,
    *,
    inherited_commitment: TransportCommitmentV5 | None = None,
    frozen_selection: VertexSelectionV1 | None = None,
) -> OutputCommitmentV7:
    """Sanitized boundary for new selection and pinned output reconstruction."""
    try:
        return _build_output_commitment_v7(
            spec,
            request,
            context,
            inherited_commitment=inherited_commitment,
            frozen_selection=frozen_selection,
        )
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise TransportIntegrityError() from None


def model_path(model: str) -> str:
    """Bare Google publisher IDs only; availability remains unknown."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", model) or ".." in model:
        raise TransportIntegrityError()
    return "publishers/google/models/" + model


def build_vertex_request_body(
    *,
    endpoint: str,
    model: str,
    prompt: str,
    output: OutputCommitmentV7,
    generation: object = None,
) -> bytes:
    """Exact google-genai 2.34 HTTP serialization, including spaces and escapes.

    Typed sampling controls become floats in the SDK; the user's original
    numeric representations remain in the independent generation commitment.
    """
    from judgmentllm.config import GenerationSpec

    model_path(model)
    if (
        endpoint != "models.generate_content"
        or (output.scope.provider, output.scope.endpoint, output.scope.model)
        != ("vertex", endpoint, model)
        or type(prompt) is not str
    ):
        raise TransportIntegrityError()
    settings = (
        GenerationSpec.model_validate(dict(generation))
        if isinstance(generation, GenerationSpec)
        else None
    )
    if generation is not None and settings is None:
        raise TransportIntegrityError()
    config: dict[str, object] = {}
    if settings is not None:
        capabilities = production_generation_capabilities(output.scope)
        if any(
            getattr(capabilities, name).supported is False
            for name in settings.model_dump(exclude_none=True)
        ):
            raise TransportIntegrityError()
    if settings is not None:
        if settings.temperature is not None:
            if settings.temperature > 2:
                raise TransportIntegrityError()
            config["temperature"] = float(settings.temperature)
        if settings.top_p is not None:
            config["topP"] = float(settings.top_p)
    config["candidateCount"] = output.request_policy.candidate_count
    if settings is not None and settings.max_output_tokens is not None:
        config["maxOutputTokens"] = settings.max_output_tokens
    if output.projection is not None:
        config.update(
            cast(
                dict[str, object],
                decode_assessment_output(output.projection.format_fragment_text),
            )
        )
    values = {
        "contents": [{"parts": [{"text": prompt}], "role": "user"}],
        "generationConfig": config,
    }
    return json.dumps(values, ensure_ascii=True, allow_nan=False).encode("utf-8")


class TransportCommitmentV5(_TransportModel):
    """Safe production receipt, never an authentication or billable-call grant."""

    transport_commitment_version: Literal[5] = 5
    transport_protocol: Literal["vertex-production-v1"] = "vertex-production-v1"
    api_scope: Literal["vertex-generate-content-v1beta1-v1"] = (
        "vertex-generate-content-v1beta1-v1"
    )
    renderer_version: Literal[3] = 3
    context_kind: VertexProductionContextKind
    scope: NativeProjectionScopeV1 = Field(repr=False)
    request_policy: VertexRequestPolicyV1
    route_sha256: SHA256
    identity: PhysicalRequestIdentityV1
    configuration_sha256: SHA256
    context_sha256: SHA256
    dependency_sha256: SHA256
    membership_sha256: SHA256
    output_commitment_sha256: SHA256
    plan_sha256: SHA256
    mechanism: VertexMechanism | Literal["text"]
    requested_mechanism: NativeTransportMechanism | None
    selection: VertexSelectionV1
    parent_commitment_sha256: SHA256 | None = None
    projection_version: Literal[2] | None = None
    projection_sha256: SHA256 | None = None
    neutral_schema_sha256: SHA256
    neutral_semantic_sha256: SHA256
    native_schema_sha256: SHA256 | None = None
    native_schema_bytes: int | None = Field(default=None, ge=0)
    sdk_config_sha256: SHA256 | None = None
    sdk_config_bytes: int | None = Field(default=None, ge=0)
    format_sha256: SHA256 | None = None
    format_bytes: int | None = Field(default=None, ge=0)
    prompt_sha256: SHA256
    prompt_bytes: int = Field(ge=0)
    payload_sha256: SHA256
    payload_bytes: int = Field(ge=0)
    local_validation_required: Literal[True] = True
    commitment_sha256: SHA256

    @model_validator(mode="after")
    def exact_commitment(self) -> TransportCommitmentV5:
        native = self.mechanism != "text"
        if (
            self.scope.provider != "vertex"
            or self.scope.endpoint != "models.generate_content"
            or self.scope.model_scope != "exact"
            or self.scope.sdk_package != "google-genai"
            or self.scope.sdk_version != VERTEX_PRODUCTION_SDK_VERSION
            or self.scope.api_version != VERTEX_API_VERSION
            or self.selection.mechanism != self.mechanism
            or self.selection.requested_mechanism != self.requested_mechanism
            or self.selection.parent_commitment_sha256 != self.parent_commitment_sha256
        ):
            raise ValueError("production receipt selection differs")
        for value in (
            self.projection_version,
            self.projection_sha256,
            self.native_schema_sha256,
            self.native_schema_bytes,
            self.sdk_config_sha256,
            self.sdk_config_bytes,
            self.format_sha256,
            self.format_bytes,
        ):
            if native != (value is not None):
                raise ValueError("production receipt artifacts differ")
        plan = TransportPlanV1(
            identity=self.identity,
            configuration_sha256=self.configuration_sha256,
            dependency_sha256=self.dependency_sha256,
            membership_sha256=self.membership_sha256,
            output_commitment_sha256=self.output_commitment_sha256,
            prompt_sha256=self.prompt_sha256,
            context_sha256=self.context_sha256,
            parent_commitment_sha256=self.parent_commitment_sha256,
        )
        if (
            self.plan_sha256 != plan.plan_sha256
            or self.commitment_sha256
            != semantic_digest(
                "vertex-transport-commitment-v5",
                self.model_dump(mode="json", exclude={"commitment_sha256"}),
            )
        ):
            raise ValueError("production receipt identity differs")
        return self


def _receipt(
    plan: TransportPlanV1,
    output: OutputCommitmentV7,
    prompt: bytes,
    payload: bytes,
    context_kind: VertexProductionContextKind,
) -> TransportCommitmentV5:
    projection = output.projection
    native = (
        None if projection is None else projection.native_schema_text.encode("utf-8")
    )
    fragment = (
        None if projection is None else projection.format_fragment_text.encode("utf-8")
    )
    sdk_config = (
        None
        if projection is None
        else projection.sdk_config_fragment_text.encode("utf-8")
    )
    values = plan.model_dump(mode="json", exclude={"plan_version"})
    values.update(
        schema_version=1,
        transport_commitment_version=5,
        transport_protocol="vertex-production-v1",
        api_scope=VERTEX_API_SCOPE,
        renderer_version=3,
        context_kind=context_kind,
        scope=output.scope.model_dump(mode="json"),
        request_policy=output.request_policy.model_dump(mode="json"),
        route_sha256=output.route_sha256,
        plan_sha256=plan.plan_sha256,
        mechanism=output.selection.mechanism,
        requested_mechanism=output.selection.requested_mechanism,
        selection=output.selection.model_dump(mode="json"),
        projection_version=None if projection is None else 2,
        projection_sha256=None if projection is None else projection.projection_sha256,
        neutral_schema_sha256=output.schema_sha256,
        neutral_semantic_sha256=output.neutral_semantic_sha256,
        native_schema_sha256=None if native is None else bytes_digest(native),
        native_schema_bytes=None if native is None else len(native),
        sdk_config_sha256=None if sdk_config is None else bytes_digest(sdk_config),
        sdk_config_bytes=None if sdk_config is None else len(sdk_config),
        format_sha256=None if fragment is None else bytes_digest(fragment),
        format_bytes=None if fragment is None else len(fragment),
        prompt_sha256=bytes_digest(prompt),
        prompt_bytes=len(prompt),
        payload_sha256=bytes_digest(payload),
        payload_bytes=len(payload),
        local_validation_required=True,
    )
    return TransportCommitmentV5.model_validate_json(
        canonical_bytes(
            {
                **values,
                "commitment_sha256": semantic_digest(
                    "vertex-transport-commitment-v5", values
                ),
            }
        )
    )


class PreparedRequestV7(PreparedRequest, _TransportModel):
    """Exact API body with independently retained production commitments."""

    preparation_version: Literal[7] = 7
    payload: bytes = Field(repr=False)
    logical_request: PlannedRequest = Field(repr=False)
    output_commitment: OutputCommitmentV7 = Field(repr=False)
    transport_plan: TransportPlanV1
    transport_commitment: TransportCommitmentV5
    rendered_text: str = Field(repr=False)

    @model_validator(mode="after")
    def exact_production_body(self) -> PreparedRequestV7:
        output = self.output_commitment
        plan = self.transport_plan
        receipt = self.transport_commitment
        request = self.logical_request
        contract = output.contract
        membership = (
            request.pair == contract.pair
            and request.trait_ids == (contract.pair.trait_id,)
            if isinstance(contract, PairwiseOutputContract)
            else request.pair is None
            and request.trait_ids == tuple(trait.trait_id for trait in contract.traits)
        )
        if (
            not membership
            or (self.provider, self.endpoint, self.model)
            != (output.scope.provider, output.scope.endpoint, output.scope.model)
            or self.request_uid != request.request_uid
            or self.task_ids != tuple(task.task_id for task in request.tasks)
            or request.trait_ids != tuple(task.trait_id for task in request.tasks)
            or self.payload
            != build_vertex_request_body(
                endpoint=self.endpoint,
                model=self.model,
                prompt=self.rendered_text,
                output=output,
                generation=self.generation,
            )
            or plan.identity.request_uid != self.request_uid
            or plan.context_sha256 != output.context_sha256
            or plan.output_commitment_sha256 != output.commitment_sha256
            or plan.parent_commitment_sha256
            != output.selection.parent_commitment_sha256
            or plan.prompt_sha256 != self.prompt_sha256
            or plan.dependency_sha256 != self.dependency_sha256
            or plan.membership_sha256
            != semantic_digest("attempt-membership", list(self.task_ids))
            or receipt
            != _receipt(
                plan,
                output,
                self.rendered_text.encode("utf-8"),
                self.payload,
                receipt.context_kind,
            )
            or receipt.scope != output.scope
            or receipt.route_sha256 != output.route_sha256
        ):
            raise ValueError("production physical binding differs")
        return self

    def content_artifacts(self) -> dict[str, bytes]:
        output = self.output_commitment
        artifacts = {
            "output_commitment": canonical_bytes(output),
            "transport_plan": canonical_bytes(self.transport_plan),
            "neutral_schema": output.schema_text.encode("utf-8"),
            "prompt": self.rendered_text.encode("utf-8"),
            "payload": self.payload,
        }
        if output.projection is not None:
            artifacts.update(
                native_schema=output.projection.native_schema_text.encode("utf-8"),
                sdk_configuration=cast(str, output.sdk_config_fragment_text).encode(
                    "utf-8"
                ),
                format_fragment=output.projection.format_fragment_text.encode("utf-8"),
            )
        return artifacts


def _validate_scope(spec: RunSpecV3, context: VertexProductionContext) -> None:
    checked = validate_run_spec(spec)
    if not isinstance(checked, RunSpecV3) or (
        checked.provider,
        checked.endpoint,
        checked.model,
    ) != (context.scope.provider, context.scope.endpoint, context.scope.model):
        raise TransportIntegrityError()
    model_path(checked.model)
    _request_policy(checked)
    report = validate_generation(
        checked, capabilities=production_generation_capabilities(context.scope)
    )
    if not report.valid:
        raise ValueError("Generation settings fail exact-scope capability validation.")


def prepare_production_request(
    spec: RunSpecV3,
    dependency: RequestDependency,
    identity: PhysicalRequestIdentityV1,
    context: VertexProductionContext,
    *,
    template: str | None = None,
    lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask]
    | None = None,
    frozen: TransportCommitmentV5 | None = None,
    parent: TransportCommitmentV5 | None = None,
    committed_output: OutputCommitmentV7 | None = None,
) -> PreparedRequestV7:
    """Prepare before sealing, or verify immutable reconstruction without fallback."""
    from judgmentllm.vertex_prompts import render_vertex_request

    try:
        token = _preparation_token((spec, dependency, identity, context))
        if token is None:
            raise TransportIntegrityError()
        original = (spec, dependency, identity, context)
        spec, dependency, identity, context = deepcopy(original)
        context = _checked_context(context)
        _validate_scope(spec, context)
        context_digest = production_context_digest(context)
        if identity.request_uid != dependency.request.request_uid:
            raise TransportIntegrityError()
        if parent is not None:
            parent = TransportCommitmentV5.model_validate(dict(parent))
            if (
                parent.identity.run_id != identity.run_id
                or parent.identity.request_uid != identity.request_uid
                or parent.identity.attempt_id == identity.attempt_id
                or parent.context_sha256 != context_digest
                or parent.configuration_sha256
                != semantic_digest("run-configuration", spec)
                or parent.scope != context.scope
                or parent.context_kind != context.kind
            ):
                raise TransportIntegrityError()
        if frozen is not None:
            frozen = TransportCommitmentV5.model_validate(dict(frozen))
            if frozen.identity != identity or frozen.context_sha256 != context_digest:
                raise TransportIntegrityError()
        output = build_output_commitment_v7(
            spec,
            dependency.request,
            context,
            inherited_commitment=parent,
            frozen_selection=None if frozen is None else frozen.selection,
        )
        if (
            committed_output is not None
            and OutputCommitmentV7.model_validate(dict(committed_output)) != output
        ):
            raise TransportIntegrityError()
        if lookup is None:
            response = dependency.request.response
            if response is None:
                raise TransportIntegrityError()

            def only_response(
                kind: Literal["response", "pair"], identifier: str
            ) -> ResponseItem | PairTask:
                if kind != "response" or response.item_id != identifier:
                    raise TransportIntegrityError()
                return response

            lookup = only_response
        rendered = render_vertex_request(
            spec, template, dependency.request, lookup, output
        )
        prompt = rendered.text.encode("utf-8")
        payload = build_vertex_request_body(
            endpoint=spec.endpoint,
            model=spec.model,
            prompt=rendered.text,
            output=output,
            generation=spec.generation,
        )
        plan = TransportPlanV1(
            identity=identity,
            configuration_sha256=semantic_digest("run-configuration", spec),
            dependency_sha256=dependency.dependency_sha256,
            membership_sha256=semantic_digest(
                "attempt-membership",
                [task.task_id for task in dependency.request.tasks],
            ),
            output_commitment_sha256=output.commitment_sha256,
            prompt_sha256=bytes_digest(prompt),
            context_sha256=context_digest,
            parent_commitment_sha256=output.selection.parent_commitment_sha256,
        )
        receipt = _receipt(plan, output, prompt, payload, context.kind)
        if frozen is not None and frozen != receipt:
            raise TransportIntegrityError()
        prepared = PreparedRequestV7(
            request_uid=dependency.request.request_uid,
            task_ids=tuple(task.task_id for task in dependency.request.tasks),
            dependency_sha256=dependency.dependency_sha256,
            prompt_sha256=bytes_digest(prompt),
            payload_sha256=bytes_digest(payload),
            provider=spec.provider,
            endpoint=spec.endpoint,
            model=spec.model,
            generation=spec.generation,
            payload=payload,
            logical_request=dependency.request,
            output_commitment=output,
            transport_plan=plan,
            transport_commitment=receipt,
            rendered_text=rendered.text,
        )
        if (
            token != _preparation_token(original)
            or production_context_digest(original[3]) != context_digest
        ):
            raise TransportIntegrityError()
        return prepared
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
        RecursionError,
    ):
        raise TransportIntegrityError() from None


# Descriptive alias used by lifecycle preview; no new contract version.
build_production_output_commitment = build_output_commitment_v7
