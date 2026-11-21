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
from batch_clock import batch_clock
from batch_privacy_audit import PrivacyAudit
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
from judgmentllm.batch_models import (
    BatchCursorV1,
    BatchMemberReceiptV1,
    BatchMemberV1,
    BatchSourceCommitmentV1,
    BatchSourceIndexV1,
)
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.native_batch import fact, latest
from judgmentllm.persistence.ownership import ExecutorOwnership, executor_is_active
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.preparation.manifest import (
    ExecutableRunManifestV12,
    parse_executable_manifest,
)
from judgmentllm.providers.batch_mock import MockBatchAdapter
from judgmentllm.providers.protocol import PreparedRequestV2
from judgmentllm.results import RubricResult

COLLECT = """
import json,sys
from pathlib import Path
from judgmentllm import BatchMockOptions, collect_batch
from judgmentllm.execution.native_batch import InvocationBudget
from judgmentllm.providers.batch_mock import MockBatchAdapter
directory=Path(sys.argv[1]); budgets=[]; reads=0; physical=[]; ranges=[]; positions={}
initialize=InvocationBudget.__init__
def capture(self, options):
    initialize(self, options)
    budgets.append(self)
InvocationBudget.__init__=capture
read=MockBatchAdapter.read_row
members=MockBatchAdapter.source_members
def observed_members(self, source, rows):
    for offset, row in enumerate(members(self, source, rows)):
        positions[(id(self), source)]=offset
        yield row
MockBatchAdapter.source_members=observed_members
results=MockBatchAdapter.results
def observed_range(self, submission, remote, source, rows, cursor=None):
    assert cursor is not None
    ranges.append({'source':source,'submission':submission,'remote':remote,
        'cursor':cursor.model_dump(mode='json')})
    return results(self, submission, remote, source, rows, cursor)
MockBatchAdapter.results=observed_range
async def counted(self, wire, position, key, source):
    global reads
    reads+=1
    raw=await read(self,wire,position,key,source)
    if raw is not None:
        physical.append([source,positions[(id(self),source)],position,key])
    return raw
MockBatchAdapter.read_row=counted
result=collect_batch('001',store=directory/'store',mock=BatchMockOptions(fixture_path=directory/'batch-fixture.json'))
budget=budgets[0]
print(json.dumps({'status':result.model_dump(mode='json'),'steps':budget.steps,
    'bytes':budget.bytes,'result_rows':budget.result_rows,'reads':reads,
    'options':budget.options.model_dump(mode='json'),'physical':physical,'ranges':ranges,
    'budgets':len(budgets)}))
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
def test_130_members_finish_repeated_same_default_fresh_collects(
    tmp_path: Path,
) -> None:
    started = time.monotonic()
    spec = batch_spec(tmp_path, responses=130, traits=1, capture="minimal")
    spec.output_path = tmp_path / "store"
    config = tmp_path / "run.json"
    config.write_text(spec.model_dump_json())
    rows = [dict[str, str]() for _ in range(129)] + [{"outcome": "provider_error"}]
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
        members = [
            BatchMemberV1.model_validate_json(row[0])
            for row in repository.connection.execute(
                "SELECT record_json FROM batch_prepared_members ORDER BY position"
            )
        ]
        manifest = parse_executable_manifest(
            (repository.run_directory("001") / "manifest.json").read_bytes()
        )
        assert isinstance(manifest, ExecutableRunManifestV12)
        submissions = repository.connection.execute(
            "SELECT submission_id,remote_id FROM submissions"
        ).fetchall()
    assert len(attempt_ids) == len(members) == 130
    assert {m.attempt_id for m in members} == attempt_ids
    assert [m.position for m in members] == list(range(130))
    assert len(submissions) == 1
    submission, remote = submissions[0]
    sources = {
        kind: semantic_digest(
            "batch-result-source-v1",
            [submission, kind, manifest.batch_context.fixture_sha256],
        )
        for kind in ("output", "error")
    }
    expected = {
        "output": [
            (m.position, m.member_key) for m in reversed(members[:-1]) for _ in range(2)
        ],
        "error": [(members[-1].position, members[-1].member_key)] * 2,
    }

    def snapshot() -> dict[str, BatchCursorV1 | None]:
        with ReadOnlyRepository(root) as repository:
            return {
                kind: latest(repository, "001", "cursor", source, BatchCursorV1)
                for kind, source in sources.items()
            }

    def polls() -> int:
        with closing(
            sqlite3.connect(
                (root / "mock-batch-service.sqlite3").as_uri() + "?mode=ro", uri=True
            )
        ) as connection:
            return int(
                connection.execute("SELECT sum(status_reads) FROM jobs").fetchone()[0]
            )

    traces: list[dict[str, Any]] = []
    previous = (0, 0)
    resumed_tail: set[int] = set()
    for _ in range(16):
        remaining = 180 - (time.monotonic() - started)
        assert remaining > 0, "bounded offline campaign did not finish"
        before, before_polls = snapshot(), polls()
        trace = fresh_collect(tmp_path, min(30, remaining))
        after = snapshot()
        progress = source_progress(root)
        traces.append(
            {
                **trace,
                "progress": progress,
                "before": {
                    k: v.model_dump(mode="json") if v else None
                    for k, v in before.items()
                },
            }
        )
        (tmp_path / "collect-trace.json").write_text(json.dumps(traces, indent=2))
        assert progress > previous, traces
        assert 0 <= polls() - before_polls <= 1
        for kind, source in sources.items():
            prior, current = before[kind], after[kind]
            start, stop = (
                prior.position if prior else 0,
                current.position if current else 0,
            )
            assert start <= stop <= len(expected[kind])
            if current is not None:
                assert (current.run_id, current.submission_id, current.source_id) == (
                    "001",
                    submission,
                    source,
                )
            if prior is not None:
                assert (
                    current is not None and current.index_sha256 == prior.index_sha256
                )
                assert not prior.exhausted or current.exhausted
            physical = [event for event in trace["physical"] if event[0] == kind]
            assert [event[1] for event in physical] == list(
                range(start, start + len(physical))
            )
            assert [tuple(event[2:]) for event in physical] == expected[kind][
                start : start + len(physical)
            ]
            assert stop - start <= len(physical)
            if kind == "output" and start > 0:
                resumed_tail.update(
                    event[1] for event in physical if event[1] in (256, 257)
                )
        for entry in trace["ranges"]:
            kind = entry["source"]
            prior = before[kind]
            assert (entry["submission"], entry["remote"]) == (submission, remote)
            assert entry["cursor"]["source_id"] == sources[kind]
            assert entry["cursor"]["position"] == (prior.position if prior else 0)
            if prior is not None:
                assert entry["cursor"]["prefix_sha256"] == prior.prefix_sha256
                assert entry["cursor"]["index_sha256"] == prior.index_sha256
        previous = progress
        assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}
        if progress == (260, 2):
            break
    assert previous == (260, 2) and len(traces) >= 3 and resumed_tail == {256, 257}, (
        traces
    )
    assert sum(t["result_rows"] for t in traces) == 260
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
        assert trace["budgets"] == 1
        assert 0 <= len(trace["physical"]) <= trace["reads"] <= 128
        assert 0 <= trace["result_rows"] <= 128
        if trace["result_rows"] == 0:
            assert trace["progress"][1] > sum(
                bool(c and c.get("exhausted")) for c in trace["before"].values()
            )
    with ReadOnlyRepository(root) as repository:
        assert {a.attempt_id for a in repository.iter_attempts("001")} == attempt_ids
        results = tuple(repository.iter_results("001"))
        assert len(results) == 130
        assert sum(r.status == "complete" for r in results) == 129
        assert sum(r.status == "errored" for r in results) == 1
        receipts = [
            BatchMemberReceiptV1.model_validate_json(row[0])
            for row in repository.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert len(receipts) == 130
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM observations"
            ).fetchone()[0]
            == 130
        )
        errors = [r for r in receipts if r.kind == "provider_error"]
        assert len(errors) == 1
        assert errors[0].identity.attempt_id == members[-1].attempt_id
        by_attempt = {m.attempt_id: m for m in members}
        assert {r.identity.attempt_id for r in receipts} == attempt_ids
        assert len({r.identity.observation_id for r in receipts}) == 130
        for receipt in receipts:
            member = by_attempt[receipt.identity.attempt_id]
            kind = "error" if member.position == 129 else "output"
            assert (
                receipt.member_key,
                receipt.identity.request_uid,
                receipt.identity.submission_id,
            ) == (member.member_key, member.request_uid, submission)
            assert receipt.source_id == sources[kind]
            assert receipt.source_position == (
                0 if kind == "error" else 2 * (128 - member.position)
            )
            assert receipt.observation_key == semantic_digest(
                "batch-observation-v1", [member.member_key, kind]
            )
            assert receipt.identity.observation_id == semantic_digest(
                "batch-observation-id-v1", [submission, member.member_key]
            )
            assert (receipt.usage.input_tokens, receipt.usage.output_tokens) == (
                (None, None) if kind == "error" else (7, 3)
            )
            assert (
                receipt.capture == "minimal" and receipt.retained.relative_path is None
            )
        for result in results:
            assert isinstance(result, RubricResult)
            member = by_attempt[result.attempt_id]
            assert result.request_uid == member.request_uid
            assert (result.item_id, result.trait_id) == (
                f"{member.position:03d}",
                "000",
            )
            if member.position == 129:
                assert (
                    result.status == "errored" and result.error_kind == "provider_error"
                )
                assert (
                    result.error_message is None
                    and result.input_tokens is result.output_tokens is None
                )
        assert sum(r.usage.input_tokens or 0 for r in receipts) == 903
        assert sum(r.usage.output_tokens or 0 for r in receipts) == 387
        assert all(r.usage.reported_cost is None for r in receipts)
        facts = [
            BatchCursorV1.model_validate_json(r[0])
            for r in repository.connection.execute(
                "SELECT record_json FROM batch_facts WHERE kind='cursor'"
            )
        ]
    with closing(
        sqlite3.connect(
            (root / "mock-batch-service.sqlite3").as_uri() + "?mode=ro", uri=True
        )
    ) as connection:
        indexes = {
            row[0]: (row[1], BatchSourceIndexV1.model_validate_json(row[2]))
            for row in connection.execute(
                "SELECT source_id,index_sha256,record_json FROM result_indexes"
            )
        }
    assert set(indexes) == set(sources.values())
    for kind, source in sources.items():
        digest, index = indexes[source]
        assert bytes_digest(canonical_bytes(index)) == digest
        assert len(index.prefix_sha256) == len(expected[kind]) + 1
        for cursor in (c for c in facts if c.source_id == source):
            assert (
                cursor.index_sha256 == digest
                and cursor.prefix_sha256 == index.prefix_sha256[cursor.position]
            )
        assert any(
            c.source_id == source and c.position == len(expected[kind]) and c.exhausted
            for c in facts
        )
    for boundary in (128, 256):
        assert any(
            c.source_id == sources["output"]
            and c.position == boundary
            and not c.exhausted
            for c in facts
        )
    for trace in traces:
        for entry in trace["ranges"]:
            assert (
                entry["cursor"]["index_sha256"] == indexes[sources[entry["source"]]][0]
            )
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
    with batch_clock(monkeypatch) as clock:
        task = asyncio.create_task(
            acollect_batch(
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
            await clock.gate(closing_read, task)
            with ReadOnlyRepository(root) as repository:
                assert executor_is_active(repository, "001")
            assert not task.done() and clock.now >= budget.deadline
            assert budget.deadline == clock.start + 0.15
            drained.set()
            assert (await clock.wait(task)).outcome == "partial"
            assert budget.closed
        finally:
            await clock.cleanup(task, drained)
    with ReadOnlyRepository(root) as repository:
        assert not executor_is_active(repository, "001")
        assert repository.summary("001").results == 0
    assert cursors(root) == [] and service_counts(root)["forwards"] == 2


PRIVACY_DEATH = """
import os,sys
from pathlib import Path
import pytest
from batch_privacy_audit import PrivacyAudit
from judgmentllm import BatchMockOptions, load_run_spec, submit_batch, collect_batch
from judgmentllm.persistence.repository import ReadOnlyRepository
directory=Path(sys.argv[1]); action=sys.argv[2]; root=directory/'store'
audit=PrivacyAudit(root)
def fault(stage, identity):
    audit.inspect(stage, identity)
    target='after_batch_operation_receipt' if action=='submit' else 'after_batch_member_receipt'
    if stage != target: return
    if action=='submit':
        with ReadOnlyRepository(root) as repository:
            row=repository.connection.execute('SELECT kind FROM batch_operations WHERE instance_id=?',(identity,)).fetchone()
        if row is None or row[0]!='create': return
    assert audit.originals, 'privacy death boundary was not exercised'
    os._exit(73)
with pytest.MonkeyPatch.context() as patch:
    audit.install(patch)
    operation=submit_batch if action=='submit' else collect_batch
    result=audit.checked(lambda: operation('001',store=root,spec=load_run_spec(directory/'run.json'),mock=BatchMockOptions(fixture_path=directory/'batch-fixture.json'),_fault=fault))
raise AssertionError('required publication death boundary was not reached')
"""


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("fault", ["unsafe_control", "unsafe_result", "raw_marker"])
def test_privacy_at_real_publication_death_precedes_recovery(
    tmp_path: Path, capture: str, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = batch_spec(tmp_path, capture=capture)
    spec.output_path = tmp_path / "store"
    config = tmp_path / "run.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    mock = mock_fixture(
        tmp_path,
        **(
            {"rows": [{"raw_marker": "PROHIBITED_BATCH_MARKER_SECRET"}]}
            if fault == "raw_marker"
            else {fault: True}
        ),
    )
    root = tmp_path / "store"
    prepare_batch(config, store=root, run_id="001", mock=mock)
    audit = PrivacyAudit(root)
    action = "submit" if fault == "unsafe_control" else "collect"
    if action == "collect":
        assert submit_batch("001", store=root, spec=spec, mock=mock).outcome == "ok"
    source = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(source / "tests"), str(source / "src"))),
    }
    sqlite_opens: list[bool] = []

    def forbidden_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        sqlite_opens.append(True)
        raise AssertionError("SQLite opened before the first postdeath privacy scan")

    assert audit.checkpoints == []
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", forbidden_connect)
        child = subprocess.run(
            [sys.executable, "-B", "-c", PRIVACY_DEATH, str(tmp_path), action],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert child.returncode == 73, (child.stdout, child.stderr)
        audit.inspect("after_process_death_before_recovery", "")
        assert sqlite_opens == []
        assert audit.checkpoints == ["after_process_death_before_recovery"]
    assert audit.failures == []
    assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}
    with ReadOnlyRepository(root) as repository:
        assert not executor_is_active(repository, "001")
        assert repository.summary("001").attempts == 2
        assert repository.summary("001").results == 0
