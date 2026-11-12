"""Required R assertions consume actual independently reopened source indexes."""

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

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts" / "source_preparation_r_fixture.py"
R_SCRIPT = ROOT / "examples" / "source_preparation_interoperability.R"


def generate(output: Path) -> None:
    for phase in ("persist", "export"):
        completed = subprocess.run(
            [sys.executable, str(FIXTURE), "--phase", phase, "--output", str(output)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr


@pytest.fixture(scope="module")
def generated_exports(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    # Both readers consume one real persist/export pipeline. The helper verifies
    # every artifact remains byte-identical after all readers finish.
    with immutable_exports(tmp_path_factory.mktemp("rf"), generate) as output:
        yield output


def test_source_preparation_reopens_in_independent_processes(
    generated_exports: Path,
) -> None:
    rows = [
        json.loads(line)
        for line in (generated_exports / "source_preparation.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [row["run_id"] for row in rows] == ["001", "002", "003", "004"]
    assert [row["capture"] for row in rows] == ["full", "minimal"] * 2
    assert [len(row["manifest"]["sources"]) for row in rows] == [5, 5, 6, 6]
    assert all(row["response_ids"] == ["001", "002"] for row in rows)
    assert all(row["request_count"] == 2 for row in rows)
    for row in rows:
        assert row["manifest"]["kind"] == "executable_run_manifest"
        assert Path(row["manifest_path"]).is_absolute()
        assert row["manifest"]["run_id"] == row["run_id"]
        assert "results" not in row
        assert "attempts" not in row
    assert rows[2]["pairs"][1]["presented_order"] == ["002", "001"]
    assert rows[3]["pairs"][1]["presented_order"] == ["002", "001"]


@pytest.mark.r_interop
def test_source_preparation_outputs_pass_required_r_assertions(
    generated_exports: Path,
) -> None:
    executable = shutil.which("Rscript")
    if executable is None:
        if os.environ.get("JUDGMENTLLM_R_STRICT") == "1":
            pytest.fail("Required Rscript is unavailable")
        pytest.skip(
            "Rscript unavailable locally; required local validation does not skip"
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
            "jsonlite unavailable locally; required local validation does not skip"
        )
    completed = subprocess.run(
        [executable, "--vanilla", str(R_SCRIPT), str(generated_exports)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Source preparation interoperability passed" in completed.stdout
