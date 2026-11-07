"""Inherited transport replay requires the verified original parent identity."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
from typing import Any

import pytest
from native_transport_fixture import MECHANISMS, context_for
from native_v2_cases import cases
from test_native_transport_integration import offline as offline
from test_transport_rendering import CAPABILITIES, fixture_request

from judgmentllm import transport
from judgmentllm.execution.retry import subset_dependency
from judgmentllm.persistence.hashing import semantic_digest
from judgmentllm.planning import plan_record
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.transport import (
    FrozenTransportSelectionV1,
    TransportCommitmentV1,
    TransportPlanV1,
    physical_identity,
    prepare_transport_request,
)


def retry_requests(mechanism: str, policy: str = "auto") -> tuple[Any, ...]:
    spec, original, responses, template = fixture_request(
        mechanism, cases()["grouped_scores"]
    )
    values = spec.model_dump(mode="json")
    values["prompt"]["structured_output"] = policy
    spec = type(spec).model_validate_json(json.dumps(values))
    assert original.response is not None
    request = next(plan_record(spec, original.response, capabilities=CAPABILITIES))
    dependency = RequestDependency(request=request, dependency_sha256="a" * 64)
    context = context_for(mechanism)
    parent = prepare_transport_request(
        spec,
        dependency,
        physical_identity("001", request.request_uid, "original"),
        context,
        lookup=lambda kind, identifier: responses[identifier],
        template=template,
    )
    subset = subset_dependency(dependency, (request.tasks[0].task_id,))
    identity = physical_identity("001", request.request_uid, "retry")
    child = prepare_transport_request(
        spec,
        subset,
        identity,
        context,
        lookup=lambda kind, identifier: responses[identifier],
        template=template,
        parent=parent.transport_commitment,
    )
    return spec, subset, identity, context, responses, template, parent, child


def rehashed_receipt(values: dict[str, Any]) -> TransportCommitmentV1:
    plan = TransportPlanV1.model_validate_json(
        json.dumps(
            {key: values[key] for key in TransportPlanV1.model_fields if key in values}
        )
    )
    values["plan_sha256"] = plan.plan_sha256
    values.pop("commitment_sha256", None)
    values["commitment_sha256"] = semantic_digest("transport-commitment-v1", values)
    return TransportCommitmentV1.model_validate_json(json.dumps(values))


@pytest.mark.parametrize("mechanism", MECHANISMS)
def test_inherited_native_family_cannot_claim_explicit_text_policy(
    mechanism: str,
) -> None:
    with pytest.raises(ValueError):
        FrozenTransportSelectionV1.model_validate(
            {
                "kind": "inherited-v1",
                "policy": "text",
                "requested_mechanism": mechanism,
                "mechanism": mechanism,
                "parent_commitment_sha256": "a" * 64,
            }
        )


@pytest.mark.parametrize("mechanism", MECHANISMS)
@pytest.mark.parametrize("policy", ["auto", "text"])
def test_raw_inherited_replay_requires_its_verified_parent(
    mechanism: str, policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, subset, identity, context, responses, template, parent, child = (
        retry_requests(mechanism, policy)
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("inherited replay cannot select a transport")

    monkeypatch.setattr(transport, "_resolve_native_transport", forbidden)
    monkeypatch.setattr(transport, "_resolve_native_transport_v2", forbidden)
    with pytest.raises(ValueError):
        prepare_transport_request(
            spec,
            subset,
            identity,
            context,
            lookup=lambda kind, identifier: responses[identifier],
            template=template,
            frozen=child.transport_commitment,
        )
    recovered = prepare_transport_request(
        spec,
        subset,
        identity,
        context,
        lookup=lambda kind, identifier: responses[identifier],
        template=template,
        frozen=child.transport_commitment,
        parent=parent.transport_commitment,
    )
    assert recovered.payload == child.payload


@pytest.mark.parametrize(
    "change",
    ["family", "context", "configuration", "run", "request", "route", "policy"],
)
def test_rehashed_child_cannot_acquire_different_parent_facts(change: str) -> None:
    spec, subset, identity, context, responses, template, parent, child = (
        retry_requests(MECHANISMS[0])
    )
    values = parent.transport_commitment.model_dump(mode="json")
    if change == "context":
        values["context_sha256"] = "b" * 64
    elif change == "configuration":
        values["configuration_sha256"] = "b" * 64
    elif change in {"run", "request"}:
        changed_identity = physical_identity(
            "other" if change == "run" else values["identity"]["run_id"],
            "other" if change == "request" else values["identity"]["request_uid"],
            values["identity"]["attempt_id"],
        )
        values["identity"] = changed_identity.model_dump(mode="json")
    else:
        # A self-consistent inherited/text receipt has opaque content hashes.
        # It still cannot replace an independently verified native parent.
        values["mechanism"] = "text"
        values["requested_mechanism"] = (
            MECHANISMS[1] if change == "route" else MECHANISMS[0]
        )
        values["parent_commitment_sha256"] = "b" * 64
        values["selection"] = FrozenTransportSelectionV1(
            kind="inherited-v1",
            policy="text" if change == "policy" else "auto",
            mechanism="text",
            requested_mechanism=values["requested_mechanism"],
            parent_commitment_sha256="b" * 64,
        ).model_dump(mode="json")
        for field in (
            "projection_version",
            "projection_sha256",
            "native_schema_sha256",
            "native_schema_bytes",
            "sdk_config_sha256",
            "sdk_config_bytes",
            "format_sha256",
            "format_bytes",
        ):
            values[field] = None
    changed_parent = rehashed_receipt(values)
    child_values = child.transport_commitment.model_dump(mode="json")
    child_values["parent_commitment_sha256"] = changed_parent.commitment_sha256
    child_values["selection"]["parent_commitment_sha256"] = (
        changed_parent.commitment_sha256
    )
    changed_child = rehashed_receipt(child_values)
    with pytest.raises(ValueError):
        prepare_transport_request(
            spec,
            subset,
            identity,
            context,
            lookup=lambda kind, identifier: responses[identifier],
            template=template,
            frozen=changed_child,
            parent=changed_parent,
        )
