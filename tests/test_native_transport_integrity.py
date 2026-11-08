"""Frozen choices and exact physical commitments reject inconsistent replay."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from native_transport_fixture import MECHANISMS, context_for
from test_native_transport_integration import (
    offline as offline,
)
from test_native_transport_integration import prepared_case, reopen

from judgmentllm import native_selection, native_selection_v2, transport
from judgmentllm.config import RunSpecV3
from judgmentllm.persistence.hashing import bytes_digest, semantic_digest
from judgmentllm.transport import (
    TransportCommitmentV1,
    physical_identity,
    prepare_transport_request,
)


@pytest.mark.parametrize(
    "field",
    [
        "neutral_schema_sha256",
        "neutral_semantic_sha256",
        "native_schema_sha256",
        "sdk_config_sha256",
        "format_sha256",
        "projection_sha256",
        "context_sha256",
        "prompt_sha256",
        "payload_sha256",
        "membership_sha256",
        "configuration_sha256",
        "output_commitment_sha256",
        "plan_sha256",
        "native_schema_bytes",
        "sdk_config_bytes",
        "format_bytes",
        "prompt_bytes",
        "payload_bytes",
        "renderer_version",
        "projection_version",
    ],
)
def test_rehashed_record_cannot_replace_frozen_commitment(
    tmp_path: Path, field: str
) -> None:
    adapter, store, _ = prepared_case(tmp_path)
    with reopen(adapter, store) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        attempt = next(prepared.repository.iter_attempts("001"))
        identity = physical_identity("001", attempt.request_uid, attempt.attempt_id)
        receipt = prepared.transport_for_attempt(attempt.attempt_id)
        values = receipt.model_dump(mode="json")
        if field.endswith("sha256"):
            values[field] = "a" * 64 if values[field] != "a" * 64 else "b" * 64
        else:
            values[field] = int(values[field]) + 1
        values["commitment_sha256"] = semantic_digest(
            "transport-commitment-v1",
            {key: value for key, value in values.items() if key != "commitment_sha256"},
        )
        # Some inconsistent records fail independently; the rest must fail against
        # the immutable attempt/manifest anchor, even with a valid fresh checksum.
        with pytest.raises(ValueError):
            changed = TransportCommitmentV1.model_validate(values)
            prepared.prepare_transport_attempt(dependency, identity, frozen=changed)
    assert adapter.submit_calls == 0


@pytest.mark.parametrize("mechanism", MECHANISMS)
@pytest.mark.parametrize("policy", ["auto", "native", "text"])
def test_reopen_never_calls_a_transport_resolver(
    tmp_path: Path, mechanism: str, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, store, _ = prepared_case(tmp_path, mechanism, policy=policy)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("reopening must verify the original selection")

    monkeypatch.setattr(transport, "_resolve_native_transport", forbidden)
    monkeypatch.setattr(transport, "_resolve_native_transport_v2", forbidden)
    monkeypatch.setattr(native_selection, "_resolve_native_transport", forbidden)
    monkeypatch.setattr(native_selection_v2, "_resolve_native_transport_v2", forbidden)
    with reopen(adapter, store) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        attempt = next(prepared.repository.iter_attempts("001"))
        prepared.prepare_transport_attempt(
            dependency,
            physical_identity("001", attempt.request_uid, attempt.attempt_id),
            frozen=prepared.transport_for_attempt(attempt.attempt_id),
        )


@pytest.mark.parametrize(
    "change",
    [
        "provider",
        "endpoint",
        "model",
        "categories",
        "trait_order",
        "trait_omission",
        "explanations",
        "layout",
        "policy",
        "route",
        "template",
    ],
)
def test_changed_assessment_or_transport_intent_cannot_replay(
    tmp_path: Path, change: str
) -> None:
    adapter, store, spec = prepared_case(tmp_path)
    assert isinstance(spec, RunSpecV3)
    with reopen(adapter, store) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        attempt = next(prepared.repository.iter_attempts("001"))
        receipt = prepared.transport_for_attempt(attempt.attempt_id)
        values = spec.model_dump(mode="json")
        if change in {"provider", "endpoint", "model"}:
            values[change] = "changed"
        elif change == "categories":
            values["rubric"]["traits"][0]["levels"][0]["label"] = "changed-label"
            values["rubric"]["traits"][0]["levels"][0]["source_label"] = "changed-label"
        elif change == "trait_order":
            request = dependency.request.model_copy(
                update={
                    "trait_ids": dependency.request.trait_ids[::-1],
                    "tasks": dependency.request.tasks[::-1],
                }
            )
            dependency = dependency.model_copy(update={"request": request})
        elif change == "trait_omission":
            request = dependency.request.model_copy(
                update={
                    "trait_ids": dependency.request.trait_ids[:1],
                    "tasks": dependency.request.tasks[:1],
                }
            )
            dependency = dependency.model_copy(update={"request": request})
        elif change == "explanations":
            values["execution"]["explanations"] = False
        elif change == "layout":
            values["prompt"]["output_order"] = "scores_first"
        elif change == "policy":
            values["prompt"]["structured_output"] = "text"
        elif change == "route":
            values["prompt"]["native_mechanism"] = MECHANISMS[1]
        with pytest.raises(ValueError):
            changed = RunSpecV3.model_validate(values)
            prepare_transport_request(
                changed,
                dependency,
                physical_identity("001", attempt.request_uid, attempt.attempt_id),
                adapter.transport_context,
                lookup=prepared._get,
                frozen=receipt,
                template="Changed instructions\n{{ rubric }}\n{{ response }}\n{{ output_instructions }}"
                if change == "template"
                else prepared.template,
            )


@pytest.mark.parametrize(
    "parent_policy,child_policy", [("native", "text"), ("text", "native")]
)
def test_explicit_family_substitution_rejected(
    tmp_path: Path, parent_policy: str, child_policy: str
) -> None:
    adapter, store, spec = prepared_case(tmp_path, policy=parent_policy)
    with reopen(adapter, store) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        attempt = next(prepared.repository.iter_attempts("001"))
        receipt = prepared.transport_for_attempt(attempt.attempt_id)
        values = spec.model_dump(mode="json")
        values["prompt"]["structured_output"] = child_policy
        with pytest.raises(ValueError):
            prepare_transport_request(
                RunSpecV3.model_validate(values),
                dependency,
                physical_identity("001", attempt.request_uid, attempt.attempt_id),
                adapter.transport_context,
                lookup=prepared._get,
                frozen=receipt,
            )


@pytest.mark.parametrize(
    "mechanism",
    [
        "gemini.generate_content.response_schema",
        "vertex.generate_content.response_schema",
    ],
)
def test_google_route_cannot_be_exchanged_during_replay(
    tmp_path: Path, mechanism: str
) -> None:
    adapter, store, spec = prepared_case(tmp_path, mechanism)
    with reopen(adapter, store) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        attempt = next(prepared.repository.iter_attempts("001"))
        receipt = prepared.transport_for_attempt(attempt.attempt_id)
        context = context_for(
            mechanism.replace("response_schema", "response_json_schema")
        )
        context = replace(
            context,
            service_identity=adapter.ledger.identity,
            fixture_sha256=adapter.transport_context.fixture_sha256,
        )
        with pytest.raises(ValueError):
            prepare_transport_request(
                spec,
                dependency,
                physical_identity("001", attempt.request_uid, attempt.attempt_id),
                context,
                lookup=prepared._get,
                frozen=receipt,
            )


@pytest.mark.parametrize(
    "damage",
    [
        "whitespace_payload",
        "task_membership",
        "format",
        "format_bytes",
        "generation",
        "provider",
    ],
)
def test_prepared_model_rejects_rehashed_inconsistent_physical_payload(
    tmp_path: Path, damage: str
) -> None:
    adapter, store, _ = prepared_case(tmp_path)
    with reopen(adapter, store) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        attempt = next(prepared.repository.iter_attempts("001"))
        wire = prepared.prepare_transport_attempt(
            dependency,
            physical_identity("001", attempt.request_uid, attempt.attempt_id),
            frozen=prepared.transport_for_attempt(attempt.attempt_id),
        )
    payload = json.loads(wire.payload)
    if damage == "task_membership":
        payload["tasks"].reverse()
    elif damage == "format":
        payload["format_fragment"] = {}
    elif damage == "format_bytes":
        payload["format_fragment_text"] += " "
    elif damage == "generation":
        payload["generation"] = {"temperature": 0.7}
    elif damage == "provider":
        payload["provider"] = "other"
    raw = (
        wire.payload + b" "
        if damage == "whitespace_payload"
        else json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    )
    digest = bytes_digest(raw)
    with pytest.raises(ValueError):
        wire.model_copy(update={"payload": raw, "payload_sha256": digest})
