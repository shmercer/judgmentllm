"""Streaming R-friendly audit views derived from validated normalized results.

These CSVs retain every supplied attempt. They do not select successful attempts,
fit models, or replace the canonical JSONL/lossless CSV contracts. Import ID
columns as character in R; quoting alone does not prevent numeric conversion.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import TypeVar

from pydantic import JsonValue

from judgmentllm.io import revalidate_result
from judgmentllm.results import PairwiseResult, RubricResult

RRecord = TypeVar("RRecord", bound=PairwiseResult | RubricResult)


def _checked_result(record: object, model: type[RRecord]) -> RRecord:
    if not isinstance(record, model):
        raise ValueError(f"R export requires {model.__name__} records")
    return revalidate_result(record)


_PAIRWISE_DERIVED = (
    "ID1",
    "ID2",
    "A_id",
    "B_id",
    "better_id",
    "result",
    "winner_pos",
    "p_first",
    "p_second",
)
_RUBRIC_DERIVED = ("response_id", "probabilities_json", "resolved_levels_json")


def _r_cell(value: JsonValue, na_string: str) -> str | int | float:
    if value is None:
        return na_string
    encoded: str | int | float
    if isinstance(value, bool):
        encoded = "TRUE" if value else "FALSE"
    elif isinstance(value, (list, dict)):
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
    else:
        encoded = value
    if str(encoded) == na_string:
        raise ValueError(
            f"CSV value equals missing-value marker {na_string!r}; choose a different na_string and use it when importing in R"
        )
    return encoded


def _validate_na(na_string: object) -> None:
    if (
        not isinstance(na_string, str)
        or not na_string
        or any(character in na_string for character in "\r\n")
    ):
        raise ValueError("na_string must be a nonempty single-line string")


def export_pairwise_r_csv(
    path: str | Path,
    records: Iterable[PairwiseResult],
    *,
    na_string: str = "NA",
) -> int:
    """Export all supplied pairwise attempts using presented positions.

    ID1/A_id and ID2/B_id are presented first and second. p_left/p_right retain
    original orientation; p_first/p_second follow presentation. Only complete,
    observed wins/ties receive numeric result/winner_pos. better_id preserves an
    explicitly observed winner even on an unsuccessful record. No winner is
    inferred from probabilities. Missing cells use na_string (default NA), with
    literal collisions rejected. Scalar floats are written without rounding.
    """
    _validate_na(na_string)
    count = 0
    fields = [*_PAIRWISE_DERIVED, *PairwiseResult.model_fields]
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            validated = _checked_result(record, PairwiseResult)
            first, second = validated.presented_order
            result: float | int | None = None
            winner_pos: int | None = None
            if validated.status == "complete":
                if validated.tie:
                    result = 0.5
                elif validated.winner_id == first:
                    result, winner_pos = 1, 1
                else:
                    result, winner_pos = 0, 2
            first_is_left = first == validated.left_item_id
            data = validated.model_dump(mode="json")
            data.update(
                ID1=first,
                ID2=second,
                A_id=first,
                B_id=second,
                better_id=validated.winner_id,
                result=result,
                winner_pos=winner_pos,
                p_first=validated.p_left if first_is_left else validated.p_right,
                p_second=validated.p_right if first_is_left else validated.p_left,
            )
            writer.writerow(
                {field: _r_cell(value, na_string) for field, value in data.items()}
            )
            count += 1
    return count


def export_rubric_r_csv(
    path: str | Path,
    records: Iterable[RubricResult],
    *,
    na_string: str = "NA",
) -> int:
    """Export one row per supplied item/trait attempt, with lossless JSON cells.

    response_id aliases item_id. probabilities_json and resolved_levels_json
    replace the corresponding nested column names in this derived view; all
    other normalized fields remain, including selected/modal category codes,
    expected value, score kind, status, identities, and provenance. Numeric
    assignments and expectations are kept distinct and are never rounded.
    """
    _validate_na(na_string)
    count = 0
    fields = [
        *_RUBRIC_DERIVED,
        *(
            field
            for field in RubricResult.model_fields
            if field not in {"probabilities", "resolved_levels"}
        ),
    ]
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            validated = _checked_result(record, RubricResult)
            data = validated.model_dump(mode="json")
            data.update(
                response_id=validated.item_id,
                probabilities_json=data.pop("probabilities"),
                resolved_levels_json=data.pop("resolved_levels"),
            )
            writer.writerow(
                {field: _r_cell(value, na_string) for field, value in data.items()}
            )
            count += 1
    return count
