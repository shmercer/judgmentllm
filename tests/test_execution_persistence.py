"""Ownership handoff, bounded execution reads and immutable response receipts."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    ArtifactIntegrityError,
    AttemptRecord,
    IntegrityError,
    ManifestReference,
    NotFoundError,
    ObservationRecord,
    OwnershipError,
    RequestRecord,
    RunRecord,
    SubmissionRecord,
    TaskRecord,
)
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.receipts import (
    AcknowledgmentReceipt,
    ResponseReceipt,
    publish_acknowledgment,
    publish_receipt,
    read_acknowledgment,
    read_receipt,
)
from judgmentllm.persistence.repository import Repository


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[Repository]:
    with Repository(tmp_path) as repository:
        repository.create_run(
            RunRecord(
                run_id="run",
                task_kind="rubric",
                provider="mock",
                endpoint="judgments",
                model="fixture",
            )
        )
        manifest = ArtifactStore(repository.run_directory("run")).put(b"{}")
        repository.seal_run("run", ManifestReference(artifact=manifest))
        yield repository


def _prepare(repository: Repository, ordinal: int) -> AttemptRecord:
    task_id, request_uid = f"task-{ordinal}", f"request-{ordinal}"
    repository.create_task(
        TaskRecord(
            run_id="run",
            task_id=task_id,
            trait_id="quality",
            item_id="001",
            scheduling_ordinal=ordinal,
        )
    )
    repository.create_request(
        RequestRecord(
            run_id="run",
            request_uid=request_uid,
            scheduling_ordinal=ordinal,
            dependency_sha256=bytes_digest(b"dependency"),
        ),
        [task_id],
    )
    prompt, payload = (
        ArtifactStore.hash_only(bytes_digest(value), len(value))
        for value in (b"prompt", b"payload")
    )
    repository.register_artifact("run", prompt)
    repository.register_artifact("run", payload)
    attempt = AttemptRecord(
        run_id="run",
        attempt_id=f"attempt-{ordinal}",
        request_uid=request_uid,
        attempt_number=1,
        prompt_sha256=prompt.sha256,
        payload_sha256=payload.sha256,
        membership_sha256=semantic_digest("attempt-membership", [task_id]),
    )
    repository.create_attempt(attempt, [task_id])
    repository.create_submission(
        SubmissionRecord(
            run_id="run",
            submission_id=f"submission-{ordinal}",
            correlation_id=f"correlation-{ordinal}",
            attempt_id=attempt.attempt_id,
            payload_sha256=payload.sha256,
        )
    )
    return attempt


def test_owner_checked_execution_transactions_and_audit(registry: Repository) -> None:
    with pytest.raises(OwnershipError):
        with registry.execution_transaction("run", "absent"):
            pytest.fail("unowned work was executable")
    ownership = ExecutorOwnership(registry, "run")
    with ownership:
        assert ownership.owner is not None
        token = ownership.owner.token
        with registry.execution_transaction("run", token):
            _prepare(registry, 0)
            with registry.execution_transaction("run", token):
                registry.transition("attempt", "run", "attempt-0", "claimed")
            registry.advance_execution_checkpoint("run", 1, owner_token=token)
        assert registry.get_execution_checkpoint("run") == 1
        events = list(registry.events("run"))
        assert [e for e in events if e.entity_kind in {"task", "attempt", "submission"}]
        assert all(
            e.owner_token == token
            for e in events
            if e.entity_kind in {"task", "attempt", "submission"}
        )
        with pytest.raises(OwnershipError):
            with registry.execution_transaction("run", token):
                with registry.execution_transaction("run", "stale"):
                    pytest.fail("nested stale token accepted")
        with pytest.raises(RuntimeError):
            with registry.execution_transaction("run", token):
                registry.advance_execution_checkpoint("run", 2, owner_token=token)
                raise RuntimeError("interrupt transaction")
        assert registry.get_execution_checkpoint("run") == 1
        with pytest.raises(IntegrityError, match="decrease"):
            registry.advance_execution_checkpoint("run", 0, owner_token=token)
    with ExecutorOwnership(registry, "run"):
        with pytest.raises(OwnershipError):
            with registry.execution_transaction("run", token):
                pytest.fail("prior owner reclaimed work")


def test_execution_transaction_rejects_cross_run_and_owner_replacement(
    registry: Repository,
) -> None:
    with ExecutorOwnership(registry, "run") as ownership:
        assert ownership.owner is not None
        token = ownership.owner.token
        with pytest.raises(OwnershipError, match="another run"):
            with registry.execution_transaction("run", token):
                registry.create_run(
                    RunRecord(
                        run_id="other",
                        task_kind="rubric",
                        provider="mock",
                        endpoint="judgments",
                        model="fixture",
                    )
                )
        with pytest.raises(NotFoundError):
            registry.get_run("other")
        with pytest.raises(OwnershipError):
            with registry.execution_transaction("run", token):
                registry.connection.execute(
                    "UPDATE runs SET owner_token='replaced' WHERE run_id='run'"
                )
        assert registry.get_owner("run") == ownership.owner


@pytest.mark.parametrize("checkpoint", ["bad", "-1", "01", "９", "9" * 20, str(2**63)])
def test_corrupt_preparation_checkpoint_fails_safely(
    registry: Repository, checkpoint: str
) -> None:
    registry.connection.execute(
        "UPDATE runs SET checkpoint=? WHERE run_id='run'", (checkpoint,)
    )
    with pytest.raises(IntegrityError, match="checkpoint"):
        registry.get_execution_checkpoint("run")


@pytest.mark.parametrize("ordinal", [-1, True, 1.5, 2**63])
def test_invalid_preparation_checkpoint_is_rejected(
    registry: Repository, ordinal: int
) -> None:
    with pytest.raises(ValueError, match="ordinal"):
        registry.advance_execution_checkpoint("run", ordinal, owner_token="unused")


def test_typed_execution_reads_and_small_keyset_pages(registry: Repository) -> None:
    assert registry.get_execution_checkpoint("run") == 0
    for ordinal in (2, 0, 1):
        _prepare(registry, ordinal)
    assert [
        r.scheduling_ordinal for r in registry.iter_requests("run", page_size=1)
    ] == [0, 1, 2]
    assert [a.request_uid for a in registry.iter_attempts("run", page_size=1)] == [
        "request-0",
        "request-1",
        "request-2",
    ]
    assert [s.submission_id for s in registry.iter_submissions("run", page_size=1)] == [
        "submission-0",
        "submission-1",
        "submission-2",
    ]
    assert registry.get_task("run", "task-0").item_id == "001"
    assert registry.get_attempt_for_request("run", "request-0") == registry.get_attempt(
        "run", "attempt-0"
    )
    assert (
        registry.get_attempt_for_request("run", "request-0", attempt_number=2) is None
    )
    assert registry.get_submission_for_attempt("run", "missing") is None
    assert registry.get_observation_for_submission("run", "submission-0") is None
    assert registry.get_result_for_attempt("run", "attempt-0") is None
    assert registry.request_task_ids("run", "request-0") == ("task-0",)
    assert registry.attempt_task_ids("run", "attempt-0") == ("task-0",)
    for factory in (
        registry.iter_requests,
        registry.iter_attempts,
        registry.iter_submissions,
    ):
        with pytest.raises(ValueError, match="page size"):
            list(factory("run", page_size=0))
    for getter in (
        registry.get_task,
        registry.get_request,
        registry.get_attempt,
        registry.get_submission,
        registry.get_observation,
    ):
        with pytest.raises(NotFoundError):
            getter("run", "absent")


def test_single_observation_lookup_rejects_multiple_evidence(
    registry: Repository,
) -> None:
    _prepare(registry, 0)
    registry.transition("attempt", "run", "attempt-0", "claimed")
    registry.transition("attempt", "run", "attempt-0", "submitting")
    registry.transition("submission", "run", "submission-0", "submitting")
    registry.transition("submission", "run", "submission-0", "acknowledged")
    observation = ObservationRecord(
        run_id="run",
        attempt_id="attempt-0",
        submission_id="submission-0",
        observation_id="observation-0",
        observation_key="first",
        kind="response",
    )
    registry.record_observation(observation)
    assert registry.get_observation_for_submission("run", "submission-0") == observation
    registry.record_observation(
        observation.model_copy(
            update={"observation_id": "observation-1", "observation_key": "second"}
        )
    )
    with pytest.raises(IntegrityError, match="conflicting"):
        registry.get_observation_for_submission("run", "submission-0")


def _receipt(store: ArtifactStore, *, minimal: bool = False) -> ResponseReceipt:
    raw = b"synthetic-raw-only-sentinel"
    identity = (
        store.hash_only(bytes_digest(raw), len(raw)) if minimal else store.put(raw)
    )
    return ResponseReceipt(
        run_id="run",
        attempt_id="attempt",
        submission_id="submission",
        correlation_id="correlation",
        payload_sha256=bytes_digest(b"payload"),
        observation_id="observation",
        observation_key="primary",
        kind="response",
        capture="minimal" if minimal else "full",
        raw=identity,
        received_at="2026-11-02T00:00:00+00:00",
    )


@pytest.mark.parametrize("minimal", [False, True])
def test_receipt_reopening_and_capture_policy(tmp_path: Path, minimal: bool) -> None:
    store = ArtifactStore(tmp_path)
    assert read_receipt(store, "submission") is None
    receipt = _receipt(store, minimal=minimal)
    path = publish_receipt(store, receipt)
    assert publish_receipt(store, receipt) == path
    reopened = read_receipt(ArtifactStore(tmp_path), "submission")
    assert reopened is not None
    assert reopened == receipt
    assert reopened.observation().raw_sha256 == receipt.raw.sha256
    if minimal:
        assert all(
            b"synthetic-raw-only-sentinel" not in p.read_bytes()
            for p in tmp_path.rglob("*")
            if p.is_file()
        )
    else:
        assert store.read(receipt.raw) == b"synthetic-raw-only-sentinel"
    assert not list(tmp_path.rglob(".pending-*"))


def test_receipt_submission_binding_and_conflicts(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    receipt = _receipt(store)
    submission = SubmissionRecord(
        run_id=receipt.run_id,
        attempt_id=receipt.attempt_id,
        submission_id=receipt.submission_id,
        correlation_id=receipt.correlation_id,
        payload_sha256=receipt.payload_sha256,
    )
    receipt.verify_submission(submission)
    with pytest.raises(ArtifactIntegrityError, match="submission"):
        receipt.verify_submission(
            submission.model_copy(update={"payload_sha256": bytes_digest(b"different")})
        )
    publish_receipt(store, receipt)
    with pytest.raises(ArtifactIntegrityError, match="conflicting"):
        publish_receipt(store, receipt.model_copy(update={"observation_id": "other"}))
    assert read_receipt(store, "submission") == receipt


@pytest.mark.parametrize(
    "change", ["malformed", "oversized", "identity", "missing_object", "raw_corrupt"]
)
def test_receipt_corruption_never_becomes_evidence(tmp_path: Path, change: str) -> None:
    store = ArtifactStore(tmp_path)
    receipt = _receipt(store)
    path = publish_receipt(store, receipt)
    if change == "malformed":
        path.write_bytes(b"private-raw-sentinel")
    elif change == "oversized":
        path.write_bytes(b"x" * 65537)
    elif change == "identity":
        path.write_bytes(
            canonical_bytes(receipt.model_copy(update={"submission_id": "different"}))
        )
    elif change == "missing_object":
        encoded = path.read_bytes()
        digest = bytes_digest(encoded)
        (tmp_path / "objects" / digest[:2] / digest).unlink()
    else:
        raw = store.verify(receipt.raw)
        assert raw is not None
        raw.write_bytes(b"corrupt")
    with pytest.raises(ArtifactIntegrityError) as error:
        read_receipt(store, "submission")
    assert "private-raw-sentinel" not in str(error.value)


def test_mutated_receipt_and_reference_traversal_fail(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    receipt = _receipt(store, minimal=True)
    with pytest.raises(ArtifactIntegrityError, match="invalid"):
        publish_receipt(store, receipt.model_copy(update={"extra": "secret-sentinel"}))
    with pytest.raises(ArtifactIntegrityError, match="invalid"):
        publish_receipt(store, receipt.model_copy(update={"capture": "full"}))
    with pytest.raises(ArtifactIntegrityError, match="location"):
        store.publish_reference(store.put(b"safe"), "../outside")
    with pytest.raises(ArtifactIntegrityError, match="retained"):
        store.publish_reference(
            store.hash_only(bytes_digest(b"safe"), 4), "receipt.json"
        )


def test_receipt_reference_symlinks_are_rejected(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    path = publish_receipt(store, _receipt(store))
    contents = path.read_bytes()
    other = tmp_path / "other.json"
    other.write_bytes(contents)
    path.unlink()
    try:
        path.symlink_to(other)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ArtifactIntegrityError, match="symlink"):
        read_receipt(store, "submission")


def _acknowledgment() -> AcknowledgmentReceipt:
    return AcknowledgmentReceipt(
        run_id="run",
        attempt_id="attempt",
        submission_id="submission",
        correlation_id="correlation",
        payload_sha256=bytes_digest(b"payload"),
        remote_id="remote",
        observation_key="original-observation",
    )


def test_acknowledgment_publication_preserves_expected_response_identity(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    receipt = _acknowledgment()
    assert read_acknowledgment(store, "submission") is None
    path = publish_acknowledgment(store, receipt)
    assert (
        path == store.root / "acknowledgments" / f"{bytes_digest(b'submission')}.json"
    )
    assert publish_acknowledgment(store, receipt) == path
    reopened = read_acknowledgment(ArtifactStore(tmp_path), "submission")
    assert reopened == receipt
    assert reopened is not None
    submission = SubmissionRecord(
        run_id=receipt.run_id,
        attempt_id=receipt.attempt_id,
        submission_id=receipt.submission_id,
        correlation_id=receipt.correlation_id,
        payload_sha256=receipt.payload_sha256,
    )
    reopened.verify_submission(submission)
    reopened.verify_submission(submission.model_copy(update={"remote_id": "remote"}))
    for field, value in (("remote_id", "other"), ("correlation_id", "other")):
        with pytest.raises(ArtifactIntegrityError, match="submission"):
            reopened.verify_submission(submission.model_copy(update={field: value}))
    with pytest.raises(ArtifactIntegrityError, match="conflicting"):
        publish_acknowledgment(
            store, receipt.model_copy(update={"observation_key": "replacement"})
        )
    assert read_acknowledgment(store, "submission") == receipt
    with pytest.raises(ArtifactIntegrityError, match="invalid"):
        publish_acknowledgment(
            store, receipt.model_copy(update={"raw_text": "private-sentinel"})
        )
    assert all(
        b"private-sentinel" not in p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    )


@pytest.mark.parametrize(
    "change", ["malformed", "oversized", "identity", "missing_object"]
)
def test_acknowledgment_corruption_is_rejected(tmp_path: Path, change: str) -> None:
    store = ArtifactStore(tmp_path)
    receipt = _acknowledgment()
    path = publish_acknowledgment(store, receipt)
    if change == "malformed":
        path.write_bytes(b"invalid-private-sentinel")
    elif change == "oversized":
        path.write_bytes(b"x" * 65537)
    elif change == "identity":
        path.write_bytes(
            canonical_bytes(receipt.model_copy(update={"submission_id": "other"}))
        )
    else:
        digest = bytes_digest(path.read_bytes())
        (store.root / "objects" / digest[:2] / digest).unlink()
    with pytest.raises(ArtifactIntegrityError) as error:
        read_acknowledgment(store, "submission")
    assert "private-sentinel" not in str(error.value)
