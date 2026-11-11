"""Revision-2 rendering and CJ wire semantics; all content is synthetic."""

from __future__ import annotations

import json
from typing import Any

import pytest

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
    generate_output_schema,
    validate_pairwise_contract_output,
)
from judgmentllm.prompts import PromptError, render_prompt


def rubric() -> Rubric:
    return Rubric(
        rubric_id="technical_rubric_canary",
        name="technical_name_canary",
        description="Apply only the supplied descriptors.\nKeep this shared guidance.",
        traits=[
            Trait(
                trait_id=f"trait_{number:02}",
                name="Rephrasing of the Thesis Statement"
                if number == 9
                else f"Dimension {number}",
                description="",
                levels=[
                    Level(label="0", description="Criterion not yet fulfilled."),
                    Level(
                        label="1",
                        description="Any one of the following:\n\n- First alternative.\n- Second alternative.  ",
                    ),
                    Level(
                        label="2",
                        description="Both conditions:\n- First condition.\n- Second condition.",
                    ),
                ],
            )
            for number in range(1, 13)
        ],
    )


@pytest.mark.parametrize("explanations", [False, True])
def test_all_traits_original_numbers_exact_descriptors_and_no_student_id(
    explanations: bool,
) -> None:
    source = rubric()
    essay = ResponseItem(
        item_id="private_student_canary",
        text="  First paragraph.\n\n\tSecond paragraph.\r\nFinal line.  \n",
    )
    rendered = render_prompt(rubric=source, response=essay, explanations=explanations)
    assert rendered.text.count("## Rubric\n") == 1
    assert "technical_rubric_canary" not in rendered.text
    assert "technical_name_canary" not in rendered.text
    assert essay.item_id not in rendered.text
    assert "## Student Essay\n\n" + essay.text in rendered.text
    assert source.description in rendered.text
    cursor = 0
    for number, trait in enumerate(source.traits, 1):
        cursor = rendered.text.index(f"### {number}. {trait.name}", cursor) + 1
        assert f"Trait ID: `{trait.trait_id}`" in rendered.text
        block = rendered.text[rendered.text.index(f"### {number}.") :]
        assert (
            block.index("**Score 2**")
            < block.index("**Score 1**")
            < block.index("**Score 0**")
        )
        for level in trait.levels:
            assert level.description in block
    assert "Ordered categories" not in rendered.text
    assert " -> " not in rendered.text
    assert rendered.text.count("Any one of the following:") == 12
    schema = rendered.output_schema
    scores: Any = schema["properties"]
    assert [
        entry["properties"]["category_label"]["enum"]
        for entry in scores["scores"]["items"]["oneOf"]
    ] == [["0", "1", "2"]] * 12


def test_independent_parent_number_and_standalone_without_invented_number() -> None:
    source = rubric()
    trait = source.traits[8]
    essay = ResponseItem(
        item_id="001", text="Text containing an author-written ID 001."
    )
    single = render_prompt(rubric=source, traits=[trait], response=essay)
    assert "### 9. Rephrasing of the Thesis Statement" in single.text
    assert "### 1." not in single.text
    standalone = render_prompt(traits=[trait], response=essay)
    assert "### Rephrasing of the Thesis Statement" in standalone.text
    assert essay.text in standalone.text


@pytest.mark.parametrize(
    "labels,codes",
    [
        (["Beginning", "Developing", "Accomplished"], [1, 3, 8]),
        (["0", "1", "2"], [0, 2, 7]),
        (["1", "3", "8"], [1, 3, 8]),
    ],
)
def test_arbitrary_scales_preserve_labels_and_mapping(
    labels: list[str], codes: list[int]
) -> None:
    trait = Trait(
        trait_id="quality",
        name="Quality",
        description="",
        levels=[
            Level(label=label, numeric_value=code, description=f"Exact {label}.")
            for label, code in zip(labels, codes, strict=True)
        ],
    )
    rendered = render_prompt(
        traits=[trait], response=ResponseItem(item_id="001", text="Essay")
    )
    assert "**Score" not in rendered.text
    assert rendered.text.index(f"**Category `{labels[2]}`**") < rendered.text.index(
        f"**Category `{labels[0]}`**"
    )
    assert trait.numeric_values == codes
    assert rendered.output_commitment is not None
    props: Any = rendered.output_schema["properties"]
    assert (
        props["scores"]["items"]["oneOf"][0]["properties"]["category_label"]["enum"]
        == labels
    )


def pair(reverse: bool = False, *, collision: bool = False) -> PairTask:
    first, second = (
        ("essay_02", "essay_01") if collision else ("student_A034", "student_B119")
    )
    return PairTask(
        pair_id="001",
        pair_uid="001-reverse" if reverse else "001-forward",
        left_item_id=first,
        right_item_id=second,
        trait_id="overall_writing_quality",
        presented_order=[second, first] if reverse else [first, second],
        is_reversal=reverse,
        reversal_group="001",
    )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "explanations,ties", [(False, False), (True, False), (False, True), (True, True)]
)
def test_cj_blocks_alias_schema_and_normalization(
    reverse: bool, explanations: bool, ties: bool
) -> None:
    occurrence = pair(reverse)
    criterion = PairwiseCriterion(
        trait_id=occurrence.trait_id,
        name="Overall Writing Quality",
        description="Exact criterion.\n\nCompare only this evidence.  ",
    )
    template = (
        "## Comparison Criterion\n\n{{ criterion }}\n## Essays\n{{ first_response }}\n\n{{ second_response }}\n## Output Format\n{{ output_instructions }}"
        if ties
        else None
    )
    rendered = render_prompt(
        pair=occurrence,
        left=ResponseItem(
            item_id=occurrence.left_item_id, text="Essay A.\n\nParagraph A."
        ),
        right=ResponseItem(
            item_id=occurrence.right_item_id, text="Essay B.\n\nParagraph B."
        ),
        criterion=criterion,
        explanations=explanations,
        allow_ties=ties,
        template=template,
    )
    assert rendered.text.count("## Comparison Criterion\n") == 1
    assert rendered.text.count("## Output Format\n") == 1
    assert criterion.description in rendered.text
    assert f"Criterion ID: `{criterion.trait_id}`" in rendered.text
    assert rendered.text.index(
        "essay_01:\nEssay B." if reverse else "essay_01:\nEssay A."
    ) < rendered.text.index("essay_02:")
    for source_id in occurrence.original_order:
        assert source_id not in rendered.text
        assert source_id not in json.dumps(rendered.output_schema)
    if ties:
        assert "Ties are not allowed" not in rendered.text
        assert '"tie": true' in rendered.text
    if explanations:
        assert "exactly one concise sentence" in rendered.text
        assert "rather than improvement advice" in rendered.text
    commitment = rendered.output_commitment
    assert commitment is not None
    contract = commitment.contract
    assert isinstance(contract, PairwiseOutputContract)
    raw: dict[str, object] = {"winner_id": "essay_02" if reverse else "essay_01"}
    if ties:
        raw["tie"] = False
    if explanations:
        raw["explanation"] = "Essay A supplies relevant evidence."
    assert (
        validate_pairwise_contract_output(json.dumps(raw), contract).winner_id
        == occurrence.left_item_id
    )
    if ties:
        raw.update(winner_id=None, tie=True)
        assert validate_pairwise_contract_output(raw, contract).winner_id is None


@pytest.mark.parametrize(
    "winner",
    [
        "student_A034",
        "unknown",
        "essay_1",
        " essay_01",
        "ESSAY_01",
        1,
        True,
        ["essay_01"],
        {"winner_id": "essay_01"},
        None,
    ],
)
def test_reject_original_unknown_and_malformed_wire_winners(winner: object) -> None:
    contract = PairwiseOutputContract(pair=pair(), presentation_version=2)
    with pytest.raises(ValueError):
        validate_pairwise_contract_output({"winner_id": winner}, contract)


def test_collision_and_historical_interpretation_are_unambiguous() -> None:
    occurrence = pair(collision=True)
    historical = PairwiseOutputContract(pair=occurrence)
    current = PairwiseOutputContract(pair=occurrence, presentation_version=2)
    assert (
        validate_pairwise_contract_output(
            '{"winner_id":"essay_01"}', historical
        ).winner_id
        == "essay_01"
    )
    assert (
        validate_pairwise_contract_output('{"winner_id":"essay_01"}', current).winner_id
        == "essay_02"
    )
    assert "presentation_version" not in historical.model_dump()
    assert current.model_dump()["presentation_version"] == 2
    assert generate_output_schema(historical) != generate_output_schema(current)


@pytest.mark.parametrize("number", range(13))
def test_any_comparison_criterion_is_preserved_without_invented_levels(
    number: int,
) -> None:
    occurrence = pair()
    criterion = PairwiseCriterion(
        trait_id=f"criterion_{number}",
        name=f"Independent criterion {number}",
        description=f"Exact standalone criterion {number}.\n\n- Preserve this author-written condition.",
    )
    occurrence.trait_id = criterion.trait_id
    rendered = render_prompt(
        pair=occurrence,
        left=ResponseItem(item_id=occurrence.left_item_id, text="Synthetic A"),
        right=ResponseItem(item_id=occurrence.right_item_id, text="Synthetic B"),
        criterion=criterion,
    )
    assert criterion.description in rendered.text
    assert f"### {criterion.name}" in rendered.text
    assert f"Criterion ID: `{criterion.trait_id}`" in rendered.text
    assert "Score" not in rendered.text and "Category" not in rendered.text


@pytest.mark.parametrize(
    "raw",
    [
        '{"winner_id":"essay_01","winner_id":"essay_02"}',
        '{"winner_id":"essay_01","tie":false}',
        '{"winner_id":"essay_01","extra":null}',
        '{"winner_id":"essay_01","explanation":null}',
    ],
)
def test_new_forced_choice_rejects_duplicate_or_additional_fields(raw: str) -> None:
    with pytest.raises(ValueError):
        validate_pairwise_contract_output(
            raw, PairwiseOutputContract(pair=pair(), presentation_version=2)
        )


@pytest.mark.parametrize(
    "placeholders",
    [("first_response", "second_response"), ("left_response", "right_response")],
)
def test_custom_placeholder_integrity_and_alias_order(
    placeholders: tuple[str, str],
) -> None:
    occurrence = pair()
    args: dict[str, Any] = dict(
        pair=occurrence,
        left=ResponseItem(item_id=occurrence.left_item_id, text="  Essay A.\n\nA2.\n"),
        right=ResponseItem(item_id=occurrence.right_item_id, text="Essay B."),
        criterion=PairwiseCriterion(
            trait_id=occurrence.trait_id, name="Overall", description=""
        ),
    )
    first, second = placeholders
    template = (
        "{{ criterion }}\n{{ "
        + first
        + " }}\n{{ "
        + second
        + " }}\n{{ output_instructions }}"
    )
    rendered = render_prompt(template=template, **args)
    assert "essay_01:\n  Essay A.\n\nA2.\n" in rendered.text
    assert occurrence.left_item_id not in rendered.text
    with pytest.raises(PromptError, match="presentation order"):
        render_prompt(
            template=template.replace(first, "PLACEHOLDER")
            .replace(second, first)
            .replace("PLACEHOLDER", second),
            **args,
        )
    with pytest.raises(PromptError):
        render_prompt(
            template=template.replace(
                "{{ " + first + " }}", "{{ " + first + " | upper }}"
            ),
            advanced=True,
            **args,
        )


@pytest.mark.parametrize("revision", [True, "2", 2.0, 0, 3])
def test_presentation_version_is_strict(revision: object) -> None:
    with pytest.raises(ValueError):
        PairwiseOutputContract.model_validate(
            {"pair": pair(), "presentation_version": revision}
        )
