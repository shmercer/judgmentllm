"""Real filesystem synchronization of owned export staging, including Windows."""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
from typing import Literal

import pytest
from test_execution import setup

from judgmentllm.execution import execute_run
from judgmentllm.execution.exports import export_run
from judgmentllm.persistence.repository import Repository
from judgmentllm.providers import MockAdapter


def staging_file(store: Path, descriptor: int) -> Path | None:
    """Identify only the owned staging descriptor, never an artifact descriptor."""
    descriptor_stat = os.fstat(descriptor)
    for candidate in store.rglob(".export-*/results"):
        if os.path.samestat(candidate.stat(), descriptor_stat):
            return candidate
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize(
    "format,view", [("jsonl", "canonical"), ("csv", "canonical"), ("csv", "r")]
)
async def test_staging_fsync_has_writable_descriptor_and_complete_exact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    empty: bool,
    format: Literal["jsonl", "csv"],
    view: Literal["canonical", "r"],
) -> None:
    spec, store, ledger = setup(tmp_path, responses=0 if empty else 2)
    await execute_run("001", store=store, adapter=MockAdapter(ledger), spec=spec)
    synchronize = os.fsync
    synchronized: list[bytes] = []

    def checked_fsync(descriptor: int) -> None:
        path = staging_file(store, descriptor)
        if path is not None:
            # These are real OS reads/writes and fsync calls on every platform.
            # Rewriting the same byte proves write access, even where fsync on
            # a read-only descriptor happens to succeed. Empty files are restored
            # to exactly zero bytes before synchronization and publication.
            before = path.read_bytes()
            position = os.lseek(descriptor, 0, os.SEEK_CUR)
            os.lseek(descriptor, 0, os.SEEK_SET)
            assert os.write(descriptor, before[:1] or b"\0") == 1
            if not before:
                os.ftruncate(descriptor, 0)
            os.lseek(descriptor, position, os.SEEK_SET)
            assert path.read_bytes() == before
            synchronized.append(before)
        synchronize(descriptor)

    with monkeypatch.context() as context:
        context.setattr(os, "fsync", checked_fsync)
        published = export_run("001", store=store, format=format, view=view)
    assert len(synchronized) == 1
    actual = Path(published.artifact_path).read_bytes()
    assert actual == synchronized[0]
    assert published.artifact.sha256 == hashlib.sha256(actual).hexdigest()
    assert published.artifact.byte_length == len(actual)
    assert published.row_count == (0 if empty else 2)
    assert export_run("001", store=store, format=format, view=view) == published
    assert not list(store.rglob(".export-*"))


@pytest.mark.parametrize(
    "format,view", [("jsonl", "canonical"), ("csv", "canonical"), ("csv", "r")]
)
def test_staging_sync_error_stops_publication_and_allows_exact_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    format: Literal["jsonl", "csv"],
    view: Literal["canonical", "r"],
) -> None:
    _, store, _ = setup(tmp_path, responses=0)
    synchronize = os.fsync
    attempted: list[bytes] = []
    boundaries: list[str] = []

    def failed_fsync(descriptor: int) -> None:
        path = staging_file(store, descriptor)
        if path is not None:
            attempted.append(path.read_bytes())
            raise OSError(errno.EIO, "synthetic staging synchronization failure")
        synchronize(descriptor)

    with monkeypatch.context() as context:
        context.setattr(os, "fsync", failed_fsync)
        with pytest.raises(OSError, match="synthetic staging synchronization failure"):
            export_run(
                "001", store=store, format=format, view=view, fault=boundaries.append
            )
    assert len(attempted) == 1
    assert "after_export_serialization" not in boundaries
    assert not list(store.rglob(".export-*"))
    with Repository(store) as repository:
        assert (
            repository.connection.execute("SELECT COUNT(*) FROM exports").fetchone()[0]
            == 0
        )
    recovered = export_run("001", store=store, format=format, view=view)
    assert Path(recovered.artifact_path).read_bytes() == attempted[0]
    assert recovered.artifact.sha256 == hashlib.sha256(attempted[0]).hexdigest()
