"""Authenticated ranges make progress with the same bounded fresh invocation."""

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from test_batch_recovery import setup
from test_batch_source_recovery import cursors, member_evidence
from test_native_batch import batch_spec, mock_fixture, service_counts

import judgmentllm.execution.native_batch as core
from judgmentllm import (
    BatchControlOptionsV1,
    acollect_batch,
    asubmit_batch,
    collect_batch,
    get_batch_status,
    prepare_batch,
    submit_batch,
)
from judgmentllm.batch_models import BatchCursorV1, BatchSourceCommitmentV1
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.native_batch import fact, latest
from judgmentllm.persistence.ownership import ExecutorOwnership, executor_is_active
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.providers.batch_mock import MockBatchAdapter
from judgmentllm.providers.protocol import PreparedRequestV2

COLLECT = """
import json,sys
from pathlib import Path
from judgmentllm import BatchMockOptions, collect_batch
from judgmentllm.execution.native_batch import InvocationBudget
from judgmentllm.providers.batch_mock import MockBatchAdapter
directory=Path(sys.argv[1]); budgets=[]; reads=0
initialize=InvocationBudget.__init__
def capture(self, options):
    initialize(self, options)
    budgets.append(self)
InvocationBudget.__init__=capture
read=MockBatchAdapter.read_row
async def counted(self,*arguments):
    global reads
    reads+=1
    return await read(self,*arguments)
MockBatchAdapter.read_row=counted
result=collect_batch('001',store=directory/'store',mock=BatchMockOptions(fixture_path=directory/'batch-fixture.json'))
budget=budgets[0]
print(json.dumps({'status':result.model_dump(mode='json'),'steps':budget.steps,
    'bytes':budget.bytes,'result_rows':budget.result_rows,'reads':reads,
    'options':budget.options.model_dump(mode='json')}))
"""


def fresh_collect(directory: Path, timeout: float = 30) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-B", "-c", COLLECT, str(directory)],
        cwd=directory,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    return json.loads(completed.stdout)


def source_progress(root: Path) -> tuple[int, int]:
    with ReadOnlyRepository(root) as repository:
        records = [
            json.loads(row[0])
            for row in repository.connection.execute(
                "SELECT f.record_json FROM batch_facts f WHERE f.kind='cursor' "
                "AND NOT EXISTS(SELECT 1 FROM batch_facts n WHERE n.run_id=f.run_id "
                "AND n.kind=f.kind AND n.entity_id=f.entity_id AND n.fact_seq>f.fact_seq)"
            )
        ]
        return sum(row["position"] for row in records), sum(
            row.get("exhausted", False) for row in records
        )


@pytest.mark.serial
def test_600_members_finish_repeated_same_default_fresh_collects(
    tmp_path: Path,
) -> None:
    started = time.monotonic()
    spec = batch_spec(tmp_path, responses=600, capture="minimal")
    spec.output_path = tmp_path / "store"
    config = tmp_path / "run.json"
    config.write_text(spec.model_dump_json())
    rows = [dict[str, str]() for _ in range(599)] + [{"outcome": "provider_error"}]
    mock = mock_fixture(
        tmp_path,
        duplicate_results=True,
        rows=rows,
        input_tokens=7,
        output_tokens=3,
    )
    root = tmp_path / "store"
    prepare_batch(config, store=root, run_id="001", mock=mock)
    submitted = submit_batch("001", store=root, spec=spec, mock=mock)
    assert submitted.batch.accepted_jobs == 1
    with ReadOnlyRepository(root) as repository:
        attempt_ids = {a.attempt_id for a in repository.iter_attempts("001")}
    assert len(attempt_ids) == 600
    traces: list[dict[str, Any]] = []
    previous = (0, 0)
    for _ in range(16):
        remaining = 180 - (time.monotonic() - started)
        assert remaining > 0, "bounded offline campaign did not finish"
        trace = fresh_collect(tmp_path, min(30, remaining))
        progress = source_progress(root)
        traces.append({**trace, "progress": progress})
        (tmp_path / "collect-trace.json").write_text(json.dumps(traces, indent=2))
        assert progress > previous, traces
        previous = progress
        assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}
        if progress == (1200, 2):
            break
    assert previous == (1200, 2), traces
    for trace in traces:
        assert trace["options"] == {
            "schema_version": 1,
            "deadline_seconds": 30,
            "max_polls": 1,
            "poll_interval_seconds": 1,
            "max_io_steps": 1024,
            "max_result_rows": 128,
        }
        assert 0 < trace["steps"] <= 1024
        assert 0 < trace["bytes"] <= 128 * 1_048_576
        assert 0 < trace["reads"] <= 128
        assert trace["result_rows"] <= 128
    with ReadOnlyRepository(root) as repository:
        assert {a.attempt_id for a in repository.iter_attempts("001")} == attempt_ids
        results = tuple(repository.iter_results("001"))
        assert len(results) == 600
        assert sum(r.status == "complete" for r in results) == 599
        assert sum(r.status == "errored" for r in results) == 1
        receipts = [
            json.loads(row[0])
            for row in repository.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert len(receipts) == 600
        errors = [r for r in receipts if r["kind"] == "provider_error"]
        assert len(errors) == 1
        assert errors[0]["usage"]["input_tokens"] is None
        assert errors[0]["usage"]["output_tokens"] is None
        assert sum(r["usage"]["input_tokens"] or 0 for r in receipts) == 599 * 7
        assert sum(r["usage"]["output_tokens"] or 0 for r in receipts) == 599 * 3
        assert all(r["usage"]["reported_cost"] is None for r in receipts)
    assert not traces[-1]["status"]["batch"]["recovery_required"]
    assert time.monotonic() - started < 180


@pytest.mark.parametrize(
    "fault", ["prefix", "replacement", "scope", "identity", "oversize", "unsafe"]
)
def test_authoritative_index_rejection_precedes_range_and_counts_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    spec, mock = setup(tmp_path, input_tokens=7, output_tokens=3)
    root = tmp_path / "store"
    submit_batch("001", store=root, spec=spec, mock=mock)
    collect_batch(
        "001",
        store=root,
        spec=spec,
        mock=mock,
        options=BatchControlOptionsV1(max_result_rows=1),
    )
    before, evidence = cursors(root), member_evidence(root)
    budgets: list[core.InvocationBudget] = []
    hashed: list[bytes] = []
    returned: list[bytes] = []
    initialize = core.InvocationBudget.__init__
    original_index = MockBatchAdapter.result_index
    reads = 0

    def capture(self: core.InvocationBudget, options: BatchControlOptionsV1) -> None:
        initialize(self, options)
        budgets.append(self)

    def digest(raw: bytes) -> str:
        hashed.append(raw)
        return bytes_digest(raw)

    async def changed(
        self: MockBatchAdapter,
        submission: str,
        remote: str,
        source: str,
        members: list[tuple[str, int, PreparedRequestV2]],
    ) -> tuple[BatchSourceCommitmentV1, bytes]:
        commitment, raw = await original_index(
            self, submission, remote, source, members
        )
        data = json.loads(raw)
        if fault == "prefix":
            data["prefix_sha256"][1] = "f" * 64
        elif fault == "replacement":
            # Coherent replacement retains the old prefix; its pinned root must fail.
            data["prefix_sha256"][-1] = "f" * 64
        elif fault == "scope":
            data["scope_sha256"] = "f" * 64
            commitment = commitment.model_copy(update={"scope_sha256": "f" * 64})
        elif fault == "identity":
            commitment = commitment.model_copy(update={"source_id": "f" * 64})
        raw = canonical_bytes(data)
        if fault in ("replacement", "scope"):
            commitment = commitment.model_copy(
                update={"index_sha256": bytes_digest(raw)}
            )
        if fault == "oversize":
            raw = b" " * (self.options.limits.max_record_bytes + 1)
        elif fault == "unsafe":
            data["prefix_sha256"][1] = "PROHIBITED_BATCH_INDEX_SECRET"
            raw = canonical_bytes(data)
        returned.append(raw)
        return commitment, raw

    async def unread(
        self: MockBatchAdapter,
        wire: PreparedRequestV2,
        position: int,
        key: str,
        source: str,
    ) -> bytes | None:
        nonlocal reads
        reads += 1
        raise AssertionError("unverified prefix must never admit a range read")

    monkeypatch.setattr(core.InvocationBudget, "__init__", capture)
    monkeypatch.setattr(core, "bytes_digest", digest)
    monkeypatch.setattr(MockBatchAdapter, "result_index", changed)
    monkeypatch.setattr(MockBatchAdapter, "read_row", unread)
    with pytest.raises(LifecycleError):
        collect_batch("001", store=root, spec=spec, mock=mock)
    assert reads == 0 and budgets[0].steps == 2
    assert budgets[0].bytes >= len(returned[0])
    assert budgets[0].result_rows == 0
    assert cursors(root) == before and member_evidence(root) == evidence
    assert get_batch_status("001", store=root).batch.recovery_required
    marker = b"PROHIBITED_BATCH_INDEX_SECRET"
    assert all(marker not in raw for raw in hashed)
    for path in root.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            assert (
                marker not in raw
                and hashlib.sha256(marker).hexdigest().encode() not in raw
            )
    assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}


def test_original_index_is_immutable_and_authenticates_historical_cursor(
    tmp_path: Path,
) -> None:
    spec, mock = setup(tmp_path, input_tokens=7, output_tokens=3)
    root = tmp_path / "store"
    submit_batch("001", store=root, spec=spec, mock=mock)
    collect_batch(
        "001",
        store=root,
        spec=spec,
        mock=mock,
        options=BatchControlOptionsV1(max_result_rows=1),
    )
    evidence = member_evidence(root)
    with (
        closing(sqlite3.connect(root / "mock-batch-service.sqlite3")) as connection,
        connection,
    ):
        original = tuple(connection.execute("SELECT * FROM result_indexes").fetchone())
        for statement, parameters in (
            ("UPDATE result_indexes SET record_json='{}'", ()),
            ("DELETE FROM result_indexes", ()),
            ("INSERT OR REPLACE INTO result_indexes VALUES (?,?,?,?)", original),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(statement, parameters)
        assert (
            tuple(connection.execute("SELECT * FROM result_indexes").fetchone())
            == original
        )
    with (
        Repository(root) as repository,
        ExecutorOwnership(repository, "001") as ownership,
    ):
        assert ownership.owner is not None
        entity = repository.connection.execute(
            "SELECT entity_id FROM batch_facts WHERE kind='cursor' ORDER BY fact_seq DESC LIMIT 1"
        ).fetchone()[0]
        cursor = latest(repository, "001", "cursor", entity, BatchCursorV1)
        assert cursor is not None and cursor.index_sha256 is not None
        historical = cursor.model_copy(update={"index_sha256": None})
        assert b'"index_sha256"' not in canonical_bytes(historical)
        fact(repository, "001", ownership.owner.token, "cursor", entity, historical)
    result = collect_batch("001", store=root, spec=spec, mock=mock)
    assert result.outcome == "ok" and not result.batch.recovery_required
    assert all(record in member_evidence(root) for record in evidence)
    with ReadOnlyRepository(root) as repository:
        assert repository.summary("001").attempts == 2
        assert repository.summary("001").results == 2
    with closing(sqlite3.connect(root / "mock-batch-service.sqlite3")) as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT * FROM result_indexes WHERE source_id=?", (original[0],)
                ).fetchone()
            )
            == original
        )
    assert source_progress(root) == (2, 2)
    assert service_counts(root)["forwards"] == 2


@pytest.mark.parametrize("fault", ["early-eof", "changed-suffix"])
def test_range_must_match_commitment_before_outcome_or_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    spec, mock = setup(tmp_path)
    root = tmp_path / "store"
    submit_batch("001", store=root, spec=spec, mock=mock)
    collect_batch(
        "001",
        store=root,
        spec=spec,
        mock=mock,
        options=BatchControlOptionsV1(max_result_rows=1),
    )
    before, evidence = cursors(root), member_evidence(root)
    original = MockBatchAdapter.results

    async def changed(
        self: MockBatchAdapter,
        submission: str,
        remote: str,
        source: str,
        members: list[tuple[str, int, PreparedRequestV2]],
        cursor: BatchCursorV1 | None = None,
    ) -> AsyncGenerator[bytes, None]:
        if fault == "early-eof":
            return
        async for raw in original(self, submission, remote, source, members, cursor):
            data = json.loads(raw)
            data["usage"]["input_tokens"] = 99
            yield canonical_bytes(data) + b"\n"

    monkeypatch.setattr(MockBatchAdapter, "results", changed)
    with pytest.raises(LifecycleError):
        collect_batch("001", store=root, spec=spec, mock=mock)
    assert cursors(root) == before and member_evidence(root) == evidence
    assert get_batch_status("001", store=root).batch.recovery_required
    assert service_counts(root)["forwards"] == 2


@pytest.mark.asyncio
async def test_index_deadline_holds_ownership_until_read_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, mock = setup(tmp_path)
    root = tmp_path / "store"
    await asubmit_batch("001", store=root, spec=spec, mock=mock)
    entered, closing_read, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow(
        self: MockBatchAdapter,
        submission: str,
        remote: str,
        source: str,
        members: list[tuple[str, int, PreparedRequestV2]],
    ) -> tuple[BatchSourceCommitmentV1, bytes]:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closing_read.set()
            await drained.wait()
        raise AssertionError("cancelled index read must not return evidence")

    monkeypatch.setattr(MockBatchAdapter, "result_index", slow)
    task = asyncio.create_task(
        acollect_batch(
            "001",
            store=root,
            spec=spec,
            mock=mock,
            options=BatchControlOptionsV1(deadline_seconds=0.15),
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(closing_read.wait(), 5)
    with ReadOnlyRepository(root) as repository:
        assert executor_is_active(repository, "001")
    assert not task.done()
    drained.set()
    assert (await task).outcome == "partial"
    with ReadOnlyRepository(root) as repository:
        assert not executor_is_active(repository, "001")
        assert repository.summary("001").results == 0
    assert cursors(root) == [] and service_counts(root)["forwards"] == 2
