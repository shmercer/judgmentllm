"""Actual R/jsonlite consumes Python OpenAI audit 2 and schema-1 results."""

from __future__ import annotations

import csv
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

    def consume(manifest: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                rscript,
                "--vanilla",
                str(root / "examples/openai_batch_interoperability.R"),
                manifest,
                endpoint,
                capture,
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    result = consume(report.manifest_path, "--diagnostics")
    assert result.returncode == 0, (result.stdout, result.stderr)

    service.set("stall_poll", "batch_1")
    before = [event for event in service.events() if event[0] == "POST"]
    assert poll_batch("001", store=store).outcome == "partial"
    service.set("stall_poll", None)
    assert poll_batch("001", store=store).batch.accepted_jobs == 1
    assert [event for event in service.events() if event[0] == "POST"] == before
    recovered = export_batch_provenance("001", store=store)
    controls = [
        json.loads(line)["record"]
        for line in file_path(recovered, "controls.jsonl").read_text().splitlines()
    ]
    with file_path(recovered, "controls.csv").open(newline="") as stream:
        assert [json.loads(row["record"]) for row in csv.DictReader(stream)] == controls
    header, failure = [
        record for record in controls if record.get("http_status") == 503
    ]
    assert (header["reason"], failure["reason"]) == ("headers", "http_status")
    assert header["call_id"] == failure["call_id"]
    for record in (header, failure):
        for field in (
            "http_request_id",
            "member_status",
            "member_request_id",
            "provider_code",
        ):
            assert field in record and record[field] is None
    result = consume(recovered.manifest_path)
    assert result.returncode == 0, (result.stdout, result.stderr)

    # Independent negative check: usage cannot be charged per normalized trait.
    path = file_path(report, "members.jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["evidence"]["usage"]["input_tokens"] = 40
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert consume(report.manifest_path, "--diagnostics").returncode != 0
