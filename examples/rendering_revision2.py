"""Exact offline writing-task prompts and generated schemas; synthetic essays only.

Run with --output DIR. No SDK, credential access or provider submission occurs.
These illustrative descriptors are not a complete writing-study package.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from judgmentllm.io import write_results_jsonl
from judgmentllm.models import (
    Level,
    PairTask,
    PairwiseCriterion,
    ResponseItem,
    Rubric,
    Trait,
)
from judgmentllm.output_contracts import (
    PairwiseOutputContract,
    validate_pairwise_contract_output,
)
from judgmentllm.prompts import render_prompt
from judgmentllm.r_exports import export_pairwise_r_csv
from judgmentllm.results import PairwiseResult, normalize_pairwise_result

WRITING_TASK = """The following writing task was provided to students. The time limit was 15 minutes.

“The internet makes traveling unnecessary.”

What do you think?

Write an argumentative essay in English, in which you discuss the statement."""


def generate(directory: Path) -> list[PairwiseResult]:
    """Write exact rendered UTF-8 text/schema bytes and original-ID audit results."""
    directory.mkdir(parents=True, exist_ok=True)
    rubric = Rubric(
        rubric_id="illustration",
        name="Illustrative rubric",
        description="Use only the stated descriptors.",
        traits=[
            Trait(
                trait_id="introductory_sentence",
                name="Introductory Sentence",
                description="",
                levels=[
                    Level(label="0", description="Criterion not yet fulfilled."),
                    Level(
                        label="1",
                        description="Any one of the following:\n\n- There is an introductory sentence, but it does not align with the topic of the writing prompt.\n- There is an introductory sentence, but it does not align with the rest of the essay.\n- There is an introductory sentence, but it does not align with the topic or the rest of the essay.",
                    ),
                    Level(
                        label="2",
                        description="There is an introductory sentence (or several), which leads to the essay’s topic.",
                    ),
                ],
            ),
            Trait(
                trait_id="synthetic_support",
                name="Supporting Evidence (synthetic illustration)",
                description="Assess the support for the stated position.",
                levels=[
                    Level(label="0", description="No supporting example is provided."),
                    Level(label="1", description="A supporting example is provided."),
                    Level(
                        label="2",
                        description="A supporting example is provided and connected to the stated position.",
                    ),
                ],
            ),
        ],
    )
    left = ResponseItem(
        item_id="001",
        text="The internet can replace some journeys, but it cannot replace every experience. A video call lets relatives speak without a long flight.\n\nHowever, visiting a city also means hearing its streets and meeting people outside a screen. Online information helps us choose a journey; it does not make every journey unnecessary.",
    )
    right = ResponseItem(
        item_id="002",
        text="The internet shows many places. I can see a museum online and I do not have to buy a ticket.\n\nTravel takes time. Therefore the internet makes traveling unnecessary.",
    )
    context = "## Writing Task\n\n" + WRITING_TASK + "\n\n"
    rubric_template = (
        context
        + "{{ rubric }}\n\n## Student Essay\n\n{{ response }}\n\n{{ output_instructions }}\n"
    )
    pair_template = (
        context
        + "{{ criterion }}\n\n## Essays\n\n{{ first_response }}\n\n{{ second_response }}\n\n{{ output_instructions }}\n"
    )
    for name, traits, explanations in [
        ("one-trait", rubric.traits[:1], False),
        ("grouped", rubric.traits, False),
        ("grouped-explanations", rubric.traits, True),
    ]:
        rendered = render_prompt(
            rubric=rubric,
            traits=traits,
            response=left,
            explanations=explanations,
            template=rubric_template,
        )
        assert rendered.output_commitment is not None
        (directory / f"{name}.txt").write_bytes(rendered.text.encode("utf-8"))
        (directory / f"{name}.schema.json").write_bytes(
            rendered.output_commitment.schema_text.encode("utf-8")
        )
    criterion = PairwiseCriterion(
        trait_id="overall_writing_quality",
        name="Overall Writing Quality",
        description="Compare how clearly each essay states and supports its position on the writing task.",
    )
    results: list[PairwiseResult] = []
    for name, reverse, explanations in [
        ("cj-forced", False, False),
        ("cj-explanations", False, True),
        ("cj-reversed", True, False),
    ]:
        pair = PairTask(
            pair_id="0001",
            pair_uid=name,
            left_item_id=left.item_id,
            right_item_id=right.item_id,
            trait_id=criterion.trait_id,
            presented_order=[right.item_id, left.item_id]
            if reverse
            else [left.item_id, right.item_id],
            is_reversal=reverse,
            reversal_group="0001",
        )
        rendered = render_prompt(
            pair=pair,
            left=left,
            right=right,
            criterion=criterion,
            explanations=explanations,
            template=pair_template,
        )
        assert rendered.output_commitment is not None
        contract = rendered.output_commitment.contract
        assert isinstance(contract, PairwiseOutputContract)
        (directory / f"{name}.txt").write_bytes(rendered.text.encode("utf-8"))
        (directory / f"{name}.schema.json").write_bytes(
            rendered.output_commitment.schema_text.encode("utf-8")
        )
        raw = {"winner_id": "essay_02" if reverse else "essay_01"}
        if explanations:
            raw["explanation"] = (
                "The first essay connects specific examples to a qualified position."
            )
        choice = validate_pairwise_contract_output(raw, contract)
        results.append(
            normalize_pairwise_result(
                pair,
                run_id="offline-illustration",
                task_id=name,
                request_uid="request-" + name,
                attempt_id="attempt-001",
                winner_id=choice.winner_id,
                explanation=choice.explanation,
                provider="synthetic",
                endpoint="offline",
                model="illustration",
            )
        )
    export_pairwise_r_csv(directory / "presentation_pairwise_r.csv", results)
    write_results_jsonl(directory / "presentation_results.jsonl", results)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    generate(parser.parse_args().output)
