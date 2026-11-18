"""R consumes the actual installed CLI's JSONL, CSV and Jev provenance."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_decisions_contracts import pair_spec
from test_typesafe_decisions import jev_spec, native_spec, ten_level_jev_spec
from test_typesafe_decisions_processes import HOOK, cli, config_path, control

from judgmentllm.persistence.repository import Repository


@pytest.mark.r_interop
@pytest.mark.serial
def test_strict_jev_exports_in_real_r(tmp_path: Path) -> None:
    rscript = shutil.which("Rscript")
    assert rscript is not None, "Required for strict local Jev R acceptance"
    exports: dict[str, dict[str, Any]] = {}
    for case in (
        "single",
        "grouped",
        "pairwise",
        "score_precision",
        "ten_level_score_precision",
    ):
        root = tmp_path / case
        base = (
            pair_spec(root / "input", criteria=2)
            if case == "pairwise"
            else native_spec(
                root / "input",
                traits=3 if case == "grouped" else 1,
                maximum=32 if case == "grouped" else None,
                capture="minimal" if case == "single" else "full",
            )
        )
        spec = (
            ten_level_jev_spec(base)
            if case == "ten_level_score_precision"
            else jev_spec(base)
        )
        result = control(
            cli(
                root,
                "run",
                "--config",
                config_path(root, spec),
                response={
                    "grouped": "filtered",
                    "score_precision": "score_precision",
                    "ten_level_score_precision": "ten_level_score_precision",
                }.get(case, "clean"),
            )
        )
        selection = ("--run", result["run_id"])
        if case == "grouped":
            with Repository(root / "store") as repository:
                failed = next(
                    row
                    for row in repository.iter_results(result["run_id"])
                    if row.status == "invalid"
                )
            control(
                cli(
                    root,
                    "resume",
                    *selection,
                    "--retry-task",
                    failed.task_id,
                    "--retry-id",
                    "retry-001",
                    response="clean",
                )
            )
        exports[case] = {
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
    export_path = tmp_path / "exports.json"
    export_path.write_text(json.dumps(exports), encoding="utf-8")
    checked = subprocess.run(
        [rscript, "--vanilla", str(HOOK / "interop.R"), str(export_path)],
        env={
            "PATH": os.environ.get("PATH", ""),
            "TMPDIR": str(tmp_path),
            "JUDGMENTLLM_R_STRICT": "1",
            **{
                key: os.environ[key]
                for key in ("SystemRoot", "WINDIR", "R_LIBS_USER")
                if key in os.environ
            },
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert checked.returncode == 0, (checked.stdout, checked.stderr)
    assert "Jev Decisions interoperability passed" in checked.stdout
