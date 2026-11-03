"""Record/index identities and validation shared by capture and verified sessions."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from typing import BinaryIO

from judgmentllm.config import (
    InputSpec,
)
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.persistence.hashing import (
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.sources import InputRecord, PairStructure
from judgmentllm.preparation.freezing import (
    PreparationError,
)
from judgmentllm.preparation.index import (
    ParsingContext,
    RecordLocator,
)

RecordReader = Callable[
    [BinaryIO, InputSpec, RecordLocator, ParsingContext], ResponseItem | PairTask
]


def record_digest(record: ResponseItem | PairTask) -> str:
    return semantic_digest("input-record", record)


def index_digest(records: Iterator[InputRecord]) -> tuple[str, int]:
    digest = hashlib.sha256(b"judgmentllm-input-index-v1\n")
    count = 0
    for record in records:
        digest.update(canonical_bytes(record))
        digest.update(b"\n")
        count += 1
    return digest.hexdigest(), count


def record_locator(record: InputRecord) -> RecordLocator:
    return RecordLocator(
        offset=record.byte_offset,
        byte_length=record.byte_length,
        line=record.ordinal + 1,
    )


def check_record(record: ResponseItem | PairTask, indexed: InputRecord) -> None:
    identifier = record.item_id if isinstance(record, ResponseItem) else record.pair_uid
    if (
        identifier != indexed.record_id
        or record_digest(record) != indexed.record_sha256
    ):
        raise PreparationError("indexed record ID or canonical hash is inconsistent")
    if isinstance(record, PairTask):
        projected = PairStructure.model_validate(
            record.model_dump(exclude={"metadata"})
        )
        if projected != PairStructure.model_validate_json(indexed.structural_json):
            raise PreparationError("indexed pair structure is inconsistent")
