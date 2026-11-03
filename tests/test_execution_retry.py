"""Frozen retry selections, physical identities, crash boundaries and backoff."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from test_preparation_rendering import specification

from judgmentllm.config import RunSpec
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.retry import (
    RetryPolicy,
    authorize_automatic_retry,
    authorize_subset_retry,
    automatic_retry_id,
    subset_dependency,
)
from judgmentllm.persistence.models import IntegrityError, OwnershipError
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.providers import MockAdapter, MockMemberScenario, MockScenario


def grouped_fixture(
    tmp_path: Path, *, capture: Literal["full", "minimal"] = "minimal"
) -> tuple[RunSpec, Path, Path]:
    spec = specification(tmp_path / "inputs", responses=1, traits=3)
    spec.provider, spec.endpoint = "mock", "synthetic"
    spec.capture = capture
    spec.execution.grouping = "grouped"
    spec.execution.concurrency = 1
    spec = RunSpec.model_validate(spec.model_dump())
    store, ledger = tmp_path / "store", tmp_path / "ledger.sqlite3"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    return spec, store, ledger


async def partial_group(
    tmp_path: Path, *, capture: Literal["full", "minimal"] = "minimal"
) -> tuple[RunSpec, Path, Path, tuple[str, ...]]:
    spec, store, ledger = grouped_fixture(tmp_path, capture=capture)
    adapter = MockAdapter(
        ledger,
        scenario=MockScenario(
            members={"002": MockMemberScenario(outcome="invalid_category")}
        ),
    )
    await execute_run("001", store=store, adapter=adapter, spec=spec)
    with Repository(store) as repository:
        request = next(repository.iter_requests("001"))
        selected = repository.request_task_ids("001", request.request_uid)
        assert [r.status for r in repository.iter_results("001")] == [
            "complete",
            "complete",
            "invalid",
        ]
    return spec, store, ledger, selected


@pytest.mark.asyncio
async def test_only_failed_member_is_submitted_and_parent_history_is_immutable(
    tmp_path: Path,
) -> None:
    spec, store, ledger, tasks = await partial_group(tmp_path)
    adapter = MockAdapter(ledger)
    attempt = authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        spec=spec,
        task_ids=(tasks[2],),
        operation_id="retry-001",
    )
    assert adapter.submit_calls == 0 and attempt.attempt_number == 2
    with Repository(store) as repository:
        original = next(repository.iter_attempts("001"))
        assert attempt.request_uid == original.request_uid
        assert attempt.authorization_id == "retry-001"
        assert attempt.payload_sha256 != original.payload_sha256
        assert attempt.prompt_sha256 != original.prompt_sha256
        assert repository.attempt_task_ids("001", attempt.attempt_id) == (tasks[2],)
        assert repository.request_task_ids("001", attempt.request_uid) == tasks
    report = await recover_run("001", store=store, adapter=adapter, spec=spec)
    assert report.summary.results == 4 and report.summary.attempts == 2
    assert adapter.submit_calls == 1 and adapter.ledger.acceptance_count() == 2
    resumed = authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        spec=spec,
        task_ids=(tasks[2],),
        operation_id="retry-001",
    )
    assert resumed.model_dump(exclude={"state"}) == attempt.model_dump(
        exclude={"state"}
    )
    assert resumed.state == "normalized"
    await recover_run("001", store=store, adapter=adapter, spec=spec)
    assert adapter.submit_calls == 1
    with Repository(store) as repository:
        outcomes = list(repository.iter_results("001"))
        assert outcomes[2].status == "invalid" and outcomes[3].status == "complete"
        assert outcomes[2].task_id == outcomes[3].task_id == tasks[2]


@pytest.mark.asyncio
async def test_retry_id_conflicts_and_completed_permission(tmp_path: Path) -> None:
    spec, store, ledger, tasks = await partial_group(tmp_path)
    adapter = MockAdapter(ledger)
    for selected in [(tasks[0],), (tasks[0], tasks[2])]:
        with pytest.raises(IntegrityError, match="completed"):
            authorize_subset_retry(
                "001",
                store=store,
                adapter=adapter,
                spec=spec,
                task_ids=selected,
                operation_id="unapproved",
            )
    attempt = authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        spec=spec,
        task_ids=(tasks[2],),
        operation_id="retry-001",
    )
    for selected, allowed, policy in [
        ((tasks[0],), True, RetryPolicy()),
        ((tasks[2],), True, RetryPolicy()),
        ((tasks[2],), False, RetryPolicy(jitter="full")),
    ]:
        with pytest.raises(IntegrityError):
            authorize_subset_retry(
                "001",
                store=store,
                adapter=adapter,
                spec=spec,
                operation_id="retry-001",
                task_ids=selected,
                allow_completed=allowed,
                policy=policy,
            )
    await recover_run("001", store=store, adapter=adapter, spec=spec)
    allowed = authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        spec=spec,
        task_ids=(tasks[0],),
        operation_id="completed-001",
        allow_completed=True,
    )
    assert allowed.attempt_number == attempt.attempt_number + 1
    await recover_run("001", store=store, adapter=adapter, spec=spec)
    with pytest.raises(IntegrityError, match="budget"):
        authorize_subset_retry(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            task_ids=(tasks[2],),
            operation_id="exhausted",
            allow_completed=True,
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
async def test_interrupted_retry_preparation_resumes_one_operation(
    tmp_path: Path, stage: str
) -> None:
    spec, store, ledger, tasks = await partial_group(tmp_path)
    adapter = MockAdapter(ledger)

    def interrupt(boundary: str, _: str) -> None:
        if stage == boundary:
            raise RuntimeError("synthetic retry interruption")

    with pytest.raises(RuntimeError, match="synthetic"):
        authorize_subset_retry(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            task_ids=(tasks[2],),
            operation_id="retry-001",
            fault=interrupt,
        )
    with Repository(store) as repository:
        assert repository.summary("001").attempts == (
            2 if stage == "after_retry_preparation" else 1
        )
        assert repository.connection.execute(
            "SELECT COUNT(*) FROM retry_operations"
        ).fetchone()[0] == (1 if stage == "after_retry_preparation" else 0)
    attempt = authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        spec=spec,
        task_ids=(tasks[2],),
        operation_id="retry-001",
    )
    assert attempt.attempt_number == 2
    await recover_run("001", store=store, adapter=adapter, spec=spec)
    assert adapter.ledger.acceptance_count() == 2


@pytest.mark.asyncio
async def test_retry_selection_rejects_duplicates_unknowns_cross_request_and_active_work(
    tmp_path: Path,
) -> None:
    spec, store, ledger, tasks = await partial_group(tmp_path)
    adapter = MockAdapter(ledger)
    for selected in [(), (tasks[2], tasks[2]), ("unknown",)]:
        with pytest.raises(IntegrityError):
            authorize_subset_retry(
                "001",
                store=store,
                adapter=adapter,
                spec=spec,
                task_ids=selected,
                operation_id="bad",
            )
    authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        spec=spec,
        task_ids=(tasks[2],),
        operation_id="retry-001",
    )
    with pytest.raises(IntegrityError, match="unresolved"):
        authorize_subset_retry(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            task_ids=(tasks[2],),
            operation_id="overlap",
        )
    with Repository(store) as repository, ExecutorOwnership(repository, "001"):
        with pytest.raises(OwnershipError):
            authorize_subset_retry(
                "001",
                store=store,
                adapter=adapter,
                spec=spec,
                task_ids=(tasks[2],),
                operation_id="competing",
            )


@pytest.mark.asyncio
async def test_cross_request_retry_selection_and_automatic_evidence_guards(
    tmp_path: Path,
) -> None:
    from test_execution import setup

    spec, store, ledger = setup(tmp_path, responses=2)
    adapter = MockAdapter(ledger)
    await execute_run("001", store=store, adapter=adapter, spec=spec)
    with Repository(store) as repository:
        tasks = tuple(r.task_id for r in repository.iter_results("001"))
    with pytest.raises(IntegrityError, match="one original request"):
        authorize_subset_retry(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            task_ids=tasks,
            operation_id="cross-request",
            allow_completed=True,
        )
    with Repository(store) as repository, ExecutorOwnership(repository, "001") as owner:
        assert owner.owner is not None
        attempt = next(repository.iter_attempts("001"))
        with pytest.raises(IntegrityError, match="definitive"):
            authorize_automatic_retry(
                repository, attempt, owner_token=owner.owner.token
            )


def test_subset_dependency_preserves_original_order_and_identity(
    tmp_path: Path,
) -> None:
    spec, store, _ = grouped_fixture(tmp_path)
    with open_prepared_run("001", store=store, spec=spec) as prepared:
        parent = next(prepared.iter_request_dependencies())
        tasks = tuple(t.task_id for t in parent.request.tasks)
        subset = subset_dependency(parent, (tasks[2], tasks[0]))
        assert subset.request.request_uid == parent.request.request_uid
        assert subset.dependency_sha256 == parent.dependency_sha256
        assert tuple(t.task_id for t in subset.request.tasks) == (tasks[0], tasks[2])
        for invalid in [(), (tasks[0], tasks[0]), ("unknown",)]:
            with pytest.raises(IntegrityError):
                subset_dependency(parent, invalid)


def test_default_backoff_retry_after_jitter_and_validation() -> None:
    policy = RetryPolicy()
    assert [policy.delay(i) for i in range(1, 8)] == [1, 2, 4, 8, 16, 30, 30]
    assert policy.delay(1, retry_after=12) == 12
    assert RetryPolicy(jitter="full").delay(3, jitter_fraction=0.25) == 1
    assert RetryPolicy(jitter="full").delay(1, retry_after=4, jitter_fraction=0) == 4
    assert (
        policy.eligible_at(2, now=datetime(2026, 1, 1, tzinfo=UTC))
        == "2026-01-01T00:00:02+00:00"
    )
    for attempt in [0, -1]:
        with pytest.raises(ValueError):
            policy.delay(attempt)
    for retry_after in [-1.0, float("inf"), float("nan")]:
        with pytest.raises(ValueError):
            policy.delay(1, retry_after=retry_after)
    with pytest.raises(ValueError):
        policy.delay(1, jitter_fraction=2)
    with pytest.raises(ValueError):
        policy.eligible_at(1, now=datetime(2026, 1, 1))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "during_retry_authorization",
        "during_retry_preparation",
        "after_retry_preparation",
    ],
)
async def test_actual_process_death_cannot_split_retry_authorization_and_preparation(
    tmp_path: Path, stage: str
) -> None:
    spec, store, ledger, tasks = await partial_group(tmp_path, capture="full")
    program = """
import os
import sys
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.providers import MockAdapter
def die(stage, request):
    if stage == sys.argv[4]:
        os._exit(73)
authorize_subset_retry('001',store=sys.argv[1],adapter=MockAdapter(sys.argv[2]),
    task_ids=(sys.argv[3],),operation_id='killed-operation',fault=die)
"""
    child = subprocess.run(
        [sys.executable, "-c", program, str(store), str(ledger), tasks[2], stage],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert child.returncode == 73, child.stderr
    assert not child.stdout and not child.stderr
    adapter = MockAdapter(ledger)
    authorized = authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        spec=spec,
        task_ids=(tasks[2],),
        operation_id="killed-operation",
    )
    assert authorized.attempt_number == 2
    await recover_run("001", store=store, adapter=adapter, spec=spec)
    with Repository(store) as repository:
        assert repository.summary("001").attempts == 2
        assert repository.summary("001").results == 4
        assert (
            repository.connection.execute(
                "SELECT COUNT(*) FROM retry_operations"
            ).fetchone()[0]
            == 1
        )
    assert adapter.ledger.acceptance_count() == 2


@pytest.mark.asyncio
async def test_automatic_authorization_requires_definitive_evidence_and_retains_eligibility(
    tmp_path: Path,
) -> None:
    spec, store, ledger = grouped_fixture(tmp_path)
    adapter = MockAdapter(
        ledger, scenario=MockScenario(transport="transient_not_submitted")
    )
    await execute_run(
        "001",
        store=store,
        adapter=adapter,
        spec=spec,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    with Repository(store) as repository, ExecutorOwnership(repository, "001") as owner:
        assert owner.owner is not None
        prior = next(repository.iter_attempts("001"))
        operation = authorize_automatic_retry(
            repository,
            prior,
            owner_token=owner.owner.token,
            now=datetime(2026, 1, 1, tzinfo=UTC),
            retry_after=9,
        )
        assert operation == automatic_retry_id(prior)
        authorized = repository.get_attempt("001", prior.attempt_id)
        assert authorized.next_eligible_at == "2026-01-01T00:00:09+00:00"
        assert (
            authorize_automatic_retry(
                repository,
                prior,
                owner_token=owner.owner.token,
                now=datetime(2027, 1, 1, tzinfo=UTC),
            )
            == operation
        )
        assert (
            repository.get_attempt("001", prior.attempt_id).next_eligible_at
            == authorized.next_eligible_at
        )
        assert (
            authorize_automatic_retry(
                repository,
                prior,
                owner_token=owner.owner.token,
                policy=RetryPolicy(max_attempts=1),
            )
            is None
        )
        selected = repository.attempt_task_ids("001", prior.attempt_id)
    with pytest.raises(IntegrityError, match="reserves"):
        authorize_subset_retry(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            task_ids=selected,
            operation_id="conflicting-reservation",
        )
