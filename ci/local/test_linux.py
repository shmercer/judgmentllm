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
