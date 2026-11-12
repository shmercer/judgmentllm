"""One rendering path for offline previews and immutable request preparation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from judgmentllm.config import RunSpecification, RunSpecV2
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.output_commitments import SchemaSelectionV2, build_output_commitment
from judgmentllm.planning import PlannedRequest
from judgmentllm.presentation import PresentationVersion
from judgmentllm.prompts import RenderedAssessmentPrompt, render_prompt


def render_request(
    spec: RunSpecification,
    template: str | None,
    request: PlannedRequest,
    lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask],
    *,
    selection: SchemaSelectionV2 | None = None,
    presentation_version: PresentationVersion = 2,
) -> RenderedAssessmentPrompt:
    """Render exact planned membership using supplied verified source lookup."""
    commitment = (
        build_output_commitment(
            spec, request, selection, presentation_version=presentation_version
        )
        if isinstance(spec, RunSpecV2) or presentation_version == 2
        else None
    )
    options: dict[str, Any] = dict(
        exemplars=spec.exemplars,
        explanations=spec.execution.explanations,
        allow_ties=spec.allow_ties,
        preset=spec.prompt.preset,
        template=template,
        advanced=spec.prompt.advanced,
        limits=spec.prompt.limits,
        presentation_version=presentation_version,
    )
    version: Literal[1, 2] = 2 if isinstance(spec, RunSpecV2) else 1
    if request.pair is not None:
        pair = lookup("pair", request.pair.pair_uid)
        assert isinstance(pair, PairTask)
        left, right = (
            lookup("response", pair.left_item_id),
            lookup("response", pair.right_item_id),
        )
        assert isinstance(left, ResponseItem) and isinstance(right, ResponseItem)
        criterion = next(c for c in spec.criteria if c.trait_id == pair.trait_id)
        return render_prompt(
            pair=pair,
            left=left,
            right=right,
            criterion=criterion,
            renderer_version=version,
            output_commitment=commitment,
            **options,
        )
    assert request.response is not None and spec.rubric is not None
    response = lookup("response", request.response.item_id)
    assert isinstance(response, ResponseItem)
    traits = {trait.trait_id: trait for trait in spec.rubric.traits}
    return render_prompt(
        rubric=spec.rubric,
        traits=[traits[trait_id] for trait_id in request.trait_ids],
        response=response,
        renderer_version=version,
        output_commitment=commitment,
        **options,
    )
