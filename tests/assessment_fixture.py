"""Provider-neutral synthetic assessments for the production transport tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast

from judgmentllm.config import (
    ExecutionSpec,
    GenerationSpec,
    InputSpec,
    PromptSpecV3,
    RunSpecV3,
)
from judgmentllm.models import (
    Level,
    PairTask,
    PairwiseCriterion,
    ResponseItem,
    Rubric,
    Trait,
)
from judgmentllm.planning import plan_record
from judgmentllm.preparation.session import RequestDependency


def assessment_case(
    tmp_path: Path,
    *,
    provider: str,
    endpoint: str,
    mechanism: str,
    policy: Literal["auto", "native", "text"],
    layout: Literal["together", "scores_first", "explanations_first"],
    explanations: bool,
    grouped: bool,
    pairwise: bool,
    ties: bool,
    generation: GenerationSpec | None,
    strict: bool,
) -> tuple[RunSpecV3, RequestDependency, dict[str, ResponseItem]]:
    traits = [
        Trait(
            trait_id="01",
            name="Clarity",
            description="Synthetic clarity.",
            levels=[Level(label="low 雪"), Level(label="high é")],
        ),
        Trait(
            trait_id="02",
            name="Detail",
            description="Synthetic detail.",
            levels=[Level(label="limited"), Level(label="complete")],
        ),
    ]
    if not grouped:
        traits = traits[:1]
    responses = {
        ident: ResponseItem(
            item_id=ident, text=f"Synthetic response {ident}: a seed grows. 雪"
        )
        for ident in ("001", "002")
    }
    spec = RunSpecV3(
        task_kind="pairwise" if pairwise else "rubric",
        input=InputSpec(path=tmp_path / "responses.jsonl"),
        rubric=None
        if pairwise
        else Rubric(rubric_id="rubric-001", name="Synthetic", traits=traits),
        criteria=[
            PairwiseCriterion(
                trait_id="01", name="Clarity", description="Compare clarity."
            )
        ]
        if pairwise
        else [],
        pairs=InputSpec(path=tmp_path / "pairs.jsonl") if pairwise else None,
        provider=provider,
        endpoint=endpoint,
        model="offline-qualified-model",
        generation=generation,
        execution=ExecutionSpec(
            grouping="grouped" if grouped and not pairwise else "independent",
            explanations=explanations,
        ),
        prompt=PromptSpecV3(
            structured_output=policy,
            native_mechanism=cast(Any, mechanism),
            output_order=layout,
            file=tmp_path / "tie.j2" if ties else None,
        ),
        allow_ties=ties,
        strict_capabilities=strict,
    )
    record = (
        PairTask(
            pair_id="pair-001",
            pair_uid="pair-001-occurrence",
            left_item_id="001",
            right_item_id="002",
            trait_id="01",
            presented_order=["002", "001"],
        )
        if pairwise
        else responses["001"]
    )
    request = next(plan_record(spec, record))
    return (
        spec,
        RequestDependency(request=request, dependency_sha256="0" * 64),
        responses,
    )
