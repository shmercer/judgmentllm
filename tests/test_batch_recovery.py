"""Process death, late cancellation, immutable evidence and conservative recovery."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_native_batch import batch_spec, mock_fixture, service_counts

from judgmentllm import (
    BatchMockOptions,
    cancel_batch,
    collect_batch,
    get_batch_status,
    load_run_spec,
    poll_batch,
    prepare_batch,
    resume_batch,
    submit_batch,
)
from judgmentllm.config import RunSpecV2
from judgmentllm.lifecycle import cancel_run
from judgmentllm.persistence.repository import ReadOnlyRepository

CHILD = """
import os,sys
from pathlib import Path
from judgmentllm import BatchMockOptions, submit_batch, resume_batch, collect_batch, cancel_batch, load_run_spec
from judgmentllm.persistence.repository import ReadOnlyRepository
action,root,config,fixture,stage,kind=sys.argv[1:]
def fault(current,identity):
    if current != stage: return
    if kind:
        with ReadOnlyRepository(root) as repository:
            row=repository.connection.execute("SELECT kind FROM batch_operations WHERE instance_id=?",(identity,)).fetchone()
        if row is None or row[0] != kind: return
    os._exit(73)
function={"submit":submit_batch,"resume":resume_batch,"collect":collect_batch,"cancel":cancel_batch}[action]
result=function("001",store=Path(root),spec=load_run_spec(config),mock=BatchMockOptions(fixture_path=Path(fixture)),_fault=fault)
print(result.model_dump_json())
"""


def child(
    directory: Path, action: str, stage: str = "", kind: str = ""
) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    # Process correctness deliberately uses source imports; installed qualification
    # remains the independent seventh gate, never credited by this fixture.
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            CHILD,
            action,
            str(directory / "store"),
            str(directory / "run.json"),
            str(directory / "batch-fixture.json"),
            stage,
            kind,
        ],
        env=environment,
        cwd=directory,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def setup(
    directory: Path, *, capture: str = "full", **fixture: object
) -> tuple[RunSpecV2, BatchMockOptions]:
    spec = batch_spec(directory, capture=capture)
    config = directory / "run.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    mock = mock_fixture(directory, **fixture)
    prepare_batch(config, store=directory / "store", run_id="001", mock=mock)
    restored = load_run_spec(config)
    assert isinstance(restored, RunSpecV2)
    return restored, mock


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_lost_create_ack_stop_late_discovery_single_cancel_and_two_rows(
    tmp_path: Path, capture: str
) -> None:
    _, mock = setup(
        tmp_path,
        capture=capture,
        rows=[{}, {"outcome": "confirmed_cancellation"}],
        input_tokens=7,
        output_tokens=3,
    )
    root = tmp_path / "store"
    assert child(tmp_path, "submit", "after_batch_forward", "create").returncode == 73
    assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}
    stopped = cancel_batch(
        "001",
        store=root,
        mock=mock.model_copy(update={"reconciliation_available": False}),
    )
    assert stopped.batch.local_stop and stopped.batch.cancel_all
    assert stopped.batch.accepted_jobs == 0
    assert stopped.batch.unresolved_operations == 1
    assert service_counts(root)["forwards"] == 2
    recovered = child(tmp_path, "resume")
    assert recovered.returncode == 0, recovered.stderr
    status = get_batch_status("001", store=root)
    assert status.batch.accepted_jobs == 1 and status.batch.missing_members == 0
    assert service_counts(root) == {"forwards": 3, "jobs": 1, "cancelled": 1}
    assert child(tmp_path, "resume").returncode == 0
    with ReadOnlyRepository(root) as repository:
        assert repository.summary("001").attempts == 2
        records = tuple(repository.iter_results("001"))
        assert {r.status for r in records} == {"complete", "cancelled"}
        receipts = [
            json.loads(r[0])
            for r in repository.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert [r["usage"]["input_tokens"] for r in receipts].count(7) == 1
        assert [r["usage"]["output_tokens"] for r in receipts].count(3) == 1


@pytest.mark.parametrize(
    "stage,kind",
    [
        ("after_batch_intent", "create"),
        ("after_batch_operation_receipt", "create"),
        ("after_batch_operation_commit", "create"),
        ("after_batch_member_receipt", ""),
        ("after_batch_member_observation", ""),
        ("during_batch_results", ""),
        ("after_batch_member_results", ""),
        ("after_batch_cursor", ""),
    ],
)
def test_death_boundaries_never_repeat_a_send(
    tmp_path: Path, stage: str, kind: str
) -> None:
    setup(tmp_path)
    action = "submit" if kind else "resume"
    died = child(tmp_path, action, stage, kind)
    assert died.returncode == 73, (died.stdout, died.stderr)
    before = service_counts(tmp_path / "store")
    recovered = child(tmp_path, "resume")
    assert recovered.returncode == 0, recovered.stderr
    assert service_counts(tmp_path / "store") == before
    status = get_batch_status("001", store=tmp_path / "store")
    if stage == "after_batch_intent":
        assert (
            status.batch.unresolved_operations == 1
            and status.batch.missing_members == 2
        )
    else:
        assert status.batch.missing_members == 0


def test_missing_sibling_after_terminal_job_stays_unresolved(tmp_path: Path) -> None:
    spec, mock = setup(
        tmp_path,
        rows=[{}, {"outcome": "confirmed_cancellation"}],
        missing_positions=[1],
        input_tokens=7,
        output_tokens=3,
    )
    root = tmp_path / "store"
    submit_batch("001", store=root, spec=spec, mock=mock)
    cancel_batch("001", store=root, mock=mock)
    poll_batch("001", store=root, mock=mock)
    result = child(tmp_path, "resume")
    assert result.returncode == 0, result.stderr
    status = get_batch_status("001", store=root)
    assert status.batch.missing_members == 1 and status.batch.recovery_required
    with ReadOnlyRepository(root) as repository:
        records = tuple(repository.iter_results("001"))
        assert len(records) == 1 and records[0].status == "complete"


def test_ambiguous_cancel_not_redelivered_and_local_stop_not_cancel_all(
    tmp_path: Path,
) -> None:
    spec, mock = setup(tmp_path, cancel="uncertain")
    root = tmp_path / "store"
    submit_batch("001", store=root, spec=spec, mock=mock)
    unavailable = mock.model_copy(update={"reconciliation_available": False})
    cancel_batch("001", store=root, mock=unavailable)
    for _ in range(2):
        result = resume_batch("001", store=root, spec=spec, mock=unavailable)
        assert result.batch.unresolved_operations == 1
    assert service_counts(root) == {"forwards": 3, "jobs": 1, "cancelled": 1}
    # Local stop works without constructing an adapter or reopening its fixture.
    other = tmp_path / "other"
    other.mkdir()
    setup(other)
    assert child(other, "submit", "after_batch_forward", "create").returncode == 73
    stopped = cancel_run("001", store=other / "store")
    assert (
        stopped.batch is not None
        and stopped.batch.local_stop
        and not stopped.batch.cancel_all
    )
    assert child(other, "resume").returncode == 0
    assert service_counts(other / "store")["cancelled"] == 0


def test_known_job_503_is_a_safe_read_not_submission_retry(tmp_path: Path) -> None:
    spec, mock = setup(tmp_path, status_failures=1)
    root = tmp_path / "store"
    submit_batch("001", store=root, spec=spec, mock=mock)
    with ReadOnlyRepository(root) as repository:
        original = tuple(
            (s.submission_id, s.remote_id) for s in repository.iter_submissions("001")
        )
    assert poll_batch("001", store=root, mock=mock).outcome == "partial"
    assert child(tmp_path, "resume").returncode == 0
    with ReadOnlyRepository(root) as repository:
        assert (
            tuple(
                (s.submission_id, s.remote_id)
                for s in repository.iter_submissions("001")
            )
            == original
        )
        assert repository.summary("001").attempts == 2
    assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_error_only_results_are_explicit_outcomes(tmp_path: Path, capture: str) -> None:
    spec, mock = setup(
        tmp_path,
        capture=capture,
        rows=[{"outcome": "provider_error"}],
        reverse_results=True,
        duplicate_results=True,
    )
    submit_batch("001", store=tmp_path / "store", spec=spec, mock=mock)
    assert (
        collect_batch(
            "001", store=tmp_path / "store", spec=spec, mock=mock
        ).batch.missing_members
        == 0
    )
    with ReadOnlyRepository(tmp_path / "store") as repository:
        assert [r.status for r in repository.iter_results("001")] == [
            "errored",
            "errored",
        ]
        assert repository.summary("001").attempts == 2


@pytest.mark.parametrize("kind", ["upload", "create", "cancel"])
@pytest.mark.parametrize(
    "stage",
    [
        "after_batch_intent",
        "after_batch_forward",
        "after_batch_operation_receipt",
        "after_batch_operation_commit",
    ],
)
def test_every_mutation_death_boundary_keeps_one_forward_per_instance(
    tmp_path: Path, kind: str, stage: str
) -> None:
    spec, mock = setup(tmp_path, rows=[{}, {"outcome": "confirmed_cancellation"}])
    root = tmp_path / "store"
    if kind == "cancel":
        submit_batch("001", store=root, spec=spec, mock=mock)
    died = child(tmp_path, "cancel" if kind == "cancel" else "submit", stage, kind)
    assert died.returncode == 73, (died.stdout, died.stderr)
    before = service_counts(root)["forwards"]
    recovered = child(tmp_path, "resume")
    assert recovered.returncode == 0, recovered.stderr
    expected_new = 1 if kind == "upload" and stage != "after_batch_intent" else 0
    assert service_counts(root)["forwards"] == before + expected_new
    import sqlite3

    with sqlite3.connect(root / "mock-batch-service.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT coalesce(max(forwards),0) FROM operations"
            ).fetchone()[0]
            <= 1
        )
    status = get_batch_status("001", store=root)
    assert status.batch.unresolved_operations == (stage == "after_batch_intent")


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_member_receipt_recovers_without_retrieval_only_when_retained(
    tmp_path: Path, capture: str
) -> None:
    spec, mock = setup(tmp_path, capture=capture)
    root = tmp_path / "store"
    assert child(tmp_path, "resume", "after_batch_member_receipt").returncode == 73
    unavailable = mock.model_copy(update={"retrieval_available": False})
    result = collect_batch("001", store=root, spec=spec, mock=unavailable)
    assert result.batch.missing_members == (1 if capture == "full" else 2)
    assert child(tmp_path, "resume").returncode == 0
    assert get_batch_status("001", store=root).batch.missing_members == 0
    assert service_counts(root)["forwards"] == 2


@pytest.mark.parametrize("create", ["accepted", "rejected"])
def test_local_create_receipt_recovers_before_remote_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, create: str
) -> None:
    import sqlite3
    from collections.abc import AsyncIterator, Callable
    from contextlib import closing

    from judgmentllm.batch_models import (
        BatchMemberReceiptV1,
        BatchOperationIntentV1,
        BatchOperationReceiptV1,
    )
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
    from judgmentllm.persistence.models import ArtifactIdentity
    from judgmentllm.persistence.ownership import executor_is_active
    from judgmentllm.providers.batch_mock import MockBatchAdapter

    spec, mock = setup(tmp_path, create=create, input_tokens=7, output_tokens=3)
    root = tmp_path / "store"
    with ReadOnlyRepository(root) as repository:
        seal = repository.get_manifest("001")
        artifacts = ArtifactStore(repository.run_directory("001"), create=False)
        manifest = artifacts.read(seal.artifact)
        initial = tuple(repository.iter_attempts("001"))
        assert len(initial) == 2 and all(a.state == "prepared" for a in initial)
        science = tuple(a.model_dump(exclude={"state"}) for a in initial)
        memberships = {
            a.attempt_id: repository.attempt_task_ids("001", a.attempt_id)
            for a in initial
        }
    assert service_counts(root) == {"forwards": 0, "jobs": 0, "cancelled": 0}
    died = child(tmp_path, "submit", "after_batch_operation_receipt", "create")
    (tmp_path / "receipt-child.json").write_text(
        json.dumps(
            {
                "returncode": died.returncode,
                "stdout": died.stdout,
                "stderr": died.stderr,
            }
        ),
        encoding="utf-8",
    )
    assert died.returncode == 73, (died.stdout, died.stderr)
    with ReadOnlyRepository(root) as repository:
        rows = repository.connection.execute(
            "SELECT record_json,intent_sha256 FROM batch_operations WHERE kind='create'"
        ).fetchall()
        assert len(rows) == 1
        intent = BatchOperationIntentV1.model_validate_json(rows[0][0])
        assert intent.run_id == "001"
        assert intent.service_id == json.loads(manifest)["batch_context"]["service_id"]
        assert canonical_bytes(intent).decode() == rows[0][0]
        assert bytes_digest(canonical_bytes(intent)) == rows[0][1]
        assert intent.manifest_sha256 == seal.artifact.sha256 == bytes_digest(manifest)
        assert artifacts.read(repository.get_artifact("001", rows[0][1]))
        raw = (
            artifacts.root / f"batch/operations/{intent.instance_id}.json"
        ).read_bytes()
        receipt = BatchOperationReceiptV1.model_validate_json(raw)
        assert canonical_bytes(receipt) == raw
        assert (receipt.instance_id, receipt.intent_sha256) == (
            intent.instance_id,
            rows[0][1],
        )
        digest = bytes_digest(raw)
        identity = ArtifactIdentity(
            sha256=digest,
            byte_length=len(raw),
            media_type="application/json",
            relative_path=f"objects/{digest[:2]}/{digest}",
        )
        assert artifacts.read(identity) == raw
        assert (
            repository.connection.execute(
                "SELECT 1 FROM artifacts WHERE run_id='001' AND sha256=?", (digest,)
            ).fetchone()
            is None
        )
        assert (
            repository.connection.execute(
                "SELECT 1 FROM batch_facts WHERE kind='operation' AND entity_id=?",
                (intent.instance_id,),
            ).fetchone()
            is None
        )
        pending = repository.get_submission("001", intent.instance_id)
        assert pending.state == "submitting" and pending.remote_id is None
        assert tuple(repository.iter_attempts("001")) == initial
        assert executor_is_active(repository, "001") is False
    with closing(
        sqlite3.connect(
            (root / "mock-batch-service.sqlite3").as_uri() + "?mode=ro", uri=True
        )
    ) as service:
        assert service.execute("SELECT identity FROM service").fetchall() == [
            (intent.service_id,)
        ]
        assert service.execute(
            "SELECT intent_sha256,sender,outcome,remote_id,fenced,forwards "
            "FROM operations WHERE instance_id=?",
            (intent.instance_id,),
        ).fetchall() == [
            (
                receipt.intent_sha256,
                intent.owner_token,
                receipt.outcome,
                receipt.remote_id,
                int(create == "rejected"),
                1,
            )
        ]
        assert service.execute("SELECT remote_id,instance_id FROM jobs").fetchall() == (
            [(receipt.remote_id, intent.instance_id)] if create == "accepted" else []
        )
    assert receipt.outcome == ("accepted" if create == "accepted" else "not_accepted")
    if create == "rejected":
        assert receipt.fence is not None and receipt.remote_id is None
        assert (
            receipt.fence.service_id,
            receipt.fence.instance_id,
            receipt.fence.intent_sha256,
            receipt.fence.sender_token,
        ) == (
            intent.service_id,
            intent.instance_id,
            receipt.intent_sha256,
            intent.owner_token,
        )
    else:
        assert receipt.remote_id is not None and receipt.fence is None
    counts = service_counts(root)
    assert counts == {"forwards": 2, "jobs": int(create == "accepted"), "cancelled": 0}
    reconcile, invoke = MockBatchAdapter.reconcile_operation, MockBatchAdapter.invoke

    async def checked_reconcile(
        self: MockBatchAdapter, candidate: BatchOperationIntentV1
    ) -> bytes | None:
        assert candidate.instance_id != intent.instance_id, (
            "local receipt was reconciled remotely"
        )
        return await reconcile(self, candidate)

    async def checked_invoke(
        self: MockBatchAdapter,
        candidate: BatchOperationIntentV1,
        payload: AsyncIterator[bytes],
        guard: Callable[[], None],
    ) -> bytes:
        assert candidate.instance_id != intent.instance_id, (
            "receipted operation was forwarded again"
        )
        return await invoke(self, candidate, payload, guard)

    recovered: list[str] = []
    parent_pid = os.getpid()

    def witness(stage: str, instance: str) -> None:
        if instance == intent.instance_id and stage in (
            "after_batch_operation_receipt",
            "after_batch_operation_commit",
        ):
            assert os.getpid() == parent_pid
            with ReadOnlyRepository(root) as repository:
                row = repository.connection.execute(
                    "SELECT receipt_sha256 FROM batch_facts WHERE kind='operation' AND entity_id=?",
                    (instance,),
                ).fetchone()
                if stage == "after_batch_operation_receipt":
                    assert row is None
                else:
                    assert row is not None and row[0] == digest
            recovered.append(stage)

    monkeypatch.setattr(MockBatchAdapter, "reconcile_operation", checked_reconcile)
    monkeypatch.setattr(MockBatchAdapter, "invoke", checked_invoke)
    result = resume_batch("001", store=root, spec=spec, mock=mock, _fault=witness)
    assert recovered == [
        "after_batch_operation_receipt",
        "after_batch_operation_commit",
    ]
    assert service_counts(root) == counts
    with ReadOnlyRepository(root) as repository:
        assert (
            tuple(
                a.model_dump(exclude={"state"}) for a in repository.iter_attempts("001")
            )
            == science
        )
        assert {
            a.attempt_id: repository.attempt_task_ids("001", a.attempt_id)
            for a in repository.iter_attempts("001")
        } == memberships
        assert repository.get_manifest("001") == seal
        assert artifacts.read(seal.artifact) == manifest
        assert artifacts.read(repository.get_artifact("001", digest)) == raw
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_operations WHERE kind='create'"
            ).fetchone()[0]
            == 1
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM retry_operations"
            ).fetchone()[0]
            == 0
        )
        submission = repository.get_submission("001", intent.instance_id)
        results = tuple(repository.iter_results("001"))
        receipts = tuple(
            tuple(row)
            for row in repository.connection.execute(
                "SELECT member_key,record_json,receipt_sha256 FROM batch_member_receipts ORDER BY member_key"
            )
        )
        if create == "rejected":
            assert result.outcome == "partial" and result.run_status != "failed"
            assert submission.state == "confirmed_not_submitted"
            assert (
                submission.certainty == "not_submitted" and submission.remote_id is None
            )
            assert tuple(repository.iter_attempts("001")) == initial
            assert not results and not receipts
            assert repository.summary("001").unsuccessful_results == 0
            assert (
                repository.connection.execute(
                    "SELECT count(*) FROM observations"
                ).fetchone()[0]
                == 0
            )
            return
        assert result.outcome == "ok" and result.run_status == "completed"
        assert (
            submission.remote_id == receipt.remote_id
            and submission.certainty == "accepted"
        )
        assert len(results) == len(receipts) == 2
        assert all(r.status == "complete" for r in results)
        members = [BatchMemberReceiptV1.model_validate_json(row[1]) for row in receipts]
        assert {m.identity.attempt_id for m in members} == set(memberships)
        assert all(m.identity.submission_id == intent.instance_id for m in members)
        assert [m.usage.input_tokens for m in members] == [7, 7]
        assert [m.usage.output_tokens for m in members] == [3, 3]
    replay = resume_batch("001", store=root, spec=spec, mock=mock, _fault=witness)
    assert replay.outcome == "ok" and service_counts(root) == counts
    assert recovered == [
        "after_batch_operation_receipt",
        "after_batch_operation_commit",
    ]
    with ReadOnlyRepository(root) as repository:
        assert tuple(repository.iter_results("001")) == results
        assert (
            tuple(
                tuple(row)
                for row in repository.connection.execute(
                    "SELECT member_key,record_json,receipt_sha256 FROM batch_member_receipts ORDER BY member_key"
                )
            )
            == receipts
        )
        assert (
            tuple(
                a.model_dump(exclude={"state"}) for a in repository.iter_attempts("001")
            )
            == science
        )


@pytest.mark.parametrize("fault", ["nonobject", "wrong-operation"])
def test_malformed_create_ack_recovers_original_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from collections.abc import AsyncIterator, Callable

    from judgmentllm.batch_models import (
        BatchMemberReceiptV1,
        BatchOperationIntentV1,
        BatchOperationReceiptV1,
    )
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
    from judgmentllm.persistence.ownership import executor_is_active
    from judgmentllm.providers.batch_mock import MockBatchAdapter

    spec, mock = setup(tmp_path, input_tokens=7, output_tokens=3)
    root = tmp_path / "store"
    with ReadOnlyRepository(root) as repository:
        science = tuple(
            a.model_dump(exclude={"state"}) for a in repository.iter_attempts("001")
        )
        assert len(science) == 2
        directory = repository.run_directory("001")
        seal = repository.get_manifest("001")
    upload: list[BatchOperationIntentV1] = []
    create: list[BatchOperationIntentV1] = []
    valid_ack: list[bytes] = []
    published: list[str] = []
    original_invoke = MockBatchAdapter.invoke

    def witness(stage: str, identity: str) -> None:
        if stage == "after_batch_operation_receipt":
            published.append(identity)

    async def altered_ack(
        self: MockBatchAdapter,
        intent: BatchOperationIntentV1,
        payload: AsyncIterator[bytes],
        guard: Callable[[], None],
    ) -> bytes:
        raw = await original_invoke(self, intent, payload, guard)
        if intent.kind == "upload":
            upload.append(intent)
            return raw
        assert intent.kind == "create" and intent.sequence == 1
        assert len(upload) == 1 and not create
        create.append(intent)
        valid_ack.append(raw)
        if fault == "nonobject":
            return b"[]"
        data = json.loads(raw)
        data["instance_id"] = upload[0].instance_id
        return canonical_bytes(data)

    with monkeypatch.context() as patch:
        patch.setattr(MockBatchAdapter, "invoke", altered_ack)
        with pytest.raises(LifecycleError) as raised:
            submit_batch("001", store=root, spec=spec, mock=mock, _fault=witness)
    safe_error = (
        raised.value.category == "execution"
        and not raised.value.retryable
        and raised.value.message == "durable run integrity or storage operation failed"
    )
    assert safe_error, "malformed ACK must retain its safe integrity diagnostic"
    assert len(upload) == len(create) == len(valid_ack) == 1
    original = create[0]
    assert published == [upload[0].instance_id]
    counts = service_counts(root)
    assert counts == {"forwards": 2, "jobs": 1, "cancelled": 0}
    pending = get_batch_status("001", store=root)
    assert pending.batch.unresolved_operations == 1 and pending.batch.recovery_required
    assert pending.batch.accepted_jobs == 0 and pending.batch.missing_members == 2
    upload_path = directory / f"batch/operations/{upload[0].instance_id}.json"
    upload_bytes = upload_path.read_bytes()
    receipt = BatchOperationReceiptV1.model_validate_json(upload_bytes)
    assert (
        receipt.outcome == "accepted" and receipt.instance_id == upload[0].instance_id
    )
    assert receipt.intent_sha256 == bytes_digest(canonical_bytes(upload[0]))
    create_path = directory / f"batch/operations/{original.instance_id}.json"
    assert not create_path.exists()
    with ReadOnlyRepository(root) as repository:
        assert repository.get_manifest("001") == seal
        assert (
            tuple(
                a.model_dump(exclude={"state"}) for a in repository.iter_attempts("001")
            )
            == science
        )
        assert (
            repository.summary("001").results
            == repository.summary("001").unsuccessful_results
            == 0
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM observations"
            ).fetchone()[0]
            == 0
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 0
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_facts WHERE kind='operation' AND entity_id=?",
                (original.instance_id,),
            ).fetchone()[0]
            == 0
        )
        submission = repository.get_submission("001", original.instance_id)
        assert (
            submission.state != "confirmed_not_submitted"
            and submission.certainty != "not_submitted"
        )
        assert (
            repository.get_owner("001") is None
            and executor_is_active(repository, "001") is False
        )
    reconciled: list[str] = []
    original_reconcile = MockBatchAdapter.reconcile_operation

    async def reconcile(
        self: MockBatchAdapter, intent: BatchOperationIntentV1
    ) -> bytes | None:
        assert intent == original
        reconciled.append(intent.instance_id)
        return await original_reconcile(self, intent)

    async def forbidden_forward(
        self: MockBatchAdapter,
        intent: BatchOperationIntentV1,
        payload: AsyncIterator[bytes],
        guard: Callable[[], None],
    ) -> bytes:
        raise AssertionError(
            "accepted original operation must be recovered without a new invoke"
        )

    with monkeypatch.context() as patch:
        patch.setattr(MockBatchAdapter, "reconcile_operation", reconcile)
        patch.setattr(MockBatchAdapter, "invoke", forbidden_forward)
        recovered = resume_batch(
            "001", store=root, spec=spec, mock=mock, _fault=witness
        )
        assert recovered.outcome == "ok" and recovered.batch.missing_members == 0
        assert (
            not recovered.batch.recovery_required and recovered.batch.accepted_jobs == 1
        )
        same_receipt = (
            create_path.read_bytes() == valid_ack[0]
            and upload_path.read_bytes() == upload_bytes
        )
        assert same_receipt, "recovery changed the genuine original receipts"
        with ReadOnlyRepository(root) as repository:
            results = tuple(repository.iter_results("001"))
            evidence = tuple(
                row[0]
                for row in repository.connection.execute(
                    "SELECT record_json FROM batch_member_receipts ORDER BY member_key"
                )
            )
            assert len(results) == len(evidence) == 2 and all(
                r.status == "complete" for r in results
            )
        replay = resume_batch("001", store=root, spec=spec, mock=mock, _fault=witness)
        assert replay.outcome == "ok" and replay.batch.missing_members == 0
    assert reconciled == [original.instance_id]
    assert published == [upload[0].instance_id, original.instance_id]
    assert service_counts(root) == counts
    with ReadOnlyRepository(root) as repository:
        unchanged = (
            tuple(repository.iter_results("001")) == results
            and tuple(
                row[0]
                for row in repository.connection.execute(
                    "SELECT record_json FROM batch_member_receipts ORDER BY member_key"
                )
            )
            == evidence
        )
        assert unchanged, "replay changed proven outcomes or accounting"
        assert (
            tuple(
                a.model_dump(exclude={"state"}) for a in repository.iter_attempts("001")
            )
            == science
        )
        assert repository.get_manifest("001") == seal
        assert (
            repository.get_submission("001", original.instance_id).certainty
            == "accepted"
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM submissions"
            ).fetchone()[0]
            == 1
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_operations WHERE kind='create' AND sequence=1"
            ).fetchone()[0]
            == 1
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_operations"
            ).fetchone()[0]
            == 2
        )
        assert repository.summary("001").retry_operations == 0
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM observations"
            ).fetchone()[0]
            == 2
        )
        members = [BatchMemberReceiptV1.model_validate_json(raw) for raw in evidence]
        assert all(m.identity.submission_id == original.instance_id for m in members)
        assert {m.identity.attempt_id for m in members} == {
            a["attempt_id"] for a in science
        }
        assert len({m.identity.observation_id for m in members}) == 2
        assert all(
            (m.usage.input_tokens, m.usage.output_tokens) == (7, 3) for m in members
        )
        assert sum(m.usage.input_tokens or 0 for m in members) == 14
        assert sum(m.usage.output_tokens or 0 for m in members) == 6
        assert (
            repository.get_owner("001") is None
            and executor_is_active(repository, "001") is False
        )


def test_native_snapshot_authority_is_store_and_session_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import AsyncIterator, Callable

    from judgmentllm.batch_models import BatchMemberReceiptV1, BatchOperationIntentV1
    from judgmentllm.lifecycle import get_run
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.ownership import executor_is_active
    from judgmentllm.preparation import iter_snapshot_authorizations
    from judgmentllm.preparation.manifest import (
        ExecutableRunManifestV12,
        parse_executable_manifest,
    )
    from judgmentllm.providers.batch_mock import MockBatchAdapter

    spec, mock = setup(tmp_path, input_tokens=7, output_tokens=3)
    root, other_store = tmp_path / "store", tmp_path / "other-store"
    prepare_batch(spec, store=other_store, run_id="other", mock=mock)
    submit_batch("001", store=root, spec=spec, mock=mock)
    counts = service_counts(root)
    assert counts == {"forwards": 2, "jobs": 1, "cancelled": 0}
    with ReadOnlyRepository(root) as repository:
        directory = repository.run_directory("001")
        seal = repository.get_manifest("001")
        artifacts = ArtifactStore(directory, create=False)
        raw_manifest = artifacts.read(seal.artifact)
        manifest = parse_executable_manifest(raw_manifest)
        assert (
            isinstance(manifest, ExecutableRunManifestV12)
            and manifest.capture == "full"
        )
        source = next(s for s in manifest.sources if s.role == "response")
        assert (
            source.snapshot is not None
            and artifacts.verify(source.snapshot) is not None
        )
        science = tuple(
            a.model_dump(exclude={"state"}) for a in repository.iter_attempts("001")
        )
        assert len(science) == 2 and repository.summary("001").results == 0
    with pytest.raises(LifecycleError) as wrong_store:
        get_run(directory, store=other_store)
    assert (
        wrong_store.value.category == "validation" and not wrong_store.value.retryable
    )
    assert wrong_store.value.message == "run directory and store disagree"
    assert get_run(directory).run_id == "001"
    assert get_batch_status(directory).batch.accepted_jobs == 1
    spec.input.path.unlink()
    assert not spec.input.path.exists()
    metadata = get_run(directory)
    assert metadata.run_id == "001" and metadata.batch is not None
    assert metadata.batch.missing_members == 2
    before = {a.operation_id for a in iter_snapshot_authorizations("001", store=root)}

    async def forbidden_forward(
        self: MockBatchAdapter,
        intent: BatchOperationIntentV1,
        payload: AsyncIterator[bytes],
        guard: Callable[[], None],
    ) -> bytes:
        raise AssertionError("snapshot continuation must not forward an accepted job")

    monkeypatch.setattr(MockBatchAdapter, "invoke", forbidden_forward)
    with pytest.raises(LifecycleError) as implicit:
        collect_batch("001", store=root, spec=spec, mock=mock)
    assert (
        implicit.value.category == "execution"
        and not implicit.value.retryable
        and implicit.value.message
        == "durable run integrity or storage operation failed"
    )
    assert {
        a.operation_id for a in iter_snapshot_authorizations("001", store=root)
    } == before
    with ReadOnlyRepository(root) as repository:
        assert repository.summary("001").results == 0
        assert (
            repository.get_owner("001") is None
            and executor_is_active(repository, "001") is False
        )
    authorized = collect_batch(
        "001", store=root, spec=spec, mock=mock, use_frozen_snapshot=True
    )
    assert (
        authorized.batch.missing_members == 0 and not authorized.batch.recovery_required
    )
    audit = {a.operation_id: a for a in iter_snapshot_authorizations("001", store=root)}
    newly_authorized = [a for identity, a in audit.items() if identity not in before]
    assert newly_authorized
    for grant in newly_authorized:
        assert grant.run_id == "001" and grant.manifest_sha256 == seal.artifact.sha256
        assert grant.explicit_caller_decision is True
        assert grant.scope == "this_prepared_run_session"
        assert {
            (d.source_id, d.reason, d.expected_sha256) for d in grant.discrepancies
        } == {(source.source_id, "missing_or_unreadable", source.raw_sha256)}
    with pytest.raises(LifecycleError) as fresh_implicit:
        collect_batch("001", store=root, spec=spec, mock=mock)
    assert (
        fresh_implicit.value.category == "execution"
        and not fresh_implicit.value.retryable
        and fresh_implicit.value.message
        == "durable run integrity or storage operation failed"
    )
    assert {
        a.operation_id: a for a in iter_snapshot_authorizations("001", store=root)
    } == audit
    assert get_run(directory).run_id == "001" and not spec.input.path.exists()
    assert service_counts(root) == counts
    assert service_counts(other_store) == {"forwards": 0, "jobs": 0, "cancelled": 0}
    with ReadOnlyRepository(root) as repository:
        assert (
            tuple(
                a.model_dump(exclude={"state"}) for a in repository.iter_attempts("001")
            )
            == science
        )
        assert repository.get_manifest("001") == seal
        same_manifest = (
            ArtifactStore(directory, create=False).read(seal.artifact) == raw_manifest
        )
        assert same_manifest, (
            "snapshot authorization changed the original sealed manifest"
        )
        results = tuple(repository.iter_results("001"))
        assert len(results) == 2 and all(r.status == "complete" for r in results)
        assert {r.attempt_id for r in results} == {a["attempt_id"] for a in science}
        receipts = [
            BatchMemberReceiptV1.model_validate_json(row[0])
            for row in repository.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert len(receipts) == len({r.identity.observation_id for r in receipts}) == 2
        assert all(
            (r.usage.input_tokens, r.usage.output_tokens) == (7, 3) for r in receipts
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM submissions WHERE certainty='accepted'"
            ).fetchone()[0]
            == 1
        )
        assert repository.summary("001").retry_operations == 0
        assert (
            repository.get_owner("001") is None
            and executor_is_active(repository, "001") is False
        )
