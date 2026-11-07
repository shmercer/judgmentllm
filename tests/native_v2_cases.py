"""Shared synthetic assessment sources for every Phase 4C2 route."""

from __future__ import annotations

from judgmentllm.models import Level, PairTask, Trait
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    PairwiseOutputContract,
    RubricOutputContract,
)

CANARY = "synthetic-private-native-v2-canary"


def cases() -> dict[str, AssessmentOutputContract]:
    """Sixteen stable cases; unrelated content never belongs in projections."""
    traits = (
        Trait(
            trait_id="001",
            name=CANARY,
            description=CANARY,
            levels=[Level(label="low"), Level(label="high")],
        ),
        Trait(
            trait_id='é"\\',
            name=CANARY,
            description=CANARY,
            levels=[
                Level(label='quote "'),
                Level(label="slash \\"),
                Level(label="é\n雪"),
            ],
        ),
    )
    output: dict[str, AssessmentOutputContract] = {}
    for single in (False, True):
        label = "single" if single else "grouped"
        chosen = traits[:1] if single else traits
        output[f"{label}_scores"] = RubricOutputContract(traits=chosen)
        for order in ("together", "scores_first", "explanations_first"):
            output[f"{label}_{order}"] = RubricOutputContract(
                traits=chosen,
                explanations=True,
                output_order=order,
            )
    for ties in (False, True):
        for explain in (False, True):
            output[f"pairwise_ties_{ties}_explanations_{explain}"] = (
                PairwiseOutputContract(
                    pair=PairTask(
                        pair_id="pair-01",
                        pair_uid="occurrence-01",
                        trait_id="preference",
                        left_item_id="001",
                        right_item_id="002",
                        presented_order=["002", "001"],
                        metadata={"private": CANARY},
                    ),
                    allow_ties=ties,
                    explanations=explain,
                )
            )
    output["reordered_scores"] = RubricOutputContract(traits=traits[::-1])
    output["reordered_explanations_first"] = RubricOutputContract(
        traits=traits[::-1],
        explanations=True,
        output_order="explanations_first",
    )
    output["escaped_single"] = RubricOutputContract(traits=traits[1:])
    output["unicode_pairwise"] = PairwiseOutputContract(
        pair=PairTask(
            pair_id="pair-雪",
            pair_uid="occurrence-é",
            trait_id="preference",
            left_item_id='001"\\',
            right_item_id="002雪",
            presented_order=["002雪", '001"\\'],
            metadata={"private": CANARY},
        ),
        allow_ties=True,
        explanations=True,
    )
    return output
