"""Read-only diagnostic inspection preserves checkpoints and observes active WAL."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest
from test_persistence_repository import _run

import judgmentllm.persistence.repository as repository_module
from judgmentllm.persistence.models import IntegrityError, NotFoundError
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


def test_preserved_active_wal_is_a_stable_committed_snapshot(tmp_path: Path) -> None:
    with Repository(tmp_path) as writer:
        _run(writer, "001")
        assert (tmp_path / "registry.sqlite3-wal").is_file()
        assert (tmp_path / "registry.sqlite3-shm").is_file()
        with (
            ReadOnlyRepository(tmp_path, preserve_files=True) as snapshot_reader,
            ReadOnlyRepository(tmp_path) as default_reader,
        ):
            assert snapshot_reader.get_run("001").run_id == "001"
            assert default_reader.get_run("001").run_id == "001"
            _run(writer, "002")
            # Preserved inspection is one stable point in time. The historical
            # default reader retains its live committed-WAL behavior.
            with pytest.raises(NotFoundError):
                snapshot_reader.get_run("002")
            assert default_reader.get_run("002").run_id == "002"
            with ReadOnlyRepository(tmp_path, preserve_files=True) as reopened:
                assert reopened.get_run("002").run_id == "002"
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


def test_writer_checkpoint_during_open_cannot_create_inspection_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = Repository(tmp_path)
    _run(writer, "001")
    original_connect = sqlite3.connect

    def checkpoint_before_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        # Model the final writer closing after the preserve_files WAL/SHM gate
        # and before SQLite opens the reader. A safe inspection may decline a
        # changed snapshot, but must not create replacement coordination files.
        writer.close()
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", checkpoint_before_connect)
    try:
        try:
            with ReadOnlyRepository(tmp_path, preserve_files=True) as reader:
                assert reader.get_run("001").run_id == "001"
        except IntegrityError:
            pass
        assert not (tmp_path / "registry.sqlite3-wal").exists()
        assert not (tmp_path / "registry.sqlite3-shm").exists()
    finally:
        writer.close()


@pytest.fixture
def inspection_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    original = tempfile.TemporaryDirectory
    parent = tmp_path / "private-snapshots"
    parent.mkdir()
    created: list[Path] = []

    def recording_temporary(
        *args: Any, **kwargs: Any
    ) -> tempfile.TemporaryDirectory[str]:
        kwargs["dir"] = str(parent)
        temporary = original(*args, **kwargs)
        created.append(Path(temporary.name))
        return temporary

    monkeypatch.setattr(tempfile, "TemporaryDirectory", recording_temporary)
    return created


def test_active_snapshot_is_private_bounded_and_removed_on_close(
    tmp_path: Path,
    inspection_snapshots: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "store"
    original_copy = shutil.copyfileobj
    chunk_sizes: list[int] = []

    def bounded_copy(source: Any, destination: Any, length: int = 0) -> None:
        chunk_sizes.append(length)
        original_copy(source, destination, length=length)

    monkeypatch.setattr(shutil, "copyfileobj", bounded_copy)
    with Repository(root) as writer:
        _run(writer, "001")
        before = snapshot(root)
        with ReadOnlyRepository(root, preserve_files=True) as reader:
            assert reader.root == root.resolve()
            assert reader.get_run("001").run_id == "001"
            assert len(inspection_snapshots) == 1
            temporary = inspection_snapshots[0]
            assert temporary.is_dir()
            if os.name != "nt":
                assert temporary.stat().st_mode & 0o777 == 0o700
            assert all(path.is_file() for path in temporary.iterdir())
            assert not (temporary / "runs").exists()
        assert chunk_sizes == [65_536, 65_536]
        assert not inspection_snapshots[0].exists()
        assert snapshot(root) == before


@pytest.mark.parametrize(
    "failure", ["copy", "database_change", "wal_change", "open", "verify"]
)
def test_active_snapshot_failure_discards_private_copies(
    tmp_path: Path,
    inspection_snapshots: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    root = tmp_path / "store"
    with Repository(root) as writer:
        _run(writer, "001")
        before = snapshot(root)
        if failure in {"copy", "database_change", "wal_change"}:
            original_copy = shutil.copyfileobj
            copied = 0

            def failing_copy(source: Any, destination: Any, length: int = 0) -> None:
                nonlocal copied
                if failure == "copy":
                    raise OSError("synthetic copy failure")
                original_copy(source, destination, length=length)
                copied += 1
                if copied == 1:
                    if failure == "database_change":
                        database = root / "registry.sqlite3"
                        stat = database.stat()
                        os.utime(database, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
                    else:
                        _run(writer, "002")

            monkeypatch.setattr(shutil, "copyfileobj", failing_copy)
        elif failure == "open":

            def failing_open(*_args: Any, **_kwargs: Any) -> Any:
                raise sqlite3.OperationalError("synthetic open failure")

            monkeypatch.setattr(sqlite3, "connect", failing_open)
        else:

            def failing_verify(_connection: sqlite3.Connection) -> None:
                raise IntegrityError("synthetic verification failure")

            monkeypatch.setattr(repository_module, "verify", failing_verify)
        with pytest.raises((IntegrityError, sqlite3.OperationalError)):
            ReadOnlyRepository(root, preserve_files=True)
        assert len(inspection_snapshots) == 1
        assert not inspection_snapshots[0].exists()
        if failure != "wal_change":
            assert snapshot(root) == before
