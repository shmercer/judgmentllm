"""Offline production byte commitments without synthetic execution authority."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import os
import socket
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from gemini_fixture import case, wire_for

from judgmentllm.config import (
    GenerationSpec,
)
from judgmentllm.evidence.scope import EvidenceProvenance
from judgmentllm.execution.retry import subset_dependency
from judgmentllm.gemini_registry import _QualifiedProfile
from judgmentllm.gemini_transport import (
    GeminiProductionContext,
    OutputCommitmentV6,
    PreparedRequestV6,
    TransportCommitmentV4,
    build_output_commitment_v6,
    prepare_production_request,
    production_context,
    production_context_digest,
)
from judgmentllm.models import (
    PairTask,
    ResponseItem,
)
from judgmentllm.persistence.hashing import (
    canonical_bytes,
    semantic_digest,
)
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


def test_unqualified_native_and_strict_auto_cannot_execute(tmp_path: Path) -> None:
    for policy, strict in (("native", False), ("auto", True)):
        with pytest.raises((TransportIntegrityError, ValueError)):
            wire_for(tmp_path, policy=policy, strict=strict)
    wire, _, _, _ = wire_for(tmp_path, policy="auto")
    assert wire.output_commitment.selection.mechanism == "text"
    assert wire.output_commitment.selection.native_support == "unknown"


def test_frozen_reconstruction_and_subset_lineage(tmp_path: Path) -> None:
    wire, spec, dep, context = wire_for(tmp_path, policy="native", probe=True)
    frozen = prepare_production_request(
        spec,
        dep,
        wire.transport_plan.identity,
        context,
        frozen=wire.transport_commitment,
    )
    assert frozen == wire
    subset = subset_dependency(dep, wire.task_ids[:1])
    retry = prepare_production_request(
        spec,
        subset,
        physical_identity("run-001", dep.request.request_uid, "attempt-002"),
        context,
        parent=wire.transport_commitment,
    )
    assert (
        retry.transport_commitment.parent_commitment_sha256
        == wire.transport_commitment.commitment_sha256
    )
    assert retry.transport_commitment.mechanism == wire.transport_commitment.mechanism
    assert retry.task_ids == wire.task_ids[:1]
    assert retry.payload != wire.payload
    assert (
        prepare_production_request(
            spec,
            subset,
            retry.transport_plan.identity,
            context,
            frozen=retry.transport_commitment,
            parent=wire.transport_commitment,
        )
        == retry
    )
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec,
            subset,
            retry.transport_plan.identity,
            context,
            frozen=retry.transport_commitment,
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
        PreparedRequestV6.model_validate({**dict(wire), field: value})
    assert "seed grows" not in str(exc.value)
    assert "low 雪" not in str(exc.value)


def test_receipt_round_trip_duplicate_ingress_and_versions(tmp_path: Path) -> None:
    wire, _, _, _ = wire_for(tmp_path)
    receipt = wire.transport_commitment
    assert (
        TransportCommitmentV4.model_validate_json(canonical_bytes(receipt)) == receipt
    )
    for changed in (
        {"transport_commitment_version": 4.0},
        {"store": 0},
        {"commitment_sha256": "a" * 64},
        {"payload_sha256": "b" * 64},
    ):
        with pytest.raises(ValueError):
            TransportCommitmentV4.model_validate({**dict(receipt), **changed})
    raw = canonical_bytes(receipt)
    with pytest.raises(ValueError):
        TransportCommitmentV4.model_validate_json(
            b'{"transport_commitment_version":4,' + raw[1:]
        )
    changed = receipt.model_dump(mode="json", exclude={"commitment_sha256"})
    changed["payload_sha256"] = "b" * 64
    changed["commitment_sha256"] = semantic_digest(
        "gemini-transport-commitment-v4", changed
    )
    with pytest.raises(ValueError):
        PreparedRequestV6.model_validate(
            {
                **dict(wire),
                "transport_commitment": TransportCommitmentV4.model_validate_json(
                    canonical_bytes(changed)
                ),
            }
        )


def test_protected_templates_limits_and_mutation_guard(tmp_path: Path) -> None:
    from judgmentllm.prompts import RenderLimits

    wire, spec, dep, context = wire_for(tmp_path)
    for template in (
        "{{ response }}",
        "{{ rubric }} {{ response }}",
        "{{ output_instructions }}",
    ):
        with pytest.raises(TransportIntegrityError):
            prepare_production_request(
                spec, dep, wire.transport_plan.identity, context, template=template
            )
    small = spec.model_copy(
        update={
            "prompt": spec.prompt.model_copy(
                update={"limits": RenderLimits(max_output_bytes=30)}
            )
        }
    )
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(small, dep, wire.transport_plan.identity, context)

    def lookup(
        kind: Literal["response", "pair"], identifier: str
    ) -> ResponseItem | PairTask:
        assert dep.request.response is not None
        spec.model = "mutated-private-canary"
        return dep.request.response

    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec, dep, wire.transport_plan.identity, context, lookup=lookup
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
        build_output_commitment_v6(changed, dep.request, context)
    assert "private-canary" not in str(exc.value)


def _reviewed_profile(
    context: GeminiProductionContext, **changes: Any
) -> _QualifiedProfile:
    fields: dict[str, Any] = {
        "scope": context.scope,
        "mechanism": "gemini.generate_content.response_json_schema",
        "schema_dialect": "gemini.json-schema.v1",
        "qualification_date": date(2026, 11, 7),
        "source_commit": "a" * 40,
        "support_status": "supported",
        "request_acceptance": "accepted",
        "assessment_behavior": "verified",
        "native_schema_behavior": "verified",
        "supported_features": ("rubric_scores", "rubric_grouped"),
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
    from judgmentllm import gemini_registry

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
    monkeypatch.setattr(gemini_registry, "_QUALIFIED_PROFILES", (unsupported,))
    output = build_output_commitment_v6(spec, dep.request, context)
    assert output.selection.mechanism == "text"
    assert output.selection.native_support == "unsupported"
    supported = _reviewed_profile(context)
    monkeypatch.setattr(gemini_registry, "_QUALIFIED_PROFILES", (supported,))
    output = build_output_commitment_v6(spec, dep.request, context)
    assert output.selection.mechanism != "text"
    wire = prepare_production_request(
        spec,
        dep,
        physical_identity("run-001", dep.request.request_uid, "attempt-001"),
        context,
    )
    monkeypatch.setattr(gemini_registry, "_QUALIFIED_PROFILES", ())
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec,
            dep,
            wire.transport_plan.identity,
            context,
            frozen=wire.transport_commitment,
        )
    monkeypatch.setattr(gemini_registry, "_QUALIFIED_PROFILES", (supported, supported))
    with pytest.raises(TransportIntegrityError):
        production_context_digest(context)


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
        OutputCommitmentV6.model_validate({**dict(output), field: value})
    assert "private-" not in str(exc.value)
    with pytest.raises(ValueError):
        OutputCommitmentV6.model_validate_json(
            b'{"commitment_version":6,' + canonical_bytes(output)[1:]
        )


def test_probe_selection_does_not_claim_serving_model_support(tmp_path: Path) -> None:
    wire, spec, dep, context = wire_for(tmp_path, policy="native", probe=True)
    selection = wire.output_commitment.selection
    assert selection.kind == "qualification_probe"
    assert selection.authority_kind == "qualification_probe"
    assert selection.native_support == "unknown"
    assert wire.transport_commitment.selection.native_support == "unknown"
    subset = subset_dependency(dep, wire.task_ids[:1])
    retry = prepare_production_request(
        spec,
        subset,
        physical_identity("run-001", dep.request.request_uid, "attempt-002"),
        context,
        parent=wire.transport_commitment,
    )
    assert retry.output_commitment.selection.kind == "inherited"
    assert retry.output_commitment.selection.authority_kind == "qualification_probe"
    assert retry.output_commitment.selection.native_support == "unknown"
    from pydantic import BaseModel

    forged = BaseModel.model_copy(selection, update={"authority_kind": "reviewed"})
    with pytest.raises(ValueError):
        type(selection).model_validate(dict(forged))


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
