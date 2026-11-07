"""Standalone native-transport policy; no durable selection or adapter activation.

The default catalog registers translator code only. It grants no real model
acceptance. Private synthetic catalogs exercise positive decisions offline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from judgmentllm.evidence.scope import ScopedProviderEvidence
from judgmentllm.native_schema import (
    NativeModel,
    NativeProjectionError,
    NativeProjectionScopeV1,
    NativeSchemaProjectionV1,
    snapshot_source,
)
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    RubricOutputContract,
    serialize_output_schema,
)
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import SHA256
from judgmentllm.providers.openai_schema import project_openai_schema

NativeSupport = Literal["supported", "unsupported", "unknown"]
NativePolicy = Literal["native", "auto", "text"]
NativeMechanism = Literal["openai.responses.text.format", "openai.chat.response_format"]
NativeFeature = Literal[
    "rubric_scores",
    "rubric_grouped",
    "rubric_explanations_together",
    "rubric_scores_first",
    "rubric_explanations_first",
    "pairwise_forced_choice",
    "pairwise_ties",
    "pairwise_explanations",
]
NativeSelectionReason = Literal[
    "compatible_native",
    "translator_unavailable",
    "translator_scope_incompatible",
    "projection_features_incompatible",
    "projection_incompatible",
    "native_known_unsupported",
    "native_support_unknown",
    "trusted_fixture_missing",
    "trusted_provenance_missing",
    "fixture_support_unestablished",
    "evidence_scope_mismatch",
    "invalid_selection_input",
    "decision_integrity_conflict",
]
_FEATURES: tuple[NativeFeature, ...] = (
    "rubric_scores",
    "rubric_grouped",
    "rubric_explanations_together",
    "rubric_scores_first",
    "rubric_explanations_first",
    "pairwise_forced_choice",
    "pairwise_ties",
    "pairwise_explanations",
)
_DIALECT = "openai.strict-json-schema.v1"


class NativeSelectionDiagnosticV1(NativeModel):
    """Only fixed codes; no schemas, category labels, identities or provider text."""

    reason: NativeSelectionReason
    native_support: NativeSupport


class NativeSelectionError(ValueError):
    """Content-free failure before any immutable decision is returned."""

    def __init__(self, reason: NativeSelectionReason, support: NativeSupport):
        self.diagnostic = NativeSelectionDiagnosticV1(
            reason=reason, native_support=support
        )
        self.reason = self.diagnostic.reason
        super().__init__("Native transport decision unavailable: " + self.reason)


class _FixtureProfile(NativeModel):
    """Private acceptance authority restricted to synthetic qualification input.

    A caller-supplied ScopedProviderEvidence is never itself a profile. The
    production catalog contains none. Fixture profiles bind independent docs
    and checked-in fixture hashes as well as all relevant translator scope.
    """

    scope: NativeProjectionScopeV1 = Field(repr=False)
    mechanism: NativeMechanism
    schema_dialect: Literal["openai.strict-json-schema.v1"] = _DIALECT
    implementation_version: Literal[1] = 1
    features: tuple[NativeFeature, ...] = Field(min_length=1, max_length=8)
    documentation_sha256: SHA256
    fixture_sha256: SHA256

    @field_validator("implementation_version", mode="before")
    @classmethod
    def strict_implementation_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("implementation version must be integer")
        return value

    @model_validator(mode="after")
    def synthetic_scope(self) -> _FixtureProfile:
        if (
            self.scope.provider != "openai"
            or self.scope.model_scope != "exact"
            or self.scope.model != "judgmentllm-synthetic-native-v1"
            or self.scope.sdk_package != "openai"
            or self.scope.sdk_version != "3.42.1"
            or self.scope.api_version is not None
            or len(set(self.features)) != len(self.features)
            or (self.scope.endpoint, self.mechanism)
            not in {
                ("responses", "openai.responses.text.format"),
                ("chat.completions", "openai.chat.response_format"),
            }
        ):
            raise ValueError("fixture profile must have an exact synthetic scope")
        return self


class _Translator(Protocol):
    def __call__(
        self, contract: AssessmentOutputContract, *, scope: NativeProjectionScopeV1
    ) -> NativeSchemaProjectionV1: ...


@dataclass(frozen=True)
class _Implementation:
    endpoint: Literal["responses", "chat.completions"]
    mechanism: NativeMechanism
    translate: _Translator
    features: tuple[NativeFeature, ...] = _FEATURES


@dataclass(frozen=True)
class _Catalog:
    implementations: tuple[_Implementation, ...]
    profiles: tuple[_FixtureProfile, ...] = ()


_DEFAULT_CATALOG = _Catalog(
    implementations=(
        _Implementation(
            "responses", "openai.responses.text.format", project_openai_schema
        ),
        _Implementation(
            "chat.completions", "openai.chat.response_format", project_openai_schema
        ),
    )
)


class TransportDecisionV1(NativeModel):
    """Frozen in-memory policy evidence, independent of SchemaSelectionV2.

    A schema digest is an exact ordered-byte identity supplied by projection;
    decision_sha256 separately commits these semantic decision fields.
    """

    policy: NativePolicy
    strict_capabilities: bool
    scope: NativeProjectionScopeV1 = Field(repr=False)
    schema_dialect: Literal["openai.strict-json-schema.v1", "unsupported"]
    source_schema_sha256: SHA256
    source_sha256: SHA256
    features: tuple[NativeFeature, ...]
    mechanism: Literal[
        "text", "openai.responses.text.format", "openai.chat.response_format"
    ]
    native_support: NativeSupport
    model_support: NativeSupport
    reason: Literal[
        "explicit_text",
        "native_selected",
        "native_unsupported_text_selected",
        "native_unknown_text_selected",
    ]
    diagnostic: NativeSelectionDiagnosticV1
    implemented: bool
    fixture_tested: bool
    live_qualified: Literal[None] = None
    implementation_version: Literal[1] | None = None
    documentation_sha256: SHA256 | None = None
    fixture_sha256: SHA256 | None = None
    projection_sha256: SHA256 | None = None
    local_validation_required: Literal[True] = True
    decision_sha256: SHA256

    @field_validator("implementation_version", mode="before")
    @classmethod
    def strict_implementation_version(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("implementation version must be integer")
        return value

    @field_validator("local_validation_required", mode="before")
    @classmethod
    def strict_local_validation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("local validation is mandatory")
        return value

    @model_validator(mode="after")
    def exact_decision(self) -> TransportDecisionV1:
        native = self.mechanism != "text"
        expected_reason = (
            "native_selected"
            if native
            else "explicit_text"
            if self.policy == "text"
            else "native_unsupported_text_selected"
            if self.native_support == "unsupported"
            else "native_unknown_text_selected"
        )
        if (
            self.native_support != self.diagnostic.native_support
            or len(set(self.features)) != len(self.features)
            or self.reason != expected_reason
            or (
                self.native_support == "supported"
                and (
                    self.model_support != "supported"
                    or not self.fixture_tested
                    or self.diagnostic.reason != "compatible_native"
                )
            )
            or self.implemented != (self.implementation_version is not None)
            or self.fixture_tested
            != (
                self.documentation_sha256 is not None
                and self.fixture_sha256 is not None
            )
            or (
                not self.fixture_tested
                and (
                    self.documentation_sha256 is not None
                    or self.fixture_sha256 is not None
                )
            )
            or (self.fixture_tested and not self.implemented)
            or (
                native
                and (
                    self.policy == "text"
                    or self.native_support != "supported"
                    or not self.fixture_tested
                    or self.projection_sha256 is None
                    or self.schema_dialect != _DIALECT
                    or (self.scope.provider, self.scope.endpoint, self.mechanism)
                    not in {
                        ("openai", "responses", "openai.responses.text.format"),
                        ("openai", "chat.completions", "openai.chat.response_format"),
                    }
                )
            )
            or (
                not native
                and (
                    self.policy == "native"
                    or self.projection_sha256 is not None
                    or (self.policy == "auto" and self.native_support == "supported")
                    or (
                        self.policy == "auto"
                        and self.strict_capabilities
                        and self.native_support == "unknown"
                    )
                )
            )
            or self.decision_sha256
            != _decision_digest(
                self.model_dump(mode="json", exclude={"decision_sha256"})
            )
        ):
            raise ValueError("native transport decision conflicts")
        return self


def _decision_digest(values: object) -> str:
    return semantic_digest("native-transport-decision-v1", values)


def _features(contract: AssessmentOutputContract) -> tuple[NativeFeature, ...]:
    if isinstance(contract, RubricOutputContract):
        result: list[NativeFeature] = ["rubric_scores"]
        if len(contract.traits) > 1:
            result.append("rubric_grouped")
        if contract.explanations:
            if contract.output_order == "together":
                result.append("rubric_explanations_together")
            elif contract.output_order == "scores_first":
                result.append("rubric_scores_first")
            else:
                result.append("rubric_explanations_first")
        return tuple(result)
    result = ["pairwise_ties" if contract.allow_ties else "pairwise_forced_choice"]
    if contract.explanations:
        result.append("pairwise_explanations")
    return tuple(result)


def _checked_evidence(
    scope: NativeProjectionScopeV1, evidence: ScopedProviderEvidence | None
) -> ScopedProviderEvidence | None:
    if evidence is None:
        return None
    checked = ScopedProviderEvidence.model_validate(dict(evidence))
    if checked.feature != "native_schema" or any(
        getattr(scope, field) != getattr(checked, field)
        for field in (
            "provider",
            "endpoint",
            "model",
            "model_scope",
            "sdk_package",
            "sdk_version",
            "api_version",
        )
    ):
        raise NativeSelectionError("evidence_scope_mismatch", "unknown")
    return checked


def resolve_native_transport(
    contract: AssessmentOutputContract,
    *,
    scope: NativeProjectionScopeV1,
    policy: NativePolicy,
    strict_capabilities: bool = False,
    evidence: ScopedProviderEvidence | None = None,
    schema_dialect: str = _DIALECT,
) -> TransportDecisionV1:
    """Resolve standalone policy with no accepted real-model profiles.

    This does not change historical rendering, durable selections or transports.
    Explicit text needs no projection or positive native-support evidence.
    """
    return _resolve_native_transport(
        contract,
        scope=scope,
        policy=policy,
        strict_capabilities=strict_capabilities,
        evidence=evidence,
        schema_dialect=schema_dialect,
        catalog=_DEFAULT_CATALOG,
    )


def _resolve_native_transport(
    contract: AssessmentOutputContract,
    *,
    scope: NativeProjectionScopeV1,
    policy: NativePolicy,
    strict_capabilities: bool = False,
    evidence: ScopedProviderEvidence | None = None,
    schema_dialect: str = _DIALECT,
    catalog: _Catalog,
) -> TransportDecisionV1:
    # Public boundary errors cannot disclose arbitrary keyword values or content.
    try:
        scope = NativeProjectionScopeV1.model_validate(dict(scope))
        if (
            policy not in {"native", "auto", "text"}
            or type(strict_capabilities) is not bool
            or type(schema_dialect) is not str
        ):
            raise NativeSelectionError("invalid_selection_input", "unknown")
        features = _features(contract)
        evidence = _checked_evidence(scope, evidence)
    except NativeSelectionError:
        raise
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
        RecursionError,
    ):
        raise NativeSelectionError("invalid_selection_input", "unknown") from None

    implementation = next(
        (
            entry
            for entry in catalog.implementations
            if scope.provider == "openai" and entry.endpoint == scope.endpoint
        ),
        None,
    )
    projection: NativeSchemaProjectionV1 | None = None
    profile: _FixtureProfile | None = None
    support: NativeSupport = "unknown"
    diagnostic: NativeSelectionReason = "native_support_unknown"
    fixture_tested = False
    if implementation is None:
        support, diagnostic = "unsupported", "translator_unavailable"
    elif (
        scope.sdk_package != "openai"
        or scope.sdk_version != "3.42.1"
        or scope.api_version is not None
        or schema_dialect != _DIALECT
    ):
        support, diagnostic = "unsupported", "translator_scope_incompatible"
    elif not set(features) <= set(implementation.features):
        support, diagnostic = "unsupported", "projection_features_incompatible"
    else:
        if policy != "text":
            # The projector budgets expansion before canonical serialization.
            try:
                projection = implementation.translate(contract, scope=scope)
            except NativeProjectionError:
                support, diagnostic = "unsupported", "projection_incompatible"
            else:
                if (
                    projection.scope != scope
                    or projection.mechanism != implementation.mechanism
                    or projection.schema_dialect != schema_dialect
                    or projection.implementation_version != 1
                ):
                    support, diagnostic = "unsupported", "projection_incompatible"
                    projection = None
        if support != "unsupported":
            profile = next(
                (
                    item
                    for item in catalog.profiles
                    if item.scope == scope
                    and item.mechanism == implementation.mechanism
                    and item.schema_dialect == schema_dialect
                    and set(features) <= set(item.features)
                ),
                None,
            )
            provenance_matches = (
                profile is not None
                and evidence is not None
                and {
                    ("documentation", profile.documentation_sha256),
                    ("offline_fixture", profile.fixture_sha256),
                }
                <= {(item.kind, item.source_sha256) for item in evidence.provenance}
            )
            fixture_tested = bool(
                provenance_matches
                and evidence is not None
                and evidence.implemented is True
                and evidence.fixture_tested is True
                and evidence.live_qualified is not True
            )
            if evidence is not None and evidence.supported is False:
                support, diagnostic = "unsupported", "native_known_unsupported"
            elif policy == "text" or evidence is None or evidence.supported is None:
                diagnostic = "native_support_unknown"
            elif profile is None:
                diagnostic = "trusted_fixture_missing"
            elif not provenance_matches:
                diagnostic = "trusted_provenance_missing"
            elif not fixture_tested:
                diagnostic = "fixture_support_unestablished"
            else:
                support, diagnostic = "supported", "compatible_native"
    if (
        policy == "native"
        and support != "supported"
        or policy == "auto"
        and strict_capabilities
        and support == "unknown"
    ):
        raise NativeSelectionError(diagnostic, support)
    try:
        # Successful candidates already commit validated canonical/source bytes.
        # Explicit text preserves the historical complete neutral contract.
        source_hash = (
            projection.neutral_schema_sha256
            if projection
            else bytes_digest(serialize_output_schema(contract))
        )
        source_identity = (
            projection.source_sha256
            if projection
            else bytes_digest(canonical_bytes(snapshot_source(contract)))
        )
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
        RecursionError,
    ):
        raise NativeSelectionError("invalid_selection_input", "unknown") from None
    native = policy != "text" and support == "supported"
    values = {
        "schema_version": 1,
        "policy": policy,
        "strict_capabilities": strict_capabilities,
        "scope": scope.model_dump(mode="json"),
        "schema_dialect": schema_dialect
        if schema_dialect == _DIALECT
        else "unsupported",
        "source_schema_sha256": source_hash,
        "source_sha256": source_identity,
        "features": list(features),
        "mechanism": implementation.mechanism if native and implementation else "text",
        "native_support": support,
        "model_support": "unknown"
        if evidence is None or evidence.supported is None
        else "supported"
        if evidence.supported
        else "unsupported",
        "reason": "native_selected"
        if native
        else "explicit_text"
        if policy == "text"
        else "native_unsupported_text_selected"
        if support == "unsupported"
        else "native_unknown_text_selected",
        "diagnostic": NativeSelectionDiagnosticV1(
            reason=diagnostic, native_support=support
        ).model_dump(mode="json"),
        "implemented": implementation is not None,
        "fixture_tested": fixture_tested,
        "live_qualified": None,
        "implementation_version": 1 if implementation is not None else None,
        "documentation_sha256": profile.documentation_sha256
        if fixture_tested and profile
        else None,
        "fixture_sha256": profile.fixture_sha256
        if fixture_tested and profile
        else None,
        "projection_sha256": projection.projection_sha256
        if native and projection
        else None,
        "local_validation_required": True,
    }
    return TransportDecisionV1.model_validate_json(
        json.dumps({**values, "decision_sha256": _decision_digest(values)})
    )


def verify_native_transport_decision(
    decision: TransportDecisionV1,
    contract: AssessmentOutputContract,
    *,
    scope: NativeProjectionScopeV1,
    evidence: ScopedProviderEvidence | None = None,
) -> TransportDecisionV1:
    """Verify fixed choice against current facts; never return a replacement."""
    return _verify_native_transport_decision(
        decision, contract, scope=scope, evidence=evidence, catalog=_DEFAULT_CATALOG
    )


def _verify_native_transport_decision(
    decision: TransportDecisionV1,
    contract: AssessmentOutputContract,
    *,
    scope: NativeProjectionScopeV1,
    evidence: ScopedProviderEvidence | None = None,
    catalog: _Catalog,
) -> TransportDecisionV1:
    try:
        checked = TransportDecisionV1.model_validate(dict(decision))
        expected = _resolve_native_transport(
            contract,
            scope=scope,
            policy=checked.policy,
            strict_capabilities=checked.strict_capabilities,
            evidence=evidence,
            schema_dialect=checked.schema_dialect,
            catalog=catalog,
        )
        if checked != expected:
            raise ValueError("decision identity changed")
        return checked
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
        RecursionError,
    ):
        raise NativeSelectionError("decision_integrity_conflict", "unknown") from None
