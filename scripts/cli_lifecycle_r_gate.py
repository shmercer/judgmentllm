"""Require the installed-console lifecycle acceptance in base R and jsonlite."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args()
    rscript = shutil.which("Rscript")
    console = Path(sys.executable).parent / (
        "judgmentllm.exe" if os.name == "nt" else "judgmentllm"
    )
    if rscript is None:
        parser.error("Required Rscript is unavailable")
    if not console.is_file():
        parser.error("Required installed judgmentllm executable is unavailable")
    root = Path(__file__).resolve().parents[1]
    started = time.monotonic()
    # Never forward developer authentication settings to retained R/CLI evidence.
    # Windows environment keys are case insensitive; use the same comparison here.
    excluded = {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "TOGETHER_API_KEY",
        "TYPESAFE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "CLOUDSDK_CONFIG",
    }
    environment = {
        key: value for key, value in os.environ.items() if key.upper() not in excluded
    }
    environment["JUDGMENTLLM_R_STRICT"] = "1"
    completed = subprocess.run(
        [
            rscript,
            "--vanilla",
            str(root / "examples/cli_lifecycle_interoperability.R"),
            str(options.output.resolve()),
            str(console),
            sys.executable,
            str(root / "scripts/cli_lifecycle_fixture.py"),
        ],
        env=environment,
        timeout=180,
        check=False,
    )
    if completed.returncode:
        raise SystemExit(completed.returncode)
    print(f"Installed CLI R lifecycle gate passed in {time.monotonic() - started:.2f}s")


if __name__ == "__main__":
    main()
