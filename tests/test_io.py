"""Synthetic streaming input and lossless normalized result round trips."""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from judgmentllm.config import InputSpec
from judgmentllm.io import (
    InputError,
    export_results_csv,
    read_jsonl,
    read_pairs,
    read_responses,
    read_results,
    read_results_csv,
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


def _result() -> RubricResult:
    trait = Trait(
        trait_id="detail",
        name="Detail",
        description="Compare detail.",
        levels=[
            Level(label="Low", numeric_value=1),
            Level(label="High", numeric_value=5),
        ],
    )
    return normalize_rubric_result(
        trait,
        run_id="run",
        task_id="task",
        request_uid="request",
        attempt_id="attempt",
        item_id="essay",
        category_label="Low",
        probabilities=[
            {"level_label": "Low", "probability": 0.25},
            {"level_label": "High", "probability": 0.75},
        ],
        explanation="",
        provider="\\N",
    )


def _pair_result() -> PairwiseResult:
    return normalize_pairwise_result(
        PairTask(
            pair_id="pair",
            pair_uid="occurrence",
            left_item_id="A",
            right_item_id="B",
            trait_id="detail",
            presented_order=["B", "A"],
        ),
        run_id="run",
        task_id="pair-task",
        request_uid="pair-request",
        attempt_id="attempt",
        winner_id="A",
        p_left=0.8,
        p_right=0.2,
    )


@pytest.mark.parametrize("extension", ["jsonl", "csv"])
def test_response_input_preserves_text_metadata_and_order(
    tmp_path: Path, extension: str
) -> None:
    items = [
        ResponseItem(
            item_id="A",
            text='Synthetic, multiline\n"quoted" résumé.',
            metadata={"group": [1, {"note": None}]},
        ),
        ResponseItem(item_id="B", text="Second synthetic."),
    ]
    source = tmp_path / f"responses.{extension}"
    if extension == "jsonl":
        assert write_jsonl(source, iter(items)) == 2
    else:
        with source.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["item_id", "text", "metadata"])
            for item in items:
                writer.writerow([item.item_id, item.text, json.dumps(item.metadata)])
    assert list(read_responses(source)) == items


def test_readers_are_lazy_and_empty_policy_is_explicit(tmp_path: Path) -> None:
    source = tmp_path / "responses.jsonl"
    source.write_text(
        '{"item_id":"A","text":"Synthetic."}\n{"item_id":"B","text":""}\ninvalid\n',
        encoding="utf-8",
    )
    reader = read_responses(source)
    assert next(reader).item_id == "A"
    with pytest.raises(InputError, match="allow_empty_text"):
        next(reader)
    reader = read_responses(InputSpec(path=source, allow_empty_text=True))
    assert next(reader).item_id == "A"
    assert next(reader).text == ""
    with pytest.raises(InputError, match="line 3"):
        next(reader)
    reader = read_responses(source, allow_empty_text=True)
    assert next(reader).item_id == "A"
    assert next(reader).text == ""
    reader.close()


@pytest.mark.parametrize(
    ("content", "suffix", "message"),
    [
        (
            '{"item_id":"A","text":"One"}\n{"item_id":"A","text":"Two"}\n',
            "jsonl",
            "duplicate response",
        ),
        ('{"item_id":"A","text":"One","unknown":1}\n', "jsonl", "Extra inputs"),
        (
            '{"schema_version":2,"item_id":"A","text":"One"}\n',
            "jsonl",
            "schema_version",
        ),
        ('{"item_id":"A","text":"One","text":"Two"}\n', "jsonl", "duplicate JSON"),
        ("\n", "jsonl", "empty JSONL"),
        ("", "csv", "header is missing"),
        ("item_id,text,text\nA,One,Two\n", "csv", "duplicate CSV"),
        ("item_id,unknown\nA,One\n", "csv", "unknown CSV"),
        ("item_id,text\nA,One,Extra\n", "csv", "expected 2"),
        ('item_id,text\nA,"unclosed\n', "csv", "unexpected end"),
        ("item_id,text,metadata\nA,One,not-json\n", "csv", "invalid JSON cell"),
        ("schema_version,item_id,text\n2,A,One\n", "csv", "schema_version"),
    ],
)
def test_invalid_response_inputs(
    tmp_path: Path, content: str, suffix: str, message: str
) -> None:
    source = tmp_path / f"responses.{suffix}"
    source.write_text(content, encoding="utf-8")
    with pytest.raises(InputError, match=message):
        list(read_responses(source))


def test_missing_or_invalid_encoding_inputs(tmp_path: Path) -> None:
    for suffix in ("jsonl", "csv"):
        source = tmp_path / f"missing.{suffix}"
        with pytest.raises(InputError, match="missing"):
            list(read_responses(source))
        source.write_bytes(b"\xff")
        with pytest.raises(InputError):
            list(read_responses(source))


@pytest.mark.parametrize("extension", ["jsonl", "csv"])
def test_pair_inputs_and_duplicate_occurrences(tmp_path: Path, extension: str) -> None:
    pair = PairTask(
        pair_id="pair",
        pair_uid="occurrence",
        left_item_id="A",
        right_item_id="B",
        trait_id="detail",
        presented_order=["B", "A"],
        metadata={"note": [1, "synthetic"]},
    )
    source = tmp_path / f"pairs.{extension}"
    if extension == "jsonl":
        write_jsonl(source, [pair, pair])
    else:
        with source.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            header = [
                "pair_id",
                "pair_uid",
                "left_item_id",
                "right_item_id",
                "trait_id",
                "original_order",
                "presented_order",
                "metadata",
                "schema_version",
                "repetition_index",
                "is_reversal",
                "source_pair_uid",
                "reversal_group",
            ]
            writer.writerow(header)
            row = [
                "pair",
                "occurrence",
                "A",
                "B",
                "detail",
                '["A","B"]',
                '["B","A"]',
                json.dumps(pair.metadata),
                "1",
                "0",
                "false",
                "",
                "",
            ]
            writer.writerow(row)
            writer.writerow(row)
    reader = read_pairs(source)
    assert next(reader) == pair
    with pytest.raises(InputError, match="duplicate pair_uid"):
        next(reader)


def test_pair_csv_omits_empty_optional_values(tmp_path: Path) -> None:
    source = tmp_path / "pairs.csv"
    source.write_text(
        "pair_id,pair_uid,left_item_id,right_item_id,trait_id,presented_order,repetition_index\npair,occ,A,B,detail,,\n",
        encoding="utf-8",
    )
    assert list(read_pairs(source))[0].presented_order == ["A", "B"]


def test_normalized_jsonl_and_csv_round_trip(tmp_path: Path) -> None:
    results = [_result(), _pair_result()]
    source = tmp_path / "results.jsonl"
    assert write_results_jsonl(source, iter(results)) == 2
    assert list(read_results(source)) == results
    reader = read_jsonl(source, RubricResult)
    assert next(reader) == results[0]
    reader.close()
    csv_path = tmp_path / "results.csv"
    assert export_results_csv(csv_path, iter(read_results(source))) == 2
    recovered = list(read_results_csv(csv_path))
    assert recovered == results
    assert recovered[0].explanation == ""
    assert recovered[0].provider == "\\N"
    assert recovered[0].model is None
    assert '"level_label"' in csv_path.read_text(encoding="utf-8").replace('""', '"')


def test_writer_consumes_one_record_at_a_time(tmp_path: Path) -> None:
    visited: list[int] = []

    def records() -> Iterator[ResponseItem]:
        for index in range(100):
            visited.append(index)
            yield ResponseItem(item_id=str(index), text="Synthetic.")

    assert write_jsonl(tmp_path / "large.jsonl", records()) == 100
    assert visited == list(range(100))
    reader = read_jsonl(tmp_path / "large.jsonl", ResponseItem)
    assert next(reader).item_id == "0"
    reader.close()


@pytest.mark.parametrize("content", ["[]\n", '{"task_kind":"unknown"}\n'])
def test_unknown_result_shapes(tmp_path: Path, content: str) -> None:
    source = tmp_path / "results.jsonl"
    source.write_text(content, encoding="utf-8")
    with pytest.raises(InputError):
        list(read_results(source))


def test_invalid_result_csv_and_nested_cells(tmp_path: Path) -> None:
    source = tmp_path / "results.csv"
    export_results_csv(source, [_result()])
    with source.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        header = reader.fieldnames
        rows = list(reader)
    assert header is not None
    rows[0]["pair_id"] = "unexpected"
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(InputError, match="does not belong"):
        list(read_results_csv(source))
    rows[0]["pair_id"] = "\\N"
    rows[0]["probabilities"] = "invalid-json"
    with source.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(InputError, match="invalid JSON cell"):
        list(read_results_csv(source))


def test_empty_writers(tmp_path: Path) -> None:
    assert write_results_jsonl(tmp_path / "empty.jsonl", []) == 0
    assert list(read_results(tmp_path / "empty.jsonl")) == []
    assert export_results_csv(tmp_path / "empty.csv", []) == 0
    assert list(read_results_csv(tmp_path / "empty.csv")) == []


def test_large_response_and_nested_metadata_csv_cells(tmp_path: Path) -> None:
    """Complete text/metadata may each exceed csv's default 128 KiB limit."""
    item = ResponseItem(
        item_id="large-synthetic",
        text="A synthetic response, with punctuation.\n" * 5_000,
        metadata={"nested": {"annotation": "Synthetic metadata. " * 8_000}},
    )
    assert len(item.text.encode("utf-8")) > 128 * 1024
    encoded_metadata = json.dumps(item.metadata)
    assert len(encoded_metadata) > 128 * 1024
    path = tmp_path / "large-response.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["item_id", "text", "metadata"])
        writer.writerow([item.item_id, item.text, encoded_metadata])
    assert list(read_responses(path)) == [item]


def test_large_probability_distribution_csv_round_trip(tmp_path: Path) -> None:
    """Many stored categories retain their complete ordered distribution."""
    labels = [
        f"Category {index:04d} with a descriptive ordinal label"
        for index in range(2_500)
    ]
    trait = Trait(
        trait_id="large-scale",
        name="Synthetic detailed scale",
        description="A synthetic scale with many distinct ordered categories.",
        levels=[Level(label=label) for label in labels],
    )
    result = normalize_rubric_result(
        trait,
        run_id="run",
        task_id="large-task",
        request_uid="request",
        attempt_id="attempt",
        item_id="essay",
        probabilities=[
            {"level_label": label, "probability": 1 / len(labels)} for label in labels
        ],
    )
    assert len(json.dumps(result.model_dump(mode="json")["probabilities"])) > 128 * 1024
    path = tmp_path / "large-probabilities.csv"
    assert export_results_csv(path, [result]) == 1
    assert list(read_results_csv(path)) == [result]


def test_csv_field_limit_handles_platform_overflow_without_shrinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise platforms whose parser cannot accept sys.maxsize directly."""
    current_limit = 128 * 1024
    attempts: list[int] = []
    accepted: list[int] = []

    def limited_field_size_limit(value: int | None = None) -> int:
        nonlocal current_limit
        previous = current_limit
        if value is not None:
            attempts.append(value)
            if value > 1_000_000:
                raise OverflowError("synthetic platform C integer overflow")
            assert value > current_limit
            current_limit = value
            accepted.append(value)
        return previous

    monkeypatch.setattr(csv, "field_size_limit", limited_field_size_limit)
    path = tmp_path / "responses.csv"
    path.write_text("item_id,text\nA,Synthetic.\n", encoding="utf-8")
    assert list(read_responses(path))[0].item_id == "A"
    assert attempts[0] == sys.maxsize
    assert len(attempts) > 1 and accepted
    assert current_limit > 128 * 1024
    current_limit = sys.maxsize
    attempt_count = len(attempts)
    assert list(read_responses(path))[0].item_id == "A"
    assert len(attempts) == attempt_count
