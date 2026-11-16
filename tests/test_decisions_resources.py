"""Unchanged 180-second deadline and 256-MiB cap at new native boundaries."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.resource
@pytest.mark.parametrize("stage", ["index", "lifecycle"])
def test_native_index_queue_and_export_memory_is_bounded(
    tmp_path: Path, stage: str
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts/decisions_memory_fixture.py"),
            "--directory",
            str(tmp_path / "fixture"),
            "--stage",
            stage,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    (tmp_path / f"measurement-decisions-{stage}.json").write_text(
        json.dumps(data, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert 0 < data["native_peak_bytes"] < 256 * 1024**2
    if stage == "index":
        assert data["sources"] == data["members"] == 100_001
    else:
        assert data["results"] == 513 * 13
        assert (
            data["request_queue_high_water"] <= 8
            and data["result_queue_high_water"] <= 4
        )
