"""Offline ownership checks; the OS lock outlives heartbeat metadata."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

import pytest

from judgmentllm.persistence.models import NotFoundError, OwnershipError, RunRecord
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[Repository]:
    with Repository(tmp_path) as repository:
        repository.create_run(
            RunRecord(
                run_id="run",
                task_kind="rubric",
                provider="mock",
                endpoint="judgments",
                model="model",
            )
        )
        yield repository


def test_acquire_heartbeat_release_and_read_access(registry: Repository) -> None:
    ownership = ExecutorOwnership(registry, "run")
    with ownership:
        owner = registry.get_owner("run")
        assert owner is not None
        assert owner.pid == os.getpid()
        assert owner.host
        assert owner.token
        with Repository(registry.root) as reader:
            assert reader.get_run("run").run_id == "run"
            assert reader.get_owner("run") == owner
        ownership.heartbeat()
        after = registry.get_owner("run")
        assert after is not None
        assert after.token == owner.token
        assert after.heartbeat_at >= owner.heartbeat_at
        with pytest.raises(OwnershipError, match="already acquired"):
            ownership.acquire()
    assert registry.get_owner("run") is None
    assert (registry.run_directory("run") / "executor.lock").is_file()
    ownership.release()
    with pytest.raises(OwnershipError, match="not acquired"):
        ownership.heartbeat()


def test_same_process_competing_handles_cannot_acquire(registry: Repository) -> None:
    with ExecutorOwnership(registry, "run"):
        with pytest.raises(OwnershipError, match="another executor"):
            ExecutorOwnership(registry, "run").acquire()


def test_unknown_run_does_not_create_lock(registry: Repository) -> None:
    with pytest.raises(NotFoundError):
        ExecutorOwnership(registry, "missing").acquire()
    assert not (registry.root / "runs").exists()


_COMPETE = """
import sys
from judgmentllm.persistence.models import OwnershipError
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
with Repository(sys.argv[1]) as repository:
    try:
        with ExecutorOwnership(repository, 'run'):
            print('acquired')
    except OwnershipError:
        sys.exit(23)
"""


def test_independent_process_cannot_take_live_lock(registry: Repository) -> None:
    with ExecutorOwnership(registry, "run"):
        result = subprocess.run(
            [sys.executable, "-c", _COMPETE, str(registry.root)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 23, result.stderr
        assert not result.stdout
    result = subprocess.run(
        [sys.executable, "-c", _COMPETE, str(registry.root)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "acquired"


_HOLD = """
import sys
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
with Repository(sys.argv[1]) as repository:
    with ExecutorOwnership(repository, 'run'):
        print('ready', flush=True)
        sys.stdin.readline()
"""


def test_process_death_releases_lock_and_recovers_stale_metadata(
    registry: Repository,
) -> None:
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", _HOLD, str(registry.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while registry.get_owner("run") is None and child.poll() is None:
            assert time.monotonic() < deadline, "child did not acquire ownership"
            time.sleep(0.01)
        owner = registry.get_owner("run")
        assert owner is not None
        assert owner.pid == child.pid
        child.kill()
        child.communicate(timeout=15)
        assert registry.get_owner("run") == owner
        with ExecutorOwnership(registry, "run") as recovered:
            assert recovered.owner is not None
            assert recovered.owner.token != owner.token
            assert recovered.owner.pid == os.getpid()
        assert registry.get_owner("run") is None
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=15)


def test_metadata_age_never_permits_takeover(registry: Repository) -> None:
    with ExecutorOwnership(registry, "run") as active:
        assert active.owner is not None
        # Metadata may be stale or even absent; only the OS lock authorizes use.
        registry.clear_owner("run", active.owner.token)
        try:
            with pytest.raises(OwnershipError, match="another executor"):
                ExecutorOwnership(registry, "run").acquire()
        finally:
            registry.set_owner("run", active.owner)


def test_wrong_process_cannot_heartbeat_or_unlock_parent(
    registry: Repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = ExecutorOwnership(registry, "run")
    active.acquire()
    with monkeypatch.context() as patch:
        patch.setattr("judgmentllm.persistence.ownership.os.getpid", lambda: -1)
        with pytest.raises(OwnershipError, match="another process"):
            active.heartbeat()
        # Do not simulate release on the parent's real descriptor here: a real
        # fork has a separate descriptor table. Cover that case below on Unix.
    active.release()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork inheritance is Unix-specific")
def test_forked_child_release_does_not_unlock_parent(registry: Repository) -> None:
    with ExecutorOwnership(registry, "run") as active:
        pid = os.fork()
        if pid == 0:
            try:
                active.release()
            except OwnershipError:
                os._exit(0)
            os._exit(1)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        with pytest.raises(OwnershipError, match="another executor"):
            ExecutorOwnership(registry, "run").acquire()


def test_database_failure_during_acquire_releases_os_lock(
    registry: Repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_: object) -> None:
        raise RuntimeError("synthetic failure")

    with monkeypatch.context() as patch:
        patch.setattr(registry, "set_owner", fail)
        with pytest.raises(RuntimeError, match="synthetic failure"):
            ExecutorOwnership(registry, "run").acquire()
    with ExecutorOwnership(registry, "run"):
        pass


def test_database_failure_during_release_releases_os_lock(
    registry: Repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_: object) -> None:
        raise RuntimeError("synthetic failure")

    active = ExecutorOwnership(registry, "run")
    active.acquire()
    with monkeypatch.context() as patch:
        patch.setattr(registry, "clear_owner", fail)
        with pytest.raises(RuntimeError, match="synthetic failure"):
            active.release()
    with ExecutorOwnership(registry, "run"):
        pass


def test_interruption_at_lock_boundary_releases_handle(
    registry: Repository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from judgmentllm.persistence import ownership

    original = ownership._lock  # pyright: ignore[reportPrivateUsage]

    def interrupted(stream: BinaryIO) -> None:
        # An interruption can arrive after the OS acquired the lock but before
        # the caller received control. Closing the handle must release it.
        original(stream)
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(ownership, "_lock", interrupted)
        with pytest.raises(KeyboardInterrupt):
            ExecutorOwnership(registry, "run").acquire()
    assert registry.get_owner("run") is None
    with ExecutorOwnership(registry, "run"):
        pass


def test_run_identifiers_cannot_choose_lock_path(registry: Repository) -> None:
    identifier = "../001"
    registry.create_run(
        RunRecord(
            run_id=identifier,
            task_kind="rubric",
            provider="mock",
            endpoint="judgments",
            model="model",
        )
    )
    with ExecutorOwnership(registry, identifier):
        directory = registry.run_directory(identifier)
        assert directory.parent == registry.root / "runs"
        assert ".." not in directory.parts
        assert (directory / "executor.lock").is_file()


def test_symlink_lock_file_rejected(registry: Repository, tmp_path: Path) -> None:
    directory = registry.run_directory("run")
    directory.mkdir(parents=True)
    target = tmp_path / "other-lock"
    target.write_bytes(b"sentinel")
    try:
        (directory / "executor.lock").symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(OwnershipError, match="symlink"):
        ExecutorOwnership(registry, "run").acquire()
    assert target.read_bytes() == b"sentinel"


def test_symlink_run_directory_rejected(registry: Repository, tmp_path: Path) -> None:
    directory = registry.run_directory("run")
    directory.parent.mkdir(parents=True)
    target = tmp_path / "another-run"
    target.mkdir()
    try:
        directory.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(OwnershipError, match="symlink"):
        ExecutorOwnership(registry, "run").acquire()
    assert not (target / "executor.lock").exists()
