"""Linux-host admission tests; guest portable tests live in test_gates.py."""

from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import grid
import pytest
from source import stage
from test_source import repository


def summaries(platform: str = "windows", mode: str = "check") -> list[dict[str, Any]]:
    return [
        {
            "commit": "a" * 40,
            "archive_sha256": "b" * 64,
            "lockfile_sha256": "c" * 64,
            "mode": mode,
            "status": "passed",
            "source_verified_before": True,
            "source_verified_after": True,
            "all_scheduled_gates_finished": True,
            "interpreter_verified": True,
            "os": {"system": grid.SYSTEMS[platform]},
            "python": {"version": version, "implementation": "CPython"},
            "started_at": "2026-11-05T18:30:00+00:00",
            "finished_at": "2026-11-05T18:40:00+00:00",
            "steps": [
                {
                    "name": name,
                    "status": "passed",
                    "exit_code": 0,
                    "coverage_enabled": name == "pytest-coverage",
                    "coverage_percent": 91.8 if name == "pytest-coverage" else None,
                }
                for name in sorted(grid.required_gates(platform, version, mode))
            ],
        }
        for version in sorted(grid.PYTHON_VERSIONS[platform])
    ]


def admit(
    items: list[dict[str, Any]], platform: str = "windows", mode: str = "check"
) -> list[str]:
    return grid.admission_errors(
        items,
        platform=platform,
        ref="a" * 40,
        mode=mode,
        archive_sha256="b" * 64,
        lockfile_sha256="c" * 64,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("commit", "d" * 40),
        ("archive_sha256", "d" * 64),
        ("lockfile_sha256", "d" * 64),
        ("os", {"system": "Linux"}),
        ("python", {"version": "3.11.17"}),
        ("status", "running"),
        ("mode", "prepare"),
        ("source_verified_after", False),
        ("all_scheduled_gates_finished", False),
        ("interpreter_verified", False),
        ("python", {"version": "3.11.18", "implementation": "PyPy"}),
    ],
)
def test_admission_rejects_wrong_source_os_or_partial_results(
    field: str, value: object
) -> None:
    items = summaries()
    assert not admit(items)
    items[0][field] = value
    assert admit(items)


def test_fresh_collection_ignores_old_results_and_rejects_extra_new_results(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old/summary.json"
    old.parent.mkdir()
    old.write_text(json.dumps({"status": "passed", "commit": "old"}))
    before = set(tmp_path.glob("*/summary.json"))
    for index, item in enumerate(summaries()):
        path = tmp_path / f"new-{index}/summary.json"
        path.parent.mkdir()
        path.write_text(json.dumps(item), encoding="utf-8-sig")
    paths, items = grid.collect_fresh(tmp_path, "*/summary.json", before)
    assert old not in paths and len(paths) == 2
    assert not grid.admission_errors(
        items,
        platform="windows",
        ref="a" * 40,
        mode="check",
        archive_sha256="b" * 64,
        lockfile_sha256="c" * 64,
    )
    assert admit(summaries() + summaries())
    assert admit(summaries()[:1])


@pytest.mark.parametrize("platform,count", [("linux", 3), ("windows", 2), ("macos", 2)])
@pytest.mark.parametrize("mode", ["prepare", "check"])
def test_admission_requires_platform_specific_exact_versions(
    platform: str, count: int, mode: str
) -> None:
    items = summaries(platform, mode)
    assert len(items) == count
    assert not admit(items, platform, mode)
    assert admit(items[:-1], platform, mode)
    assert admit(items + [items[0]], platform, mode)
    items[-1] = items[0]
    assert any("duplicates Python" in error for error in admit(items, platform, mode))


def test_linux_315_failure_or_missing_result_prevents_full_grid_qualification() -> None:
    items = summaries("linux")
    assert {item["python"]["version"] for item in items} == {
        "3.11.18",
        "3.12.16",
        "3.15.2",
    }
    assert not any(admit(summaries(platform), platform) for platform in grid.STATES)
    assert admit(items[:2], "linux")
    items[-1]["status"] = "failed"
    assert admit(items, "linux")


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-11-05T18:45:00+00:00",
        "2026-11-05T18:30:00",
        "invalid",
        None,
    ],
)
def test_admission_rejects_invalid_or_unordered_timestamps(
    timestamp: str | None,
) -> None:
    items = summaries("linux")
    items[-1]["started_at"] = timestamp
    assert any("timestamps" in error for error in admit(items, "linux"))


@pytest.mark.parametrize("mutation", ["missing", "failed", "duplicate", "unexpected"])
def test_admission_checks_actual_required_steps_even_with_completion_flag(
    mutation: str,
) -> None:
    items = summaries("linux")
    steps = items[-1]["steps"]
    if mutation == "missing":
        steps[:] = [s for s in steps if s["name"] != "r-cli-lifecycle-assertions"]
    elif mutation == "failed":
        steps[-1]["exit_code"] = 1
    elif mutation == "duplicate":
        steps.append(steps[0])
    else:
        steps.append({"name": "informational", "status": "passed", "exit_code": 0})
    assert items[-1]["all_scheduled_gates_finished"] is True
    assert admit(items, "linux")


def test_seven_configurations_require_49_r_gates_and_one_coverage_gate() -> None:
    configurations = [
        (platform, version)
        for platform, versions in grid.PYTHON_VERSIONS.items()
        for version in versions
    ]
    assert len(configurations) == 7
    r_gates = [
        name
        for platform, version in configurations
        for name in grid.required_gates(platform, version, "check")
        if name.startswith("r-") and name.endswith("-assertions")
    ]
    assert len(r_gates) == 49
    assert (
        sum(
            "pytest-coverage" in grid.required_gates(platform, version, "check")
            for platform, version in configurations
        )
        == 1
    )
    assert (
        sum(
            "pytest-no-coverage" in grid.required_gates(platform, version, "check")
            for platform, version in configurations
        )
        == 6
    )


@pytest.mark.parametrize(
    "platform,version,enabled,percentage",
    [
        ("linux", "3.11.18", False, None),
        ("linux", "3.11.18", True, None),
        ("linux", "3.11.18", True, 89.99),
        ("linux", "3.11.18", True, float("nan")),
        ("linux", "3.12.16", True, 91.8),
        ("linux", "3.15.2", False, 0),
        ("windows", "3.11.18", False, 91.8),
        ("windows", "3.12.16", True, None),
        ("macos", "3.11.18", True, None),
        ("macos", "3.12.16", False, 0),
    ],
)
def test_admission_enforces_measured_primary_and_null_noncoverage_results(
    platform: str, version: str, enabled: bool, percentage: float | None
) -> None:
    items = summaries(platform)
    item = next(item for item in items if item["python"]["version"] == version)
    step = next(step for step in item["steps"] if step["name"].startswith("pytest-"))
    step.update(coverage_enabled=enabled, coverage_percent=percentage)
    assert any("coverage evidence" in error for error in admit(items, platform))


def test_download_checks_bytes_before_publishing_and_reuses_valid_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def download(url: str, timeout: int) -> io.BytesIO:
        return io.BytesIO(b"downloaded fixture")

    monkeypatch.setattr(grid.urllib.request, "urlopen", download)
    with pytest.raises(ValueError, match="checksum mismatch"):
        grid.asset(tmp_path, "uv.tar.gz", "0" * 64, "https://unused.invalid")
    assert not (tmp_path / "uv.tar.gz").exists()
    expected = hashlib.sha256(b"downloaded fixture").hexdigest()
    cached = grid.asset(tmp_path, "uv.tar.gz", expected, "https://unused.invalid")

    def no_network(url: str, timeout: int) -> io.BytesIO:
        raise AssertionError("Valid cached asset must not access the network")

    monkeypatch.setattr(grid.urllib.request, "urlopen", no_network)
    assert (
        grid.asset(tmp_path, "uv.tar.gz", expected, "https://unused.invalid") == cached
    )


def test_source_archive_rejects_windows_drive_or_stream_names(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repository(repo)
    (repo / "drive:stream").write_text("synthetic unsafe portable path")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "core.protectNTFS=false", "add", "drive:stream"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=CI fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "unsafe portable filename",
        ],
        check=True,
    )
    ref = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    with pytest.raises(ValueError, match="unsafe path"):
        stage(repo, ref, tmp_path / "bundle")


def test_parallel_adapters_overlap_and_collect_peer_failures(tmp_path: Path) -> None:
    # Each adapter requires all peers to have started, proving real overlap
    # without relying on a tight runtime threshold.
    program = """
import pathlib, sys, time
root, name, code = pathlib.Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
(root / (name + '.ready')).touch()
deadline = time.monotonic() + 10
while len(list(root.glob('*.ready'))) != 3:
    if time.monotonic() > deadline:
        raise SystemExit(99)
    time.sleep(0.01)
print(name, flush=True)
raise SystemExit(code)
"""
    lanes: list[dict[str, Any]] = [
        {
            "platform": name,
            "command": [sys.executable, "-c", program, str(tmp_path), name, str(code)],
        }
        for name, code in (("linux", 0), ("macos", 7), ("windows", 0))
    ]
    finished: list[dict[str, Any]] = []
    handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    grid.run_parallel(lanes, tmp_path, finished.append)
    assert len(finished) == 3
    assert {s: signal.getsignal(s) for s in handlers} == handlers
    assert [lane["exit_code"] for lane in lanes] == [0, 7, 0]
    assert max(lane["started_at"] for lane in lanes) < min(
        lane["finished_at"] for lane in lanes
    )
    for lane in lanes:
        assert (tmp_path / f"{lane['platform']}.log").read_text().strip() == lane[
            "platform"
        ]


def test_parallel_launch_error_does_not_skip_other_adapters(tmp_path: Path) -> None:
    lanes: list[dict[str, Any]] = [
        {"platform": "missing", "command": [str(tmp_path / "absent")]},
        {"platform": "working", "command": [sys.executable, "-c", "print('ok')"]},
    ]
    finished: list[dict[str, Any]] = []
    grid.run_parallel(lanes, tmp_path, finished.append)
    assert len(finished) == 2
    assert lanes[0]["exit_code"] is None
    assert lanes[0]["error"]["type"] == "FileNotFoundError"
    assert lanes[1]["exit_code"] == 0


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_parallel_cancellation_joins_adapter_cleanup_without_signalling_its_group(
    tmp_path: Path, signum: signal.Signals
) -> None:
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        """
import pathlib, signal, subprocess, sys, time
root = pathlib.Path(sys.argv[2])
deadline = time.monotonic() + 10
if sys.argv[1] == 'child':
    def unexpected_signal(signum, frame):
        (root / 'child-signalled').touch()
    signal.signal(signal.SIGTERM, unexpected_signal)
    (root / 'child-ready').touch()
    while not (root / 'stop').exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    (root / 'child-cleaned').touch()
else:
    stopped = False
    def stop(signum, frame):
        global stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    child = subprocess.Popen([sys.executable, __file__, 'child', str(root)])
    while not stopped and time.monotonic() < deadline:
        time.sleep(0.01)
    (root / 'stop').touch()
    child.wait(timeout=5)
    (root / 'adapter-cleaned').touch()
"""
    )
    coordinator = """
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import grid
root = pathlib.Path(sys.argv[2])
lanes = []
for name in ('one', 'two'):
    lane = root / name
    lane.mkdir()
    lanes.append({'platform': name, 'command': [sys.executable, str(root/'adapter.py'), 'parent', str(lane)]})
try:
    grid.run_parallel(lanes, root, lambda lane: None)
except InterruptedError:
    (root/'interrupted.json').write_text(json.dumps(lanes))
    raise SystemExit(11)
raise SystemExit(99)
"""
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            coordinator,
            str(Path(grid.__file__).parent),
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        try:
            deadline = time.monotonic() + 5
            while not all(
                (tmp_path / name / "child-ready").exists() for name in ("one", "two")
            ):
                assert process.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.01)
            process.send_signal(signum)
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 11, (stdout, stderr)
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=15)
    for name in ("one", "two"):
        assert (tmp_path / name / "adapter-cleaned").exists()
        assert (tmp_path / name / "child-cleaned").exists()
        assert not (tmp_path / name / "child-signalled").exists()
    assert all(
        lane["interrupted"]
        for lane in json.loads((tmp_path / "interrupted.json").read_text())
    )


def test_signal_after_launch_registration_reaps_every_adapter(tmp_path: Path) -> None:
    lanes: list[dict[str, Any]] = [
        {
            "platform": name,
            "command": [sys.executable, "-c", "import time; time.sleep(10)"],
        }
        for name in ("one", "two")
    ]
    finished: list[dict[str, Any]] = []
    with pytest.raises(InterruptedError, match="owned adapters reaped"):
        grid.run_parallel(
            lanes,
            tmp_path,
            finished.append,
            on_started=lambda: os.kill(os.getpid(), signal.SIGTERM),
        )
    assert len(finished) == 2
    assert all(lane["interrupted"] for lane in finished)
    assert all(lane["exit_code"] != 0 for lane in finished)
