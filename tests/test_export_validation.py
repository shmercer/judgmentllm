"""Serialization rejects current invalid state instead of trusting past validation."""

from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, Field, RootModel

from judgmentllm.io import (
    export_results_csv,
    read_jsonl,
    read_results,
    read_results_csv,
    revalidate_result,
    write_jsonl,
    write_results_jsonl,
)
from judgmentllm.models import Level, PairTask, ResponseItem, Trait
from judgmentllm.results import (
    PairwiseResult,
    RubricResult,
    normalize_pairwise_result,
    normalize_rubric_result,
)

Result = RubricResult | PairwiseResult
Writer = Literal["generic_jsonl", "results_jsonl", "csv"]


def _rubric() -> RubricResult:
    trait = Trait(
        trait_id="detail",
        name="Detail",
        description="Assess synthetic detail.",
        levels=[
            Level(label="low", numeric_value=1),
            Level(label="high", numeric_value=7),
        ],
    )
    return normalize_rubric_result(
        trait,
        run_id="run",
        task_id="task",
        request_uid="request",
        attempt_id="attempt",
        item_id="001",
        category_label="low",
        probabilities=[
            {"level_label": "low", "probability": 0.25},
            {"level_label": "high", "probability": 0.75},
        ],
    )


def _pair() -> PairwiseResult:
    return normalize_pairwise_result(
        PairTask(
            pair_id="pair",
            pair_uid="occurrence",
            left_item_id="001",
            right_item_id="002",
            trait_id="detail",
            presented_order=["002", "001"],
        ),
        run_id="run",
        task_id="pair-task",
        request_uid="pair-request",
        attempt_id="attempt",
        winner_id="001",
        p_left=0.75,
        p_right=0.25,
    )


def _write(path: Path, records: Iterator[Result], writer: Writer) -> int:
    if writer == "generic_jsonl":
        return write_jsonl(path, records)
    if writer == "results_jsonl":
        return write_results_jsonl(path, records)
    return export_results_csv(path, records)


@pytest.mark.parametrize("writer", ["generic_jsonl", "results_jsonl", "csv"])
@pytest.mark.parametrize(
    "mutation",
    [
        "score",
        "score_kind",
        "id",
        "copied_score",
        "copied_extra",
        "nested_mapping",
        "nested_probability",
        "nested_extra",
        "missing_probability",
    ],
)
def test_invalid_rubric_mutations_never_serialize(
    tmp_path: Path, writer: Writer, mutation: str
) -> None:
    result = _rubric()
    assert result.probabilities is not None
    if mutation == "score":
        result.primary_score = 999
    elif mutation == "score_kind":
        result.primary_score_kind = "category_code"
    elif mutation == "id":
        result.item_id = ""
    elif mutation == "copied_score":
        result = result.model_copy(update={"primary_score": -1})
    elif mutation == "copied_extra":
        result = result.model_copy(update={"unrequested": "invalid"})
    elif mutation == "nested_mapping":
        result.resolved_levels[0].numeric_value = 2
    elif mutation == "nested_probability":
        result.probabilities[0].probability = -0.1
    elif mutation == "nested_extra":
        result.resolved_levels[0] = result.resolved_levels[0].model_copy(
            update={"unrequested": "invalid"}
        )
    else:
        result.probabilities.pop()
    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        _write(output, iter([result]), writer)
    if writer != "csv":
        assert output.read_text() == ""
    else:
        assert len(output.read_text().splitlines()) == 1  # Header only.


@pytest.mark.parametrize("writer", ["generic_jsonl", "results_jsonl", "csv"])
@pytest.mark.parametrize(
    "mutation",
    [
        "winner",
        "original_order",
        "presented_order",
        "copied_order",
        "probability",
        "tie",
        "copy_index",
    ],
)
def test_invalid_pair_mutations_never_serialize(
    tmp_path: Path, writer: Writer, mutation: str
) -> None:
    result = _pair()
    if mutation == "winner":
        result.winner_id = "003"
    elif mutation == "original_order":
        result.original_order.reverse()
    elif mutation == "presented_order":
        result.presented_order[1] = result.presented_order[0]
    elif mutation == "copied_order":
        result = result.model_copy(update={"presented_order": ["001", "unknown"]})
    elif mutation == "probability":
        result.p_left = 1.5
    elif mutation == "tie":
        result.tie = True
    else:
        result = result.model_copy(update={"repetition_index": -1})
    with pytest.raises(ValueError):
        _write(tmp_path / "invalid", iter([result]), writer)


def test_revalidation_copies_nested_records_without_changing_source() -> None:
    original = _rubric()
    checked = revalidate_result(original)
    assert checked == original and checked is not original
    assert checked.resolved_levels is not original.resolved_levels
    assert checked.resolved_levels[0] is not original.resolved_levels[0]
    assert checked.probabilities is not original.probabilities
    assert checked.probabilities is not None and original.probabilities is not None
    assert checked.probabilities[0] is not original.probabilities[0]


@pytest.mark.parametrize("writer", ["generic_jsonl", "results_jsonl", "csv"])
def test_exports_remain_streaming_and_leave_only_valid_prefix(
    tmp_path: Path, writer: Writer
) -> None:
    first = _rubric()
    invalid = _pair()
    invalid.winner_id = "unknown"
    consumed: list[int] = []

    def records() -> Iterator[Result]:
        consumed.append(1)
        yield first
        consumed.append(2)
        yield invalid
        raise AssertionError("writer buffered/consumed records after failure")

    output = tmp_path / "results"
    with pytest.raises(ValueError):
        _write(output, records(), writer)
    assert consumed == [1, 2]
    reader = read_results_csv(output) if writer == "csv" else read_results(output)
    assert list(reader) == [first]


def test_export_rejects_instead_of_repairing_mutated_category_labels(
    tmp_path: Path,
) -> None:
    trait = Trait(
        trait_id="quality",
        name="Quality",
        description="Synthetic",
        levels=[Level(label="1"), Level(label="2")],
    )
    result = normalize_rubric_result(
        trait,
        run_id="run",
        task_id="task",
        request_uid="request",
        attempt_id="attempt",
        item_id="001",
        category_label="1",
    ).model_copy(update={"category_label": 1})
    # Normal schema construction accepts numeric input labels. Export must not
    # silently apply that convenience conversion to an invalid mutated result.
    with pytest.raises(ValueError, match="would change"):
        write_results_jsonl(tmp_path / "results.jsonl", [result])


def test_generic_writer_checks_current_nested_record_data(tmp_path: Path) -> None:
    response = ResponseItem(item_id="001", text="Synthetic", metadata={"nested": [0.5]})
    response.metadata["nested"] = [float("nan")]
    with pytest.raises(ValueError):
        write_jsonl(tmp_path / "responses.jsonl", [response])
    level = Level(label="low")
    level.source_label = None
    with pytest.raises(ValueError, match="would change"):
        write_jsonl(tmp_path / "levels.jsonl", [level])


def test_generic_writer_preserves_root_records(tmp_path: Path) -> None:
    path = tmp_path / "roots.jsonl"
    value = RootModel[list[int]]([1, 2])
    assert write_jsonl(path, [value]) == 1
    assert list(read_jsonl(path, RootModel[list[int]])) == [value]
    invalid = value.model_copy(update={"root": ["invalid"]})
    with pytest.raises(ValueError):
        write_jsonl(path, [invalid])
    unknown = value.model_copy(update={"extra": "invalid"})
    with pytest.raises(ValueError, match="undeclared"):
        write_jsonl(path, [unknown])


def test_generic_writer_snapshots_tuple_nested_models(tmp_path: Path) -> None:
    from judgmentllm.models import StrictModel

    class Collection(StrictModel):
        records: tuple[ResponseItem, ...]

    response = ResponseItem(item_id="001", text="Synthetic")
    collection = Collection(records=(response,))
    response.item_id = ""
    with pytest.raises(ValueError):
        write_jsonl(tmp_path / "collections.jsonl", [collection])


def test_generic_writer_revalidates_field_names_for_aliased_models(
    tmp_path: Path,
) -> None:
    class AliasedRecord(BaseModel):
        value: int = Field(alias="external", gt=0)

    record = AliasedRecord.model_validate({"external": 1})
    output = tmp_path / "aliased.jsonl"
    assert write_jsonl(output, [record]) == 1
    assert output.read_text().strip() == '{"value": 1}'
    record.value = -1
    with pytest.raises(ValueError):
        write_jsonl(output, [record])
