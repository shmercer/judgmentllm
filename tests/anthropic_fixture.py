"""Private deterministic Anthropic fixture helpers; no provider authority."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from assessment_fixture import assessment_case

from judgmentllm.anthropic_transport import (
    AnthropicProductionContext,
    PreparedRequestV5,
    _qualification_context,
    prepare_production_request,
    production_context,
)
from judgmentllm.config import GenerationSpec, RunSpecV3
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.transport import physical_identity


def case(
    tmp_path: Path,
    *,
    endpoint: str = "messages",
    policy: Literal["auto", "native", "text"] = "text",
    layout: Literal["together", "scores_first", "explanations_first"] = "together",
    explanations: bool = False,
    grouped: bool = True,
    pairwise: bool = False,
    ties: bool = False,
    generation: GenerationSpec | None = None,
    strict: bool = False,
) -> tuple[RunSpecV3, RequestDependency, dict[str, ResponseItem]]:
    mechanism = "anthropic.messages.output_config.format"
    return assessment_case(
        tmp_path,
        provider="anthropic",
        endpoint=endpoint,
        mechanism=mechanism,
        policy=policy,
        layout=layout,
        explanations=explanations,
        grouped=grouped,
        pairwise=pairwise,
        ties=ties,
        generation=generation,
        strict=strict,
    )


def wire_for(
    tmp_path: Path, *, probe: bool = False, **kwargs: Any
) -> tuple[PreparedRequestV5, RunSpecV3, RequestDependency, AnthropicProductionContext]:
    spec, dep, records = case(tmp_path, **kwargs)
    context = (
        _qualification_context(spec, source_commit="a" * 40)
        if probe
        else production_context(spec)
    )

    def lookup(kind: str, ident: str) -> ResponseItem | PairTask:
        if kind == "pair":
            assert dep.request.pair is not None
            return dep.request.pair
        return records[ident]

    template = (
        "Compare.\n{{ criterion }}\n{{ first_response }}\n{{ second_response }}\n{{ output_instructions }}"
        if spec.allow_ties
        else None
    )
    wire = prepare_production_request(
        spec,
        dep,
        physical_identity("run-001", dep.request.request_uid, "attempt-001"),
        context,
        lookup=cast(Any, lookup),
        template=template,
    )
    return wire, spec, dep, context
