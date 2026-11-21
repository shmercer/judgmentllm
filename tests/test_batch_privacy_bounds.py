"""Pre-hash privacy, finite I/O, immutable mutation caps and real sender fences."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path

import pytest
from test_native_batch import batch_spec, mock_fixture, service_counts

from judgmentllm import (
    BatchControlOptionsV1,
    asubmit_batch,
    cancel_batch,
    collect_batch,
    get_batch_status,
    prepare_batch,
    resume_batch,
    submit_batch,
)
from judgmentllm.batch_models import BatchOperationIntentV1
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.persistence.ownership import executor_is_active
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.providers.batch_mock import MockBatchAdapter
from judgmentllm.providers.protocol import PreparedRequestV2


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
    seen: list[bytes] = []
    original = MockBatchAdapter.screen_result

    def screen(
        self: MockBatchAdapter, raw: bytes, keys: Mapping[str, PreparedRequestV2]
    ):
        seen.append(raw)
        return original(self, raw, keys)

    monkeypatch.setattr(MockBatchAdapter, "screen_result", screen)
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    submit_batch("001", store=root, spec=spec, mock=mock)

    def inspect(stage: str, identity: str) -> None:
        for path in root.rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
                for marker in (
                    b"PROHIBITED_BATCH_CONTROL_SECRET",
                    b"PROHIBITED_BATCH_RESULT_SECRET",
                    b"PROHIBITED_BATCH_MEMBER_SECRET",
                    b"PROHIBITED_BATCH_MARKER_SECRET",
                ):
                    assert marker not in raw
                    assert hashlib.sha256(marker).hexdigest().encode() not in raw
                if fault in ("unsafe_result", "unsafe_key", "raw_marker"):
                    assert all(
                        hashlib.sha256(body).hexdigest().encode() not in raw
                        for body in seen
                    )

    status = collect_batch("001", store=root, spec=spec, mock=mock, _fault=inspect)
    inspect("finished", "")
    assert status.batch.missing_members == (
        2 if fault in ("unsafe_key", "unsafe_cursor") else 0
    )
    if fault in ("unsafe_result", "raw_marker"):
        with ReadOnlyRepository(root) as repository:
            records = [
                json.loads(r[0])
                for r in repository.connection.execute(
                    "SELECT record_json FROM batch_member_receipts"
                )
            ]
            assert all(
                r["original"] is None and r["disposition"] == "redacted"
                for r in records
            )
    if capture == "minimal":
        with ReadOnlyRepository(root) as repository:
            for row in repository.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            ):
                assert json.loads(row[0])["retained"]["relative_path"] is None


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
    task = asyncio.create_task(
        asubmit_batch(
            "001",
            store=root,
            spec=spec,
            mock=mock,
            options=BatchControlOptionsV1(deadline_seconds=0.15),
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(closing.wait(), 5)
    with ReadOnlyRepository(root) as repository:
        assert executor_is_active(repository, "001") is True
    assert not task.done()
    drained.set()
    assert (await task).outcome == "partial"
    with ReadOnlyRepository(root) as repository:
        assert executor_is_active(repository, "001") is False
    assert service_counts(root)["forwards"] == 1


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
