"""Backlog-aware accepted-main qualification; installation never enables a timer."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from allocation import QualificationLease, alive, foreground_pending
from resource_budget import ResourceLease

PUBLIC_REPOSITORY = "https://github.com/shmercer/judgmentllm.git"
STATES = {"pending", "running", "passed", "failed", "superseded", "unavailable"}


def atomic(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    with temporary.open("x") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class Queue:
    """One locked writer; every source keeps its own honest outcome/attempts."""

    def __init__(
        self, root: Path, *, strict: bool = False, quiet_seconds: float = 120
    ) -> None:
        if not 0 <= quiet_seconds <= 3600:
            raise ValueError(
                "Quiet period must be finite and between 0 and 3600 seconds"
            )
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "queue.json"
        self.strict = strict
        self.quiet_seconds = quiet_seconds
        self.data: dict[str, Any] = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {
                "schema_version": 1,
                "tip": None,
                "tip_changed_at": 0,
                "commits": {},
                "latest_failure": None,
            }
        )
        if self.data.get("schema_version") != 1 or any(
            item.get("state") not in STATES for item in self.data["commits"].values()
        ):
            raise ValueError("Invalid main qualification queue")

    def save(self) -> None:
        atomic(self.path, self.data)

    def observe(self, commits: list[str], now: float) -> None:
        if not commits or any(
            re.fullmatch(r"[0-9a-f]{40}", sha) is None for sha in commits
        ):
            raise ValueError("Main observations require complete commit identities")
        tip = commits[-1]
        for sha in commits:
            self.data["commits"].setdefault(
                sha, {"state": "pending", "observed_at": now, "attempts": []}
            )
        if self.data["tip"] != tip:
            self.data.update(tip=tip, tip_changed_at=now)
        if not self.strict:
            for sha, item in self.data["commits"].items():
                if sha != tip and item["state"] in {"pending", "unavailable"}:
                    item.update(state="superseded", superseded_by=tip)
        self.save()

    def recover(self) -> None:
        for sha, item in self.data["commits"].items():
            if item["state"] == "running" and not alive(item.get("owner", {})):
                item["attempts"][-1].update(
                    status="interrupted",
                    reason="worker disappeared",
                    finished_at=time.time(),
                )
                item["state"] = (
                    "pending"
                    if self.strict or sha == self.data["tip"]
                    else "superseded"
                )
        self.save()

    def ready(self, now: float) -> str | None:
        if now - self.data["tip_changed_at"] < self.quiet_seconds:
            return None
        pending = [
            sha
            for sha, item in self.data["commits"].items()
            if item["state"] in {"pending", "unavailable"}
        ]
        if self.strict:
            return pending[0] if pending else None
        return self.data["tip"] if self.data["tip"] in pending else None

    def start(self, sha: str, owner: dict[str, Any]) -> str:
        item = self.data["commits"][sha]
        if item["state"] not in {"pending", "unavailable"}:
            raise ValueError("Commit is not pending qualification")
        attempt = uuid.uuid4().hex
        item.update(state="running", owner=owner)
        item["attempts"].append(
            {"attempt": attempt, "status": "running", "started_at": time.time()}
        )
        self.save()
        return attempt

    def finish(self, sha: str, status: str, **evidence: Any) -> None:
        if status not in {"passed", "failed", "interrupted"}:
            raise ValueError("Invalid qualification terminal outcome")
        item = self.data["commits"][sha]
        if item["state"] != "running":
            raise ValueError("No running attempt can establish this result")
        item["attempts"][-1].update(status=status, finished_at=time.time(), **evidence)
        item["state"] = (
            status
            if status != "interrupted"
            else "pending"
            if self.strict or sha == self.data["tip"]
            else "superseded"
        )
        if status == "failed":
            self.data["latest_failure"] = sha
        elif status == "passed" and sha == self.data["tip"]:
            self.data["latest_failure"] = None
        self.save()


def fetch(root: Path, previous: str | None) -> list[str]:
    mirror = root / "mirror.git"
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    if not mirror.exists():
        subprocess.run(
            ["git", "init", "--bare", "-q", str(mirror)], env=env, check=True
        )
    subprocess.run(
        [
            "git",
            "-C",
            str(mirror),
            "fetch",
            "--no-tags",
            PUBLIC_REPOSITORY,
            "refs/heads/main:refs/remotes/origin/main",
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    tip = subprocess.check_output(
        ["git", "-C", str(mirror), "rev-parse", "refs/remotes/origin/main"],
        env=env,
        text=True,
    ).strip()
    if previous is None or previous == tip:
        return [tip]
    if subprocess.run(
        ["git", "-C", str(mirror), "merge-base", "--is-ancestor", previous, tip],
        env=env,
        check=False,
    ).returncode:
        raise ValueError("Main history is not a verified fast-forward")
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(mirror),
            "rev-list",
            "--first-parent",
            "--reverse",
            f"{previous}..{tip}",
        ],
        env=env,
        text=True,
    ).splitlines()


def verify_local(output: Path, sha: str) -> None:
    plan = json.loads((output / "qualification/plan.json").read_text())
    receipt = json.loads((output / "qualification/primary/receipt.json").read_text())
    if (
        plan.get("purpose") != "main-local"
        or plan.get("source_sha") != sha
        or plan.get("coverage_mode") != "historical"
        or plan.get("resources") is not True
        or len(plan.get("r_gates", [])) != 7
        or receipt.get("source_sha") != sha
        or receipt.get("status") != "passed"
        or receipt.get("source_verified") is not True
        or receipt.get("execution_scope") != "local"
        or receipt.get("purpose") != "main-local"
        or receipt.get("plan_sha256") != plan.get("plan_sha256")
    ):
        raise ValueError(
            "Main qualification lacks exact-source complete local acceptance"
        )


def once(root: Path, *, strict: bool, quiet_seconds: float) -> int:
    import psutil
    from process import run as run_owned

    with ResourceLease(root / "worker-lock", 1, timeout=0):
        queue = Queue(root, strict=strict, quiet_seconds=quiet_seconds)
        queue.recover()
        queue.observe(fetch(root, queue.data["tip"]), time.time())
        sha = queue.ready(time.time())
        if sha is None:
            return 1 if queue.data["latest_failure"] else 0
        try:
            with QualificationLease(background=True) as allocation:
                attempt = queue.start(
                    sha, {"pid": os.getpid(), "birth": psutil.Process().create_time()}
                )
                output = root / "runs" / sha / attempt
                output.mkdir(parents=True, exist_ok=False)
                cancel, stop_monitor, foreground_seen = (
                    threading.Event(),
                    threading.Event(),
                    threading.Event(),
                )

                def prioritize_foreground() -> None:
                    while not stop_monitor.wait(0.25):
                        if foreground_pending(allocation.root):
                            foreground_seen.set()
                            if not stop_monitor.wait(5):
                                cancel.set()
                            return

                monitor = threading.Thread(target=prioritize_foreground, daemon=True)
                monitor.start()
                code = 1
                try:
                    source = output / "source"
                    subprocess.run(
                        [
                            "git",
                            "clone",
                            "--shared",
                            "--quiet",
                            str(root / "mirror.git"),
                            str(source),
                        ],
                        check=True,
                    )
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(source),
                            "checkout",
                            "--detach",
                            "--quiet",
                            sha,
                        ],
                        check=True,
                    )
                    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
                    from acceptance import clean_environment

                    environment = {**clean_environment(), **allocation.environment()}
                    environment["JUDGMENTLLM_BACKGROUND_RUN"] = "1"
                    environment["UV_OFFLINE"] = "0"
                    commands = [
                        (
                            "prepare",
                            [
                                "uv",
                                "sync",
                                "--project",
                                str(source),
                                "--locked",
                                "--all-extras",
                                "--group",
                                "dev",
                                "--python",
                                "3.11",
                            ],
                        ),
                        (
                            "qualification",
                            [
                                str(source / ".venv/bin/python"),
                                "-B",
                                "ci/check.py",
                                "--profile",
                                "main",
                                "--ref",
                                sha,
                                "--output",
                                str(output / "qualification"),
                                "--workers",
                                "12",
                            ],
                        ),
                    ]
                    for phase, command in commands:
                        if cancel.is_set():
                            break
                        if phase == "qualification":
                            environment["UV_OFFLINE"] = "1"
                        with (output / (phase + ".log")).open("w") as stream:
                            code, processes = run_owned(
                                command,
                                cwd=source,
                                env=environment,
                                stream=stream,
                                evidence=output / (phase + "-owner"),
                                cancel=cancel,
                            )
                        if code or processes.get("cleanup_complete") is not True:
                            code = code or 1
                            break
                    if code == 0 and not cancel.is_set():
                        verify_local(output, sha)
                        plan = json.loads(
                            (output / "qualification/plan.json").read_text()
                        )
                        with (output / "admission.log").open("w") as stream:
                            code, processes = run_owned(
                                [
                                    str(source / ".venv/bin/python"),
                                    "-B",
                                    "ci/acceptance.py",
                                    "verify-local",
                                    "--source",
                                    str(source),
                                    "--plan",
                                    str(output / "qualification/plan.json"),
                                    "--base",
                                    plan["base_sha"],
                                    "--head",
                                    sha,
                                    "--purpose",
                                    "main-local",
                                    "--receipts",
                                    str(output / "qualification/primary"),
                                    "--output",
                                    str(output / "admission.json"),
                                    "--run-id",
                                    "local",
                                ],
                                cwd=source,
                                env=environment,
                                stream=stream,
                                evidence=output / "admission-owner",
                                cancel=cancel,
                                identity={
                                    "source_sha": sha,
                                    "attempt": attempt,
                                    "phase": "admission",
                                },
                            )
                        if processes.get("cleanup_complete") is not True:
                            code = code or 1
                except Exception as error:
                    (output / "worker-error.json").write_text(
                        json.dumps(
                            {"type": type(error).__name__, "message": str(error)}
                        )
                        + "\n"
                    )
                    code = 1
                finally:
                    stop_monitor.set()
                    monitor.join(timeout=2)
                if foreground_pending(allocation.root):
                    foreground_seen.set()
                queue.finish(
                    sha,
                    "interrupted"
                    if foreground_seen.is_set()
                    else "passed"
                    if code == 0
                    else "failed",
                    output=str(output),
                    exit_code=code,
                )
                return 125 if foreground_seen.is_set() else int(code != 0)
        except (BlockingIOError, TimeoutError) as error:
            if queue.data["commits"][sha]["state"] == "running":
                queue.finish(sha, "failed", reason=str(error))
                return 1
            queue.data["commits"][sha].update(state="unavailable", reason=str(error))
            queue.save()
            return 2


def install(unit_dir: Path, root: Path) -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    for kind in ("service", "timer"):
        text = script.with_name(f"judgmentllm-main.{kind}.in").read_text()

        def escaped(value: str) -> str:
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError("Invalid control character in unit configuration")
            return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")

        text = (
            text.replace("@PYTHON@", escaped(sys.executable))
            .replace("@SCRIPT@", escaped(str(script)))
            .replace("@STATE@", escaped(str(root.resolve())))
        )
        (unit_dir / f"judgmentllm-main.{kind}").write_text(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=(
            "once",
            "status",
            "retry",
            "install",
            "recover",
            "activate",
            "rollback",
        ),
    )
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path(
            os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
        )
        / "judgmentllm-ci/main",
    )
    parser.add_argument(
        "--unit-dir", type=Path, default=Path.home() / ".config/systemd/user"
    )
    parser.add_argument("--strict-per-commit", action="store_true")
    parser.add_argument("--quiet-seconds", type=float, default=120)
    args = parser.parse_args()
    if args.operation == "install":
        install(args.unit_dir, args.state_root)
        print(
            "Installed definitions only; production activation requires independent review and merge."
        )
        return 0
    if args.operation == "activate":
        root = Path(__file__).resolve().parents[2]
        subprocess.run(["git", "fetch", "origin", "main"], cwd=root, check=True)
        for relative in subprocess.check_output(
            ["git", "ls-files", "ci", ".github/workflows/ci.yml"], cwd=root, text=True
        ).splitlines():
            path = root / relative
            if (
                subprocess.check_output(
                    ["git", "show", f"origin/main:{relative}"], cwd=root
                )
                != path.read_bytes()
            ):
                raise ValueError(
                    "Production activation requires the reviewed merged CI source"
                )
        if args.unit_dir.resolve() != (Path.home() / ".config/systemd/user").resolve():
            raise ValueError("Production activation requires the user unit directory")
        install(args.unit_dir, args.state_root)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "judgmentllm-main.timer"],
            check=True,
        )
        return 0
    if args.operation == "rollback":
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "judgmentllm-main.timer"],
            check=True,
        )
        return 0
    args.state_root.mkdir(parents=True, exist_ok=True)
    if args.operation == "once":
        return once(
            args.state_root,
            strict=args.strict_per_commit,
            quiet_seconds=args.quiet_seconds,
        )
    with ResourceLease(args.state_root / "worker-lock", 1, timeout=0):
        queue = Queue(
            args.state_root,
            strict=args.strict_per_commit,
            quiet_seconds=args.quiet_seconds,
        )
        if args.operation in {"recover", "status"}:
            queue.recover()
        elif args.operation == "retry":
            tip = queue.data["tip"]
            if tip is None or queue.data["commits"][tip]["state"] != "failed":
                raise ValueError("Only the latest failed tip can be explicitly retried")
            queue.data["commits"][tip]["state"] = "pending"
            queue.save()
        print(json.dumps(queue.data, indent=2, sort_keys=True))
        return int(queue.data["latest_failure"] is not None)


if __name__ == "__main__":
    raise SystemExit(main())
