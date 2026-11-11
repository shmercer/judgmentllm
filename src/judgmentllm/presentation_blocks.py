"""Shared revision-2 Markdown blocks. Source descriptions and text stay exact."""

from __future__ import annotations

from collections.abc import Sequence

from judgmentllm.models import PairTask, PairwiseCriterion, ResponseItem, Rubric, Trait
from judgmentllm.output_contracts import PairwiseOutputContract
from judgmentllm.presentation import ESSAY_ALIASES

Content = tuple[str, str, str]


def rubric_blocks(traits: Sequence[Trait], rubric: Rubric | None) -> list[Content]:
    positions = (
        {trait.trait_id: index for index, trait in enumerate(rubric.traits, 1)}
        if rubric is not None
        else {}
    )
    blocks: list[Content] = []
    for trait in traits:
        ordinal = positions.get(trait.trait_id)
        title = f"{ordinal}. {trait.name}" if ordinal is not None else trait.name
        parts = [f"### {title}\nTrait ID: `{trait.trait_id}`"]
        if trait.description:
            parts.append(trait.description)
        # Numeric labels are scores only when they name their actual consecutive
        # codes. Textual/nonuniform mappings retain categorical label semantics.
        try:
            numeric = [float(level.label) for level in trait.levels]
            scores = numeric == trait.numeric_values and all(
                high - low == 1 for low, high in zip(numeric, numeric[1:], strict=False)
            )
        except ValueError:
            scores = False
        for level in reversed(trait.levels):
            heading = (
                f"**Score {level.label}**"
                if scores
                else f"**Category `{level.label}`**"
            )
            parts.append(
                heading + ("\n\n" + level.description if level.description else "")
            )
        content = "\n\n".join(parts)
        if not blocks:
            guidance = (
                "\n\n" + rubric.description if rubric and rubric.description else ""
            )
            content = "## Rubric" + guidance + "\n\n" + content
        blocks.append(("trait", trait.trait_id, content))
    return blocks


def criterion_blocks(criterion: PairwiseCriterion) -> list[Content]:
    return [
        (
            "criterion",
            criterion.trait_id,
            f"## Comparison Criterion\n\n### {criterion.name}\n"
            f"Criterion ID: `{criterion.trait_id}`\n\n{criterion.description}",
        )
    ]


def essay_blocks(
    pair: PairTask, left: ResponseItem, right: ResponseItem
) -> list[Content]:
    items = {left.item_id: left, right.item_id: right}
    return [
        (position, alias, f"{alias}:\n{items[identifier].text}")
        for position, alias, identifier in zip(
            ("first", "second"), ESSAY_ALIASES, pair.presented_order, strict=True
        )
    ]


def pairwise_instructions(contract: PairwiseOutputContract) -> str:
    fields = '"winner_id"'
    if contract.allow_ties:
        fields += ' and "tie"'
    if contract.explanations:
        fields += ' and "explanation"'
    parts = [
        "## Output Format",
        f"Return only a JSON object containing {fields}.",
        'The value of "winner_id" must be exactly "essay_01" or "essay_02", '
        "identifying the essay that better fulfills the comparison criterion.",
    ]
    if contract.allow_ties:
        parts[-1] += (
            ' For a tie, use "winner_id": null and "tie": true. '
            'For a winning essay, use "tie": false.'
        )
    else:
        parts.append("Ties are not allowed.")
    if contract.explanations:
        parts.append(
            "Provide exactly one concise sentence explaining how specific evidence "
            "in the essays supports the judgment. Give a judgment justification "
            "rather than improvement advice."
        )
        parts.append("Do not include additional fields.")
    else:
        parts.append("Do not include explanations or additional fields.")
    return "\n\n".join(parts)
