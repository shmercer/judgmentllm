"""Durable evidence and relational invariants without execution orchestration."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from judgmentllm.io import (
    export_results_csv,
    read_results,
    read_results_csv,
    write_results_jsonl,
)
from judgmentllm.models import Level, PairTask, Trait
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, semantic_digest
from judgmentllm.persistence.models import (
    ArtifactIntegrityError,
    AttemptRecord,
    FailureEvidence,
    IntegrityError,
    ManifestReference,
    NotFoundError,
    ObservationRecord,
    PersistenceError,
    RequestRecord,
    RetryAuthorization,
    RunRecord,
    StateTransitionError,
    SubmissionRecord,
    TaskRecord,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.r_exports import export_pairwise_r_csv, export_rubric_r_csv
from judgmentllm.results import (
    PairwiseResult,
    RubricResult,
    normalize_pairwise_result,
    normalize_rubric_result,
)


def _run(
    repository: Repository, run_id: str = "run", *, pairwise: bool = False
) -> None:
    repository.create_run(
        RunRecord(
            run_id=run_id,
            task_kind="pairwise" if pairwise else "rubric",
            provider="mock",
            endpoint="judgments",
            model="fixture",
        )
    )
    manifest = ArtifactStore(repository.run_directory(run_id)).put(
        b'{"synthetic":true}', "application/json"
    )
    repository.seal_run(run_id, ManifestReference(artifact=manifest))


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[Repository]:
    with Repository(tmp_path) as repository:
        _run(repository)
        yield repository


def _trait(index: int = 0) -> Trait:
    return Trait(
        trait_id=f"quality-{index}",
        name="Quality",
        description="Synthetic scale",
        levels=[
            Level(label="low", numeric_value=1),
            Level(label="middle", numeric_value=3),
            Level(label="high", numeric_value=8),
        ],
    )


def _group(
    repository: Repository,
    count: int = 1,
    *,
    run_id: str = "run",
    pairwise: bool = False,
) -> list[str]:
    tasks = [f"task-{index}" for index in range(count)]
    for index, task_id in enumerate(tasks):
        repository.create_task(
            TaskRecord(
                run_id=run_id,
                task_id=task_id,
                trait_id=_trait(index).trait_id,
                item_id=None if pairwise else "001",
                pair_uid="occurrence" if pairwise else None,
                pair_id="pair" if pairwise else None,
                scheduling_ordinal=index,
            )
        )
    repository.create_request(
        RequestRecord(
            run_id=run_id,
            request_uid="request",
            scheduling_ordinal=0,
            dependency_sha256=bytes_digest(b"dependencies"),
        ),
        tasks,
    )
    repository.transition("request", run_id, "request", "prepared")
    return tasks


def _attempt(
    repository: Repository,
    task_ids: list[str],
    *,
    run_id: str = "run",
    number: int = 1,
    authorization_id: str | None = None,
) -> str:
    attempt_id = f"attempt-{number}"
    prompt = ArtifactStore.hash_only(bytes_digest(f"prompt-{number}".encode()), 8)
    payload = ArtifactStore.hash_only(bytes_digest(f"payload-{number}".encode()), 9)
    repository.register_artifact(run_id, prompt)
    repository.register_artifact(run_id, payload)
    repository.create_attempt(
        AttemptRecord(
            run_id=run_id,
            attempt_id=attempt_id,
            request_uid="request",
            attempt_number=number,
            prompt_sha256=prompt.sha256,
            payload_sha256=payload.sha256,
            membership_sha256=semantic_digest("attempt-membership", task_ids),
            authorization_id=authorization_id,
        ),
        task_ids,
    )
    submission_id = f"submission-{number}"
    repository.create_submission(
        SubmissionRecord(
            run_id=run_id,
            submission_id=submission_id,
            correlation_id=f"correlation-{run_id}-{number}",
            payload_sha256=payload.sha256,
            attempt_id=attempt_id,
        )
    )
    return attempt_id


def _accepted(
    repository: Repository,
    *,
    run_id: str = "run",
    number: int = 1,
    kind: str = "response",
) -> str:
    attempt_id = f"attempt-{number}"
    submission_id = f"submission-{number}"
    repository.transition("attempt", run_id, attempt_id, "claimed")
    repository.transition("attempt", run_id, attempt_id, "submitting")
    repository.transition("submission", run_id, submission_id, "submitting")
    repository.transition(
        "submission",
        run_id,
        submission_id,
        "acknowledged",
        remote_id=f"remote-{number}",
    )
    repository.transition("attempt", run_id, attempt_id, "acknowledged")
    observation_id = f"observation-{number}"
    repository.record_observation(
        ObservationRecord.model_validate(
            {
                "run_id": run_id,
                "observation_id": observation_id,
                "submission_id": submission_id,
                "attempt_id": attempt_id,
                "observation_key": "primary",
                "kind": kind,
            }
        )
    )
    repository.transition("attempt", run_id, attempt_id, "response_saved")
    return observation_id


def _complete(*, index: int = 0, number: int = 1) -> RubricResult:
    return normalize_rubric_result(
        _trait(index),
        run_id="run",
        task_id=f"task-{index}",
        request_uid="request",
        attempt_id=f"attempt-{number}",
        item_id="001",
        category_label="middle",
        probabilities=[
            {"level_label": "low", "probability": 0.10},
            {"level_label": "middle", "probability": 0.25},
            {"level_label": "high", "probability": 0.65},
        ],
        provider_expected_index=1.55,
        provider="mock",
        endpoint="judgments",
        model="fixture",
    )


def _failed(
    *,
    index: int = 0,
    number: int = 1,
    status: str = "invalid",
    error_kind: str = "invalid_output",
) -> RubricResult:
    return RubricResult.model_validate(
        {
            "run_id": "run",
            "task_id": f"task-{index}",
            "request_uid": "request",
            "attempt_id": f"attempt-{number}",
            "item_id": "001",
            "trait_id": _trait(index).trait_id,
            "resolved_levels": [
                level.model_dump() for level in _trait(index).resolved_levels
            ],
            "status": status,
            "error_kind": error_kind,
            "error_message": "Synthetic unsuccessful outcome",
        }
    )


def test_initialize_reopen_durability_pragmas_and_summary(registry: Repository) -> None:
    assert registry.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert registry.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert registry.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert registry.connection.execute("PRAGMA user_version").fetchone()[0] == 1
    with Repository(registry.root) as reopened:
        assert reopened.get_run("run") == registry.get_run("run")
        assert reopened.summary("run").tasks == 0
        assert reopened.summary("run").unresolved_submissions == 0
        assert reopened.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unique_and_run_scoped_foreign_constraints(registry: Repository) -> None:
    tasks = _group(registry)
    with pytest.raises(IntegrityError):
        registry.create_run(
            RunRecord(
                run_id="run",
                task_kind="rubric",
                provider="mock",
                endpoint="judgments",
                model="fixture",
            )
        )
    _run(registry, "other")
    with pytest.raises(IntegrityError):
        registry.create_request(
            RequestRecord(
                run_id="other",
                request_uid="foreign",
                scheduling_ordinal=0,
                dependency_sha256="a" * 64,
            ),
            tasks,
        )
    assert (
        registry.connection.execute(
            "SELECT COUNT(*) FROM requests WHERE run_id='other'"
        ).fetchone()[0]
        == 0
    )
    _attempt(registry, tasks)
    with pytest.raises(IntegrityError):
        registry.create_submission(
            SubmissionRecord(
                run_id="run",
                submission_id="other-submission",
                correlation_id="another-correlation",
                attempt_id="attempt-1",
                payload_sha256=bytes_digest(b"payload-1"),
            )
        )


def test_unknown_records_and_invalid_parameters(registry: Repository) -> None:
    with pytest.raises(NotFoundError):
        registry.get_run("absent")
    with pytest.raises(NotFoundError):
        list(registry.iter_results("absent"))
    with pytest.raises(ValueError):
        list(registry.iter_results("run", page_size=0))
    with pytest.raises(ValueError):
        Repository(registry.root, busy_timeout_ms=-1)
    with pytest.raises(IntegrityError):
        registry.transition("unknown", "run", "run", "running")  # type: ignore[arg-type]


def test_valid_and_invalid_state_transitions(registry: Repository) -> None:
    with pytest.raises(StateTransitionError):
        registry.transition("run", "run", "run", "completed")
    registry.transition("run", "run", "run", "running")
    registry.transition("run", "run", "run", "partial")
    registry.transition("run", "run", "run", "running")
    assert registry.get_run("run").state == "running"
    before = len(list(registry.events("run")))
    with pytest.raises(StateTransitionError):
        registry.transition("run", "run", "run", "created")
    assert len(list(registry.events("run"))) == before


def test_nested_savepoint_and_outer_transaction_rollbacks(registry: Repository) -> None:
    with registry.transaction():
        registry.create_task(
            TaskRecord(
                run_id="run",
                task_id="kept",
                trait_id="quality",
                item_id="001",
                scheduling_ordinal=0,
            )
        )
        with pytest.raises(IntegrityError):
            with registry.transaction():
                registry.create_task(
                    TaskRecord(
                        run_id="run",
                        task_id="rolled-back",
                        trait_id="quality",
                        item_id="002",
                        scheduling_ordinal=1,
                    )
                )
                registry.create_task(
                    TaskRecord(
                        run_id="run",
                        task_id="kept",
                        trait_id="quality",
                        item_id="003",
                        scheduling_ordinal=2,
                    )
                )
    assert registry.summary("run").tasks == 1
    assert [
        event.entity_id
        for event in registry.events("run")
        if event.entity_kind == "task"
    ] == ["kept"]
    with pytest.raises(KeyboardInterrupt):
        with registry.transaction():
            registry.create_task(
                TaskRecord(
                    run_id="run",
                    task_id="interrupted",
                    trait_id="quality",
                    item_id="004",
                    scheduling_ordinal=3,
                )
            )
            raise KeyboardInterrupt
    assert registry.summary("run").tasks == 1
    assert not registry.connection.in_transaction


def test_uncommitted_writes_are_invisible_to_independent_reader(
    registry: Repository,
) -> None:
    with Repository(registry.root) as reader:
        with registry.transaction():
            registry.create_task(
                TaskRecord(
                    run_id="run",
                    task_id="task",
                    trait_id="quality",
                    item_id="001",
                    scheduling_ordinal=0,
                )
            )
            assert reader.summary("run").tasks == 0
        assert reader.summary("run").tasks == 1


def test_summary_does_not_commit_its_callers_transaction(registry: Repository) -> None:
    with pytest.raises(KeyboardInterrupt):
        with registry.transaction():
            registry.create_task(
                TaskRecord(
                    run_id="run",
                    task_id="uncommitted",
                    trait_id="quality",
                    item_id="001",
                    scheduling_ordinal=0,
                )
            )
            assert registry.summary("run").tasks == 1
            assert registry.connection.in_transaction
            raise KeyboardInterrupt
    assert registry.summary("run").tasks == 0


def test_initial_states_and_membership_preconditions(registry: Repository) -> None:
    with pytest.raises(IntegrityError, match="unsealed"):
        registry.create_run(
            RunRecord(
                run_id="invalid",
                task_kind="rubric",
                provider="mock",
                endpoint="judgments",
                model="fixture",
                state="submitted",
            )
        )
    with pytest.raises(IntegrityError, match="pending"):
        registry.create_task(
            TaskRecord(
                run_id="run",
                task_id="invalid",
                trait_id="quality",
                item_id="001",
                scheduling_ordinal=0,
                state="active",
            )
        )
    request = RequestRecord(
        run_id="run",
        request_uid="invalid",
        scheduling_ordinal=0,
        dependency_sha256="a" * 64,
    )
    for members in ([], ["same", "same"]):
        with pytest.raises(IntegrityError, match="unique ordered"):
            registry.create_request(request, members)
    with pytest.raises(IntegrityError, match="unique ordered"):
        registry.create_request(
            request.model_copy(update={"state": "prepared"}), ["task"]
        )
    tasks = _group(registry)
    record = AttemptRecord(
        run_id="run",
        attempt_id="invalid",
        request_uid="request",
        attempt_number=1,
        prompt_sha256="a" * 64,
        payload_sha256="b" * 64,
        membership_sha256=semantic_digest("attempt-membership", tasks),
    )
    for members in ([], ["same", "same"]):
        with pytest.raises(IntegrityError, match="unique ordered"):
            registry.create_attempt(record, members)
    with pytest.raises(IntegrityError, match="unique ordered"):
        registry.create_attempt(record.model_copy(update={"state": "claimed"}), tasks)
    with pytest.raises(IntegrityError, match="digest"):
        registry.create_attempt(
            record.model_copy(update={"membership_sha256": "c" * 64}), tasks
        )
    with pytest.raises(IntegrityError, match="prepared local attempt"):
        registry.create_submission(
            SubmissionRecord(
                run_id="run",
                submission_id="invalid",
                correlation_id="invalid",
                batch_uid="not-implemented",
                payload_sha256="a" * 64,
            )
        )


def test_completed_state_requires_explicit_completed_retry_authorization(
    registry: Repository,
) -> None:
    registry.transition("run", "run", "run", "running")
    registry.transition("run", "run", "run", "completed")
    with pytest.raises(IntegrityError, match="explicit retry"):
        registry.transition("run", "run", "run", "running")
    registry.authorize_retry(
        RetryAuthorization(
            run_id="run", operation_id="failed-only", selector_sha256="a" * 64
        )
    )
    with pytest.raises(IntegrityError, match="explicit retry"):
        registry.transition(
            "run", "run", "run", "running", authorization_id="failed-only"
        )
    registry.authorize_retry(
        RetryAuthorization(
            run_id="run",
            operation_id="allow-completed",
            selector_sha256="b" * 64,
            allow_completed=True,
        )
    )
    registry.transition(
        "run", "run", "run", "running", authorization_id="allow-completed"
    )
    with pytest.raises(IntegrityError, match="remote identity"):
        registry.transition("run", "run", "run", "partial", remote_id="not-a-run-field")
    assert registry.get_run("run").state == "running"


def test_concurrent_connections_serialize_short_writes(registry: Repository) -> None:
    root = registry.root

    def write(index: int) -> None:
        with Repository(root) as writer:
            writer.create_task(
                TaskRecord(
                    run_id="run",
                    task_id=f"task-{index}",
                    trait_id="quality",
                    item_id=f"{index:03d}",
                    scheduling_ordinal=index,
                )
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(12)))
    assert registry.summary("run").tasks == 12
    assert (
        len([event for event in registry.events("run") if event.entity_kind == "task"])
        == 12
    )


def test_busy_writer_errors_are_safe(registry: Repository) -> None:
    with Repository(registry.root, busy_timeout_ms=0) as competing:
        with registry.transaction():
            with pytest.raises(PersistenceError, match="database operation failed"):
                competing.create_task(
                    TaskRecord(
                        run_id="run",
                        task_id="task",
                        trait_id="quality",
                        item_id="001",
                        scheduling_ordinal=0,
                    )
                )
    assert registry.summary("run").tasks == 0


def test_process_death_rolls_back_uncommitted_records(registry: Repository) -> None:
    program = """
import os, sys
from judgmentllm.persistence.repository import Repository
from judgmentllm.persistence.models import TaskRecord
repository = Repository(sys.argv[1])
with repository.transaction():
    repository.create_task(TaskRecord(run_id='run',task_id='uncommitted',trait_id='quality',item_id='001',scheduling_ordinal=0))
    os._exit(21)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(registry.root)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 21, result.stderr
    with Repository(registry.root) as reopened:
        assert reopened.summary("run").tasks == 0
        assert not [
            event
            for event in reopened.events("run")
            if event.entity_id == "uncommitted"
        ]


def test_manifest_and_artifact_identity_are_immutable(registry: Repository) -> None:
    original = registry.get_run("run").manifest_sha256
    store = ArtifactStore(registry.run_directory("run"))
    replacement = store.put(b'{"changed":true}', "application/json")
    with pytest.raises(IntegrityError):
        registry.seal_run("run", ManifestReference(artifact=replacement))
    assert registry.get_run("run").manifest_sha256 == original
    identity = store.put(b"synthetic artifact")
    registry.register_artifact("run", identity)
    before = len(list(registry.events("run")))
    registry.register_artifact("run", identity)
    assert len(list(registry.events("run"))) == before
    with pytest.raises(IntegrityError):
        registry.register_artifact(
            "run", identity.model_copy(update={"media_type": "different/type"})
        )
    missing = store.put(b"missing")
    path = store.verify(missing)
    assert path is not None
    path.unlink()
    with pytest.raises(ArtifactIntegrityError):
        registry.register_artifact("run", missing)


def test_artifact_and_manifest_queries_verify_retained_references(
    registry: Repository,
) -> None:
    manifest = registry.get_manifest("run")
    assert manifest.artifact.sha256 == registry.get_run("run").manifest_sha256
    assert (
        ArtifactStore(registry.run_directory("run")).read(manifest.artifact)
        == b'{"synthetic":true}'
    )
    assert registry.get_artifact("run", manifest.artifact.sha256) == manifest.artifact
    with pytest.raises(NotFoundError):
        registry.get_artifact("run", "0" * 64)
    with pytest.raises(NotFoundError):
        registry.get_manifest("absent")
    registry.create_run(
        RunRecord(
            run_id="unsealed",
            task_kind="rubric",
            provider="mock",
            endpoint="judgments",
            model="fixture",
        )
    )
    with pytest.raises(NotFoundError, match="no sealed manifest"):
        registry.get_manifest("unsealed")


def test_repeated_same_manifest_seal_does_not_fabricate_an_event(
    registry: Repository,
) -> None:
    manifest = registry.get_manifest("run")
    before = list(registry.events("run"))
    with pytest.raises(IntegrityError):
        registry.seal_run("run", manifest)
    assert list(registry.events("run")) == before
    assert registry.get_manifest("run") == manifest


@pytest.mark.parametrize("target", ["artifact", "manifest"])
@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_reopened_reference_queries_detect_corruption_and_disappearance(
    registry: Repository, target: str, damage: str
) -> None:
    store = ArtifactStore(registry.run_directory("run"))
    if target == "manifest":
        identity = registry.get_manifest("run").artifact
    else:
        identity = store.put(b"synthetic retained object")
        registry.register_artifact("run", identity)
    path = store.verify(identity)
    assert path is not None
    if damage == "corrupt":
        path.write_bytes(b"damaged object")
    else:
        path.unlink()
    with Repository(registry.root) as reopened:
        with pytest.raises(ArtifactIntegrityError):
            reopened.get_artifact("run", identity.sha256)
        if target == "manifest":
            with pytest.raises(ArtifactIntegrityError):
                reopened.get_manifest("run")
            assert reopened.get_run("run").manifest_sha256 == identity.sha256
        if damage == "corrupt":
            assert path.read_bytes() == b"damaged object"
        else:
            assert not path.exists()


def test_hash_only_reference_cannot_be_silently_upgraded(registry: Repository) -> None:
    data = b"synthetic hash-only content"
    identity = ArtifactStore.hash_only(bytes_digest(data), len(data))
    registry.register_artifact("run", identity)
    with Repository(registry.root) as reopened:
        assert reopened.get_artifact("run", identity.sha256) == identity
        assert reopened.get_artifact("run", identity.sha256).relative_path is None
        retained = ArtifactStore(reopened.run_directory("run")).put(data)
        with pytest.raises(IntegrityError, match="registered differently"):
            reopened.register_artifact("run", retained)
        assert reopened.get_artifact("run", identity.sha256) == identity


@pytest.mark.parametrize(
    "table", ["events", "artifacts", "request_tasks", "attempt_tasks"]
)
def test_immutable_tables_reject_update_and_delete(
    registry: Repository, table: str
) -> None:
    tasks = _group(registry)
    _attempt(registry, tasks)
    with pytest.raises(sqlite3.IntegrityError):
        registry.connection.execute(f"DELETE FROM {table} WHERE run_id='run'")
    column = {
        "events": "entity_id",
        "artifacts": "media_type",
        "request_tasks": "position",
        "attempt_tasks": "position",
    }[table]
    value: str | int = "changed" if column in {"entity_id", "media_type"} else 9
    with pytest.raises(sqlite3.IntegrityError):
        registry.connection.execute(
            f"UPDATE {table} SET {column}=? WHERE run_id='run'", (value,)
        )


def test_grouped_success_invalid_missing_survive_restart_and_exports(
    registry: Repository, tmp_path: Path
) -> None:
    tasks = _group(registry, 3)
    _attempt(registry, tasks)
    observation = _accepted(registry)
    records = [
        _complete(),
        _failed(index=1),
        _failed(index=2, error_kind="missing_output"),
    ]
    with registry.transaction():
        for record in records:
            registry.append_result(record, observation_id=observation)
    with Repository(registry.root) as reopened:
        persisted = list(reopened.iter_results("run", page_size=1))
        assert persisted == records
        assert reopened.summary("run").results == 3
        assert reopened.summary("run").successful_results == 1
        for record in persisted[1:]:
            assert isinstance(record, RubricResult)
            assert record.primary_score is None
            assert record.expected_value is None
    jsonl = tmp_path / "audit.jsonl"
    csv = tmp_path / "audit.csv"
    r_csv = tmp_path / "rubric_r.csv"
    assert write_results_jsonl(jsonl, iter(persisted)) == 3
    assert export_results_csv(csv, iter(persisted)) == 3
    assert (
        export_rubric_r_csv(
            r_csv, (record for record in persisted if isinstance(record, RubricResult))
        )
        == 3
    )
    assert list(read_results(jsonl)) == records
    assert list(read_results_csv(csv)) == records
    import csv as csv_module

    with r_csv.open(newline="", encoding="utf-8") as stream:
        rows = list(csv_module.DictReader(stream))
    assert [row["status"] for row in rows] == ["complete", "invalid", "invalid"]
    assert [row["item_id"] for row in rows] == ["001"] * 3


def test_group_transaction_failure_leaves_no_partial_results(
    registry: Repository,
) -> None:
    tasks = _group(registry, 2)
    _attempt(registry, tasks)
    observation = _accepted(registry)
    before = len(list(registry.events("run")))
    with pytest.raises(IntegrityError):
        with registry.transaction():
            registry.append_result(_complete(), observation_id=observation)
            registry.append_result(_complete(), observation_id=observation)
    assert list(registry.iter_results("run")) == []
    assert len(list(registry.events("run"))) == before


@pytest.mark.parametrize("state", ["prepared", "submitting", "uncertain"])
def test_no_results_or_observations_for_unattempted_or_uncertain_work(
    registry: Repository, state: str
) -> None:
    tasks = _group(registry)
    _attempt(registry, tasks)
    if state != "prepared":
        registry.transition("attempt", "run", "attempt-1", "claimed")
        registry.transition("attempt", "run", "attempt-1", "submitting")
        registry.transition("submission", "run", "submission-1", "submitting")
        if state == "uncertain":
            registry.transition("attempt", "run", "attempt-1", "uncertain")
            registry.transition("submission", "run", "submission-1", "uncertain")
    with pytest.raises(IntegrityError):
        registry.record_observation(
            ObservationRecord(
                run_id="run",
                observation_id="fabricated",
                submission_id="submission-1",
                attempt_id="attempt-1",
                observation_key="primary",
                kind="response",
            )
        )
    with pytest.raises(IntegrityError):
        registry.append_result(_failed(), failure_submission_id="submission-1")
    with pytest.raises(IntegrityError):
        registry.append_result(_complete(), observation_id="absent")
    assert registry.summary("run").results == 0
    assert (
        registry.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        == 0
    )


def test_result_requires_exactly_one_evidence_source(registry: Repository) -> None:
    with pytest.raises(IntegrityError, match="exactly one"):
        registry.append_result(_complete())
    with pytest.raises(IntegrityError, match="exactly one"):
        registry.append_result(
            _complete(),
            observation_id="observation",
            failure_submission_id="submission",
        )


def test_definitive_transport_failure_is_not_provider_refusal(
    registry: Repository,
) -> None:
    tasks = _group(registry)
    _attempt(registry, tasks)
    registry.transition("attempt", "run", "attempt-1", "claimed")
    registry.transition("attempt", "run", "attempt-1", "submitting")
    registry.transition("submission", "run", "submission-1", "submitting")
    registry.transition("submission", "run", "submission-1", "confirmed_not_submitted")
    registry.transition("attempt", "run", "attempt-1", "not_submitted")
    registry.record_failure(
        FailureEvidence(
            run_id="run",
            attempt_id="attempt-1",
            submission_id="submission-1",
            kind="transport_not_submitted",
        )
    )
    failed = _failed(status="errored", error_kind="transport_not_submitted")
    registry.append_result(failed, failure_submission_id="submission-1")
    assert (
        registry.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        == 0
    )
    authorization = RetryAuthorization(
        run_id="run",
        operation_id="retry",
        selector_sha256=semantic_digest("retry", tasks),
    )
    registry.authorize_retry(authorization)
    _attempt(registry, tasks, number=2, authorization_id="retry")
    refusal_observation = _accepted(registry, number=2, kind="refusal")
    refused = _failed(number=2, status="refused", error_kind="provider_refusal")
    registry.append_result(refused, observation_id=refusal_observation)
    with Repository(registry.root) as reopened:
        assert list(reopened.iter_results("run")) == [failed, refused]
        assert (
            reopened.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[
                0
            ]
            == 1
        )
    with pytest.raises(IntegrityError):
        registry.append_result(_complete(), failure_submission_id="submission-1")


def test_failed_member_retry_has_distinct_subset_and_immutable_history(
    registry: Repository,
) -> None:
    tasks = _group(registry, 2)
    _attempt(registry, tasks)
    first_observation = _accepted(registry)
    first = _complete()
    failed = _failed(index=1, error_kind="missing_output")
    registry.append_result(first, observation_id=first_observation)
    registry.append_result(failed, observation_id=first_observation)
    authorization = RetryAuthorization(
        run_id="run",
        operation_id="retry-one",
        selector_sha256=semantic_digest("retry", [tasks[1]]),
    )
    with pytest.raises(KeyboardInterrupt):
        with registry.transaction():
            registry.authorize_retry(authorization)
            raise KeyboardInterrupt
    assert (
        registry.connection.execute("SELECT COUNT(*) FROM retry_operations").fetchone()[
            0
        ]
        == 0
    )
    registry.authorize_retry(authorization)
    registry.authorize_retry(authorization)
    _attempt(
        registry, [tasks[1]], number=2, authorization_id=authorization.operation_id
    )
    with Repository(registry.root) as resumed:
        second_observation = _accepted(resumed, number=2)
        retried = _complete(index=1, number=2)
        resumed.append_result(retried, observation_id=second_observation)
        assert list(resumed.iter_results("run")) == [first, failed, retried]
        assert [
            row[0]
            for row in resumed.connection.execute(
                "SELECT task_id FROM request_tasks ORDER BY position"
            )
        ] == tasks
        assert [
            row[0]
            for row in resumed.connection.execute(
                "SELECT task_id FROM attempt_tasks WHERE attempt_id='attempt-2' ORDER BY position"
            )
        ] == [tasks[1]]
        with pytest.raises(IntegrityError):
            resumed.append_result(
                _complete(number=2), observation_id=second_observation
            )
        with pytest.raises(IntegrityError):
            resumed.authorize_retry(
                authorization.model_copy(update={"selector_sha256": "b" * 64})
            )
        rows = resumed.connection.execute(
            "SELECT payload_sha256 FROM attempts ORDER BY attempt_number"
        ).fetchall()
        assert rows[0][0] != rows[1][0]


def test_pairwise_orientation_probabilities_and_failures_survive(
    registry: Repository, tmp_path: Path
) -> None:
    _run(registry, "pair-run", pairwise=True)
    tasks = _group(registry, run_id="pair-run", pairwise=True)
    _attempt(registry, tasks, run_id="pair-run")
    observation = _accepted(registry, run_id="pair-run")
    pair = PairTask(
        pair_id="pair",
        pair_uid="occurrence",
        left_item_id="001",
        right_item_id="002",
        trait_id="quality-0",
        presented_order=["002", "001"],
        source_pair_uid="source",
        repetition_index=1,
        is_reversal=True,
        reversal_group="reversal",
    )
    complete = normalize_pairwise_result(
        pair,
        run_id="pair-run",
        task_id="task-0",
        request_uid="request",
        attempt_id="attempt-1",
        winner_id="001",
        p_left=0.8,
        p_right=0.2,
    )
    registry.append_result(complete, observation_id=observation)
    authorization = RetryAuthorization(
        run_id="pair-run",
        operation_id="explicit-repeat",
        selector_sha256=semantic_digest("retry", tasks),
        allow_completed=True,
    )
    registry.authorize_retry(authorization)
    _attempt(
        registry,
        tasks,
        run_id="pair-run",
        number=2,
        authorization_id=authorization.operation_id,
    )
    second_observation = _accepted(registry, run_id="pair-run", number=2)
    failed = PairwiseResult.model_validate(
        {
            **complete.model_dump(),
            "attempt_id": "attempt-2",
            "status": "invalid",
            "winner_id": None,
            "p_left": None,
            "p_right": None,
            "probability_sum_tolerance": None,
            "error_kind": "invalid_output",
        }
    )
    registry.append_result(failed, observation_id=second_observation)
    with Repository(registry.root) as reopened:
        records = list(reopened.iter_results("pair-run"))
        assert records == [complete, failed]
    assert (
        export_pairwise_r_csv(
            tmp_path / "pairwise_r.csv",
            (record for record in records if isinstance(record, PairwiseResult)),
        )
        == 2
    )


def test_result_observation_and_audit_records_are_append_only(
    registry: Repository,
) -> None:
    tasks = _group(registry)
    _attempt(registry, tasks)
    observation = _accepted(registry)
    registry.append_result(_complete(), observation_id=observation)
    with pytest.raises(IntegrityError):
        registry.append_result(_complete(), observation_id=observation)
    for table in ("results", "observations", "events"):
        with pytest.raises(sqlite3.IntegrityError):
            registry.connection.execute(f"DELETE FROM {table}")
        column = (
            "status"
            if table == "results"
            else "observation_key"
            if table == "observations"
            else "entity_id"
        )
        with pytest.raises(sqlite3.IntegrityError):
            registry.connection.execute(f"UPDATE {table} SET {column}='changed'")
        with pytest.raises(sqlite3.IntegrityError):
            registry.connection.execute(f"UPDATE {table} SET {column}={column}")


def test_mutated_result_and_execution_models_revalidated(registry: Repository) -> None:
    tasks = _group(registry)
    _attempt(registry, tasks)
    observation = _accepted(registry)
    invalid = _complete().model_copy(
        update={"primary_score": 999, "unrequested": "private-sentinel"}
    )
    with pytest.raises(IntegrityError):
        registry.append_result(invalid, observation_id=observation)
    with pytest.raises(ValueError):
        registry.create_run(
            RunRecord(
                run_id="new",
                task_kind="rubric",
                provider="mock",
                endpoint="judgments",
                model="fixture",
            ).model_copy(update={"unknown": "private-sentinel"})
        )
    assert list(registry.iter_results("run")) == []


def test_read_results_uses_fixed_high_water_mark(registry: Repository) -> None:
    tasks = _group(registry, 2)
    _attempt(registry, tasks)
    observation = _accepted(registry)
    registry.append_result(_complete(), observation_id=observation)
    iterator = registry.iter_results("run", page_size=1)
    assert next(iterator) == _complete()
    registry.append_result(_complete(index=1), observation_id=observation)
    assert list(iterator) == []
    assert len(list(registry.iter_results("run"))) == 2


def test_database_errors_do_not_echo_synthetic_assessment_text(
    registry: Repository,
) -> None:
    _group(registry)
    with pytest.raises(IntegrityError) as error:
        registry.create_task(
            TaskRecord(
                run_id="run",
                task_id="task-0",
                trait_id="private-sentinel",
                item_id="001",
                scheduling_ordinal=1,
            )
        )
    assert "private-sentinel" not in str(error.value)
    assert "private-sentinel" not in json.dumps(
        [event.model_dump() for event in registry.events("run")]
    )
