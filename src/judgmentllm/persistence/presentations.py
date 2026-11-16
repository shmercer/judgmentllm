"""Owner-checked source presentation indexes, independent of physical grouping."""

from __future__ import annotations

from collections.abc import Iterator

from pydantic import Field

from judgmentllm.decisions.records import PairPresentationInputV1
from judgmentllm.persistence.hashing import canonical_bytes, semantic_digest
from judgmentllm.persistence.models import SHA256, ExecutionModel, IntegrityError
from judgmentllm.persistence.repository import Repository


class PresentationInputIndexV1(ExecutionModel):
    run_id: str
    source_presentation_id: str
    source_id: str
    ordinal: int = Field(ge=0)
    byte_offset: int = Field(ge=0)
    byte_length: int = Field(gt=0)
    record_sha256: SHA256
    structure_json: str


def add_presentation(
    repository: Repository,
    index: PresentationInputIndexV1,
    source: PairPresentationInputV1,
    owner_token: str,
) -> None:
    repository.require_decisions_storage()
    source = PairPresentationInputV1.model_validate_json(canonical_bytes(source))
    if (
        index.source_presentation_id != source.source_presentation_id
        or index.record_sha256 != semantic_digest("presentation-input-v1", source)
        or index.structure_json != canonical_bytes(source).decode("utf-8")
    ):
        raise IntegrityError("presentation index differs from source")
    with repository.execution_transaction(index.run_id, owner_token):
        repository._source_write_owner(index.run_id, owner_token)  # pyright: ignore[reportPrivateUsage]
        parent = repository.get_source(index.run_id, index.source_id)
        if (
            parent.role != "pair_presentations"
            or index.byte_offset + index.byte_length > parent.byte_length
        ):
            raise IntegrityError("presentation locator differs from source")
        for item_id in (source.left_item_id, source.right_item_id):
            repository.get_input_record(index.run_id, "response", item_id)
        repository.connection.execute(
            "INSERT INTO presentation_inputs VALUES (?,?,?,?,?,?,?,?)",
            (
                index.run_id,
                index.source_presentation_id,
                index.source_id,
                index.ordinal,
                index.byte_offset,
                index.byte_length,
                index.record_sha256,
                index.structure_json,
            ),
        )
        repository.connection.executemany(
            "INSERT INTO presentation_source_members VALUES (?,?,?,?,?)",
            (
                (
                    index.run_id,
                    source.source_presentation_id,
                    m.pair_uid,
                    m.trait_id,
                    position,
                )
                for position, m in enumerate(source.members)
            ),
        )


def iter_presentations(
    repository: Repository, run_id: str, *, page_size: int = 128
) -> Iterator[PresentationInputIndexV1]:
    repository.require_decisions_storage()
    if type(page_size) is not int or page_size < 1:
        raise ValueError("page size must be positive")
    after = -1
    while True:
        rows = repository.connection.execute(
            "SELECT * FROM presentation_inputs WHERE run_id=? AND ordinal>? ORDER BY ordinal LIMIT ?",
            (run_id, after, page_size),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            yield PresentationInputIndexV1.model_validate(dict(row))
        after = int(rows[-1]["ordinal"])


def get_presentation(
    repository: Repository, run_id: str, source_id: str
) -> PresentationInputIndexV1:
    row = repository.connection.execute(
        "SELECT * FROM presentation_inputs WHERE run_id=? AND source_presentation_id=?",
        (run_id, source_id),
    ).fetchone()
    if row is None:
        raise IntegrityError("presentation source is unavailable")
    return PresentationInputIndexV1.model_validate(dict(row))


def verify_members(
    repository: Repository,
    index: PresentationInputIndexV1,
    source: PairPresentationInputV1,
) -> None:
    rows = repository.connection.execute(
        "SELECT source_pair_uid,trait_id,position FROM presentation_source_members WHERE run_id=? AND source_presentation_id=? ORDER BY position",
        (index.run_id, index.source_presentation_id),
    ).fetchall()
    if [tuple(r) for r in rows] != [
        (m.pair_uid, m.trait_id, i) for i, m in enumerate(source.members)
    ]:
        raise IntegrityError("presentation source membership changed")
