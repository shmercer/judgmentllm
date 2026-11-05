"""The interruption tool owns process instances; journal PIDs are untrusted data."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import cli_process_supervisor as supervisor


def evidence(pid: int | None, *, token: str | None = "owner") -> dict[str, Any]:
    return {
        "runs": [{"run_id": "run-001", "owner_pid": pid, "owner_token": token}],
        "results": [{"record_sha256": "a" * 64}],
        "submissions": [{"state": "submitting"}],
        "acceptances": [{"acceptance_count": 1}],
    }


def wait_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"Worker did not publish {path.name}"


def sentinel_process(directory: Path) -> supervisor.OwnedProcess:
    # A Windows venv executable may launch another interpreter. Give the
    # unrelated sentinel its own ownership object too, so cleanup joins its tree.
    with (
        (directory / "sentinel-out").open("wb") as stdout,
        (directory / "sentinel-err").open("wb") as stderr,
    ):
        return supervisor.launch(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            directory,
            stdout,
            stderr,
        )


def test_os_ownership_rejects_foreign_pid_and_only_terminates_owned_process(
    tmp_path: Path,
) -> None:
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    sentinel = sentinel_process(tmp_path)
    try:
        with (
            (tmp_path / "out").open("wb") as stdout,
            (tmp_path / "err").open("wb") as stderr,
        ):
            owned = supervisor.launch(command, tmp_path, stdout, stderr)
            try:
                assert owned.owns(owned.pid)
                assert not owned.owns(sentinel.pid)
                assert not owned.owns(0)
                owned.terminate(timeout=5)
                assert sentinel.owns(sentinel.pid)
            finally:
                owned.close()
            assert not owned.owns(owned.pid)
    finally:
        sentinel.close()


@pytest.mark.parametrize(
    "identity", ["foreign", "stale", "wrapped", "missing", "wrong_token", "wrong_run"]
)
def test_substituted_journal_identity_never_signals_unrelated_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    sentinel = sentinel_process(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    captured: list[supervisor.OwnedProcess] = []
    original = supervisor.launch
    progress = json.dumps(
        {"event": "progress", "category": "prepared", "run_id": "run-001"}
    )

    def launch(
        _: list[str], cwd: Path, stdout: BinaryIO, stderr: BinaryIO
    ) -> supervisor.OwnedProcess:
        worker = original(
            [
                sys.executable,
                "-c",
                f"import sys,time; print({progress!r},file=sys.stderr,flush=True); time.sleep(30)",
            ],
            cwd,
            stdout,
            stderr,
        )
        captured.append(worker)
        return worker

    def read(directory: Path) -> dict[str, Any]:
        if not (directory / "killed-stderr.jsonl").read_text(encoding="utf-8").strip():
            return {}
        selected = (
            sentinel.pid + 2**32
            if identity == "wrapped"
            else sentinel.pid
            if identity == "foreign"
            else dead.pid
            if identity == "stale"
            else None
            if identity == "missing"
            else captured[0].pid
        )
        result = evidence(
            selected, token=None if identity == "wrong_token" else "owner"
        )
        if identity == "wrong_run":
            result["runs"][0]["run_id"] = "substituted-run"
        return result

    monkeypatch.setattr(supervisor, "launch", launch)
    try:
        with pytest.raises(supervisor.ProcessIdentityError):
            supervisor.interrupt_run(
                tmp_path, Path(sys.executable), read, boundary_timeout=5
            )
        assert len(captured) == 1 and not captured[0].owns(captured[0].pid)
        assert sentinel.owns(sentinel.pid)
    finally:
        sentinel.close()
        dead.wait(timeout=5)


def test_boundary_timeout_cleans_up_only_owned_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[supervisor.OwnedProcess] = []
    original = supervisor.launch

    def launch(
        _: list[str], cwd: Path, stdout: BinaryIO, stderr: BinaryIO
    ) -> supervisor.OwnedProcess:
        worker = original(
            [sys.executable, "-c", "import time; time.sleep(30)"], cwd, stdout, stderr
        )
        captured.append(worker)
        return worker

    monkeypatch.setattr(supervisor, "launch", launch)
    with pytest.raises(TimeoutError, match="durable submitting boundary"):
        supervisor.interrupt_run(
            tmp_path, Path(sys.executable), lambda _: {}, boundary_timeout=0.05
        )
    assert len(captured) == 1 and not captured[0].owns(captured[0].pid)


def test_existing_store_is_rejected_before_any_process_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "registry.sqlite3").touch()

    def forbidden(*_: object) -> supervisor.OwnedProcess:
        raise AssertionError("Must not start an executor for an existing fixture")

    monkeypatch.setattr(supervisor, "launch", forbidden)
    with pytest.raises(supervisor.ProcessIdentityError, match="fresh fixture"):
        supervisor.interrupt_run(tmp_path, Path(sys.executable), lambda _: {})


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX handler controls delayed SIGTERM exit; Windows waits on job handles",
)
def test_termination_waits_for_actual_exit_without_a_fixed_sleep(
    tmp_path: Path,
) -> None:
    ready, received, release, exited = [
        tmp_path / name for name in ("ready", "received", "release", "exited")
    ]
    code = """
import signal,sys,time
from pathlib import Path
ready,received,release,exited=map(Path,sys.argv[1:])
def stop(signum, frame):
    received.touch()
    while not release.exists(): time.sleep(0.005)
    exited.touch()
    raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
ready.touch()
while True: signal.pause()
"""
    with (
        (tmp_path / "out").open("wb") as stdout,
        (tmp_path / "err").open("wb") as stderr,
    ):
        owned = supervisor.launch(
            [
                sys.executable,
                "-c",
                code,
                *(str(p) for p in (ready, received, release, exited)),
            ],
            tmp_path,
            stdout,
            stderr,
        )
        try:
            wait_file(ready)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(owned.terminate, 5)
                try:
                    wait_file(received)
                    assert not pending.done() and not exited.exists()
                finally:
                    release.touch()
                pending.result(timeout=5)
            assert exited.exists() and not owned.owns(owned.pid)
        finally:
            owned.close()


@pytest.mark.skipif(
    os.name == "nt", reason="SIGCHLD auto-reaping is a POSIX process-identity hazard"
)
def test_automatic_child_reaping_is_rejected_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ignored(_signum: int) -> int:
        return signal.SIG_IGN

    monkeypatch.setattr(supervisor.signal, "getsignal", ignored)

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("Auto-reaping must be rejected before starting a child")

    monkeypatch.setattr(supervisor.subprocess, "Popen", forbidden)
    with (
        (tmp_path / "out").open("wb") as stdout,
        (tmp_path / "err").open("wb") as stderr,
    ):
        with pytest.raises(supervisor.ProcessIdentityError, match="default SIGCHLD"):
            supervisor.PosixProcess([sys.executable], tmp_path, stdout, stderr)


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX cleanup must not signal after an interrupted child join",
)
def test_interrupted_wait_never_signals_a_released_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        (tmp_path / "out").open("wb") as stdout,
        (tmp_path / "err").open("wb") as stderr,
    ):
        owned = supervisor.PosixProcess(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tmp_path,
            stdout,
            stderr,
        )
        original = owned.process.wait

        def reap_then_interrupt(timeout: float | None = None) -> int:
            original(timeout=timeout)
            raise KeyboardInterrupt

        monkeypatch.setattr(owned.process, "wait", reap_then_interrupt)
        with pytest.raises(KeyboardInterrupt):
            owned.terminate(5)

        def forbidden(*_: object) -> None:
            raise AssertionError("Must not signal a process group after reaping")

        monkeypatch.setattr(supervisor.os, "killpg", forbidden)
        monkeypatch.setattr(owned.process, "wait", original)
        owned.close()
        assert not owned.owns(owned.pid)
