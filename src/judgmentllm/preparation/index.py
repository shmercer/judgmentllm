"""Bounded byte-accurate input parsing for immutable source indexes.

This module owns no persistence and no source authority. Callers supply a
verified binary descriptor, enforce duplicate IDs durably, and compare returned
IDs/canonical hashes with their immutable index. CSV grammar and field
conversion are shared with the ordinary strict input readers.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO, Literal

from pydantic import ConfigDict, Field

from judgmentllm.config import InputSpec, parse_json
from judgmentllm.io import (
    InputError,
    _raise_csv_field_limit,  # pyright: ignore[reportPrivateUsage]
    _validate,  # pyright: ignore[reportPrivateUsage]
    pair_from_csv,
    response_from_csv,
    validate_csv_header,
    validate_response_text,
)
from judgmentllm.models import PairTask, ResponseItem, StrictModel

RecordKind = Literal["response", "pair"]
InputRecord = ResponseItem | PairTask
_NEWLINE = re.compile(b"\r\n|\r|\n")
_CHUNK_SIZE = 64 * 1024


class RecordLocator(StrictModel):
    """Exact encoded logical record interval and ending physical line number."""

    model_config = ConfigDict(frozen=True)
    offset: int = Field(ge=0)
    byte_length: int = Field(gt=0)
    line: int = Field(gt=0)


class ParsingContext(StrictModel):
    """Non-text parsing commitments needed to reconstruct one indexed record."""

    model_config = ConfigDict(frozen=True)
    version: Literal[1] = 1
    kind: RecordKind
    format: Literal["csv", "jsonl"]
    csv_header: tuple[str, ...] | None = None
    header_byte_length: int = Field(default=0, ge=0)
    header_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class LocatedRecord:
    """One validated record with its source-relative locator/context."""

    record: InputRecord
    locator: RecordLocator
    context: ParsingContext


class _BinaryLines(Iterator[str]):
    """Universal physical lines with exact bytes and bounded read-ahead.

    Unlike BinaryIO.readline(), recognizes lone CR as the existing text readers
    do. It preserves terminators for csv.reader(newline='') semantics. Memory
    grows only with the current physical line, never the whole input.
    """

    def __init__(self, stream: BinaryIO, *, limit: int | None = None) -> None:
        self.stream = stream
        self.remaining = limit
        self.buffer = bytearray()
        self.eof = False
        self.position = 0
        self.line = 0
        self.digest = hashlib.sha256()

    def __next__(self) -> str:
        scan = 0
        while True:
            match = _NEWLINE.search(self.buffer, scan)
            if match is not None and (
                match.end() < len(self.buffer)
                or self.buffer[match.start()] != 13
                or self.eof
            ):
                end = match.end()
                break
            if self.eof:
                if not self.buffer:
                    raise StopIteration
                end = len(self.buffer)
                break
            scan = max(0, len(self.buffer) - 1)
            size = (
                _CHUNK_SIZE
                if self.remaining is None
                else min(_CHUNK_SIZE, self.remaining)
            )
            chunk = self.stream.read(size) if size else b""
            if self.remaining is not None:
                self.remaining -= len(chunk)
            self.buffer.extend(chunk)
            self.eof = not chunk
        raw = bytes(self.buffer[:end])
        del self.buffer[:end]
        self.position += len(raw)
        self.line += 1
        self.digest.update(raw)
        return raw.decode("utf-8")


def _header_rules(kind: RecordKind) -> tuple[set[str], set[str]]:
    if kind == "response":
        return set(ResponseItem.model_fields), {"item_id", "text"}
    return set(PairTask.model_fields), {
        "pair_id",
        "pair_uid",
        "left_item_id",
        "right_item_id",
        "trait_id",
    }


def _csv_record(
    row: list[str],
    header: tuple[str, ...],
    spec: InputSpec,
    line: int,
    kind: RecordKind,
) -> InputRecord:
    if len(row) != len(header):
        raise InputError(
            f"{spec.path}: line {line}: expected {len(header)} columns, got {len(row)}"
        )
    data = dict(zip(header, row, strict=True))
    return (
        response_from_csv(data, spec.path, line)
        if kind == "response"
        else pair_from_csv(data, spec.path, line)
    )


def _json_record(
    text: str, spec: InputSpec, line: int, kind: RecordKind
) -> InputRecord:
    if not text.strip():
        raise InputError(f"{spec.path}: line {line}: empty JSONL record")
    try:
        value = parse_json(text)
    except ValueError as exc:
        raise InputError(f"{spec.path}: line {line}: {exc}") from exc
    if kind == "response":
        return _validate(ResponseItem, value, spec.path, line)
    return _validate(PairTask, value, spec.path, line)


def _csv_context(
    reader: Iterator[list[str]], lines: _BinaryLines, spec: InputSpec, kind: RecordKind
) -> ParsingContext:
    header = next(reader, None)
    allowed, required = _header_rules(kind)
    validate_csv_header(header, spec.path, allowed, required)
    assert header is not None
    return ParsingContext(
        kind=kind,
        format="csv",
        csv_header=tuple(header),
        header_byte_length=lines.position,
        header_sha256=lines.digest.hexdigest(),
    )


def parsing_context(
    stream: BinaryIO, spec: InputSpec, *, kind: RecordKind
) -> ParsingContext:
    """Read context even for empty JSONL or header-only CSV input sources."""
    stream.seek(0)
    if spec.format == "jsonl":
        return ParsingContext(kind=kind, format="jsonl")
    try:
        _raise_csv_field_limit()
        lines = _BinaryLines(stream)
        return _csv_context(csv.reader(lines, strict=True), lines, spec, kind)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InputError(f"{spec.path}: {exc}") from exc


def iter_records(
    stream: BinaryIO, spec: InputSpec, *, kind: RecordKind
) -> Iterator[LocatedRecord]:
    """Stream complete records from byte zero, without retaining seen IDs.

    Duplicate rejection belongs to the caller's durable unique index. The
    descriptor may read ahead by one bounded chunk. Locators always describe
    logical consumed bytes rather than descriptor/text-stream positions.
    """
    stream.seek(0)
    lines = _BinaryLines(stream)
    try:
        if spec.format == "jsonl":
            context = ParsingContext(kind=kind, format="jsonl")
            for text in lines:
                record = _json_record(text, spec, lines.line, kind)
                if isinstance(record, ResponseItem):
                    validate_response_text(record, spec)
                length = len(text.encode("utf-8"))
                yield LocatedRecord(
                    record,
                    RecordLocator(
                        offset=lines.position - length,
                        byte_length=length,
                        line=lines.line,
                    ),
                    context,
                )
            return
        _raise_csv_field_limit()
        reader = csv.reader(lines, strict=True)
        context = _csv_context(reader, lines, spec, kind)
        assert context.csv_header is not None
        while True:
            offset = lines.position
            row = next(reader, None)
            if row is None:
                return
            record = _csv_record(row, context.csv_header, spec, reader.line_num, kind)
            if isinstance(record, ResponseItem):
                validate_response_text(record, spec)
            yield LocatedRecord(
                record,
                RecordLocator(
                    offset=offset,
                    byte_length=lines.position - offset,
                    line=reader.line_num,
                ),
                context,
            )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InputError(f"{spec.path}: {exc}") from exc


def _verify_header(stream: BinaryIO, spec: InputSpec, context: ParsingContext) -> None:
    if (
        context.csv_header is None
        or context.header_sha256 is None
        or not context.header_byte_length
    ):
        raise InputError(f"{spec.path}: missing indexed CSV header context")
    stream.seek(0)
    lines = _BinaryLines(stream, limit=context.header_byte_length)
    reader = csv.reader(lines, strict=True)
    header = next(reader, None)
    allowed, required = _header_rules(context.kind)
    validate_csv_header(header, spec.path, allowed, required)
    if (
        next(reader, None) is not None
        or tuple(header or ()) != context.csv_header
        or lines.position != context.header_byte_length
        or lines.digest.hexdigest() != context.header_sha256
    ):
        raise InputError(f"{spec.path}: indexed CSV header has changed")


def _verify_locator_boundaries(
    stream: BinaryIO, spec: InputSpec, locator: RecordLocator
) -> None:
    """Reject slices beginning/ending inside physical text or a CRLF pair."""
    if locator.offset:
        stream.seek(locator.offset - 1)
        prefix = stream.read(2)
        if not prefix or prefix[0] not in (10, 13) or prefix == b"\r\n":
            raise InputError(f"{spec.path}: indexed record start is not a boundary")
    stream.seek(locator.offset + locator.byte_length - 1)
    suffix = stream.read(2)
    if len(suffix) > 1 and (suffix[0] not in (10, 13) or suffix == b"\r\n"):
        raise InputError(f"{spec.path}: indexed record end is not a boundary")


def read_record(
    stream: BinaryIO,
    spec: InputSpec,
    locator: RecordLocator,
    context: ParsingContext,
) -> InputRecord:
    """Reparse exactly one indexed record, verifying its parsing context.

    The caller must verify source generation around this read and compare the
    returned ID and canonical hash to the durable index before using its text.
    """
    if spec.format != context.format:
        raise InputError(f"{spec.path}: indexed input format mismatch")
    try:
        _raise_csv_field_limit()
        if context.format == "csv":
            _verify_header(stream, spec, context)
            if locator.offset < context.header_byte_length:
                raise InputError(f"{spec.path}: locator overlaps CSV header")
        _verify_locator_boundaries(stream, spec, locator)
        stream.seek(locator.offset)
        lines = _BinaryLines(stream, limit=locator.byte_length)
        if context.format == "jsonl":
            text = next(lines, None)
            if text is None or next(lines, None) is not None:
                raise InputError(f"{spec.path}: locator must contain one JSONL record")
            record = _json_record(text, spec, locator.line, context.kind)
        else:
            assert context.csv_header is not None
            reader = csv.reader(lines, strict=True)
            row = next(reader, None)
            if row is None or next(reader, None) is not None:
                raise InputError(f"{spec.path}: locator must contain one CSV record")
            record = _csv_record(
                row, context.csv_header, spec, locator.line, context.kind
            )
        if lines.position != locator.byte_length:
            raise InputError(f"{spec.path}: indexed record length mismatch")
        if isinstance(record, ResponseItem):
            validate_response_text(record, spec)
        return record
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InputError(f"{spec.path}: {exc}") from exc
