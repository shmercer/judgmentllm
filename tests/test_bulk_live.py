"""Bulk admission, monotonic quota and committed audit cuts."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from test_cli_lifecycle import configuration, control
from test_decisions_lifecycle import native_spec

from judgmentllm import _bulk_dispatch as clock
from judgmentllm._bulk_dispatch import (
    BulkDispatch,
    QuotaWait,
    presenting,
    scoring_forward,
)
from judgmentllm.config import (
    BulkLimits,
    RunSpecification,
    load_run_spec,
    validate_run_spec,
)
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.retry import RetryPolicy
from judgmentllm.lifecycle import (
    arun_bulk_live,
    arun_live,
    export_run_provenance,
    validate_inputs,
)
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.bulk import BulkReservation, reservation_reference
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.ownership import executor_is_active
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.decisions_mock import MockDecisionsAdapter
from judgmentllm.providers.ledger import AcceptanceRecord
from judgmentllm.providers.mock import MockAdapter, MockScenario
from judgmentllm.providers.protocol import PreparedRequest


def bulk_spec(
    root: Path, *, decisions: bool = False, limits: dict[str, int] | None = None
) -> RunSpecification:
    if decisions:
        spec = native_spec(root)
        document = spec.model_dump()
    else:
        document = load_run_spec(configuration(root, grouped=True)).model_dump()
        document.update(schema_version=2)
    document["execution"].update(mode="bulk_live")
    if limits is not None:
        document["execution"]["bulk_limits"] = limits
    return validate_run_spec(document)


def physical_rows(run_id: str, store: Path) -> tuple[str, list[dict[str, Any]]]:
    report = export_run_provenance(run_id, store=store)
    file = next(file for file in report.manifest.files if file.name == "physical.jsonl")
    assert file.artifact.relative_path is not None
    path = Path(report.manifest_path).parent.parent / file.artifact.relative_path
    return report.bundle_id, [
        json.loads(line) for line in path.read_text().splitlines()
    ]


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "1"])
def test_bulk_limits_are_strict(bad: object) -> None:
    with pytest.raises(ValueError):
        BulkLimits.model_validate({"requests_per_minute": bad})


@pytest.mark.asyncio
async def test_quota_ages_from_transport_end_and_unused_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(clock, "monotonic", lambda: now)
    limits = BulkLimits(requests_per_minute=2, tokens_per_minute=10)
    budget = BulkDispatch("owner", limits, prior_debits=False)
    payload = b"synthetic"
    wire = PreparedRequest(
        request_uid="uid",
        task_ids=("001",),
        dependency_sha256="0" * 64,
        prompt_sha256="0" * 64,
        payload_sha256=bytes_digest(payload),
        provider="mock",
        endpoint="synthetic",
        model="fixture",
        payload=payload,
        bulk_live=True,
    )
    debit = BulkReservation(
        run_id="run",
        request_uid="uid",
        attempt_id="attempt",
        submission_id="s",
        manifest_sha256="0" * 64,
        payload_sha256=wire.payload_sha256,
        limits=limits,
        method="synthetic_utf8_v1",
        tokens=6,
    )
    assert budget.check(6) is None
    used = budget.committed(debit)
    with presenting(used), scoring_forward(wire, "s"):
        now += 65
        assert budget.check(6) == QuotaWait(None)
        assert budget.check(4) is None  # Another request can use the remaining quota.
        now += 5  # The receiver has accepted the request, but its transport is open.
        assert budget.check(6) == QuotaWait(None)
    assert budget.check(6) == QuotaWait(230.0)
    now = 229.99
    assert budget.check(6) == QuotaWait(230.0)
    now = 230.0
    assert budget.check(6) is None
    unused = budget.committed(debit)
    now += 70
    assert budget.check(6) == QuotaWait(None)
    budget.retire(unused)
    assert not unused.used
    assert budget.check(6) == QuotaWait(360.0)
    now = 360.0
    assert budget.check(6) is None
    recovered = BulkDispatch("new-owner", limits, prior_debits=True)
    assert recovered.check(1) == QuotaWait(420.0)
    now = 420.0
    assert recovered.check(10) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("decisions", [False, True])
async def test_snapshot_ignores_orphan_and_recovery_reuses_exact_receipt(
    tmp_path: Path,
    decisions: bool,
) -> None:
    spec = bulk_spec(tmp_path, decisions=decisions)
    root = tmp_path / "runtime"
    ledger = root / "mock-service.sqlite3"
    adapter = MockDecisionsAdapter(ledger) if decisions else MockAdapter(ledger)
    with prepare_run(
        spec, store=root, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass

    def fault(stage: str, request_uid: str) -> None:
        if stage == "after_bulk_reservation":
            raise RuntimeError("file published before debit")

    with pytest.raises(ExceptionGroup, match="TaskGroup"):
        await execute_run("001", spec=spec, store=root, adapter=adapter, fault=fault)
    before, rows = physical_rows("001", root)
    assert len(rows) == 1 and rows[0].get("reservation") is None
    with ReadOnlyRepository(root) as repository:
        submission = next(repository.iter_submissions("001"))
        assert submission.state == "prepared"
        file = repository.run_directory("001") / reservation_reference(
            submission.submission_id
        )
    original = file.read_bytes()
    assert adapter.submit_calls == 0
    await recover_run("001", spec=spec, store=root, adapter=adapter)
    assert file.read_bytes() == original
    after, rows = physical_rows("001", root)
    assert before != after
    assert len(rows) == 1
    assert rows[0]["reservation"]["receipt_sha256"] == bytes_digest(original)
    assert rows[0]["selected_usage"]["invocation_kind"] == "managed_bulk_live"
    assert adapter.submit_calls == 1
    assert physical_rows("001", root)[0] == after


@pytest.mark.asyncio
@pytest.mark.parametrize("decisions", [False, True])
async def test_concurrent_export_verifies_publication_at_its_original_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decisions: bool
) -> None:
    spec = bulk_spec(tmp_path, decisions=decisions)
    root = tmp_path / "runtime"
    ledger = root / "mock-service.sqlite3"
    adapter = MockDecisionsAdapter(ledger) if decisions else MockAdapter(ledger)
    with prepare_run(
        spec, store=root, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass

    frozen = threading.Event()
    release = threading.Event()
    reference_path = ArtifactStore.reference_path
    prefix = "decisions-provenance/" if decisions else "bulk-provenance/"

    def pause_at_reference(store: ArtifactStore, relative_path: str) -> Path:
        if relative_path.startswith(prefix) and not frozen.is_set():
            frozen.set()
            assert release.wait(15)
        return reference_path(store, relative_path)

    monkeypatch.setattr(ArtifactStore, "reference_path", pause_at_reference)
    first = asyncio.create_task(
        asyncio.to_thread(export_run_provenance, "001", store=root)
    )
    try:
        assert await asyncio.to_thread(frozen.wait, 10)
        published = await asyncio.wait_for(
            asyncio.to_thread(export_run_provenance, "001", store=root), 10
        )
        await asyncio.wait_for(
            execute_run("001", store=root, adapter=adapter, spec=spec), 10
        )
    finally:
        release.set()
        original_cut = await asyncio.wait_for(first, 10)
    assert original_cut == published
    newer_bundle, rows = physical_rows("001", root)
    assert newer_bundle != original_cut.bundle_id
    assert rows[0]["reservation"]["receipt"]["requests"] == 1


@pytest.mark.asyncio
async def test_cancel_during_debit_settles_commit_before_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = bulk_spec(tmp_path)
    root = tmp_path / "store"
    adapter = MockAdapter(root / "mock-service.sqlite3")
    with prepare_run(
        spec, store=root, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    finish = threading.Event()
    closed = False

    def fault(stage: str, request_uid: str) -> None:
        if stage == "during_bulk_debit":
            loop.call_soon_threadsafe(entered.set)
            assert finish.wait(10)

    async def close() -> None:
        nonlocal closed
        with ReadOnlyRepository(root) as repository:
            assert executor_is_active(repository, "001") is True
            assert next(repository.iter_submissions("001")).state == "uncertain"
        closed = True

    monkeypatch.setattr(adapter, "aclose", close, raising=False)
    run = asyncio.create_task(
        execute_run("001", spec=spec, store=root, adapter=adapter, fault=fault)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        run.cancel()
        await asyncio.sleep(0)
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert closed and adapter.submit_calls == 0
    with ReadOnlyRepository(root) as repository:
        assert executor_is_active(repository, "001") is False
    _, rows = physical_rows("001", root)
    assert rows[0]["reservation"]["receipt"]["requests"] == 1
    assert rows[0]["selected_usage"] is None


@pytest.mark.asyncio
async def test_cancel_drains_transient_rejection_with_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = bulk_spec(tmp_path)
    root = tmp_path / "runtime"
    adapter = MockAdapter(
        root / "mock-service.sqlite3",
        scenario=MockScenario(transport="transient_not_submitted", retry_after=17),
    )
    with prepare_run(
        spec, store=root, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass

    sending = asyncio.Event()
    stopping = asyncio.Event()
    release = asyncio.Event()
    establish = adapter._establish
    stop = BulkDispatch.stop

    async def wait_for_cancellation(
        record: AcceptanceRecord, service_identity: str
    ) -> AcceptanceRecord:
        assert not record.accepted
        sending.set()
        await release.wait()
        return await establish(record, service_identity)

    def mark_stop(dispatch: BulkDispatch) -> None:
        stop(dispatch)
        stopping.set()

    monkeypatch.setattr(adapter, "_establish", wait_for_cancellation)
    monkeypatch.setattr(BulkDispatch, "stop", mark_stop)
    run = asyncio.create_task(
        execute_run("001", store=root, adapter=adapter, spec=spec)
    )
    try:
        await asyncio.wait_for(sending.wait(), 10)
        run.cancel()
        await asyncio.wait_for(stopping.wait(), 10)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 10)

    with ReadOnlyRepository(root) as repository:
        assert repository.get_run("001").state == "cancelled"
        assert (
            next(repository.iter_submissions("001")).state == "confirmed_not_submitted"
        )
        (receipt,) = repository.iter_evidence("001")
        assert receipt.failure_transient
        assert receipt.failure_retry_after == 17
        (attempt,) = repository.iter_attempts("001")
        assert attempt.next_eligible_at is not None
    assert adapter.submit_calls == 1


def test_bulk_cli_interrupt_during_adapter_close_keeps_completed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = bulk_spec(tmp_path)
    config = tmp_path / "bulk.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    root = tmp_path / "runtime"
    closed: list[str] = []

    async def close(_: MockAdapter) -> None:
        with ReadOnlyRepository(root) as repository:
            run_id = repository.connection.execute(
                "SELECT run_id FROM runs"
            ).fetchone()[0]
            assert repository.get_run(run_id).state == "completed"
            assert executor_is_active(repository, run_id)
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(0)
        with ReadOnlyRepository(root) as repository:
            assert executor_is_active(repository, run_id)
        closed.append(run_id)

    monkeypatch.setattr(MockAdapter, "aclose", close, raising=False)
    envelope, _ = control(["run", "--config", str(config), "--store", str(root)], 130)
    assert envelope["error"]["category"] == "interrupted"
    assert closed == [envelope["run_id"]]
    with ReadOnlyRepository(root) as repository:
        assert repository.get_run(closed[0]).state == "completed"
        assert not executor_is_active(repository, closed[0])
        assert all(
            row.status == "complete" for row in repository.iter_results(closed[0])
        )


@pytest.mark.asyncio
async def test_retry_uses_new_debit_and_missing_committed_receipt_stops_reopen(
    tmp_path: Path,
) -> None:
    spec = bulk_spec(tmp_path)
    root = tmp_path / "runtime"
    adapter = MockAdapter(
        root / "mock-service.sqlite3",
        scenario=MockScenario(transient_failures=1, retry_after=0),
    )
    with prepare_run(
        spec, store=root, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass
    report = await execute_run(
        "001",
        spec=spec,
        store=root,
        adapter=adapter,
        retry_policy=RetryPolicy(initial_backoff=0),
    )
    assert report.summary.successful_results == 3 and adapter.submit_calls == 2
    _, rows = physical_rows("001", root)
    assert len(rows) == 2
    assert [r["attempt_number"] for r in rows] == [1, 2]
    assert len({r["reservation"]["event_seq"] for r in rows}) == 2
    assert all(
        r["selected_usage"]["invocation_kind"] == "managed_bulk_live" for r in rows
    )
    with ReadOnlyRepository(root) as repository:
        reference = repository.run_directory("001") / reservation_reference(
            rows[0]["submission_id"]
        )
    reference.unlink()
    with pytest.raises(IntegrityError, match="bulk reservation reference"):
        await recover_run("001", spec=spec, store=root, adapter=adapter)
    assert adapter.submit_calls == 2


@pytest.mark.asyncio
async def test_token_bound_and_public_mode_admission(tmp_path: Path) -> None:
    oversized = bulk_spec(tmp_path, limits={"tokens_per_minute": 1})
    store = tmp_path / "oversized"
    with pytest.raises(Exception, match="token bound exceeds"):
        await arun_bulk_live(oversized, store=store)
    with Repository(store) as repository:
        assert not any(
            event.new_state == "submitting"
            for event in repository.events(
                repository.connection.execute("SELECT run_id FROM runs").fetchone()[0]
            )
        )
    with pytest.raises(Exception, match="execution.mode"):
        await arun_live(oversized, store=tmp_path / "wrong-mode")
    accepted = bulk_spec(
        tmp_path / "accepted",
        limits={"tokens_per_minute": 100_000, "requests_per_minute": 1},
    )
    summary = await arun_bulk_live(accepted, store=tmp_path / "accepted-store")
    assert summary.run_status == "completed"
    _, rows = physical_rows(summary.run_id, tmp_path / "accepted-store")
    assert rows[0]["reservation"]["receipt"]["tokens"] > 1
    assert rows[0]["reservation"]["receipt"]["method"] == "synthetic_utf8_v1"


def test_production_tpm_is_an_offline_validation_error(tmp_path: Path) -> None:
    data = bulk_spec(
        tmp_path, limits={"tokens_per_minute": 10, "requests_per_minute": 1}
    ).model_dump()
    data.update(
        schema_version=3, provider="openai", endpoint="responses", model="gpt-6-luna"
    )
    data["prompt"]["structured_output"] = "text"
    report = validate_inputs(validate_run_spec(data))
    assert not report.valid
    assert report.issues[0].field == "execution.bulk_limits.tokens_per_minute"
    assert "no verified token bound" in report.issues[0].message
    result, _ = control(["runs", "cancel", "--help"])
    assert result["command"] == "runs cancel"


_RECOVER_CHILD = """
import asyncio
import sys
from pathlib import Path
from judgmentllm import _bulk_dispatch as clock
from judgmentllm.config import load_run_spec
from judgmentllm.execution import recover_run
from judgmentllm.persistence.models import OwnershipError
from judgmentllm.providers.mock import MockAdapter

root, config, expectation = sys.argv[1:]
adapter = MockAdapter(Path(root) / "mock-service.sqlite3", reconciliation_available=False)
now = 500.0
clock.monotonic = lambda: now
waited = False

async def wait(self, capacity, changed):
    global now, waited
    assert not waited and adapter.submit_calls == 0
    assert capacity.deadline == now + 60
    now += 59
    assert self.check(0) == capacity
    now += 1
    assert self.check(0) is None
    waited = True

clock.BulkDispatch.wait = wait
try:
    report = asyncio.run(recover_run("001", spec=load_run_spec(config), store=root, adapter=adapter))
except OwnershipError:
    assert expectation == "held" and adapter.submit_calls == 0
else:
    assert expectation == "reopened" and waited and adapter.submit_calls == 1
    assert report.summary.unresolved_submissions == 1
    assert report.summary.successful_results == 3
print(expectation)
"""


@pytest.mark.asyncio
async def test_process_handoff_restarts_window_and_keeps_uncertainty(
    tmp_path: Path,
) -> None:
    spec = bulk_spec(tmp_path, limits={"requests_per_minute": 1})
    with spec.input.path.open("a", encoding="utf-8") as source:
        source.write(
            json.dumps({"item_id": "002", "text": "Second synthetic input."}) + "\n"
        )
    configuration_path = tmp_path / "bulk.json"
    configuration_path.write_text(spec.model_dump_json(), encoding="utf-8")
    root = tmp_path / "store"
    adapter = MockAdapter(root / "mock-service.sqlite3")
    with prepare_run(
        spec, store=root, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    closed = threading.Event()

    def fault(stage: str, _: str) -> None:
        if stage == "after_submitting":
            loop.call_soon_threadsafe(entered.set)
            assert closed.wait(15)
            raise RuntimeError("interrupted after debit commit")

    def child(expectation: str) -> None:
        environment = {
            name: os.environ[name]
            for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR")
            if name in os.environ
        }
        environment.update(HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _RECOVER_CHILD,
                str(root),
                str(configuration_path),
                expectation,
            ],
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == expectation

    run = asyncio.create_task(
        execute_run("001", spec=spec, store=root, adapter=adapter, fault=fault)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        await asyncio.to_thread(child, "held")
    finally:
        closed.set()
    with pytest.raises(ExceptionGroup, match="TaskGroup"):
        await run
    await asyncio.to_thread(child, "reopened")
    with ReadOnlyRepository(root) as repository:
        assert {s.state for s in repository.iter_submissions("001")} == {
            "uncertain",
            "resolved",
        }
        assert not list(repository.connection.execute("SELECT * FROM retry_operations"))
