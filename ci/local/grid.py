"""Run committed local CI adapters and require seven fresh, exact-source results.

Preparation may download pinned prerequisites; checks never download assets.
Every adapter uses a separate project namespace and the existing VM locks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, cast

from gates import R_GATES
from source import stage

UV_ASSETS = {
    "linux": (
        "uv-x86_64-unknown-linux-gnu.tar.gz",
        "f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645",
    ),
    "windows": (
        "uv-x86_64-pc-windows-msvc.zip",
        "a2a8820d5eea32f08ef467ef3b7f66883a9b076d5737a2fc2fbb5f458d1bb50f",
    ),
    "macos": (
        "uv-x86_64-apple-darwin.tar.gz",
        "63ba17fdda55ad1a230d4b37fc6017ffa10c23688c83818bd3bfdf914c652a02",
    ),
}
JSONLITE_SHA = "e947f4ce2c3a8194ae42c4015cdc557cec1c1cea06f85cd20da037aa93dfa5e4"
STATES = {
    "linux": Path("/mnt/VM_Fast/judgmentllm-ci/linux"),
    "macos": Path("/mnt/VM_Fast/judgmentllm-ci/macos"),
    "windows": Path("/mnt/VM_Fast/pairwiseLLM-checks/judgmentllm-ci"),
}
RESULTS = {
    "linux": "runs/*/python-*/results/summary.json",
    "windows": "results/windows-*/python-*/summary.json",
    "macos": "runs/*/guest/results/python-*/summary.json",
}
SYSTEMS = {"linux": "Linux", "windows": "Windows", "macos": "Darwin"}
PYTHON_VERSIONS = {
    "linux": frozenset(("3.11.18", "3.12.16", "3.15.2")),
    "windows": frozenset(("3.11.18", "3.12.16")),
    "macos": frozenset(("3.11.18", "3.12.16")),
}


def required_gates(platform: str, version: str, mode: str) -> set[str]:
    """Require complete native gate evidence, rather than only a completion flag."""
    names = {
        "uv-version",
        "locked-sync",
        "dependency-versions",
        "bundled-type-checker",
        "r-jsonlite-versions",
        "build",
        "distribution-metadata",
        "clean-wheel-installation",
        "clean-source-installation",
    }
    if mode == "prepare":
        return names | {"warm-pyright-node"}
    names.update(("ruff-lint", "ruff-format", "strict-pyright"))
    names.add(
        "pytest-coverage"
        if platform == "linux" and version == "3.11.18"
        else "pytest-no-coverage"
    )
    for name, _, _ in R_GATES:
        phases = ("generate",) if name == "exports" else ("persist", "export")
        names.update(f"r-{name}-{phase}" for phase in phases)
        names.add(f"r-{name}-assertions")
    names.add("r-cli-lifecycle-assertions")
    if platform == "windows":
        names.update(
            (
                "windows-durability-cancellation",
                "windows-signatures",
                "windows-privacy-locks",
            )
        )
    return names


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def asset(directory: Path, name: str, expected: str, url: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if target.exists():
        if not target.is_file() or digest(target) != expected:
            raise ValueError(f"Cached asset differs; refusing overwrite: {target}")
        return target
    temporary = directory / f".{name}-{os.getpid()}.download"
    try:
        with urllib.request.urlopen(url, timeout=90) as response:
            with temporary.open("xb") as output:
                while data := response.read(1024 * 1024):
                    output.write(data)
        if digest(temporary) != expected:
            raise ValueError(f"Downloaded asset checksum mismatch: {name}")
        # Exclusive creation prevents replacing a concurrently prepared asset.
        with temporary.open("rb") as incoming, target.open("xb") as output:
            while data := incoming.read(1024 * 1024):
                output.write(data)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def save(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def admission_errors(
    summaries: Sequence[object],
    *,
    platform: str,
    ref: str,
    mode: str,
    archive_sha256: str,
    lockfile_sha256: str,
) -> list[str]:
    """Admit only the platform's exact Python/OS/source results from this run."""
    errors: list[str] = []
    expected_versions = set(PYTHON_VERSIONS[platform])
    if len(summaries) != len(expected_versions):
        errors.append(
            f"Expected {len(expected_versions)} fresh {platform} Python summaries; "
            f"found {len(summaries)}"
        )
    versions: set[str] = set()
    for index, raw in enumerate(summaries):
        if not isinstance(raw, dict):
            errors.append(f"Summary {index + 1} is not a JSON object")
            continue
        item = cast(dict[str, Any], raw)
        python = item.get("python")
        operating_system = item.get("os")
        version = (
            cast(dict[str, Any], python).get("version")
            if isinstance(python, dict)
            else None
        )
        if isinstance(version, str):
            if version in versions:
                errors.append(f"Summary {index + 1} duplicates Python {version}")
            versions.add(version)
        if (
            item.get("interpreter_verified") is not True
            or not isinstance(python, dict)
            or cast(dict[str, Any], python).get("implementation") != "CPython"
        ):
            errors.append(f"Summary {index + 1} lacks exact CPython verification")
        try:
            started_at = datetime.fromisoformat(str(item.get("started_at")))
            finished_at = datetime.fromisoformat(str(item.get("finished_at")))
            valid_timestamps = (
                started_at.utcoffset() is not None
                and finished_at.utcoffset() is not None
                and started_at <= finished_at
            )
        except (TypeError, ValueError):
            valid_timestamps = False
        if not valid_timestamps:
            errors.append(f"Summary {index + 1} has invalid or unordered timestamps")
        for field, expected in (
            ("commit", ref),
            ("mode", mode),
            ("archive_sha256", archive_sha256),
            ("lockfile_sha256", lockfile_sha256),
            ("status", "passed"),
        ):
            if item.get(field) != expected:
                errors.append(f"Summary {index + 1} has incompatible {field}")
        system = (
            cast(dict[str, Any], operating_system).get("system")
            if isinstance(operating_system, dict)
            else None
        )
        if system != SYSTEMS[platform]:
            errors.append(f"Summary {index + 1} has incompatible operating system")
        if (
            item.get("source_verified_before") is not True
            or item.get("source_verified_after") is not True
        ):
            errors.append(f"Summary {index + 1} lacks complete source verification")
        if item.get("all_scheduled_gates_finished") is not True:
            errors.append(f"Summary {index + 1} did not finish its scheduled gates")
        raw_steps = item.get("steps")
        if not isinstance(raw_steps, list):
            errors.append(f"Summary {index + 1} lacks required gate evidence")
            continue
        steps = cast(list[object], raw_steps)
        passed_names: set[str] = set()
        for raw_step in steps:
            if not isinstance(raw_step, dict):
                errors.append(f"Summary {index + 1} contains an invalid gate")
                continue
            step = cast(dict[str, Any], raw_step)
            name = step.get("name")
            if (
                not isinstance(name, str)
                or name in passed_names
                or step.get("status") != "passed"
                or step.get("exit_code") != 0
            ):
                errors.append(
                    f"Summary {index + 1} contains a failed or duplicate gate"
                )
            else:
                passed_names.add(name)
            if name in {
                "pytest-coverage",
                "pytest-no-coverage",
                "windows-durability-cancellation",
                "windows-privacy-locks",
            }:
                enabled = name == "pytest-coverage"
                percentage = step.get("coverage_percent")
                valid_coverage = (
                    isinstance(percentage, (int, float))
                    and not isinstance(percentage, bool)
                    and math.isfinite(percentage)
                    and 90 <= percentage <= 100
                    if enabled
                    else percentage is None
                )
                if step.get("coverage_enabled") is not enabled or not valid_coverage:
                    errors.append(
                        f"Summary {index + 1} has incompatible coverage evidence"
                    )
        expected_names = required_gates(platform, str(version), mode)
        if passed_names != expected_names:
            errors.append(
                f"Summary {index + 1} has incomplete or unexpected gates: "
                f"missing={sorted(expected_names - passed_names)}, "
                f"extra={sorted(passed_names - expected_names)}"
            )
    if versions != expected_versions:
        errors.append(
            f"Fresh summaries must cover exact {platform} Python versions "
            f"{sorted(expected_versions)}; found {sorted(versions)}"
        )
    return errors


def collect_fresh(
    state: Path, pattern: str, before: set[Path]
) -> tuple[list[Path], list[object]]:
    # Freshness uses new per-run paths and exact source identity. Guest clocks
    # need not match the host clock, so timestamps are not a cross-OS time gate.
    paths = sorted(set(state.glob(pattern)) - before)
    return paths, [json.loads(path.read_text(encoding="utf-8-sig")) for path in paths]


def run_parallel(
    lanes: Sequence[dict[str, Any]],
    output: Path,
    on_finish: Callable[[dict[str, Any]], None],
    on_started: Callable[[], None] | None = None,
) -> None:
    """Run isolated adapters together; reap owned adapters on interruption."""
    active: dict[int, tuple[dict[str, Any], subprocess.Popen[bytes], float]] = {}
    interrupted: int | None = None

    def receive(signum: int, frame: FrameType | None) -> None:
        nonlocal interrupted
        # Do not raise between spawning a process and registering its ownership.
        interrupted = signum

    previous = {
        signum: signal.signal(signum, receive)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }

    def stop() -> None:
        for lane, process, _ in active.values():
            if process.poll() is None:
                lane["interrupted"] = True
                try:
                    # The adapter owns guest cleanup; its process group may
                    # include Quickemu and must never receive a group signal.
                    process.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass

    try:
        with ExitStack() as logs:
            try:
                for lane in lanes:
                    if interrupted is not None:
                        lane["status"] = "cancelled"
                        continue
                    lane["status"] = "running"
                    lane["started_at"] = datetime.now(UTC).isoformat()
                    started = time.monotonic()
                    log = logs.enter_context(
                        (output / f"{lane['platform']}.log").open("wb")
                    )
                    try:
                        process = subprocess.Popen(
                            lane["command"],
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            start_new_session=True,
                        )
                    except OSError as error:
                        lane.update(
                            exit_code=None,
                            error={"type": type(error).__name__, "message": str(error)},
                            duration_seconds=time.monotonic() - started,
                        )
                        on_finish(lane)
                    else:
                        active[process.pid] = (lane, process, started)
                if on_started is not None:
                    on_started()
                stopping = False
                while active:
                    if interrupted is not None and not stopping:
                        stop()
                        stopping = True
                    for pid, (lane, process, started) in list(active.items()):
                        code = process.poll()
                        if code is None:
                            continue
                        del active[pid]
                        lane.update(
                            exit_code=code,
                            duration_seconds=time.monotonic() - started,
                            finished_at=datetime.now(UTC).isoformat(),
                        )
                        on_finish(lane)
                    if active:
                        time.sleep(0.1)
            finally:
                # No forced-kill fallback: adapters retain their locks while
                # collecting evidence and gracefully stopping guests they own.
                stop()
                for lane, process, started in active.values():
                    lane.update(
                        exit_code=process.wait(),
                        duration_seconds=time.monotonic() - started,
                        status="cancelled",
                        finished_at=datetime.now(UTC).isoformat(),
                    )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if interrupted is not None:
        raise InterruptedError(f"Received signal {interrupted}; owned adapters reaped")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--ref", required=True)
    parser.add_argument(
        "--mode", choices=("doctor", "prepare", "check"), default="check"
    )
    parser.add_argument("--platform", choices=(*STATES, "all"), default="all")
    parser.add_argument(
        "--asset-root", type=Path, default=Path("/mnt/VM_Fast/judgmentllm-ci/assets")
    )
    parser.add_argument(
        "--result-root", type=Path, default=Path("/mnt/VM_Fast/judgmentllm-ci/grid")
    )
    args = parser.parse_args()
    if re.fullmatch(r"[0-9a-f]{40}", args.ref) is None:
        parser.error("--ref must be the complete proposed commit SHA")
    repo = args.repo.resolve(strict=True)
    platforms = list(STATES) if args.platform == "all" else [args.platform]
    harness = repo / "ci/local"
    # Do not qualify committed source using an accidentally edited host harness.
    for path in sorted(harness.iterdir()):
        if path.suffix not in {".py", ".sh", ".ps1"} and path.name != "Dockerfile":
            continue
        relative = path.relative_to(repo).as_posix()
        committed = subprocess.check_output(
            ["git", "-C", str(repo), "show", f"{args.ref}:{relative}"]
        )
        if path.read_bytes() != committed:
            raise ValueError(f"Host harness differs from proposed commit: {relative}")
    args.result_root = args.result_root.resolve()
    if not args.result_root.is_relative_to(Path("/mnt/VM_Fast/judgmentllm-ci")):
        parser.error("--result-root must be beneath the JudgmentLLM state namespace")
    subprocess.run(["mountpoint", "-q", "/mnt/VM_Fast"], check=True)
    run_id = (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + f"-{args.ref[:12]}-{args.mode}-{os.getpid()}"
    )
    output = args.result_root / run_id
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "schema_version": 1,
        "commit": args.ref,
        "mode": args.mode,
        "platforms": platforms,
        "scheduling": "parallel platforms and Python versions",
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "lanes": [],
    }
    save(output / "summary.json", report)
    print(f"Grid evidence: {output}", flush=True)
    started = time.monotonic()
    try:
        expected = stage(repo, args.ref, output / "source")
        archive_sha256 = str(expected["archive_sha256"])
        lockfile_sha256 = str(expected["lockfile_sha256"])
        report["expected_source"] = {
            "commit": args.ref,
            "archive_sha256": archive_sha256,
            "lockfile_sha256": lockfile_sha256,
            "manifest": str(output / "source/manifest.json"),
        }
        save(output / "summary.json", report)
        before_by_platform = {
            platform: set(STATES[platform].glob(RESULTS[platform]))
            for platform in platforms
        }
        for platform in platforms:
            command = [
                "bash",
                str(harness / f"{platform}.sh"),
                "--repo",
                str(repo),
                "--ref",
                args.ref,
                "--mode",
                args.mode,
            ]
            if args.mode == "prepare":
                name, sha = UV_ASSETS[platform]
                uv = asset(
                    args.asset_root,
                    name,
                    sha,
                    f"https://github.com/astral-sh/uv/releases/download/0.13.6/{name}",
                )
                command.extend(("--uv-archive", str(uv), "--uv-sha256", sha))
                if platform == "linux":
                    archive = asset(
                        args.asset_root,
                        "jsonlite-2.0.0.tar.gz",
                        JSONLITE_SHA,
                        "https://github.com/cran/jsonlite/archive/2559608891cc42b9f857a5a0aff231738a670576.tar.gz",
                    )
                    command.extend(
                        (
                            "--jsonlite-archive",
                            str(archive),
                            "--jsonlite-sha256",
                            JSONLITE_SHA,
                        )
                    )
            lane: dict[str, Any] = {
                "platform": platform,
                "command": command,
                "status": "pending",
            }
            report["lanes"].append(lane)
        save(output / "summary.json", report)

        def finish(lane: dict[str, Any]) -> None:
            platform = str(lane["platform"])
            try:
                fresh, summaries = collect_fresh(
                    STATES[platform], RESULTS[platform], before_by_platform[platform]
                )
            except (OSError, ValueError) as error:
                fresh, summaries = [], []
                lane["error"] = {"type": type(error).__name__, "message": str(error)}
            lane.update(evidence=[str(path) for path in fresh], summaries=summaries)
            valid = lane["exit_code"] == 0 and "error" not in lane
            if args.mode != "doctor":
                errors = admission_errors(
                    summaries,
                    platform=platform,
                    ref=args.ref,
                    mode=args.mode,
                    archive_sha256=archive_sha256,
                    lockfile_sha256=lockfile_sha256,
                )
                lane["admission_errors"] = errors
                valid = valid and not errors
            if lane.get("interrupted"):
                lane["status"] = "cancelled"
            else:
                lane["status"] = "passed" if valid else "failed"
            print(f"{platform} {args.mode}: {lane['status']}", flush=True)
            save(output / "summary.json", report)

        print(f"Starting {len(platforms)} platform adapters in parallel", flush=True)
        run_parallel(
            report["lanes"],
            output,
            finish,
            on_started=lambda: save(output / "summary.json", report),
        )
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        report["status"] = (
            "passed"
            if "error" not in report
            and len(report["lanes"]) == len(platforms)
            and all(lane["status"] == "passed" for lane in report["lanes"])
            else "failed"
        )
        report["qualification"] = (
            args.mode == "check"
            and args.platform == "all"
            and report["status"] == "passed"
        )
        report["duration_seconds"] = time.monotonic() - started
        report["finished_at"] = datetime.now(UTC).isoformat()
        save(output / "summary.json", report)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
