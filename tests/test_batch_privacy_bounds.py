"""Pre-hash privacy, finite I/O, immutable mutation caps and real sender fences."""

import asyncio
import json
import sqlite3
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import closing
from pathlib import Path

import pytest
from batch_clock import batch_clock
from batch_privacy_audit import PrivacyAudit
from test_native_batch import batch_spec, mock_fixture, service_counts

from judgmentllm import (
    BatchControlOptionsV1,
    acollect_batch,
    asubmit_batch,
    cancel_batch,
    collect_batch,
    get_batch_status,
    prepare_batch,
    resume_batch,
    submit_batch,
)
from judgmentllm.batch_models import (
    BatchCursorV1,
    BatchMemberReceiptV1,
    BatchOperationIntentV1,
    BatchOperationReceiptV1,
    BatchSourceIndexV1,
)
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.native_batch import latest
from judgmentllm.persistence.ownership import ExecutorOwnership, executor_is_active
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.preparation.manifest import (
    ExecutableRunManifestV12,
    parse_executable_manifest,
)
from judgmentllm.providers.batch_mock import MockBatchAdapter


def accepted_job(root: Path, attempts: set[str]) -> tuple[str, str]:
    assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}
    with ReadOnlyRepository(root) as repository:
        assert {a.attempt_id for a in repository.iter_attempts("001")} == attempts
        assert len(attempts) == 2
        records = repository.connection.execute(
            "SELECT o.kind,o.instance_id,o.intent_sha256,f.record_json "
            "FROM batch_operations o JOIN batch_facts f ON f.run_id=o.run_id "
            "AND f.kind='operation' AND f.entity_id=o.instance_id ORDER BY o.kind"
        ).fetchall()
        assert [r[0] for r in records] == ["create", "upload"]
        receipts: list[BatchOperationReceiptV1] = []
        for row in records:
            receipt = BatchOperationReceiptV1.model_validate_json(row[3])
            assert receipt.outcome == "accepted"
            assert (receipt.instance_id, receipt.intent_sha256) == (row[1], row[2])
            receipts.append(receipt)
        create = receipts[0]
        assert (
            repository.get_submission("001", create.instance_id).remote_id
            == create.remote_id
        )
        raw = (repository.run_directory("001") / "manifest.json").read_bytes()
        manifest = parse_executable_manifest(raw)
        assert isinstance(manifest, ExecutableRunManifestV12)
        assert canonical_bytes(manifest) == raw
        return create.instance_id, manifest.batch_context.fixture_sha256


def assert_source_cursors(root: Path, submission: str, fixture: str) -> None:
    with (
        ReadOnlyRepository(root) as repository,
        closing(
            sqlite3.connect(
                (root / "mock-batch-service.sqlite3").as_uri() + "?mode=ro", uri=True
            )
        ) as service,
    ):
        for kind, position in (("output", 2), ("error", 0)):
            source = semantic_digest(
                "batch-result-source-v1", [submission, kind, fixture]
            )
            cursor = latest(repository, "001", "cursor", source, BatchCursorV1)
            assert cursor is not None and cursor.exhausted
            assert cursor.submission_id == submission and cursor.position == position
            row = service.execute(
                "SELECT index_sha256,record_json FROM result_indexes WHERE source_id=?",
                (source,),
            ).fetchone()
            assert row is not None
            index = BatchSourceIndexV1.model_validate_json(row[1])
            assert cursor.index_sha256 == row[0] == bytes_digest(canonical_bytes(index))
            assert cursor.prefix_sha256 == index.prefix_sha256[position]
            assert len(index.prefix_sha256) == position + 1


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "fault",
    ["unsafe_control", "unsafe_result", "unsafe_key", "unsafe_cursor", "raw_marker"],
)
def test_screening_precedes_all_durable_content_and_hashes(
    tmp_path: Path, capture: str, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = batch_spec(tmp_path, capture=capture)
    mock = mock_fixture(
        tmp_path,
        **(
            {"rows": [{"raw_marker": "PROHIBITED_BATCH_MARKER_SECRET"}]}
            if fault == "raw_marker"
            else {fault: True}
        ),
    )
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    audit = PrivacyAudit(root)
    audit.install(monkeypatch)
    audit.inspect("prepared", "")
    with ReadOnlyRepository(root) as repository:
        attempts = {a.attempt_id for a in repository.iter_attempts("001")}
    submitted = audit.checked(
        lambda: submit_batch(
            "001", store=root, spec=spec, mock=mock, _fault=audit.inspect
        )
    )
    assert submitted.outcome == "ok" and submitted.batch.accepted_jobs == 1
    assert submitted.batch.unresolved_operations == 0
    submission, fixture = accepted_job(root, attempts)
    status = audit.checked(
        lambda: collect_batch(
            "001", store=root, spec=spec, mock=mock, _fault=audit.inspect
        )
    )
    audit.inspect("finished", "")
    assert "after_batch_operation_receipt" in audit.checkpoints
    assert audit.sources == ["output", "error"]
    accepted_job(root, attempts)
    assert status.batch.missing_members == (
        2 if fault in ("unsafe_key", "unsafe_cursor") else 0
    )
    with ReadOnlyRepository(root) as repository:
        records = [
            BatchMemberReceiptV1.model_validate_json(r[0])
            for r in repository.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        results = tuple(repository.iter_results("001"))
        withheld = repository.connection.execute(
            "SELECT entity_id,record_json FROM batch_facts WHERE kind='withheld'"
        ).fetchall()
        if fault in ("unsafe_key", "unsafe_cursor"):
            assert status.outcome == "partial"
            assert records == [] and results == ()
            assert (
                repository.connection.execute(
                    "SELECT count(*) FROM observations"
                ).fetchone()[0]
                == 0
            )
            assert len(withheld) == 2
            assert all(
                json.loads(row[1])["code"] == "unsafe_evidence" for row in withheld
            )
            if fault == "unsafe_key":
                source = semantic_digest(
                    "batch-result-source-v1", [submission, "output", fixture]
                )
                assert {r[0] for r in withheld} == {source + ":0", source + ":1"}
                assert set(audit.reads) == {("output", 0), ("output", 1)}
            else:
                assert audit.reads == []
                assert {r[0] for r in withheld} == {
                    semantic_digest(
                        "batch-result-source-v1", [submission, kind, fixture]
                    )
                    for kind in ("output", "error")
                }
                assert (
                    repository.connection.execute(
                        "SELECT count(*) FROM batch_facts WHERE kind='cursor'"
                    ).fetchone()[0]
                    == 0
                )
        else:
            assert status.outcome == "ok" and not status.batch.recovery_required
            assert len(records) == len(results) == 2 and withheld == []
            assert {r.identity.attempt_id for r in records} == attempts
            assert {r.attempt_id for r in results} == attempts
            assert all(r.status == "complete" for r in results)
            assert set(audit.reads) == {("output", 0), ("output", 1)}
            store = ArtifactStore(repository.run_directory("001"), create=False)
            for receipt in records:
                assert receipt.identity.submission_id == submission
                assert receipt.capture == capture
                assert (receipt.retained.relative_path is None) == (
                    capture == "minimal"
                )
                store.verify(receipt.retained)
                if fault in ("unsafe_result", "raw_marker"):
                    assert (
                        receipt.original is None and receipt.disposition == "redacted"
                    )
                else:
                    assert (
                        receipt.original is not None and receipt.disposition == "exact"
                    )
        assert not executor_is_active(repository, "001")
    if fault != "unsafe_cursor":
        assert audit.originals
        assert_source_cursors(root, submission, fixture)


@pytest.mark.parametrize(
    "error_type",
    [AssertionError, TimeoutError, OSError],
    ids=["assertion", "timeout", "oserror"],
)
def test_inspector_failure_cannot_become_a_partial_control_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    spec, mock = batch_spec(tmp_path), mock_fixture(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    audit = PrivacyAudit(root)
    failure = error_type("synthetic inspector failure")

    def fail() -> None:
        raise failure

    monkeypatch.setattr(audit, "scan", fail)
    with pytest.raises(error_type) as raised:
        audit.checked(
            lambda: submit_batch(
                "001", store=root, spec=spec, mock=mock, _fault=audit.inspect
            )
        )
    assert raised.value is failure
    assert audit.failures and all(error is failure for error in audit.failures)
    assert service_counts(root) == {"forwards": 0, "jobs": 0, "cancelled": 0}


@pytest.mark.parametrize("failure", ["budget", "timeout", "oserror"])
def test_pre_screening_partial_does_not_prove_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    spec, mock = batch_spec(tmp_path), mock_fixture(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    audit = PrivacyAudit(root)
    audit.install(monkeypatch)
    with ReadOnlyRepository(root) as repository:
        attempts = {a.attempt_id for a in repository.iter_attempts("001")}
    submitted = audit.checked(
        lambda: submit_batch(
            "001", store=root, spec=spec, mock=mock, _fault=audit.inspect
        )
    )
    assert submitted.outcome == "ok" and submitted.batch.accepted_jobs == 1
    accepted_job(root, attempts)
    entered: list[str] = []

    async def unavailable(self: MockBatchAdapter, submission: str, source: str) -> str:
        entered.append(source)
        if failure == "timeout":
            raise TimeoutError("synthetic pre-screening timeout")
        raise OSError("synthetic pre-screening retrieval failure")

    if failure != "budget":
        monkeypatch.setattr(MockBatchAdapter, "result_source", unavailable)
    result = audit.checked(
        lambda: collect_batch(
            "001",
            store=root,
            spec=spec,
            mock=mock,
            _fault=audit.inspect,
            options=BatchControlOptionsV1(max_io_steps=1)
            if failure == "budget"
            else None,
        )
    )
    assert result.outcome == "partial" and result.batch.missing_members == 2
    assert entered == ([] if failure == "budget" else ["output"])
    assert audit.originals == audit.reads == audit.failures == []
    accepted_job(root, attempts)
    with ReadOnlyRepository(root) as repository:
        assert tuple(repository.iter_results("001")) == ()
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 0
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_facts WHERE kind IN ('cursor','withheld')"
            ).fetchone()[0]
            == 0
        )


def test_privacy_audit_defers_only_the_live_windows_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "store"
    prepare_batch(batch_spec(tmp_path), store=root, run_id="001")
    audit = PrivacyAudit(root)
    sqlite_opens: list[bool] = []

    def forbidden_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        sqlite_opens.append(True)
        raise AssertionError("SQLite opened during privacy inspection")

    def owned(inspector: PrivacyAudit) -> None:
        with Repository(root) as repository, ExecutorOwnership(repository, "001"):
            assert executor_is_active(repository, "001") is True
            with monkeypatch.context() as patch:
                patch.setattr(sqlite3, "connect", forbidden_connect)
                inspector.inspect("owned", "")
                assert sqlite_opens == []

    audit.checked(lambda: owned(audit))
    assert audit.deferred == (
        {audit.executor_lock} if sys.platform == "win32" else set()
    )
    assert audit.executor_lock.read_bytes() == b"\0"
    original = Path.read_bytes
    foreign = root / "pending.tmp"
    foreign.write_bytes(b"synthetic")
    for path in (audit.executor_lock, foreign):
        fresh = PrivacyAudit(root)
        unowned = PrivacyAudit(root) if path == audit.executor_lock else None

        def denied(self: Path, denied_path: Path = path) -> bytes:
            if self == denied_path:
                raise PermissionError("synthetic access denial")
            return original(self)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_bytes", denied)
            with pytest.raises(AssertionError, match="unexpected unreadable"):
                if path == audit.executor_lock:
                    fresh.inspect("released", "")
                else:
                    fresh.checked(lambda inspector=fresh: owned(inspector))
            if unowned is not None and sys.platform == "win32":
                with pytest.raises(
                    AssertionError, match="deferred executor lock is not held"
                ):
                    unowned.checked(
                        lambda inspector=unowned: inspector.inspect("unowned", "")
                    )
        assert audit.executor_lock.read_bytes() == b"\0"
    audit.executor_lock.write_bytes(b"X")
    try:
        with pytest.raises(AssertionError):
            PrivacyAudit(root).inspect("released", "")
    finally:
        audit.executor_lock.write_bytes(b"\0")


def test_rejections_require_explicit_invocations_and_stop_at_three(
    tmp_path: Path,
) -> None:
    spec = batch_spec(tmp_path)
    mock = mock_fixture(tmp_path, create="rejected")
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    for number in range(1, 4):
        result = submit_batch("001", store=root, spec=spec, mock=mock)
        assert result.outcome == "partial"
        assert service_counts(root)["forwards"] == number + 1
    submit_batch("001", store=root, spec=spec, mock=mock)
    assert service_counts(root) == {"forwards": 4, "jobs": 0, "cancelled": 0}
    with ReadOnlyRepository(root) as repository:
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_operations WHERE kind='create'"
            ).fetchone()[0]
            == 3
        )
        assert repository.summary("001").attempts == 2


@pytest.mark.asyncio
async def test_delayed_stale_sender_is_rejected_before_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = batch_spec(tmp_path)
    mock = mock_fixture(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    entered, release = asyncio.Event(), asyncio.Event()
    pending: list[tuple[MockBatchAdapter, BatchOperationIntentV1]] = []
    original = MockBatchAdapter.invoke

    async def delayed(
        self: MockBatchAdapter,
        intent: BatchOperationIntentV1,
        payload: AsyncIterator[bytes],
        guard: Callable[[], None],
    ) -> bytes:
        if intent.kind == "create" and intent.sequence == 1:
            pending.append((self, intent))
            entered.set()
            await release.wait()
        return await original(self, intent, payload, guard)

    monkeypatch.setattr(MockBatchAdapter, "invoke", delayed)
    task = asyncio.create_task(asubmit_batch("001", store=root, spec=spec, mock=mock))
    await asyncio.wait_for(entered.wait(), 5)
    adapter, intent = pending[0]
    await adapter.fence_unsent(intent)
    release.set()
    with pytest.raises(LifecycleError):
        await task
    assert service_counts(root) == {"forwards": 1, "jobs": 0, "cancelled": 0}
    # Learning the proof is one invocation; a later explicit continuation may send.
    await asubmit_batch("001", store=root, spec=spec, mock=mock)
    assert service_counts(root)["forwards"] == 1
    await asubmit_batch("001", store=root, spec=spec, mock=mock)
    assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}


@pytest.mark.asyncio
async def test_deadline_keeps_ownership_until_slow_sender_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = batch_spec(tmp_path)
    mock = mock_fixture(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    entered, closing, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = MockBatchAdapter.invoke

    async def slow(
        self: MockBatchAdapter,
        intent: BatchOperationIntentV1,
        payload: AsyncIterator[bytes],
        guard: Callable[[], None],
    ) -> bytes:
        if intent.kind != "create":
            return await original(self, intent, payload, guard)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closing.set()
            await drained.wait()
        raise AssertionError("cancelled sender must not return acceptance")

    monkeypatch.setattr(MockBatchAdapter, "invoke", slow)
    with batch_clock(monkeypatch) as clock:
        task = asyncio.create_task(
            asubmit_batch(
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
            assert budget.options.deadline_seconds == 0.15
            assert budget.deadline == clock.start + 0.15
            clock.advance(0.15)
            await clock.gate(closing, task)
            with ReadOnlyRepository(root) as repository:
                assert executor_is_active(repository, "001") is True
            assert not task.done() and clock.now >= budget.deadline
            assert budget.deadline == clock.start + 0.15
            drained.set()
            assert (await clock.wait(task)).outcome == "partial"
            assert budget.closed
        finally:
            await clock.cleanup(task, drained)
    with ReadOnlyRepository(root) as repository:
        assert executor_is_active(repository, "001") is False
    assert service_counts(root)["forwards"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["submit", "collect"])
async def test_deadline_before_adapter_entry_has_no_result_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    spec = batch_spec(tmp_path)
    mock = mock_fixture(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    if action == "collect":
        await asubmit_batch("001", store=root, spec=spec, mock=mock)
    entered: list[bool] = []

    async def forbidden(*args: object, **kwargs: object) -> bytes:
        entered.append(True)
        raise AssertionError("expired invocation entered an adapter operation")

    monkeypatch.setattr(
        MockBatchAdapter, "invoke" if action == "submit" else "result_index", forbidden
    )
    with batch_clock(monkeypatch, expire_before_entry=True) as clock:
        operation = asubmit_batch if action == "submit" else acollect_batch
        task = asyncio.create_task(
            operation(
                "001",
                store=root,
                spec=spec,
                mock=mock,
                options=BatchControlOptionsV1(deadline_seconds=0.15),
            )
        )
        try:
            result = await clock.wait(task)
            assert result.outcome == "partial" and entered == []
            assert len(clock.budgets) == 1
            assert clock.budgets[0].deadline == clock.start + 0.15
            assert clock.budgets[0].result_rows == 0
        finally:
            await clock.cleanup(task, asyncio.Event())
    assert service_counts(root) == {
        "forwards": 0 if action == "submit" else 2,
        "jobs": 0 if action == "submit" else 1,
        "cancelled": 0,
    }
    with ReadOnlyRepository(root) as repository:
        assert not executor_is_active(repository, "001")
        assert repository.summary("001").results == 0
        for table in ("batch_member_receipts", "observations"):
            assert (
                repository.connection.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                == 0
            )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_facts WHERE kind='cursor'"
            ).fetchone()[0]
            == 0
        )


def test_io_step_exhaustion_cannot_forward_or_reset_ambiguity(tmp_path: Path) -> None:
    spec = batch_spec(tmp_path)
    mock = mock_fixture(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    result = submit_batch(
        "001",
        store=root,
        spec=spec,
        mock=mock,
        options=BatchControlOptionsV1(max_io_steps=1),
    )
    assert result.outcome == "partial" and result.batch.unresolved_operations == 1
    assert service_counts(root)["forwards"] == 0
    resume_batch("001", store=root, spec=spec, mock=mock)
    assert service_counts(root)["forwards"] == 0
    assert get_batch_status("001", store=root).batch.unresolved_operations == 1


@pytest.mark.parametrize("kind", ["upload", "cancel"])
def test_upload_and_cancel_caps_survive_explicit_continuations(
    tmp_path: Path, kind: str
) -> None:
    spec = batch_spec(tmp_path)
    mock = mock_fixture(tmp_path, **{kind: "rejected"})
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    if kind == "cancel":
        submit_batch("001", store=root, spec=spec, mock=mock)
    action = cancel_batch if kind == "cancel" else submit_batch
    for _ in range(4):
        action("001", store=root, spec=spec, mock=mock)
    assert service_counts(root)["forwards"] == (5 if kind == "cancel" else 3)
    with ReadOnlyRepository(root) as repository:
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_operations WHERE kind=?", (kind,)
            ).fetchone()[0]
            == 3
        )
        assert repository.summary("001").attempts == 2
