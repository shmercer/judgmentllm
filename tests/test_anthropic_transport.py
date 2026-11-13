"""Offline production byte commitments without synthetic execution authority."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import os
import socket
from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from anthropic_fixture import case, wire_for
from transport_contract import (
    assert_frozen_reconstruction_and_subset_lineage,
    assert_probe_selection_does_not_claim_serving_model_support,
    assert_protected_templates_limits_and_mutation_guard,
)

from judgmentllm.anthropic_registry import _QualifiedProfile
from judgmentllm.anthropic_transport import (
    AnthropicProductionContext,
    OutputCommitmentV5,
    PreparedRequestV5,
    TransportCommitmentV3,
    _qualification_context,
    build_output_commitment_v5,
    prepare_production_request,
    production_context,
    production_context_digest,
    production_context_for_identity,
)
from judgmentllm.config import (
    GenerationSpec,
)
from judgmentllm.evidence.scope import EvidenceProvenance
from judgmentllm.models import (
    Exemplar,
)
from judgmentllm.persistence.hashing import (
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.service_tiers import ServiceQualificationScope
from judgmentllm.transport import TransportIntegrityError, physical_identity


@pytest.fixture(autouse=True)
def external_network_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    original = socket.socket.connect
    original_dns = socket.getaddrinfo

    def connect(current: socket.socket, address: Any) -> Any:
        if current.family in {socket.AF_INET, socket.AF_INET6} and address[0] not in {
            "127.0.0.1",
            "::1",
        }:
            raise AssertionError("External network blocked offline")
        return original(current, address)

    def dns(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError("External DNS blocked offline")
        return original_dns(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", dns)


def test_omitted_settings_and_candidate_final_binding(tmp_path: Path) -> None:
    wire, spec, dep, candidate = wire_for(tmp_path, policy="native", probe=True)
    final = _qualification_context(spec, source_commit="a" * 40, plan_sha256="b" * 64)
    final_wire = prepare_production_request(
        spec, dep, wire.transport_plan.identity, final
    )
    assert final_wire.payload == wire.payload
    assert final_wire.transport_commitment != wire.transport_commitment
    assert production_context_digest(final) != production_context_digest(candidate)
    assert (
        b"temperature" not in wire.payload and b"max_output_tokens" not in wire.payload
    )
    assert b"top_p" not in wire.payload
    assert (
        repr(candidate) == "AnthropicProductionContext(<scoped production authority>)"
    )
    assert not candidate.trusted_synthetic


def test_unqualified_native_and_strict_auto_cannot_execute(tmp_path: Path) -> None:
    for policy, strict in (("native", False), ("auto", True)):
        with pytest.raises((TransportIntegrityError, ValueError)):
            wire_for(tmp_path, policy=policy, strict=strict)
    wire, _, _, _ = wire_for(tmp_path, policy="auto")
    assert wire.output_commitment.selection.mechanism == "text"
    assert wire.output_commitment.selection.native_support == "unknown"


def test_frozen_reconstruction_and_subset_lineage(tmp_path: Path) -> None:
    assert_frozen_reconstruction_and_subset_lineage(
        tmp_path, wire_for, prepare_production_request
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("payload", b"{}"),
        ("rendered_text", "changed"),
        ("model", "changed"),
        ("task_ids", ("changed",)),
        ("dependency_sha256", "f" * 64),
    ],
)
def test_current_prepared_objects_revalidate_without_content_leaks(
    tmp_path: Path, field: str, value: Any
) -> None:
    wire, _, _, _ = wire_for(tmp_path)
    with pytest.raises(ValueError) as exc:
        PreparedRequestV5.model_validate({**dict(wire), field: value})
    assert "seed grows" not in str(exc.value)
    assert "low 雪" not in str(exc.value)


def test_receipt_round_trip_duplicate_ingress_and_versions(tmp_path: Path) -> None:
    wire, _, _, _ = wire_for(tmp_path)
    receipt = wire.transport_commitment
    assert (
        TransportCommitmentV3.model_validate_json(canonical_bytes(receipt)) == receipt
    )
    for changed in (
        {"transport_commitment_version": 3.0},
        {"store": 0},
        {"commitment_sha256": "a" * 64},
        {"payload_sha256": "b" * 64},
    ):
        with pytest.raises(ValueError):
            TransportCommitmentV3.model_validate({**dict(receipt), **changed})
    raw = canonical_bytes(receipt)
    with pytest.raises(ValueError):
        TransportCommitmentV3.model_validate_json(
            b'{"transport_commitment_version":3,' + raw[1:]
        )
    changed = receipt.model_dump(mode="json", exclude={"commitment_sha256"})
    changed["payload_sha256"] = "b" * 64
    changed["commitment_sha256"] = semantic_digest(
        "anthropic-transport-commitment-v3", changed
    )
    with pytest.raises(ValueError):
        PreparedRequestV5.model_validate(
            {
                **dict(wire),
                "transport_commitment": TransportCommitmentV3.model_validate_json(
                    canonical_bytes(changed)
                ),
            }
        )


def test_credential_presence_does_not_authorize_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-present-key")
    with pytest.raises((ValueError, TransportIntegrityError)):
        wire_for(tmp_path, policy="native")
    context = production_context_for_identity(
        "anthropic", "messages", "offline-qualified-model"
    )
    assert context == production_context(case(tmp_path)[0])
    with pytest.raises(TransportIntegrityError):
        production_context_for_identity(
            "anthropic", "messages", "judgmentllm-synthetic-native-v1"
        )
    with pytest.raises(TransportIntegrityError):
        production_context_digest(replace(context, kind=cast(Any, "synthetic")))
    with pytest.raises(TransportIntegrityError):
        production_context_digest(replace(context, qualification_plan_sha256="f" * 64))


def test_controlled_registry_exact_features_and_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import anthropic_registry

    spec, dep, _ = case(tmp_path, policy="native")
    context = production_context(spec)
    profile = _QualifiedProfile(
        qualification_date=date(2026, 11, 7),
        source_commit="a" * 40,
        support_status="supported",
        request_acceptance="accepted",
        assessment_behavior="verified",
        native_schema_behavior="verified",
        provenance=tuple(
            EvidenceProvenance(
                kind=cast(Any, kind),
                source_sha256=digest * 64,
                recorded_on=date(2026, 11, 7),
            )
            for kind, digest in (
                ("documentation", "a"),
                ("offline_fixture", "b"),
                ("independent_live", "c"),
            )
        ),
        scope=context.scope,
        service_scopes=(
            ServiceQualificationScope(
                account_scope="synthetic-account",
                tier="standard",
                features=(
                    "rubric_scores",
                    "rubric_grouped",
                    "assessment_presentation_v2",
                ),
            ),
        ),
        supported_features=(
            "rubric_scores",
            "rubric_grouped",
            "assessment_presentation_v2",
        ),
        documentation_sha256="a" * 64,
        offline_fixture_sha256="b" * 64,
        independent_live_sha256="c" * 64,
    )
    monkeypatch.setattr(anthropic_registry, "_QUALIFIED_PROFILES", (profile,))
    output = build_output_commitment_v5(spec, dep.request, context)
    assert output.selection.kind == "native"
    assert output.selection.native_support == "supported"
    assert OutputCommitmentV5.model_validate_json(canonical_bytes(output)) == output
    changed = spec.model_copy(update={"model": "another-exact-model"})
    with pytest.raises(ValueError):
        build_output_commitment_v5(changed, dep.request, production_context(changed))
    monkeypatch.setattr(
        anthropic_registry,
        "_QUALIFIED_PROFILES",
        (
            profile.model_copy(
                update={
                    "supported_features": (),
                    "unsupported_features": ("rubric_scores",),
                }
            ),
        ),
    )
    with pytest.raises(ValueError):
        build_output_commitment_v5(spec, dep.request, context)


def test_protected_templates_limits_and_mutation_guard(tmp_path: Path) -> None:
    assert_protected_templates_limits_and_mutation_guard(
        tmp_path, wire_for, prepare_production_request
    )


def test_scope_generation_and_parent_changes_fail_closed(tmp_path: Path) -> None:
    wire, spec, dep, context = wire_for(tmp_path)
    changed_context = replace(
        context, scope=context.scope.model_copy(update={"sdk_version": "2.3.1"})
    )
    with pytest.raises(TransportIntegrityError):
        production_context_digest(changed_context)
    changed = spec.model_copy(
        update={"generation": GenerationSpec(max_output_tokens=10)}
    )
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            changed,
            dep,
            wire.transport_plan.identity,
            context,
            frozen=wire.transport_commitment,
        )
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec,
            dep,
            physical_identity("another-run", dep.request.request_uid, "attempt-002"),
            context,
            parent=wire.transport_commitment,
        )
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec,
            dep,
            wire.transport_plan.identity,
            context,
            parent=wire.transport_commitment,
        )
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec,
            dep,
            physical_identity("run-001", "another-request", "attempt-002"),
            context,
        )


def test_pure_preparation_never_discovers_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, dep, _ = case(tmp_path)
    context = production_context(spec)
    original_getitem = type(os.environ).__getitem__

    def environment_getitem(environment: Any, key: Any) -> Any:
        if os.fsdecode(key).startswith(("ANTHROPIC_", "AWS_", "GOOGLE_", "GEMINI_")):
            raise AssertionError("Credential discovery forbidden during preparation")
        return original_getitem(environment, key)

    with monkeypatch.context() as restricted:
        restricted.setattr(type(os.environ), "__getitem__", environment_getitem)
        wire = prepare_production_request(
            spec,
            dep,
            physical_identity("run-001", dep.request.request_uid, "attempt-001"),
            context,
        )
    receipt = canonical_bytes(wire.transport_commitment)
    assert b"seed grows" not in receipt and "low 雪" not in receipt.decode()
    assert "seed grows" not in repr(wire)
    assert "low 雪" not in repr(wire.output_commitment)


def test_output_ingress_sanitizes_invalid_private_configuration(tmp_path: Path) -> None:
    spec, dep, _ = case(tmp_path)
    context = production_context(spec)
    changed = spec.model_copy(
        update={"private-canary-config-field": "private-canary-value"}
    )
    with pytest.raises(ValueError) as exc:
        build_output_commitment_v5(changed, dep.request, context)
    assert "private-canary" not in str(exc.value)


def _reviewed_profile(
    context: AnthropicProductionContext, **changes: Any
) -> _QualifiedProfile:
    fields: dict[str, Any] = {
        "scope": context.scope,
        "service_scopes": (
            ServiceQualificationScope(
                account_scope="synthetic-account",
                tier="standard",
                features=(
                    "rubric_scores",
                    "rubric_grouped",
                    "assessment_presentation_v2",
                ),
            ),
        ),
        "qualification_date": date(2026, 11, 7),
        "source_commit": "a" * 40,
        "support_status": "supported",
        "request_acceptance": "accepted",
        "assessment_behavior": "verified",
        "native_schema_behavior": "verified",
        "supported_features": (
            "rubric_scores",
            "rubric_grouped",
            "assessment_presentation_v2",
        ),
        "documentation_sha256": "a" * 64,
        "offline_fixture_sha256": "b" * 64,
        "independent_live_sha256": "c" * 64,
        "provenance": tuple(
            EvidenceProvenance(
                kind=cast(Any, kind),
                source_sha256=digest * 64,
                recorded_on=date(2026, 11, 7),
            )
            for kind, digest in (
                ("documentation", "a"),
                ("offline_fixture", "b"),
                ("independent_live", "c"),
            )
        ),
    }
    return _QualifiedProfile.model_validate(fields | changes)


@pytest.mark.parametrize(
    "change",
    [
        {"translator_version": True},
        {"source_commit": "private-config-canary"},
        {"request_acceptance": "unknown"},
        {"assessment_behavior": "unknown"},
        {"native_schema_behavior": "unknown"},
        {"support_status": "unknown"},
        {"provenance": ()},
        {"unsupported_features": ("rubric_scores",)},
        {"supported_features": ("rubric_scores", "rubric_scores")},
        {"api_scope": "private-api-scope-canary"},
    ],
)
def test_reviewed_profiles_reject_incomplete_or_conflicting_qualification(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    context = production_context(case(tmp_path)[0])
    with pytest.raises(ValueError) as exc:
        _reviewed_profile(context, **change)
    assert "private-" not in str(exc.value)


def test_known_unsupported_auto_and_changed_registry_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import anthropic_registry

    spec, dep, _ = case(tmp_path, policy="auto")
    spec.strict_capabilities = True
    context = production_context(spec)
    unsupported = _reviewed_profile(
        context,
        support_status="unsupported",
        supported_features=(),
        unsupported_features=("rubric_scores",),
        native_schema_behavior="unsupported",
    )
    monkeypatch.setattr(anthropic_registry, "_QUALIFIED_PROFILES", (unsupported,))
    output = build_output_commitment_v5(spec, dep.request, context)
    assert output.selection.mechanism == "text"
    assert output.selection.native_support == "unsupported"
    supported = _reviewed_profile(context)
    monkeypatch.setattr(anthropic_registry, "_QUALIFIED_PROFILES", (supported,))
    output = build_output_commitment_v5(spec, dep.request, context)
    assert output.selection.mechanism != "text"
    wire = prepare_production_request(
        spec,
        dep,
        physical_identity("run-001", dep.request.request_uid, "attempt-001"),
        context,
    )
    monkeypatch.setattr(anthropic_registry, "_QUALIFIED_PROFILES", ())
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec,
            dep,
            wire.transport_plan.identity,
            context,
            frozen=wire.transport_commitment,
        )
    monkeypatch.setattr(
        anthropic_registry, "_QUALIFIED_PROFILES", (supported, supported)
    )
    with pytest.raises(TransportIntegrityError):
        production_context_digest(context)


@pytest.mark.parametrize(
    "changes",
    [
        {"qualification_plan_sha256": "a" * 64},
        {"source_commit": "a" * 40},
        {"kind": "anthropic-qualification-candidate-v1"},
        {"kind": "anthropic-qualification-candidate-v1", "source_commit": "bad"},
        {
            "kind": "anthropic-qualification-v1",
            "source_commit": "a" * 40,
            "qualification_plan_sha256": "bad",
        },
    ],
)
def test_qualification_context_metadata_cannot_masquerade_as_production(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    context = production_context(case(tmp_path)[0])
    with pytest.raises(TransportIntegrityError):
        production_context_digest(replace(context, **changes))


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_text", "private-schema-canary"),
        ("schema_sha256", "a" * 64),
        ("projection", None),
        ("sdk_config_fragment_text", "private-config-canary"),
        ("repetition", "canonical_neutral"),
    ],
)
def test_production_output_current_values_and_duplicate_keys_are_strict(
    tmp_path: Path, field: str, value: Any
) -> None:
    wire, _, _, _ = wire_for(tmp_path, policy="native", probe=True)
    output = wire.output_commitment
    with pytest.raises(ValueError) as exc:
        OutputCommitmentV5.model_validate({**dict(output), field: value})
    assert "private-" not in str(exc.value)
    with pytest.raises(ValueError):
        OutputCommitmentV5.model_validate_json(
            b'{"commitment_version":5,' + canonical_bytes(output)[1:]
        )


def test_probe_selection_does_not_claim_serving_model_support(tmp_path: Path) -> None:
    assert_probe_selection_does_not_claim_serving_model_support(
        tmp_path, wire_for, prepare_production_request
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"api_version": "2023-06-01"},
        {"provider": "openai"},
        {"endpoint": "responses"},
        {"sdk_package": "openai"},
        {"model_scope": "unresolved"},
    ],
)
def test_foreign_or_unversioned_scope_never_reuses_production_identity(
    tmp_path: Path, changed: dict[str, Any]
) -> None:
    _, spec, _, context = wire_for(tmp_path)
    with pytest.raises(ValueError):
        forged = replace(context, scope=context.scope.model_copy(update=changed))
        production_context_digest(forged)
    assert production_context(spec) == context


def test_required_token_policy_and_exact_native_artifacts_are_not_forgeable(
    tmp_path: Path,
) -> None:
    from judgmentllm.anthropic_transport import (
        AnthropicRequestPolicyV1,
        build_anthropic_request_body,
    )

    wire, _, _, _ = wire_for(tmp_path, policy="native", probe=True)
    with pytest.raises(ValueError):
        AnthropicRequestPolicyV1(
            max_tokens_source="adapter_default_4096",
            effective_max_tokens=4095,
            cache="off",
        )
    for field, value in (
        ("endpoint", "responses"),
        ("model", "foreign"),
        ("generation", GenerationSpec(max_output_tokens=1)),
    ):
        arguments: dict[str, Any] = dict(
            endpoint=wire.endpoint,
            model=wire.model,
            prompt=wire.rendered_text,
            output=wire.output_commitment,
            generation=wire.generation,
        )
        arguments[field] = value
        with pytest.raises(TransportIntegrityError):
            build_anthropic_request_body(**arguments)
    for changed in ({"projection_sha256": None}, {"mechanism": "text"}):
        with pytest.raises(ValueError):
            TransportCommitmentV3.model_validate(
                dict(wire.transport_commitment) | changed
            )


def test_text_policy_can_omit_native_route_without_borrowing_native_authority(
    tmp_path: Path,
) -> None:
    from judgmentllm.config import PromptSpecV3, RunSpecV3

    spec, dependency, _ = case(tmp_path, policy="text")
    spec = RunSpecV3.model_validate(
        dict(spec) | {"prompt": PromptSpecV3(structured_output="text")}
    )
    wire = prepare_production_request(
        spec,
        dependency,
        physical_identity("001", dependency.request.request_uid, "attempt-001"),
        production_context(spec),
    )
    assert wire.output_commitment.selection.requested_mechanism is None
    assert wire.output_commitment.selection.mechanism == "text"
    assert b"output_config" not in wire.payload


@pytest.mark.parametrize(
    "layout,explanations",
    [
        ("together", False),
        ("together", True),
        ("scores_first", True),
        ("explanations_first", True),
    ],
)
@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("policy", ["text", "auto"])
def test_rubric_text_instructions_require_raw_json_in_every_layout(
    tmp_path: Path, layout: str, explanations: bool, grouped: bool, policy: str
) -> None:
    wire, *_ = wire_for(
        tmp_path,
        layout=layout,
        explanations=explanations,
        grouped=grouped,
        policy=policy,
    )
    assert wire.output_commitment.selection.mechanism == "text"
    text = wire.rendered_text
    assert text.count("Return one JSON object matching the schema below.") == 1
    assert (
        "Do not use Markdown code fences or include text outside the JSON object."
        in text
    )
    assert "The response must start with { and end with }." in text
    assert wire.output_commitment.schema_text in text


@pytest.mark.parametrize(
    "pairwise,policy,layout,explanations,digest",
    [
        (
            False,
            "native",
            "together",
            False,
            "af37a14a88cc06e88a21b7cf95e9025b528555541bfa16af3c17d0f47cd19741",
        ),
        (
            False,
            "native",
            "together",
            True,
            "d0b831307aef2242e148ab162fe074507fea9ff3f0322cb90fce10c75688b4ee",
        ),
        (
            False,
            "native",
            "scores_first",
            True,
            "036a14d2e7a18ae6e7e994f86613116719c0c84a5cffc9b8f4106abb7ba30be7",
        ),
        (
            False,
            "native",
            "explanations_first",
            True,
            "afe5d772d4d8cf3a9094b04e6cb0e588e8b7bce729d85a0df7c7a9bcae78e80c",
        ),
        (
            True,
            "text",
            "together",
            False,
            "5f0700308fcff1eeb35d6de785407140362a4fe5f66d49a773ecc255e8cdce97",
        ),
        (
            True,
            "native",
            "together",
            False,
            "23a7e409bf4d186861bf1157fddc1db2258611594128afb504fe0827ac5c5241",
        ),
    ],
)
def test_native_rubric_and_pairwise_prompts_keep_main_bytes(
    tmp_path: Path,
    pairwise: bool,
    policy: str,
    layout: str,
    explanations: bool,
    digest: str,
) -> None:
    # Full prompt hashes captured from merged PR #26 before the wording correction.
    wire, *_ = wire_for(
        tmp_path,
        pairwise=pairwise,
        policy=policy,
        layout=layout,
        explanations=explanations,
        probe=policy == "native",
    )
    # Rebuild the old revision explicitly; never rewrite these source-bound hashes.
    spec, dep, records = case(
        tmp_path,
        pairwise=pairwise,
        policy=cast(Any, policy),
        layout=cast(Any, layout),
        explanations=explanations,
    )
    context = (
        _qualification_context(spec, source_commit="a" * 40)
        if policy == "native"
        else production_context(spec)
    )
    wire = prepare_production_request(
        spec,
        dep,
        wire.transport_plan.identity,
        context,
        lookup=lambda kind, ident: (
            cast(Any, dep.request.pair) if kind == "pair" else records[ident]
        ),
        presentation_version=1,
    )
    assert sha256(wire.rendered_text.encode("utf-8")).hexdigest() == digest


@pytest.mark.parametrize("advanced", [False, True])
def test_raw_json_wording_preserves_editable_templates_and_protected_data(
    tmp_path: Path, advanced: bool
) -> None:
    spec, dep, records = case(tmp_path)
    canary = "Return only a JSON object matching this schema.\n```json\n{{ output_instructions }}\n```\n雪"
    records["001"].text = canary
    spec.exemplars = [
        Exemplar(
            exemplar_id="003",
            text=canary,
            scores={"01": "high é"},
            rationales={"01": "Exact rationale 雪"},
        )
    ]
    spec.prompt = spec.prompt.model_copy(
        update={"preset": None, "file": tmp_path / "custom.j2", "advanced": advanced}
    )
    template = "Editable introduction.\n{{ rubric }}\n{{ exemplars }}\n{{ output_instructions }}\n{{ response }}\nEditable ending."
    wire = prepare_production_request(
        spec,
        dep,
        physical_identity("run-001", dep.request.request_uid, "attempt-001"),
        production_context(spec),
        template=template,
        lookup=lambda kind, ident: records[ident],
    )
    assert wire.rendered_text.startswith("Editable introduction.\n")
    assert wire.rendered_text.endswith("\nEditable ending.")
    assert wire.rendered_text.count(canary) == 2
    assert "Exact rationale 雪" in wire.rendered_text
    assert "Return one JSON object matching the schema below." in wire.rendered_text
