"""Actual R consumers are local; installed R-driven orchestration is gate seven."""

import csv
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_batch_exports_retry import file_path

from judgmentllm import (
    BatchMockOptions,
    cancel_batch,
    collect_batch,
    export_batch_provenance,
    poll_batch,
    prepare_batch,
    submit_batch,
)
from judgmentllm.execution.batch_exports import BatchAuditReportV1


def make_audit(tmp_path: Path, case: str) -> BatchAuditReportV1:
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
    return report


def consume(report: BatchAuditReportV1) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[1]
    rscript = shutil.which("Rscript")
    assert rscript is not None, "required local Rscript is unavailable"
    return subprocess.run(
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
    completed = consume(make_audit(tmp_path, case))
    assert completed.returncode == 0, (completed.stdout, completed.stderr)


@pytest.mark.r_interop
@pytest.mark.parametrize("fault", ["lossless_identity", "r_score", "unknown_usage"])
def test_r_consumer_rejects_changed_csv_and_zero_unknown_usage(
    tmp_path: Path, fault: str
) -> None:
    report = make_audit(tmp_path, "batch-terminal-full")
    if fault == "unknown_usage":
        path = file_path(report, "members.jsonl")
        members = [json.loads(line) for line in path.read_text().splitlines()]
        sibling = next(
            m for m in members if m["evidence"]["kind"] == "confirmed_cancellation"
        )
        assert sibling["evidence"]["usage"]["output_tokens"] is None
        sibling["evidence"]["usage"]["output_tokens"] = 0
        path.write_text("".join(json.dumps(m) + "\n" for m in members))
    else:
        path = file_path(
            report, "results.csv" if fault == "lossless_identity" else "results_r.csv"
        )
        with path.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames
            rows = list(reader)
        assert fields is not None
        row = next(row for row in rows if row["status"] == "complete")
        row["request_uid" if fault == "lossless_identity" else "primary_score"] = (
            "wrong-request" if fault == "lossless_identity" else "2.9"
        )
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    rejected = consume(report)
    assert rejected.returncode != 0
