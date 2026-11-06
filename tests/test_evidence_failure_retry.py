"""Submission-level usage persists through failed, retried and uncertain calls."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest
from durable_evidence_fixture import EvidenceFixtureAdapter, specification
from test_durable_evidence_lifecycle import use_adapter

from judgmentllm import list_runs, resume_run, run_live
from judgmentllm.evidence import ReportedUsageInput
from judgmentllm.persistence.evidence import iter_evidence
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.providers import (
    MockScenario,
    PreparedRequest,
    ProviderAcknowledgment,
    TransientNotSubmitted,
    TransportNotSubmitted,
)


class FailureFixtureAdapter(EvidenceFixtureAdapter):
    """Explicit reported failure usage, independently of provider acceptance."""

    report_failure_usage = True

    async def submit(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderAcknowledgment:
        try:
            return await super().submit(
                prepared, submission_id=submission_id, correlation_id=correlation_id
            )
        except TransportNotSubmitted as failure:
            usage = (
                ReportedUsageInput(
                    usage_source="synthetic.reported_usage", input_tokens=3
                )
                if self.report_failure_usage
                else None
            )
            if isinstance(failure, TransientNotSubmitted):
                raise TransientNotSubmitted(
                    retry_after=failure.retry_after, usage=usage
                ) from None
            raise TransportNotSubmitted(usage=usage) from None


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("reported", [True, False])
def test_definitive_failure_has_usage_without_an_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: Literal["full", "minimal"],
    reported: bool,
) -> None:
    source = specification(tmp_path / "inputs", capture=capture)
    store = tmp_path / "runtime"
    adapter = FailureFixtureAdapter(
        store / "mock-service.sqlite3", scenario=MockScenario(transport="not_submitted")
    )
    adapter.report_failure_usage = reported
    use_adapter(monkeypatch, adapter)
    report = run_live(source, store=store)
    assert report.completed_tasks == 0
    with ReadOnlyRepository(store) as repository:
        evidence = list(iter_evidence(repository, report.run_id))
        assert len(evidence) == 1
        assert evidence[0].usage.input_tokens == (3 if reported else None)
        assert evidence[0].usage.total_tokens is None
        assert evidence[0].usage.reported_cost is None
        assert evidence[0].usage.observation_kind == "invocation"
        assert evidence[0].identity.observation_id is None
        assert evidence[0].response_receipt is None
        submission = next(repository.iter_submissions(report.run_id))
        assert submission.state == "confirmed_not_submitted"
        assert (
            repository.get_observation_for_submission(
                report.run_id, submission.submission_id
            )
            is None
        )
    resume_run(report.run_id, store=store)
    assert adapter.submit_calls == 1 and adapter.ledger.acceptance_count() == 0
    with ReadOnlyRepository(store) as repository:
        assert list(iter_evidence(repository, report.run_id)) == evidence


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_physical_transient_retry_preserves_separate_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: Literal["full", "minimal"],
) -> None:
    source = specification(tmp_path / "inputs", capture=capture)
    store = tmp_path / "runtime"
    adapter = FailureFixtureAdapter(
        store / "mock-service.sqlite3",
        scenario=MockScenario(transient_failures=1, retry_after=0.0),
    )
    use_adapter(monkeypatch, adapter)
    report = run_live(source, store=store)
    assert report.completed_tasks == 2
    with ReadOnlyRepository(store) as repository:
        evidence = list(iter_evidence(repository, report.run_id))
        assert len(evidence) == 2
        assert len({e.identity.submission_id for e in evidence}) == 2
        assert sorted(
            value for e in evidence if (value := e.usage.input_tokens) is not None
        ) == [3, 11]
        assert sorted(e.usage.observation_kind for e in evidence) == [
            "invocation",
            "response",
        ]
        assert sum(e.identity.observation_id is not None for e in evidence) == 1
        assert len(list(repository.iter_results(report.run_id))) == 4
    resume_run(report.run_id, store=store)
    assert adapter.submit_calls == 2 and adapter.ledger.acceptance_count() == 1
    with ReadOnlyRepository(store) as repository:
        assert list(iter_evidence(repository, report.run_id)) == evidence


@pytest.mark.parametrize("accepted", [False, True])
def test_uncertain_acceptance_never_fabricates_usage_or_authorizes_a_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    accepted: bool,
) -> None:
    source = specification(tmp_path / "inputs")
    store = tmp_path / "runtime"
    adapter = EvidenceFixtureAdapter(
        store / "mock-service.sqlite3",
        scenario=MockScenario(
            transport="timeout_after_acceptance"
            if accepted
            else "timeout_before_acceptance"
        ),
    )
    adapter.reconciliation_available = False
    use_adapter(monkeypatch, adapter)
    report = run_live(source, store=store)
    assert report.completed_tasks == 0
    with ReadOnlyRepository(store) as repository:
        assert list(iter_evidence(repository, report.run_id)) == []
    resume_run(report.run_id, store=store)
    assert adapter.submit_calls == 1
    assert adapter.ledger.acceptance_count() == int(accepted)
    adapter.reconciliation_available = True
    recovered = resume_run(report.run_id, store=store)
    with ReadOnlyRepository(store) as repository:
        evidence = list(iter_evidence(repository, report.run_id))
        assert len(evidence) == int(accepted)
        assert recovered.completed_tasks == (2 if accepted else 0)
    assert adapter.submit_calls == 1


_PROCESS = r"""
import os, sys
from pathlib import Path
import judgmentllm.lifecycle as lifecycle
from judgmentllm import run_live, resume_run, list_runs
from judgmentllm.providers import MockScenario
from test_evidence_failure_retry import FailureFixtureAdapter
source, runtime, stage = sys.argv[1:]
store = Path(runtime)
adapter = FailureFixtureAdapter(store / "mock-service.sqlite3", scenario=MockScenario(transport="not_submitted"))
lifecycle._mock_adapter = lambda *args, **kwargs: adapter
original = lifecycle.execute_run
def fault(current, uid):
    if current == stage:
        os._exit(79)
async def execute(*args, **kwargs):
    return await original(*args, **kwargs, fault=fault)
lifecycle.execute_run = execute
if stage:
    run_live(source, store=store)
else:
    resume_run(list_runs(store=store).runs[0].run_id, store=store)
"""


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_failure_receipt_process_death_restores_original_reported_usage(
    tmp_path: Path,
    capture: Literal["full", "minimal"],
) -> None:
    source = specification(tmp_path / "inputs", capture=capture)
    store = tmp_path / "runtime"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(Path(__file__).parent), str(Path(__file__).parents[1] / "src"))
    )
    for stage, status in (("after_failure_evidence_receipt", 79), ("", 0)):
        result = subprocess.run(
            [sys.executable, "-c", _PROCESS, str(source), str(store), stage],
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
        assert result.returncode == status, result.stderr
        assert not result.stdout and not result.stderr
    run_id = list_runs(store=store).runs[0].run_id
    with ReadOnlyRepository(store) as repository:
        evidence = list(iter_evidence(repository, run_id))
        assert len(evidence) == 1
        assert evidence[0].usage.input_tokens == 3
        assert evidence[0].identity.observation_id is None
        assert len(list(repository.iter_submissions(run_id))) == 1
        assert len(list(repository.iter_results(run_id))) == 2
    assert (
        FailureFixtureAdapter(store / "mock-service.sqlite3").ledger.acceptance_count()
        == 0
    )
