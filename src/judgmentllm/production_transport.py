"""Exact OpenAI production bodies, distinct from sealed synthetic V3 packets.

No credentials, SDK clients or network operations belong in this module. Every
physical body is committed before submission; SDK serialization must reproduce
these bytes exactly or the authenticated transport must refuse to forward it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import Field, field_validator, model_validator

from judgmentllm.config import (
    GenerationSpec,
    NativeTransportMechanism,
    RunSpecV3,
    validate_run_spec,
)
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.native_schema import NativeProjectionScopeV1, NativeSchemaProjectionV1
from judgmentllm.native_selection import NativeFeature, _features
from judgmentllm.output_commitments import (
    MemberPropertyOrder,
    _property_orders,
    ordered_json_bytes,
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
from judgmentllm.presentation import PresentationVersion
from judgmentllm.production_registry import (
    OPENAI_API_SCOPE,
    OPENAI_PRODUCTION_SDK_VERSION,
    _profile_for,
    production_generation_capabilities,
)
from judgmentllm.providers.openai_schema import project_openai_schema
from judgmentllm.providers.protocol import PreparedRequest
from judgmentllm.providers.reasoning import (
    build_reasoning_policy,
    generation_capabilities,
    qualified_reasoning,
    verify_policy_scope,
    wire_controls,
)
from judgmentllm.reasoning import ReasoningPolicyV1
from judgmentllm.service_tiers import (
    ServicePolicyV2,
    ServicePolicyVersion,
    qualified_service,
    validate_service_support,
)
from judgmentllm.service_tiers import (
    service_policy as resolve_service_policy,
)
from judgmentllm.transport import (
    PhysicalRequestIdentityV1,
    TransportIntegrityError,
    TransportPlanV1,
    _contract,
    _preparation_token,
    _TransportModel,
)

# Reuse immutable identity and protected rendering primitives, never synthetic
# authority or its payload parser.
# pyright: reportPrivateUsage=false

ProductionContextKind = Literal[
    "openai-production-v1",
    "openai-qualification-candidate-v1",
    "openai-study-candidate-v1",
    "openai-qualification-v1",
]
OpenAIMechanism = Literal["openai.responses.text.format", "openai.chat.response_format"]


@dataclass(frozen=True, repr=False)
class ProductionContext:
    """Descriptive scoped authority; qualification context is never call consent.

    Ordinary execution accepts only the production kind. The dedicated harness
    separately verifies a bounded grant before submitting a qualification seal.
    Candidate contexts can compile bodies offline and cannot execute.
    """

    scope: NativeProjectionScopeV1
    kind: ProductionContextKind = "openai-production-v1"
    qualification_plan_sha256: str | None = None
    source_commit: str | None = None

    @property
    def api_scope(self) -> Literal["openai-v1"]:
        return "openai-v1"

    @property
    def trusted_synthetic(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "ProductionContext(<scoped production authority>)"


def _checked_context(context: ProductionContext) -> ProductionContext:
    if type(context) is not ProductionContext or set(vars(context)) != set(
        ProductionContext.__dataclass_fields__
    ):
        raise TransportIntegrityError()
    scope = NativeProjectionScopeV1.model_validate(dict(context.scope))
    if (
        scope.provider != "openai"
        or scope.endpoint not in {"responses", "chat.completions"}
        or scope.model_scope != "exact"
        or scope.model is None
        or scope.model.startswith("judgmentllm-synthetic-")
        or scope.sdk_package != "openai"
        or scope.sdk_version != OPENAI_PRODUCTION_SDK_VERSION
        or scope.api_version is not None
        or context.kind
        not in {
            "openai-production-v1",
            "openai-qualification-candidate-v1",
            "openai-study-candidate-v1",
            "openai-qualification-v1",
        }
    ):
        raise TransportIntegrityError()
    qualifier = context.kind != "openai-production-v1"
    if qualifier != (context.source_commit is not None):
        raise TransportIntegrityError()
    if context.source_commit is not None and not re.fullmatch(
        r"[0-9a-f]{40}", context.source_commit
    ):
        raise TransportIntegrityError()
    if (context.kind == "openai-qualification-v1") != (
        context.qualification_plan_sha256 is not None
    ):
        raise TransportIntegrityError()
    if context.qualification_plan_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", context.qualification_plan_sha256
    ):
        raise TransportIntegrityError()
    return ProductionContext(
        scope=scope,
        kind=context.kind,
        qualification_plan_sha256=context.qualification_plan_sha256,
        source_commit=context.source_commit,
    )


def production_context(spec: RunSpecV3) -> ProductionContext:
    """Create a credential-free exact scope for ordinary production preparation."""
    checked = validate_run_spec(spec)
    if not isinstance(checked, RunSpecV3):
        raise TransportIntegrityError()
    return production_context_for_identity(
        checked.provider, checked.endpoint, checked.model
    )


def production_context_for_identity(
    provider: str, endpoint: str, model: str
) -> ProductionContext:
    """Create exact credential-free scope before reopening frozen sources."""
    return _checked_context(
        ProductionContext(
            scope=NativeProjectionScopeV1(
                provider=provider,
                endpoint=endpoint,
                model=model,
                model_scope="exact",
                sdk_package="openai",
                sdk_version=OPENAI_PRODUCTION_SDK_VERSION,
            )
        )
    )


def _qualification_context(  # pyright: ignore[reportUnusedFunction]
    spec: RunSpecV3, *, source_commit: str, plan_sha256: str | None = None
) -> ProductionContext:
    """Private offline probe compilation; this does not grant API execution."""
    scope = production_context(spec).scope
    return _checked_context(
        ProductionContext(
            scope=scope,
            kind="openai-qualification-candidate-v1"
            if plan_sha256 is None
            else "openai-qualification-v1",
            qualification_plan_sha256=plan_sha256,
            source_commit=source_commit,
        )
    )


def _authority_sha256(context: ProductionContext) -> str:
    profile = _profile_for(context.scope)
    return semantic_digest(
        "openai-production-authority-v1",
        {
            "kind": context.kind,
            "profile": profile,
            "qualification_plan_sha256": context.qualification_plan_sha256,
            "source_commit": context.source_commit,
        },
    )


def production_context_digest(context: ProductionContext) -> str:
    """Immutable exact-scope registry/context identity, containing no secrets."""
    try:
        checked = _checked_context(context)
        return semantic_digest(
            "openai-production-context-v1",
            {
                "scope": checked.scope,
                "api_scope": OPENAI_API_SCOPE,
                "authority_sha256": _authority_sha256(checked),
            },
        )
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise TransportIntegrityError() from None


class ProductionSelectionV1(_TransportModel):
    """Frozen production choice independent of historical synthetic decisions."""

    selection_version: Literal[1] = 1
    kind: Literal["text", "native", "qualification_probe", "inherited"]
    policy: Literal["auto", "native", "text"]
    requested_mechanism: NativeTransportMechanism | None
    mechanism: OpenAIMechanism | Literal["text"]
    native_support: Literal["supported", "unsupported", "unknown"]
    authority_kind: Literal["reviewed", "unqualified", "qualification_probe"]
    features: tuple[NativeFeature, ...]
    authority_sha256: SHA256
    parent_commitment_sha256: SHA256 | None = None
    local_validation_required: Literal[True] = True

    @model_validator(mode="after")
    def exact_choice(self) -> ProductionSelectionV1:
        native = self.mechanism != "text"
        if (
            len(set(self.features)) != len(self.features)
            or not self.features
            or (self.kind == "inherited") != (self.parent_commitment_sha256 is not None)
            or (self.policy == "native" and not native)
            or (self.policy == "text" and native)
            or (native and self.requested_mechanism != self.mechanism)
            or (self.kind == "text" and native)
            or (self.kind in {"native", "qualification_probe"} and not native)
            or (
                native
                and self.native_support != "supported"
                and not (
                    self.native_support == "unknown"
                    and self.authority_kind == "qualification_probe"
                )
            )
            or (native and self.authority_kind == "unqualified")
            or (
                self.kind == "qualification_probe"
                and self.authority_kind != "qualification_probe"
            )
        ):
            raise ValueError("production selection conflicts")
        return self


def _support(
    context: ProductionContext,
    features: tuple[NativeFeature, ...],
    service: ServicePolicyV2 | None = None,
    generation: GenerationSpec | None = None,
) -> Literal["supported", "unsupported", "unknown"]:
    profile = _profile_for(context.scope)
    if profile is not None and not qualified_service(
        service, profile.service_scopes, features
    ):
        return "unknown"
    if profile is not None:
        if not qualified_reasoning(
            profile.reasoning_scopes, generation, service, features
        ):
            return "unknown"
        if set(features) & set(profile.unsupported_features):
            return "unsupported"
        if set(features) <= set(profile.supported_features):
            return "supported"
    return "unknown"


def _verify_selection(
    selection: ProductionSelectionV1,
    spec: RunSpecV3,
    contract: AssessmentOutputContract,
    context: ProductionContext,
    parent: TransportCommitmentV2 | None,
    service: ServicePolicyV2 | None = None,
) -> None:
    expected_route = (
        "openai.responses.text.format"
        if spec.endpoint == "responses"
        else "openai.chat.response_format"
    )
    qualifier = context.kind != "openai-production-v1"
    authority_kind = (
        "qualification_probe"
        if qualifier
        else "reviewed"
        if _profile_for(context.scope) is not None
        else "unqualified"
    )
    if (
        selection.policy != spec.prompt.structured_output
        or selection.requested_mechanism != spec.prompt.native_mechanism
        or selection.features != _features(contract)
        or selection.authority_sha256 != _authority_sha256(context)
        or selection.authority_kind != authority_kind
        or selection.mechanism not in {"text", expected_route}
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
    ):
        raise TransportIntegrityError()
    support = _support(context, selection.features, service, spec.generation)
    if selection.native_support != support:
        raise TransportIntegrityError()
    if selection.mechanism != "text":
        if support == "unsupported" or (support != "supported" and not qualifier):
            raise TransportIntegrityError()
        if selection.kind not in {
            "inherited",
            "qualification_probe" if qualifier else "native",
        }:
            raise TransportIntegrityError()
    elif (
        selection.native_support != support
        or (spec.prompt.structured_output == "auto" and support == "supported")
        or (
            spec.prompt.structured_output == "auto"
            and spec.strict_capabilities
            and support == "unknown"
        )
    ):
        raise TransportIntegrityError()


def _selection(
    spec: RunSpecV3,
    contract: AssessmentOutputContract,
    context: ProductionContext,
    parent: TransportCommitmentV2 | None,
    service: ServicePolicyV2 | None = None,
) -> ProductionSelectionV1:
    policy = spec.prompt.structured_output
    features = _features(contract)
    support = _support(context, features, service, spec.generation)
    qualifier = context.kind != "openai-production-v1"
    if parent is not None:
        mechanism = parent.mechanism
    elif policy == "text":
        mechanism = "text"
    elif support == "supported" or (qualifier and support != "unsupported"):
        mechanism = cast(OpenAIMechanism, spec.prompt.native_mechanism)
    elif policy == "native" or (spec.strict_capabilities and support == "unknown"):
        raise ValueError("Native output requires reviewed exact-scope qualification.")
    else:
        mechanism = "text"
    native = mechanism != "text"
    result = ProductionSelectionV1(
        kind="inherited"
        if parent is not None
        else "qualification_probe"
        if native and qualifier
        else "native"
        if native
        else "text",
        policy=policy,
        requested_mechanism=spec.prompt.native_mechanism,
        mechanism=mechanism,
        native_support=support,
        authority_kind="qualification_probe"
        if qualifier
        else "reviewed"
        if _profile_for(context.scope) is not None
        else "unqualified",
        features=features,
        authority_sha256=_authority_sha256(context),
        parent_commitment_sha256=None if parent is None else parent.commitment_sha256,
    )
    _verify_selection(result, spec, contract, context, parent, service)
    return result


class OutputCommitmentV4(_TransportModel):
    """Production output commitment; canonical contract and projection stay V2/V1."""

    commitment_version: Literal[4] = 4
    renderer_version: Literal[3] = 3
    reasoning_policy: ReasoningPolicyV1 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    service_policy: ServicePolicyV2 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    scope: NativeProjectionScopeV1 = Field(repr=False)
    contract: AssessmentOutputContract = Field(repr=False)
    selection: ProductionSelectionV1
    schema_text: str = Field(repr=False)
    schema_sha256: SHA256
    neutral_semantic_sha256: SHA256
    projection: NativeSchemaProjectionV1 | None = Field(default=None, repr=False)
    sdk_config_fragment_text: str | None = Field(default=None, repr=False)
    repetition: Literal["canonical_neutral", "omit"]
    context_sha256: SHA256
    local_validation_required: Literal[True] = True

    @model_validator(mode="after")
    def exact_representation(self) -> OutputCommitmentV4:
        verify_policy_scope(
            self.reasoning_policy,
            self.scope,
            self.service_policy,
            self.selection.requested_mechanism,
        )
        raw = serialize_output_schema(self.contract)
        if (
            self.service_policy is not None
            and self.service_policy.provider != self.scope.provider
        ):
            raise ValueError("service provider differs")
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
            self.projection != project_openai_schema(self.contract, scope=self.scope)
            or self.projection.mechanism != self.selection.mechanism
            or self.sdk_config_fragment_text != self.projection.format_fragment_text
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
        return semantic_digest("output-commitment-v4", self)


def _build_output_commitment_v4(
    spec: RunSpecV3,
    request: PlannedRequest,
    context: ProductionContext,
    *,
    inherited_commitment: TransportCommitmentV2 | None = None,
    frozen_selection: ProductionSelectionV1 | None = None,
    presentation_version: PresentationVersion = 2,
    service_policy_version: ServicePolicyVersion = 2,
) -> OutputCommitmentV4:
    """Select once, or verify an original frozen choice without resolving auto."""
    context = _checked_context(context)
    _validate_scope(spec, context)
    if inherited_commitment is not None:
        service_policy_version = 1 if inherited_commitment.service_policy is None else 2
    service = resolve_service_policy(spec, service_policy_version)
    profile = _profile_for(context.scope)
    validate_service_support(
        service,
        () if profile is None else profile.service_scopes,
        strict=spec.strict_capabilities,
        qualification="qualification" in context.kind
        or context.kind.endswith("-study-candidate-v1"),
    )
    contract = _contract(
        spec,
        request,
        presentation_version=inherited_commitment.presentation_version
        if inherited_commitment
        else presentation_version,
    )
    selection = (
        _selection(spec, contract, context, inherited_commitment, service)
        if frozen_selection is None
        else ProductionSelectionV1.model_validate(dict(frozen_selection))
    )
    _verify_selection(selection, spec, contract, context, inherited_commitment, service)
    projection = (
        None
        if selection.mechanism == "text"
        else project_openai_schema(contract, scope=context.scope)
    )
    raw = serialize_output_schema(contract)
    return OutputCommitmentV4(
        reasoning_policy=build_reasoning_policy(spec, context.scope),
        service_policy=service,
        scope=context.scope,
        contract=contract,
        selection=selection,
        schema_text=raw.decode("utf-8"),
        schema_sha256=bytes_digest(raw),
        neutral_semantic_sha256=semantic_digest(
            "neutral-schema-semantics-v1", generate_output_schema(contract)
        ),
        projection=projection,
        sdk_config_fragment_text=None
        if projection is None
        else projection.format_fragment_text,
        repetition="canonical_neutral" if projection is None else "omit",
        context_sha256=production_context_digest(context),
    )


def build_output_commitment_v4(
    spec: RunSpecV3,
    request: PlannedRequest,
    context: ProductionContext,
    *,
    inherited_commitment: TransportCommitmentV2 | None = None,
    frozen_selection: ProductionSelectionV1 | None = None,
    presentation_version: PresentationVersion = 2,
    service_policy_version: ServicePolicyVersion = 2,
) -> OutputCommitmentV4:
    """Sanitized boundary for new selection and pinned output reconstruction."""
    try:
        return _build_output_commitment_v4(
            spec,
            request,
            context,
            inherited_commitment=inherited_commitment,
            frozen_selection=frozen_selection,
            presentation_version=presentation_version,
            service_policy_version=service_policy_version,
        )
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise TransportIntegrityError() from None


def build_openai_request_body(
    *,
    endpoint: str,
    model: str,
    prompt: str,
    output: OutputCommitmentV4,
    generation: object = None,
) -> bytes:
    """Complete body in pinned SDK order; never accept caller provider JSON.

    Numeric representations and omitted controls are preserved. JudgmentLLM
    identities and authentication material are deliberately outside the body.
    """
    from judgmentllm.config import GenerationSpec

    if (
        endpoint not in {"responses", "chat.completions"}
        or (output.scope.provider, output.scope.endpoint, output.scope.model)
        != ("openai", endpoint, model)
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
    fragment = (
        {}
        if output.sdk_config_fragment_text is None
        else cast(
            dict[str, object], decode_assessment_output(output.sdk_config_fragment_text)
        )
    )
    values: dict[str, object] = {"model": model, "stream": False}
    if output.service_policy is not None:
        values["service_tier"] = output.service_policy.wire_tier
    if endpoint == "responses":
        values["input"] = [
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
        ]
    else:
        values["messages"] = [{"role": "user", "content": prompt}]
    if settings is not None and settings.max_output_tokens is not None:
        values[
            "max_output_tokens" if endpoint == "responses" else "max_completion_tokens"
        ] = settings.max_output_tokens
    if endpoint == "chat.completions":
        values["n"] = 1
        if fragment:
            values["response_format"] = fragment["response_format"]
    controls = wire_controls(settings, output.reasoning_policy)
    if controls:
        if endpoint != "responses":
            raise TransportIntegrityError()
        values.update(controls)
    values["store"] = False
    if settings is not None and settings.temperature is not None:
        values["temperature"] = settings.temperature
    if endpoint == "responses" and fragment:
        values["text"] = fragment["text"]
    if settings is not None and settings.top_p is not None:
        values["top_p"] = settings.top_p
    if endpoint == "responses":
        values["truncation"] = "disabled"
    return ordered_json_bytes(values)


class TransportCommitmentV2(_TransportModel):
    """Safe production receipt, never an authentication or billable-call grant."""

    transport_commitment_version: Literal[2] = 2
    transport_protocol: Literal["openai-production-v1"] = "openai-production-v1"
    api_scope: Literal["openai-v1"] = "openai-v1"
    renderer_version: Literal[3] = 3
    presentation_version: PresentationVersion = Field(
        default=1, exclude_if=lambda value: value == 1
    )
    context_kind: ProductionContextKind
    service_policy: ServicePolicyV2 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    scope: NativeProjectionScopeV1 = Field(repr=False)
    store: Literal[False] = False
    identity: PhysicalRequestIdentityV1
    configuration_sha256: SHA256
    context_sha256: SHA256
    dependency_sha256: SHA256
    membership_sha256: SHA256
    output_commitment_sha256: SHA256
    plan_sha256: SHA256
    mechanism: OpenAIMechanism | Literal["text"]
    requested_mechanism: NativeTransportMechanism | None
    selection: ProductionSelectionV1
    parent_commitment_sha256: SHA256 | None = None
    projection_version: Literal[1] | None = None
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

    @field_validator("store", mode="before")
    @classmethod
    def exact_store(cls, value: object) -> object:
        if value is not False:
            raise ValueError("production storage policy differs")
        return value

    @model_validator(mode="after")
    def exact_commitment(self) -> TransportCommitmentV2:
        native = self.mechanism != "text"
        if (
            self.scope.provider != "openai"
            or self.scope.endpoint not in {"responses", "chat.completions"}
            or self.scope.model_scope != "exact"
            or self.scope.sdk_package != "openai"
            or self.scope.sdk_version != OPENAI_PRODUCTION_SDK_VERSION
            or self.scope.api_version is not None
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
                "transport-commitment-v2",
                self.model_dump(mode="json", exclude={"commitment_sha256"}),
            )
        ):
            raise ValueError("production receipt identity differs")
        return self


def _receipt(
    plan: TransportPlanV1,
    output: OutputCommitmentV4,
    prompt: bytes,
    payload: bytes,
    context_kind: ProductionContextKind,
) -> TransportCommitmentV2:
    projection = output.projection
    native = (
        None if projection is None else projection.native_schema_text.encode("utf-8")
    )
    fragment = (
        None if projection is None else projection.format_fragment_text.encode("utf-8")
    )
    values = plan.model_dump(mode="json", exclude={"plan_version"})
    values.update(
        schema_version=1,
        transport_commitment_version=2,
        transport_protocol="openai-production-v1",
        api_scope=OPENAI_API_SCOPE,
        renderer_version=3,
        presentation_version=output.contract.presentation_version,
        context_kind=context_kind,
        scope=output.scope.model_dump(mode="json"),
        store=False,
        plan_sha256=plan.plan_sha256,
        mechanism=output.selection.mechanism,
        requested_mechanism=output.selection.requested_mechanism,
        selection=output.selection.model_dump(mode="json"),
        projection_version=None if projection is None else 1,
        projection_sha256=None if projection is None else projection.projection_sha256,
        neutral_schema_sha256=output.schema_sha256,
        neutral_semantic_sha256=output.neutral_semantic_sha256,
        native_schema_sha256=None if native is None else bytes_digest(native),
        native_schema_bytes=None if native is None else len(native),
        sdk_config_sha256=None if fragment is None else bytes_digest(fragment),
        sdk_config_bytes=None if fragment is None else len(fragment),
        format_sha256=None if fragment is None else bytes_digest(fragment),
        format_bytes=None if fragment is None else len(fragment),
        prompt_sha256=bytes_digest(prompt),
        prompt_bytes=len(prompt),
        payload_sha256=bytes_digest(payload),
        payload_bytes=len(payload),
        local_validation_required=True,
    )
    if output.service_policy is not None:
        values["service_policy"] = output.service_policy.model_dump(mode="json")
    if output.contract.presentation_version == 1:
        values.pop("presentation_version")
    return TransportCommitmentV2.model_validate_json(
        canonical_bytes(
            {
                **values,
                "commitment_sha256": semantic_digest("transport-commitment-v2", values),
            }
        )
    )


class PreparedRequestV4(PreparedRequest, _TransportModel):
    """Exact API body with independently retained production commitments."""

    preparation_version: Literal[4] = 4
    payload: bytes = Field(repr=False)
    logical_request: PlannedRequest = Field(repr=False)
    output_commitment: OutputCommitmentV4 = Field(repr=False)
    transport_plan: TransportPlanV1
    transport_commitment: TransportCommitmentV2
    rendered_text: str = Field(repr=False)

    @model_validator(mode="after")
    def exact_production_body(self) -> PreparedRequestV4:
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
            != build_openai_request_body(
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


def _validate_scope(spec: RunSpecV3, context: ProductionContext) -> None:
    checked = validate_run_spec(spec)
    if not isinstance(checked, RunSpecV3) or (
        checked.provider,
        checked.endpoint,
        checked.model,
    ) != (context.scope.provider, context.scope.endpoint, context.scope.model):
        raise TransportIntegrityError()
    from judgmentllm.study.admission import study_generation

    report = validate_generation(
        checked,
        capabilities=study_generation(checked, context.scope)
        if context.kind == "openai-study-candidate-v1"
        else generation_capabilities(
            checked, context.scope, production_generation_capabilities(context.scope)
        ),
    )
    if not report.valid:
        raise ValueError("Generation settings fail exact-scope capability validation.")


def prepare_production_request(
    spec: RunSpecV3,
    dependency: RequestDependency,
    identity: PhysicalRequestIdentityV1,
    context: ProductionContext,
    *,
    template: str | None = None,
    lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask]
    | None = None,
    frozen: TransportCommitmentV2 | None = None,
    parent: TransportCommitmentV2 | None = None,
    committed_output: OutputCommitmentV4 | None = None,
    presentation_version: PresentationVersion = 2,
    service_policy_version: ServicePolicyVersion = 2,
) -> PreparedRequestV4:
    """Prepare before sealing, or verify immutable reconstruction without fallback."""
    from judgmentllm.production_prompts import render_production_request

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
            parent = TransportCommitmentV2.model_validate(dict(parent))
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
            frozen = TransportCommitmentV2.model_validate(dict(frozen))
            if frozen.identity != identity or frozen.context_sha256 != context_digest:
                raise TransportIntegrityError()
        output = build_output_commitment_v4(
            spec,
            dependency.request,
            context,
            inherited_commitment=parent,
            service_policy_version=(1 if frozen.service_policy is None else 2)
            if frozen is not None
            else service_policy_version,
            frozen_selection=None if frozen is None else frozen.selection,
            presentation_version=frozen.presentation_version
            if frozen
            else parent.presentation_version
            if parent
            else presentation_version,
        )
        if (
            committed_output is not None
            and OutputCommitmentV4.model_validate(dict(committed_output)) != output
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
        rendered = render_production_request(
            spec, template, dependency.request, lookup, output
        )
        prompt = rendered.text.encode("utf-8")
        payload = build_openai_request_body(
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
        prepared = PreparedRequestV4(
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
build_production_output_commitment = build_output_commitment_v4
