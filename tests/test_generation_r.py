"""Actual separately persisted Python generation sections consumed by R."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts" / "generation_r_fixture.py"
R_SCRIPT = ROOT / "examples" / "generation_interoperability.R"


def generate(output: Path) -> None:
    for phase in ("persist", "export"):
        completed = subprocess.run(
            [sys.executable, str(FIXTURE), "--phase", phase, "--output", str(output)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr


def test_generation_sections_reopened_in_a_separate_process(tmp_path: Path) -> None:
    generate(tmp_path)
    rows = [
        json.loads(line)
        for line in (tmp_path / "generation_manifests.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 6
    assert all(row["kind"] == "generation_manifest_fixture" for row in rows)
    assert [row["run_id"] for row in rows] == [f"{index:03d}" for index in range(1, 7)]
    assert all(row["generation"]["settings"] == {} for row in rows[2:])
    assert len({row["normalized_configuration_sha256"] for row in rows[2:]}) == 1
    assert len({row["configuration_source_sha256"] for row in rows[2:]}) == 4


def test_generation_outputs_pass_required_r_assertions(tmp_path: Path) -> None:
    executable = shutil.which("Rscript")
    if executable is None:
        if os.environ.get("JUDGMENTLLM_R_STRICT") == "1":
            pytest.fail("Required Rscript is unavailable")
        pytest.skip(
            "Rscript unavailable locally; required Ubuntu CI step does not skip"
        )
    available = subprocess.run(
        [
            executable,
            "--vanilla",
            "-e",
            "quit(status=if(requireNamespace('jsonlite',quietly=TRUE)) 0L else 1L)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if available.returncode:
        if os.environ.get("JUDGMENTLLM_R_STRICT") == "1":
            pytest.fail("Required jsonlite is unavailable")
        pytest.skip(
            "jsonlite unavailable locally; required Ubuntu CI step does not skip"
        )
    generate(tmp_path)
    completed = subprocess.run(
        [executable, "--vanilla", str(R_SCRIPT), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Generation interoperability passed" in completed.stdout
