"""Actual R/jsonlite consumes Python OpenAI audit 2 and schema-1 results."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from openai_batch_fixture import Service, install
from test_batch_exports_retry import file_path

from judgmentllm import (
    collect_batch,
    export_batch_provenance,
    poll_batch,
    prepare_batch,
    submit_batch,
)
from judgmentllm.config import load_run_spec


@pytest.mark.r_interop
@pytest.mark.parametrize(
    "case,endpoint,capture",
    [
        ("openai-batch-responses-full", "responses", "full"),
        ("openai-batch-chat-minimal", "chat.completions", "minimal"),
    ],
)
def test_real_r_openai_grouped_and_reversed_pair(
    tmp_path: Path, monkeypatch: Any, case: str, endpoint: str, capture: str
) -> None:
    root = Path(__file__).resolve().parents[1]
    module_spec = importlib.util.spec_from_file_location(
        "openai_gate_fixture", root / "scripts/openai_batch_fixture.py"
    )
    assert module_spec is not None and module_spec.loader is not None
    module: Any = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    module.create(tmp_path)
    directory = tmp_path / case
    service = Service(directory / "service.sqlite")
    install(monkeypatch, service)
    config = directory / "config.json"
    spec = load_run_spec(config)
    store = directory / "store"
    prepare_batch(config, store=store, run_id="001")
    service.configure(store, spec)
    submit_batch("001", store=store)
    poll_batch("001", store=store)
    collect_batch("001", store=store)
    collect_batch("001", store=store)
    report = export_batch_provenance("001", store=store)
    rscript = shutil.which("Rscript")
    assert rscript is not None, "Required local Rscript is unavailable"

    def consume() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                rscript,
                "--vanilla",
                str(root / "examples/openai_batch_interoperability.R"),
                report.manifest_path,
                endpoint,
                capture,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    result = consume()
    assert result.returncode == 0, (result.stdout, result.stderr)
    # Independent negative check: usage cannot be charged per normalized trait.
    path = file_path(report, "members.jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["evidence"]["usage"]["input_tokens"] = 40
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert consume().returncode != 0
