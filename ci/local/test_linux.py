"""Exercise Linux lane scheduling without Docker, downloads or virtual machines."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

LOCAL = Path(__file__).parent
PINS = {"3.11": "3.11.18", "3.12": "3.12.16", "3.15": "3.15.2"}


@pytest.mark.parametrize("platform", ["linux", "windows", "macos"])
@pytest.mark.parametrize(
    "option,value,message",
    [
        ("--distribution", "loadfile; invalid-command", "Invalid pytest distribution"),
        ("--benchmark-workers", "0,2; invalid-command", "Invalid worker sweep"),
        ("--benchmark-workers", "0,2,32", "Invalid worker sweep"),
    ],
)
def test_worker_options_fail_before_any_host_or_guest_control(
    platform: str, option: str, value: str, message: str
) -> None:
    result = subprocess.run(
        ["bash", str(LOCAL / (platform + ".sh")), "--mode", "doctor", option, value],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.parametrize("sweep", ["", "0,2,4"])
def test_macos_ssh_argument_join_preserves_optional_worker_sweep(
    tmp_path: Path, sweep: str
) -> None:
    source = (LOCAL / "macos.sh").read_text()
    invocation = next(
        line
        for line in source.splitlines()
        if line.startswith("ssh_guest /bin/bash -s --")
    )
    assignments = source.split("<<'GUEST' &\n", 1)[1].split(
        'ROOT="$HOME/judgmentllm-ci"', 1
    )[0]
    values = {
        "RUN_ID": "owned-run",
        "MODE": "check",
        "PYTHON_VERSION": "all",
        "REF": "a" * 40,
        "ARCHIVE_SHA256": "b" * 64,
        "UV_BINARY_SHA256": "none",
        "UV_SHA256": "none",
        "WORKERS": "2",
        "MAX_LANES": "1",
        "RESOURCE_SLOTS": "1",
        "SUITE_STAGE": "full",
        "BENCHMARK_WORKERS": sweep,
        "DISTRIBUTION": "loadfile",
    }
    setup = "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items())
    program = (
        "set -euo pipefail\n"
        + setup
        + '\nssh_guest() { bash -c "$*"; }\n'
        + invocation
        + "\n"
        + assignments
        + '\nprintf "%s|%s|%s\\n" "$WORKERS" "$BENCHMARK_WORKERS" "$DISTRIBUTION"\nGUEST\nwait\n'
    )
    completed = subprocess.run(
        ["bash", "-c", program],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == f"2|{sweep}|loadfile"


@pytest.mark.parametrize(
    "option,value,message",
    [
        ("--workers", "6", "Workers exceed the measured four-CPU guest"),
        ("--benchmark-workers", "0,2,4,6", "Invalid worker sweep"),
    ],
)
def test_macos_worker_cap_uses_runtime_topology_before_guest_startup(
    option: str, value: str, message: str
) -> None:
    result = subprocess.run(
        ["bash", str(LOCAL / "macos.sh"), "--mode", "doctor", option, value],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.parametrize("selection", ["all", "3.11", "3.12", "3.15"])
@pytest.mark.parametrize("failure", ["", "3.15"])
def test_linux_launches_selected_lanes_together_and_joins_every_result(
    tmp_path: Path, selection: str, failure: str
) -> None:
    # Execute the actual launch/join block with a replacement for start_container.
    # Every fake container waits for all peers to start before it can finish. A
    # sequential launcher fails the barrier instead of depending on timing ratios.
    launcher = (LOCAL / "linux.sh").read_text()
    scheduling = launcher[launcher.index("versions=(") :]
    expected = list(PINS) if selection == "all" else [selection]
    fake = tmp_path / "container.py"
    fake.write_text(
        """
import json, pathlib, sys, time
count, failure = int(sys.argv[1]), sys.argv[2]
lane = pathlib.Path(sys.argv[3]).parent
version = lane.name.removeprefix('python-')
(lane / 'command.json').write_text(json.dumps(sys.argv[5:]))
(lane / 'ready').touch()
deadline = time.monotonic() + 5
while len(list(lane.parent.glob('python-*/ready'))) != count:
    if time.monotonic() > deadline:
        raise SystemExit(99)
    time.sleep(0.01)
(lane / 'finished').touch()
raise SystemExit(7 if version == failure else 0)
"""
    )
    setup = {
        "run_root": str(tmp_path),
        "state_root": "/mnt/VM_Fast/judgmentllm-ci/linux",
        "run_id": "synthetic-run",
        "ref": "a" * 40,
        "archive_hash": "b" * 64,
        "image": "judgmentllm-local-ci:synthetic",
        "python_version": selection,
        "mode": "check",
        "workers": "0",
        "max_lanes": "3",
        "resource_slots": "3",
        "cpu_limit": "6",
        "suite_stage": "full",
        "benchmark_workers": "",
        "distribution": "loadfile",
        "fake_python": sys.executable,
        "fake_program": str(fake),
        "expected": str(len(expected)),
        "failure": failure,
    }
    program = "set -Eeuo pipefail\n" + "\n".join(
        f"{name}={shlex.quote(value)}" for name, value in setup.items()
    )
    program += """
container_clients=()
fatal() { echo "$*" >&2; exit 2; }
start_container() {
  "$fake_python" "$fake_program" "$expected" "$failure" "$@" &
  container_clients+=("$!")
}
"""
    completed = subprocess.run(
        ["bash", "-c", program + scheduling],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == (1 if failure in expected else 0), (
        completed.stdout + completed.stderr
    )
    assert sorted(path.name for path in tmp_path.glob("python-*")) == [
        f"python-{version}" for version in expected
    ]
    for version in expected:
        lane = tmp_path / f"python-{version}"
        assert (lane / "finished").exists()
        assert (lane / "exit-code").read_text().strip() == (
            "7" if failure == version else "0"
        )
        assert (lane / "scratch").is_dir()
        arguments = cast(list[str], json.loads((lane / "command.json").read_text()))
        assert arguments[:6] == ["--network", "none", "--cpus", "6", "--memory", "16g"]
        assert arguments[-7:] == [
            "/state/runs/synthetic-run/bundle",
            f"/state/runs/synthetic-run/python-{version}",
            "check",
            version,
            PINS[version],
            "a" * 40,
            "b" * 64,
        ]
        assert f"TMPDIR=/state/runs/synthetic-run/python-{version}/scratch" in arguments
        script = arguments[arguments.index("-c") + 1]
        assert 'sha256sum "$bundle/source.tar"' in script
        assert '[[ ${actual%% *} == "$archive_hash" ]]' in script
        assert 'tar -xf "$bundle/source.tar" -C "$lane/source"' in script
        assert '"$lane/source/.judgmentllm-source.json"' in script
        assert '--python "$patch" --offline python' in script
        assert '--output "$lane/results"' in script
        assert '--commit "$commit" --archive-sha256 "$archive_hash"' in script


@pytest.mark.parametrize("failed_version", ["", "3.12"])
def test_linux_reuses_completed_slot_without_waiting_for_oldest_lane(
    tmp_path: Path, failed_version: str
) -> None:
    scheduling = (LOCAL / "linux.sh").read_text().split("versions=(", 1)[1]
    fake = tmp_path / "container.py"
    fake.write_text(
        "import pathlib, sys, time\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "version, failed = sys.argv[2:]\n"
        "(root / ('started-' + version)).touch()\n"
        "if version == '3.11':\n"
        "    deadline = time.monotonic() + 5\n"
        "    while not (root / 'started-3.15').exists():\n"
        "        if time.monotonic() > deadline: raise SystemExit(99)\n"
        "        time.sleep(0.01)\n"
        "(root / ('finished-' + version)).touch()\n"
        "raise SystemExit(7 if version == failed else 0)\n"
    )
    values = {
        "run_root": str(tmp_path),
        "state_root": "/state",
        "run_id": "synthetic",
        "ref": "a" * 40,
        "archive_hash": "b" * 64,
        "image": "synthetic",
        "python_version": "all",
        "mode": "check",
        "workers": "4",
        "max_lanes": "2",
        "resource_slots": "2",
        "cpu_limit": "6",
        "suite_stage": "full",
        "benchmark_workers": "",
        "distribution": "loadfile",
        "fake_python": sys.executable,
        "fake_program": str(fake),
        "failed_version": failed_version,
    }
    setup = "set -Eeuo pipefail\n" + "\n".join(
        f"{key}={shlex.quote(value)}" for key, value in values.items()
    )
    setup += """
container_clients=()
fatal() { exit 2; }
start_container() {
  "$fake_python" "$fake_program" "$run_root" "$version" "$failed_version" &
  container_clients+=("$!")
}
"""
    result = subprocess.run(
        ["bash", "-c", setup + "versions=(" + scheduling],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == bool(failed_version), result.stdout + result.stderr
    for version in PINS:
        assert (tmp_path / f"finished-{version}").exists()
        expected = "7" if version == failed_version else "0"
        assert (
            tmp_path / f"python-{version}/exit-code"
        ).read_text().strip() == expected


def test_linux_image_preserves_pinned_isolated_toolchain_and_cleanup() -> None:
    dockerfile = (LOCAL / "Dockerfile").read_text()
    launcher = (LOCAL / "linux.sh").read_text()
    assert (
        "uv python install --no-bin --no-registry " + " ".join(PINS.values())
        in dockerfile
    )
    assert (
        'org.judgmentllm.local-ci.python="' + ",".join(PINS.values()) + '"'
        in dockerfile
    )
    assert 'org.judgmentllm.local-ci.uv="0.13.6"' in dockerfile
    for checksum in (
        "f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645",
        "e947f4ce2c3a8194ae42c4015cdc557cec1c1cea06f85cd20da037aa93dfa5e4",
    ):
        assert checksum in dockerfile and checksum in launcher
    assert "UV_PYTHON_INSTALL_DIR=/opt/judgmentllm/python" in dockerfile
    assert "R CMD INSTALL --library=/opt/judgmentllm/r-library" in dockerfile
    assert "base_image=pairwisellm-linux-ci:release" in launcher
    assert "3.11|3.12|3.15|all" in launcher
    assert "3\\.11|3\\.12|3\\.15|all" in launcher
    assert "docker run --rm --init --stop-signal SIGTERM" in launcher
    assert '--label "$run_label=$run_id"' in launcher
    assert '[[ $actual == "$candidate" && $label == "$run_id" ]]' in launcher
    assert 'docker stop --timeout=-1 "$candidate"' in launcher
    assert 'wait "$client" 2>/dev/null || true' in launcher
