"""Privacy scanning accounts only for the exact Windows-owned lock byte."""

from __future__ import annotations

import mmap
import sys
from pathlib import Path

import pytest
from test_execution import read_live_runtime_file

from judgmentllm.persistence.models import RunRecord
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository


def test_native_lock_read_and_post_release_privacy(tmp_path: Path) -> None:
    with Repository(tmp_path / "store") as repository:
        repository.create_run(
            RunRecord(
                run_id="001",
                task_kind="rubric",
                provider="mock",
                endpoint="synthetic",
                model="fixture",
            )
        )
        lock = repository.run_directory("001") / "executor.lock"
        deferred: set[Path] = set()
        with ExecutorOwnership(repository, "001"):
            if sys.platform == "win32":
                with pytest.raises(PermissionError):
                    lock.read_bytes()
            else:
                assert lock.read_bytes() == b"\0"
            assert read_live_runtime_file(
                lock,
                root=tmp_path,
                executor_lock=lock,
                deferred=deferred,
                windows_locking=sys.platform == "win32",
            ) == (None if sys.platform == "win32" else b"\0")
            assert deferred == ({lock} if sys.platform == "win32" else set())
            # Database, WAL and SHM reads do not receive the lock-file exception.
            for path in repository.root.iterdir():
                if path.is_file():
                    assert (
                        read_live_runtime_file(
                            path,
                            root=tmp_path,
                            executor_lock=lock,
                            deferred=deferred,
                            windows_locking=sys.platform == "win32",
                        )
                        is not None
                    )
        assert lock.read_bytes() == b"\0"
        assert lock.stat().st_size == 1


@pytest.mark.parametrize(
    "role",
    [
        "registry.sqlite3",
        "registry.sqlite3-wal",
        "registry.sqlite3-shm",
        "ledger.sqlite3",
        "receipt.json",
        "pending.tmp",
        "other/executor.lock",
    ],
)
def test_unexpected_permission_failure_is_never_deferred(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    path = tmp_path / role
    deferred: set[Path] = set()

    def denied(_: Path, *args: object, **kwargs: object) -> bytes:
        raise PermissionError("synthetic access denial")

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(
        AssertionError, match="unexpected unreadable live runtime file"
    ) as error:
        read_live_runtime_file(
            path,
            root=tmp_path,
            executor_lock=tmp_path / "executor.lock",
            deferred=deferred,
            windows_locking=True,
        )
    assert role in str(error.value)
    assert not deferred


def test_lock_read_failure_is_not_allowed_on_other_platforms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = tmp_path / "executor.lock"
    lock.write_bytes(b"\0")

    def denied(_: Path, *args: object, **kwargs: object) -> bytes:
        raise PermissionError("synthetic access denial")

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(AssertionError, match="unexpected unreadable"):
        read_live_runtime_file(
            lock,
            root=tmp_path,
            executor_lock=lock,
            deferred=set(),
            windows_locking=False,
        )


@pytest.mark.parametrize("content", [b"", b"\0PRIVATE_ASSESSMENT_SENTINEL"])
def test_unexpected_lock_contents_cannot_hide_behind_windows_deferral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
) -> None:
    lock = tmp_path / "executor.lock"
    lock.write_bytes(content)

    def denied(_: Path, *args: object, **kwargs: object) -> bytes:
        raise PermissionError("synthetic access denial")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", denied)
        with pytest.raises(
            AssertionError, match="unexpected executor lock identity/length"
        ):
            read_live_runtime_file(
                lock,
                root=tmp_path,
                executor_lock=lock,
                deferred=set(),
                windows_locking=True,
            )
    assert lock.read_bytes() == content


def test_native_sqlite_wal_index_privacy_read_during_write(tmp_path: Path) -> None:
    with Repository(tmp_path / "store") as repository:
        repository.create_run(
            RunRecord(
                run_id="001",
                task_kind="rubric",
                provider="mock",
                endpoint="synthetic",
                model="fixture",
            )
        )
        shm = repository.root / "registry.sqlite3-shm"
        with repository.transaction():
            repository.connection.execute(
                "UPDATE runs SET checkpoint='1' WHERE run_id='001'"
            )
            assert shm.is_file()
            if sys.platform == "win32":
                with pytest.raises(PermissionError):
                    shm.read_bytes()
            deferred: set[Path] = set()
            mapped: set[Path] = set()
            content = read_live_runtime_file(
                shm,
                root=tmp_path,
                executor_lock=repository.run_directory("001") / "executor.lock",
                deferred=deferred,
                windows_locking=sys.platform == "win32",
                sqlite_shm=shm,
                mapped=mapped,
            )
            assert content is not None and len(content) == shm.stat().st_size
            assert not deferred
            assert mapped == ({shm} if sys.platform == "win32" else set())
            with shm.open("rb") as stream:
                with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as view:
                    assert content == view[:]
        assert len(shm.read_bytes()) == len(content)


def test_mapped_sqlite_index_still_exposes_every_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shm = tmp_path / "registry.sqlite3-shm"
    sentinel = b"RAW_PRIVATE_SHM_REGRESSION"
    shm.write_bytes(sentinel)

    def denied(_: Path, *args: object, **kwargs: object) -> bytes:
        raise PermissionError("synthetic SQLite byte-range lock")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", denied)
        deferred: set[Path] = set()
        mapped: set[Path] = set()
        content = read_live_runtime_file(
            shm,
            root=tmp_path,
            executor_lock=tmp_path / "executor.lock",
            deferred=deferred,
            windows_locking=True,
            sqlite_shm=shm,
            mapped=mapped,
        )
        assert (
            content == sentinel
        )  # The caller's existing sentinel assertion must fail.
        assert sentinel in content
        assert mapped == {shm} and not deferred
    assert shm.read_bytes() == sentinel


def test_sqlite_mapping_failure_is_not_exempted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shm = tmp_path / "registry.sqlite3-shm"
    shm.write_bytes(b"synthetic")

    def denied(_: Path, *args: object, **kwargs: object) -> bytes:
        raise PermissionError("synthetic access denial")

    monkeypatch.setattr(Path, "read_bytes", denied)
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(
        AssertionError, match="unreadable live SQLite WAL index: registry.sqlite3-shm"
    ):
        read_live_runtime_file(
            shm,
            root=tmp_path,
            executor_lock=tmp_path / "executor.lock",
            deferred=set(),
            windows_locking=True,
            sqlite_shm=shm,
            mapped=set(),
        )
