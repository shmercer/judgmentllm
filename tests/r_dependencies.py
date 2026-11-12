"""Uncached dependency preflight for local R interoperability assertions."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


def require_rscript() -> str:
    """Return Rscript after checking jsonlite, honoring required local validation."""
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
    return executable
