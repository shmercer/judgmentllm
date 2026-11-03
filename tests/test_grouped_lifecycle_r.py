"""Independent Python processes and strict base R/jsonlite lifecycle assertions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from judgmentllm.io import read_results, read_results_csv

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts" / "grouped_lifecycle_r_fixture.py"
R_SCRIPT = ROOT / "examples" / "grouped_lifecycle_interoperability.R"


def generate(output: Path) -> None:
    for phase in ("persist", "export"):
        completed = subprocess.run(
            [sys.executable, str(FIXTURE), "--phase", phase, "--output", str(output)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr


def test_grouped_lifecycle_survives_independent_processes(tmp_path: Path) -> None:
    generate(tmp_path)
    evidence = [
        json.loads(line)
        for line in (tmp_path / "grouped-lifecycle.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["run_id"] for row in evidence] == ["001", "002"]
    for row in evidence:
        assert len(row["attempts"]) == len(row["acceptances"]) == 2
        assert [len(attempt["task_ids"]) for attempt in row["attempts"]] == [3, 1]
        assert all(accepted["acceptance_count"] == 1 for accepted in row["acceptances"])
        records = list(read_results(tmp_path / f"{row['run_id']}.jsonl"))
        assert records == list(read_results_csv(tmp_path / f"{row['run_id']}.csv"))
        assert [record.status for record in records] == [
            "complete",
            "complete",
            "invalid",
            "complete",
        ]


def test_grouped_lifecycle_passes_required_r_assertions(tmp_path: Path) -> None:
    executable = shutil.which("Rscript")
    if executable is None:
        if os.environ.get("JUDGMENTLLM_R_STRICT") == "1":
            pytest.fail("Required Rscript is unavailable")
        pytest.skip("Rscript unavailable locally; required Ubuntu CI does not skip")
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
        pytest.skip("jsonlite unavailable locally; required Ubuntu CI does not skip")
    generate(tmp_path)
    completed = subprocess.run(
        [executable, "--vanilla", str(R_SCRIPT), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Grouped lifecycle interoperability passed" in completed.stdout
