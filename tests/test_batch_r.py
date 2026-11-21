"""Actual R consumers are local; installed R-driven orchestration is gate seven."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from judgmentllm import (
    BatchMockOptions,
    cancel_batch,
    collect_batch,
    export_batch_provenance,
    poll_batch,
    prepare_batch,
    submit_batch,
)


@pytest.mark.r_interop
@pytest.mark.parametrize(
    "case",
    [
        "batch-terminal-full",
        "batch-terminal-minimal",
        "batch-missing",
        "batch-pairwise",
    ],
)
def test_real_r_consumes_python_batch_exports(tmp_path: Path, case: str) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "batch_cli_fixture", root / "scripts" / "batch_cli_fixture.py"
    )
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    fixture.create(tmp_path)
    directory = tmp_path / case
    fixture_path = directory / "batch-fixture.json"
    recipe = json.loads(fixture_path.read_text())
    recipe["ack_delay_seconds"] = 0
    recipe["status_failures"] = 0
    fixture_path.write_text(json.dumps(recipe))
    mock = BatchMockOptions(fixture_path=fixture_path)
    store = directory / "store"
    prepare_batch(directory / "config.json", store=store, run_id="001", mock=mock)
    submit_batch("001", store=store, mock=mock)
    if case != "batch-pairwise":
        cancel_batch("001", store=store, mock=mock)
    poll_batch("001", store=store, mock=mock)
    collect_batch("001", store=store, mock=mock)
    report = export_batch_provenance("001", store=store)
    rscript = shutil.which("Rscript")
    assert rscript is not None, "required local Rscript is unavailable"
    completed = subprocess.run(
        [
            rscript,
            "--vanilla",
            str(root / "examples" / "batch_interoperability.R"),
            report.manifest_path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
