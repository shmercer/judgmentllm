"""Offline production byte commitments without synthetic execution authority."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from transport_contract import (
    assert_frozen_reconstruction_and_subset_lineage,
    assert_probe_selection_does_not_claim_serving_model_support,
    assert_protected_templates_limits_and_mutation_guard,
)
from vertex_fixture import case, wire_for

from judgmentllm.config import (
    GenerationSpec,
)
from judgmentllm.persistence.hashing import (
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.transport import TransportIntegrityError, physical_identity
from judgmentllm.vertex_transport import (
    OutputCommitmentV7,
    PreparedRequestV7,
    TransportCommitmentV5,
    build_output_commitment_v7,
    prepare_production_request,
    production_context,
    production_context_digest,
)


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
        PreparedRequestV7.model_validate({**dict(wire), field: value})
    assert "seed grows" not in str(exc.value)
    assert "low 雪" not in str(exc.value)


def test_receipt_round_trip_duplicate_ingress_and_versions(tmp_path: Path) -> None:
    wire, _, _, _ = wire_for(tmp_path)
    receipt = wire.transport_commitment
    assert (
        TransportCommitmentV5.model_validate_json(canonical_bytes(receipt)) == receipt
    )
    for changed in (
        {"transport_commitment_version": 5.0},
        {"store": 0},
        {"commitment_sha256": "a" * 64},
        {"payload_sha256": "b" * 64},
    ):
        with pytest.raises(ValueError):
            TransportCommitmentV5.model_validate({**dict(receipt), **changed})
    raw = canonical_bytes(receipt)
    with pytest.raises(ValueError):
        TransportCommitmentV5.model_validate_json(
            b'{"transport_commitment_version":5,' + raw[1:]
        )
    changed = receipt.model_dump(mode="json", exclude={"commitment_sha256"})
    changed["payload_sha256"] = "b" * 64
    changed["commitment_sha256"] = semantic_digest(
        "vertex-transport-commitment-v5", changed
    )
    with pytest.raises(ValueError):
        PreparedRequestV7.model_validate(
            {
                **dict(wire),
                "transport_commitment": TransportCommitmentV5.model_validate_json(
                    canonical_bytes(changed)
                ),
            }
        )


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
        if os.fsdecode(key).startswith(("ANTHROPIC_", "AWS_", "GOOGLE_", "VERTEX_")):
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
        build_output_commitment_v7(changed, dep.request, context)
    assert "private-canary" not in str(exc.value)


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
        OutputCommitmentV7.model_validate({**dict(output), field: value})
    assert "private-" not in str(exc.value)
    with pytest.raises(ValueError):
        OutputCommitmentV7.model_validate_json(
            b'{"commitment_version":7,' + canonical_bytes(output)[1:]
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


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    from vertex_offline import isolate

    isolate(monkeypatch)
