"""Actual installed-SDK Python-to-R production and filtered provenance round-trip."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.r_interop
@pytest.mark.serial
def test_strict_production_exports_in_real_r(tmp_path: Path) -> None:
    rscript = shutil.which("Rscript")
    assert rscript is not None, "Required local Decisions acceptance needs Rscript"
    for phase in ("persist", "export"):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.openai_decisions_r_fixture",
                "--phase",
                phase,
                "--output",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
    result = subprocess.run(
        [
            rscript,
            "--vanilla",
            str(
                Path(__file__).parents[1]
                / "examples/openai_decisions_interoperability.R"
            ),
            str(tmp_path),
        ],
        env={**os.environ, "JUDGMENTLLM_R_STRICT": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "OpenAI Decisions interoperability passed" in result.stdout
