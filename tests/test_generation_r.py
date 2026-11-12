"""Actual separately persisted Python generation sections consumed by R."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixture_artifacts import immutable_exports
from r_dependencies import require_rscript

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


@pytest.fixture(scope="module")
def generated_exports(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    # Both readers consume one real persist/export pipeline. The helper verifies
    # every artifact remains byte-identical after all readers finish.
    with immutable_exports(tmp_path_factory.mktemp("rf"), generate) as output:
        yield output


def test_generation_sections_reopened_in_a_separate_process(
    generated_exports: Path,
) -> None:
    rows = [
        json.loads(line)
        for line in (generated_exports / "generation_manifests.jsonl")
        .read_text()
        .splitlines()
    ]
    assert len(rows) == 6
    assert all(row["kind"] == "generation_manifest_fixture" for row in rows)
    assert [row["run_id"] for row in rows] == [f"{index:03d}" for index in range(1, 7)]
    assert all(row["generation"]["settings"] == {} for row in rows[2:])
    assert len({row["normalized_configuration_sha256"] for row in rows[2:]}) == 1
    assert len({row["configuration_source_sha256"] for row in rows[2:]}) == 4


@pytest.mark.r_interop
def test_generation_outputs_pass_required_r_assertions(generated_exports: Path) -> None:
    executable = require_rscript()
    completed = subprocess.run(
        [executable, "--vanilla", str(R_SCRIPT), str(generated_exports)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Generation interoperability passed" in completed.stdout
