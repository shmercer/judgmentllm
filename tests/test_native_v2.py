"""Additive native contracts and exact synthetic route-selection authority."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError
from test_native_schema import pairwise, rubric

from judgmentllm import native_schema_v2 as schemas
from judgmentllm import native_selection_v2 as selection
from judgmentllm.evidence.scope import EvidenceProvenance, ScopedProviderEvidence
from judgmentllm.native_schema import NativeModel, NativeProjectionScopeV1
from judgmentllm.native_schema_v2 import (
    NativeConstraintCoverageV2,
    NativeProjectionErrorV2,
    NativeSchemaProjectionV2,
    project_native_schema_v2,
)
from judgmentllm.native_selection import NativePolicy
from judgmentllm.native_selection_v2 import (
    NativeSelectionErrorV2,
    TransportDecisionV2,
    resolve_native_transport_v2,
    verify_native_transport_decision_v2,
)
from judgmentllm.output_contracts import serialize_output_schema
from judgmentllm.persistence.hashing import bytes_digest

ROUTES = schemas._ROUTES_V2
SELECTION_FIXTURE = (
    Path(__file__).parent / "fixtures/native_v2/synthetic-selection.json"
)
SELECTION_SHA256 = "e5f1f99e04db2861d168a7fc7f7e4393a4a7a1ff18652de86c60746bfb4d5b02"


def scope(route: schemas._RouteV2, **updates: object) -> NativeProjectionScopeV1:
    return NativeProjectionScopeV1.model_validate(
        {
            "provider": route.provider,
            "endpoint": route.endpoint,
            "model": "judgmentllm-synthetic-native-v2",
            "model_scope": "exact",
            "sdk_package": route.sdk_package,
            "sdk_version": route.sdk_version,
            "api_version": route.api_version,
            **updates,
        }
    )


def profile(route: schemas._RouteV2) -> selection._FixtureProfileV2:
    raw = SELECTION_FIXTURE.read_bytes()
    assert bytes_digest(raw) == SELECTION_SHA256
    fixture = json.loads(raw)
    projection = project_native_schema_v2(
        rubric(), scope=scope(route), mechanism=route.mechanism
    )
    documentation = next(
        item.source_sha256
        for item in projection.provenance
        if item.kind == "documentation"
    )
    return selection._FixtureProfileV2.model_validate(
        {
            "scope": scope(route),
            "mechanism": route.mechanism,
            "schema_dialect": route.schema_dialect,
            "features": tuple(fixture["features"]),
            "documentation_sha256": documentation,
            "fixture_sha256": SELECTION_SHA256,
        }
    )


def catalog(route: schemas._RouteV2) -> selection._CatalogV2:
    return replace(selection._DEFAULT_CATALOG_V2, profiles=(profile(route),))


def evidence(
    route: schemas._RouteV2, supported: bool | None = True, **updates: object
) -> ScopedProviderEvidence:
    fixture = profile(route)
    return ScopedProviderEvidence.model_validate(
        {
            **scope(route).model_dump(),
            "feature": "native_schema",
            "supported": supported,
            "implemented": True,
            "fixture_tested": True,
            "stage": "fixture_tested",
            "provenance": (
                EvidenceProvenance(
                    kind="documentation",
                    source_sha256=fixture.documentation_sha256,
                    recorded_on=date(2026, 11, 7),
                ),
                EvidenceProvenance(
                    kind="offline_fixture",
                    source_sha256=fixture.fixture_sha256,
                    recorded_on=date(2026, 11, 7),
                ),
            ),
            **updates,
        }
    )


def resolve(
    route: schemas._RouteV2,
    *,
    policy: NativePolicy = "auto",
    supported: bool | None = True,
    strict: bool = False,
) -> TransportDecisionV2:
    return selection._resolve_native_transport_v2(
        rubric(),
        scope=scope(route),
        mechanism=route.mechanism,
        policy=policy,
        strict_capabilities=strict,
        evidence=evidence(route, supported),
        catalog=catalog(route),
    )


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.mechanism)
def test_independent_version_boundary_and_exact_roundtrip(
    route: schemas._RouteV2,
) -> None:
    contract = rubric("explanations_first", explanations=True)
    before = serialize_output_schema(contract)
    projected = project_native_schema_v2(
        contract, scope=scope(route), mechanism=route.mechanism
    )
    assert projected.schema_version == 1 and projected.projection_version == 2
    assert (
        projected.source_contract_version == 2 and projected.implementation_version == 1
    )
    assert projected.schema_dialect == route.schema_dialect
    assert (
        projected.neutral_schema_text.encode()
        == before
        == serialize_output_schema(contract)
    )
    assert projected.local_validation_required is True
    assert all(item.represented for item in projected.native_constraints)
    assert all(not item.represented for item in projected.local_constraints)
    assert all(
        item.enforcement_verified is None
        for item in (*projected.native_constraints, *projected.local_constraints)
    )
    for name in (
        "neutral_schema",
        "native_schema",
        "sdk_config_fragment",
        "format_fragment",
    ):
        assert bytes_digest(getattr(projected, name + "_text").encode()) == getattr(
            projected, name + "_sha256"
        )
    encoded = projected.model_dump_json()
    assert (
        NativeSchemaProjectionV2.model_validate_json(encoded).model_dump_json()
        == encoded
    )
    assert projected == project_native_schema_v2(
        contract, scope=scope(route), mechanism=route.mechanism
    )


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.mechanism)
@pytest.mark.parametrize("policy", ["native", "auto", "text"])
@pytest.mark.parametrize("supported", [True, False, None])
@pytest.mark.parametrize("strict", [False, True])
def test_complete_synthetic_policy_matrix(
    route: schemas._RouteV2, policy: NativePolicy, supported: bool | None, strict: bool
) -> None:
    if (policy == "native" and supported is not True) or (
        policy == "auto" and strict and supported is None
    ):
        with pytest.raises(NativeSelectionErrorV2):
            resolve(route, policy=policy, supported=supported, strict=strict)
        return
    decision = resolve(route, policy=policy, supported=supported, strict=strict)
    native = policy != "text" and supported is True
    assert decision.mechanism == (route.mechanism if native else "text")
    assert decision.requested_mechanism == route.mechanism
    assert decision.schema_version == 1 and decision.decision_version == 2
    assert (
        decision.fixture_tested
        and decision.implemented
        and decision.live_qualified is None
    )
    assert (decision.projection_sha256 is not None) is native
    assert decision.native_support == (
        "supported" if native else "unsupported" if supported is False else "unknown"
    )
    encoded = decision.model_dump_json()
    assert TransportDecisionV2.model_validate_json(encoded).model_dump_json() == encoded
    assert decision == selection._verify_native_transport_decision_v2(
        decision,
        rubric(),
        scope=scope(route),
        evidence=evidence(route, supported),
        catalog=catalog(route),
    )


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.mechanism)
def test_caller_evidence_cannot_authorize_real_or_synthetic_models(
    route: schemas._RouteV2,
) -> None:
    decision = resolve_native_transport_v2(
        rubric(),
        scope=scope(route),
        mechanism=route.mechanism,
        policy="auto",
        evidence=evidence(route),
    )
    assert (
        decision.native_support == "unknown" and decision.model_support == "supported"
    )
    assert (
        not decision.fixture_tested
        and decision.diagnostic.reason == "trusted_fixture_missing"
    )
    for policy, strict in (("native", False), ("auto", True)):
        with pytest.raises(NativeSelectionErrorV2):
            resolve_native_transport_v2(
                rubric(),
                scope=scope(route),
                mechanism=route.mechanism,
                policy=cast(NativePolicy, policy),
                strict_capabilities=strict,
                evidence=evidence(route),
            )
    real_scope = scope(route, model="unqualified-real-model")
    real_evidence = evidence(route).model_copy(update={"model": real_scope.model})
    real = resolve_native_transport_v2(
        rubric(),
        scope=real_scope,
        mechanism=route.mechanism,
        policy="auto",
        evidence=real_evidence,
    )
    assert real.mechanism == "text" and real.native_support == "unknown"


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.mechanism)
def test_explicit_text_never_invokes_projection(
    route: schemas._RouteV2, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("explicit text translated")

    monkeypatch.setattr(selection, "project_native_schema_v2", forbidden)
    decision = resolve_native_transport_v2(
        rubric(), scope=scope(route), mechanism=route.mechanism, policy="text"
    )
    assert decision.reason == "explicit_text" and decision.projection_sha256 is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("sdk_version", "0.0.0"),
        ("api_version", "mismatched"),
        ("sdk_package", "other"),
        ("provider", "other"),
        ("endpoint", "other"),
    ],
)
@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.mechanism)
def test_exact_scope_rejects_mismatches(
    route: schemas._RouteV2, field: str, value: str
) -> None:
    changed = scope(route, **{field: value})
    with pytest.raises(NativeProjectionErrorV2, match="unsupported_scope"):
        project_native_schema_v2(rubric(), scope=changed, mechanism=route.mechanism)
    decision = resolve_native_transport_v2(
        rubric(),
        scope=changed,
        mechanism=route.mechanism,
        policy="auto",
        strict_capabilities=True,
    )
    assert decision.native_support == "unsupported" and decision.mechanism == "text"
    with pytest.raises(NativeSelectionErrorV2):
        resolve_native_transport_v2(
            rubric(), scope=changed, mechanism=route.mechanism, policy="native"
        )


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.mechanism)
def test_unavailable_dialect_and_known_unsupported(route: schemas._RouteV2) -> None:
    decision = resolve_native_transport_v2(
        rubric(),
        scope=scope(route),
        mechanism=route.mechanism,
        policy="auto",
        schema_dialect="unreviewed-dialect",
    )
    assert (
        decision.schema_dialect == "unsupported"
        and decision.native_support == "unsupported"
    )
    decision = resolve_native_transport_v2(
        rubric(),
        scope=scope(route),
        mechanism=route.mechanism,
        policy="auto",
        strict_capabilities=True,
        evidence=evidence(route, False),
    )
    assert decision.diagnostic.reason == "native_known_unsupported"
    assert decision.mechanism == "text"


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.mechanism)
def test_trusted_provenance_and_fixture_facts_required(route: schemas._RouteV2) -> None:
    missing = evidence(
        route,
        provenance=(
            EvidenceProvenance(
                kind="documentation",
                source_sha256="0" * 64,
                recorded_on=date(2026, 11, 7),
            ),
            EvidenceProvenance(
                kind="offline_fixture",
                source_sha256="0" * 64,
                recorded_on=date(2026, 11, 7),
            ),
        ),
    )
    decision = selection._resolve_native_transport_v2(
        rubric(),
        scope=scope(route),
        mechanism=route.mechanism,
        policy="auto",
        evidence=missing,
        catalog=catalog(route),
    )
    assert decision.diagnostic.reason == "trusted_provenance_missing"
    incomplete = evidence(route, fixture_tested=None, stage="implemented")
    decision = selection._resolve_native_transport_v2(
        rubric(),
        scope=scope(route),
        mechanism=route.mechanism,
        policy="auto",
        evidence=incomplete,
        catalog=catalog(route),
    )
    assert decision.diagnostic.reason == "fixture_support_unestablished"
    mismatched = evidence(route, model="different-model")
    with pytest.raises(NativeSelectionErrorV2, match="evidence_scope_mismatch"):
        resolve_native_transport_v2(
            rubric(),
            scope=scope(route),
            mechanism=route.mechanism,
            policy="auto",
            evidence=mismatched,
        )


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.mechanism)
def test_frozen_decision_verification_never_substitutes(
    route: schemas._RouteV2,
) -> None:
    fixed = resolve(route)
    for changed_evidence in (None, evidence(route, False)):
        with pytest.raises(NativeSelectionErrorV2, match="decision_integrity_conflict"):
            selection._verify_native_transport_decision_v2(
                fixed,
                rubric(),
                scope=scope(route),
                evidence=changed_evidence,
                catalog=catalog(route),
            )
    with pytest.raises(NativeSelectionErrorV2):
        verify_native_transport_decision_v2(
            fixed, rubric(), scope=scope(route), evidence=evidence(route)
        )
    with pytest.raises(NativeSelectionErrorV2):
        selection._verify_native_transport_decision_v2(
            fixed,
            pairwise(),
            scope=scope(route),
            evidence=evidence(route),
            catalog=catalog(route),
        )
    with pytest.raises(NativeSelectionErrorV2):
        selection._verify_native_transport_decision_v2(
            fixed,
            rubric(),
            scope=scope(route, model="changed"),
            evidence=evidence(route),
            catalog=catalog(route),
        )


@pytest.mark.parametrize(
    "field",
    [
        "native_schema_text",
        "native_schema_sha256",
        "sdk_config_fragment_text",
        "sdk_config_fragment_sha256",
        "format_fragment_text",
        "format_fragment_sha256",
        "neutral_schema_text",
        "neutral_schema_sha256",
        "projection_sha256",
        "schema_dialect",
        "mechanism",
        "root_properties",
        "member_properties",
        "native_constraints",
        "local_constraints",
        "provenance",
        "source_sha256",
    ],
)
def test_mutated_and_rehashed_projection_is_not_a_new_valid_record(field: str) -> None:
    route = ROUTES[-1]
    projected = project_native_schema_v2(
        rubric(explanations=True), scope=scope(route), mechanism=route.mechanism
    )
    values = projected.model_dump()
    values[field] = (
        []
        if isinstance(values[field], tuple)
        else "0" * 64
        if field.endswith("sha256")
        else "changed"
    )
    values["projection_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        NativeSchemaProjectionV2.model_validate(values)
    values = projected.model_dump(mode="json")
    values["format_fragment_text"] += " "
    values["format_fragment_sha256"] = bytes_digest(
        values["format_fragment_text"].encode()
    )
    values["projection_sha256"] = selection.semantic_digest(
        "native-schema-projection-v2",
        {key: value for key, value in values.items() if key != "projection_sha256"},
    )
    with pytest.raises(ValidationError):
        NativeSchemaProjectionV2.model_validate_json(json.dumps(values))


def test_google_route_is_part_of_frozen_decision_identity() -> None:
    cloud, json_route = ROUTES[3], ROUTES[4]
    fixed = resolve(cloud)
    alternate = resolve(json_route)
    assert fixed.source_schema_sha256 == alternate.source_schema_sha256
    assert fixed.decision_sha256 != alternate.decision_sha256
    values = fixed.model_dump(mode="json")
    values["requested_mechanism"] = json_route.mechanism
    values["decision_sha256"] = selection._decision_digest_v2(
        {key: value for key, value in values.items() if key != "decision_sha256"}
    )
    with pytest.raises(ValidationError):
        TransportDecisionV2.model_validate_json(json.dumps(values))


@pytest.mark.parametrize("model_name", ["projection", "decision", "profile"])
@pytest.mark.parametrize("escaped", [False, True])
def test_duplicate_json_and_last_valid_value_rejected(
    model_name: str, escaped: bool
) -> None:
    route = ROUTES[-1]
    model: NativeModel = (
        project_native_schema_v2(
            rubric(), scope=scope(route), mechanism=route.mechanism
        )
        if model_name == "projection"
        else resolve(route)
        if model_name == "decision"
        else profile(route)
    )
    raw = model.model_dump_json()
    key = '"schema_versio\\u006e"' if escaped else '"schema_version"'
    duplicate = "{" + key + ':"synthetic-secret-canary",' + raw[1:]
    with pytest.raises(ValidationError) as caught:
        type(model).model_validate_json(duplicate)
    assert "synthetic-secret-canary" not in str(caught.value) + caught.value.json()
    nested = raw.replace(
        '"scope":{', '"scope":{"provider":"synthetic-secret-canary",', 1
    )
    with pytest.raises(ValidationError):
        type(model).model_validate_json(nested)


@pytest.mark.parametrize(
    "field,value",
    [
        ("projection_version", True),
        ("implementation_version", 1.0),
        ("local_validation_required", 1),
        ("source_contract_version", "2"),
    ],
)
def test_exact_version_and_validation_tokens(field: str, value: object) -> None:
    projected = project_native_schema_v2(
        rubric(), scope=scope(ROUTES[-1]), mechanism=ROUTES[-1].mechanism
    )
    raw = projected.model_dump(mode="json")
    raw[field] = value
    with pytest.raises(ValidationError):
        NativeSchemaProjectionV2.model_validate_json(json.dumps(raw))


def test_coverage_dimensions_are_independent() -> None:
    represented = NativeConstraintCoverageV2(
        constraint="closed_object",
        path="root",
        represented=True,
        sdk_retained=True,
        documented_support=None,
    )
    assert represented.enforcement_verified is None
    assert represented.documented_support is None
    for update in (
        {"enforcement_verified": True},
        {"represented": False},
        {"represented": 1},
    ):
        with pytest.raises(ValidationError):
            represented.model_copy(update=update)


def test_profiles_cannot_authorize_real_models_or_other_routes() -> None:
    original = profile(ROUTES[-1])
    for updates in (
        {"scope": scope(ROUTES[-1], model="real-model")},
        {"schema_dialect": ROUTES[0].schema_dialect},
        {"features": ("rubric_scores", "rubric_scores")},
        {"profile_version": True},
    ):
        with pytest.raises(ValidationError):
            original.model_copy(update=updates)


def test_budget_failure_precedes_source_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = rubric()
    oversized = copy.copy(contract)
    object.__setattr__(oversized, "traits", contract.traits * 129)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("expanded oversized contract")

    monkeypatch.setattr(schemas, "snapshot_source", forbidden)
    with pytest.raises(NativeProjectionErrorV2) as caught:
        project_native_schema_v2(
            oversized, scope=scope(ROUTES[-1]), mechanism=ROUTES[-1].mechanism
        )
    assert caught.value.diagnostic.limit == "properties"


@pytest.mark.parametrize(
    "text,limit",
    [
        ('"' + "x" * 1_000_000 + '"', "schema_bytes"),
        ("[" * 34 + "null" + "]" * 34, "nesting"),
        (json.dumps({"anyOf": [{}] * 257}), "alternatives"),
    ],
)
def test_implementation_budgets_have_controlled_diagnostics(
    text: str, limit: str
) -> None:
    with pytest.raises(NativeProjectionErrorV2) as caught:
        schemas._fragment_budget_v2(text)
    assert caught.value.diagnostic.limit == limit


def test_unknown_route_and_invalid_selector_input_are_content_free() -> None:
    for operation in (
        lambda: project_native_schema_v2(
            rubric(), scope=scope(ROUTES[-1]), mechanism=cast(Any, "synthetic-secret")
        ),
        lambda: resolve_native_transport_v2(
            rubric(),
            scope=scope(ROUTES[-1]),
            mechanism=ROUTES[-1].mechanism,
            policy=cast(Any, "synthetic-secret"),
        ),
        lambda: resolve_native_transport_v2(
            rubric(),
            scope=scope(ROUTES[-1]),
            mechanism=ROUTES[-1].mechanism,
            policy="auto",
            strict_capabilities=cast(Any, "synthetic-secret"),
        ),
    ):
        with pytest.raises(ValueError) as caught:
            operation()
        assert "synthetic-secret" not in str(caught.value)


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: r.mechanism)
@pytest.mark.parametrize("policy", ["native", "text", "auto"])
def test_verification_never_invokes_resolver(
    route: schemas._RouteV2, policy: NativePolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted = resolve(
        route, policy=policy, supported=None if policy == "auto" else True
    )
    trusted = evidence(route, None if policy == "auto" else True)
    current_catalog = catalog(route)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("verification reselected")

    monkeypatch.setattr(selection, "_resolve_native_transport_v2", forbidden)
    if accepted.mechanism == "text":
        monkeypatch.setattr(selection, "project_native_schema_v2", forbidden)
    assert (
        selection._verify_native_transport_decision_v2(
            accepted,
            rubric(),
            scope=scope(route),
            evidence=trusted,
            catalog=current_catalog,
        )
        == accepted
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"fixture_tested": False},
        {"model_support": "unknown"},
        {"local_validation_required": False},
        {"implemented": False},
        {"implementation_version": None},
        {"evidence_sha256": None},
        {"documentation_sha256": None},
        {"fixture_sha256": None},
        {"projection_sha256": None},
        {"features": []},
        {
            "scope": {
                "provider": "ollama",
                "endpoint": "chat",
                "sdk_package": "ollama",
                "sdk_version": "0.6.3",
                "model_scope": "unresolved",
            }
        },
    ],
)
def test_rehashed_inconsistent_decisions_are_rejected(
    updates: dict[str, object],
) -> None:
    fixed = resolve(ROUTES[-1])
    values = fixed.model_dump(mode="json")
    values.update(updates)
    values["decision_sha256"] = selection._decision_digest_v2(
        {key: value for key, value in values.items() if key != "decision_sha256"}
    )
    with pytest.raises(ValidationError):
        TransportDecisionV2.model_validate_json(json.dumps(values))


def test_verification_checks_support_facts_even_after_rehash() -> None:
    route = ROUTES[-1]
    fixed = resolve(route, policy="text")
    values = fixed.model_dump(mode="json")
    values.update(
        {
            "native_support": "unsupported",
            "diagnostic": {
                "schema_version": 1,
                "reason": "translator_scope_incompatible",
                "native_support": "unsupported",
            },
        }
    )
    values["decision_sha256"] = selection._decision_digest_v2(
        {key: value for key, value in values.items() if key != "decision_sha256"}
    )
    forged = TransportDecisionV2.model_validate_json(json.dumps(values))
    with pytest.raises(NativeSelectionErrorV2):
        selection._verify_native_transport_decision_v2(
            forged,
            rubric(),
            scope=scope(route),
            evidence=evidence(route),
            catalog=catalog(route),
        )


@pytest.mark.parametrize("route", ROUTES[1:5], ids=lambda r: r.mechanism)
def test_conflicting_google_routes_rejected_after_exact_rehash(
    route: schemas._RouteV2,
) -> None:
    projection = project_native_schema_v2(
        rubric(), scope=scope(route), mechanism=route.mechanism
    )
    values = projection.model_dump(mode="json")
    config = json.loads(values["sdk_config_fragment_text"])
    other = (
        "response_json_schema"
        if route.mechanism.endswith(".response_schema")
        else "response_schema"
    )
    config[other] = json.loads(values["native_schema_text"])
    values["sdk_config_fragment_text"] = json.dumps(
        config, ensure_ascii=False, separators=(",", ":")
    )
    values["sdk_config_fragment_sha256"] = bytes_digest(
        values["sdk_config_fragment_text"].encode()
    )
    values["projection_sha256"] = selection.semantic_digest(
        "native-schema-projection-v2",
        {key: value for key, value in values.items() if key != "projection_sha256"},
    )
    with pytest.raises(ValidationError):
        NativeSchemaProjectionV2.model_validate_json(json.dumps(values))


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "enum": ["001", None]},
        {"type": ["string", "null"], "enum": ["001", None]},
        {"type": "string", "enum": [False, "001"]},
        {"type": "string", "enum": []},
        {"type": "string", "enum": ["001"], "oneOf": [{"type": "null"}]},
    ],
)
def test_cloud_schema_rejects_unreviewed_nullable_enum_shapes(
    schema: dict[str, Any],
) -> None:
    from judgmentllm.providers import google_schema

    with pytest.raises(NativeProjectionErrorV2, match="unsupported_schema"):
        google_schema._cloud_schema(schema, reject_empty=True)


def test_cloud_nullable_enum_preserves_exact_nonnull_membership() -> None:
    from judgmentllm.providers import google_schema

    result = google_schema._cloud_schema(
        {"type": "string", "enum": ["002", "001"], "nullable": True}, reject_empty=True
    )
    assert result == {"enum": ["002", "001"], "nullable": True, "type": "STRING"}


def test_verification_rejects_fabricated_rehashed_projection_failure() -> None:
    route = ROUTES[3]
    fixed = resolve_native_transport_v2(
        rubric(), scope=scope(route), mechanism=route.mechanism, policy="auto"
    )
    values = fixed.model_dump(mode="json")
    values.update(
        {
            "native_support": "unsupported",
            "reason": "native_unsupported_text_selected",
            "diagnostic": {
                "schema_version": 1,
                "reason": "projection_incompatible",
                "native_support": "unsupported",
            },
        }
    )
    values["decision_sha256"] = selection._decision_digest_v2(
        {key: value for key, value in values.items() if key != "decision_sha256"}
    )
    forged = TransportDecisionV2.model_validate_json(json.dumps(values))
    with pytest.raises(NativeSelectionErrorV2, match="decision_integrity_conflict"):
        verify_native_transport_decision_v2(forged, rubric(), scope=scope(route))


def test_auto_projection_failure_verifies_only_the_same_failing_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = ROUTES[-1]
    original = rubric()
    too_many = original.model_copy(
        update={
            "traits": tuple(
                original.traits[0].model_copy(update={"trait_id": str(index)})
                for index in range(257)
            )
        }
    )
    fixed = resolve_native_transport_v2(
        too_many, scope=scope(route), mechanism=route.mechanism, policy="auto"
    )
    assert fixed.diagnostic.reason == "projection_incompatible"
    called: list[schemas.NativeMechanismV2] = []
    projector = selection.project_native_schema_v2

    def observe(
        contract: Any,
        *,
        scope: NativeProjectionScopeV1,
        mechanism: schemas.NativeMechanismV2,
    ) -> NativeSchemaProjectionV2:
        called.append(mechanism)
        return projector(contract, scope=scope, mechanism=mechanism)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("verification invoked selection")

    monkeypatch.setattr(selection, "project_native_schema_v2", observe)
    monkeypatch.setattr(selection, "_resolve_native_transport_v2", forbidden)
    assert (
        verify_native_transport_decision_v2(fixed, too_many, scope=scope(route))
        == fixed
    )
    assert called == [route.mechanism]
