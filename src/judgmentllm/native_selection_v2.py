"""Standalone V2 route selection; no production model acceptance or execution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import Field, model_validator

from judgmentllm.evidence.scope import ScopedProviderEvidence
from judgmentllm.native_schema import (
    NativeModel,
    NativeProjectionScopeV1,
    snapshot_source,
)
from judgmentllm.native_schema_v2 import (
    _ROUTES_V2,  # pyright: ignore[reportPrivateUsage]
    NativeMechanismV2,
    NativeProjectionErrorV2,
    NativeSchemaDialectV2,
    NativeSchemaProjectionV2,
    _checked_scope_v2,  # pyright: ignore[reportPrivateUsage]
    _route_v2,  # pyright: ignore[reportPrivateUsage]
    _RouteV2,  # pyright: ignore[reportPrivateUsage]
    project_native_schema_v2,
)
from judgmentllm.native_selection import (
    NativeFeature,
    NativePolicy,
    NativeSelectionReason,
    NativeSupport,
    _features,  # pyright: ignore[reportPrivateUsage]
)
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    serialize_output_schema,
)
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import SHA256

_FEATURES_V2: tuple[NativeFeature, ...] = (
    "rubric_scores",
    "rubric_grouped",
    "rubric_explanations_together",
    "rubric_scores_first",
    "rubric_explanations_first",
    "pairwise_forced_choice",
    "pairwise_ties",
    "pairwise_explanations",
)


class NativeSelectionDiagnosticV2(NativeModel):
    reason: NativeSelectionReason
    native_support: NativeSupport


class NativeSelectionErrorV2(ValueError):
    def __init__(self, reason: NativeSelectionReason, support: NativeSupport) -> None:
        self.diagnostic = NativeSelectionDiagnosticV2(
            reason=reason, native_support=support
        )
        self.reason = reason
        super().__init__("Native transport decision unavailable: " + reason)


class _FixtureProfileV2(NativeModel):
    """Private synthetic authority, never a real-model capability assertion."""

    profile_version: Literal[2] = 2
    scope: NativeProjectionScopeV1 = Field(repr=False)
    mechanism: NativeMechanismV2
    schema_dialect: NativeSchemaDialectV2
    implementation_version: Literal[1] = 1
    features: tuple[NativeFeature, ...] = Field(min_length=1, max_length=9)
    documentation_sha256: SHA256
    fixture_sha256: SHA256

    @model_validator(mode="after")
    def exact_synthetic_scope(self) -> _FixtureProfileV2:
        route = _route_v2(self.mechanism)
        _checked_scope_v2(self.scope, route)
        if (
            self.scope.model_scope != "exact"
            or self.scope.model != "judgmentllm-synthetic-native-v2"
            or self.schema_dialect != route.schema_dialect
            or len(set(self.features)) != len(self.features)
        ):
            raise ValueError("fixture scope must be exact and synthetic")
        return self


@dataclass(frozen=True)
class _CatalogV2:
    implementations: tuple[_RouteV2, ...] = _ROUTES_V2
    profiles: tuple[_FixtureProfileV2, ...] = ()


_DEFAULT_CATALOG_V2 = _CatalogV2()


def _decision_digest_v2(values: object) -> str:
    return semantic_digest("native-transport-decision-v2", values)


class TransportDecisionV2(NativeModel):
    """Frozen explicit route choice; verification cannot substitute a route."""

    decision_version: Literal[2] = 2
    policy: NativePolicy
    strict_capabilities: bool
    scope: NativeProjectionScopeV1 = Field(repr=False)
    requested_mechanism: NativeMechanismV2
    schema_dialect: NativeSchemaDialectV2 | Literal["unsupported"]
    source_schema_sha256: SHA256
    source_sha256: SHA256
    evidence_sha256: SHA256 | None
    features: tuple[NativeFeature, ...] = Field(min_length=1, max_length=9)
    mechanism: NativeMechanismV2 | Literal["text"]
    native_support: NativeSupport
    model_support: NativeSupport
    reason: Literal[
        "explicit_text",
        "native_selected",
        "native_unsupported_text_selected",
        "native_unknown_text_selected",
    ]
    diagnostic: NativeSelectionDiagnosticV2
    implemented: bool
    fixture_tested: bool
    live_qualified: Literal[None] = None
    implementation_version: Literal[1] | None = None
    documentation_sha256: SHA256 | None = None
    fixture_sha256: SHA256 | None = None
    projection_sha256: SHA256 | None = None
    local_validation_required: Literal[True] = True
    decision_sha256: SHA256

    @model_validator(mode="after")
    def exact_decision(self) -> TransportDecisionV2:
        route = _route_v2(self.requested_mechanism)
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
            or self.reason != expected_reason
            or len(set(self.features)) != len(self.features)
            or not set(self.features)
            <= (set(_FEATURES_V2) | {"assessment_presentation_v2"})
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
            or (
                self.fixture_tested
                and (not self.implemented or self.evidence_sha256 is None)
            )
            or (self.evidence_sha256 is None and self.model_support != "unknown")
            or (self.policy == "text" and self.native_support == "supported")
            or (
                self.diagnostic.reason == "compatible_native"
                and self.native_support != "supported"
            )
            or (
                self.diagnostic.reason == "native_known_unsupported"
                and self.model_support != "unsupported"
            )
            or (
                not self.implemented
                and self.diagnostic.reason != "translator_unavailable"
            )
            or (
                self.native_support == "supported"
                and (
                    self.model_support != "supported"
                    or not self.fixture_tested
                    or self.diagnostic.reason != "compatible_native"
                )
            )
            or (
                native
                and (
                    self.policy == "text"
                    or self.native_support != "supported"
                    or not self.fixture_tested
                    or self.projection_sha256 is None
                    or self.schema_dialect != route.schema_dialect
                    or self.mechanism != self.requested_mechanism
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
            != _decision_digest_v2(
                self.model_dump(mode="json", exclude={"decision_sha256"})
            )
        ):
            raise ValueError("native transport decision conflicts")
        if native:
            _checked_scope_v2(self.scope, route)
            if (
                self.scope.model_scope != "exact"
                or self.scope.model != "judgmentllm-synthetic-native-v2"
            ):
                raise ValueError(
                    "native acceptance requires a trusted synthetic profile"
                )
        return self


def _checked_evidence_v2(
    scope: NativeProjectionScopeV1, evidence: ScopedProviderEvidence | None
) -> ScopedProviderEvidence | None:
    if evidence is None:
        return None
    checked = ScopedProviderEvidence.model_validate(dict(evidence))
    if checked.feature != "native_schema" or any(
        getattr(scope, name) != getattr(checked, name)
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
        raise NativeSelectionErrorV2("evidence_scope_mismatch", "unknown")
    return checked


def resolve_native_transport_v2(
    contract: AssessmentOutputContract,
    *,
    scope: NativeProjectionScopeV1,
    mechanism: NativeMechanismV2,
    policy: NativePolicy,
    strict_capabilities: bool = False,
    evidence: ScopedProviderEvidence | None = None,
    schema_dialect: str | None = None,
) -> TransportDecisionV2:
    """Resolve an explicitly chosen route; the default catalog grants no models."""
    return _resolve_native_transport_v2(
        contract,
        scope=scope,
        mechanism=mechanism,
        policy=policy,
        strict_capabilities=strict_capabilities,
        evidence=evidence,
        schema_dialect=schema_dialect,
        catalog=_DEFAULT_CATALOG_V2,
    )


def _resolve_native_transport_v2(
    contract: AssessmentOutputContract,
    *,
    scope: NativeProjectionScopeV1,
    mechanism: NativeMechanismV2,
    policy: NativePolicy,
    strict_capabilities: bool = False,
    evidence: ScopedProviderEvidence | None = None,
    schema_dialect: str | None = None,
    catalog: _CatalogV2,
) -> TransportDecisionV2:
    try:
        scope = NativeProjectionScopeV1.model_validate(dict(scope))
        route = _route_v2(mechanism)
        if (
            policy not in {"native", "auto", "text"}
            or type(strict_capabilities) is not bool
            or (schema_dialect is not None and type(schema_dialect) is not str)
        ):
            raise ValueError("invalid selection input")
        dialect = route.schema_dialect if schema_dialect is None else schema_dialect
        features = _features(contract)
        evidence = _checked_evidence_v2(scope, evidence)
    except NativeSelectionErrorV2:
        raise
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
        RecursionError,
    ):
        raise NativeSelectionErrorV2("invalid_selection_input", "unknown") from None

    implemented = route in catalog.implementations
    projection: NativeSchemaProjectionV2 | None = None
    profile: _FixtureProfileV2 | None = None
    support: NativeSupport = "unknown"
    diagnostic: NativeSelectionReason = "native_support_unknown"
    fixture_tested = False
    if not implemented:
        support, diagnostic = "unsupported", "translator_unavailable"
    else:
        try:
            _checked_scope_v2(scope, route)
            if dialect != route.schema_dialect:
                raise NativeProjectionErrorV2("unsupported_scope")
        except NativeProjectionErrorV2:
            support, diagnostic = "unsupported", "translator_scope_incompatible"
        if support != "unsupported" and policy != "text":
            try:
                projection = project_native_schema_v2(
                    contract, scope=scope, mechanism=mechanism
                )
            except NativeProjectionErrorV2:
                support, diagnostic = "unsupported", "projection_incompatible"
        if support != "unsupported":
            profile = next(
                (
                    item
                    for item in catalog.profiles
                    if (
                        item.scope == scope
                        and item.mechanism == mechanism
                        and item.schema_dialect == dialect
                        and set(features) <= set(item.features)
                    )
                ),
                None,
            )
            provenance_matches = bool(
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
    if (policy == "native" and support != "supported") or (
        policy == "auto" and strict_capabilities and support == "unknown"
    ):
        raise NativeSelectionErrorV2(diagnostic, support)
    try:
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
        raise NativeSelectionErrorV2("invalid_selection_input", "unknown") from None
    native = policy != "text" and support == "supported"
    values = {
        "schema_version": 1,
        "decision_version": 2,
        "policy": policy,
        "strict_capabilities": strict_capabilities,
        "scope": scope.model_dump(mode="json"),
        "requested_mechanism": mechanism,
        "schema_dialect": dialect if dialect == route.schema_dialect else "unsupported",
        "source_schema_sha256": source_hash,
        "source_sha256": source_identity,
        "evidence_sha256": None
        if evidence is None
        else semantic_digest("native-scope-evidence-v2", evidence),
        "features": list(features),
        "mechanism": mechanism if native else "text",
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
        "diagnostic": NativeSelectionDiagnosticV2(
            reason=diagnostic, native_support=support
        ).model_dump(mode="json"),
        "implemented": implemented,
        "fixture_tested": fixture_tested,
        "live_qualified": None,
        "implementation_version": 1 if implemented else None,
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
    return TransportDecisionV2.model_validate_json(
        json.dumps({**values, "decision_sha256": _decision_digest_v2(values)})
    )


def verify_native_transport_decision_v2(
    decision: TransportDecisionV2,
    contract: AssessmentOutputContract,
    *,
    scope: NativeProjectionScopeV1,
    evidence: ScopedProviderEvidence | None = None,
) -> TransportDecisionV2:
    """Verify the frozen mechanism and identity, without replacement or fallback."""
    return _verify_native_transport_decision_v2(
        decision, contract, scope=scope, evidence=evidence, catalog=_DEFAULT_CATALOG_V2
    )


def _verify_native_transport_decision_v2(
    decision: TransportDecisionV2,
    contract: AssessmentOutputContract,
    *,
    scope: NativeProjectionScopeV1,
    evidence: ScopedProviderEvidence | None = None,
    catalog: _CatalogV2,
) -> TransportDecisionV2:
    try:
        checked = TransportDecisionV2.model_validate(dict(decision))
        checked_scope = NativeProjectionScopeV1.model_validate(dict(scope))
        checked_evidence = _checked_evidence_v2(checked_scope, evidence)
        route = _route_v2(checked.requested_mechanism)
        features = _features(contract)
        evidence_hash = (
            None
            if checked_evidence is None
            else semantic_digest("native-scope-evidence-v2", checked_evidence)
        )
        if (
            checked.scope != checked_scope
            or checked.features != features
            or checked.evidence_sha256 != evidence_hash
            or checked.source_schema_sha256
            != bytes_digest(serialize_output_schema(contract))
            or checked.source_sha256
            != bytes_digest(canonical_bytes(snapshot_source(contract)))
            or checked.implemented != (route in catalog.implementations)
        ):
            raise ValueError("frozen decision facts changed")
        profile = next(
            (
                item
                for item in catalog.profiles
                if (
                    item.scope == checked_scope
                    and item.mechanism == checked.requested_mechanism
                    and item.schema_dialect == checked.schema_dialect
                    and set(features) <= set(item.features)
                )
            ),
            None,
        )
        provenance_matches = bool(
            profile is not None
            and checked_evidence is not None
            and {
                ("documentation", profile.documentation_sha256),
                ("offline_fixture", profile.fixture_sha256),
            }
            <= {(item.kind, item.source_sha256) for item in checked_evidence.provenance}
        )
        fixture_tested = bool(
            provenance_matches
            and checked_evidence is not None
            and checked_evidence.implemented is True
            and checked_evidence.fixture_tested is True
            and checked_evidence.live_qualified is not True
        )
        # Incompatible translator decisions carry no fixture acceptance. An
        # auto projection failure is rechecked below against its frozen route.
        if checked.diagnostic.reason in {
            "translator_unavailable",
            "translator_scope_incompatible",
            "projection_incompatible",
        }:
            fixture_tested = False
        if (
            checked.fixture_tested != fixture_tested
            or checked.documentation_sha256
            != (profile.documentation_sha256 if fixture_tested and profile else None)
            or checked.fixture_sha256
            != (profile.fixture_sha256 if fixture_tested and profile else None)
        ):
            raise ValueError("trusted profile facts changed")
        model_support: NativeSupport = (
            "unknown"
            if checked_evidence is None or checked_evidence.supported is None
            else "supported"
            if checked_evidence.supported
            else "unsupported"
        )
        if checked.model_support != model_support:
            raise ValueError("model evidence facts changed")
        scope_compatible = checked.schema_dialect == route.schema_dialect
        try:
            _checked_scope_v2(checked_scope, route)
        except NativeProjectionErrorV2:
            scope_compatible = False
        expected_support: NativeSupport = "unknown"
        expected_diagnostic: NativeSelectionReason = "native_support_unknown"
        if not checked.implemented:
            expected_support, expected_diagnostic = (
                "unsupported",
                "translator_unavailable",
            )
        elif not scope_compatible:
            expected_support, expected_diagnostic = (
                "unsupported",
                "translator_scope_incompatible",
            )
        elif checked.diagnostic.reason == "projection_incompatible":
            if checked.policy == "text":
                raise ValueError(
                    "explicit text cannot contain a translation observation"
                )
            # An auto fallback's recorded incompatibility must still be true
            # for its exact frozen route. This probes no alternative and never
            # chooses a replacement. A fabricated rehashed failure must fail.
            try:
                project_native_schema_v2(
                    contract, scope=checked_scope, mechanism=checked.requested_mechanism
                )
            except NativeProjectionErrorV2:
                pass
            else:
                raise ValueError("recorded projection failure is not established")
            expected_support, expected_diagnostic = (
                "unsupported",
                "projection_incompatible",
            )
        elif checked_evidence is not None and checked_evidence.supported is False:
            expected_support, expected_diagnostic = (
                "unsupported",
                "native_known_unsupported",
            )
        elif (
            checked.policy == "text"
            or checked_evidence is None
            or checked_evidence.supported is None
        ):
            pass
        elif profile is None:
            expected_diagnostic = "trusted_fixture_missing"
        elif not provenance_matches:
            expected_diagnostic = "trusted_provenance_missing"
        elif not fixture_tested:
            expected_diagnostic = "fixture_support_unestablished"
        else:
            expected_support, expected_diagnostic = "supported", "compatible_native"
        if (
            checked.native_support != expected_support
            or checked.diagnostic.reason != expected_diagnostic
        ):
            raise ValueError("committed support facts changed")
        if checked.mechanism != "text":
            if (
                not fixture_tested
                or checked_evidence is None
                or checked_evidence.supported is not True
            ):
                raise ValueError("native acceptance is no longer established")
            projection = project_native_schema_v2(
                contract, scope=checked_scope, mechanism=checked.requested_mechanism
            )
            if (
                checked.projection_sha256 != projection.projection_sha256
                or checked.schema_dialect != projection.schema_dialect
                or checked.implementation_version != projection.implementation_version
            ):
                raise ValueError("pinned native projection changed")
        # Return the same choice. No resolver, alternative route or replacement
        # is permitted; explicit text never invokes a translator.
        return checked
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
        RecursionError,
    ):
        raise NativeSelectionErrorV2("decision_integrity_conflict", "unknown") from None
