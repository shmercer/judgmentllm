"""Independent resource child; preserve the existing 32/256-MiB ceilings."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.resource
@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_batch_sdk_grouped_workload_is_bounded(
    tmp_path: Path, endpoint: str, capture: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    scratch = tmp_path / "temp"
    scratch.mkdir()
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(root / "src"),
        "TMPDIR": str(scratch),
        "TMP": str(scratch),
        "TEMP": str(scratch),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(root / "scripts/openai_batch_resource_fixture.py"),
            "--directory",
            str(tmp_path / "workload"),
            "--endpoint",
            endpoint,
            "--capture",
            capture,
        ],
        env=environment,
        cwd=scratch,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    measured = json.loads(result.stdout)
    assert measured["successful_results"] == 320 and measured["members"] == 64
    assert measured["mutation_forwards"] == 2
    assert 0 < measured["python_peak_bytes"] < 32 * 1024 * 1024
    assert measured["python_after_gc_bytes"] < measured["python_peak_bytes"]
    assert 0 < measured["native_peak_bytes"] < 256 * 1024 * 1024
    (tmp_path / "measurement.json").write_text(json.dumps(measured))
