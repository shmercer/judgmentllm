"""Typed persistence identities stay distinct from assessment interpretation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from judgmentllm.persistence.models import (
    ArtifactIdentity,
    AttemptRecord,
    ExecutionEvent,
    ExecutionSummary,
    FailureEvidence,
    ManifestReference,
    ObservationRecord,
    OwnerInfo,
    RequestRecord,
    RetryAuthorization,
    RunRecord,
    StateTransitionError,
    SubmissionRecord,
    TaskRecord,
    utc_now,
    validate_transition,
)
from judgmentllm.persistence.repository import Repository

DIGEST = "a" * 64


def run() -> RunRecord:
    return RunRecord(
        run_id="001",
        task_kind="rubric",
        provider="mock",
        endpoint="judgments",
        model="unlisted-model",
    )


def test_run_defaults_version_and_immutable_identity() -> None:
    record = run()
    assert record.run_id == "001"
    assert record.schema_version == 1
    assert record.capture == "full"
    assert record.state == "created"
    assert record.manifest_sha256 is None
    assert record.model == "unlisted-model"
    assert datetime.fromisoformat(record.created_at).utcoffset() == UTC.utcoffset(None)
    assert RunRecord.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError, match="frozen"):
        record.run_id = "002"


@pytest.mark.parametrize("version", [True, 1.0, "1", 0, 2, None])
def test_schema_version_is_strict_integer_one(version: object) -> None:
    data = run().model_dump()
    data["schema_version"] = version
    with pytest.raises(ValidationError):
        RunRecord.model_validate(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": 1},
        {"run_id": " 001"},
        {"run_id": "001\n"},
        {"capture": "anonymous"},
        {"state": "paused"},
        {"metadata": {"text": "private-sentinel"}},
    ],
)
def test_run_rejects_coercion_unknown_states_and_unstructured_metadata(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RunRecord.model_validate({**run().model_dump(), **changes})


def test_copied_invalid_model_rejected_at_repository_boundary(tmp_path: Path) -> None:
    copied = run().model_copy(update={"unexpected": "private-sentinel"})
    with Repository(tmp_path) as repository:
        with pytest.raises(ValueError) as error:
            repository.create_run(copied)
        assert "private-sentinel" not in str(error.value)
        assert (
            repository.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/objects/file",
        "../file",
        "objects/../file",
        "objects/./file",
        "objects//file",
        "objects/",
        "C:/file",
        "objects\\file",
        "objects/\nfile",
        "objects/\0file",
        "objects/\x7ffile",
    ],
)
def test_artifact_paths_are_portable_relative_and_control_free(path: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactIdentity(sha256=DIGEST, byte_length=0, relative_path=path)


@pytest.mark.parametrize(
    "changes",
    [
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"sha256": "g" * 64},
        {"byte_length": -1},
        {"byte_length": True},
        {"byte_length": 1.0},
    ],
)
def test_artifact_identity_is_exact_and_strict(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ArtifactIdentity.model_validate({"sha256": DIGEST, "byte_length": 0, **changes})


def test_hash_only_identity_cannot_be_manifest_reference() -> None:
    identity = ArtifactIdentity(sha256=DIGEST, byte_length=0)
    assert identity.relative_path is None
    with pytest.raises(ValidationError, match="retained"):
        ManifestReference(artifact=identity)
    retained = ArtifactIdentity(
        sha256=DIGEST,
        byte_length=0,
        relative_path=f"objects/aa/{DIGEST}",
    )
    reference = ManifestReference(artifact=retained)
    assert reference.canonicalization_version == 1
    assert (
        ManifestReference.model_validate_json(reference.model_dump_json()) == reference
    )
    with pytest.raises(ValidationError, match="frozen"):
        retained.relative_path = "replacement"


@pytest.mark.parametrize("version", [True, 1.0, "1", 2, None])
def test_manifest_canonicalization_version_is_strict(version: object) -> None:
    with pytest.raises(ValidationError):
        ManifestReference.model_validate(
            {
                "artifact": {
                    "sha256": DIGEST,
                    "byte_length": 0,
                    "relative_path": f"objects/aa/{DIGEST}",
                },
                "canonicalization_version": version,
            }
        )


@pytest.mark.parametrize(
    "target",
    [
        {},
        {"item_id": "001", "pair_uid": "pair", "pair_id": "underlying"},
        {"pair_uid": "pair"},
        {"pair_id": "underlying"},
        {"item_id": "001", "pair_id": "underlying"},
    ],
)
def test_task_has_exactly_one_complete_target(target: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        TaskRecord.model_validate(
            {
                "run_id": "001",
                "task_id": "task",
                "trait_id": "trait",
                "scheduling_ordinal": 0,
                **target,
            }
        )


def test_pair_task_retains_both_logical_pair_identifiers() -> None:
    record = TaskRecord(
        run_id="001",
        task_id="task",
        trait_id="trait",
        pair_uid="occurrence",
        pair_id="underlying",
        scheduling_ordinal=0,
    )
    assert record.pair_uid == "occurrence"
    assert record.pair_id == "underlying"
    assert record.item_id is None
    assert record.state == "pending"


def test_request_and_attempt_keep_structural_and_content_identities_separate() -> None:
    request = RequestRecord(
        run_id="001",
        request_uid="request",
        scheduling_ordinal=0,
        dependency_sha256=DIGEST,
    )
    attempt = AttemptRecord(
        run_id="001",
        attempt_id="attempt-2",
        request_uid=request.request_uid,
        attempt_number=2,
        prompt_sha256=DIGEST,
        payload_sha256="b" * 64,
        membership_sha256="c" * 64,
        authorization_id="retry-operation",
    )
    assert attempt.request_uid == request.request_uid
    assert attempt.attempt_id != request.request_uid
    assert attempt.prompt_sha256 != attempt.payload_sha256
    assert attempt.state == "prepared"
    assert attempt.authorization_id == "retry-operation"
    authorization = RetryAuthorization(
        run_id="001",
        operation_id="retry-operation",
        selector_sha256="d" * 64,
    )
    assert not authorization.allow_completed
    with pytest.raises(ValidationError):
        RetryAuthorization.model_validate(
            {
                **authorization.model_dump(),
                "allow_completed": "true",
            }
        )


@pytest.mark.parametrize("targets", [{}, {"attempt_id": "a", "batch_uid": "b"}])
def test_submission_requires_one_physical_target(targets: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        SubmissionRecord.model_validate(
            {
                "run_id": "001",
                "submission_id": "submission",
                "correlation_id": "correlation",
                "payload_sha256": DIGEST,
                **targets,
            }
        )


def test_submission_identity_and_certainty_are_not_observation_evidence() -> None:
    submission = SubmissionRecord(
        run_id="001",
        submission_id="submission",
        correlation_id="correlation",
        payload_sha256=DIGEST,
        attempt_id="attempt",
    )
    assert submission.state == "prepared"
    assert submission.certainty == "not_started"
    assert submission.remote_id is None
    assert submission.submission_id != submission.correlation_id
    observed = ObservationRecord(
        run_id="001",
        observation_id="observation",
        submission_id="submission",
        attempt_id="attempt",
        observation_key="provider-key",
        kind="refusal",
    )
    assert observed.raw_sha256 is None
    failure = FailureEvidence(
        run_id="001",
        attempt_id="attempt",
        submission_id="submission",
        kind="transport_not_submitted",
    )
    assert "observation_id" not in failure.model_dump()
    with pytest.raises(ValidationError):
        FailureEvidence.model_validate(
            {**failure.model_dump(), "kind": "timeout_unknown"}
        )


def test_events_reject_arbitrary_diagnostics() -> None:
    event = ExecutionEvent(
        event_seq=1,
        run_id="001",
        kind="transition",
        entity_kind="submission",
        entity_id="submission",
        previous_state="submitting",
        new_state="uncertain",
        occurred_at=utc_now(),
    )
    assert event.previous_state == "submitting"
    assert event.new_state == "uncertain"
    with pytest.raises(ValidationError):
        ExecutionEvent.model_validate(
            {**event.model_dump(), "message": "private-sentinel"}
        )
    owner = OwnerInfo(
        token="token",
        pid=1,
        host="host",
        acquired_at=utc_now(),
        heartbeat_at=utc_now(),
    )
    with pytest.raises(ValidationError):
        OwnerInfo.model_validate({**owner.model_dump(), "pid": True})


@pytest.mark.parametrize(
    "entity,path",
    [
        ("run", ("created", "submitted", "running", "partial", "running", "completed")),
        ("request", ("planned", "prepared", "active", "blocked", "active", "partial")),
        ("task", ("pending", "active", "blocked", "active", "invalid")),
        (
            "attempt",
            (
                "prepared",
                "claimed",
                "submitting",
                "uncertain",
                "acknowledged",
                "response_saved",
                "normalized",
            ),
        ),
        (
            "submission",
            ("prepared", "submitting", "uncertain", "acknowledged", "resolved"),
        ),
    ],
)
def test_recoverable_state_paths(entity: str, path: tuple[str, ...]) -> None:
    for previous, target in zip(path, path[1:], strict=False):
        validate_transition(entity, previous, target)


@pytest.mark.parametrize(
    "entity,previous,target",
    [
        ("run", "created", "completed"),
        ("run", "running", "running"),
        ("request", "blocked", "prepared"),
        ("task", "pending", "complete"),
        ("attempt", "uncertain", "prepared"),
        ("attempt", "normalized", "submitting"),
        ("submission", "uncertain", "prepared"),
        ("submission", "resolved", "submitting"),
        ("unknown-entity", "private-sentinel", "target"),
    ],
)
def test_no_silent_rewind_noop_or_unknown_state(
    entity: str,
    previous: str,
    target: str,
) -> None:
    with pytest.raises(StateTransitionError) as error:
        validate_transition(entity, previous, target)
    assert "private-sentinel" not in str(error.value)


def test_summary_counts_cannot_contradict_successful_observations() -> None:
    summary = ExecutionSummary(
        run_id="001",
        state="partial",
        tasks=2,
        attempts=1,
        results=2,
        successful_results=1,
        unresolved_submissions=0,
    )
    assert summary.results - summary.successful_results == 1
    for changes in ({"tasks": -1}, {"results": True}, {"successful_results": 3}):
        with pytest.raises(ValidationError):
            ExecutionSummary.model_validate({**summary.model_dump(), **changes})
