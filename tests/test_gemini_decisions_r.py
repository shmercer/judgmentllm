"""Exercise Gemini Decisions exports through the installed console and real R."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_decisions_contracts import pair_spec
from test_decisions_lifecycle import native_spec
from test_gemini_decisions import gemini_spec
from test_gemini_decisions_processes import HOOK, cli, config_path, control

from judgmentllm.persistence.repository import Repository


@pytest.mark.r_interop
@pytest.mark.serial
def test_strict_gemini_exports_in_real_r(tmp_path: Path) -> None:
    rscript = shutil.which("Rscript")
    assert rscript is not None, (
        "Required for strict local Gemini Decisions R acceptance"
    )
    exports: dict[str, dict[str, Any]] = {}
    for name in ("rubric", "pairwise", "rejection", "refusal", "oversized-integer"):
        root = tmp_path / name
        spec = gemini_spec(
            pair_spec(root / "input", criteria=2)
            if name == "pairwise"
            else native_spec(
                root / "input", traits=2 if name == "oversized-integer" else 3
            )
        )
        result = control(
            cli(
                root,
                "run",
                "--config",
                config_path(root, spec),
                scenario={
                    "rubric": "filtered",
                    "rejection": "context",
                    "refusal": "200-refusal",
                    "oversized-integer": "oversized-integer",
                }.get(name, "clean"),
            )
        )
        run_id = result["run_id"]
        selection = ("--run", run_id)
        if name in ("rubric", "refusal"):
            failed_status = "invalid" if name == "rubric" else "refused"
            with Repository(root / "store") as repository:
                failed = next(
                    row
                    for row in repository.iter_results(run_id)
                    if row.status == failed_status
                )
            retry = ("--retry-task", failed.task_id, "--retry-id", "retry-001")
            for _ in range(2):
                control(cli(root, "resume", *selection, *retry))
            assert (root / "forwards").read_text().splitlines() == [
                "decisions",
                "decisions",
            ]
        exports[name] = {
            "canonical": control(cli(root, "results", "export", *selection))["data"],
            "csv": control(
                cli(root, "results", "export", *selection, "--format", "csv")
            )["data"],
            "r": control(
                cli(
                    root,
                    "results",
                    "export",
                    *selection,
                    "--format",
                    "csv",
                    "--view",
                    "r",
                )
            )["data"],
            "provenance": control(cli(root, "results", "provenance", *selection))[
                "data"
            ],
        }
    path = tmp_path / "exports.json"
    path.write_text(json.dumps(exports), encoding="utf-8")
    checked = subprocess.run(
        [rscript, "--vanilla", str(HOOK / "interop.R"), str(path)],
        env={
            **{
                key: os.environ[key]
                for key in ("PATH", "SystemRoot", "WINDIR", "R_LIBS_USER")
                if key in os.environ
            },
            "TMPDIR": str(tmp_path),
            "JUDGMENTLLM_R_STRICT": "1",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert checked.returncode == 0, (checked.stdout, checked.stderr)
    assert "Gemini Decisions interoperability passed" in checked.stdout
