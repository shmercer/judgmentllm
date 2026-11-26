"""R/jsonlite checks real schema-one exports and Anthropic audit provenance."""

import shutil
import subprocess
from pathlib import Path

import pytest

from judgmentllm import (
    collect_batch,
    export_batch_provenance,
    poll_batch,
    prepare_batch,
    submit_batch,
)
from scripts.anthropic_batch_fixture import Service, installed, write_case


@pytest.mark.r_interop
@pytest.mark.parametrize("capture,pairwise", [("full", False), ("minimal", True)])
def test_actual_anthropic_batch_exports_in_r(
    tmp_path: Path, capture: str, pairwise: bool
) -> None:
    config = write_case(tmp_path, capture=capture, pairwise=pairwise)
    store = tmp_path / "store"
    prepare_batch(config, store=store, run_id="001")
    with installed(Service(tmp_path / "service.sqlite")):
        submit_batch("001", store=store)
        poll_batch("001", store=store)
        assert collect_batch("001", store=store).outcome == "ok"
    report = export_batch_provenance("001", store=store)
    rscript = shutil.which("Rscript")
    assert rscript is not None, "Required local Rscript is unavailable"
    consumer = (
        Path(__file__).resolve().parents[1]
        / "examples/anthropic_batch_interoperability.R"
    )
    result = subprocess.run(
        [
            rscript,
            "--vanilla",
            str(consumer),
            report.manifest_path,
            capture,
            "pairwise" if pairwise else "rubric",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
