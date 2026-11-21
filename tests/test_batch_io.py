"""One finite admission budget covers every protocol read, write and stream."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
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
from judgmentllm.batch_models import BatchOperationIntentV1
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
    entered = asyncio.Event()

    async def slow_pause(self: MockBatchAdapter) -> None:
        entered.set()
        await asyncio.Event().wait()

    if boundary == "create":
        original = MockBatchAdapter.invoke

        async def slow_create(
            self: MockBatchAdapter,
            intent: BatchOperationIntentV1,
            payload: AsyncIterator[bytes],
            guard: Callable[[], None],
        ) -> bytes:
            if intent.kind == "create":
                await slow_pause(self)
            return await original(self, intent, payload, guard)

        monkeypatch.setattr(MockBatchAdapter, "invoke", slow_create)
    elif boundary == "stream":

        async def slow_chunks(
            self: MockBatchAdapter, raw: bytes
        ) -> AsyncIterator[bytes]:
            await slow_pause(self)
            yield raw

        monkeypatch.setattr(MockBatchAdapter, "chunks", slow_chunks)
    else:
        monkeypatch.setattr(MockBatchAdapter, "pause", slow_pause)
    action = (
        acancel_batch
        if boundary == "cancel"
        else apoll_batch
        if boundary == "status"
        else acollect_batch
        if boundary in ("source", "stream")
        else asubmit_batch
    )
    result = await action(
        "001",
        store=root,
        spec=spec,
        mock=mock,
        options=BatchControlOptionsV1(deadline_seconds=0.5),
    )
    assert entered.is_set() and result.outcome == "partial"
    assert service_counts(root)["forwards"] == before + (boundary == "create")
    with ReadOnlyRepository(root) as repository:
        assert not executor_is_active(repository, "001")
        assert repository.summary("001").results == 0


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
