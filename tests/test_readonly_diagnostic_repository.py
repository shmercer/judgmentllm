"""Read-only diagnostic inspection preserves checkpoints and observes active WAL."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_persistence_repository import _run

from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository


def checkpointed(root: Path) -> None:
    with Repository(root) as repository:
        _run(repository, "001")
    assert not (root / "registry.sqlite3-wal").exists()
    assert not (root / "registry.sqlite3-shm").exists()


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_preserved_checkpointed_inspection_creates_no_sidecars(tmp_path: Path) -> None:
    checkpointed(tmp_path)
    before = snapshot(tmp_path)
    for _ in range(2):
        with ReadOnlyRepository(tmp_path, preserve_files=True) as repository:
            assert repository.get_run("001").run_id == "001"
            assert not (tmp_path / "registry.sqlite3-wal").exists()
            assert not (tmp_path / "registry.sqlite3-shm").exists()
    assert snapshot(tmp_path) == before


def test_preserved_active_wal_observes_new_committed_rows(tmp_path: Path) -> None:
    with Repository(tmp_path) as writer:
        _run(writer, "001")
        assert (tmp_path / "registry.sqlite3-wal").is_file()
        assert (tmp_path / "registry.sqlite3-shm").is_file()
        with ReadOnlyRepository(tmp_path, preserve_files=True) as reader:
            assert reader.get_run("001").run_id == "001"
            _run(writer, "002")
            assert reader.get_run("002").run_id == "002"
        assert writer.get_run("002").run_id == "002"


@pytest.mark.parametrize("writer_closes", [False, True])
def test_new_writer_invalidates_preserved_checkpoint_snapshot(
    tmp_path: Path, writer_closes: bool
) -> None:
    checkpointed(tmp_path)
    reader = ReadOnlyRepository(tmp_path, preserve_files=True)
    assert reader.get_run("001").run_id == "001"
    writer = Repository(tmp_path)
    try:
        _run(writer, "002")
        if writer_closes:
            writer.close()
        with pytest.raises(IntegrityError, match="changed during inspection"):
            reader.close()
        # A failed snapshot check still closes the underlying connection.
        with pytest.raises(sqlite3.ProgrammingError):
            reader.connection.execute("SELECT 1")
    finally:
        writer.close()


@pytest.mark.parametrize("preserve", [None, 0, 1, "true", [], {}])
def test_preserve_flag_rejects_implicit_conversion(
    tmp_path: Path, preserve: Any
) -> None:
    with pytest.raises(ValueError, match="preserve_files must be a boolean"):
        ReadOnlyRepository(tmp_path, preserve_files=preserve)
    assert not tuple(tmp_path.iterdir())


def test_missing_wal_coordination_fails_without_creating_shm(tmp_path: Path) -> None:
    checkpointed(tmp_path)
    wal = tmp_path / "registry.sqlite3-wal"
    wal.write_bytes(b"synthetic orphan WAL")
    before = snapshot(tmp_path)
    with pytest.raises(IntegrityError, match="coordination is unavailable"):
        ReadOnlyRepository(tmp_path, preserve_files=True)
    assert not (tmp_path / "registry.sqlite3-shm").exists()
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("component", ["database", "wal", "shm", "broken_wal"])
def test_preserved_inspection_rejects_registry_symlinks_before_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    root = tmp_path / "store"
    checkpointed(root)
    target = tmp_path / "external-synthetic-target"
    target.write_bytes(b"synthetic target must remain unchanged")
    database = root / "registry.sqlite3"
    if component == "database":
        target.write_bytes(database.read_bytes())
        database.unlink()
        link = database
    elif component == "wal" or component == "broken_wal":
        link = root / "registry.sqlite3-wal"
        (root / "registry.sqlite3-shm").write_bytes(b"synthetic coordination")
        if component == "broken_wal":
            target.unlink()
    else:
        link = root / "registry.sqlite3-shm"
        (root / "registry.sqlite3-wal").write_bytes(b"synthetic WAL")
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("Symlink creation is unavailable on this platform")
    before = target.read_bytes() if target.exists() else None

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Symlink admission must fail before SQLite opens")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with pytest.raises(IntegrityError):
        ReadOnlyRepository(root, preserve_files=True)
    assert (target.read_bytes() if target.exists() else None) == before
