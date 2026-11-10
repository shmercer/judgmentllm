"""Offline prompt preview built on the same structural plans and renderer."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from judgmentllm import gemini_types, vertex_types
from judgmentllm.config import (
    RunSpec,
    RunSpecification,
    RunSpecV2,
    RunSpecV3,
    load_run_spec,
    validate_run_spec,
)
from judgmentllm.io import read_pairs, read_responses
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.planning import CapabilityProfile, plan_record, plan_run
from judgmentllm.prompts import RenderedAssessmentPrompt
from judgmentllm.request_rendering import render_request

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    from judgmentllm.anthropic_prompts import RenderedAnthropicPromptV1
    from judgmentllm.planning import PlannedRequest
    from judgmentllm.production_prompts import RenderedProductionPromptV1


def _preview_render(
    spec: RunSpecification,
    template: str | None,
    request: PlannedRequest,
    lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask],
) -> (
    RenderedAssessmentPrompt
    | RenderedProductionPromptV1
    | RenderedAnthropicPromptV1
    | vertex_types.RenderedVertexPromptV1
    | gemini_types.RenderedGeminiPromptV1
):
    if isinstance(spec, RunSpecV3) and spec.provider == "vertex":
        from judgmentllm.vertex_prompts import render_vertex_request
        from judgmentllm.vertex_transport import build_output_commitment_v7
        from judgmentllm.vertex_transport import (
            production_context as vertex_context,
        )

        output = build_output_commitment_v7(spec, request, vertex_context(spec))
        return render_vertex_request(spec, template, request, lookup, output)
    if isinstance(spec, RunSpecV3) and spec.provider == "gemini":
        from judgmentllm.gemini_prompts import render_gemini_request
        from judgmentllm.gemini_transport import build_output_commitment_v6
        from judgmentllm.gemini_transport import (
            production_context as gemini_context,
        )

        output = build_output_commitment_v6(spec, request, gemini_context(spec))
        return render_gemini_request(spec, template, request, lookup, output)
    if isinstance(spec, RunSpecV3) and spec.provider == "anthropic":
        from judgmentllm.anthropic_prompts import render_anthropic_request
        from judgmentllm.anthropic_transport import build_output_commitment_v5
        from judgmentllm.anthropic_transport import (
            production_context as anthropic_context,
        )

        output = build_output_commitment_v5(spec, request, anthropic_context(spec))
        return render_anthropic_request(spec, template, request, lookup, output)
    if isinstance(spec, RunSpecV3):
        from judgmentllm.production_prompts import render_production_request
        from judgmentllm.production_transport import (
            build_production_output_commitment,
            production_context,
        )

        output = build_production_output_commitment(
            spec, request, production_context(spec)
        )
        return render_production_request(spec, template, request, lookup, output)
    return render_request(spec, template, request, lookup)


def preview_prompt(
    spec: RunSpecification | str | Path,
    *,
    item_id: str | None = None,
    trait_id: str | None = None,
    pair_uid: str | None = None,
    capabilities: CapabilityProfile | None = None,
) -> (
    RenderedAssessmentPrompt
    | RenderedProductionPromptV1
    | RenderedAnthropicPromptV1
    | vertex_types.RenderedVertexPromptV1
    | gemini_types.RenderedGeminiPromptV1
):
    """Render one planned request without credentials, networking, or execution.

    For grouped rubric requests, selecting a trait previews its entire fixed
    group. Pairwise input is scanned while retaining only the two required texts.
    """
    spec = (
        validate_run_spec(spec)
        if isinstance(spec, (RunSpec, RunSpecV2, RunSpecV3))
        else load_run_spec(spec)
    )
    if isinstance(spec, RunSpecV3):
        from judgmentllm.lifecycle import _profile  # pyright: ignore[reportPrivateUsage]

        capabilities = _profile(spec, capabilities)
    plan = plan_run(spec, capabilities=capabilities)
    template = (
        spec.prompt.file.read_text(encoding="utf-8") if spec.prompt.file else None
    )
    if spec.task_kind == "rubric":
        if item_id is None or pair_uid is not None:
            raise ValueError(
                "rubric preview requires item_id and does not accept pair_uid"
            )
        assert spec.rubric is not None
        group = next((group for group in plan.trait_groups if trait_id in group), None)
        if trait_id is None and len(plan.trait_groups) == 1:
            group = plan.trait_groups[0]
        if group is None:
            raise ValueError("select a known trait_id to preview its request group")
        responses = _responses(spec, {item_id})
        response = responses[item_id]
        request = next(
            request
            for request in plan_record(
                spec, response, template_text=template, capabilities=capabilities
            )
            if request.trait_ids == group
        )
        return _preview_render(
            spec, template, request, lambda kind, identifier: responses[identifier]
        )
    if pair_uid is None or item_id is not None or trait_id is not None:
        raise ValueError(
            "pairwise preview requires pair_uid and does not accept item_id/trait_id"
        )
    assert spec.pairs is not None
    pair = None
    for candidate in read_pairs(spec.pairs):
        if candidate.pair_uid == pair_uid:
            pair = candidate
    if pair is None:
        raise ValueError(f"Unknown pair_uid {pair_uid!r}")
    criterion = next(
        (
            criterion
            for criterion in spec.criteria
            if criterion.trait_id == pair.trait_id
        ),
        None,
    )
    if criterion is None:
        raise ValueError(f"Unknown pair criterion {pair.trait_id!r}")
    responses = _responses(spec, {pair.left_item_id, pair.right_item_id})
    request = next(
        plan_record(spec, pair, template_text=template, capabilities=capabilities)
    )

    def lookup(kind: str, identifier: str) -> ResponseItem | PairTask:
        return pair if kind == "pair" else responses[identifier]

    return _preview_render(spec, template, request, lookup)


def _responses(spec: RunSpecification, needed: set[str]) -> dict[str, ResponseItem]:
    found: dict[str, ResponseItem] = {}
    for response in read_responses(spec.input):
        if response.item_id in needed:
            found[response.item_id] = response
    missing = needed - found.keys()
    if missing:
        raise ValueError(f"Missing response item IDs: {', '.join(sorted(missing))}")
    return found
