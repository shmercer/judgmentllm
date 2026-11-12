"""Exact current-production prompts and unchanged historical/schema commitments."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import pytest
from anthropic_fixture import case as anthropic_case
from gemini_fixture import case as gemini_case
from native_transport_fixture import MECHANISMS, context_for
from test_presentation_revision import rubric
from test_production_transport import case as openai_case
from vertex_fixture import case as vertex_case

from judgmentllm.anthropic_prompts import render_anthropic_request
from judgmentllm.anthropic_transport import (
    _qualification_context as anthropic_context,
)
from judgmentllm.anthropic_transport import build_output_commitment_v5
from judgmentllm.gemini_prompts import render_gemini_request
from judgmentllm.gemini_transport import _qualification_context as gemini_context
from judgmentllm.gemini_transport import build_output_commitment_v6
from judgmentllm.models import Exemplar, PairTask, ResponseItem
from judgmentllm.native_prompts import render_request_v3
from judgmentllm.output_contracts import (
    PairwiseOutputContract,
    RubricOutputContract,
    validate_pairwise_contract_output,
    validate_rubric_contract_output,
)
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.planning import plan_record
from judgmentllm.presentation import PresentationVersion
from judgmentllm.production_prompts import render_production_request
from judgmentllm.production_transport import (
    _qualification_context as openai_context,
)
from judgmentllm.production_transport import build_output_commitment_v4
from judgmentllm.transport import build_output_commitment_v3
from judgmentllm.vertex_prompts import render_vertex_request
from judgmentllm.vertex_transport import _qualification_context as vertex_context
from judgmentllm.vertex_transport import build_output_commitment_v7

FIXTURES = Path(__file__).parent / "fixtures/output-instructions"
VARIANTS = {
    "rubric_scores": (False, "together", False, False),
    "rubric_together": (True, "together", False, False),
    "rubric_scores_first": (True, "scores_first", False, False),
    "rubric_explanations_first": (True, "explanations_first", False, False),
    "cj_winner": (False, "together", True, False),
    "cj_explanation": (True, "together", True, False),
    "cj_ties": (False, "together", True, True),
    "cj_ties_explanation": (True, "together", True, True),
}
ROUTES = {
    "openai.responses": {"endpoint": "responses"},
    "openai.chat": {"endpoint": "chat.completions"},
    "anthropic": {},
    "gemini.json": {"mechanism": "gemini.generate_content.response_json_schema"},
    "gemini.schema": {"mechanism": "gemini.generate_content.response_schema"},
    "vertex.json": {"mechanism": "vertex.generate_content.response_json_schema"},
    "vertex.schema": {"mechanism": "vertex.generate_content.response_schema"},
}
PROVIDERS: dict[str, tuple[Callable[..., Any], ...]] = {
    "openai": (
        openai_case,
        openai_context,
        build_output_commitment_v4,
        render_production_request,
    ),
    "anthropic": (
        anthropic_case,
        anthropic_context,
        build_output_commitment_v5,
        render_anthropic_request,
    ),
    "gemini": (
        gemini_case,
        gemini_context,
        build_output_commitment_v6,
        render_gemini_request,
    ),
    "vertex": (
        vertex_case,
        vertex_context,
        build_output_commitment_v7,
        render_vertex_request,
    ),
}
CANARY = "  User-authored protected rubric.\n\nKeep this whitespace. 雪\r\n"
TEXT_FORMAT = (
    "Return one JSON object matching the schema below. The response must start "
    "with { and end with }. Do not use Markdown code fences or include text "
    "outside the JSON object."
)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Prompt tests must not access credentials or network")

    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr("judgmentllm._auth._resolve_credentials", denied)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "synthetic-output-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.setenv("JUDGMENTLLM_VERTEX_AUTH_MODE", "api_key")


def rendered_case(
    directory: Path,
    route: str,
    variant: str,
    policy: Literal["text", "native"],
    presentation_version: PresentationVersion = 2,
    *,
    reverse: bool = True,
    custom: bool = False,
) -> Any:
    """Use real output builders/renderers with only synthetic qualification scope."""
    factory, context_factory, build, render = PROVIDERS[route.split(".")[0]]
    explanations, layout, pairwise, ties = VARIANTS[variant]
    spec, dep, records = factory(
        directory,
        policy=policy,
        layout=layout,
        explanations=explanations,
        pairwise=pairwise,
        ties=ties,
        **ROUTES[route],
    )
    if not pairwise:
        spec.rubric = rubric()
        spec.rubric.description = CANARY
    else:
        spec.criteria[0].description = CANARY
    records["001"].text = CANARY
    spec.exemplars = [Exemplar(exemplar_id="anchor", text=CANARY)]
    record = dep.request.pair if pairwise else records["001"]
    if pairwise and not reverse:
        record = record.model_copy(update={"presented_order": ["001", "002"]})
    template = None
    if custom:
        spec.prompt = spec.prompt.model_copy(
            update={"preset": None, "file": directory / "custom.j2"}
        )
    if ties or custom:
        body = (
            "{{ criterion }}\n\n{{ first_response }}\n\n{{ second_response }}"
            if pairwise
            else "{{ rubric }}\n\n{{ response }}"
        )
        template = (
            body
            + "\n\n{{ exemplars }}\n\n## Output Format\n\n{{ output_instructions }}\n"
        )
    request = next(plan_record(spec, record, template_text=template))
    context = context_factory(spec, source_commit="a" * 40)
    output = build(spec, request, context, presentation_version=presentation_version)

    def lookup(kind: str, ident: str) -> Any:
        return record if kind == "pair" else records[ident]

    return render(
        spec,
        template,
        request,
        lookup,
        output,
    )


def output_section(text: str) -> str:
    section = text[text.index("## Output Format") :]
    # Default templates put the student essay(s) after the output block.
    return section.split("\n\n## ", 1)[0].rstrip("\n")


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("policy", ["text", "native"])
@pytest.mark.parametrize("presentation_version", [1, 2])
def test_actual_production_prompts_and_contract_bytes(
    tmp_path: Path,
    route: str,
    variant: str,
    policy: Literal["text", "native"],
    presentation_version: PresentationVersion,
) -> None:
    rendered = rendered_case(tmp_path, route, variant, policy, presentation_version)
    baseline = json.loads((FIXTURES / "baseline.json").read_text(encoding="utf-8"))
    previous = baseline["cases"][f"{route}/{variant}/{policy}/{presentation_version}"]
    # Includes canonical schema bytes, native projection/configuration, selection,
    # qualification scope, metadata and hashes: only rendered instructions change.
    assert (
        bytes_digest(canonical_bytes(rendered.output_commitment)) == previous["output"]
    )
    if presentation_version == 1:
        assert bytes_digest(rendered.text.encode("utf-8")) == previous["prompt"]
        return
    assert bytes_digest(rendered.text.encode("utf-8")) != previous["prompt"]
    golden = json.loads((FIXTURES / "prompts.json").read_text(encoding="utf-8"))
    assert rendered.text == golden[f"{variant}/{policy}"]
    section = output_section(rendered.text)
    assert rendered.text.count("## Output Format") == 1
    assert section.startswith("## Output Format\n\nReturn ")
    assert "\n\n\n" not in section and "```" not in section
    assert "protected rubric" not in section
    if policy == "text":
        assert TEXT_FORMAT in section
        assert section.endswith(
            "Required JSON schema:\n\n" + rendered.output_commitment.schema_text
        )
    else:
        assert "response must start" not in section
        assert "Required JSON schema:" not in section
        assert rendered.output_commitment.schema_text not in section
    assert rendered.text.count(CANARY) == 3


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("policy", ["text", "native"])
@pytest.mark.parametrize("reverse", [False, True])
def test_custom_templates_content_and_presented_winner_mapping(
    tmp_path: Path,
    variant: str,
    policy: Literal["text", "native"],
    reverse: bool,
) -> None:
    rendered = rendered_case(
        tmp_path, "anthropic", variant, policy, reverse=reverse, custom=True
    )
    assert rendered.text.count("## Output Format") == 1
    assert rendered.text.count(CANARY) == 3
    contract = rendered.output_commitment.contract
    if isinstance(contract, PairwiseOutputContract):
        raw: dict[str, Any] = {"winner_id": "essay_01"}
        if contract.allow_ties:
            raw["tie"] = False
        if contract.explanations:
            raw["explanation"] = "Specific essay evidence supports this judgment."
        assert validate_pairwise_contract_output(raw, contract).winner_id == (
            "002" if reverse else "001"
        )
        with pytest.raises(ValueError):
            validate_pairwise_contract_output({**raw, "winner_id": "001"}, contract)
        with pytest.raises(ValueError):
            validate_pairwise_contract_output(
                {**raw, "winner_id": None, "tie": False}, contract
            )
        if contract.allow_ties:
            assert validate_pairwise_contract_output(
                {**raw, "winner_id": None, "tie": True}, contract
            ).tie
    else:
        assert isinstance(contract, RubricOutputContract)
        assert len(contract.traits) == 12
        scores = [
            {"trait_id": trait.trait_id, "category_label": "0"}
            for trait in contract.traits
        ]
        raw = {"scores": scores}
        if contract.explanations:
            if contract.output_order == "together":
                for score in scores:
                    score["explanation"] = "Specific evidence supports this category."
            else:
                raw["explanations"] = [
                    {"trait_id": trait.trait_id, "text": "Specific evidence."}
                    for trait in contract.traits
                ]
        validate_rubric_contract_output(raw, contract)
        with pytest.raises(ValueError):
            validate_rubric_contract_output({**raw, "scores": scores[:-1]}, contract)
        with pytest.raises(ValueError):
            validate_rubric_contract_output(
                {**raw, "scores": [scores[0]] * 12}, contract
            )
        with pytest.raises(ValueError):
            validate_rubric_contract_output(
                {**raw, "scores": [{**scores[0], "category_label": 0}, *scores[1:]]},
                contract,
            )

    def validate(value: object) -> None:
        if isinstance(contract, PairwiseOutputContract):
            validate_pairwise_contract_output(value, contract)
        else:
            validate_rubric_contract_output(value, contract)

    with pytest.raises(ValueError):
        validate({**raw, "extra": "forbidden"})
    with pytest.raises(ValueError):
        validate("```json\n" + json.dumps(raw) + "\n```")


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("mechanism", MECHANISMS)
@pytest.mark.parametrize("policy", ["text", "native"])
def test_synthetic_current_repetition_preserves_exact_schema_bytes(
    tmp_path: Path,
    variant: str,
    mechanism: str,
    policy: Literal["text", "native"],
) -> None:
    from test_transport_rendering import fixture_request

    source = rendered_case(tmp_path, "anthropic", variant, "native")
    contract = source.output_commitment.contract
    spec, request, records, template = fixture_request(mechanism, contract)
    spec.prompt.structured_output = policy
    output = build_output_commitment_v3(
        spec, request, context_for(mechanism), presentation_version=2
    )

    def lookup(
        kind: Literal["response", "pair"], ident: str
    ) -> ResponseItem | PairTask:
        if kind == "pair":
            assert request.pair is not None
            return request.pair
        return records[ident]

    rendered = render_request_v3(
        spec,
        template,
        request,
        lookup,
        output,
    )
    assert output.schema_text == source.output_commitment.schema_text
    if policy == "text":
        assert output.repetition == "canonical_neutral"
        assert TEXT_FORMAT in rendered.text
        assert "Required JSON schema:\n\n" + output.schema_text in rendered.text
    else:
        if mechanism.startswith(("together.", "ollama.")):
            assert (
                output.repetition == "projected_native"
                and output.projection is not None
            )
            assert (
                "Native schema guidance (full requirements above still apply):\n\n"
                + output.projection.native_schema_text
            ) in rendered.text
        else:
            assert output.repetition == "omit"
        assert "Required JSON schema:" not in rendered.text
        assert "response must start" not in rendered.text
