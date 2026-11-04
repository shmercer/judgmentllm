"""Own and interrupt only a newly launched installed-console process instance.

This test tool never treats a PID read from SQLite as signaling authority.
POSIX retains an unreaped direct child; Windows retains a private job containing
both the console launcher and its interpreter. Successful return confirms exit.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO, Protocol


class ProcessIdentityError(RuntimeError):
    """The journal owner cannot be bound to the process instance we launched."""


class OwnedProcess(Protocol):
    @property
    def pid(self) -> int: ...

    def owns(self, pid: int) -> bool: ...

    def terminate(self, timeout: float = 10) -> None: ...

    def close(self) -> None: ...


class PosixProcess:
    """An unreaped child anchors its PID and private session until exit is joined."""

    def __init__(
        self, command: list[str], cwd: Path, stdout: BinaryIO, stderr: BinaryIO
    ) -> None:
        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            raise ProcessIdentityError(
                "POSIX supervision requires default SIGCHLD; automatic child reaping is unsafe"
            )
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        self._reaped = False
        self._can_signal = True

    @property
    def pid(self) -> int:
        return self.process.pid

    def owns(self, pid: int) -> bool:
        # Never poll/wait before this check: even an exited child keeps its PID
        # reserved until reaped. Unix installed shebang consoles execute in that
        # direct child, unlike Windows' additional interpreter process.
        return (
            self._can_signal
            and not self._reaped
            and self.process.returncode is None
            and pid == self.pid
        )

    def _signal(self, signum: int) -> None:
        if self._can_signal and not self._reaped and self.process.returncode is None:
            try:
                os.killpg(self.pid, signum)
            except ProcessLookupError:
                pass

    def terminate(self, timeout: float = 10) -> None:
        if self._reaped:
            return
        self._signal(signal.SIGTERM)
        # wait() can reap and then propagate KeyboardInterrupt. After beginning
        # an ambiguous join, no cleanup may signal that numerical group again.
        self._can_signal = False
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # This explicit outcome guarantees wait did not reap the child.
            self._can_signal = self.process.returncode is None
            raise
        self._reaped = True

    def close(self) -> None:
        if self.process.returncode is not None:
            self._reaped = True
        if not self._reaped:
            # Cleanup still uses our unreaped child's anchored session. Never
            # signal a numerical PID after wait() could have released it.
            self._signal(signal.SIGKILL)
            self._can_signal = False
            self.process.wait(timeout=10)
            self._reaped = True


def launch(
    command: list[str], cwd: Path, stdout: BinaryIO, stderr: BinaryIO
) -> OwnedProcess:
    if os.name == "nt":
        if __package__:
            from .cli_process_windows import WindowsProcess
        else:
            from cli_process_windows import WindowsProcess

        return WindowsProcess(command, cwd, stdout, stderr)
    return PosixProcess(command, cwd, stdout, stderr)


def _prepared_run(stderr: Path) -> str | None:
    identifiers: set[str] = set()
    for line in stderr.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # A writer can be between a JSON write and its newline.
        if record.get("event") == "progress" and record.get("category") == "prepared":
            identifier = record.get("run_id")
            if isinstance(identifier, str):
                identifiers.add(identifier)
    if len(identifiers) > 1:
        raise ProcessIdentityError(
            "Console progress contains conflicting run identities"
        )
    return next(iter(identifiers), None)


def interrupt_run(
    directory: Path,
    executable: Path,
    read_evidence: Callable[[Path], dict[str, Any]],
    *,
    boundary_timeout: float = 30,
    exit_timeout: float = 10,
) -> dict[str, Any]:
    """Launch an installed console, stop at a durable boundary, and confirm death.

    The fixture store and independent ledger must both be fresh. Progress from
    our launched console binds the run, while the OS ownership object binds the
    process instance. Caller-controlled journal PIDs cannot select kill targets.
    """
    if (directory / "store" / "registry.sqlite3").exists() or (
        directory / "ledger.sqlite3"
    ).exists():
        raise ProcessIdentityError(
            "Interruption requires a fresh fixture store and ledger"
        )
    command = [
        str(executable),
        "run",
        "--config",
        str(directory / "config.json"),
        "--store",
        str(directory / "store"),
        "--mock-ledger",
        str(directory / "ledger.sqlite3"),
        "--mock-fixture",
        str(directory / "fixture.json"),
        "--json",
    ]
    stderr_path = directory / "killed-stderr.jsonl"
    with (
        (directory / "killed-stdout.json").open("wb") as stdout,
        stderr_path.open("wb") as stderr,
    ):
        owned = launch(command, directory, stdout, stderr)
        try:
            deadline = time.monotonic() + boundary_timeout
            while time.monotonic() < deadline:
                try:
                    evidence = read_evidence(directory)
                except (OSError, ValueError, sqlite3.Error):
                    evidence = {}
                if (
                    evidence.get("runs")
                    and evidence.get("results")
                    and any(
                        row["state"] == "submitting" for row in evidence["submissions"]
                    )
                ):
                    if len(evidence["runs"]) != 1:
                        raise ProcessIdentityError(
                            "Fixture has conflicting durable run identities"
                        )
                    owner = evidence["runs"][0]
                    pid = owner["owner_pid"]
                    if (
                        type(pid) is not int
                        or pid <= 0
                        or not owner["owner_token"]
                        or owner["run_id"] != _prepared_run(stderr_path)
                        or not owned.owns(pid)
                    ):
                        raise ProcessIdentityError(
                            "Journal owner is not the launched console instance"
                        )
                    # The OS object, not the journal PID, selects termination.
                    # Do not return or permit takeover until its exit is joined.
                    owned.terminate(timeout=exit_timeout)
                    return {
                        "pid": pid,
                        "run_id": owner["run_id"],
                        "result_sha256": [
                            row["record_sha256"] for row in evidence["results"]
                        ],
                        "accepted": sum(
                            row["acceptance_count"] for row in evidence["acceptances"]
                        ),
                        "exit_confirmed": True,
                    }
                time.sleep(
                    0.02
                )  # Poll a durable condition, never assume process death.
            raise TimeoutError("Fixture did not reach its durable submitting boundary")
        finally:
            owned.close()
