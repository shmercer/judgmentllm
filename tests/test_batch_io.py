"""One finite admission budget covers every protocol read, write and stream."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from batch_clock import batch_clock
from test_native_batch import batch_spec, mock_fixture, service_counts

from judgmentllm import (
    BatchControlOptionsV1,
    BatchMockOptions,
    acancel_batch,
    acollect_batch,
    apoll_batch,
    asubmit_batch,
    prepare_batch,
)
from judgmentllm.batch_models import BatchOperationIntentV1, BatchStatusEvidenceV1
from judgmentllm.persistence.ownership import executor_is_active
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.providers.batch_mock import MockBatchAdapter


@pytest.mark.asyncio
async def test_mock_connections_close_without_gc_and_keep_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.persistence.hashing import bytes_digest
    from judgmentllm.persistence.models import IntegrityError

    ledger = tmp_path / "service.sqlite3"
    opened: list[sqlite3.Connection] = []
    original = sqlite3.connect

    def tracked(database: Any, *arguments: Any, **options: Any) -> sqlite3.Connection:
        connection = original(database, *arguments, **options)
        if Path(database) == ledger:
            opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", tracked)
    mock = BatchMockOptions(ledger_path=ledger)
    adapter = MockBatchAdapter(ledger, mock, create=True)
    # Strong references prevent garbage collection from hiding leaked handles.
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    intent = BatchOperationIntentV1(
        run_id="001",
        shard_id="shard",
        logical_id="1" * 64,
        instance_id="2" * 64,
        sequence=1,
        kind="cancel",
        service_id=adapter.identity,
        manifest_sha256="3" * 64,
        payload_sha256=bytes_digest(b"{}"),
        owner_token="owner",
        remote_id="mock_job_" + "4" * 64,
    )

    async def payload() -> AsyncIterator[bytes]:
        yield b"{}"

    with pytest.raises(IntegrityError, match="unknown job"):
        await adapter.invoke(intent, payload(), lambda: None)
    assert await adapter.reconcile_operation(intent) is None
    await adapter.fence_unsent(intent)
    assert await adapter.reconcile_operation(intent) is not None
    with closing(original(ledger)) as connection:
        assert connection.execute("SELECT forwards FROM operations").fetchall() == [
            (0,)
        ]
    spec = batch_spec(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    await asubmit_batch("001", store=root, spec=spec, mock=mock)
    from judgmentllm import acancel_batch, aresume_batch

    await acancel_batch("001", store=root, mock=mock)
    await aresume_batch("001", store=root, spec=spec, mock=mock)
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    # Windows must be able to remove the service family while handles are held
    # strongly by this test; no gc.collect() or process exit establishes cleanup.
    for path in tmp_path.glob("service.sqlite3*"):
        path.unlink()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    ["upload", "create", "cancel", "reconcile", "status", "source", "stream"],
)
async def test_each_protocol_boundary_obeys_shared_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    spec = batch_spec(tmp_path)
    mock = mock_fixture(
        tmp_path, create="uncertain" if boundary == "reconcile" else "accepted"
    )
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    if boundary not in ("upload", "create"):
        await asubmit_batch("001", store=root, spec=spec, mock=mock)
    before = service_counts(root)["forwards"]
    pending = None
    with ReadOnlyRepository(root) as repository:
        attempts = {a.attempt_id for a in repository.iter_attempts("001")}
        submissions = repository.connection.execute(
            "SELECT submission_id,remote_id FROM submissions"
        ).fetchall()
        if boundary == "reconcile":
            records = repository.connection.execute(
                "SELECT record_json FROM batch_operations WHERE kind='create'"
            ).fetchall()
            assert len(records) == 1
            pending = BatchOperationIntentV1.model_validate_json(records[0][0])
    assert len(attempts) == 2
    if boundary in ("upload", "create"):
        assert submissions == []
        submission = remote = None
    else:
        assert len(submissions) == 1
        submission, remote = submissions[0]
        assert (remote is None) == (boundary == "reconcile")
    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        assert not entered.is_set()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closing.set()
            await release.wait()
        raise AssertionError("cancelled protocol operation must not return evidence")

    if boundary in ("upload", "create", "cancel"):
        invoke = MockBatchAdapter.invoke

        async def slow_invoke(
            self: MockBatchAdapter,
            intent: BatchOperationIntentV1,
            payload: AsyncIterator[bytes],
            guard: Callable[[], None],
        ) -> bytes:
            if intent.kind == boundary:
                assert intent.run_id == "001"
                if boundary == "cancel":
                    assert remote is not None and intent.remote_id == remote
                await hold()
            return await invoke(self, intent, payload, guard)

        monkeypatch.setattr(MockBatchAdapter, "invoke", slow_invoke)
    elif boundary == "reconcile":
        reconcile = MockBatchAdapter.reconcile_operation

        async def slow_reconcile(
            self: MockBatchAdapter, intent: BatchOperationIntentV1
        ) -> bytes | None:
            if intent.kind == "create":
                assert intent == pending and intent.instance_id == submission
                await hold()
            return await reconcile(self, intent)

        monkeypatch.setattr(MockBatchAdapter, "reconcile_operation", slow_reconcile)
    elif boundary == "status":
        status = MockBatchAdapter.status

        async def slow_status(
            self: MockBatchAdapter, submission_id: str, remote_id: str
        ) -> BatchStatusEvidenceV1:
            assert (submission_id, remote_id) == (submission, remote)
            await hold()
            return await status(self, submission_id, remote_id)

        monkeypatch.setattr(MockBatchAdapter, "status", slow_status)
    elif boundary == "source":
        source = MockBatchAdapter.result_source

        async def slow_source(
            self: MockBatchAdapter, submission_id: str, kind: str
        ) -> str:
            if kind == "output":
                assert submission_id == submission
                await hold()
            return await source(self, submission_id, kind)

        monkeypatch.setattr(MockBatchAdapter, "result_source", slow_source)
    else:
        chunks = MockBatchAdapter.chunks

        async def slow_chunks(
            self: MockBatchAdapter, raw: bytes
        ) -> AsyncIterator[bytes]:
            assert raw
            await hold()
            async for chunk in chunks(self, raw):
                yield chunk

        monkeypatch.setattr(MockBatchAdapter, "chunks", slow_chunks)
    action = (
        acancel_batch
        if boundary == "cancel"
        else apoll_batch
        if boundary == "status"
        else acollect_batch
        if boundary in ("source", "stream")
        else asubmit_batch
    )
    with batch_clock(monkeypatch) as clock:
        task = asyncio.create_task(
            action(
                "001",
                store=root,
                spec=spec,
                mock=mock,
                options=BatchControlOptionsV1(deadline_seconds=0.5),
            )
        )
        try:
            await clock.gate(entered, task)
            assert len(clock.budgets) == 1
            budget = clock.budgets[0]
            assert budget.options.deadline_seconds == 0.5
            assert budget.deadline == clock.start + 0.5
            counters = budget.steps, budget.bytes, budget.result_rows
            assert budget.steps > 0 and budget.result_rows == 0
            assert service_counts(root)["forwards"] == before + (boundary == "create")
            clock.advance(0.5)
            await clock.gate(closing, task)
            with ReadOnlyRepository(root) as repository:
                assert executor_is_active(repository, "001") is True
            assert not task.done() and clock.now >= budget.deadline
            assert (budget.steps, budget.bytes, budget.result_rows) == counters
            release.set()
            assert (await clock.wait(task)).outcome == "partial"
            assert budget.closed and budget.deadline == clock.start + 0.5
            assert (budget.steps, budget.bytes, budget.result_rows) == counters
        finally:
            await clock.cleanup(task, release)
    assert service_counts(root)["forwards"] == before + (boundary == "create")
    with ReadOnlyRepository(root) as repository:
        assert not executor_is_active(repository, "001")
        assert repository.summary("001").results == 0
        assert {a.attempt_id for a in repository.iter_attempts("001")} == attempts


@pytest.mark.asyncio
async def test_poll_counter_stops_without_spinning_or_sending(tmp_path: Path) -> None:
    spec = batch_spec(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001")
    await asubmit_batch("001", store=root, spec=spec)
    result = await apoll_batch(
        "001",
        store=root,
        options=BatchControlOptionsV1(
            max_polls=100, poll_interval_seconds=0, max_io_steps=1
        ),
    )
    assert result.outcome == "partial" and service_counts(root)["forwards"] == 2
    import sqlite3

    with sqlite3.connect(root / "mock-batch-service.sqlite3") as connection:
        assert (
            connection.execute("SELECT sum(status_reads) FROM jobs").fetchone()[0] == 1
        )


@pytest.mark.asyncio
async def test_oversized_chunk_never_enters_staging_or_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = batch_spec(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001")
    await asubmit_batch("001", store=root, spec=spec)

    async def huge(self: MockBatchAdapter, raw: bytes) -> AsyncIterator[bytes]:
        yield b"PROHIBITED_HUGE_STREAM" + b"x" * 65_536

    monkeypatch.setattr(MockBatchAdapter, "chunks", huge)
    from judgmentllm.lifecycle_models import LifecycleError

    with pytest.raises(LifecycleError):
        await acollect_batch("001", store=root, spec=spec)
    with ReadOnlyRepository(root) as repository:
        assert repository.summary("001").results == 0
    assert all(
        b"PROHIBITED_HUGE_STREAM" not in p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    )
