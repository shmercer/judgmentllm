"""Actual Python-generated canonical/CSV/companion files consumed by strict R."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.r_interop
@pytest.mark.serial
def test_actual_decisions_exports_pass_strict_jsonlite(tmp_path: Path) -> None:
    executable = shutil.which("Rscript")
    assert executable is not None, "Required local Decisions acceptance needs Rscript"
    for phase in ("persist", "export"):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.decisions_r_fixture",
                "--phase",
                phase,
                "--output",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [
            executable,
            "--vanilla",
            str(Path(__file__).parents[1] / "examples/decisions_interoperability.R"),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "Decisions interoperability passed" in result.stdout
