"""Tiny tool checks; full diagnostics remain separately invoked qualification."""

from __future__ import annotations

import importlib
import json
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
