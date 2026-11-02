"""Synthetic assessment schema and category-coding checks."""

from typing import Any

import pytest
from pydantic import ValidationError

from judgmentllm.models import (
    Exemplar,
    Level,
    PairTask,
    PairwiseCriterion,
    ResponseItem,
    Rubric,
    Trait,
    VersionedModel,
    validate_exemplars,
)


def trait(labels: list[Any], codes: list[float] | None = None) -> Trait:
    return Trait(
        trait_id="quality",
        name="Quality",
        description="Synthetic criteria",
        levels=[
            Level.model_validate(
                {"label": label, **({"numeric_value": codes[index]} if codes else {})}
            )
            for index, label in enumerate(labels)
        ],
    )


def rubric() -> Rubric:
    return Rubric(
        rubric_id="rubric", name="Synthetic rubric", traits=[trait(["low", "high"])]
    )


def pair(**changes: Any) -> PairTask:
    data: dict[str, Any] = {
        "pair_id": "AB",
        "pair_uid": "AB-0",
        "left_item_id": "A",
        "right_item_id": "B",
        "trait_id": "quality",
    }
    return PairTask.model_validate(data | changes)


@pytest.mark.parametrize("version", [True, False, 1.0, "1", 0, 2, None])
def test_schema_version_requires_supported_integer(version: object) -> None:
    with pytest.raises(ValidationError, match="supported integer 1"):
        VersionedModel.model_validate({"schema_version": version})


def test_schema_version_and_unknown_fields() -> None:
    assert VersionedModel().model_dump() == {"schema_version": 1}
    with pytest.raises(ValidationError, match="Extra inputs"):
        ResponseItem.model_validate(
            {"item_id": "x", "text": "synthetic", "secret": "no"}
        )
    with pytest.raises(ValidationError):
        ResponseItem.model_validate({"item_id": "x", "text": 2})


@pytest.mark.parametrize("identifier", ["", " ", " x", "x ", "x\n", "a\tb", "a\x7fb"])
def test_invalid_identifiers(identifier: str) -> None:
    with pytest.raises(ValidationError, match="identifier"):
        ResponseItem(item_id=identifier, text="")


def test_empty_response_representable_metadata_roundtrip() -> None:
    item = ResponseItem(item_id="item", text="", metadata={"nested": [1, True, None]})
    assert ResponseItem.model_validate_json(item.model_dump_json()) == item


@pytest.mark.parametrize(
    "label", [True, False, {}, [], None, "", "  ", float("nan"), float("inf")]
)
def test_invalid_category_scalar(label: object) -> None:
    with pytest.raises(ValidationError, match="category label"):
        Level.model_validate({"label": label})


@pytest.mark.parametrize(
    "labels,codes,kind",
    [
        ([1, 3, 7], [1.0, 3.0, 7.0], "numeric"),
        (["-1", "0.5", "2e1"], [-1.0, 0.5, 20.0], "numeric"),
        (["low", "middle", "high"], [0.0, 1.0, 2.0], "indices"),
        ([0, "high"], [0.0, 1.0], "indices"),
    ],
)
def test_default_coding(labels: list[Any], codes: list[float], kind: str) -> None:
    selected = trait(labels)
    assert selected.numeric_values == codes
    assert selected.coding_kind == kind
    assert [level.label for level in selected.resolved_levels] == [
        str(label) for label in labels
    ]


def test_mixed_labels_warn_only_when_numeric_code_changes() -> None:
    with pytest.warns(UserWarning, match="zero-based indices"):
        selected = trait([7, "high"])
    assert selected.numeric_values == [0.0, 1.0]


def test_explicit_mapping_overrides_numeric_labels_and_roundtrips() -> None:
    selected = trait([9, 2], [-5, 8])
    assert selected.numeric_values == [-5, 8]
    assert selected.coding_kind == "explicit"
    assert selected.levels[0].source_label == 9
    assert Trait.model_validate_json(selected.model_dump_json()) == selected
    assert selected.resolved_levels[1].numeric_value == 8


@pytest.mark.parametrize(
    "labels", [["x", "x"], [1, "1"], [1, "1.0"], [3, 2], ["NaN", "Inf"]]
)
def test_duplicate_or_nonmonotonic_numeric_labels(labels: list[Any]) -> None:
    with pytest.raises(ValidationError, match="duplicate|increasing|finite"):
        trait(labels)


@pytest.mark.parametrize(
    "values", [[1, 1], [2, 1], [1, float("inf")], [float("nan"), 3], [True, 2]]
)
def test_invalid_explicit_values(values: list[Any]) -> None:
    with pytest.raises(ValidationError):
        trait(["low", "high"], values)


def test_partial_mapping_and_source_mismatch() -> None:
    with pytest.raises(ValidationError, match="every level"):
        Trait(
            trait_id="q",
            name="Q",
            description="",
            levels=[Level(label="a", numeric_value=0), Level(label="b")],
        )
    with pytest.raises(ValidationError, match="same category"):
        Level(label="1", source_label=2)
    with pytest.raises(ValidationError):
        trait(["single"])


def test_duplicate_trait_ids_and_empty_rubric() -> None:
    with pytest.raises(ValidationError, match="duplicate trait"):
        Rubric(rubric_id="r", name="R", traits=[trait([0, 1]), trait([1, 2])])
    with pytest.raises(ValidationError):
        Rubric(rubric_id="r", name="R", traits=[])


def test_many_traits_and_exemplars_have_no_schema_limits() -> None:
    traits = [
        Trait(
            trait_id=f"q{i}",
            name="Q",
            description="",
            levels=[Level(label="a"), Level(label="b")],
        )
        for i in range(300)
    ]
    assessment = Rubric(rubric_id="r", name="R", traits=traits)
    examples = [
        Exemplar(exemplar_id=f"e{i}", text=f"Synthetic {i}") for i in range(1500)
    ]
    validate_exemplars(examples, assessment)
    assert len(assessment.traits) == 300
    assert len(examples) == 1500


@pytest.mark.parametrize("count", [0, 1, 5])
def test_exemplar_assignment_and_order(count: int) -> None:
    examples = [
        Exemplar(
            exemplar_id=f"e{i}",
            text="synthetic",
            scores={"quality": "high"},
            rationales={"quality": "clear"},
        )
        for i in range(count)
    ]
    validate_exemplars(examples, rubric())
    assert [value.exemplar_id for value in examples] == [f"e{i}" for i in range(count)]


def test_exemplar_invalid_assignments() -> None:
    example = Exemplar(exemplar_id="e", text="synthetic")
    with pytest.raises(ValueError, match="duplicate exemplar"):
        validate_exemplars([example, example], rubric())
    with pytest.raises(ValueError, match="unknown traits"):
        validate_exemplars(
            [Exemplar(exemplar_id="e", text="synthetic", rationales={"missing": "x"})],
            rubric(),
        )
    with pytest.raises(ValueError, match="unknown category"):
        validate_exemplars(
            [
                Exemplar(
                    exemplar_id="e", text="synthetic", scores={"quality": "missing"}
                )
            ],
            rubric(),
        )
    criterion = PairwiseCriterion(
        trait_id="quality", name="Quality", description="Choose clearer."
    )
    with pytest.raises(ValueError, match="ordinal scores"):
        validate_exemplars(
            [Exemplar(exemplar_id="e", text="synthetic", scores={"quality": "high"})],
            criteria=[criterion],
        )
    with pytest.raises(ValueError, match="duplicate criterion"):
        validate_exemplars([], criteria=[criterion, criterion])
    validate_exemplars(
        [Exemplar(exemplar_id="e", text="synthetic", rationales={"quality": "clear"})],
        criteria=[criterion],
    )


def test_pair_order_defaults_and_reversal() -> None:
    selected = pair()
    assert selected.original_order == selected.presented_order == ["A", "B"]
    reversed_pair = pair(
        presented_order=["B", "A"],
        is_reversal=True,
        source_pair_uid="AB-0",
        pair_uid="AB-0-r",
    )
    assert reversed_pair.original_order == ["A", "B"]
    assert reversed_pair.presented_order == ["B", "A"]
    assert reversed_pair.pair_uid != selected.pair_uid
    assert (
        PairTask.model_validate_json(reversed_pair.model_dump_json()) == reversed_pair
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"right_item_id": "A"},
        {"original_order": ["B", "A"]},
        {"presented_order": ["A", "A"]},
        {"presented_order": ["A"]},
        {"presented_order": ["A", "C"]},
        {"repetition_index": -1},
        {"repetition_index": True},
    ],
)
def test_invalid_pairs(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        pair(**changes)


@pytest.mark.parametrize("payload", ["invalid", {}])
def test_level_invalid_container_or_missing_label(payload: object) -> None:
    with pytest.raises(ValidationError):
        Level.model_validate(payload)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
def test_nested_metadata_must_contain_finite_json_values(number: float) -> None:
    with pytest.raises(ValidationError, match="finite"):
        ResponseItem(
            item_id="a", text="synthetic", metadata={"nested": {"values": [number]}}
        )


@pytest.mark.parametrize("field", ["original_order", "presented_order"])
def test_explicit_empty_pair_order_is_invalid(field: str) -> None:
    with pytest.raises(ValidationError, match="original_order|presented_order"):
        pair(**{field: []})
