"""One-cut audit artifacts and deliberate scientific retries retain all history."""

import csv
import json
from pathlib import Path

import pytest
from test_native_batch import batch_spec, mock_fixture

from judgmentllm import (
    BatchControlOptionsV1,
    RetrySelection,
    collect_batch,
    export_batch_provenance,
    prepare_batch,
    resume_batch,
    submit_batch,
)
from judgmentllm.execution.batch_exports import BatchAuditReportV1
from judgmentllm.lifecycle import export_run_provenance, export_run_results, get_run
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.repository import ReadOnlyRepository


def file_path(report: BatchAuditReportV1, name: str) -> Path:
    file = next(f for f in report.manifest.files if f.name == name)
    assert file.artifact.relative_path is not None
    # The manifest lives at <run>/batch/audits/<id>.json.
    return Path(report.manifest_path).parents[2] / file.artifact.relative_path


def test_audit_files_share_one_cut_and_old_bundle_survives_collection(
    tmp_path: Path,
) -> None:
    spec = batch_spec(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001")
    submit_batch("001", store=root, spec=spec)
    collect_batch(
        "001", store=root, spec=spec, options=BatchControlOptionsV1(max_result_rows=1)
    )
    first = export_batch_provenance("001", store=root)
    old = file_path(first, "results.jsonl").read_bytes()
    assert len(old.splitlines()) == 1
    for file in first.manifest.files:
        path = file_path(first, file.name)
        assert bytes_digest(path.read_bytes()) == file.artifact.sha256
    resumed = resume_batch("001", store=root, spec=spec)
    assert resumed.batch.missing_members == 0
    second = export_run_provenance("001", store=root)
    assert isinstance(second, BatchAuditReportV1)
    assert len(file_path(second, "results.jsonl").read_bytes().splitlines()) == 2
    reopened = export_batch_provenance(
        "001", store=root, bundle_id=first.manifest.bundle_id
    )
    assert file_path(reopened, "results.jsonl").read_bytes() == old
    members = [
        json.loads(line)
        for line in file_path(second, "members.jsonl").read_text().splitlines()
    ]
    assert sum(len(m["result_ids"]) for m in members) == 2
    assert all(
        m["evidence"]["identity"]["attempt_id"] == m["preparation"]["attempt_id"]
        for m in members
    )
    assert get_run("001", store=root).batch is not None
    assert export_run_results("001", store=root).row_count == 2


def test_explicit_retry_creates_scientific_attempt_and_child_shard(
    tmp_path: Path,
) -> None:
    spec = batch_spec(tmp_path, responses=1)
    root = tmp_path / "store"
    first_mock = mock_fixture(tmp_path, rows=[{"outcome": "invalid"}])
    prepare_batch(spec, store=root, run_id="001", mock=first_mock)
    resume_batch("001", store=root, spec=spec, mock=first_mock)
    with ReadOnlyRepository(root) as repository:
        failed = next(repository.iter_results("001"))
    new_fixture = tmp_path / "retry.json"
    new_fixture.write_text(json.dumps({"schema_version": 1, "rows": [{}]}))
    new_mock = first_mock.model_copy(update={"fixture_path": new_fixture})
    retry = RetrySelection(task_ids=(failed.task_id,), operation_id="retry-01")
    result = resume_batch("001", store=root, spec=spec, mock=new_mock, retry=retry)
    assert result.batch.missing_members == 0
    with ReadOnlyRepository(root) as repository:
        results = tuple(repository.iter_results("001"))
        assert [r.status for r in results] == ["invalid", "complete"]
        assert results[0].task_id == results[1].task_id
        assert results[0].attempt_id != results[1].attempt_id
        assert repository.summary("001").attempts == 2
    resume_batch("001", store=root, spec=spec, mock=new_mock, retry=retry)
    with ReadOnlyRepository(root) as repository:
        assert repository.summary("001").attempts == 2
    with pytest.raises(LifecycleError):
        resume_batch(
            "001",
            store=root,
            spec=spec,
            mock=new_mock,
            retry=RetrySelection(
                task_ids=(failed.task_id,),
                operation_id="different",
                allow_completed=False,
            ),
        )


def test_empty_audit_companions_keep_typed_csv_headers(tmp_path: Path) -> None:
    spec = batch_spec(tmp_path, responses=0)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001")
    report = export_batch_provenance("001", store=root)
    for name in ("jobs", "members", "operations", "controls"):
        assert file_path(report, name + ".jsonl").read_bytes() == b""
        with file_path(report, name + ".csv").open(
            newline="", encoding="utf-8"
        ) as stream:
            reader = csv.DictReader(stream)
            assert (
                reader.fieldnames is not None and "schema_version" in reader.fieldnames
            )
            assert list(reader) == []
    assert (
        report.manifest.cut.operation_count
        == report.manifest.cut.member_receipt_count
        == 0
    )


def test_cross_request_batch_retry_preserves_seal_and_selected_order(
    tmp_path: Path,
) -> None:
    from base64 import b64decode

    from test_native_batch import service_counts

    from judgmentllm.batch_models import (
        BatchContextV1,
        BatchMemberReceiptV1,
        BatchMemberV1,
        BatchShardV1,
    )
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.native_batch import read_reference
    from judgmentllm.persistence.ownership import executor_is_active
    from judgmentllm.providers.mock import MockPayloadV2

    spec = batch_spec(tmp_path, responses=2, traits=3, grouped=True)
    mock = mock_fixture(
        tmp_path,
        rows=[{"outcome": "invalid"}, {"outcome": "invalid"}, {"outcome": "valid"}],
        input_tokens=7,
        output_tokens=3,
    )
    assert mock.fixture_path is not None
    fixture_bytes = mock.fixture_path.read_bytes()
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    initial = resume_batch("001", store=root, spec=spec, mock=mock)
    assert initial.outcome == "ok" and initial.batch.missing_members == 0
    assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}
    with ReadOnlyRepository(root) as repository:
        parents = tuple(repository.iter_attempts("001"))
        assert len(parents) == 2 and len({a.request_uid for a in parents}) == 2
        tasks = [repository.request_task_ids("001", a.request_uid) for a in parents]
        assert all(len(group) == 3 for group in tasks)
        original_results = tuple(repository.iter_results("001"))
        assert len(original_results) == 6
        assert all(r.status == "invalid" for r in original_results)
        directory = repository.run_directory("001")
        artifacts = ArtifactStore(directory, create=False)
        manifest = repository.get_manifest("001").artifact
        manifest_bytes = artifacts.read(manifest)

    def scientific_state() -> dict[str, tuple[tuple[object, ...], ...]]:
        with ReadOnlyRepository(root) as repository:
            return {
                table: tuple(
                    tuple(row)
                    for row in repository.connection.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    )
                )
                for table in (
                    "attempts",
                    "attempt_tasks",
                    "retry_operations",
                    "batch_preparations",
                    "batch_prepared_members",
                    "batch_members",
                    "submissions",
                    "observations",
                    "results",
                    "batch_member_receipts",
                )
            }

    checkpoints: list[tuple[str, str]] = []

    def observe(stage: str, identity: str) -> None:
        if stage.startswith("after_batch_retry_"):
            checkpoints.append((stage, identity))

    bad = RetrySelection(
        task_ids=(tasks[0][0], tasks[1][0]), operation_id="cross-request-rejected"
    )
    before = scientific_state()
    with pytest.raises(LifecycleError) as raised:
        resume_batch("001", store=root, spec=spec, mock=mock, retry=bad, _fault=observe)
    safe_error = (
        raised.value.category == "validation"
        and not raised.value.retryable
        and raised.value.message
        == "configuration, source, or recovery inputs are invalid or incompatible"
    )
    assert safe_error, "cross-request rejection must retain its safe public boundary"
    assert checkpoints == [("after_batch_retry_context", bad.operation_id)]
    unchanged = scientific_state() == before
    assert unchanged, "rejected selection changed scientific work or evidence"
    assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}
    with ReadOnlyRepository(root) as repository:
        reserved = read_reference(
            repository,
            "001",
            f"batch/retry-contexts/{bytes_digest(bad.operation_id.encode())}.json",
            BatchContextV1,
        )
        assert reserved is not None
        assert repository.get_owner("001") is None
        assert executor_is_active(repository, "001") is False

    selected = (tasks[0][0], tasks[0][2])
    retry = RetrySelection(task_ids=selected[::-1], operation_id="ordered-subset")
    completed = resume_batch(
        "001", store=root, spec=spec, mock=mock, retry=retry, _fault=observe
    )
    assert completed.outcome == "ok" and completed.batch.missing_members == 0
    assert completed.batch.accepted_jobs == completed.batch.jobs == 2
    assert not completed.batch.recovery_required
    assert checkpoints[1:] == [
        ("after_batch_retry_context", retry.operation_id),
        ("after_batch_retry_attempt", retry.operation_id),
        ("after_batch_retry_shard", retry.operation_id),
    ]
    with ReadOnlyRepository(root) as repository:
        child_attempt = repository.get_attempt_for_request(
            "001", parents[0].request_uid, attempt_number=2
        )
        assert (
            child_attempt is not None
            and child_attempt.authorization_id == retry.operation_id
        )
        assert repository.attempt_task_ids("001", child_attempt.attempt_id) == selected
        assert repository.request_task_ids("001", parents[0].request_uid) == tasks[0]
        assert repository.request_task_ids("001", parents[1].request_uid) == tasks[1]
        child = BatchMemberV1.model_validate_json(
            repository.connection.execute(
                "SELECT record_json FROM batch_prepared_members WHERE attempt_id=?",
                (child_attempt.attempt_id,),
            ).fetchone()[0]
        )
        shard = BatchShardV1.model_validate_json(
            repository.connection.execute(
                "SELECT record_json FROM batch_preparations WHERE shard_id=?",
                (child.shard_id,),
            ).fetchone()[0]
        )
        assert child.request_uid == parents[0].request_uid
        assert child.position == 2 and shard.ordinal == 1 and shard.member_count == 1
        assert shard.context == reserved
        assert (child.payload.sha256, child.prompt.sha256, child.membership_sha256) == (
            child_attempt.payload_sha256,
            child_attempt.prompt_sha256,
            child_attempt.membership_sha256,
        )
        artifacts = ArtifactStore(directory, create=False)
        payload = artifacts.read(child.payload)
        artifacts.read(child.prompt)
        wire = MockPayloadV2.model_validate_json(payload)
        assert wire.request_uid == parents[0].request_uid
        assert tuple(m.task_id for m in wire.members) == selected
        assert shard.context == BatchContextV1.model_validate(
            json.loads(manifest_bytes)["batch_context"]
        )
        framed = artifacts.read(shard.input)
        valid_frame = framed.endswith(b"\n") and len(framed.splitlines()) == 1
        assert valid_frame, "child input must retain exactly one complete record"
        row = json.loads(framed)
        matching_wire = (
            row["member_key"] == child.member_key
            and b64decode(row["payload"], validate=True) == payload
        )
        assert matching_wire, "stored child source differs from authorized wire"
        assert bytes_digest(framed) == child.row_sha256 == shard.input.sha256
        assert len(framed) == child.row_bytes == shard.input.byte_length
        assert child.parent_receipt_sha256 is not None
        parent = BatchMemberReceiptV1.model_validate_json(
            artifacts.read(repository.get_artifact("001", child.parent_receipt_sha256))
        )
        assert (parent.identity.request_uid, parent.identity.attempt_id) == (
            parents[0].request_uid,
            parents[0].attempt_id,
        )
        assert (
            repository.connection.execute(
                "SELECT receipt_sha256 FROM batch_member_receipts WHERE attempt_id=?",
                (parents[0].attempt_id,),
            ).fetchone()[0]
            == child.parent_receipt_sha256
        )
        results = tuple(repository.iter_results("001"))
        original_preserved = results[:6] == original_results
        assert original_preserved, "retry replaced original outcomes"
        assert len(results) == 8
        assert {r.task_id for r in results[6:]} == set(selected)
        assert all(
            r.status == "complete" and r.attempt_id == child_attempt.attempt_id
            for r in results[6:]
        )
        assert repository.summary("001").attempts == 3
        assert repository.summary("001").retry_operations == 1
        receipts = [
            BatchMemberReceiptV1.model_validate_json(row[0])
            for row in repository.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert len(receipts) == len({r.identity.observation_id for r in receipts}) == 3
        assert all(
            (r.usage.input_tokens, r.usage.output_tokens) == (7, 3) for r in receipts
        )
        assert sum(r.usage.input_tokens or 0 for r in receipts) == 21
        assert sum(r.usage.output_tokens or 0 for r in receipts) == 9
        physical = repository.connection.execute(
            "SELECT submission_id,remote_id FROM submissions WHERE certainty='accepted'"
        ).fetchall()
        assert (
            len(physical)
            == len({r[0] for r in physical})
            == len({r[1] for r in physical})
            == 2
        )
        assert all(r[1] is not None for r in physical)
        preserved = artifacts.read(manifest) == manifest_bytes
        assert preserved, "scientific retry changed the initial manifest"
    final = scientific_state()
    replay = resume_batch(
        "001", store=root, spec=spec, mock=mock, retry=retry, _fault=observe
    )
    assert replay.outcome == "ok" and replay.batch.missing_members == 0
    unchanged = scientific_state() == final
    assert unchanged, "same-ID replay changed scientific work or evidence"
    assert checkpoints[-2:] == [
        ("after_batch_retry_context", retry.operation_id),
        ("after_batch_retry_attempt", retry.operation_id),
    ]
    assert sum(stage == "after_batch_retry_shard" for stage, _ in checkpoints) == 1
    assert service_counts(root) == {"forwards": 4, "jobs": 2, "cancelled": 0}
    frozen_fixture = mock.fixture_path.read_bytes() == fixture_bytes
    assert frozen_fixture, "test changed its original scientific recipe"
    with ReadOnlyRepository(root) as repository:
        assert repository.get_owner("001") is None
        assert executor_is_active(repository, "001") is False


def test_changed_retry_fixture_cannot_replace_authorized_child(tmp_path: Path) -> None:
    from test_native_batch import service_counts

    from judgmentllm import BatchMockOptions
    from judgmentllm.batch_models import BatchContextV1, BatchMemberV1
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.native_batch import read_reference
    from judgmentllm.persistence.ownership import executor_is_active
    from judgmentllm.providers.batch_mock import BatchMockFixtureV1
    from judgmentllm.providers.mock import MockPayloadV2
    from judgmentllm.results import RubricResult

    spec = batch_spec(tmp_path, responses=1, traits=2, grouped=True)
    mock_a = mock_fixture(
        tmp_path,
        rows=[{"outcome": "invalid"}, {"outcome": "valid", "category_index": 0}],
        input_tokens=7,
        output_tokens=3,
    )
    assert mock_a.fixture_path is not None
    bytes_a = mock_a.fixture_path.read_bytes()
    recipe_b = json.loads(bytes_a)
    recipe_b["rows"][1]["category_index"] = 1
    path_b = tmp_path / "retry-b.json"
    path_b.write_text(json.dumps(recipe_b), encoding="utf-8")
    bytes_b = path_b.read_bytes()
    mock_b = BatchMockOptions(fixture_path=path_b)
    recipe_a = BatchMockFixtureV1.model_validate_json(bytes_a)
    checked_b = BatchMockFixtureV1.model_validate_json(bytes_b)
    assert recipe_a.rows[0] == checked_b.rows[0]
    assert (recipe_a.rows[1].category_index, checked_b.rows[1].category_index) == (0, 1)
    assert recipe_a.commitment() != checked_b.commitment()
    assert mock_a.model_dump(exclude={"fixture_path"}) == mock_b.model_dump(
        exclude={"fixture_path"}
    )
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock_a)
    initial = resume_batch("001", store=root, spec=spec, mock=mock_a)
    assert initial.outcome == "ok" and initial.batch.missing_members == 0
    with ReadOnlyRepository(root) as repository:
        original = tuple(repository.iter_results("001"))
        assert len(original) == 2 and all(r.status == "invalid" for r in original)
        parent = next(repository.iter_attempts("001"))
        tasks = repository.attempt_task_ids("001", parent.attempt_id)
        assert len(tasks) == 2
        directory = repository.run_directory("001")
        artifacts = ArtifactStore(directory, create=False)
        manifest = repository.get_manifest("001").artifact
        manifest_bytes = artifacts.read(manifest)
    retry = RetrySelection(task_ids=(tasks[0],), operation_id="fixed-child-context")
    checkpoints: list[tuple[str, str]] = []

    def observe(stage: str, identity: str) -> None:
        if stage.startswith("after_batch_retry_"):
            checkpoints.append((stage, identity))

    completed = resume_batch(
        "001", store=root, spec=spec, mock=mock_a, retry=retry, _fault=observe
    )
    assert completed.outcome == "ok" and completed.batch.missing_members == 0
    assert checkpoints == [
        (stage, retry.operation_id)
        for stage in (
            "after_batch_retry_context",
            "after_batch_retry_attempt",
            "after_batch_retry_shard",
        )
    ]
    with ReadOnlyRepository(root) as repository:
        child_attempt = repository.get_attempt_for_request(
            "001", parent.request_uid, attempt_number=2
        )
        assert (
            child_attempt is not None
            and child_attempt.authorization_id == retry.operation_id
        )
        assert (
            repository.attempt_task_ids("001", child_attempt.attempt_id)
            == retry.task_ids
        )
        child = BatchMemberV1.model_validate_json(
            repository.connection.execute(
                "SELECT record_json FROM batch_prepared_members WHERE attempt_id=?",
                (child_attempt.attempt_id,),
            ).fetchone()[0]
        )
        assert child.position == 1
        artifacts = ArtifactStore(directory, create=False)
        wire = MockPayloadV2.model_validate_json(artifacts.read(child.payload))
        assert len(wire.members) == 1 and wire.members[0].task_id == tasks[0]
        assert len(wire.members[0].category_labels) >= 2
        assert wire.members[0].category_labels[0] != wire.members[0].category_labels[1]
        outcomes = repository.get_results_for_attempt("001", child_attempt.attempt_id)
        assert len(outcomes) == 1 and isinstance(outcomes[0], RubricResult)
        assert outcomes[0].status == "complete"
        assert outcomes[0].category_label == wire.members[0].category_labels[0]
        context = read_reference(
            repository,
            "001",
            f"batch/retry-contexts/{bytes_digest(retry.operation_id.encode())}.json",
            BatchContextV1,
        )
        assert context is not None and context.fixture_sha256 == recipe_a.commitment()
        assert context.fixture_sha256 != checked_b.commitment()
        context_path = (
            directory
            / f"batch/retry-contexts/{bytes_digest(retry.operation_id.encode())}.json"
        )
        context_bytes = context_path.read_bytes()

    def scientific_state() -> dict[str, tuple[tuple[object, ...], ...]]:
        with ReadOnlyRepository(root) as repository:
            return {
                table: tuple(
                    tuple(row)
                    for row in repository.connection.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    )
                )
                for table in (
                    "attempts",
                    "attempt_tasks",
                    "retry_operations",
                    "batch_preparations",
                    "batch_prepared_members",
                    "batch_members",
                    "submissions",
                    "observations",
                    "results",
                    "batch_member_receipts",
                )
            }

    before = scientific_state()
    before_checkpoints = len(checkpoints)
    assert service_counts(root) == {"forwards": 4, "jobs": 2, "cancelled": 0}
    with pytest.raises(LifecycleError) as raised:
        resume_batch(
            "001", store=root, spec=spec, mock=mock_b, retry=retry, _fault=observe
        )
    safe_error = (
        raised.value.category == "execution"
        and not raised.value.retryable
        and raised.value.message == "durable run integrity or storage operation failed"
    )
    assert safe_error, "fixture conflict must retain its safe integrity diagnostic"
    assert len(checkpoints) == before_checkpoints
    unchanged = (
        scientific_state() == before and context_path.read_bytes() == context_bytes
    )
    assert unchanged, "rejected fixture replaced authorized scientific evidence"
    assert service_counts(root) == {"forwards": 4, "jobs": 2, "cancelled": 0}
    with ReadOnlyRepository(root) as repository:
        assert repository.get_owner("001") is None
        assert executor_is_active(repository, "001") is False
    replay = resume_batch(
        "001", store=root, spec=spec, mock=mock_a, retry=retry, _fault=observe
    )
    assert replay.outcome == "ok" and replay.batch.missing_members == 0
    assert checkpoints[before_checkpoints:] == [
        ("after_batch_retry_context", retry.operation_id),
        ("after_batch_retry_attempt", retry.operation_id),
    ]
    unchanged = (
        scientific_state() == before and context_path.read_bytes() == context_bytes
    )
    assert unchanged, "original same-ID replay changed the authorized child"
    with ReadOnlyRepository(root) as repository:
        summary = repository.summary("001")
        assert (summary.attempts, summary.results, summary.retry_operations) == (
            2,
            3,
            1,
        )
        results = tuple(repository.iter_results("001"))
        preserved = results[:2] == original
        assert preserved, "child replay changed original outcomes"
        physical = repository.connection.execute(
            "SELECT submission_id,remote_id FROM submissions WHERE certainty='accepted'"
        ).fetchall()
        assert (
            len(physical)
            == len({r[0] for r in physical})
            == len({r[1] for r in physical})
            == 2
        )
        assert all(r[1] is not None for r in physical)
        receipts = [
            json.loads(row[0])
            for row in repository.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert len(receipts) == 2
        assert sum(r["usage"]["input_tokens"] for r in receipts) == 14
        assert sum(r["usage"]["output_tokens"] for r in receipts) == 6
        preserved = (
            ArtifactStore(directory, create=False).read(manifest) == manifest_bytes
        )
        assert preserved, "fixture replay changed the initial manifest"
        assert repository.get_owner("001") is None
        assert executor_is_active(repository, "001") is False
    assert service_counts(root) == {"forwards": 4, "jobs": 2, "cancelled": 0}
    frozen = (
        mock_a.fixture_path.read_bytes() == bytes_a and path_b.read_bytes() == bytes_b
    )
    assert frozen, "test changed a frozen fixture"
