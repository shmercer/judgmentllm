"""Native batch control never turns inconsistent evidence into new authority."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from test_batch_adapter_rejections import batch_session as batch_session
from test_batch_adapter_rejections import operation
from test_batch_preparation_rejections import corrupt_row
from test_batch_rejections import prepared_batch as prepared_batch

from judgmentllm import (
    BatchControlOptionsV1,
    BatchMockOptions,
    RetrySelection,
    RunSpecV2,
    aresume_batch,
    asubmit_batch,
)
from judgmentllm.batch_models import (
    BatchCursorV1,
    BatchFenceV1,
    BatchOperationIntentV1,
    BatchOperationReceiptV1,
    BatchShardV1,
    BatchStatusEvidenceV1,
)
from judgmentllm.execution import native_batch
from judgmentllm.execution.native_batch import (
    BatchSession,
    BudgetExhausted,
    InvocationBudget,
)
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.native_batch import read_reference, rows
from judgmentllm.persistence.ownership import executor_is_active
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.batch_mock import MockBatchAdapter
from judgmentllm.providers.protocol import PreparedRequestV2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["plain-manifest", "context"])
async def test_session_initialization_releases_ownership_on_rejection(
    prepared_batch: tuple[RunSpecV2, Path],
    fault: str,
) -> None:
    spec, root = prepared_batch
    identifier = "001"
    if fault == "plain-manifest":
        spec.execution.mode = "live"
        identifier = "plain"
        with prepare_run(spec, store=root, run_id=identifier):
            pass
    adapter = MockBatchAdapter(root / "mock-batch-service.sqlite3", BatchMockOptions())
    if fault == "context":
        adapter.identity = "other-service"
    with pytest.raises(IntegrityError):
        BatchSession(
            identifier, root, adapter, InvocationBudget(BatchControlOptionsV1())
        )
    with Repository(root) as repository:
        assert not executor_is_active(repository, identifier)


@pytest.mark.asyncio
async def test_scientific_retry_is_rejected_outside_explicit_resume(
    prepared_batch: tuple[RunSpecV2, Path],
) -> None:
    spec, root = prepared_batch
    with pytest.raises(LifecycleError) as error:
        await asubmit_batch(
            "001",
            store=root,
            spec=spec,
            retry=RetrySelection(task_ids=("unsubmitted",), operation_id="retry"),
        )
    assert error.value.category == "validation"
    with Repository(root) as repository:
        assert not executor_is_active(repository, "001")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["withheld", "observation", "conflict", "bridge"])
async def test_member_acceptance_rechecks_attribution_and_durable_bridge(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    import json

    from judgmentllm.batch_models import BatchMemberV1
    from judgmentllm.preparation.batch import reconstruct

    session = batch_session
    await session.submit()
    assert session.prepared is not None
    member = next(
        rows(session.repository, "batch_prepared_members", "001", BatchMemberV1)
    )
    submission = session.accepted_submission(member.shard_id)
    assert submission is not None
    adapter = cast(MockBatchAdapter, session.adapter)
    wire = reconstruct(session.prepared, adapter, member)[1]
    assert isinstance(wire, PreparedRequestV2)
    raw = adapter.source_row(wire, member.position, member.member_key, "output")
    assert raw is not None
    raw = raw.rstrip(b"\n")
    if fault == "withheld":

        def withheld(*args: object) -> None:
            return None

        monkeypatch.setattr(adapter, "screen_result", withheld)
        digest = session.save_member(member, submission, "f" * 64, 0, raw)
        assert digest == semantic_digest("batch-withheld-v1", ["f" * 64, 0])
        assert session.repository.summary("001").results == 0
    elif fault == "observation":
        fields = json.loads(raw)
        fields["response"]["observation_key"] = "other-observation"
        with pytest.raises(IntegrityError, match="sealed attribution"):
            session.save_member(
                member, submission, "f" * 64, 0, canonical_bytes(fields)
            )
    else:
        session.save_member(member, submission, "f" * 64, 0, raw)
        if fault == "conflict":
            fields = json.loads(raw)
            fields["usage"]["input_tokens"] = 999
            session.save_member(
                member, submission, "f" * 64, 0, canonical_bytes(fields)
            )
            assert (
                session.repository.connection.execute(
                    "SELECT count(*) FROM batch_facts WHERE kind='conflict'"
                ).fetchone()[0]
                == 1
            )
        else:
            manifest_sha = session.repository.get_manifest("001").artifact.sha256
            corrupt_row(
                session.root,
                "batch_member_receipts",
                "UPDATE batch_member_receipts SET receipt_sha256=?",
                (manifest_sha,),
            )
            with pytest.raises(IntegrityError, match="durable bridge"):
                session.save_member(member, submission, "f" * 64, 0, raw)
        assert session.repository.summary("001").results == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["source", "budget", "index-size", "prefix"])
async def test_collection_rejects_unverified_index_metadata_before_results(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    import json

    from judgmentllm.batch_models import BatchSourceCommitmentV1

    session = batch_session
    await session.submit()
    original = session.adapter.result_index
    if fault == "source":

        async def source(*args: object) -> str:
            return "f" * 64

        monkeypatch.setattr(session.adapter, "result_source", source)
        await session.collect()
    else:
        if fault == "budget":
            session.budget.bytes = (
                session.budget.options.max_result_rows
                * session.manifest.batch_context.limits.max_record_bytes
            )

        async def index(
            submission: str,
            remote: str,
            source: str,
            members: list[tuple[str, int, PreparedRequestV2]],
        ) -> tuple[BatchSourceCommitmentV1, bytes]:
            commitment, raw = await original(submission, remote, source, members)
            if fault == "index-size":
                return commitment, b" " * (
                    session.manifest.batch_context.limits.max_record_bytes + 1
                )
            if fault == "prefix":
                data = json.loads(raw)
                data["prefix_sha256"][0] = "f" * 64
                raw = canonical_bytes(data)
                commitment = commitment.model_copy(
                    update={"index_sha256": bytes_digest(raw)}
                )
            return commitment, raw

        monkeypatch.setattr(session.adapter, "result_index", index)
        with pytest.raises(BudgetExhausted if fault == "budget" else IntegrityError):
            await session.collect()
    assert session.repository.summary("001").results == 0


@pytest.mark.asyncio
async def test_replay_keeps_active_tasks_without_duplicate_results(
    batch_session: BatchSession,
) -> None:
    from judgmentllm.batch_models import BatchMemberV1

    session = batch_session
    member = next(
        rows(session.repository, "batch_prepared_members", "001", BatchMemberV1)
    )
    with session.repository.execution_transaction("001", session.token):
        session.repository.transition("request", "001", member.request_uid, "active")
        for task_id in session.repository.attempt_task_ids("001", member.attempt_id):
            session.repository.transition("task", "001", task_id, "active")
    await session.submit()
    await session.collect()
    shard = next(rows(session.repository, "batch_preparations", "001", BatchShardV1))
    submission = session.accepted_submission(shard.shard_id)
    assert submission is not None
    _, cursor = session.result_cursor(shard, submission, "output")
    assert cursor is not None and cursor.exhausted
    await session.collect()
    assert session.repository.summary("001").results == 1
    assert session.result_cursor(shard, submission, "output")[1] == cursor


@pytest.mark.asyncio
async def test_missing_fence_readback_never_authorizes_a_successor(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = batch_session
    adapter = cast(MockBatchAdapter, session.adapter)
    adapter.fixture = adapter.fixture.model_copy(update={"upload": "rejected"})
    shard = next(rows(session.repository, "batch_preparations", "001", BatchShardV1))
    data = session.store.read(shard.input)
    first = await session.mutate(shard, "upload", data)
    assert first is not None and first.outcome == "not_accepted"
    session.block_mutations = False

    async def unavailable(intent: BatchOperationIntentV1) -> None:
        return None

    monkeypatch.setattr(adapter, "reconcile_operation", unavailable)
    assert await session.mutate(shard, "upload", data) is None
    assert session.block_mutations
    assert (
        session.repository.connection.execute(
            "SELECT count(*) FROM batch_operations"
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_cleanup_exception_survives_invocation_cancellation() -> None:
    entered = asyncio.Event()

    async def operation() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("synthetic cleanup failure") from None

    async def invoke() -> None:
        await InvocationBudget(BatchControlOptionsV1()).call(operation)

    task = asyncio.create_task(invoke())
    await entered.wait()
    task.cancel()
    with pytest.raises(RuntimeError, match="cleanup failure"):
        await task


@pytest.mark.asyncio
async def test_completion_overlap_cannot_return_after_external_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import Awaitable

    original = asyncio.wait_for
    budgets: list[InvocationBudget] = []

    async def cancelled_completion(
        future: Awaitable[object], timeout: float | None
    ) -> object:
        result = await original(future, timeout)
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return result

    monkeypatch.setattr(asyncio, "wait_for", cancelled_completion)

    async def invoke() -> None:
        budget = InvocationBudget(BatchControlOptionsV1())
        budgets.append(budget)
        await budget.call(lambda: asyncio.sleep(0))
        pytest.fail("cancelled invocation returned a result")

    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(invoke())
    assert budgets[0].closed and budgets[0].external_cancelled()


@pytest.mark.asyncio
async def test_unexpected_shield_failure_is_propagated_with_owned_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import Awaitable

    children: list[asyncio.Task[object]] = []

    async def operation() -> None:
        task = asyncio.current_task()
        assert task is not None
        children.append(task)
        await asyncio.Event().wait()

    async def interrupted(future: Awaitable[object], timeout: float | None) -> object:
        await asyncio.sleep(0)
        raise TimeoutError("synthetic wait failure")

    async def failed_shield(future: Awaitable[object]) -> object:
        raise RuntimeError("synthetic shield failure")

    monkeypatch.setattr(asyncio, "wait_for", interrupted)
    monkeypatch.setattr(asyncio, "shield", failed_shield)
    try:
        with pytest.raises(RuntimeError, match="shield failure"):
            await InvocationBudget(BatchControlOptionsV1()).call(operation)
    finally:
        for task in children:
            task.cancel()
        await asyncio.gather(*children, return_exceptions=True)
    assert children and all(task.done() for task in children)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["unresolved", "fence", "stale-sender", "replay", "intent"]
)
async def test_control_evidence_cannot_create_or_repeat_an_unproven_forward(
    batch_session: BatchSession,
    fault: str,
) -> None:
    session = batch_session
    adapter = cast(MockBatchAdapter, session.adapter)
    intent = operation(adapter)
    if fault == "unresolved":
        session.accept_receipt(
            intent,
            BatchOperationReceiptV1(
                instance_id=intent.instance_id,
                intent_sha256=bytes_digest(canonical_bytes(intent)),
                outcome="unresolved",
            ),
        )
        assert (
            session.repository.connection.execute(
                "SELECT count(*) FROM batch_facts"
            ).fetchone()[0]
            == 0
        )
    elif fault == "fence":
        digest = bytes_digest(canonical_bytes(intent))
        receipt = BatchOperationReceiptV1(
            instance_id=intent.instance_id,
            intent_sha256=digest,
            outcome="not_accepted",
            fence=BatchFenceV1(
                service_id=adapter.identity,
                instance_id="f" * 64,
                intent_sha256=digest,
                sender_token=intent.owner_token,
                proof_sha256=semantic_digest(
                    "batch-fence-v1",
                    [adapter.identity, intent.instance_id, digest, intent.owner_token],
                ),
            ),
        )
        with pytest.raises(IntegrityError, match="earlier sender"):
            session.accept_receipt(intent, receipt)
    elif fault == "stale-sender":
        with pytest.raises(IntegrityError, match="stopped or stale"):
            session.forward_guard(intent)
    else:
        await session.submit()
        if fault == "intent":
            corrupt_row(
                session.root,
                "batch_operations",
                "UPDATE batch_operations SET record_json=json_set(record_json,'$.owner_token','other')",
            )
            with pytest.raises(IntegrityError, match="durable intent"):
                await session.recover_operations()
        else:
            before = session.repository.connection.execute(
                "SELECT count(*) FROM batch_facts"
            ).fetchone()[0]
            for row in session.repository.connection.execute(
                "SELECT record_json FROM batch_operations"
            ):
                intent = BatchOperationIntentV1.model_validate_json(row[0])
                receipt = session.operation_receipt(intent)
                assert receipt is not None
                session.accept_receipt(intent, receipt)
            assert (
                session.repository.connection.execute(
                    "SELECT count(*) FROM batch_facts"
                ).fetchone()[0]
                == before
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["submit", "collect", "payload"])
async def test_unfinished_shards_require_the_original_fixture_and_bytes(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    session = batch_session
    adapter = cast(MockBatchAdapter, session.adapter)
    if action == "collect":
        await session.submit()
    if action == "payload":
        original = native_batch.encode_row

        def changed_row(key: str, wire: PreparedRequestV2) -> bytes:
            return original(key, wire) + b" "

        monkeypatch.setattr(native_batch, "encode_row", changed_row)
    else:
        adapter.fixture = adapter.fixture.model_copy(update={"input_tokens": 9})
    with pytest.raises(IntegrityError):
        await (session.collect() if action == "collect" else session.submit())
    assert session.repository.summary("001").results == 0


@pytest.mark.asyncio
async def test_mismatched_status_is_withheld_without_inventing_a_terminal_job(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = batch_session
    await session.submit()

    async def status(submission_id: str, remote_id: str) -> BatchStatusEvidenceV1:
        return BatchStatusEvidenceV1(
            submission_id="f" * 64, remote_id=remote_id, state="completed"
        )

    monkeypatch.setattr(session.adapter, "status", status)
    await session.poll()
    assert (
        session.repository.connection.execute(
            "SELECT count(*) FROM batch_facts WHERE kind='withheld'"
        ).fetchone()[0]
        == 1
    )
    assert (
        session.repository.connection.execute(
            "SELECT count(*) FROM batch_facts WHERE kind='status'"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["cursor", "member", "withheld"])
async def test_collection_requires_matching_local_and_service_evidence(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    session = batch_session
    await session.submit()
    shard = next(rows(session.repository, "batch_preparations", "001", BatchShardV1))
    submission = session.accepted_submission(shard.shard_id)
    assert submission is not None
    if fault == "cursor":
        source_id, _ = session.result_cursor(shard, submission, "output")
        session.write_fact(
            "cursor",
            source_id,
            BatchCursorV1(
                run_id="other",
                submission_id=submission.submission_id,
                source_id=source_id,
                position=0,
                prefix_sha256="f" * 64,
            ),
        )
        with pytest.raises(IntegrityError, match="cursor differs"):
            session.result_cursor(shard, submission, "output")
    elif fault == "member":
        await session.collect()
        original = read_reference

        def missing(*args: object) -> object:
            if str(args[2]).startswith("batch/members/"):
                return None
            return original(*args)  # type: ignore[arg-type]

        monkeypatch.setattr(native_batch, "read_reference", missing)
        with pytest.raises(IntegrityError, match="matching evidence"):
            await session.collect()
    else:

        def withheld(*args: object) -> None:
            return None

        monkeypatch.setattr(session.adapter, "screen_result", withheld)
        await session.collect()
        assert session.repository.summary("001").results == 0
        assert (
            session.repository.connection.execute(
                "SELECT count(*) FROM batch_facts WHERE kind='withheld'"
            ).fetchone()[0]
            > 0
        )


@pytest.mark.asyncio
async def test_unterminated_record_is_rejected_before_cursor_or_result_commit(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = batch_session
    await session.submit()
    session.budget.options = session.budget.options.model_copy(
        update={"max_result_rows": 2}
    )

    async def oversized(*args: object) -> AsyncIterator[bytes]:
        for _ in range(17):
            yield b"x" * 65_536

    monkeypatch.setattr(session.adapter, "results", oversized)
    with pytest.raises(IntegrityError, match="record exceeds"):
        await session.collect()
    assert session.repository.summary("001").results == 0


@pytest.mark.asyncio
async def test_exhausted_row_budget_does_not_commit_an_extra_member(
    batch_session: BatchSession,
) -> None:
    session = batch_session
    await session.submit()
    session.budget.result_rows = session.budget.options.max_result_rows
    with pytest.raises(BudgetExhausted):
        await session.collect()
    assert session.repository.summary("001").results == 0


@pytest.mark.asyncio
async def test_cancellation_during_final_storage_failure_still_records_stop(
    prepared_batch: tuple[RunSpecV2, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, root = prepared_batch

    def finish(session: BatchSession) -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        raise OSError("synthetic final storage failure")

    monkeypatch.setattr(BatchSession, "finish", finish)
    task = asyncio.create_task(aresume_batch("001", store=root, spec=spec))
    with pytest.raises(asyncio.CancelledError):
        await task
    from judgmentllm import get_batch_status

    assert get_batch_status("001", store=root).batch.local_stop
    with Repository(root) as repository:
        assert not executor_is_active(repository, "001")


@pytest.mark.asyncio
async def test_completed_error_member_still_requires_unread_source_eof(
    tmp_path: Path,
) -> None:
    from test_native_batch import batch_spec, mock_fixture, service_counts

    from judgmentllm import prepare_batch

    spec = batch_spec(tmp_path, responses=1)
    mock = mock_fixture(tmp_path, rows=[{"outcome": "provider_error"}])
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    first = await aresume_batch(
        "001",
        store=root,
        spec=spec,
        mock=mock,
        options=BatchControlOptionsV1(max_result_rows=1),
    )
    assert first.batch.missing_members == 0 and first.batch.recovery_required
    before = service_counts(root)
    final = await aresume_batch("001", store=root, spec=spec, mock=mock)
    assert not final.batch.recovery_required
    assert service_counts(root) == before
    with Repository(root) as repository:
        assert [result.status for result in repository.iter_results("001")] == [
            "errored"
        ]


@pytest.mark.asyncio
async def test_cancellation_after_collection_records_stop_before_returning(
    prepared_batch: tuple[RunSpecV2, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, root = prepared_batch
    original = BatchSession.collect

    async def collected(session: BatchSession) -> None:
        await original(session)
        task = asyncio.current_task()
        assert task is not None
        task.cancel()

    monkeypatch.setattr(BatchSession, "collect", collected)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.create_task(aresume_batch("001", store=root, spec=spec))
    from judgmentllm import get_batch_status

    assert get_batch_status("001", store=root).batch.local_stop
    with Repository(root) as repository:
        assert repository.summary("001").results == 1
        assert not executor_is_active(repository, "001")


@pytest.mark.asyncio
async def test_internal_stop_control_opens_no_sources_or_mutations(
    prepared_batch: tuple[RunSpecV2, Path],
) -> None:
    from test_native_batch import service_counts

    from judgmentllm.native_batch import _control  # pyright: ignore[reportPrivateUsage]

    _, root = prepared_batch
    status = await _control("stop", "001", store=root)
    assert status.batch.local_stop
    assert service_counts(root) == {"forwards": 0, "jobs": 0, "cancelled": 0}
