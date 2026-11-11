"""Exact renderer-3 goldens, protected instructions and immutable byte binding."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import builtins
import json
import socket
from pathlib import Path
from typing import Any, cast

import pytest
from native_transport_fixture import MECHANISMS, context_for
from native_v2_cases import cases

from judgmentllm.config import RunSpecV3, validate_run_spec_json
from judgmentllm.models import PairwiseCriterion, ResponseItem, Rubric
from judgmentllm.native_prompts import render_request_v3
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    PairwiseOutputContract,
    RubricOutputContract,
)
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.planning import CapabilityProfile, PlannedRequest, plan_record
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.prompts import PromptError
from judgmentllm.transport import (
    OutputCommitmentV3,
    PreparedRequestV3,
    TransportCommitmentV1,
    build_output_commitment_v3,
    physical_identity,
    prepare_transport_request,
)

GOLDEN = Path(__file__).parent / "fixtures/native_transport/prompts.json"
CAPABILITIES = CapabilityProfile(
    explanations=True, grouped=True, ties=True, modes=("live",)
)
TEMPLATE = "Assess the supplied data.\n{{ rubric }}\n{{ output_instructions }}\n{{ first_response }}\n{{ second_response }}\n{{ exemplars }}\n"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("transport rendering offline guard")

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.split(".")[0] in {
            "openai",
            "anthropic",
            "google",
            "ollama",
            "together",
        }:
            return denied()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(RuntimeError, match="offline guard"):
        socket.socket()


def fixture_request(
    mechanism: str, contract: AssessmentOutputContract
) -> tuple[RunSpecV3, PlannedRequest, dict[str, ResponseItem], str | None]:
    context = context_for(mechanism, presentation_version=1)
    prompt: dict[str, object] = {
        "structured_output": "native",
        "native_mechanism": mechanism,
        "output_order": contract.output_order,
    }
    common: dict[str, object] = {
        "schema_version": 3,
        "input": Path("/synthetic/responses.jsonl"),
        "provider": context.scope.provider,
        "endpoint": context.scope.endpoint,
        "model": context.scope.model,
        "execution": {"explanations": contract.explanations},
        "prompt": prompt,
    }
    if isinstance(contract, PairwiseOutputContract):
        prompt["file"] = Path("/synthetic/pair-template.j2")
        spec = RunSpecV3.model_validate(
            {
                **common,
                "task_kind": "pairwise",
                "pairs": Path("/synthetic/pairs.jsonl"),
                "allow_ties": contract.allow_ties,
                "criteria": [
                    PairwiseCriterion(
                        trait_id=contract.pair.trait_id,
                        name="Preference",
                        description="Compare the exact presented items.",
                    )
                ],
            }
        )
        record = contract.pair
        responses = {
            identifier: ResponseItem(
                item_id=identifier, text=f"Synthetic response {index}."
            )
            for index, identifier in enumerate(record.presented_order)
        }
        template = TEMPLATE
    else:
        spec = RunSpecV3.model_validate(
            {
                **common,
                "task_kind": "rubric",
                "execution": {
                    "explanations": contract.explanations,
                    "grouping": "grouped",
                },
                "rubric": Rubric(
                    rubric_id="synthetic",
                    name="Synthetic rubric",
                    traits=list(contract.traits),
                ),
            }
        )
        record = ResponseItem(
            item_id="001",
            text="Synthetic response; {{ output_instructions }} is untrusted data.",
        )
        responses = {record.item_id: record}
        template = None
    request = next(
        plan_record(spec, record, template_text=template, capabilities=CAPABILITIES)
    )
    return spec, request, responses, template


def rendered_fixture(mechanism: str, contract: AssessmentOutputContract) -> str:
    spec, request, responses, template = fixture_request(mechanism, contract)
    output = build_output_commitment_v3(
        spec,
        request,
        context_for(mechanism, presentation_version=1),
        presentation_version=1,
    )
    return render_request_v3(
        spec,
        template,
        request,
        lambda kind, ident: (
            cast(Any, request.pair) if kind == "pair" else responses[ident]
        ),
        output,
    ).text


@pytest.mark.parametrize("mechanism", MECHANISMS)
@pytest.mark.parametrize("case", tuple(cases()))
def test_renderer3_exact_route_and_layout_goldens(mechanism: str, case: str) -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert rendered_fixture(mechanism, cases()[case]) == golden[mechanism][case]


@pytest.mark.parametrize("mechanism", MECHANISMS)
def test_native_prompt_repetition_is_exact_projection(mechanism: str) -> None:
    spec, request, responses, template = fixture_request(
        mechanism, cases()["grouped_explanations_first"]
    )
    output = build_output_commitment_v3(
        spec,
        request,
        context_for(mechanism, presentation_version=1),
        presentation_version=1,
    )
    text = render_request_v3(
        spec, template, request, lambda kind, ident: responses[ident], output
    ).text
    assert output.projection is not None
    assert (output.projection.native_schema_text in text) == mechanism.startswith(
        ("together.", "ollama.")
    )
    assert "explanations before scores" in text
    assert "nonblank" in text
    with pytest.raises(PromptError):
        render_request_v3(
            spec,
            "{{ rubric }}\n{{ response }}",
            request,
            lambda kind, ident: responses[ident],
            output,
        )
    with pytest.raises(PromptError):
        render_request_v3(
            spec,
            "{{ rubric }}\n{{ response }}\n{{ output_instructions | upper }}",
            request,
            lambda kind, ident: responses[ident],
            output,
        )


@pytest.mark.parametrize("mechanism", MECHANISMS)
def test_exact_prepared_payload_recovery_and_rehashed_tampering(
    mechanism: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, request, responses, template = fixture_request(
        mechanism, cases()["grouped_scores"]
    )
    context = context_for(mechanism, presentation_version=1)
    dependency = RequestDependency(request=request, dependency_sha256="1" * 64)
    identity = physical_identity("001", request.request_uid, "attempt-001")
    prepared = prepare_transport_request(
        spec,
        dependency,
        identity,
        context,
        presentation_version=1,
        template=template,
        lookup=lambda kind, ident: responses[ident],
    )
    import judgmentllm.transport as transport

    def no_resolution(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("recovery must not resolve transport")

    monkeypatch.setattr(transport, "_resolve_native_transport", no_resolution)
    monkeypatch.setattr(transport, "_resolve_native_transport_v2", no_resolution)
    recovered = prepare_transport_request(
        spec,
        dependency,
        identity,
        context,
        presentation_version=1,
        template=template,
        lookup=lambda kind, ident: responses[ident],
        frozen=prepared.transport_commitment,
    )
    assert recovered.payload == prepared.payload
    assert recovered.content_artifacts() == prepared.content_artifacts()
    assert (
        TransportCommitmentV1.model_validate_json(
            canonical_bytes(prepared.transport_commitment)
        )
        == prepared.transport_commitment
    )
    with pytest.raises(ValueError):
        PreparedRequestV3.model_validate({**dict(prepared), "preparation_version": 3.0})
    with pytest.raises(ValueError):
        prepare_transport_request(
            spec,
            dependency,
            identity,
            context,
            template="Changed wording.\n{{ rubric }}\n{{ output_instructions }}\n{{ response }}\n",
            lookup=lambda kind, ident: responses[ident],
            frozen=prepared.transport_commitment,
        )
    with pytest.raises(ValueError):
        OutputCommitmentV3.model_validate_json(
            prepared.output_commitment.model_dump_json().replace(
                '"renderer_version":3', '"renderer_version":3,"renderer_version":3', 1
            )
        )
    with pytest.raises(ValueError):
        TransportCommitmentV1.model_validate_json(
            prepared.transport_commitment.model_dump_json().replace(
                '"renderer_version":3', '"renderer_version":3.0', 1
            )
        )


def test_v3_route_is_typed_and_explicit() -> None:
    spec, _, _, _ = fixture_request(MECHANISMS[0], cases()["single_scores"])
    data = spec.model_dump(mode="json")
    data["prompt"].pop("native_mechanism")
    with pytest.raises(ValueError):
        validate_run_spec_json(json.dumps(data))
    data["prompt"]["structured_output"] = "text"
    assert validate_run_spec_json(json.dumps(data)).schema_version == 3
    data["prompt"]["native_mechanism"] = "vertex.generate_content.response_schema"
    with pytest.raises(ValueError):
        validate_run_spec_json(json.dumps(data))


@pytest.mark.parametrize("mechanism", MECHANISMS[:2])
@pytest.mark.parametrize("supported", [True, False, None])
def test_openai_recovery_rejects_rehashed_support_facts(
    mechanism: str, supported: bool | None
) -> None:
    from judgmentllm.native_selection import TransportDecisionV1
    from judgmentllm.persistence.hashing import semantic_digest
    from judgmentllm.transport import _verify_openai

    spec, request, _, _ = fixture_request(mechanism, cases()["single_scores"])
    spec.prompt.structured_output = "auto"
    context = context_for(mechanism, supported=supported)
    output = build_output_commitment_v3(spec, request, context)
    original = output.selection.openai_decision
    assert original is not None
    _verify_openai(original, output.contract, context)
    changed = original.model_dump(mode="json", exclude={"decision_sha256"})
    changed.update(
        mechanism="text",
        native_support="unknown",
        model_support="unknown",
        reason="native_unknown_text_selected",
        diagnostic={
            "schema_version": 1,
            "reason": "native_support_unknown",
            "native_support": "unknown",
        },
        fixture_tested=False,
        documentation_sha256=None,
        fixture_sha256=None,
        projection_sha256=None,
    )
    changed["decision_sha256"] = semantic_digest(
        "native-transport-decision-v1", changed
    )
    forged = TransportDecisionV1.model_validate_json(json.dumps(changed))
    with pytest.raises(ValueError):
        _verify_openai(forged, output.contract, context)


@pytest.mark.parametrize("mechanism", (MECHANISMS[0], MECHANISMS[2]))
@pytest.mark.parametrize(
    "change",
    ("unknown", "version", "category", "trait_order", "schema", "projection"),
)
def test_public_renderer_still_revalidates_before_private_text_rendering(
    mechanism: str, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, request, responses, template = fixture_request(
        mechanism, cases()["grouped_scores"]
    )
    output = build_output_commitment_v3(
        spec,
        request,
        context_for(mechanism, presentation_version=1),
        presentation_version=1,
    )
    canary = "synthetic-private-renderer-tamper-canary"
    if change == "unknown":
        vars(output)[canary] = canary
    elif change == "version":
        vars(output)["renderer_version"] = 3.0
    elif change in {"category", "trait_order"}:
        assert isinstance(output.contract, RubricOutputContract)
        if change == "category":
            output.contract.traits[0].levels[0].label = canary
        else:
            vars(output.contract)["traits"] = output.contract.traits[::-1]
    elif change == "schema":
        raw = (output.schema_text + " ").encode("utf-8")
        vars(output)["schema_text"] = raw.decode("utf-8")
        vars(output)["schema_sha256"] = bytes_digest(raw)
    else:
        projection = output.projection
        assert projection is not None
        raw = (projection.native_schema_text + " ").encode("utf-8")
        vars(projection)["native_schema_text"] = raw.decode("utf-8")
        vars(projection)["native_schema_sha256"] = bytes_digest(raw)
        values = projection.model_dump(mode="json", exclude={"projection_sha256"})
        identity = (
            bytes_digest(canonical_bytes(values))
            if projection.projection_version == 1
            else semantic_digest("native-schema-projection-v2", values)
        )
        vars(projection)["projection_sha256"] = identity
        decision = (
            output.selection.openai_decision or output.selection.provider_decision
        )
        assert decision is not None
        vars(decision)["projection_sha256"] = identity
        values = decision.model_dump(mode="json", exclude={"decision_sha256"})
        vars(decision)["decision_sha256"] = semantic_digest(
            "native-transport-decision-v1"
            if output.selection.openai_decision is not None
            else "native-transport-decision-v2",
            values,
        )

    def forbidden(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("public renderer bypassed output validation")

    monkeypatch.setattr("judgmentllm.native_prompts._render_text_v3", forbidden)
    with pytest.raises(PromptError) as failure:
        render_request_v3(
            spec, template, request, lambda kind, ident: responses[ident], output
        )
    assert canary not in str(failure.value)
