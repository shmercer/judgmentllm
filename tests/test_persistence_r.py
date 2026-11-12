"""Actual persisted Python outputs, reopened in another process, consumed by R."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from r_dependencies import require_rscript

from judgmentllm.io import read_results, read_results_csv
from judgmentllm.persistence.repository import Repository

FIXTURE = Path(__file__).resolve().parents[1] / "scripts" / "persistence_r_fixture.py"
R_SCRIPT = FIXTURE.parent.parent / "examples" / "persistence_interoperability.R"


def generate(output: Path) -> None:
    for phase in ("persist", "export"):
        completed = subprocess.run(
            [sys.executable, str(FIXTURE), "--phase", phase, "--output", str(output)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr


def test_separate_process_persistence_preserves_all_attempt_exports(
    tmp_path: Path,
) -> None:
    generate(tmp_path)
    for name, count in (("rubric", 6), ("pairwise", 3)):
        records = list(read_results(tmp_path / f"{name}.jsonl"))
        assert len(records) == count
        assert list(read_results_csv(tmp_path / f"{name}.csv")) == records
    rubric = list(read_results(tmp_path / "rubric.jsonl"))
    assert [result.status for result in rubric] == [
        "complete",
        "invalid",
        "invalid",
        "complete",
        "refused",
        "errored",
    ]
    assert (
        len({(result.run_id, result.task_id, result.attempt_id) for result in rubric})
        == 6
    )
    index = json.loads((tmp_path / "persistence_index.json").read_text())
    assert index["request_members"] == ["evidence", "organization", "clarity"]
    assert index["retry_members"] == ["organization"]
    assert index["transport_observations"] == 0
    assert index["transport_failure_evidence"] == 1
    with Repository(tmp_path / "store") as repository:
        assert list(repository.iter_results("foundation-rubric")) == rubric


@pytest.mark.r_interop
def test_persistence_outputs_pass_required_r_assertions(tmp_path: Path) -> None:
    executable = require_rscript()
    generate(tmp_path)
    completed = subprocess.run(
        [executable, "--vanilla", str(R_SCRIPT), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Persistence interoperability passed" in completed.stdout
