"""Bounded source/index repository operations retain typed metadata only."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.models import (
    IntegrityError,
    ManifestReference,
    OwnershipError,
    RunRecord,
)
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
from judgmentllm.persistence.sources import InputRecord, SourceRecord


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[tuple[Repository, str]]:
    with Repository(tmp_path / "store") as repository:
        repository.create_run(
            RunRecord(
                run_id="001",
                task_kind="rubric",
                provider="fixture",
                endpoint="text",
                model="test",
            )
        )
        with ExecutorOwnership(repository, "001") as owner:
            assert owner.owner is not None
            yield repository, owner.owner.token


def source(**updates: object) -> SourceRecord:
    return SourceRecord.model_validate(
        {
            "run_id": "001",
            "source_id": "input",
            "role": "response",
            "ordinal": 0,
            "original_location": "/synthetic/input.csv",
            "raw_sha256": bytes_digest(b"bytes"),
            "byte_length": 5,
            "format": "csv",
            "generation_json": '{"dev":1,"ino":2,"size":5,"mtime_ns":3,"ctime_ns":4}',
            **updates,
        }
    )


def record(**updates: object) -> InputRecord:
    return InputRecord.model_validate(
        {
            "run_id": "001",
            "record_kind": "response",
            "record_id": "001",
            "source_id": "input",
            "ordinal": 0,
            "record_sha256": bytes_digest(b"record"),
            "byte_offset": 0,
            "byte_length": 5,
            **updates,
        }
    )


def test_source_records_ownership_order_and_reopen(
    owned: tuple[Repository, str],
) -> None:
    repository, token = owned
    with pytest.raises(OwnershipError):
        repository.add_source(source(), owner_token="wrong")
    repository.add_source(source(), owner_token=token)
    repository.add_source(source(source_id="later", ordinal=1), owner_token=token)
    assert [item.source_id for item in repository.iter_sources("001")] == [
        "input",
        "later",
    ]
    assert repository.get_source("001", "input") == source()
    repository.add_input_records((record(),), owner_token=token)
    with Repository(repository.root) as second:
        assert second.get_input_record("001", "response", "001") == record()
        assert list(second.iter_input_records("001", page_size=1)) == [record()]
        assert list(second.iter_input_records("001", "pair")) == []
    with pytest.raises(ValueError, match="positive"):
        list(repository.iter_input_records("001", page_size=0))


def test_source_snapshot_and_seal(owned: tuple[Repository, str]) -> None:
    repository, token = owned
    repository.add_source(source(), owner_token=token)
    store = ArtifactStore(repository.run_directory("001"))
    snapshot = store.put(b"bytes")
    repository.register_artifact("001", snapshot)
    repository.update_source_snapshot(
        "001", "input", snapshot.sha256, owner_token=token
    )
    assert repository.get_source("001", "input").snapshot_sha256 == snapshot.sha256
    assert list(repository.iter_artifacts("001")) == [snapshot]
    wrong = store.put(b"other")
    repository.register_artifact("001", wrong)
    with pytest.raises(IntegrityError, match="differs"):
        repository.update_source_snapshot(
            "001", "input", wrong.sha256, owner_token=token
        )
    absent = store.hash_only(bytes_digest(b"absent"), 6)
    repository.register_artifact("001", absent)
    with pytest.raises(IntegrityError, match="retained"):
        repository.update_source_snapshot(
            "001", "input", absent.sha256, owner_token=token
        )
    manifest = store.put(b"{}")
    repository.seal_run("001", ManifestReference(artifact=manifest))
    with pytest.raises(IntegrityError, match="sealed"):
        repository.add_source(source(source_id="late", ordinal=1), owner_token=token)
    with pytest.raises(IntegrityError, match="sealed"):
        repository.update_source_snapshot(
            "001", "input", snapshot.sha256, owner_token=token
        )


def test_input_batch_duplicate_locator_kind_and_token_guards(
    owned: tuple[Repository, str],
) -> None:
    repository, token = owned
    repository.add_source(source(), owner_token=token)
    with pytest.raises(OwnershipError):
        repository.add_input_records((record(),), owner_token="wrong")
    with pytest.raises(IntegrityError, match="exceeds"):
        repository.add_input_records((record(byte_offset=1),), owner_token=token)
    repository.add_source(source(source_id="rubric", role="rubric"), owner_token=token)
    with pytest.raises(IntegrityError, match="role"):
        repository.add_input_records((record(source_id="rubric"),), owner_token=token)
    repository.add_input_records((record(),), owner_token=token)
    with pytest.raises(IntegrityError):
        repository.add_input_records((record(ordinal=1),), owner_token=token)
    with pytest.raises(ValueError, match="1024"):
        repository.add_input_records((record(),) * 1025, owner_token=token)
    assert list(repository.iter_input_records("001")) == [record()]


@pytest.mark.parametrize(
    "generation",
    [
        '{"unexpected":"source-sentinel"}',
        "[]",
        '{"size":true}',
        '{"size":1,"size":2}',
        "bad-json",
    ],
)
def test_generation_metadata_is_numeric_and_allowlisted(generation: str) -> None:
    with pytest.raises(ValidationError):
        source(generation_json=generation)


def test_structural_metadata_excludes_source_text_and_keeps_pair_orientation() -> None:
    for structure in (
        '{"text":"student-sentinel"}',
        '{"metadata":{"note":"sentinel"}}',
    ):
        with pytest.raises(ValidationError):
            record(structural_json=structure)
    pair = {
        "pair_id": "001",
        "pair_uid": "0001",
        "left_item_id": "001",
        "right_item_id": "002",
        "trait_id": "quality",
        "original_order": ["001", "002"],
        "presented_order": ["002", "001"],
    }
    current = record(
        record_kind="pair", record_id="0001", structural_json=json.dumps(pair)
    )
    assert json.loads(current.structural_json)["presented_order"] == ["002", "001"]
    with pytest.raises(ValidationError):
        record(record_kind="pair", structural_json=json.dumps(pair))
    with pytest.raises(ValidationError):
        record(
            record_kind="pair",
            record_id="0001",
            structural_json=json.dumps({**pair, "metadata": {"text": "sentinel"}}),
        )
