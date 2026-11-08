"""Additive, offline-only durable native transport contracts.

Content-bearing projections never belong in minimal-capture metadata. Selection
happens once; replay regenerates only the pinned representation and compares it.
No SDK, authentication discovery, or provider client is imported here.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, is_dataclass, replace
from datetime import date
from types import FunctionType
from typing import Literal, Self, cast

from pydantic import (
    BaseModel,
    Field,
    ModelWrapValidatorHandler,
    field_validator,
    model_validator,
)

from judgmentllm._verification import ExactToken, exact_token
from judgmentllm.config import NativeTransportMechanism, RunSpecV3, validate_run_spec
from judgmentllm.evidence.scope import ScopedProviderEvidence
from judgmentllm.models import ID, PairTask, ResponseItem
from judgmentllm.native_schema import (
    NativeModel,
    NativeProjectionScopeV1,
    NativeSchemaProjectionV1,
    snapshot_source,
)
from judgmentllm.native_schema_v2 import (
    NativeMechanismV2,
    NativeSchemaProjectionV2,
    project_native_schema_v2,
)
from judgmentllm.native_selection import (
    _DEFAULT_CATALOG,
    TransportDecisionV1,
    _Catalog,
    _features,
    _resolve_native_transport,
)
from judgmentllm.native_selection_v2 import (
    _DEFAULT_CATALOG_V2,
    TransportDecisionV2,
    _CatalogV2,
    _resolve_native_transport_v2,
    _verify_native_transport_decision_v2,
)
from judgmentllm.output_commitments import (
    MemberPropertyOrder,
    _property_orders,
    ordered_json_bytes,
)
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    PairwiseOutputContract,
    RubricOutputContract,
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
from judgmentllm.planning import PlannedRequest, stable_id
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.providers.openai_schema import project_openai_schema
from judgmentllm.providers.protocol import PreparedRequest

# Private catalog APIs deliberately remain private: the default catalogs contain
# no positive model authority, and persistence cannot create that authority.
# pyright: reportPrivateUsage=false


class TransportIntegrityError(ValueError):
    """Controlled compatibility failure; never includes content or provider data."""

    def __init__(self) -> None:
        super().__init__("Native transport commitment is incompatible or inconsistent.")


@dataclass(frozen=True, repr=False)
class TransportContext:
    """Explicit runtime authority, supplied again when reopening a fixture run."""

    scope: NativeProjectionScopeV1
    evidence: ScopedProviderEvidence | None = None
    openai_catalog: _Catalog | None = None
    provider_catalog: _CatalogV2 | None = None
    service_identity: str | None = None
    fixture_sha256: str | None = None

    @property
    def trusted_synthetic(self) -> bool:
        if self.scope.model_scope != "exact":
            return False
        catalogs = (
            self.openai_catalog or _DEFAULT_CATALOG,
            self.provider_catalog or _DEFAULT_CATALOG_V2,
        )
        return any(
            profile.scope == self.scope
            for catalog in catalogs
            for profile in catalog.profiles
        ) and self.scope.model in {
            "judgmentllm-synthetic-native-v1",
            "judgmentllm-synthetic-native-v2",
        }

    def __repr__(self) -> str:
        return "TransportContext(<private runtime authority>)"


def transport_context_digest(context: TransportContext) -> str:
    """Bounded digest of authority/scope, never retained private schema content."""
    try:
        scope = NativeProjectionScopeV1.model_validate(dict(context.scope))
        evidence = (
            ScopedProviderEvidence.model_validate(dict(context.evidence))
            if context.evidence is not None
            else None
        )
        first = context.openai_catalog or _DEFAULT_CATALOG
        second = context.provider_catalog or _DEFAULT_CATALOG_V2
        return semantic_digest(
            "native-runtime-context-v1",
            {
                "scope": scope,
                "evidence": evidence,
                "openai_implementations": [
                    [
                        i.endpoint,
                        i.mechanism,
                        list(i.features),
                        i.translate.__module__,
                        getattr(
                            i.translate, "__qualname__", type(i.translate).__name__
                        ),
                    ]
                    for i in first.implementations
                ],
                "openai_profiles": list(first.profiles),
                "provider_implementations": [
                    [
                        i.provider,
                        i.endpoint,
                        i.mechanism,
                        i.schema_dialect,
                        i.sdk_package,
                        i.sdk_version,
                        i.api_version,
                        i.implementation_version,
                    ]
                    for i in second.implementations
                ],
                "provider_profiles": list(second.profiles),
                "service_identity": context.service_identity,
                "fixture_sha256": context.fixture_sha256,
            },
        )
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise TransportIntegrityError() from None


def _effective_context(context: TransportContext) -> TransportContext:
    """Own every catalog whose metadata enters the context commitment."""
    if type(context) is not TransportContext or set(vars(context)) != set(
        TransportContext.__dataclass_fields__
    ):
        raise TransportIntegrityError()
    return replace(
        context,
        openai_catalog=context.openai_catalog or _DEFAULT_CATALOG,
        provider_catalog=context.provider_catalog or _DEFAULT_CATALOG_V2,
    )


def _preparation_token(
    value: object, active: set[int] | None = None
) -> ExactToken | None:
    """Exact transient guard, including dates in native provenance.

    This token lives for one physical preparation call. It is never retained in
    a prepared request or session, and unsupported values keep the full path.
    """
    if type(value) is date:
        return (date, value.isoformat())
    kind = type(value)
    if kind in (type(None), bool, int, float, str, bytes):
        # Most leaves are primitives. Preserve the exact shared token semantics
        # without repeating model/dataclass discovery for every scalar leaf.
        return exact_token(value)
    if kind is FunctionType:
        function = cast(FunctionType, value)
        if (
            type(function.__module__) is not str
            or type(function.__qualname__) is not str
            or function.__closure__ is not None
        ):
            return None
        metadata = exact_token(
            (function.__defaults__, function.__kwdefaults__, function.__dict__)
        )
        if metadata is None:
            return None
        return (
            kind,
            function,
            function.__module__,
            function.__qualname__,
            id(function.__code__),
            function.__code__,
            metadata,
        )
    model = (
        isinstance(value, BaseModel)
        and kind.__module__.startswith("judgmentllm.")
        and getattr(sys.modules.get(kind.__module__), kind.__name__, None) is kind
    )
    record = (
        is_dataclass(value)
        and kind.__module__.startswith("judgmentllm.")
        and getattr(sys.modules.get(kind.__module__), kind.__name__, None) is kind
    )
    if not model and not record and kind not in (dict, list, tuple):
        return exact_token(value)
    active = set() if active is None else active
    identity = id(value)
    if identity in active:
        raise ValueError("cyclic current preparation cannot be verified")
    active.add(identity)
    try:
        if model:
            entries = tuple(cast(BaseModel, value))
        elif record:
            entries = tuple(vars(value).items())
        elif kind is dict:
            entries = tuple(cast(dict[object, object], value).items())
        else:
            children = tuple(
                _preparation_token(item, active)
                for item in cast(list[object] | tuple[object, ...], value)
            )
            return None if any(item is None for item in children) else (kind, children)
        pairs = tuple(
            (_preparation_token(key, active), _preparation_token(item, active))
            for key, item in entries
        )
        if any(key is None or item is None for key, item in pairs):
            return None
        return (kind, pairs)
    finally:
        active.remove(identity)


def _translator_snapshot(context: TransportContext) -> tuple[tuple[object, ...], ...]:
    translators = (
        project_openai_schema,
        project_native_schema_v2,
        *(
            i.translate
            for i in (context.openai_catalog or _DEFAULT_CATALOG).implementations
        ),
    )
    return tuple(
        (
            translator,
            getattr(translator, "__module__", None),
            getattr(translator, "__qualname__", None),
            getattr(translator, "__code__", None),
        )
        for translator in translators
    )


def _same_translators(
    before: tuple[tuple[object, ...], ...], after: tuple[tuple[object, ...], ...]
) -> bool:
    return len(before) == len(after) and all(
        first[0] is second[0] and first[1:3] == second[1:3] and first[3] is second[3]
        for first, second in zip(before, after, strict=True)
    )


class _TransportModel(NativeModel):
    @model_validator(mode="wrap")
    @classmethod
    def strict_versions(
        cls, value: object, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        if isinstance(value, dict) and any(
            key.endswith("_version") and item is not None and type(item) is not int
            for key, item in cast(dict[str, object], value).items()
        ):
            raise ValueError("transport version tokens must be integers")
        return handler(value)


class PhysicalRequestIdentityV1(_TransportModel):
    identity_version: Literal[1] = 1
    run_id: ID
    request_uid: ID
    attempt_id: ID
    submission_id: ID
    correlation_id: ID

    @model_validator(mode="after")
    def exact_identity(self) -> PhysicalRequestIdentityV1:
        submission = stable_id("submission", self.run_id, self.attempt_id)
        if self.submission_id != submission or self.correlation_id != stable_id(
            "correlation", self.run_id, submission
        ):
            raise ValueError("physical identity differs")
        return self


def physical_identity(
    run_id: str, request_uid: str, attempt_id: str
) -> PhysicalRequestIdentityV1:
    submission = stable_id("submission", run_id, attempt_id)
    return PhysicalRequestIdentityV1(
        run_id=run_id,
        request_uid=request_uid,
        attempt_id=attempt_id,
        submission_id=submission,
        correlation_id=stable_id("correlation", run_id, submission),
    )


class FrozenTransportSelectionV1(_TransportModel):
    """Explicit dispatch around unchanged standalone V1 and V2 decisions."""

    selection_version: Literal[1] = 1
    kind: Literal["text-v1", "openai-v1", "provider-v2", "inherited-v1"]
    policy: Literal["auto", "native", "text"]
    requested_mechanism: NativeTransportMechanism | None
    mechanism: NativeTransportMechanism | Literal["text"]
    openai_decision: TransportDecisionV1 | None = Field(default=None, repr=False)
    provider_decision: TransportDecisionV2 | None = Field(default=None, repr=False)
    parent_commitment_sha256: SHA256 | None = None

    @model_validator(mode="after")
    def exact_dispatch(self) -> FrozenTransportSelectionV1:
        decision = self.openai_decision or self.provider_decision
        if self.kind == "inherited-v1":
            valid = self.parent_commitment_sha256 is not None and decision is None
        elif self.kind == "text-v1":
            valid = (
                self.policy == "text"
                and self.mechanism == "text"
                and decision is None
                and self.parent_commitment_sha256 is None
            )
        else:
            valid = (
                self.parent_commitment_sha256 is None
                and (
                    (
                        self.kind == "openai-v1"
                        and self.openai_decision is not None
                        and self.provider_decision is None
                    )
                    or (
                        self.kind == "provider-v2"
                        and self.provider_decision is not None
                        and self.openai_decision is None
                    )
                )
                and decision is not None
                and decision.policy == self.policy
                and decision.mechanism == self.mechanism
            )
        if (
            not valid
            or (self.policy == "native" and self.mechanism == "text")
            or (self.policy == "text" and self.mechanism != "text")
        ):
            raise ValueError("selection dispatch differs")
        if self.mechanism != "text" and self.requested_mechanism != self.mechanism:
            raise ValueError("selected route differs")
        return self


Projection = NativeSchemaProjectionV1 | NativeSchemaProjectionV2


class OutputCommitmentV3(_TransportModel):
    """Content-bearing output representation, independent of physical attempt."""

    commitment_version: Literal[3] = 3
    renderer_version: Literal[3] = 3
    scope: NativeProjectionScopeV1 = Field(repr=False)
    contract: AssessmentOutputContract = Field(repr=False)
    selection: FrozenTransportSelectionV1
    schema_text: str = Field(repr=False)
    schema_sha256: SHA256
    neutral_semantic_sha256: SHA256
    projection: Projection | None = Field(default=None, repr=False)
    sdk_config_fragment_text: str | None = Field(default=None, repr=False)
    repetition: Literal["canonical_neutral", "projected_native", "omit"]
    context_sha256: SHA256
    local_validation_required: Literal[True] = True

    @model_validator(mode="after")
    def exact_representation(self) -> OutputCommitmentV3:
        raw = serialize_output_schema(self.contract)
        if (
            self.schema_text.encode("utf-8") != raw
            or self.schema_sha256 != bytes_digest(raw)
            or self.neutral_semantic_sha256
            != semantic_digest(
                "neutral-schema-semantics-v1", generate_output_schema(self.contract)
            )
        ):
            raise ValueError("neutral representation differs")
        decision = self.selection.openai_decision or self.selection.provider_decision
        if decision is not None and (
            decision.scope != self.scope
            or decision.source_schema_sha256 != self.schema_sha256
            or decision.source_sha256
            != bytes_digest(canonical_bytes(snapshot_source(self.contract)))
            or decision.features != _features(self.contract)
        ):
            raise ValueError("selection source identity differs")
        native = self.selection.mechanism != "text"
        if native != (self.projection is not None):
            raise ValueError("projection presence differs")
        if self.projection is None:
            if (
                self.sdk_config_fragment_text is not None
                or self.repetition != "canonical_neutral"
            ):
                raise ValueError("text representation differs")
        else:
            projection = type(self.projection).model_validate(dict(self.projection))
            if (
                projection.scope != self.scope
                or projection.source != snapshot_source(self.contract)
                or projection.neutral_schema_text != self.schema_text
                or projection.mechanism != self.selection.mechanism
                or self.sdk_config_fragment_text
                != (
                    projection.format_fragment_text
                    if isinstance(projection, NativeSchemaProjectionV1)
                    else projection.sdk_config_fragment_text
                )
            ):
                raise ValueError("native source or fragments differ")
            expected = (
                "projected_native"
                if projection.scope.provider in {"together", "ollama"}
                else "omit"
            )
            if self.repetition != expected:
                raise ValueError("prompt repetition differs")
            decision = (
                self.selection.openai_decision or self.selection.provider_decision
            )
            if decision is not None and (
                decision.projection_sha256 != projection.projection_sha256
                or decision.scope != self.scope
            ):
                raise ValueError("selected projection differs")
        return self

    @property
    def root_properties(self) -> tuple[str, ...]:
        return _property_orders(self.contract)[0]

    @property
    def member_properties(self) -> tuple[MemberPropertyOrder, ...]:
        return _property_orders(self.contract)[1]

    @property
    def commitment_sha256(self) -> str:
        return semantic_digest("output-commitment-v3", self)


def _contract(spec: RunSpecV3, request: PlannedRequest) -> AssessmentOutputContract:
    from judgmentllm.execution.processing import _request_contract

    if request.pair is not None:
        contract: AssessmentOutputContract = PairwiseOutputContract(
            pair=request.pair,
            explanations=spec.execution.explanations,
            allow_ties=spec.allow_ties,
            output_order=spec.prompt.output_order,
        )
    else:
        if spec.rubric is None:
            raise TransportIntegrityError()
        traits = {trait.trait_id: trait for trait in spec.rubric.traits}
        if tuple(task.trait_id for task in request.tasks) != request.trait_ids:
            raise TransportIntegrityError()
        try:
            contract = RubricOutputContract(
                traits=tuple(traits[i] for i in request.trait_ids),
                explanations=spec.execution.explanations,
                output_order=spec.prompt.output_order,
            )
        except KeyError:
            raise TransportIntegrityError() from None
    return _request_contract(
        contract, spec, RequestDependency(request=request, dependency_sha256="0" * 64)
    )


def _project(
    contract: AssessmentOutputContract,
    context: TransportContext,
    mechanism: NativeTransportMechanism,
) -> Projection:
    if mechanism.startswith("openai."):
        projection = project_openai_schema(contract, scope=context.scope)
        if projection.mechanism != mechanism:
            raise TransportIntegrityError()
        return projection
    return project_native_schema_v2(
        contract, scope=context.scope, mechanism=cast(NativeMechanismV2, mechanism)
    )


def _verify_openai(
    decision: TransportDecisionV1,
    contract: AssessmentOutputContract,
    context: TransportContext,
) -> None:
    """Verify frozen V1 facts without the historical auto resolver call."""
    checked = TransportDecisionV1.model_validate(dict(decision))
    catalog = context.openai_catalog or _DEFAULT_CATALOG
    implementation = next(
        (
            i
            for i in catalog.implementations
            if context.scope.provider == "openai"
            and i.endpoint == context.scope.endpoint
        ),
        None,
    )
    if (
        checked.scope != context.scope
        or checked.features != _features(contract)
        or checked.source_sha256
        != bytes_digest(canonical_bytes(snapshot_source(contract)))
        or checked.source_schema_sha256
        != bytes_digest(serialize_output_schema(contract))
        or checked.implemented != (implementation is not None)
        or checked.implementation_version != (1 if implementation is not None else None)
    ):
        raise TransportIntegrityError()
    profile = next(
        (
            p
            for p in catalog.profiles
            if p.scope == context.scope
            and implementation is not None
            and p.mechanism == implementation.mechanism
            and p.schema_dialect == checked.schema_dialect
            and set(checked.features) <= set(p.features)
        ),
        None,
    )
    evidence = context.evidence
    model_support = (
        "unknown"
        if evidence is None or evidence.supported is None
        else "supported"
        if evidence.supported
        else "unsupported"
    )
    provenance = bool(
        profile is not None
        and evidence is not None
        and {
            ("documentation", profile.documentation_sha256),
            ("offline_fixture", profile.fixture_sha256),
        }
        <= {(p.kind, p.source_sha256) for p in evidence.provenance}
    )
    tested = bool(
        provenance
        and evidence is not None
        and evidence.implemented is True
        and evidence.fixture_tested is True
        and evidence.live_qualified is not True
    )
    incompatible = checked.diagnostic.reason in {
        "translator_unavailable",
        "translator_scope_incompatible",
        "projection_features_incompatible",
        "projection_incompatible",
    }
    if incompatible:
        tested = False
    if (
        checked.model_support != model_support
        or checked.fixture_tested != tested
        or checked.documentation_sha256
        != (profile.documentation_sha256 if tested and profile else None)
        or checked.fixture_sha256
        != (profile.fixture_sha256 if tested and profile else None)
    ):
        raise TransportIntegrityError()
    support = "unknown"
    diagnostic = "native_support_unknown"
    if implementation is None:
        support, diagnostic = "unsupported", "translator_unavailable"
    elif (
        context.scope.sdk_package != "openai"
        or context.scope.sdk_version != "3.42.1"
        or context.scope.api_version is not None
        or checked.schema_dialect != "openai.strict-json-schema.v1"
    ):
        support, diagnostic = "unsupported", "translator_scope_incompatible"
    elif not set(checked.features) <= set(implementation.features):
        support, diagnostic = "unsupported", "projection_features_incompatible"
    elif checked.diagnostic.reason == "projection_incompatible":
        if checked.policy == "text":
            raise TransportIntegrityError()
        try:
            project_openai_schema(contract, scope=context.scope)
        except ValueError:
            pass
        else:
            raise TransportIntegrityError()
        support, diagnostic = "unsupported", "projection_incompatible"
    elif evidence is not None and evidence.supported is False:
        support, diagnostic = "unsupported", "native_known_unsupported"
    elif checked.policy == "text" or evidence is None or evidence.supported is None:
        pass
    elif profile is None:
        diagnostic = "trusted_fixture_missing"
    elif not provenance:
        diagnostic = "trusted_provenance_missing"
    elif not tested:
        diagnostic = "fixture_support_unestablished"
    else:
        support, diagnostic = "supported", "compatible_native"
    if checked.native_support != support or checked.diagnostic.reason != diagnostic:
        raise TransportIntegrityError()
    if checked.mechanism != "text":
        projection = _project(contract, context, checked.mechanism)
        if projection.projection_sha256 != checked.projection_sha256:
            raise TransportIntegrityError()


def _validate_context(spec: RunSpecV3, context: TransportContext) -> None:
    validate_run_spec(spec)
    if (context.scope.provider, context.scope.endpoint, context.scope.model) != (
        spec.provider,
        spec.endpoint,
        spec.model,
    ):
        raise TransportIntegrityError()
    if context.evidence is not None and any(
        getattr(context.evidence, name) != getattr(context.scope, name)
        for name in (
            "provider",
            "endpoint",
            "model",
            "model_scope",
            "sdk_package",
            "sdk_version",
            "api_version",
        )
    ):
        raise TransportIntegrityError()


def build_output_commitment_v3(
    spec: RunSpecV3,
    request: PlannedRequest,
    context: TransportContext,
    *,
    inherited_commitment: TransportCommitmentV1 | None = None,
) -> OutputCommitmentV3:
    """Select a fresh request, or inherit the parent's exact family and route."""
    return _build_output_commitment_v3(
        spec,
        request,
        context,
        transport_context_digest(context),
        inherited_commitment=inherited_commitment,
    )


def _build_output_commitment_v3(
    spec: RunSpecV3,
    request: PlannedRequest,
    context: TransportContext,
    context_sha256: str,
    *,
    inherited_commitment: TransportCommitmentV1 | None = None,
) -> OutputCommitmentV3:
    try:
        _validate_context(spec, context)
        contract = _contract(spec, request)
        mechanism = spec.prompt.native_mechanism
        projection: Projection | None = None
        if inherited_commitment is not None:
            parent = TransportCommitmentV1.model_validate(dict(inherited_commitment))
            if (
                parent.selection.policy != spec.prompt.structured_output
                or parent.configuration_sha256
                != semantic_digest("run-configuration", spec)
                or parent.context_sha256 != context_sha256
                or parent.requested_mechanism != mechanism
            ):
                raise TransportIntegrityError()
            selection = FrozenTransportSelectionV1(
                kind="inherited-v1",
                policy=spec.prompt.structured_output,
                requested_mechanism=mechanism,
                mechanism=parent.mechanism,
                parent_commitment_sha256=parent.commitment_sha256,
            )
            if parent.mechanism != "text":
                projection = _project(contract, context, parent.mechanism)
        elif spec.prompt.structured_output == "text":
            selection = FrozenTransportSelectionV1(
                kind="text-v1",
                policy="text",
                requested_mechanism=mechanism,
                mechanism="text",
            )
        elif mechanism is not None and mechanism.startswith("openai."):
            decision = _resolve_native_transport(
                contract,
                scope=context.scope,
                policy=spec.prompt.structured_output,
                strict_capabilities=spec.strict_capabilities,
                evidence=context.evidence,
                catalog=context.openai_catalog or _DEFAULT_CATALOG,
            )
            selection = FrozenTransportSelectionV1(
                kind="openai-v1",
                policy=decision.policy,
                requested_mechanism=mechanism,
                mechanism=decision.mechanism,
                openai_decision=decision,
            )
            if decision.mechanism != "text":
                projection = _project(contract, context, mechanism)
        elif mechanism is not None:
            decision_v2 = _resolve_native_transport_v2(
                contract,
                scope=context.scope,
                mechanism=cast(NativeMechanismV2, mechanism),
                policy=spec.prompt.structured_output,
                strict_capabilities=spec.strict_capabilities,
                evidence=context.evidence,
                catalog=context.provider_catalog or _DEFAULT_CATALOG_V2,
            )
            selection = FrozenTransportSelectionV1(
                kind="provider-v2",
                policy=decision_v2.policy,
                requested_mechanism=mechanism,
                mechanism=decision_v2.mechanism,
                provider_decision=decision_v2,
            )
            if decision_v2.mechanism != "text":
                projection = _project(contract, context, mechanism)
        else:
            raise TransportIntegrityError()
        raw = serialize_output_schema(contract)
        return OutputCommitmentV3(
            scope=context.scope,
            contract=contract,
            selection=selection,
            schema_text=raw.decode("utf-8"),
            schema_sha256=bytes_digest(raw),
            neutral_semantic_sha256=semantic_digest(
                "neutral-schema-semantics-v1", generate_output_schema(contract)
            ),
            projection=projection,
            sdk_config_fragment_text=(
                None
                if projection is None
                else projection.format_fragment_text
                if isinstance(projection, NativeSchemaProjectionV1)
                else projection.sdk_config_fragment_text
            ),
            repetition="canonical_neutral"
            if projection is None
            else "projected_native"
            if context.scope.provider in {"together", "ollama"}
            else "omit",
            context_sha256=context_sha256,
        )
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise TransportIntegrityError() from None


class TransportPlanV1(_TransportModel):
    """Acyclic payload-embedded commitment, before final payload hashing."""

    plan_version: Literal[1] = 1
    identity: PhysicalRequestIdentityV1
    configuration_sha256: SHA256
    dependency_sha256: SHA256
    membership_sha256: SHA256
    output_commitment_sha256: SHA256
    prompt_sha256: SHA256
    context_sha256: SHA256
    parent_commitment_sha256: SHA256 | None = None

    @property
    def plan_sha256(self) -> str:
        return semantic_digest("transport-plan-v1", self)


class TransportCommitmentV1(_TransportModel):
    """Safe receipt: hashes, lengths, controlled tokens and original identities."""

    transport_commitment_version: Literal[1] = 1
    renderer_version: Literal[3] = 3
    identity: PhysicalRequestIdentityV1
    configuration_sha256: SHA256
    context_sha256: SHA256
    dependency_sha256: SHA256
    membership_sha256: SHA256
    output_commitment_sha256: SHA256
    plan_sha256: SHA256
    mechanism: NativeTransportMechanism | Literal["text"]
    requested_mechanism: NativeTransportMechanism | None
    selection: FrozenTransportSelectionV1
    parent_commitment_sha256: SHA256 | None = None
    projection_version: Literal[1, 2] | None = None
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
    def exact_commitment(self) -> TransportCommitmentV1:
        native = self.mechanism != "text"
        if (
            self.selection.mechanism != self.mechanism
            or self.selection.requested_mechanism != self.requested_mechanism
            or self.selection.parent_commitment_sha256 != self.parent_commitment_sha256
        ):
            raise ValueError("receipt selection differs")
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
                raise ValueError("native artifact presence differs")
        if native and self.requested_mechanism != self.mechanism:
            raise ValueError("committed route differs")
        if native and self.projection_version != (
            1 if self.mechanism.startswith("openai.") else 2
        ):
            raise ValueError("projection family differs")
        decision = self.selection.openai_decision or self.selection.provider_decision
        if decision is not None and (
            decision.projection_sha256 != self.projection_sha256
            or decision.source_schema_sha256 != self.neutral_schema_sha256
        ):
            raise ValueError("decision receipt identities differ")
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
        if self.plan_sha256 != plan.plan_sha256:
            raise ValueError("receipt plan identity differs")
        if self.commitment_sha256 != semantic_digest(
            "transport-commitment-v1",
            self.model_dump(mode="json", exclude={"commitment_sha256"}),
        ):
            raise ValueError("transport receipt identity differs")
        return self


class PreparedRequestV3(PreparedRequest, _TransportModel):
    """Exact private synthetic payload; persist content only with full capture."""

    preparation_version: Literal[3] = 3
    payload: bytes = Field(repr=False)
    logical_request: PlannedRequest = Field(repr=False)
    output_commitment: OutputCommitmentV3 = Field(repr=False)
    transport_plan: TransportPlanV1
    transport_commitment: TransportCommitmentV1
    rendered_text: str = Field(repr=False)

    @field_validator("preparation_version", mode="before")
    @classmethod
    def strict_preparation_version(cls, value: object) -> object:
        if type(value) is not int or value != 3:
            raise ValueError("preparation version must be integer 3")
        return value

    @model_validator(mode="after")
    def committed_bytes(self) -> PreparedRequestV3:
        receipt = self.transport_commitment
        plan = self.transport_plan
        output = self.output_commitment
        payload = cast(dict[str, object], decode_assessment_output(self.payload))
        expected_payload = {
            "protocol": "judgmentllm-native-offline-v1",
            "service_identity": payload.get("service_identity"),
            "fixture_sha256": payload.get("fixture_sha256"),
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model": self.model,
            "request_uid": self.request_uid,
            "tasks": self.logical_request.tasks,
            "prompt": self.rendered_text,
            "output_commitment": output,
            "transport_plan": plan,
            "format_fragment": None
            if output.projection is None
            else decode_assessment_output(output.projection.format_fragment_text),
            "format_fragment_text": None
            if output.projection is None
            else output.projection.format_fragment_text,
        }
        if self.generation is not None:
            expected_payload["generation"] = self.generation
        tasks = payload.get("tasks")
        if not isinstance(tasks, list):
            raise ValueError("prepared membership missing")
        task_data = cast(list[object], tasks)
        if any(not isinstance(task, dict) for task in task_data):
            raise ValueError("prepared membership malformed")
        payload_ids = tuple(
            cast(dict[str, object], task).get("task_id") for task in task_data
        )
        contract = output.contract
        original = self.logical_request
        exact_membership = (
            original.pair == contract.pair
            and original.trait_ids == (contract.pair.trait_id,)
            if isinstance(contract, PairwiseOutputContract)
            else original.pair is None
            and original.trait_ids == tuple(trait.trait_id for trait in contract.traits)
        )
        if (
            (self.provider, self.endpoint, self.model)
            != (output.scope.provider, output.scope.endpoint, output.scope.model)
            or not exact_membership
            or original.request_uid != self.request_uid
            or tuple(task.task_id for task in original.tasks) != self.task_ids
            or tuple(task.trait_id for task in original.tasks) != original.trait_ids
            or self.payload != ordered_json_bytes(_json_object(expected_payload))
            or payload_ids != self.task_ids
            or receipt
            != _receipt(plan, output, self.rendered_text.encode("utf-8"), self.payload)
            or receipt.prompt_sha256 != self.prompt_sha256
            or plan.prompt_sha256 != self.prompt_sha256
            or plan.identity.request_uid != self.request_uid
            or plan.context_sha256 != output.context_sha256
            or plan.membership_sha256
            != semantic_digest("attempt-membership", list(self.task_ids))
            # The output field has independently validated its complete current
            # representation before this after-validator. Hash its detached
            # primitive snapshot rather than reconstructing it a second time
            # through the public export boundary. No snapshot escapes this call.
            or plan.output_commitment_sha256
            != semantic_digest("output-commitment-v3", output.model_dump(mode="json"))
            or plan.parent_commitment_sha256
            != output.selection.parent_commitment_sha256
            or plan.dependency_sha256 != self.dependency_sha256
        ):
            raise ValueError("prepared transport binding differs")
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
                {
                    "native_schema": output.projection.native_schema_text.encode(
                        "utf-8"
                    ),
                    "sdk_configuration": cast(
                        str, output.sdk_config_fragment_text
                    ).encode("utf-8"),
                    "format_fragment": output.projection.format_fragment_text.encode(
                        "utf-8"
                    ),
                }
            )
        return artifacts


def _receipt(
    plan: TransportPlanV1, output: OutputCommitmentV3, prompt: bytes, payload: bytes
) -> TransportCommitmentV1:
    projection = output.projection
    native = (
        None if projection is None else projection.native_schema_text.encode("utf-8")
    )
    sdk = (
        None
        if output.sdk_config_fragment_text is None
        else output.sdk_config_fragment_text.encode("utf-8")
    )
    fragment = (
        None if projection is None else projection.format_fragment_text.encode("utf-8")
    )
    values = {
        **plan.model_dump(),
        "plan_sha256": plan.plan_sha256,
        "mechanism": output.selection.mechanism,
        "requested_mechanism": output.selection.requested_mechanism,
        "selection": output.selection,
        "projection_version": None
        if projection is None
        else projection.projection_version,
        "projection_sha256": None
        if projection is None
        else projection.projection_sha256,
        "neutral_schema_sha256": output.schema_sha256,
        "neutral_semantic_sha256": output.neutral_semantic_sha256,
        "native_schema_sha256": None if native is None else bytes_digest(native),
        "native_schema_bytes": None if native is None else len(native),
        "sdk_config_sha256": None if sdk is None else bytes_digest(sdk),
        "sdk_config_bytes": None if sdk is None else len(sdk),
        "format_sha256": None if fragment is None else bytes_digest(fragment),
        "format_bytes": None if fragment is None else len(fragment),
        "prompt_sha256": bytes_digest(prompt),
        "prompt_bytes": len(prompt),
        "payload_sha256": bytes_digest(payload),
        "payload_bytes": len(payload),
        "schema_version": 1,
        "transport_commitment_version": 1,
        "renderer_version": 3,
        "local_validation_required": True,
    }
    values.pop("plan_version", None)
    return TransportCommitmentV1.model_validate(
        {
            **values,
            "commitment_sha256": semantic_digest("transport-commitment-v1", values),
        }
    )


def prepare_transport_request(
    spec: RunSpecV3,
    dependency: RequestDependency,
    identity: PhysicalRequestIdentityV1,
    context: TransportContext,
    *,
    template: str | None = None,
    lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask]
    | None = None,
    frozen: TransportCommitmentV1 | None = None,
    parent: TransportCommitmentV1 | None = None,
    committed_output: OutputCommitmentV3 | None = None,
) -> PreparedRequestV3:
    """Build complete offline bytes or verify exact committed reconstruction.

    Replay reconstructs from the frozen selection without ordinary resolution.
    An inherited attempt also requires its independently verified parent receipt;
    a child's checksum cannot establish the originally committed family.
    """
    from judgmentllm.native_prompts import _render_text_v3, render_request_v3

    try:
        _validate_context(spec, context)
        live_context = context
        context = _effective_context(context)
        live_inputs = (spec, dependency, identity, context)
        input_token = _preparation_token(live_inputs)
        translators = _translator_snapshot(context)
        if input_token is not None:
            # All work uses one detached metadata/input snapshot. A callback
            # mutating and restoring caller-owned objects cannot influence a
            # decision while remaining invisible to the final live-value guard.
            spec, dependency, identity, context = deepcopy(
                (spec, dependency, identity, context)
            )
            if input_token != _preparation_token(
                (spec, dependency, identity, context)
            ) or not _same_translators(translators, _translator_snapshot(context)):
                raise TransportIntegrityError()
        context_sha256 = transport_context_digest(context)
        if (
            not context.trusted_synthetic
            or identity.request_uid != dependency.request.request_uid
        ):
            raise TransportIntegrityError()
        if parent is not None:
            parent = TransportCommitmentV1.model_validate(dict(parent))
            if (
                parent.identity.run_id != identity.run_id
                or parent.identity.request_uid != identity.request_uid
                or parent.identity.attempt_id == identity.attempt_id
                or parent.context_sha256 != context_sha256
                or parent.configuration_sha256
                != semantic_digest("run-configuration", spec)
                or parent.requested_mechanism != spec.prompt.native_mechanism
                or parent.selection.policy != spec.prompt.structured_output
            ):
                raise TransportIntegrityError()
        if frozen is not None:
            frozen = TransportCommitmentV1.model_validate(dict(frozen))
            if frozen.parent_commitment_sha256 is None:
                if parent is not None:
                    raise TransportIntegrityError()
            elif (
                parent is None
                or frozen.parent_commitment_sha256 != parent.commitment_sha256
                or frozen.mechanism != parent.mechanism
                or frozen.requested_mechanism != parent.requested_mechanism
                or frozen.selection.policy != parent.selection.policy
                or frozen.context_sha256 != parent.context_sha256
                or frozen.configuration_sha256 != parent.configuration_sha256
                or frozen.identity.run_id != parent.identity.run_id
                or frozen.identity.request_uid != parent.identity.request_uid
            ):
                raise TransportIntegrityError()
            if frozen.identity != identity or frozen.context_sha256 != context_sha256:
                raise TransportIntegrityError()
            if committed_output is None:
                contract = _contract(spec, dependency.request)
                projection = (
                    None
                    if frozen.mechanism == "text"
                    else _project(contract, context, frozen.mechanism)
                )
                raw = serialize_output_schema(contract)
                output = OutputCommitmentV3(
                    scope=context.scope,
                    contract=contract,
                    selection=frozen.selection,
                    schema_text=raw.decode("utf-8"),
                    schema_sha256=bytes_digest(raw),
                    neutral_semantic_sha256=semantic_digest(
                        "neutral-schema-semantics-v1", generate_output_schema(contract)
                    ),
                    projection=projection,
                    sdk_config_fragment_text=(
                        None
                        if projection is None
                        else projection.format_fragment_text
                        if isinstance(projection, NativeSchemaProjectionV1)
                        else projection.sdk_config_fragment_text
                    ),
                    repetition="canonical_neutral"
                    if projection is None
                    else "projected_native"
                    if context.scope.provider in {"together", "ollama"}
                    else "omit",
                    context_sha256=context_sha256,
                )
            else:
                output = OutputCommitmentV3.model_validate(
                    deepcopy(dict(committed_output))
                )
            if (
                output.context_sha256 != context_sha256
                or output.contract != _contract(spec, dependency.request)
                or output.selection.requested_mechanism != spec.prompt.native_mechanism
                or output.selection.policy != spec.prompt.structured_output
            ):
                raise TransportIntegrityError()
            if output.selection.openai_decision is not None:
                _verify_openai(
                    output.selection.openai_decision, output.contract, context
                )
            if output.selection.provider_decision is not None:
                _verify_native_transport_decision_v2(
                    output.selection.provider_decision,
                    output.contract,
                    scope=context.scope,
                    evidence=context.evidence,
                    catalog=context.provider_catalog or _DEFAULT_CATALOG_V2,
                )
        else:
            output = (
                build_output_commitment_v3(
                    spec, dependency.request, context, inherited_commitment=parent
                )
                if input_token is None
                else _build_output_commitment_v3(
                    spec,
                    dependency.request,
                    context,
                    context_sha256,
                    inherited_commitment=parent,
                )
            )
        # The constructor above fully validated these same values. Reuse only a
        # detached primitive snapshot for the plan hash within this call; the
        # public prepared-request validator independently validates current output
        # against this original hash and exact ordered payload before return.
        output_token = _preparation_token(output) if input_token is not None else None
        output_snapshot = output.model_dump(mode="json")
        output_sha256 = (
            semantic_digest("output-commitment-v3", output_snapshot)
            if output_token is not None
            else output.commitment_sha256
        )
        if lookup is None:
            record = dependency.request.response
            if record is None:
                raise TransportIntegrityError()

            def only_response(
                kind: Literal["response", "pair"], identifier: str
            ) -> ResponseItem | PairTask:
                if kind != "response" or record.item_id != identifier:
                    raise TransportIntegrityError()
                return record

            lookup = only_response
        rendered_text = (
            render_request_v3(spec, template, dependency.request, lookup, output).text
            if input_token is None
            else _render_text_v3(spec, template, dependency.request, lookup, output)
        )
        prompt = rendered_text.encode("utf-8")
        membership = semantic_digest(
            "attempt-membership", [task.task_id for task in dependency.request.tasks]
        )
        plan = TransportPlanV1(
            identity=identity,
            configuration_sha256=semantic_digest("run-configuration", spec),
            dependency_sha256=dependency.dependency_sha256,
            membership_sha256=membership,
            output_commitment_sha256=output_sha256,
            prompt_sha256=bytes_digest(prompt),
            context_sha256=context_sha256,
            parent_commitment_sha256=output.selection.parent_commitment_sha256,
        )
        payload_object: dict[str, object] = {
            "protocol": "judgmentllm-native-offline-v1",
            "service_identity": context.service_identity,
            "fixture_sha256": context.fixture_sha256,
            "provider": spec.provider,
            "endpoint": spec.endpoint,
            "model": spec.model,
            "request_uid": dependency.request.request_uid,
            "tasks": dependency.request.tasks,
            "prompt": rendered_text,
            "output_commitment": output_snapshot,
            "transport_plan": plan,
            "format_fragment": None
            if output.projection is None
            else decode_assessment_output(output.projection.format_fragment_text),
        }
        if spec.generation is not None:
            payload_object["generation"] = spec.generation
        # Preserve ordered native fragment bytes as a string as well as its
        # decoded physical configuration; neither hash replaces the final body.
        payload_object["format_fragment_text"] = (
            None
            if output.projection is None
            else output.projection.format_fragment_text
        )
        payload = ordered_json_bytes(_json_object(payload_object))
        receipt = _receipt(plan, output, prompt, payload)
        if frozen is not None and receipt != frozen:
            raise TransportIntegrityError()
        prepared = PreparedRequestV3(
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
            rendered_text=rendered_text,
        )
        if (
            (
                input_token is None
                and context_sha256 != transport_context_digest(live_context)
            )
            or not _same_translators(translators, _translator_snapshot(live_context))
            or not _same_translators(translators, _translator_snapshot(context))
            or (
                input_token is not None
                and (
                    input_token
                    != _preparation_token(
                        (*live_inputs[:3], _effective_context(live_context))
                    )
                    or input_token
                    != _preparation_token((spec, dependency, identity, context))
                )
            )
            # Guard the original raw object, too: a newly validated replacement
            # cannot authorize normalization of a changed literal such as 3.0.
            or (output_token is not None and output_token != _preparation_token(output))
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


def _json_object(value: object) -> object:
    """Canonical primitives without sorting insertion-ordered transport maps."""
    from pydantic import BaseModel

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            key: _json_object(item)
            for key, item in cast(dict[str, object], value).items()
        }
    if isinstance(value, (tuple, list)):
        return [
            _json_object(item)
            for item in cast(tuple[object, ...] | list[object], value)
        ]
    return value
