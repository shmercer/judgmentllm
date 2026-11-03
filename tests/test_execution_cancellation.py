"""Cancellation drains local writes while retaining original remote certainty."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from test_execution import setup
from test_execution_retry import partial_group

from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.cancellation import cancel_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.persistence.models import OwnershipError
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
from judgmentllm.providers import MockAdapter


@pytest.mark.asyncio
async def test_cancel_before_preparation_creates_no_provider_outcome(
    tmp_path: Path,
) -> None:
    spec, store, ledger = setup(tmp_path)
    assert cancel_run("001", store=store).state == "cancelled"
    adapter = MockAdapter(ledger)
    report = await recover_run("001", store=store, spec=spec, adapter=adapter)
    assert report.summary.state == "cancelled" and report.summary.results == 0
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 0
    assert cancel_run("001", store=store) == report.summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "after_preparation",
        "after_submitting",
        "after_submit",
        "after_acknowledgment",
        "during_result_transaction",
        "after_result",
    ],
)
async def test_cancel_during_durable_boundary_drains_and_recovers(
    tmp_path: Path, stage: str
) -> None:
    spec, store, ledger = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def fault(current: str, _: str) -> None:
        if current == stage:
            entered.set()
            assert release.wait(10)

    adapter = MockAdapter(ledger)
    task = asyncio.create_task(
        execute_run("001", store=store, spec=spec, adapter=adapter, fault=fault)
    )
    assert await asyncio.to_thread(entered.wait, 10)
    with pytest.raises(OwnershipError):
        await asyncio.to_thread(cancel_run, "001", store=store)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with Repository(store) as repository:
        assert repository.get_owner("001") is None
        summary = repository.summary("001")
        assert summary.state in {"cancelled", "completed"}
        committed = tuple(repository.iter_results("001"))
        assert all(r.status == "complete" for r in committed)
        submission = next(repository.iter_submissions("001"))
        if stage == "after_preparation":
            assert submission.certainty == "not_started" and not committed
        if stage in {"after_submit", "after_acknowledgment"}:
            assert submission.certainty in {"unknown", "accepted"}
    recovered = await recover_run("001", store=store, spec=spec, adapter=adapter)
    assert adapter.ledger.acceptance_count() <= 1
    assert recovered.summary.state in {"cancelled", "completed"}
    with Repository(store) as repository:
        assert tuple(repository.iter_results("001"))[: len(committed)] == committed
        assert all(r.status != "cancelled" for r in repository.iter_results("001"))
        with ExecutorOwnership(repository, "001"):
            pass


@pytest.mark.asyncio
async def test_cancel_after_partial_group_preserves_successes_and_history(
    tmp_path: Path,
) -> None:
    spec, store, ledger, _ = await partial_group(tmp_path)
    with Repository(store) as repository:
        original = tuple(repository.iter_results("001"))
    summary = cancel_run("001", store=store)
    assert summary.state == "cancelled" and summary.results == 3
    adapter = MockAdapter(ledger)
    report = await recover_run("001", store=store, adapter=adapter, spec=spec)
    assert report.summary.state == "cancelled"
    with Repository(store) as repository:
        assert tuple(repository.iter_results("001")) == original
    assert adapter.submit_calls == 0 and adapter.ledger.acceptance_count() == 1


@pytest.mark.asyncio
async def test_cancel_of_completed_run_preserves_completion(tmp_path: Path) -> None:
    spec, store, ledger = setup(tmp_path)
    report = await execute_run(
        "001", store=store, adapter=MockAdapter(ledger), spec=spec
    )
    assert cancel_run("001", store=store) == report.summary


@pytest.mark.asyncio
async def test_completed_task_retry_can_be_cancelled_before_submission(
    tmp_path: Path,
) -> None:
    spec, store, ledger = setup(tmp_path)
    adapter = MockAdapter(ledger)
    await execute_run("001", store=store, adapter=adapter, spec=spec)
    with Repository(store) as repository:
        original = tuple(repository.iter_results("001"))
    retry = authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        spec=spec,
        task_ids=(original[0].task_id,),
        operation_id="explicit-completed-retry",
        allow_completed=True,
    )
    with Repository(store) as repository:
        assert repository.summary("001").state == "partial"
    cancelled = cancel_run("001", store=store)
    assert cancelled.state == "cancelled"
    with Repository(store) as repository:
        assert repository.get_attempt("001", retry.attempt_id).state == "cancelled"
        assert tuple(repository.iter_results("001")) == original
    report = await recover_run("001", store=store, adapter=adapter, spec=spec)
    assert report.summary.state == "cancelled"
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1
