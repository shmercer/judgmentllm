"""Tiny tool checks; full diagnostics remain separately invoked qualification."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from judgmentllm.config import load_run_spec
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.persistence.repository import Repository
from judgmentllm.providers import MockAdapter, MockScenario


@pytest.mark.asyncio
async def test_small_diagnostic_export_and_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "path",
        [str(Path(__file__).resolve().parents[1] / "benchmarks"), *sys.path],
    )
    diagnostic = importlib.import_module("scale_diagnostics")
    configuration, store = diagnostic.fixture(tmp_path / "sample", 1)
    spec = load_run_spec(configuration)
    adapter = MockAdapter(
        configuration.parent / "service.sqlite3",
        scenario=MockScenario(probabilities=True),
    )
    result = await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert result.summary.results == 20
    before = diagnostic.exports(store)
    with Repository(store) as repository:
        request = next(repository.iter_requests("001"))
        task = repository.request_task_ids("001", request.request_uid)[0]
    # Authorization is idempotent and never resubmits until recovery runs.
    diagnostic.authorize(
        store,
        configuration,
        adapter,
        [(request.request_uid, (task,), "repeat-one")],
        allow_completed=True,
    )
    assert adapter.ledger.acceptance_count() == 4
    result = await recover_run("001", store=store, adapter=adapter, spec=spec)
    assert result.summary.results == 21 and adapter.ledger.acceptance_count() == 5
    after = diagnostic.exports(store)
    assert [row["rows"] for row in after] == [21, 21, 21]
    previous = diagnostic.exports(store, high_water_mark=20)
    assert [row["sha256"] for row in before] == [row["sha256"] for row in previous]
    rows = [
        json.loads(line) for line in Path(after[0]["path"]).read_text().splitlines()
    ]
    assert sum(row["task_id"] == task for row in rows) == 2
    assert all(row["status"] == "complete" and row["probabilities"] for row in rows)


@pytest.mark.parametrize("capture", ["minimal", "full"])
def test_focused_initialization_traces_real_session_without_creating_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture: str
) -> None:
    monkeypatch.setattr(
        sys,
        "path",
        [str(Path(__file__).resolve().parents[1] / "benchmarks"), *sys.path],
    )
    workloads = importlib.import_module("workloads")
    configuration = workloads.inputs(
        tmp_path / "inputs", workloads.scale_workload(responses=2), capture
    )
    before = {path.name: path.read_bytes() for path in configuration.parent.iterdir()}
    root = Path(__file__).resolve().parents[1]
    child = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'benchmarks'))
from focused_execution import initialization_sample
from judgmentllm.execution.session import ExecutionSession
from judgmentllm.providers.mock import MockAdapter
def forbidden(*args, **kwargs):
    raise AssertionError('Initialization advanced execution')
ExecutionSession.next_work = forbidden
MockAdapter.submit = forbidden
initialization_sample(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), 2, sys.argv[4])
"""
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            str(root),
            str(configuration),
            str(tmp_path / "init"),
            capture,
        ],
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads((tmp_path / "init/report.json").read_text())
    assert result["indexed_responses"] == 2
    assert result["accepted_physical_requests"] == result["provider_submit_calls"] == 0
    assert all(count == 0 for count in result["counts"].values())
    assert set(result["phases"]) == {
        "configuration",
        "adapter",
        "preparation",
        "session_initialization",
        "session_close",
    }
    assert result["whole_traced_peak_bytes"] >= max(
        phase["peak_absolute_bytes"] for phase in result["phases"].values()
    )
    assert all(
        phase["baseline_live_bytes"] >= 0 and phase["retained_after_gc_bytes"] >= 0
        for phase in result["phases"].values()
    )
    assert all(value["native_peak_bytes"] > 0 for value in result["baselines"].values())
    assert {
        path.name: path.read_bytes() for path in configuration.parent.iterdir()
    } == before


def test_focused_execution_cli_uses_tiny_test_only_prefixes(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    tmp_path = tmp_path_factory.mktemp("f")
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "focused"
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "benchmarks/focused_execution.py"),
            "--source-root",
            str(root),
            "--output",
            str(output),
            "--small-responses",
            "2",
            "--large-responses",
            "3",
            "--completed-requests",
            "2",
        ],
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "passed" and report["qualified"] is False
    assert report["qualification_scope"] == "test-only"
    assert len(report["cases"]) == 4
    assert all(case["completed_physical_requests"] == 2 for case in report["cases"])
    assert all(not item["eligible"] for item in report["growth"])
    initialization_pids: set[int] = set()
    for case in report["cases"]:
        initialized = json.loads(Path(case["initialization_report_path"]).read_text())
        initialization_pids.add(initialized["process_id"])
        assert initialized["input_sha256"]
        assert all(count == 0 for count in initialized["counts"].values())
        prefix = json.loads(Path(case["prefix_report_path"]).read_text())
        assert (
            prefix["completed_physical_requests"] == 2 and prefix["result_rows"] == 10
        )
        assert prefix["verification_complete"] is True
    assert len(initialization_pids) == 4 and os.getpid() not in initialization_pids
