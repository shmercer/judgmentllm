"""Byte-accurate shared parsing and incremental request identity boundaries."""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
from typing import Literal

import pytest

from judgmentllm.config import (
    InputSpec,
    _load_run_spec,  # pyright: ignore[reportPrivateUsage]
    load_run_spec,
    parse_document,
)
from judgmentllm.io import InputError, read_pairs, read_responses
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.planning import iter_requests, plan_record
from judgmentllm.preparation import index
from judgmentllm.preparation.index import (
    ParsingContext,
    RecordLocator,
    iter_records,
    parsing_context,
    read_record,
)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("format", ["csv", "jsonl"])
def test_exact_records_and_shared_reader_parity(
    tmp_path: Path, newline: str, format: Literal["csv", "jsonl"]
) -> None:
    path = tmp_path / f"responses.{format}"
    items = [
        ResponseItem(item_id="001", text='Unicode résumé, "quoted".\nSecond line.'),
        ResponseItem(item_id="002", text="Next synthetic response."),
    ]
    text = io.StringIO(newline="")
    if format == "csv":
        writer = csv.writer(text, lineterminator=newline)
        writer.writerow(["item_id", "text"])
        for item in items:
            writer.writerow([item.item_id, item.text])
    else:
        text.write(newline.join(item.model_dump_json() for item in items) + newline)
    raw = text.getvalue().encode()
    path.write_bytes(raw)
    spec = InputSpec(path=path)
    with path.open("rb") as stream:
        located = list(iter_records(stream, spec, kind="response"))
        assert [entry.record for entry in located] == list(read_responses(spec))
        assert [entry.record for entry in located] == items
        for entry in located:
            assert (
                read_record(stream, spec, entry.locator, entry.context) == entry.record
            )
            start = entry.locator.offset
            end = start + entry.locator.byte_length
            assert raw[start:end].endswith(newline.encode())
        assert located[-1].locator.offset + located[-1].locator.byte_length == len(raw)
        if format == "csv":
            context = located[0].context
            assert (
                context.header_sha256
                == hashlib.sha256(raw[: context.header_byte_length]).hexdigest()
            )
            assert located[1].locator.line == 4


def test_crlf_chunk_boundary_and_large_multiline_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index, "_CHUNK_SIZE", 7)
    text = 'line\r\n"quotes",résumé\r' * 12_000
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["item_id", "text", "metadata"])
    writer.writerow(["001", text, '{"permitted":"metadata"}'])
    raw = output.getvalue().encode()
    spec = InputSpec(path=tmp_path / "responses.csv")
    stream = io.BytesIO(raw)
    entry = next(iter_records(stream, spec, kind="response"))
    assert isinstance(entry.record, ResponseItem)
    assert entry.record.text == text
    assert read_record(stream, spec, entry.locator, entry.context) == entry.record


@pytest.mark.parametrize("format", ["csv", "jsonl"])
def test_pair_defaults_orientation_and_character_ids(
    tmp_path: Path, format: str
) -> None:
    path = tmp_path / f"pairs.{format}"
    if format == "csv":
        raw = (
            b"pair_id,pair_uid,left_item_id,right_item_id,trait_id,presented_order,repetition_index\n"
            b'001,0001,001,002,detail,"[""002"",""001""]",\n'
        )
    else:
        raw = (
            b'{"pair_id":"001","pair_uid":"0001","left_item_id":"001",'
            b'"right_item_id":"002","trait_id":"detail","presented_order":["002","001"]}\n'
        )
    path.write_bytes(raw)
    spec = InputSpec(path=path)
    stream = io.BytesIO(raw)
    entry = next(iter_records(stream, spec, kind="pair"))
    assert [entry.record] == list(read_pairs(spec))
    assert isinstance(entry.record, PairTask)
    assert entry.record.original_order == ["001", "002"]
    assert entry.record.presented_order == ["002", "001"]
    assert read_record(stream, spec, entry.locator, entry.context) == entry.record


@pytest.mark.parametrize(
    ("raw", "format", "message"),
    [
        (b"\n", "jsonl", "empty JSONL"),
        (b'{"item_id":"001","text":"a","text":"b"}\n', "jsonl", "duplicate JSON"),
        (b'{"item_id":"001","text":NaN}\n', "jsonl", "nonfinite JSON"),
        (b'{"item_id":"001","text":"\xff"}\n', "jsonl", "utf-8"),
        (b"", "csv", "header is missing"),
        (b"item_id,text,text\n001,a,b\n", "csv", "duplicate CSV"),
        (b"item_id,unknown\n001,a\n", "csv", "unknown CSV"),
        (b"item_id,text\n001,a,extra\n", "csv", "expected 2"),
        (b'item_id,text\n001,"unclosed\n', "csv", "unexpected end"),
        (b"item_id,text,metadata\n001,a,bad\n", "csv", "invalid JSON cell"),
        (b"item_id,text\n001,\n", "csv", "allow_empty_text"),
    ],
)
def test_invalid_inputs_reuse_existing_semantics(
    tmp_path: Path, raw: bytes, format: Literal["csv", "jsonl"], message: str
) -> None:
    path = tmp_path / f"responses.{format}"
    path.write_bytes(raw)
    with pytest.raises(InputError, match=message):
        list(iter_records(io.BytesIO(raw), InputSpec(path=path), kind="response"))
    with pytest.raises(InputError, match=message):
        list(read_responses(path))


def test_index_does_not_retain_duplicate_id_set(tmp_path: Path) -> None:
    raw = b'{"item_id":"001","text":"synthetic"}\n' * 2
    spec = InputSpec(path=tmp_path / "responses.jsonl")
    # Uniqueness is enforced by SQLite as each record is registered, not by a
    # second dataset-sized Python set in the bounded parser.
    assert len(list(iter_records(io.BytesIO(raw), spec, kind="response"))) == 2


def test_locator_rejects_changed_header_lengths_and_multiple_records(
    tmp_path: Path,
) -> None:
    raw = b"item_id,text\n001,One\n002,Two\n"
    spec = InputSpec(path=tmp_path / "responses.csv")
    first, second = list(iter_records(io.BytesIO(raw), spec, kind="response"))
    with pytest.raises(InputError, match="header has changed"):
        read_record(
            io.BytesIO(raw.replace(b"item_id,text", b"text,item_id")),
            spec,
            first.locator,
            first.context,
        )
    with pytest.raises(InputError, match="one CSV"):
        read_record(
            io.BytesIO(raw),
            spec,
            first.locator.model_copy(
                update={
                    "byte_length": first.locator.byte_length
                    + second.locator.byte_length
                }
            ),
            first.context,
        )
    with pytest.raises(InputError, match="overlaps"):
        read_record(
            io.BytesIO(raw),
            spec,
            first.locator.model_copy(update={"offset": 0}),
            first.context,
        )
    with pytest.raises(InputError, match="length mismatch"):
        read_record(io.BytesIO(raw[:-1]), spec, second.locator, second.context)
    with pytest.raises(InputError, match="missing indexed CSV"):
        read_record(
            io.BytesIO(raw),
            spec,
            first.locator,
            ParsingContext(kind="response", format="csv"),
        )
    with pytest.raises(InputError, match="format mismatch"):
        read_record(
            io.BytesIO(raw),
            spec,
            first.locator,
            ParsingContext(kind="response", format="jsonl"),
        )


@pytest.mark.parametrize("raw", [b"", b"\n", b"{}\n{}\n"])
def test_jsonl_locator_requires_exactly_one_record(tmp_path: Path, raw: bytes) -> None:
    with pytest.raises(InputError):
        read_record(
            io.BytesIO(raw),
            InputSpec(path=tmp_path / "responses.jsonl"),
            RecordLocator(offset=0, byte_length=max(1, len(raw)), line=1),
            ParsingContext(kind="response", format="jsonl"),
        )


def test_verified_document_resolver_and_no_template_reopen(tmp_path: Path) -> None:
    from test_planning import rubric_spec

    spec = rubric_spec()
    assert spec.rubric is not None
    run = tmp_path / "run.json"
    rubric = tmp_path / "rubric.yaml"
    documents = {
        run: {
            "task_kind": "rubric",
            "input": "responses.csv",
            "rubric": "rubric.yaml",
            "provider": "synthetic",
            "endpoint": "categorical",
            "model": "unknown-model",
            "prompt": {"file": "template.txt"},
        },
        rubric: spec.rubric.model_dump(mode="json"),
    }
    visited: list[Path] = []

    def verified_reader(path: Path) -> object:
        visited.append(path)
        return documents[path]

    resolved = _load_run_spec(run, document_reader=verified_reader)
    assert visited == [run, rubric]
    assert resolved.input.path == tmp_path / "responses.csv"
    assert resolved.prompt.file == tmp_path / "template.txt"
    item = ResponseItem(item_id="001", text="Synthetic text")
    template = "Verified custom template."
    expected = list(plan_record(resolved, item, template_text=template))
    assert resolved.prompt.file is not None
    assert not resolved.prompt.file.exists()
    resolved.prompt.file.write_text(template, encoding="utf-8")
    assert list(iter_requests(resolved, [item])) == expected
    resolved.prompt.file.unlink()
    assert list(plan_record(resolved, item, template_text=template)) == expected
    assert parse_document('{"checked":true}', "original.json") == {"checked": True}
    assert parse_document("checked: true", "original.yaml") == {"checked": True}
    with pytest.raises(ValueError, match="extension"):
        parse_document("checked: true", "original.txt")
    # The ordinary loader still reads actual original files.
    with pytest.raises(ValueError):
        load_run_spec(run)


def test_single_record_plan_preserves_task_and_pair_ids() -> None:
    from test_planning import pair, pair_spec, rubric_spec

    for spec, record in (
        (rubric_spec(), ResponseItem(item_id="001", text="Synthetic text")),
        (pair_spec(), pair()),
    ):
        assert list(plan_record(spec, record)) == list(iter_requests(spec, [record]))


@pytest.mark.parametrize("format", ["csv", "jsonl"])
def test_parsing_context_for_empty_sources(tmp_path: Path, format: str) -> None:
    raw = b"item_id,text\r\n" if format == "csv" else b""
    spec = InputSpec(path=tmp_path / f"responses.{format}")
    context = parsing_context(io.BytesIO(raw), spec, kind="response")
    assert context.format == format
    assert list(iter_records(io.BytesIO(raw), spec, kind="response")) == []
    if format == "csv":
        assert context.header_byte_length == len(raw)
        assert context.csv_header == ("item_id", "text")
    else:
        assert context.header_byte_length == 0


def test_locator_rejects_split_physical_boundaries(tmp_path: Path) -> None:
    raw = b"item_id,text\r\n001,One\r\n002,Two\r\n"
    spec = InputSpec(path=tmp_path / "responses.csv")
    entry = next(iter_records(io.BytesIO(raw), spec, kind="response"))
    for update in (
        {"offset": entry.locator.offset + 1},
        {"byte_length": entry.locator.byte_length - 1},
        {"byte_length": entry.locator.byte_length - 2},
    ):
        with pytest.raises(InputError, match="boundary"):
            read_record(
                io.BytesIO(raw),
                spec,
                entry.locator.model_copy(update=update),
                entry.context,
            )


def test_scan_observes_exact_bytes_with_bounded_read_requests(tmp_path: Path) -> None:
    raw = b"item_id,text\n" + b"001,Synthetic\n" * 10_000

    class ObservedStream(io.BytesIO):
        def __init__(self) -> None:
            super().__init__(raw)
            self.digest = hashlib.sha256()
            self.count = 0

        def read(self, size: int | None = -1) -> bytes:
            assert size is not None and 0 <= size <= 64 * 1024
            value = super().read(size)
            self.digest.update(value)
            self.count += len(value)
            return value

    stream = ObservedStream()
    spec = InputSpec(path=tmp_path / "responses.csv")
    assert sum(1 for _ in iter_records(stream, spec, kind="response")) == 10_000
    assert stream.count == len(raw)
    assert stream.digest.hexdigest() == hashlib.sha256(raw).hexdigest()
