"""Automatic retries require transient authoritative nonacceptance evidence."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_execution import interrupt, setup

from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.retry import RetryPolicy
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
from judgmentllm.providers import MockAdapter, MockScenario

IMMEDIATE = RetryPolicy(initial_backoff=0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [1, 2, 3, 10])
async def test_transient_nonacceptance_and_three_submission_budget(
    tmp_path: Path, failures: int
) -> None:
    spec, store, ledger = setup(tmp_path)
    adapter = MockAdapter(ledger, scenario=MockScenario(transient_failures=failures))
    report = await execute_run(
        "001", store=store, adapter=adapter, spec=spec, retry_policy=IMMEDIATE
    )
    expected_attempts = min(failures + 1, 3)
    assert adapter.submit_calls == expected_attempts == report.summary.attempts
    assert report.summary.results == expected_attempts
    assert adapter.ledger.acceptance_count() == (1 if failures < 3 else 0)
    with Repository(store) as repository:
        attempts = list(repository.iter_attempts("001"))
        assert len({a.attempt_id for a in attempts}) == expected_attempts
        # The same exact body may legitimately be used for a new physical
        # attempt; distinct attempt/submission identities establish execution.
        assert len({a.payload_sha256 for a in attempts}) == 1
        results = list(repository.iter_results("001"))
        assert all(
            r.error_kind == "transport_not_submitted" for r in results[:failures]
        )
        if failures < 3:
            assert results[-1].status == "complete"
            assert report.summary.state == "completed"
        else:
            assert not report.summary.unresolved_submissions
            assert report.summary.state == "partial"
        assert (
            repository.connection.execute(
                "SELECT COUNT(*) FROM retry_operations"
            ).fetchone()[0]
            == expected_attempts - 1
        )
    resumed = await recover_run(
        "001", store=store, adapter=adapter, spec=spec, retry_policy=IMMEDIATE
    )
    assert (
        adapter.submit_calls == expected_attempts
        and resumed.summary.results == expected_attempts
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "before_retry_preparation",
        "after_retry_preparation_artifacts",
        "during_retry_authorization",
        "during_retry_preparation",
        "after_retry_preparation",
    ],
)
async def test_automatic_authorization_and_preparation_are_recoverable(
    tmp_path: Path, stage: str
) -> None:
    spec, store, ledger = setup(tmp_path)
    scenario = MockScenario(transient_failures=1, retry_after=0.025)
    adapter = MockAdapter(ledger, scenario=scenario)
    with pytest.raises((RuntimeError, ExceptionGroup)):
        await execute_run(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            retry_policy=IMMEDIATE,
            fault=interrupt(stage),
        )
    with Repository(store) as repository:
        initial = next(repository.iter_attempts("001"))
        eligible = initial.next_eligible_at
        assert (
            eligible is not None and datetime.fromisoformat(eligible).tzinfo is not None
        )
        assert repository.summary("001").results == 1
        assert (
            repository.connection.execute(
                "SELECT COUNT(*) FROM retry_operations"
            ).fetchone()[0]
            == 1
        )
    fresh = MockAdapter(ledger, scenario=scenario)
    resumed = await recover_run(
        "001", store=store, adapter=fresh, spec=spec, retry_policy=IMMEDIATE
    )
    assert resumed.summary.attempts == 2 and resumed.summary.results == 2
    assert fresh.submit_calls == fresh.ledger.acceptance_count() == 1
    with Repository(store) as repository:
        attempts = list(repository.iter_attempts("001"))
        assert attempts[0].next_eligible_at == attempts[1].next_eligible_at == eligible


@pytest.mark.asyncio
async def test_backoff_does_not_hold_worker_slot_or_sqlite_write_transaction(
    tmp_path: Path,
) -> None:
    spec, store, ledger = setup(tmp_path, responses=2)
    # Concurrency one is deliberate: the second original request must finish
    # before the first request's retry interval has expired.
    from judgmentllm.preparation import open_prepared_run

    with open_prepared_run("001", store=store, spec=spec) as prepared:
        first = next(prepared.iter_request_dependencies()).request.request_uid
    adapter = MockAdapter(
        ledger, scenarios={first: MockScenario(transient_failures=1, retry_after=30.0)}
    )
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()

    def observe(stage: str, uid: str) -> None:
        if stage == "after_result" and uid != first:
            loop.call_soon_threadsafe(finished.set)

    # Bind Event's loop before a callback runs on the dedicated I/O thread.
    waiter = asyncio.create_task(finished.wait())
    execution = asyncio.create_task(
        execute_run(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            retry_policy=IMMEDIATE,
            fault=observe,
        )
    )
    await asyncio.wait_for(waiter, timeout=10)
    assert not execution.done()
    with Repository(store) as repository:
        # A write on a different connection succeeds during durable backoff.
        with repository.transaction():
            assert repository.summary("001").results == 2
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=2)
    assert adapter.submit_calls == 2 and adapter.ledger.acceptance_count() == 1


@pytest.mark.asyncio
async def test_process_cancellation_during_backoff_retains_budget_and_eligibility(
    tmp_path: Path,
) -> None:
    spec, store, ledger = setup(tmp_path)
    scenario = MockScenario(transient_failures=1, retry_after=30.0)
    adapter = MockAdapter(ledger, scenario=scenario)
    prepared = asyncio.Event()
    loop = asyncio.get_running_loop()

    def observe(stage: str, _: str) -> None:
        if stage == "after_retry_preparation":
            loop.call_soon_threadsafe(prepared.set)

    execution = asyncio.create_task(
        execute_run(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            retry_policy=IMMEDIATE,
            fault=observe,
        )
    )
    await asyncio.wait_for(prepared.wait(), timeout=10)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=2)
    with Repository(store) as repository, ExecutorOwnership(repository, "001"):
        attempts = list(repository.iter_attempts("001"))
        assert (
            len(attempts) == 2
            and attempts[0].next_eligible_at == attempts[1].next_eligible_at
        )
        assert attempts[0].next_eligible_at is not None
        assert repository.get_run("001").state == "cancelled"
    assert adapter.submit_calls == 1 and adapter.ledger.acceptance_count() == 0


@pytest.mark.asyncio
async def test_process_death_during_backoff_recovers_same_eligibility_and_attempt(
    tmp_path: Path,
) -> None:
    spec, store, ledger = setup(tmp_path, capture="full")
    program = """
import asyncio
import os
import sys
from judgmentllm.execution import execute_run
from judgmentllm.execution.retry import RetryPolicy
from judgmentllm.providers import MockAdapter, MockScenario
def die(stage, request):
    if stage == 'after_retry_preparation':
        os._exit(73)
asyncio.run(execute_run('001',store=sys.argv[1],
    adapter=MockAdapter(sys.argv[2],scenario=MockScenario(transient_failures=1,retry_after=0.5)),
    retry_policy=RetryPolicy(initial_backoff=0.0),fault=die))
"""
    child = subprocess.run(
        [sys.executable, "-c", program, str(store), str(ledger)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == 73, child.stderr
    assert not child.stdout and not child.stderr
    with Repository(store) as repository:
        initial, retry = list(repository.iter_attempts("001"))
        assert initial.next_eligible_at == retry.next_eligible_at
        assert retry.next_eligible_at is not None
        eligible = datetime.fromisoformat(retry.next_eligible_at)
    boundaries: list[datetime] = []

    def observe(stage: str, _: str) -> None:
        if stage == "after_submitting":
            boundaries.append(datetime.now(UTC))

    adapter = MockAdapter(
        ledger, scenario=MockScenario(transient_failures=1, retry_after=0.5)
    )
    report = await recover_run(
        "001",
        store=store,
        adapter=adapter,
        spec=spec,
        retry_policy=IMMEDIATE,
        fault=observe,
    )
    assert report.summary.attempts == report.summary.results == 2
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1
    assert len(boundaries) == 1 and boundaries[0] >= eligible
    with Repository(store) as repository:
        assert list(repository.iter_attempts("001"))[1].attempt_id == retry.attempt_id
        assert (
            list(repository.iter_attempts("001"))[1].next_eligible_at
            == retry.next_eligible_at
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        MockScenario(transport="timeout_before_acceptance"),
        MockScenario(transport="timeout_after_acceptance"),
        MockScenario(outcome="invalid"),
        MockScenario(outcome="refusal"),
        MockScenario(outcome="provider_error"),
        MockScenario(transport="not_submitted"),
    ],
)
async def test_uncertain_accepted_and_nontransient_outcomes_are_never_automatically_retried(
    tmp_path: Path, scenario: MockScenario
) -> None:
    spec, store, ledger = setup(tmp_path)
    adapter = MockAdapter(ledger, scenario=scenario)
    report = await execute_run(
        "001", store=store, adapter=adapter, spec=spec, retry_policy=IMMEDIATE
    )
    assert adapter.submit_calls == report.summary.attempts == 1
    with Repository(store) as repository:
        assert (
            repository.connection.execute(
                "SELECT COUNT(*) FROM retry_operations"
            ).fetchone()[0]
            == 0
        )
        assert next(repository.iter_attempts("001")).next_eligible_at is None
