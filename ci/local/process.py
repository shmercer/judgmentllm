"""Own one Linux test process tree, including after launcher death."""

from __future__ import annotations

import argparse
import ctypes
import functools
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import FrameType
from typing import Any, TextIO

import psutil


@functools.lru_cache(maxsize=1)
def systemd_available() -> bool:
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") == "true":
        return False
    try:
        return (
            subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", "default.target"],
                env={"PATH": os.defpath, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"},
                capture_output=True,
                timeout=5,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _children() -> list[psutil.Process]:
    return psutil.Process().children(recursive=True)


def _living() -> list[psutil.Process]:
    result: list[psutil.Process] = []
    for process in _children():
        try:
            if process.status() != psutil.STATUS_ZOMBIE:
                result.append(process)
        except psutil.NoSuchProcess:
            pass
    return result


def _stop(processes: list[psutil.Process], *, force: bool = False) -> None:
    for process in processes:
        try:
            # psutil verifies PID/birth identity before sending a signal.
            process.kill() if force else process.terminate()
        except psutil.NoSuchProcess:
            pass


def worker(request: Path) -> int:
    from telemetry import ProcessSampler

    settings = json.loads(request.read_text())
    request_sha256 = hashlib.sha256(request.read_bytes()).hexdigest()
    stopping = threading.Event()

    def stop(signum: int, frame: FrameType | None) -> None:
        stopping.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, stop)
    library = ctypes.CDLL(None, use_errno=True)
    if (
        library.prctl(36, 1, 0, 0, 0) != 0
        or library.prctl(1, signal.SIGTERM, 0, 0, 0) != 0
    ):
        raise OSError("Cannot establish owned child subreaper/death signal")
    parent = psutil.Process(settings["owner_pid"])
    if (
        parent.create_time() != settings["owner_birth"]
        or not settings.get("systemd")
        and os.getppid() != parent.pid
    ):
        raise ValueError("Launcher identity changed before stage admission")
    started = time.monotonic()
    process = subprocess.Popen(settings["command"], cwd=settings["cwd"])
    try:
        child_birth = psutil.Process(process.pid).create_time()
    except psutil.NoSuchProcess:
        child_birth = None
    request.with_name("started.json").write_text(
        json.dumps(
            {
                "worker_pid": os.getpid(),
                "worker_birth": psutil.Process().create_time(),
                "child_pid": process.pid,
                "child_birth": child_birth,
            }
        )
        + "\n"
    )
    cancelled = False
    with ProcessSampler(root_pid=process.pid) as sampler:
        while process.poll() is None:
            try:
                alive = (
                    parent.is_running()
                    and parent.create_time() == settings["owner_birth"]
                    and parent.status() != psutil.STATUS_ZOMBIE
                )
            except psutil.NoSuchProcess:
                alive = False
            if stopping.wait(0.1) or not alive:
                cancelled = True
                break
        if cancelled:
            _stop(_children())
            _, living = psutil.wait_procs(_children(), timeout=5)
            _stop(living, force=True)
        code = process.wait()
        # A successful parent with living descendants is incomplete execution.
        remaining = _living()
        leaked = bool(remaining)
        _stop(remaining)
        _, living = psutil.wait_procs(remaining, timeout=5)
        _stop(living, force=True)
        psutil.wait_procs(living, timeout=5)
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        clean = not _children()
    result = {
        "schema_version": 1,
        "identity": settings.get("identity"),
        "request_sha256": request_sha256,
        "owner_pid": settings["owner_pid"],
        "owner_birth": settings["owner_birth"],
        "containment": "systemd-control-group"
        if settings.get("systemd")
        else "linux-subreaper",
        "command_exit_code": code,
        "cancelled": cancelled,
        "leaked_descendants": leaked,
        "cleanup_complete": clean,
        "seconds": time.monotonic() - started,
        "telemetry": sampler.report(),
    }
    Path(settings["result"]).write_text(json.dumps(result, indent=2) + "\n")
    return (
        code
        if code and not cancelled
        else 125
        if cancelled or leaked or not clean
        else 0
    )


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stream: TextIO,
    evidence: Path,
    cancel: threading.Event | None = None,
    identity: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    if sys.platform != "linux":
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=False,
        )
        return result.returncode, {
            "method": "existing native adapter ownership",
            "cleanup_complete": result.returncode == 0,
        }
    evidence.mkdir(parents=True, exist_ok=False)
    request = evidence / "request.json"
    result_path = evidence / "result.json"
    unit = "judgmentllm-ci-" + uuid.uuid4().hex if systemd_available() else None
    settings = {
        "identity": identity,
        "unit": unit,
        "command": command,
        "cwd": str(cwd),
        "owner_pid": os.getpid(),
        "owner_birth": psutil.Process().create_time(),
        "result": str(result_path),
        "systemd": unit is not None,
    }
    request.write_text(json.dumps(settings) + "\n")
    request_sha256 = hashlib.sha256(request.read_bytes()).hexdigest()
    launch = [
        sys.executable,
        "-B",
        str(Path(__file__).resolve()),
        "--worker",
        str(request),
    ]
    launcher_env = env
    if unit is not None:
        # Clear the user manager's environment before entering test code.
        launch = [
            "systemd-run",
            "--user",
            "--quiet",
            "--wait",
            "--pipe",
            "--collect",
            "--service-type=exec",
            f"--unit={unit}",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=15s",
            "env",
            "-i",
            *(f"{key}={value}" for key, value in sorted(env.items())),
            *launch,
        ]
        launcher_env = {
            "PATH": env.get("PATH", os.defpath),
            "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        }
    child = subprocess.Popen(
        launch,
        cwd=cwd,
        env=launcher_env,
        stdout=stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    def terminate() -> None:
        if unit is not None:
            subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "kill",
                    "--kill-whom=main",
                    "--signal=TERM",
                    unit,
                ],
                env=launcher_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        else:
            child.send_signal(signal.SIGTERM)

    try:
        while child.poll() is None:
            if cancel is not None and cancel.wait(0.1):
                terminate()
                break
            if cancel is None:
                time.sleep(0.1)
        code = child.wait(timeout=20)
    except BaseException:
        terminate()
        child.wait(timeout=20)
        raise
    if not result_path.is_file():
        return code or 1, {
            "cleanup_complete": False,
            "error": "missing owned process receipt",
        }
    result = json.loads(result_path.read_text())
    if (
        result.get("owner_pid") != settings["owner_pid"]
        or result.get("identity") != identity
        or result.get("request_sha256") != request_sha256
        or result.get("owner_birth") != settings["owner_birth"]
        or result.get("cleanup_complete") is not True
    ):
        return code or 1, result
    return code, result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    raise SystemExit(worker(parser.parse_args().worker))
