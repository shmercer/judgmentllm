"""Public entrypoints retain the existing source, evidence and capture boundaries."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Literal

import pytest
from test_preparation_rendering import specification

from judgmentllm import (
    LifecycleError,
    MockExecutionOptions,
    RetrySelection,
    aresume_run,
    arun_live,
    export_run_results,
    get_run,
    resume_run,
    run_live,
)
from judgmentllm import lifecycle as services
from judgmentllm.config import RunSpec
from judgmentllm.io import read_results
from judgmentllm.results import RubricResult


def mock_spec(directory: Path, capture: Literal["full", "minimal"]) -> RunSpec:
    spec = specification(directory, responses=2, traits=3)
    spec.provider, spec.endpoint = "mock", "synthetic"
    spec.capture = capture
    spec.execution.grouping = "grouped"
    return spec


def acceptances(store: Path) -> int:
    # Independent service evidence, not executor/result summaries.
    with closing(sqlite3.connect(store / "mock-service.sqlite3")) as connection:
        return int(
            connection.execute(
                "SELECT COALESCE(SUM(acceptance_count),0) FROM acceptances"
            ).fetchone()[0]
        )


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_public_source_change_blocks_resume_but_not_status_or_export(
    tmp_path: Path, capture: Literal["full", "minimal"]
) -> None:
    spec = mock_spec(tmp_path / "inputs", capture)
    store = tmp_path / "store"
    summary = run_live(spec, store=store)
    original = export_run_results(summary.run_id, store=store)
    original_bytes = Path(original.files[0].path).read_bytes()
    spec.input.path.write_text('{"item_id":"000","text":"CHANGED SOURCE"}\n')
    assert get_run(summary.run_id, store=store).completed_tasks == 6
    assert export_run_results(summary.run_id, store=store) == original
    with pytest.raises(LifecycleError):
        resume_run(summary.run_id, store=store, spec=spec)
    if capture == "full":
        resumed = resume_run(summary.run_id, store=store, use_frozen_snapshot=True)
        assert resumed.outcome == "ok"
    else:
        with pytest.raises(LifecycleError):
            resume_run(summary.run_id, store=store, spec=spec, use_frozen_snapshot=True)
    assert acceptances(store) == 2
    assert Path(original.files[0].path).read_bytes() == original_bytes


def test_public_minimal_python_origin_requires_original_spec(tmp_path: Path) -> None:
    spec = mock_spec(tmp_path / "inputs", "minimal")
    store = tmp_path / "store"
    summary = run_live(spec, store=store)
    with pytest.raises(LifecycleError):
        resume_run(summary.run_id, store=store)
    assert resume_run(summary.run_id, store=store, spec=spec).outcome == "ok"
    assert acceptances(store) == 2


def test_public_minimal_recovery_requires_authoritative_original_observation(
    tmp_path: Path,
) -> None:
    spec = mock_spec(tmp_path / "inputs", "minimal")
    store = tmp_path / "store"
    unavailable = MockExecutionOptions(retrieval_available=False)
    summary = run_live(spec, store=store, mock=unavailable)
    assert summary.outcome == "partial"
    assert summary.results == 0 and summary.unresolved_submissions == 2
    assert acceptances(store) == 2
    blocked = resume_run(summary.run_id, store=store, spec=spec, mock=unavailable)
    assert blocked.results == 0 and blocked.recovery_required
    assert acceptances(store) == 2
    recovered = resume_run(summary.run_id, store=store, spec=spec)
    assert recovered.completed_tasks == 6 and recovered.results == 6
    assert acceptances(store) == 2
    exported = export_run_results(summary.run_id, store=store)
    rows = list(read_results(exported.files[0].path))
    assert len(rows) == 6 and {
        row.item_id for row in rows if isinstance(row, RubricResult)
    } == {"000", "001"}


def test_public_run_prepares_once_without_validation_or_estimation_scans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = mock_spec(tmp_path / "inputs", "minimal")
    original = services.prepare_run
    preparations: list[str] = []

    def prepare(*args: object, **kwargs: object) -> object:
        preparations.append("prepare")
        return original(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("run_live repeated a full validation or estimation pipeline")

    monkeypatch.setattr(services, "prepare_run", prepare)
    monkeypatch.setattr(services, "validate_inputs", forbidden)
    monkeypatch.setattr(services, "estimate_run", forbidden)
    summary = run_live(spec, store=tmp_path / "store")
    assert summary.completed_tasks == 6 and preparations == ["prepare"]


def test_public_export_old_snapshot_paths_describe_selected_bytes(
    tmp_path: Path,
) -> None:
    spec = mock_spec(tmp_path / "inputs", "minimal")
    store = tmp_path / "store"
    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps({"scenario": {"members": {"001": {"outcome": "invalid_category"}}}})
    )
    summary = run_live(
        spec, store=store, mock=MockExecutionOptions(fixture_path=fixture)
    )
    before = export_run_results(summary.run_id, store=store)
    failed = next(
        row for row in read_results(before.files[0].path) if row.status != "complete"
    )
    resume_run(
        summary.run_id,
        store=store,
        spec=spec,
        retry=RetrySelection(task_ids=(failed.task_id,), operation_id="retry-one"),
    )
    newer = export_run_results(summary.run_id, store=store)
    assert newer.files[0].path != before.files[0].path
    selected = export_run_results(
        summary.run_id,
        store=store,
        high_water_mark=before.result_high_water_mark,
    )
    assert selected.files == before.files
    assert selected.artifacts.results_jsonl == before.files[0].path
    assert selected.artifacts.export == before.files[0].path


def test_public_status_retains_export_with_hash_only_source_identity(
    tmp_path: Path,
) -> None:
    spec = mock_spec(tmp_path / "inputs", "minimal")
    spec.input.path.write_bytes(b"")
    store = tmp_path / "store"
    summary = run_live(spec, store=store)
    empty = export_run_results(summary.run_id, store=store)
    assert empty.row_count == 0 and Path(empty.files[0].path).read_bytes() == b""
    assert (
        get_run(summary.run_id, store=store).artifacts.results_jsonl
        == empty.files[0].path
    )


@pytest.mark.asyncio
async def test_cancelled_retry_preserves_cancellation_when_drained_writer_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = mock_spec(tmp_path / "inputs", "minimal")
    store = tmp_path / "store"
    summary = await arun_live(spec, store=store)
    result = export_run_results(summary.run_id, store=store)
    selected = next(read_results(result.files[0].path)).task_id
    entered, release = threading.Event(), threading.Event()

    def fail_authorization(*args: object, **kwargs: object) -> None:
        entered.set()
        assert release.wait(10)
        raise RuntimeError("synthetic failure after cancellation")

    monkeypatch.setattr(services, "authorize_subset_retry", fail_authorization)
    task = asyncio.create_task(
        aresume_run(
            summary.run_id,
            store=store,
            spec=spec,
            retry=RetrySelection(
                task_ids=(selected,), operation_id="retry", allow_completed=True
            ),
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert acceptances(store) == 2
