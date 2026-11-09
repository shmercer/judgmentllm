"""Fresh-process bounded offline workloads use the real pinned SDK transport."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest


@pytest.mark.resource
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("kind", ["rubric", "pairwise"])
def test_anthropic_sdk_workload_is_bounded(
    tmp_path: Path, capture: str, kind: str
) -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "anthropic_resource_fixture.py"
    )
    process = subprocess.run(
        [
            sys.executable,
            str(fixture),
            "--directory",
            str(tmp_path / "workload"),
            "--endpoint",
            "messages",
            "--capture",
            capture,
            "--kind",
            kind,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert process.returncode == 0, process.stderr
    assert not process.stderr
    measured = cast(dict[str, int | float], json.loads(process.stdout))
    (tmp_path / "measurement.json").write_text(
        json.dumps(measured, sort_keys=True), encoding="utf-8"
    )
    evidence_directory = os.environ.get("JUDGMENTLLM_ANTHROPIC_RESOURCE_EVIDENCE")
    if evidence_directory:
        target = Path(evidence_directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"messages-{kind}-{capture}.json").write_text(
            json.dumps(measured, sort_keys=True), encoding="utf-8"
        )
    assert measured["calls"] == 64
    assert measured["successful_results"] == (320 if kind == "rubric" else 64)
    assert measured["worker_count"] == 4
    assert 0 < measured["request_queue_high_water"] <= 8
    assert 0 < measured["result_queue_high_water"] <= 4
    assert measured["serialization_seconds"] > 0
    assert 0 < measured["python_peak_bytes"] < 32 * 1024 * 1024
    assert measured["python_after_gc_bytes"] < measured["python_peak_bytes"]
    assert 0 < measured["native_peak_bytes"] < 256 * 1024 * 1024
