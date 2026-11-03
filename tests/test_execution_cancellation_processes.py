"""Actual process death during cancellation and authoritative cancellation recovery."""

from __future__ import annotations

import json
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

import pytest
from test_execution_processes import _accepted, _command, _fixture, _run  # pyright: ignore[reportPrivateUsage]
from test_preparation_rendering import specification

from judgmentllm.config import RunSpec
from judgmentllm.execution import execute_run
from judgmentllm.execution.cancellation import cancel_run
from judgmentllm.execution.exports import export_run
from judgmentllm.io import read_results
from judgmentllm.persistence.models import OwnershipError
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers import MockAdapter, MockScenario

CANCEL_CHILD = """
import os, sys
from judgmentllm.execution.cancellation import cancel_run
from judgmentllm.persistence.repository import Repository
store, stage = sys.argv[1:]
original_transition = Repository.transition
original_attempts = Repository.iter_attempts
def transition(self, entity, run_id, entity_id, new_state, **kwargs):
    outcome = original_transition(self, entity, run_id, entity_id, new_state, **kwargs)
    if new_state == 'cancelled' and (
        (stage == 'before_run_commit' and entity == 'run') or
        (stage == 'during_attempt_drain' and entity == 'attempt')):
        os._exit(73)
    return outcome
def attempts(self, run_id, **kwargs):
    if stage == 'after_run_commit':
        os._exit(73)
    return original_attempts(self, run_id, **kwargs)
Repository.transition = transition
Repository.iter_attempts = attempts
print(cancel_run('001', store=store).model_dump_json())
"""

CANCELLED_RESPONSE_CHILD = """
import asyncio, os, sys
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.cancellation import cancel_run
from judgmentllm.providers import MockAdapter, MockScenario
store, ledger, stage, unavailable = sys.argv[1:]
adapter = MockAdapter(ledger, scenario=MockScenario(outcome='confirmed_cancellation'),
    retrieval_available=unavailable != 'yes')
def fault(current, request):
    if current == stage:
        os._exit(73)
if not stage:
    cancel_run('001', store=store)
report = asyncio.run((execute_run if stage else recover_run)(
    '001', store=store, adapter=adapter, fault=fault if stage else None))
print(report.model_dump_json())
"""


@pytest.mark.parametrize(
    "stage", ["before_run_commit", "after_run_commit", "during_attempt_drain"]
)
def test_process_death_during_cancellation_resumes_drain(
    tmp_path: Path, stage: str
) -> None:
    fixture = _fixture(tmp_path, "minimal")
    assert _run(fixture, "after_preparation").returncode == 73
    killed = subprocess.run(
        [sys.executable, "-c", CANCEL_CHILD, str(fixture[0]), stage],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert killed.returncode == 73 and not killed.stdout and not killed.stderr
    with Repository(fixture[0]) as repository:
        assert repository.summary("001").state == (
            "running" if stage == "before_run_commit" else "cancelled"
        )
        assert next(repository.iter_attempts("001")).state == "prepared"
        assert not list(repository.iter_results("001"))
    finished = subprocess.run(
        [sys.executable, "-c", CANCEL_CHILD, str(fixture[0]), ""],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert finished.returncode == 0, finished.stderr
    assert json.loads(finished.stdout)["state"] == "cancelled"
    with Repository(fixture[0]) as repository:
        assert next(repository.iter_attempts("001")).state == "cancelled"
        assert next(repository.iter_submissions("001")).certainty == "not_started"
        assert repository.get_owner("001") is None
    recovered = _run(fixture)
    assert (
        recovered.returncode == 0
        and json.loads(recovered.stdout)["summary"]["state"] == "cancelled"
    )
    assert _accepted(fixture[1]) == 0


@pytest.mark.parametrize(
    "stage",
    [
        "after_submitting",
        "after_submit",
        "after_acknowledgment",
        "during_result_transaction",
        "after_result",
    ],
)
def test_cancelled_process_retains_submission_evidence(
    tmp_path: Path, stage: str
) -> None:
    fixture = _fixture(tmp_path, "minimal")
    assert _run(fixture, stage).returncode == 73
    summary = cancel_run("001", store=fixture[0])
    expected = int(stage == "after_result")
    assert summary.results == expected
    unavailable = _run(fixture, mode="unavailable")
    assert unavailable.returncode == 0, unavailable.stderr
    report = json.loads(unavailable.stdout)
    assert report["summary"]["state"] == "cancelled"
    assert report["summary"]["results"] == expected
    recovered = _run(fixture)
    assert recovered.returncode == 0, recovered.stderr
    expected = int(stage != "after_submitting")
    assert json.loads(recovered.stdout)["summary"]["results"] == expected
    assert _accepted(fixture[1]) == expected
    with Repository(fixture[0]) as repository:
        assert repository.get_owner("001") is None
        assert all(
            record.status == "complete" for record in repository.iter_results("001")
        )


@pytest.mark.parametrize("repetition", range(3))
def test_competing_process_cannot_cancel_exclusive_executor(
    tmp_path: Path, repetition: int
) -> None:
    fixture = _fixture(tmp_path, "minimal")
    marker = tmp_path / "executor.pid"
    process = subprocess.Popen(
        _command(fixture, "after_claim", "hold", marker),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    worker_pid: int | None = None
    worker_handle: int | None = None
    worker_exit: int | None = None
    native_wait: int | None = None
    try:
        deadline = time.monotonic() + 15
        while (
            not marker.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert marker.exists(), "executor did not reach the holding boundary"
        worker_pid = int(marker.read_text(encoding="ascii"))
        if sys.platform == "win32":
            import _winapi

            # The venv launcher and lock-owning interpreter may be different
            # processes. Capture the real owner's kernel identity while alive;
            # PID disappearance or reaping only the launcher cannot prove exit.
            worker_handle = _winapi.OpenProcess(
                _winapi.SYNCHRONIZE | 0x0001 | 0x1000, False, worker_pid
            )
            assert _winapi.WaitForSingleObject(worker_handle, 0) == _winapi.WAIT_TIMEOUT
        else:
            assert worker_pid == process.pid
        with pytest.raises(OwnershipError):
            cancel_run("001", store=fixture[0])
    finally:
        try:
            if sys.platform == "win32" and worker_handle is not None:
                import _winapi

                # TerminateProcess is asynchronous. Wait on the exact process
                # object until all its threads/I/O/handles have been released,
                # then let its launcher exit and reap that separate process.
                _winapi.TerminateProcess(worker_handle, int(signal.SIGTERM))
                native_wait = _winapi.WaitForSingleObject(worker_handle, 10_000)
                assert native_wait == _winapi.WAIT_OBJECT_0
                worker_exit = _winapi.GetExitCodeProcess(worker_handle)
                assert worker_exit != _winapi.STILL_ACTIVE
            elif process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)
            assert process.returncode is not None
        finally:
            if sys.platform == "win32" and worker_handle is not None:
                import _winapi

                _winapi.CloseHandle(worker_handle)
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
    print(
        json.dumps(
            {
                "repetition": repetition,
                "platform": sys.platform,
                "launcher_pid": process.pid,
                "worker_pid": worker_pid,
                "worker_native_wait": native_wait,
                "worker_exit": worker_exit,
                "launcher_exit": process.returncode,
                "sqlite_version": sqlite3.sqlite_version,
                "stage": "owner_terminated_and_launcher_reaped",
            }
        )
    )
    assert cancel_run("001", store=fixture[0]).state == "cancelled"
    assert _accepted(fixture[1]) == 0
    with Repository(fixture[0]) as repository:
        integrity = repository.connection.execute("PRAGMA integrity_check").fetchall()
        assert [tuple(row) for row in integrity] == [("ok",)]
        assert (
            repository.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        )
        assert repository.get_owner("001") is None
        assert repository.summary("001").results == 0
        events = tuple(repository.events("001"))
        assert any(event.kind == "owner_recovered" for event in events)
    print(json.dumps({"integrity_check": "ok", "foreign_key_check": "ok"}))


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "stage",
    ["after_submit", "after_receipt", "during_result_transaction", "after_result"],
)
def test_confirmed_grouped_cancellation_has_durable_observation_evidence(
    tmp_path: Path, capture: Literal["full", "minimal"], stage: str
) -> None:
    spec = specification(tmp_path / "inputs", responses=1, traits=3)
    spec.provider, spec.endpoint, spec.capture = "mock", "synthetic", capture
    spec.execution.grouping, spec.execution.concurrency = "grouped", 1
    spec.output_path = (tmp_path / "output").resolve()
    spec = RunSpec.model_validate(spec.model_dump())
    configuration = tmp_path / "run.json"
    configuration.write_text(spec.model_dump_json(), encoding="utf-8")
    store, ledger = tmp_path / "store", tmp_path / "ledger.sqlite3"
    provider = MockAdapter(ledger)
    with prepare_run(
        configuration,
        store=store,
        run_id="001",
        capabilities=provider.capabilities(spec),
    ):
        pass

    def run(boundary: str, unavailable: str = "no") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                CANCELLED_RESPONSE_CHILD,
                str(store),
                str(ledger),
                boundary,
                unavailable,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

    assert run(stage).returncode == 73
    lost = run("", "yes")
    assert lost.returncode == 0, lost.stderr
    if capture == "minimal" and stage != "after_result":
        assert json.loads(lost.stdout)["summary"]["results"] == 0
        assert json.loads(lost.stdout)["recovery_required"]
    recovered = run("")
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["summary"]["results"] == 3
    with Repository(store) as repository:
        records = tuple(repository.iter_results("001"))
        assert len(records) == 3 and all(row.status == "cancelled" for row in records)
        assert all(
            row.error_kind == "confirmed_cancellation" and row.explanation is None
            for row in records
        )
        links = repository.connection.execute(
            "SELECT DISTINCT observation_id FROM results"
        ).fetchall()
        assert len(links) == 1 and links[0][0]
        observed = repository.get_observation_for_submission(
            "001", next(repository.iter_submissions("001")).submission_id
        )
        assert observed is not None and observed.kind == "confirmed_cancellation"
    published = export_run("001", store=store)
    assert len(list(read_results(published.artifact_path))) == 3
    assert _accepted(ledger) == 1


@pytest.mark.asyncio
async def test_confirmed_pairwise_cancellation_keeps_null_outcome(
    tmp_path: Path,
) -> None:
    from test_execution import setup

    spec, store, ledger = setup(tmp_path, pairwise=True)
    report = await execute_run(
        "001",
        store=store,
        spec=spec,
        adapter=MockAdapter(
            ledger, scenario=MockScenario(outcome="confirmed_cancellation")
        ),
    )
    assert report.summary.results == 1 and report.summary.successful_results == 0
    with Repository(store) as repository:
        record = next(repository.iter_results("001"))
        assert (
            record.status == "cancelled"
            and record.error_kind == "confirmed_cancellation"
        )
        assert record.explanation is None
