"""Streaming CSV/JSONL inputs and normalized result serialization.

CSV nested values are JSON cells. Normalized CSV uses ``\\N`` for null and
escapes a literal leading backslash with another backslash. No dataframe or
provider SDK is required.
"""

from __future__ import annotations

import csv
import json
import sys
import types
from collections.abc import Generator, Iterable, Iterator
from pathlib import Path
from typing import Annotated, Literal, TypeVar, Union, cast, get_args, get_origin

from pydantic import BaseModel, ValidationError

from judgmentllm.config import InputSpec, parse_json
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.results import PairwiseResult, RubricResult

Record = TypeVar("Record", bound=BaseModel)
Result = RubricResult | PairwiseResult
NormalizedResult = TypeVar("NormalizedResult", bound=Result)
_NULL_CELL = "\\N"


class InputError(ValueError):
    """Input syntax, identity, or model validation failed at a known record."""


def _error_details(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, entry['loc'])) or '<record>'}: {entry['msg']}"
        for entry in error.errors(include_input=False, include_url=False)
    )


def _validate(model: type[Record], value: object, source: Path, line: int) -> Record:
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise InputError(
            f"{source}: record/line {line}: {_error_details(exc)}"
        ) from exc


def _json_records(path: str | Path) -> Iterator[tuple[int, object]]:
    source = Path(path)
    try:
        with source.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    raise InputError(
                        f"{source}: line {line_number}: empty JSONL record"
                    )
                try:
                    yield line_number, parse_json(line)
                except ValueError as exc:
                    raise InputError(f"{source}: line {line_number}: {exc}") from exc
    except (OSError, UnicodeError) as exc:
        raise InputError(f"{source}: {exc}") from exc


def read_jsonl(path: str | Path, model: type[Record]) -> Generator[Record, None, None]:
    """Read one validated model at a time; malformed/blank records are errors."""
    source = Path(path)
    for line, data in _json_records(source):
        yield _validate(model, data, source, line)


def _record_data(value: object) -> object:
    """Snapshot current values, including copied extras and nested models.

    model_dump() alone drops unknown keys injected by model_copy(update=...).
    Iterating models retains those keys so reconstruction can reject them.
    """
    if value is None or type(value) in (str, bool, int, float):
        return value
    if isinstance(value, BaseModel):
        data = {name: _record_data(item) for name, item in value}
        if value.__pydantic_root_model__:
            if set(data) != {"root"}:
                raise ValueError("root record contains undeclared fields")
            return data["root"]
        return data
    if isinstance(value, dict):
        return {
            key: _record_data(item)
            for key, item in cast(dict[object, object], value).items()
        }
    if isinstance(value, list):
        return [_record_data(item) for item in cast(list[object], value)]
    if isinstance(value, tuple):
        return tuple(_record_data(item) for item in cast(tuple[object, ...], value))
    return value


def _revalidate_record(record: Record) -> Record:
    """Reconstruct from current plain data and reject validator-induced repairs."""
    validated = type(record).model_validate(_record_data(record), by_name=True)
    if _record_data(validated) != _record_data(record):
        raise ValueError(
            "record data would change during validation; export requires valid current data"
        )
    return validated


def revalidate_result(record: NormalizedResult) -> NormalizedResult:
    """Validate a fresh snapshot of a normalized result before deriving exports.

    This includes every current nested list/model and copied field. It does not
    trust the original validation or repair mutated assessment data. Only one
    record is copied, so exporters can continue to consume input iterators.
    """
    return _revalidate_record(record)


def write_jsonl(path: str | Path, records: Iterable[BaseModel]) -> int:
    """Stream models to UTF-8 JSONL; return the number written.

    Every record, including generic typed records, is freshly revalidated from
    current nested values immediately before serialization. Invalid mutations
    raise rather than being repaired. Pydantic's serializer methods by themselves
    are not this validation boundary.

    The caller owns output directory creation. A failure may leave a partial
    file; this utility is not an execution journal or transaction system.
    """
    count = 0
    with Path(path).open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            validated = _revalidate_record(record)
            stream.write(
                json.dumps(
                    validated.model_dump(mode="json"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
            stream.write("\n")
            count += 1
    return count


def _csv_records(
    path: str | Path, allowed: set[str], required: set[str]
) -> Iterator[tuple[int, dict[str, str]]]:
    source = Path(path)
    try:
        with source.open(encoding="utf-8", newline="") as stream:
            _raise_csv_field_limit()
            reader = csv.reader(stream, strict=True)
            header = next(reader, None)
            validate_csv_header(header, source, allowed, required)
            assert header is not None
            for row in reader:
                if len(row) != len(header):
                    raise InputError(
                        f"{source}: line {reader.line_num}: expected {len(header)} columns, got {len(row)}"
                    )
                yield reader.line_num, dict(zip(header, row, strict=True))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InputError(f"{source}: {exc}") from exc


def validate_csv_header(
    header: list[str] | None, source: Path, allowed: set[str], required: set[str]
) -> None:
    """Apply the common strict header contract to streaming/indexed readers."""
    if header is None:
        raise InputError(f"{source}: CSV header is missing")
    if len(header) != len(set(header)):
        raise InputError(f"{source}: duplicate CSV column")
    unknown = set(header) - allowed
    missing = required - set(header)
    if unknown or missing:
        raise InputError(
            f"{source}: unknown CSV columns {sorted(unknown)}; missing required columns {sorted(missing)}"
        )


def _raise_csv_field_limit() -> None:
    """Increase the process-wide stdlib limit without restoring/shrinking it.

    Some platforms accept a smaller C integer than sys.maxsize. Retrying with
    smaller candidates avoids imposing csv's small default on complete inputs.
    """
    candidate = sys.maxsize
    while candidate > csv.field_size_limit():
        try:
            csv.field_size_limit(candidate)
        except OverflowError:
            candidate //= 10
        else:
            break


def _input_spec(source: InputSpec | str | Path) -> InputSpec:
    return source if isinstance(source, InputSpec) else InputSpec(path=Path(source))


def _parse_cell(value: str, source: Path, line: int, field: str) -> object:
    try:
        return parse_json(value)
    except ValueError as exc:
        raise InputError(
            f"{source}: line {line}, field {field}: invalid JSON cell: {exc}"
        ) from exc


def response_from_csv(row: dict[str, str], source: Path, line: int) -> ResponseItem:
    """Validate one CSV response with the same conversions as ordinary input."""
    data: dict[str, object] = dict(row)
    if "metadata" in row:
        data["metadata"] = (
            _parse_cell(row["metadata"], source, line, "metadata")
            if row["metadata"]
            else {}
        )
    if "schema_version" in row:
        data["schema_version"] = _parse_cell(
            row["schema_version"], source, line, "schema_version"
        )
    return _validate(ResponseItem, data, source, line)


def pair_from_csv(row: dict[str, str], source: Path, line: int) -> PairTask:
    """Validate one CSV pair; absent optional cells retain established defaults."""
    data: dict[str, object] = dict(row)
    nested = {"metadata", "original_order", "presented_order"}
    scalars = {"schema_version", "repetition_index", "is_reversal"}
    for field in nested | scalars:
        if field in row:
            if not row[field]:
                del data[field]
            else:
                data[field] = _parse_cell(row[field], source, line, field)
    for field in ("reversal_group", "source_pair_uid"):
        if data.get(field) == "":
            data[field] = None
    return _validate(PairTask, data, source, line)


def validate_response_text(item: ResponseItem, spec: InputSpec) -> None:
    """Check the explicit empty-text policy without retaining any ID set."""
    if not spec.allow_empty_text and item.text == "":
        raise InputError(
            f"{spec.path}: response {item.item_id!r} has empty text; set allow_empty_text: true to permit it"
        )


def read_responses(
    source: InputSpec | str | Path, *, allow_empty_text: bool | None = None
) -> Generator[ResponseItem, None, None]:
    """Yield responses lazily, checking duplicate IDs and the empty-text policy.

    Only seen IDs and the current record are retained. An explicit keyword
    overrides the InputSpec policy for direct library use.
    """
    spec = _input_spec(source)
    empty_allowed = (
        spec.allow_empty_text if allow_empty_text is None else allow_empty_text
    )
    if spec.format == "jsonl":
        items = read_jsonl(spec.path, ResponseItem)
    else:

        def csv_items() -> Iterator[ResponseItem]:
            for line, row in _csv_records(
                spec.path, set(ResponseItem.model_fields), {"item_id", "text"}
            ):
                yield response_from_csv(row, spec.path, line)

        items = csv_items()
    seen: set[str] = set()
    for item in items:
        if item.item_id in seen:
            raise InputError(
                f"{spec.path}: duplicate response item_id {item.item_id!r}"
            )
        validate_response_text(
            item, spec.model_copy(update={"allow_empty_text": empty_allowed})
        )
        seen.add(item.item_id)
        yield item


def read_pairs(source: InputSpec | str | Path) -> Generator[PairTask, None, None]:
    """Yield explicit pair occurrences, rejecting repeated pair_uid values."""
    spec = _input_spec(source)
    if spec.format == "jsonl":
        items = read_jsonl(spec.path, PairTask)
    else:

        def csv_items() -> Iterator[PairTask]:
            required = {
                "pair_id",
                "pair_uid",
                "left_item_id",
                "right_item_id",
                "trait_id",
            }
            for line, row in _csv_records(
                spec.path, set(PairTask.model_fields), required
            ):
                yield pair_from_csv(row, spec.path, line)

        items = csv_items()
    seen: set[str] = set()
    for item in items:
        if item.pair_uid in seen:
            raise InputError(f"{spec.path}: duplicate pair_uid {item.pair_uid!r}")
        seen.add(item.pair_uid)
        yield item


def _result_model(
    data: object, path: Path, line: int
) -> type[RubricResult] | type[PairwiseResult]:
    if not isinstance(data, dict):
        raise InputError(f"{path}: line {line}: result must be an object")
    kind = cast(dict[str, object], data).get("task_kind")
    if kind == "rubric":
        return RubricResult
    if kind == "pairwise":
        return PairwiseResult
    raise InputError(
        f"{path}: line {line}: result task_kind must be rubric or pairwise"
    )


def read_results(path: str | Path) -> Generator[Result, None, None]:
    """Read normalized mixed rubric/pairwise JSONL with schema validation."""
    source = Path(path)
    for line, data in _json_records(source):
        model = _result_model(data, source, line)
        yield _validate(model, data, source, line)


def write_results_jsonl(path: str | Path, records: Iterable[Result]) -> int:
    """Write normalized rubric/pairwise records in canonical JSONL form."""
    return write_jsonl(path, records)


def _csv_encode(value: object) -> str:
    if value is None:
        return _NULL_CELL
    if isinstance(value, str):
        return "\\" + value if value.startswith("\\") else value
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def export_results_csv(path: str | Path, records: Iterable[Result]) -> int:
    """Stream normalized records to CSV, preserving all nested result fields.

    Each current result is freshly revalidated before serialization; no invalid
    mutation is repaired or written. A deterministic union header permits mixed
    result kinds without buffering.
    Columns belonging only to the other kind contain the null sentinel.
    """
    fields = list(
        dict.fromkeys([*RubricResult.model_fields, *PairwiseResult.model_fields])
    )
    count = 0
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            data = revalidate_result(record).model_dump(mode="json")
            writer.writerow({field: _csv_encode(data.get(field)) for field in fields})
            count += 1
    return count


def _string_annotation(annotation: object) -> bool:
    if annotation is str:
        return True
    origin = get_origin(annotation)
    if origin is Annotated:
        return _string_annotation(get_args(annotation)[0])
    if origin in (types.UnionType, Union):
        return any(_string_annotation(value) for value in get_args(annotation))
    return origin is Literal and all(
        isinstance(value, str) for value in get_args(annotation)
    )


def read_results_csv(path: str | Path) -> Generator[Result, None, None]:
    """Read this module's normalized CSV format with full model validation."""
    source = Path(path)
    allowed = set(RubricResult.model_fields) | set(PairwiseResult.model_fields)
    for line, row in _csv_records(source, allowed, {"task_kind"}):
        model = _result_model(row, source, line)
        data: dict[str, object] = {}
        for field, value in row.items():
            if field not in model.model_fields:
                if value != _NULL_CELL:
                    raise InputError(
                        f"{source}: line {line}: field {field} does not belong to {row['task_kind']} results"
                    )
                continue
            if value == _NULL_CELL:
                data[field] = None
            elif _string_annotation(model.model_fields[field].annotation):
                data[field] = value[1:] if value.startswith("\\\\") else value
            else:
                data[field] = _parse_cell(value, source, line, field)
        yield _validate(model, data, source, line)
