"""Shared assertions for each provider's production transport contract."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest

from judgmentllm.anthropic_transport import PreparedRequestV5
from judgmentllm.config import RunSpecV3
from judgmentllm.execution.retry import subset_dependency
from judgmentllm.gemini_transport import PreparedRequestV6
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.production_transport import PreparedRequestV4
from judgmentllm.transport import TransportIntegrityError, physical_identity
from judgmentllm.vertex_transport import PreparedRequestV7

PreparedRequest = (
    PreparedRequestV4 | PreparedRequestV5 | PreparedRequestV6 | PreparedRequestV7
)


def assert_frozen_reconstruction_and_subset_lineage(
    tmp_path: Path,
    wire_for: Callable[
        ..., tuple[PreparedRequest, RunSpecV3, RequestDependency, object]
    ],
    prepare_production_request: Callable[..., PreparedRequest],
) -> None:
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


def assert_protected_templates_limits_and_mutation_guard(
    tmp_path: Path,
    wire_for: Callable[
        ..., tuple[PreparedRequest, RunSpecV3, RequestDependency, object]
    ],
    prepare_production_request: Callable[..., PreparedRequest],
) -> None:
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


def assert_probe_selection_does_not_claim_serving_model_support(
    tmp_path: Path,
    wire_for: Callable[
        ..., tuple[PreparedRequest, RunSpecV3, RequestDependency, object]
    ],
    prepare_production_request: Callable[..., PreparedRequest],
) -> None:
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
