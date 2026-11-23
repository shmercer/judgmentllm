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


@pytest.mark.asyncio
async def test_caller_cancellation_drains_before_recording_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import get_batch_status

    spec, mock = batch_spec(tmp_path), mock_fixture(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    await asubmit_batch("001", store=root, spec=spec, mock=mock)
    counts = service_counts(root)
    assert counts == {"forwards": 2, "jobs": 1, "cancelled": 0}
    with ReadOnlyRepository(root) as repository:
        attempts = tuple(repository.iter_attempts("001"))
        submissions = tuple(repository.iter_submissions("001"))
        results = tuple(repository.iter_results("001"))
        assert len(attempts) == len(submissions) == 2 and not results
        accepted = submissions[0]
        assert submissions == (accepted, accepted)
        assert accepted.certainty == "accepted" and accepted.remote_id is not None
        assert accepted.batch_uid is not None
        assert [
            tuple(row)
            for row in repository.connection.execute(
                "SELECT submission_id,batch_uid,remote_id FROM submissions WHERE run_id='001'"
            )
        ] == [(accepted.submission_id, accepted.batch_uid, accepted.remote_id)]
        assert [
            tuple(row)
            for row in repository.connection.execute(
                "SELECT batch_uid,remote_id FROM batches WHERE run_id='001'"
            )
        ] == [(accepted.batch_uid, accepted.remote_id)]
        members = repository.connection.execute(
            "SELECT attempt_id FROM batch_members WHERE run_id='001' AND batch_uid=?",
            (accepted.batch_uid,),
        ).fetchall()
        assert len(members) == 2
        assert {row[0] for row in members} == {a.attempt_id for a in attempts}
    entered, closing, release, drained = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    stops: list[str] = []
    status = MockBatchAdapter.status

    async def held_status(
        self: MockBatchAdapter, submission_id: str, remote_id: str
    ) -> BatchStatusEvidenceV1:
        assert (submission_id, remote_id) == (
            accepted.submission_id,
            accepted.remote_id,
        )
        evidence = await status(self, submission_id, remote_id)
        assert (evidence.submission_id, evidence.remote_id) == (
            submission_id,
            remote_id,
        )
        assert not entered.is_set()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closing.set()
            await release.wait()
            drained.set()
        raise AssertionError("cancelled status read returned evidence")

    def witness(stage: str, identity: str) -> None:
        if stage == "after_batch_stop":
            assert drained.is_set() and release.is_set()
            stops.append(stage)

    monkeypatch.setattr(MockBatchAdapter, "status", held_status)
    with batch_clock(monkeypatch) as clock:
        task = asyncio.create_task(
            apoll_batch("001", store=root, mock=mock, _fault=witness)
        )
        try:
            await clock.gate(entered, task)
            assert len(clock.budgets) == 1
            assert clock.budgets[0].options == BatchControlOptionsV1()
            assert task.cancel()
            await clock.gate(closing, task)
            assert not task.done() and not drained.is_set() and not stops
            with ReadOnlyRepository(root) as repository:
                assert executor_is_active(repository, "001") is True
                assert repository.get_owner("001") is not None
            pending = get_batch_status("001", store=root)
            assert not pending.batch.local_stop and not pending.batch.cancel_all
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await clock.wait(task)
            assert drained.is_set() and stops == ["after_batch_stop"]
        finally:
            await clock.cleanup(task, release)
    stopped = get_batch_status("001", store=root)
    assert stopped.batch.local_stop and not stopped.batch.cancel_all
    assert service_counts(root) == counts
    with ReadOnlyRepository(root) as repository:
        assert executor_is_active(repository, "001") is False
        assert repository.get_owner("001") is None
        assert tuple(repository.iter_attempts("001")) == attempts
        assert tuple(repository.iter_submissions("001")) == submissions
        assert tuple(repository.iter_results("001")) == results


@pytest.mark.asyncio
async def test_deadline_drain_survives_repeated_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import get_batch_status
    from judgmentllm.batch_models import BatchMemberReceiptV1

    spec = batch_spec(tmp_path)
    mock = mock_fixture(tmp_path, input_tokens=7, output_tokens=3)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    await asubmit_batch("001", store=root, spec=spec, mock=mock)
    counts = service_counts(root)
    assert counts == {"forwards": 2, "jobs": 1, "cancelled": 0}
    with ReadOnlyRepository(root) as repository:
        science = tuple(
            a.model_dump(exclude={"state"}) for a in repository.iter_attempts("001")
        )
        physical = repository.connection.execute(
            "SELECT submission_id,remote_id FROM submissions WHERE certainty='accepted'"
        ).fetchall()
        assert len(science) == 2 and len(physical) == 1
        submission, remote = physical[0]
        assert remote is not None and repository.summary("001").results == 0
    entered, closing, interrupted, second_turn, release, drained = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    original_status = MockBatchAdapter.status
    stops: list[str] = []

    async def held_status(
        self: MockBatchAdapter, submission_id: str, remote_id: str
    ) -> BatchStatusEvidenceV1:
        assert (submission_id, remote_id) == (submission, remote)
        evidence = await original_status(self, submission_id, remote_id)
        assert (evidence.submission_id, evidence.remote_id) == (submission, remote)
        assert not entered.is_set()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closing.set()
            # One owned waiter survives interrupts until cleanup is released.
            while not release_waiter.done():
                try:
                    await asyncio.shield(release_waiter)
                except asyncio.CancelledError:
                    interrupted.set()
            assert await release_waiter
            drained.set()
        raise AssertionError("cancelled read must not return status evidence")

    def witness(stage: str, identity: str) -> None:
        if stage == "after_batch_stop":
            assert drained.is_set() and release.is_set()
            stops.append(stage)

    monkeypatch.setattr(MockBatchAdapter, "status", held_status)
    with batch_clock(monkeypatch) as clock:
        release_waiter = asyncio.create_task(release.wait())
        task = asyncio.create_task(
            apoll_batch(
                "001",
                store=root,
                mock=mock,
                options=BatchControlOptionsV1(deadline_seconds=0.15),
                _fault=witness,
            )
        )
        try:
            await clock.gate(entered, task)
            assert len(clock.budgets) == 1
            budget = clock.budgets[0]
            assert budget.options == BatchControlOptionsV1(deadline_seconds=0.15)
            assert budget.deadline == clock.start + 0.15
            clock.advance(0.15)
            await clock.gate(closing, task)
            assert not interrupted.is_set()
            cancelling_at_deadline = task.cancelling()
            assert clock.now >= budget.deadline and not release_waiter.done()
            assert task.cancel()
            await clock.gate(interrupted, task)
            assert task.cancelling() == cancelling_at_deadline + 1 and not task.done()
            assert task.cancel()
            asyncio.get_running_loop().call_soon(second_turn.set)
            await clock.gate(second_turn, task)
            assert task.cancelling() == cancelling_at_deadline + 2 and not task.done()
            assert not release_waiter.done() and not drained.is_set() and not stops
            assert budget.deadline == clock.start + 0.15
            with ReadOnlyRepository(root) as repository:
                assert repository.get_owner("001") is not None
                assert executor_is_active(repository, "001") is True
            pending = get_batch_status("001", store=root)
            assert not pending.batch.local_stop and not pending.batch.cancel_all
            assert service_counts(root) == counts
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await clock.wait(task)
            assert budget.closed and 0 < budget.steps <= budget.options.max_io_steps
            assert budget.result_rows == 0 and drained.is_set()
            assert stops == ["after_batch_stop"]
        finally:
            release.set()
            try:
                await clock.cleanup(task, release)
            finally:
                await clock.wait(asyncio.gather(release_waiter, return_exceptions=True))
        assert release_waiter.done() and not release_waiter.cancelled()
    stopped = get_batch_status("001", store=root)
    assert stopped.batch.local_stop and not stopped.batch.cancel_all
    assert service_counts(root) == counts
    with ReadOnlyRepository(root) as repository:
        assert repository.get_owner("001") is None
        assert executor_is_active(repository, "001") is False
        assert repository.summary("001").results == 0
    collected = await acollect_batch("001", store=root, spec=spec, mock=mock)
    assert collected.batch.missing_members == 0 and collected.batch.local_stop
    assert not collected.batch.cancel_all and service_counts(root) == counts
    with ReadOnlyRepository(root) as repository:
        assert (
            tuple(
                a.model_dump(exclude={"state"}) for a in repository.iter_attempts("001")
            )
            == science
        )
        results = tuple(repository.iter_results("001"))
        assert len(results) == 2 and all(r.status == "complete" for r in results)
        receipts = [
            BatchMemberReceiptV1.model_validate_json(row[0])
            for row in repository.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert len(receipts) == len({r.identity.observation_id for r in receipts}) == 2
        assert all(r.identity.submission_id == submission for r in receipts)
        assert all(
            (r.usage.input_tokens, r.usage.output_tokens) == (7, 3) for r in receipts
        )
        assert repository.summary("001").retry_operations == 0
        assert repository.get_owner("001") is None
        assert executor_is_active(repository, "001") is False


def _assert_invocation_state(
    root: Path, *, active: bool, stopped: bool, forwards: int = 2, jobs: int = 1
) -> None:
    from judgmentllm import get_batch_status

    assert service_counts(root) == {"forwards": forwards, "jobs": jobs, "cancelled": 0}
    status = get_batch_status("001", store=root)
    assert status.batch.local_stop is stopped and not status.batch.cancel_all
    with ReadOnlyRepository(root) as repository:
        assert (repository.get_owner("001") is not None) is active
        assert executor_is_active(repository, "001") is active
        assert repository.summary("001").attempts == 2
        assert repository.summary("001").results == 0
        assert repository.summary("001").retry_operations == 0
        assert repository.connection.execute(
            "SELECT count(*) FROM batch_facts WHERE kind='stop'"
        ).fetchone()[0] == int(stopped)
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_facts WHERE kind='cancel_all'"
            ).fetchone()[0]
            == 0
        )
        for table in ("observations", "batch_member_receipts"):
            assert (
                repository.connection.execute(
                    "SELECT count(*) FROM " + table
                ).fetchone()[0]
                == 0
            )
        if not active:
            lock = repository.run_directory("001") / "executor.lock"
            assert not lock.is_symlink() and lock.read_bytes() == b"\0"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["deadline-only", "caller-cancel", "repeat-caller-cancel", "inherited"]
)
async def test_late_cleanup_distinguishes_invocation_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    import sys

    from judgmentllm.batch_models import BatchMemberReceiptV1, BatchRunStatusV1
    from judgmentllm.execution.native_batch import InvocationBudget

    spec = batch_spec(tmp_path)
    mock = mock_fixture(tmp_path, input_tokens=7, output_tokens=3)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    await asubmit_batch("001", store=root, spec=spec, mock=mock)
    with ReadOnlyRepository(root) as repository:
        attempts = {a.attempt_id for a in repository.iter_attempts("001")}
    entered, closing, release, drained = (asyncio.Event() for _ in range(4))
    count = {"caller-cancel": 1, "repeat-caller-cancel": 2}.get(mode, 0)
    repeat_direct_await = count == 2 and sys.version_info >= (3, 12)
    acknowledgments = [asyncio.Event() for _ in range(count)]
    interrupts = 0
    settled_cancels: list[int] = []
    stops: list[str] = []
    reads: list[asyncio.Task[Any]] = []
    original_status = MockBatchAdapter.status

    async def held_status(
        self: MockBatchAdapter, submission_id: str, remote_id: str
    ) -> BatchStatusEvidenceV1:
        nonlocal interrupts
        current = asyncio.current_task()
        assert current is not None
        reads.append(current)
        evidence = await original_status(self, submission_id, remote_id)
        assert (evidence.submission_id, evidence.remote_id) == (
            submission_id,
            remote_id,
        )
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closing.set()
            while not release_waiter.done():
                try:
                    await asyncio.shield(release_waiter)
                except asyncio.CancelledError:
                    interrupts += 1
                    if interrupts == 1 and acknowledgments:
                        acknowledgments[0].set()
                    elif repeat_direct_await and interrupts == 2:
                        acknowledgments[1].set()
            assert await release_waiter
            drained.set()
            raise OSError("synthetic late cleanup error")

    def witness(stage: str, identity: str) -> None:
        if stage == "after_batch_stop":
            assert release.is_set() and drained.is_set()
            if repeat_direct_await:
                assert len(settled_cancels) == 1
            stops.append(stage)

    async def invoke() -> BatchRunStatusV1:
        current = asyncio.current_task()
        assert current is not None
        if mode == "inherited":
            assert current.cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass
            assert current.cancelling() == 1
        return await apoll_batch(
            "001",
            store=root,
            mock=mock,
            options=BatchControlOptionsV1(deadline_seconds=0.15),
            _fault=witness,
        )

    monkeypatch.setattr(MockBatchAdapter, "status", held_status)
    with batch_clock(monkeypatch) as clock:
        release_waiter = asyncio.create_task(release.wait())
        task = asyncio.create_task(invoke())
        try:
            await clock.gate(entered, task)
            assert len(clock.budgets) == 1
            budget = clock.budgets[0]
            assert budget.options == BatchControlOptionsV1(deadline_seconds=0.15)
            clock.advance(0.15)
            await clock.gate(closing, task)
            parent_before = task.cancelling()
            read_before = reads[0].cancelling()
            assert read_before > 0 and interrupts == 0
            if count:
                assert task.cancel()
                await clock.gate(acknowledgments[0], task)
                assert task.cancelling() == parent_before + 1
                read_after_first = reads[0].cancelling()
                assert read_after_first > read_before
                assert interrupts == 1
                if count == 2:
                    original_external_cancelled = InvocationBudget.external_cancelled
                    repeat_ack = acknowledgments[1]

                    def observe_repeat(self: InvocationBudget) -> bool:
                        external = original_external_cancelled(self)
                        if (
                            repeat_direct_await
                            and self is budget
                            and asyncio.current_task() is task
                            and drained.is_set()
                            and not settled_cancels
                        ):
                            assert release.is_set() and not stops
                            assert external
                            # Timeout exit removes its own cancellation request.
                            assert task.cancelling() == parent_before + 1
                            assert reads[0].cancelling() == read_after_first + 1
                            assert interrupts == 2
                            settled_cancels.append(task.cancelling())
                        if (
                            not repeat_direct_await
                            and self is budget
                            and asyncio.current_task() is task
                            and task.cancelling() == parent_before + 2
                            and not repeat_ack.is_set()
                        ):
                            assert external
                            assert reads[0].cancelling() == read_after_first
                            assert interrupts == 1
                            assert not release.is_set() and not drained.is_set()
                            assert not stops
                            repeat_ack.set()
                        return external

                    monkeypatch.setattr(
                        InvocationBudget, "external_cancelled", observe_repeat
                    )
                    assert task.cancel()
                    await clock.gate(repeat_ack, task)
                    assert task.cancelling() == parent_before + 2
                    if repeat_direct_await:
                        assert reads[0].cancelling() == read_after_first + 1
                        assert interrupts == 2 and not settled_cancels
                    else:
                        assert reads[0].cancelling() == read_after_first
                        assert interrupts == 1
            assert not release_waiter.done() and not drained.is_set() and not stops
            _assert_invocation_state(root, active=True, stopped=False)
            release.set()
            if count:
                with pytest.raises(asyncio.CancelledError):
                    await clock.wait(task)
            else:
                assert (await clock.wait(task)).outcome == "partial"
            if repeat_direct_await:
                assert settled_cancels == [parent_before + 1]
            assert drained.is_set() and reads[0].done()
            assert budget.closed and budget.result_rows == 0
            assert 0 < budget.steps <= budget.options.max_io_steps
            assert budget.deadline == clock.start + 0.15
            assert stops == (["after_batch_stop"] if count else [])
        finally:
            release.set()
            try:
                await clock.cleanup(task, release)
            finally:
                await clock.wait(asyncio.gather(release_waiter, return_exceptions=True))
    _assert_invocation_state(root, active=False, stopped=bool(count))
    if count:
        for _ in range(2):
            collected = await acollect_batch("001", store=root, spec=spec, mock=mock)
            assert collected.batch.missing_members == 0 and collected.batch.local_stop
        assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}
        with ReadOnlyRepository(root) as repository:
            results = tuple(repository.iter_results("001"))
            assert {r.attempt_id for r in results} == attempts and len(results) == 2
            assert (
                repository.connection.execute(
                    "SELECT count(*) FROM batch_member_receipts"
                ).fetchone()[0]
                == 2
            )
            receipts = [
                BatchMemberReceiptV1.model_validate_json(row[0])
                for row in repository.connection.execute(
                    "SELECT record_json FROM batch_member_receipts"
                )
            ]
            assert all(
                (r.usage.input_tokens, r.usage.output_tokens) == (7, 3)
                for r in receipts
            )


@pytest.mark.asyncio
async def test_child_self_cancellation_has_no_external_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = batch_spec(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001")
    await asubmit_batch("001", store=root, spec=spec)
    original_status = MockBatchAdapter.status
    entered = asyncio.Event()

    async def self_cancel(
        self: MockBatchAdapter, submission_id: str, remote_id: str
    ) -> BatchStatusEvidenceV1:
        await original_status(self, submission_id, remote_id)
        current = asyncio.current_task()
        assert current is not None and current is not task
        entered.set()
        assert current.cancel()
        await asyncio.sleep(0)
        raise AssertionError("child self-cancellation was not delivered")

    monkeypatch.setattr(MockBatchAdapter, "status", self_cancel)
    with batch_clock(monkeypatch) as clock:
        task = asyncio.create_task(apoll_batch("001", store=root))
        try:
            with pytest.raises(asyncio.CancelledError):
                await clock.wait(task)
            assert entered.is_set() and task.cancelling() == 0
        finally:
            await clock.cleanup(task, asyncio.Event())
    _assert_invocation_state(root, active=False, stopped=False)


@pytest.mark.asyncio
async def test_actual_nested_payload_deadline_has_no_external_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Awaitable

    from judgmentllm.execution.native_batch import InvocationBudget

    spec = batch_spec(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001")
    entered, closing, release, drained = (asyncio.Event() for _ in range(4))
    payload_tasks: list[asyncio.Task[Any]] = []
    blocks: list[asyncio.Task[Any]] = []
    original_payload = InvocationBudget.payload
    original_call = InvocationBudget.call

    async def observed_payload(
        self: InvocationBudget, raw: bytes
    ) -> AsyncIterator[bytes]:
        current = asyncio.current_task()
        assert current is not None and current is not task
        payload_tasks.append(current)
        async for chunk in original_payload(self, raw):
            yield chunk

    async def observed_call(
        self: InvocationBudget, function: Callable[[], Awaitable[Any]]
    ) -> Any:
        if not payload_tasks or asyncio.current_task() is not payload_tasks[-1]:
            return await original_call(self, function)

        async def real_block_then_hold() -> bytes:
            raw = await function()
            assert isinstance(raw, bytes) and raw
            current = asyncio.current_task()
            assert current is not None and current is not task
            blocks.append(current)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closing.set()
                while not release_waiter.done():
                    try:
                        await asyncio.shield(release_waiter)
                    except asyncio.CancelledError:
                        continue
                assert await release_waiter
                drained.set()
                raise OSError("synthetic payload deadline cleanup")

        return await original_call(self, real_block_then_hold)

    monkeypatch.setattr(InvocationBudget, "payload", observed_payload)
    monkeypatch.setattr(InvocationBudget, "call", observed_call)
    with batch_clock(monkeypatch) as clock:
        release_waiter = asyncio.create_task(release.wait())
        task = asyncio.create_task(
            asubmit_batch(
                "001",
                store=root,
                spec=spec,
                options=BatchControlOptionsV1(deadline_seconds=0.15),
            )
        )
        try:
            await clock.gate(entered, task)
            assert len(payload_tasks) == len(blocks) == len(clock.budgets) == 1
            budget = clock.budgets[0]
            assert budget.steps >= 2 and budget.deadline == clock.start + 0.15
            _assert_invocation_state(
                root, active=True, stopped=False, forwards=0, jobs=0
            )
            clock.advance(0.15)
            await clock.gate(closing, task)
            assert not drained.is_set() and not release_waiter.done()
            release.set()
            assert (await clock.wait(task)).outcome == "partial"
            assert task.cancelling() == 0 and drained.is_set() and budget.closed
            assert payload_tasks[0].done() and blocks[0].done()
            assert (
                budget.result_rows == 0 and budget.steps <= budget.options.max_io_steps
            )
        finally:
            release.set()
            try:
                await clock.cleanup(task, release)
            finally:
                await clock.wait(asyncio.gather(release_waiter, return_exceptions=True))
    _assert_invocation_state(root, active=False, stopped=False, forwards=0, jobs=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["status", "eof"])
async def test_deadline_normal_evidence_is_not_external_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from collections.abc import AsyncGenerator

    from judgmentllm import get_batch_status

    spec = batch_spec(tmp_path)
    mock = mock_fixture(tmp_path, input_tokens=7, output_tokens=3)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    await asubmit_batch("001", store=root, spec=spec, mock=mock)
    entered, closing, release = (asyncio.Event() for _ in range(3))

    async def hold_completed_evidence() -> None:
        assert not entered.is_set()
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            closing.set()
            while not release_waiter.done():
                try:
                    await asyncio.shield(release_waiter)
                except asyncio.CancelledError:
                    continue
            assert await release_waiter

    original_status = MockBatchAdapter.status

    async def normal_status(
        self: MockBatchAdapter, submission_id: str, remote_id: str
    ) -> BatchStatusEvidenceV1:
        evidence = await original_status(self, submission_id, remote_id)
        await hold_completed_evidence()
        return evidence

    original_results = MockBatchAdapter.results

    class GenuineEOF:
        def __init__(self, stream: AsyncGenerator[bytes, None], source: str):
            self.stream, self.source = stream, source

        def __aiter__(self) -> "GenuineEOF":
            return self

        async def __anext__(self) -> bytes:
            try:
                return await anext(self.stream)
            except StopAsyncIteration:
                if self.source == "output":
                    await hold_completed_evidence()
                raise

        async def aclose(self) -> None:
            await self.stream.aclose()

    def genuine_results(
        self: MockBatchAdapter, *args: Any, **kwargs: Any
    ) -> GenuineEOF:
        assert isinstance(args[2], str)
        return GenuineEOF(original_results(self, *args, **kwargs), args[2])

    if boundary == "status":
        monkeypatch.setattr(MockBatchAdapter, "status", normal_status)
    else:
        monkeypatch.setattr(MockBatchAdapter, "results", genuine_results)
    action = apoll_batch if boundary == "status" else acollect_batch
    with batch_clock(monkeypatch) as clock:
        release_waiter = asyncio.create_task(release.wait())
        task = asyncio.create_task(
            action(
                "001",
                store=root,
                spec=spec,
                mock=mock,
                options=BatchControlOptionsV1(deadline_seconds=0.15),
            )
        )
        try:
            await clock.gate(entered, task)
            budget = clock.budgets[0]
            clock.advance(0.15)
            await clock.gate(closing, task)
            with ReadOnlyRepository(root) as repository:
                assert executor_is_active(repository, "001") is True
            release.set()
            result = await clock.wait(task)
            assert task.cancelling() == 0 and budget.closed
            assert not result.batch.local_stop and not result.batch.cancel_all
            assert result.outcome == ("ok" if boundary == "status" else "partial")
            assert budget.deadline == clock.start + 0.15
        finally:
            release.set()
            try:
                await clock.cleanup(task, release)
            finally:
                await clock.wait(asyncio.gather(release_waiter, return_exceptions=True))
    assert not get_batch_status("001", store=root).batch.local_stop
    assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}
    with ReadOnlyRepository(root) as repository:
        assert repository.get_owner("001") is None
        assert executor_is_active(repository, "001") is False
        assert repository.summary("001").results == (0 if boundary == "status" else 2)
        eof_count = repository.connection.execute(
            "SELECT count(*) FROM batch_facts WHERE kind='cursor' "
            "AND json_extract(record_json,'$.exhausted')=1"
        ).fetchone()[0]
        assert eof_count == (0 if boundary == "status" else 1)


@pytest.mark.asyncio
async def test_awaited_stream_close_cannot_mask_external_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import AsyncGenerator

    spec = batch_spec(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001")
    await asubmit_batch("001", store=root, spec=spec)
    entered, read_closing, read_release, close_entered, close_release = (
        asyncio.Event() for _ in range(5)
    )
    original_results = MockBatchAdapter.results

    class HeldClose:
        def __init__(self, stream: AsyncGenerator[bytes, None]):
            self.stream = stream

        def __aiter__(self) -> "HeldClose":
            return self

        async def __anext__(self) -> bytes:
            raw = await anext(self.stream)
            assert raw and not entered.is_set()
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                read_closing.set()
                while not read_waiter.done():
                    try:
                        await asyncio.shield(read_waiter)
                    except asyncio.CancelledError:
                        continue
                assert await read_waiter
            raise AssertionError("cancelled read returned a result row")

        async def aclose(self) -> None:
            await self.stream.aclose()
            close_entered.set()
            while not close_waiter.done():
                try:
                    await asyncio.shield(close_waiter)
                except asyncio.CancelledError:
                    continue
            assert await close_waiter
            raise OSError("synthetic late stream-close error")

    def held_results(self: MockBatchAdapter, *args: Any, **kwargs: Any) -> HeldClose:
        return HeldClose(original_results(self, *args, **kwargs))

    monkeypatch.setattr(MockBatchAdapter, "results", held_results)
    with batch_clock(monkeypatch) as clock:
        read_waiter = asyncio.create_task(read_release.wait())
        close_waiter = asyncio.create_task(close_release.wait())
        task = asyncio.create_task(acollect_batch("001", store=root, spec=spec))
        try:
            await clock.gate(entered, task)
            assert task.cancel()
            await clock.gate(read_closing, task)
            _assert_invocation_state(root, active=True, stopped=False)
            read_release.set()
            await clock.gate(close_entered, task)
            assert not close_waiter.done()
            _assert_invocation_state(root, active=True, stopped=False)
            close_release.set()
            with pytest.raises(asyncio.CancelledError):
                await clock.wait(task)
        finally:
            read_release.set()
            close_release.set()
            try:
                await clock.cleanup(task, read_release)
            finally:
                await clock.wait(
                    asyncio.gather(read_waiter, close_waiter, return_exceptions=True)
                )
    _assert_invocation_state(root, active=False, stopped=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["direct", "reconcile"])
async def test_accepted_ack_completion_cancellation_keeps_original_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    from judgmentllm import aresume_batch
    from judgmentllm.batch_models import BatchMemberReceiptV1, BatchOperationReceiptV1
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.hashing import canonical_bytes

    spec = batch_spec(tmp_path)
    mock = mock_fixture(
        tmp_path,
        input_tokens=7,
        output_tokens=3,
        create="uncertain" if entry == "reconcile" else "accepted",
    )
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    with ReadOnlyRepository(root) as repository:
        original_attempts = {a.attempt_id for a in repository.iter_attempts("001")}
    expected_intent: BatchOperationIntentV1 | None = None
    if entry == "reconcile":
        assert (
            await asubmit_batch("001", store=root, spec=spec, mock=mock)
        ).outcome == "partial"
        assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}
        with ReadOnlyRepository(root) as repository:
            row = repository.connection.execute(
                "SELECT record_json FROM batch_operations WHERE kind='create'"
            ).fetchone()
            expected_intent = BatchOperationIntentV1.model_validate_json(row[0])
            assert (
                repository.connection.execute(
                    "SELECT count(*) FROM batch_facts WHERE kind='operation' AND entity_id=?",
                    (expected_intent.instance_id,),
                ).fetchone()[0]
                == 0
            )
            store = ArtifactStore(repository.run_directory("001"), create=False)
            assert not store.reference_path(
                "batch/operations/" + expected_intent.instance_id + ".json"
            ).exists()
    captured: list[tuple[BatchOperationIntentV1, bytes]] = []
    observations: list[tuple[bool, bool, bool]] = []
    accepted_cancels: list[bool] = []
    failures: list[BaseException] = []
    phases: list[str] = []
    seen = asyncio.Event()
    original_invoke = MockBatchAdapter.invoke
    original_lookup = MockBatchAdapter.reconcile_operation
    lookup_calls = 0

    def completed_ack(raw: bytes, intent: BatchOperationIntentV1) -> None:
        current = asyncio.current_task()
        assert current is not None and current is not task
        assert not captured
        captured.append((intent, raw))
        phases.append("raw_returned")

        def cancel_before_parent_wakeup() -> None:
            try:
                assert current.done() and not current.cancelled()
                assert current.exception() is None and not task.done()
                assert clock.now < clock.budgets[0].deadline
                with ReadOnlyRepository(root) as repository:
                    store = ArtifactStore(repository.run_directory("001"), create=False)
                    reference = store.reference_path(
                        "batch/operations/" + intent.instance_id + ".json"
                    )
                    projection = repository.get_submission("001", intent.instance_id)
                    observations.append(
                        (
                            reference.is_file() and not reference.is_symlink(),
                            projection.certainty == "accepted",
                            repository.get_owner("001") is not None
                            and executor_is_active(repository, "001") is True,
                        )
                    )
                phases.append("cancel_before_parent_wakeup")
                before = task.cancelling()
                accepted_cancels.append(task.cancel())
                assert task.cancelling() == before + 1
            except BaseException as error:
                failures.append(error)
            finally:
                seen.set()

        # Queue now, before task completion queues the parent's existing wakeup.
        asyncio.get_running_loop().call_soon(cancel_before_parent_wakeup)

    async def observed_invoke(
        self: MockBatchAdapter,
        intent: BatchOperationIntentV1,
        payload: AsyncIterator[bytes],
        guard: Callable[[], None],
    ) -> bytes:
        assert entry == "direct" and not captured
        raw = await original_invoke(self, intent, payload, guard)
        if intent.kind == "create":
            completed_ack(raw, intent)
        return raw

    async def observed_lookup(
        self: MockBatchAdapter, intent: BatchOperationIntentV1
    ) -> bytes | None:
        nonlocal lookup_calls
        assert entry == "reconcile" and intent == expected_intent
        lookup_calls += 1
        assert lookup_calls == 1
        raw = await original_lookup(self, intent)
        assert raw is not None
        completed_ack(raw, intent)
        return raw

    def witness(stage: str, identity: str) -> None:
        if stage == "after_batch_stop":
            assert seen.is_set() and captured
            assert "after_batch_operation_commit" in phases
            phases.append(stage)
        elif (
            captured
            and identity == captured[0][0].instance_id
            and stage
            in ("after_batch_operation_receipt", "after_batch_operation_commit")
        ):
            phases.append(stage)

    monkeypatch.setattr(MockBatchAdapter, "invoke", observed_invoke)
    monkeypatch.setattr(MockBatchAdapter, "reconcile_operation", observed_lookup)
    action = asubmit_batch if entry == "direct" else aresume_batch
    with batch_clock(monkeypatch) as clock:
        task = asyncio.create_task(
            action(
                "001",
                store=root,
                spec=spec,
                mock=mock,
                _fault=witness,
                options=BatchControlOptionsV1(deadline_seconds=0.15),
            )
        )
        try:
            await clock.wait(asyncio.create_task(seen.wait()))
            assert not failures, "completion observer failed"
            assert accepted_cancels == [True]
            with pytest.raises(asyncio.CancelledError):
                await clock.wait(task)
            assert observations == [(True, True, True)]
            assert phases == [
                "raw_returned",
                "after_batch_operation_receipt",
                "after_batch_operation_commit",
                "cancel_before_parent_wakeup",
                "after_batch_stop",
            ]
        finally:
            await clock.cleanup(task, asyncio.Event())
    assert lookup_calls == (1 if entry == "reconcile" else 0)
    _assert_invocation_state(root, active=False, stopped=True)
    intent, raw = captured[0]
    receipt = BatchOperationReceiptV1.model_validate_json(raw)
    assert receipt.outcome == "accepted" and receipt.instance_id == intent.instance_id
    if expected_intent is not None:
        assert intent == expected_intent
    with ReadOnlyRepository(root) as repository:
        directory = repository.run_directory("001")
        store = ArtifactStore(directory, create=False)
        assert store.reference_path(
            "batch/operations/" + intent.instance_id + ".json"
        ).read_bytes() == canonical_bytes(receipt)
        submission = repository.get_submission("001", intent.instance_id)
        assert (
            submission.remote_id == receipt.remote_id
            and submission.certainty == "accepted"
        )
        assert len(tuple(repository.iter_submissions("001"))) == 2
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM submissions"
            ).fetchone()[0]
            == 1
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_facts WHERE kind='operation'"
            ).fetchone()[0]
            == 2
        )
        assert {
            a.attempt_id for a in repository.iter_attempts("001")
        } == original_attempts

    async def forbidden_invoke(
        self: MockBatchAdapter,
        intent: BatchOperationIntentV1,
        payload: AsyncIterator[bytes],
        guard: Callable[[], None],
    ) -> bytes:
        raise AssertionError("accepted original operation must not be sent again")

    async def forbidden_lookup(
        self: MockBatchAdapter, intent: BatchOperationIntentV1
    ) -> bytes | None:
        raise AssertionError("durable receipt must recover without another lookup")

    monkeypatch.setattr(MockBatchAdapter, "invoke", forbidden_invoke)
    monkeypatch.setattr(MockBatchAdapter, "reconcile_operation", forbidden_lookup)
    unavailable = mock.model_copy(update={"reconciliation_available": False})
    prior_results: tuple[Any, ...] | None = None
    for _ in range(2):
        result = await aresume_batch("001", store=root, spec=spec, mock=unavailable)
        assert result.batch.missing_members == 0 and result.batch.local_stop
        with ReadOnlyRepository(root) as repository:
            results = tuple(repository.iter_results("001"))
            assert (
                len(results) == 2
                and {r.attempt_id for r in results} == original_attempts
            )
            if prior_results is not None:
                assert results == prior_results
            prior_results = results
            receipts = [
                BatchMemberReceiptV1.model_validate_json(row[0])
                for row in repository.connection.execute(
                    "SELECT record_json FROM batch_member_receipts"
                )
            ]
            assert len(receipts) == 2
            assert all(
                (r.usage.input_tokens, r.usage.output_tokens) == (7, 3)
                for r in receipts
            )
            assert repository.summary("001").attempts == 2
            assert repository.summary("001").retry_operations == 0
            current = repository.get_submission("001", intent.instance_id)
            assert (
                current.submission_id,
                current.remote_id,
                current.correlation_id,
                current.payload_sha256,
                current.batch_uid,
            ) == (
                submission.submission_id,
                submission.remote_id,
                submission.correlation_id,
                submission.payload_sha256,
                submission.batch_uid,
            )
            assert repository.get_owner("001") is None
            assert executor_is_active(repository, "001") is False
    assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fresh_cancel", [False, True], ids=["no-fresh-cancel", "fresh-cancel"]
)
async def test_receipt_publication_failure_is_not_transport_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fresh_cancel: bool
) -> None:
    from judgmentllm import get_batch_status
    from judgmentllm.execution.native_batch import BatchSession
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.artifacts import ArtifactStore

    spec = batch_spec(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001")
    with ReadOnlyRepository(root) as repository:
        original_attempts = {a.attempt_id for a in repository.iter_attempts("001")}
        original_manifest = repository.get_manifest("001")
    received: list[BatchOperationIntentV1] = []
    owners: list[str] = []
    denials: list[str] = []
    requests: list[tuple[int, int]] = []
    stop_attempts: list[str] = []
    original_invoke = MockBatchAdapter.invoke
    original_reference = ArtifactStore.reference_path
    original_stop = BatchSession.stop

    async def accepted_create(
        self: MockBatchAdapter,
        intent: BatchOperationIntentV1,
        payload: AsyncIterator[bytes],
        guard: Callable[[], None],
    ) -> bytes:
        raw = await original_invoke(self, intent, payload, guard)
        if intent.kind == "create":
            assert not received
            received.append(intent)
            assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}
        return raw

    def denied_precheck(self: ArtifactStore, relative_path: str) -> Path:
        if (
            received
            and relative_path == "batch/operations/" + received[0].instance_id + ".json"
            and not denials
        ):
            current = asyncio.current_task()
            assert current is not None and current is not task
            with ReadOnlyRepository(root) as repository:
                owner = repository.get_owner("001")
                assert (
                    owner is not None and executor_is_active(repository, "001") is True
                )
                owners.append(owner.token)
            denials.append(relative_path)
            if fresh_cancel:
                before = task.cancelling()
                assert not task.done() and task.cancel()
                requests.append((before, task.cancelling()))
                assert requests[-1] == (before, before + 1)
            raise PermissionError("synthetic reference precheck failure detail")
        return original_reference(self, relative_path)

    def owned_stop(self: BatchSession, *, remote: bool) -> None:
        assert not remote
        with ReadOnlyRepository(root) as repository:
            owner = repository.get_owner("001")
            assert owner is not None and owner.token == owners[0]
            assert executor_is_active(repository, "001") is True
        stop_attempts.append(self.run_id)
        original_stop(self, remote=remote)

    monkeypatch.setattr(MockBatchAdapter, "invoke", accepted_create)
    monkeypatch.setattr(ArtifactStore, "reference_path", denied_precheck)
    monkeypatch.setattr(BatchSession, "stop", owned_stop)
    with batch_clock(monkeypatch) as clock:
        task = asyncio.create_task(asubmit_batch("001", store=root, spec=spec))
        try:
            with pytest.raises(LifecycleError) as raised:
                await clock.wait(task)
            assert raised.value.category == "execution" and not raised.value.retryable
            assert (
                raised.value.message
                == "durable run integrity or storage operation failed"
            )
            assert raised.value.__cause__ is None
            assert "synthetic reference precheck failure detail" not in str(
                raised.value
            )
            assert task.cancelling() == int(fresh_cancel)
            assert clock.now < clock.budgets[0].deadline
        finally:
            await clock.cleanup(task, asyncio.Event())
    assert len(received) == len(denials) == len(owners) == 1
    assert len(requests) == int(fresh_cancel)
    assert stop_attempts == (["001"] if fresh_cancel else [])
    _assert_invocation_state(root, active=False, stopped=fresh_cancel)
    status = get_batch_status("001", store=root)
    assert status.batch.accepted_jobs == 0 and status.batch.unresolved_operations == 1
    assert status.batch.missing_members == 2
    intent = received[0]
    with ReadOnlyRepository(root) as repository:
        assert repository.get_manifest("001") == original_manifest
        assert {
            a.attempt_id for a in repository.iter_attempts("001")
        } == original_attempts
        submission = repository.get_submission("001", intent.instance_id)
        assert submission.remote_id is None and submission.certainty != "accepted"
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM submissions"
            ).fetchone()[0]
            == 1
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_operations"
            ).fetchone()[0]
            == 2
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_facts WHERE kind='operation'"
            ).fetchone()[0]
            == 1
        )
        store = ArtifactStore(repository.run_directory("001"), create=False)
        assert not store.reference_path(denials[0]).exists()
    with closing(
        sqlite3.connect(
            (root / "mock-batch-service.sqlite3").as_uri() + "?mode=ro", uri=True
        )
    ) as service:
        jobs = service.execute("SELECT instance_id,cancelled FROM jobs").fetchall()
        assert jobs == [(intent.instance_id, 0)]
