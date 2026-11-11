"""R consumes actual mock execution and independently reopened evidence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixture_artifacts import immutable_exports

from judgmentllm.io import read_results, read_results_csv

# This full multi-provider fixture has a fixed 60-second subprocess deadline.
# Preserve the deadline while keeping SDK/persistence work out of xdist contention.
pytestmark = pytest.mark.serial

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts" / "execution_r_fixture.py"
R_SCRIPT = ROOT / "examples" / "execution_interoperability.R"


def generate(output: Path) -> None:
    for phase in ("persist", "export"):
        completed = subprocess.run(
            [sys.executable, str(FIXTURE), "--phase", phase, "--output", str(output)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr


@pytest.fixture(scope="module")
def generated_exports(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    # Both readers consume one real persist/export pipeline. The helper verifies
    # every artifact remains byte-identical after all readers finish.
    with immutable_exports(tmp_path_factory.mktemp("rf"), generate) as output:
        yield output


def test_execution_reopens_without_duplicate_acceptances(
    generated_exports: Path,
) -> None:
    rows = [
        json.loads(line)
        for line in (generated_exports / "execution.jsonl").read_text().splitlines()
    ]
    assert [row["run_id"] for row in rows] == ["001", "002", "003", "004"]
    assert [row["capture"] for row in rows] == ["full", "minimal"] * 2
    assert all(row["run"]["state"] == "completed" for row in rows)
    for row in rows:
        assert (
            len(row["attempts"])
            == len(row["submissions"])
            == len(row["observations"])
            == 2
        )
        assert len(row["acceptances"]) == 2
        assert all(accepted["acceptance_count"] == 1 for accepted in row["acceptances"])
    for name in ("rubric", "pairwise"):
        records = list(read_results(generated_exports / f"{name}.jsonl"))
        assert len(records) == 4
        assert list(read_results_csv(generated_exports / f"{name}.csv")) == records
        assert all(record.status == "complete" for record in records)


def test_execution_outputs_pass_required_r_assertions(generated_exports: Path) -> None:
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
    completed = subprocess.run(
        [executable, "--vanilla", str(R_SCRIPT), str(generated_exports)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Execution interoperability passed" in completed.stdout
