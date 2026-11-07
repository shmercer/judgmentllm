"""Private synthetic acceptance catalogs never activate real provider transport."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_auth_cli import exclude_secrets, private_failure

from judgmentllm import native_selection as selection
from judgmentllm.evidence.scope import EvidenceProvenance, ScopedProviderEvidence
from judgmentllm.models import Level, PairTask, Trait
from judgmentllm.native_schema import (
    NativeProjectionError,
    NativeProjectionScopeV1,
    NativeSchemaProjectionV1,
)
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    PairwiseOutputContract,
    RubricOutputContract,
)
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.providers.openai_schema import OPENAI_DOCUMENTATION_SHA256

FIXTURE = Path(__file__).parent / "fixtures/native_selection/synthetic-profile.json"
FIXTURE_SHA256 = "0d3e8a30d432a996adc25f1edceedcfda95e13e3d7353e0901996488e8f1dbf9"


def scope(
    selected_endpoint: str = "responses", /, **updates: object
) -> NativeProjectionScopeV1:
    return NativeProjectionScopeV1.model_validate(
        {
            "provider": "openai",
            "endpoint": selected_endpoint,
            "model": "judgmentllm-synthetic-native-v1",
            "model_scope": "exact",
            "sdk_package": "openai",
            "sdk_version": "3.42.1",
            **updates,
        }
    )


def contract(*, grouped: bool = False) -> RubricOutputContract:
    traits = tuple(
        Trait(
            trait_id=identifier,
            name="Synthetic",
            description="Synthetic.",
            levels=[Level(label="low"), Level(label="high")],
        )
        for identifier in (("01", "02") if grouped else ("01",))
    )
    return RubricOutputContract(traits=traits)


def profile(selected_scope: NativeProjectionScopeV1) -> selection._FixtureProfile:
    raw = FIXTURE.read_bytes()
    assert bytes_digest(raw) == FIXTURE_SHA256
    fixture = json.loads(raw)
    assert fixture["documentation_sha256"] == OPENAI_DOCUMENTATION_SHA256
    assert fixture["acceptance"] == "synthetic_offline_only"
    assert fixture["live_qualified"] is None
    return selection._FixtureProfile.model_validate_json(
        json.dumps(
            {
                "scope": selected_scope.model_dump(mode="json"),
                "mechanism": fixture["mechanisms"][selected_scope.endpoint],
                "schema_dialect": fixture["schema_dialect"],
                "implementation_version": fixture["implementation_version"],
                "features": fixture["features"],
                "documentation_sha256": fixture["documentation_sha256"],
                "fixture_sha256": FIXTURE_SHA256,
            }
        )
    )


def catalog(selected_scope: NativeProjectionScopeV1) -> selection._Catalog:
    return replace(selection._DEFAULT_CATALOG, profiles=(profile(selected_scope),))


def evidence(
    selected_scope: NativeProjectionScopeV1,
    state: bool | None = True,
    /,
    **updates: object,
) -> ScopedProviderEvidence:
    return ScopedProviderEvidence.model_validate(
        {
            **dict(selected_scope),
            "feature": "native_schema",
            "supported": state,
            "implemented": True,
            "fixture_tested": True,
            "stage": "fixture_tested",
            "provenance": (
                EvidenceProvenance(
                    kind="documentation",
                    source_sha256=OPENAI_DOCUMENTATION_SHA256,
                    recorded_on=date(2026, 11, 7),
                ),
                EvidenceProvenance(
                    kind="offline_fixture",
                    source_sha256=FIXTURE_SHA256,
                    recorded_on=date(2026, 11, 7),
                ),
            ),
            **updates,
        }
    )


def resolve(
    *,
    selected_scope: NativeProjectionScopeV1 | None = None,
    selected_contract: AssessmentOutputContract | None = None,
    supported: bool | None = True,
    policy: selection.NativePolicy = "auto",
    strict: bool = False,
) -> selection.TransportDecisionV1:
    selected_scope = selected_scope or scope()
    return selection._resolve_native_transport(
        selected_contract or contract(),
        scope=selected_scope,
        policy=policy,
        strict_capabilities=strict,
        evidence=evidence(selected_scope, supported),
        catalog=catalog(selected_scope),
    )


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("policy", ["native", "auto", "text"])
@pytest.mark.parametrize("supported", [True, False, None])
@pytest.mark.parametrize("strict", [False, True])
def test_complete_policy_matrix(
    endpoint: str, policy: selection.NativePolicy, supported: bool | None, strict: bool
) -> None:
    selected_scope = scope(endpoint)
    if (policy == "native" and supported is not True) or (
        policy == "auto" and strict and supported is None
    ):
        with pytest.raises(selection.NativeSelectionError) as failure:
            resolve(
                selected_scope=selected_scope,
                policy=policy,
                supported=supported,
                strict=strict,
            )
        assert failure.value.diagnostic.native_support == (
            "unsupported" if supported is False else "unknown"
        )
        return
    decision = resolve(
        selected_scope=selected_scope, policy=policy, supported=supported, strict=strict
    )
    native = policy != "text" and supported is True
    assert (decision.mechanism != "text") is native
    assert decision.model_support == (
        "unknown" if supported is None else "supported" if supported else "unsupported"
    )
    assert decision.native_support == (
        "unknown" if policy == "text" and supported is True else decision.model_support
    )
    assert decision.implemented is True and decision.fixture_tested is True
    assert decision.live_qualified is None
    assert decision.local_validation_required is True
    assert (decision.projection_sha256 is not None) is native
    assert decision == selection.TransportDecisionV1.model_validate_json(
        decision.model_dump_json()
    )


def test_caller_claims_cannot_authorize_default_catalog() -> None:
    selected_scope = scope()
    record = evidence(selected_scope)
    decision = selection.resolve_native_transport(
        contract(), scope=selected_scope, policy="auto", evidence=record
    )
    assert decision.mechanism == "text"
    assert (
        decision.native_support == "unknown" and decision.model_support == "supported"
    )
    assert decision.implemented and not decision.fixture_tested
    assert decision.diagnostic.reason == "trusted_fixture_missing"
    for policy in ("native", "auto"):
        with pytest.raises(selection.NativeSelectionError):
            selection.resolve_native_transport(
                contract(),
                scope=selected_scope,
                policy=policy,
                strict_capabilities=True,
                evidence=record,
            )
    assert selection._DEFAULT_CATALOG.profiles == ()


@pytest.mark.parametrize("strict", [False, True])
def test_known_absent_translator_preserves_positive_model_claim(strict: bool) -> None:
    selected_scope = scope()
    decision = selection._resolve_native_transport(
        contract(),
        scope=selected_scope,
        policy="auto",
        strict_capabilities=strict,
        evidence=evidence(selected_scope),
        catalog=selection._Catalog(()),
    )
    assert decision.mechanism == "text" and decision.native_support == "unsupported"
    assert decision.model_support == "supported"
    assert not decision.implemented and not decision.fixture_tested
    assert decision.diagnostic.reason == "translator_unavailable"


@pytest.mark.parametrize(
    "updates,reason",
    [
        (
            {
                "provenance": (
                    EvidenceProvenance(
                        kind="documentation",
                        source_sha256=OPENAI_DOCUMENTATION_SHA256,
                        recorded_on=date(2026, 11, 7),
                    ),
                ),
                "fixture_tested": None,
                "implemented": None,
                "stage": "discovered",
            },
            "trusted_provenance_missing",
        ),
        (
            {"fixture_tested": None, "stage": "implemented"},
            "fixture_support_unestablished",
        ),
        (
            {
                "provenance": (
                    EvidenceProvenance(
                        kind="documentation",
                        source_sha256="a" * 64,
                        recorded_on=date(2026, 11, 7),
                    ),
                    EvidenceProvenance(
                        kind="offline_fixture",
                        source_sha256=FIXTURE_SHA256,
                        recorded_on=date(2026, 11, 7),
                    ),
                )
            },
            "trusted_provenance_missing",
        ),
    ],
)
def test_documentation_or_untrusted_fixtures_do_not_select_native(
    updates: dict[str, object], reason: str
) -> None:
    selected_scope = scope()
    decision = selection._resolve_native_transport(
        contract(),
        scope=selected_scope,
        policy="auto",
        evidence=evidence(selected_scope, **updates),
        catalog=catalog(selected_scope),
    )
    assert decision.native_support == "unknown" and not decision.fixture_tested
    assert decision.diagnostic.reason == reason


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "other"},
        {"endpoint": "chat.completions"},
        {"model": "other"},
        {"sdk_version": "3.42.2"},
        {"api_version": "v2"},
        {"feature": "usage"},
    ],
)
@pytest.mark.parametrize("policy", ["text", "auto", "native"])
def test_evidence_scope_must_match_every_relevant_dimension(
    changes: dict[str, object], policy: selection.NativePolicy
) -> None:
    selected_scope = scope()
    record = evidence(selected_scope, **changes)
    with pytest.raises(selection.NativeSelectionError) as failure:
        selection._resolve_native_transport(
            contract(),
            scope=selected_scope,
            policy=policy,
            evidence=record,
            catalog=catalog(selected_scope),
        )
    assert failure.value.reason == "evidence_scope_mismatch"


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "other"},
        {"endpoint": "unknown"},
        {"sdk_package": "anthropic"},
        {"sdk_version": "3.42.2"},
        {"sdk_package": None, "sdk_version": None},
        {"api_version": "v2"},
    ],
)
def test_registered_implementation_rejects_incompatible_scope(
    changes: dict[str, object],
) -> None:
    selected_scope = scope(**changes)
    decision = selection.resolve_native_transport(
        contract(),
        scope=selected_scope,
        policy="auto",
        strict_capabilities=True,
        evidence=evidence(selected_scope),
    )
    assert decision.native_support == "unsupported" and decision.mechanism == "text"
    assert decision.model_support == "supported"


def test_unresolved_and_different_model_scope_are_not_accepted() -> None:
    for selected_scope in (
        scope(model=None, model_scope="unresolved"),
        scope(model="real-model-name"),
    ):
        decision = selection.resolve_native_transport(
            contract(),
            scope=selected_scope,
            policy="auto",
            evidence=evidence(selected_scope),
        )
        assert decision.native_support == "unknown"
        with pytest.raises(ValidationError):
            profile(selected_scope)


def test_dialect_mismatch_has_fixed_safe_representation() -> None:
    decision = selection.resolve_native_transport(
        contract(),
        scope=scope(),
        policy="auto",
        strict_capabilities=True,
        evidence=evidence(scope()),
        schema_dialect="synthetic-unrecognized-dialect",
    )
    assert decision.native_support == "unsupported"
    assert decision.schema_dialect == "unsupported"
    assert "synthetic-unrecognized-dialect" not in decision.model_dump_json()


def test_implementation_features_and_profile_features_are_separate() -> None:
    selected_scope = scope()
    implementation = replace(
        selection._DEFAULT_CATALOG.implementations[0], features=("rubric_scores",)
    )
    selected_catalog = replace(
        catalog(selected_scope), implementations=(implementation,)
    )
    decision = selection._resolve_native_transport(
        contract(grouped=True),
        scope=selected_scope,
        policy="auto",
        strict_capabilities=True,
        evidence=evidence(selected_scope),
        catalog=selected_catalog,
    )
    assert decision.native_support == "unsupported"
    assert decision.diagnostic.reason == "projection_features_incompatible"
    limited_profile = profile(selected_scope).model_copy(
        update={"features": ("rubric_scores",)}
    )
    selected_catalog = replace(catalog(selected_scope), profiles=(limited_profile,))
    decision = selection._resolve_native_transport(
        contract(grouped=True),
        scope=selected_scope,
        policy="auto",
        evidence=evidence(selected_scope),
        catalog=selected_catalog,
    )
    assert decision.native_support == "unknown"
    assert decision.diagnostic.reason == "trusted_fixture_missing"


def test_projection_incompatibility_selects_text_without_truncation() -> None:
    def incompatible(
        contract: AssessmentOutputContract, *, scope: NativeProjectionScopeV1
    ) -> NativeSchemaProjectionV1:
        raise NativeProjectionError("schema_limit_exceeded", limit="properties")

    selected_scope = scope()
    implementation = replace(
        selection._DEFAULT_CATALOG.implementations[0], translate=incompatible
    )
    selected_catalog = replace(
        catalog(selected_scope), implementations=(implementation,)
    )
    original = contract(grouped=True)
    snapshot = original.model_dump_json()
    decision = selection._resolve_native_transport(
        original,
        scope=selected_scope,
        policy="auto",
        strict_capabilities=True,
        evidence=evidence(selected_scope),
        catalog=selected_catalog,
    )
    assert decision.native_support == "unsupported"
    assert decision.diagnostic.reason == "projection_incompatible"
    assert original.model_dump_json() == snapshot


def test_decisions_are_deterministic_and_semantics_change_identity() -> None:
    first = resolve()
    assert first == resolve()
    assert first.model_dump_json() == resolve().model_dump_json()
    assert (
        first.decision_sha256
        != resolve(selected_scope=scope("chat.completions")).decision_sha256
    )
    assert (
        first.decision_sha256
        != resolve(selected_contract=contract(grouped=True)).decision_sha256
    )
    for order in ("together", "scores_first", "explanations_first"):
        expanded = contract().model_copy(
            update={"explanations": True, "output_order": order}
        )
        assert (
            first.decision_sha256 != resolve(selected_contract=expanded).decision_sha256
        )
    changed = contract()
    changed.traits[0].levels[0] = Level(label="changed")
    assert first.decision_sha256 != resolve(selected_contract=changed).decision_sha256
    assert first.decision_sha256 != resolve(policy="text").decision_sha256
    assert first.decision_sha256 != resolve(strict=True).decision_sha256


def test_pairwise_identity_preserves_orientation_beyond_schema() -> None:
    pair = PairTask(
        pair_id="001",
        pair_uid="001-r",
        left_item_id="001",
        right_item_id="002",
        trait_id="01",
        presented_order=["001", "002"],
    )
    original = PairwiseOutputContract(pair=pair)
    reversed_pair = pair.model_copy(
        update={
            "left_item_id": "002",
            "right_item_id": "001",
            "original_order": ["002", "001"],
        }
    )
    reversed_contract = PairwiseOutputContract(pair=reversed_pair)
    first = resolve(selected_contract=original)
    reversed_decision = resolve(selected_contract=reversed_contract)
    assert first.source_schema_sha256 == reversed_decision.source_schema_sha256
    assert first.source_sha256 != reversed_decision.source_sha256
    assert first.decision_sha256 != reversed_decision.decision_sha256
    assert (
        first.decision_sha256
        != resolve(
            selected_contract=original.model_copy(update={"allow_ties": True})
        ).decision_sha256
    )
    assert (
        first.decision_sha256
        != resolve(
            selected_contract=original.model_copy(update={"explanations": True})
        ).decision_sha256
    )


def test_verify_never_falls_back_after_decision() -> None:
    selected_scope = scope()
    decision = resolve()
    check = selection._verify_native_transport_decision(
        decision,
        contract(),
        scope=selected_scope,
        evidence=evidence(selected_scope),
        catalog=catalog(selected_scope),
    )
    assert check == decision
    for record in (evidence(selected_scope, False), evidence(selected_scope, None)):
        with pytest.raises(selection.NativeSelectionError) as failure:
            selection._verify_native_transport_decision(
                decision,
                contract(),
                scope=selected_scope,
                evidence=record,
                catalog=catalog(selected_scope),
            )
        assert failure.value.reason == "decision_integrity_conflict"
    with pytest.raises(selection.NativeSelectionError):
        selection.verify_native_transport_decision(
            decision,
            contract(),
            scope=selected_scope,
            evidence=evidence(selected_scope),
        )
    assert decision.mechanism == "openai.responses.text.format"
    text = selection.resolve_native_transport(
        contract(), scope=selected_scope, policy="text"
    )
    assert (
        selection.verify_native_transport_decision(
            text, contract(), scope=selected_scope
        )
        == text
    )
    with pytest.raises(selection.NativeSelectionError):
        selection.verify_native_transport_decision(
            text, contract(grouped=True), scope=selected_scope
        )


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_versions_are_exact_integers(version: object) -> None:
    decision = resolve()
    for field in ("schema_version", "implementation_version"):
        with pytest.raises(ValidationError):
            decision.model_copy(update={field: version})
    with pytest.raises(ValidationError):
        profile(scope()).model_copy(update={"implementation_version": version})


@pytest.mark.parametrize(
    "changes",
    [
        {"mechanism": "openai.chat.response_format"},
        {"schema_dialect": "unsupported"},
        {"fixture_tested": False},
        {"model_support": "unknown"},
        {"local_validation_required": 1},
        {"documentation_sha256": None},
        {"live_qualified": True},
        {"unexpected": True},
    ],
)
def test_self_validation_rejects_rehashed_inconsistent_records(
    changes: dict[str, object],
) -> None:
    values: dict[str, Any] = resolve().model_dump(
        mode="json", exclude={"decision_sha256"}
    )
    values.update(changes)
    values["decision_sha256"] = selection._decision_digest(values)
    with pytest.raises(ValidationError):
        selection.TransportDecisionV1.model_validate_json(json.dumps(values))


def test_digest_tampering_and_frozen_copies_cannot_change_choice() -> None:
    decision = resolve()
    with pytest.raises(ValidationError):
        decision.model_copy(update={"decision_sha256": "a" * 64})
    with pytest.raises(ValidationError):
        decision.model_copy(update={"mechanism": "text"})
    with pytest.raises(ValidationError):
        mutated: dict[str, Any] = decision.model_dump()
        mutated["decision_sha256"] = "a" * 64
        decision.model_construct(**mutated)
    with pytest.raises(ValidationError):
        decision.mechanism = "text"
    with pytest.raises(FrozenInstanceError):
        selection._DEFAULT_CATALOG.profiles = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    "policy,strict,dialect",
    [("bad", False, "x"), ("auto", 1, "x"), ("auto", False, None)],
)
def test_invalid_policy_input_is_content_free(
    policy: selection.NativePolicy, strict: bool, dialect: str
) -> None:
    with pytest.raises(selection.NativeSelectionError) as failure:
        selection.resolve_native_transport(
            contract(),
            scope=scope(),
            policy=policy,
            strict_capabilities=strict,
            schema_dialect=dialect,
        )
    assert failure.value.reason == "invalid_selection_input"


def test_mutated_source_and_evidence_fail_before_decision() -> None:
    invalid = contract()
    invalid.traits[0].levels[0].label = ""
    with pytest.raises(selection.NativeSelectionError):
        selection.resolve_native_transport(invalid, scope=scope(), policy="text")
    selected_scope = scope()
    forged = evidence(selected_scope)
    object.__setattr__(forged, "feature", "unexpected")
    with pytest.raises(selection.NativeSelectionError):
        selection.resolve_native_transport(
            contract(), scope=selected_scope, policy="auto", evidence=forged
        )


@pytest.mark.parametrize("policy", ["native", "auto"])
def test_native_candidates_reuse_projector_hashes_before_duplicate_expansion(
    monkeypatch: pytest.MonkeyPatch,
    policy: selection.NativePolicy,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("selection duplicated canonical expansion")

    monkeypatch.setattr(selection, "serialize_output_schema", forbidden)
    monkeypatch.setattr(selection, "snapshot_source", forbidden)
    decision = resolve(policy=policy)
    assert decision.mechanism == "openai.responses.text.format"


def test_explicit_text_never_calls_native_projector() -> None:
    def forbidden(
        contract: AssessmentOutputContract, *, scope: NativeProjectionScopeV1
    ) -> NativeSchemaProjectionV1:
        raise AssertionError("explicit text invoked a native translator")

    selected_scope = scope()
    implementation = replace(
        selection._DEFAULT_CATALOG.implementations[0], translate=forbidden
    )
    selected_catalog = replace(
        catalog(selected_scope), implementations=(implementation,)
    )
    decision = selection._resolve_native_transport(
        contract(),
        scope=selected_scope,
        policy="text",
        strict_capabilities=True,
        evidence=evidence(selected_scope),
        catalog=selected_catalog,
    )
    assert decision.mechanism == "text" and decision.native_support == "unknown"
    assert decision.model_support == "supported" and decision.fixture_tested


def test_native_limit_failure_precedes_full_source_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def incompatible(
        contract: AssessmentOutputContract, *, scope: NativeProjectionScopeV1
    ) -> NativeSchemaProjectionV1:
        raise NativeProjectionError("schema_limit_exceeded", limit="properties")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("source expanded after native limit rejection")

    selected_scope = scope()
    implementation = replace(
        selection._DEFAULT_CATALOG.implementations[0], translate=incompatible
    )
    monkeypatch.setattr(selection, "serialize_output_schema", forbidden)
    monkeypatch.setattr(selection, "snapshot_source", forbidden)
    with pytest.raises(selection.NativeSelectionError) as failure:
        selection._resolve_native_transport(
            contract(),
            scope=selected_scope,
            policy="native",
            evidence=evidence(selected_scope),
            catalog=replace(catalog(selected_scope), implementations=(implementation,)),
        )
    assert failure.value.reason == "projection_incompatible"


def test_registered_projection_identity_must_match_its_mechanism() -> None:
    selected_scope = scope()
    implementation = replace(
        selection._DEFAULT_CATALOG.implementations[0],
        mechanism="openai.chat.response_format",
    )
    decision = selection._resolve_native_transport(
        contract(),
        scope=selected_scope,
        policy="auto",
        strict_capabilities=True,
        evidence=evidence(selected_scope),
        catalog=replace(catalog(selected_scope), implementations=(implementation,)),
    )
    assert decision.native_support == "unsupported"
    assert decision.diagnostic.reason == "projection_incompatible"


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
def test_json_versions_reject_tokens_before_literal_coercion(version: object) -> None:
    decision = resolve()
    for field in ("schema_version", "implementation_version"):
        raw = decision.model_dump(mode="json")
        raw[field] = version
        with pytest.raises(ValidationError):
            selection.TransportDecisionV1.model_validate_json(json.dumps(raw))
    raw_profile = profile(scope()).model_dump(mode="json")
    raw_profile["implementation_version"] = version
    with pytest.raises(ValidationError):
        selection._FixtureProfile.model_validate_json(json.dumps(raw_profile))


def test_json_mandatory_validation_flag_rejects_integer_alias() -> None:
    values = resolve().model_dump(mode="json", exclude={"decision_sha256"})
    values["local_validation_required"] = 1
    values["decision_sha256"] = selection._decision_digest(values)
    with pytest.raises(ValidationError):
        selection.TransportDecisionV1.model_validate_json(json.dumps(values))


@private_failure
def test_mutated_output_order_cannot_escape_into_diagnostics() -> None:
    secret = "synthetic-private-layout-canary"
    invalid = contract().model_copy(
        update={"explanations": True, "output_order": secret}
    )
    for policy in ("text", "auto", "native"):
        with pytest.raises(selection.NativeSelectionError) as failure:
            selection.resolve_native_transport(invalid, scope=scope(), policy=policy)
        exclude_secrets(
            str(failure.value)
            + repr(failure.value)
            + failure.value.diagnostic.model_dump_json(),
            (secret,),
        )


def _duplicated_member_json(
    values: dict[str, Any], path: tuple[str, ...], first: object = "discarded"
) -> str:
    """Keep the committed value last so ordinary JSON parsing loses the attack."""
    target = path[0]
    members: list[str] = []
    for key, value in values.items():
        name = json.dumps(key)
        if key == target and len(path) == 1:
            members.append(name + ":" + json.dumps(first))
        if key == target and len(path) > 1:
            encoded = _duplicated_member_json(value, path[1:], first)
        else:
            encoded = json.dumps(value)
        members.append(name + ":" + encoded)
    return "{" + ",".join(members) + "}"


@pytest.mark.parametrize(
    "path",
    [
        ("schema_version",),
        ("policy",),
        ("strict_capabilities",),
        ("mechanism",),
        ("schema_dialect",),
        ("native_support",),
        ("model_support",),
        ("source_schema_sha256",),
        ("source_sha256",),
        ("implementation_version",),
        ("documentation_sha256",),
        ("fixture_sha256",),
        ("projection_sha256",),
        ("decision_sha256",),
        ("local_validation_required",),
        ("scope", "schema_version"),
        ("scope", "provider"),
        ("scope", "endpoint"),
        ("scope", "model"),
        ("scope", "model_scope"),
        ("scope", "sdk_package"),
        ("scope", "sdk_version"),
        ("scope", "api_version"),
        ("diagnostic", "reason"),
        ("diagnostic", "native_support"),
    ],
)
def test_decision_rejects_duplicate_json_members_before_last_value_wins(
    path: tuple[str, ...],
) -> None:
    values = resolve().model_dump(mode="json")
    raw = _duplicated_member_json(values, path)
    assert json.loads(raw) == values
    with pytest.raises(ValidationError):
        selection.TransportDecisionV1.model_validate_json(raw)


@pytest.mark.parametrize(
    "path",
    [
        ("schema_version",),
        ("mechanism",),
        ("schema_dialect",),
        ("implementation_version",),
        ("features",),
        ("documentation_sha256",),
        ("fixture_sha256",),
        ("scope", "schema_version"),
        ("scope", "provider"),
        ("scope", "endpoint"),
        ("scope", "model"),
        ("scope", "model_scope"),
        ("scope", "sdk_package"),
        ("scope", "sdk_version"),
        ("scope", "api_version"),
    ],
)
def test_trusted_fixture_profile_rejects_duplicate_json_members(
    path: tuple[str, ...],
) -> None:
    values = profile(scope()).model_dump(mode="json")
    raw = _duplicated_member_json(values, path)
    assert json.loads(raw) == values
    with pytest.raises(ValidationError):
        selection._FixtureProfile.model_validate_json(raw)


@pytest.mark.parametrize("kind", ["decision", "profile"])
@pytest.mark.parametrize("encoding", ["str", "bytes", "bytearray"])
def test_duplicate_safe_json_keeps_valid_selection_identity_and_input_types(
    kind: str,
    encoding: str,
) -> None:
    record = resolve() if kind == "decision" else profile(scope())
    raw = record.model_dump_json()
    valid = (
        raw
        if encoding == "str"
        else raw.encode("utf-8")
        if encoding == "bytes"
        else bytearray(raw, "utf-8")
    )
    assert type(record).model_validate_json(valid) == record
    duplicated = _duplicated_member_json(
        record.model_dump(mode="json"), ("scope", "model")
    )
    invalid = (
        duplicated
        if encoding == "str"
        else duplicated.encode("utf-8")
        if encoding == "bytes"
        else bytearray(duplicated, "utf-8")
    )
    with pytest.raises(ValidationError):
        type(record).model_validate_json(invalid)


@pytest.mark.parametrize("kind", ["decision", "profile"])
@private_failure
def test_duplicate_json_errors_never_echo_secret_keys_or_values(kind: str) -> None:
    secret = "synthetic-private-duplicate-json-canary"
    record = resolve() if kind == "decision" else profile(scope())
    values = record.model_dump(mode="json")
    invalid = _duplicated_member_json(values, ("scope", "model"), secret)
    unknown_key = "unknown-" + secret
    unknown = dict(values)
    unknown[unknown_key] = secret
    for raw in (invalid, _duplicated_member_json(unknown, (unknown_key,), secret)):
        with pytest.raises(ValidationError) as failure:
            type(record).model_validate_json(raw)
        error = failure.value
        exclude_secrets(
            str(error) + repr(error) + repr(error.errors()) + error.json(), (secret,)
        )
