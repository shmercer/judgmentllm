"""Durable response receipt identities, independent of observation transactions.

Full capture retains exact response bytes in the existing artifact store. Minimal
capture publishes only strict identity/hash metadata. Neither a receipt nor its
hash establishes acceptance: the executor must reconcile provider evidence before
binding it to an accepted submission in the registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import model_validator

from judgmentllm.models import ID
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import (
    SHA256,
    ArtifactIdentity,
    ArtifactIntegrityError,
    ExecutionModel,
    ObservationRecord,
    SubmissionRecord,
)


class ResponseReceipt(ExecutionModel):
    """Strict, immutable binding of a raw observation to its exact submission."""

    run_id: ID
    attempt_id: ID
    submission_id: ID
    correlation_id: ID
    payload_sha256: SHA256
    observation_id: ID
    observation_key: ID
    kind: Literal["response", "refusal", "provider_error", "confirmed_cancellation"]
    capture: Literal["full", "minimal"]
    raw: ArtifactIdentity
    received_at: str

    @model_validator(mode="after")
    def permitted_capture(self) -> ResponseReceipt:
        if (self.raw.relative_path is not None) != (self.capture == "full"):
            raise ValueError("receipt raw retention must match the capture policy")
        return self

    def verify_submission(self, submission: SubmissionRecord) -> None:
        """Reject receipts for different immutable work, even with matching text."""
        submission = SubmissionRecord.model_validate_json(canonical_bytes(submission))
        if (
            self.run_id != submission.run_id
            or self.attempt_id != submission.attempt_id
            or self.submission_id != submission.submission_id
            or self.correlation_id != submission.correlation_id
            or self.payload_sha256 != submission.payload_sha256
        ):
            raise ArtifactIntegrityError("receipt does not match its submission")

    def observation(self) -> ObservationRecord:
        """Return metadata only; repository acceptance guards remain mandatory."""
        return ObservationRecord(
            run_id=self.run_id,
            attempt_id=self.attempt_id,
            submission_id=self.submission_id,
            observation_id=self.observation_id,
            observation_key=self.observation_key,
            kind=self.kind,
            raw_sha256=self.raw.sha256,
            received_at=self.received_at,
        )


class AcknowledgmentReceipt(ExecutionModel):
    """Exact acknowledged service identity, without request or response bodies.

    Publish before changing local acceptance state. Recovery can then require
    the same remote identity and observation key rather than adopting the first
    subsequently retrieved response as if it matched the acknowledgment.
    """

    run_id: ID
    attempt_id: ID
    submission_id: ID
    correlation_id: ID
    payload_sha256: SHA256
    remote_id: ID
    observation_key: ID

    def verify_submission(self, submission: SubmissionRecord) -> None:
        submission = SubmissionRecord.model_validate_json(canonical_bytes(submission))
        if (
            self.run_id != submission.run_id
            or self.attempt_id != submission.attempt_id
            or self.submission_id != submission.submission_id
            or self.correlation_id != submission.correlation_id
            or self.payload_sha256 != submission.payload_sha256
            or (
                submission.remote_id is not None
                and self.remote_id != submission.remote_id
            )
        ):
            raise ArtifactIntegrityError("acknowledgment does not match its submission")


def _acknowledgment_path(submission_id: str) -> str:
    return f"acknowledgments/{bytes_digest(submission_id.encode('utf-8'))}.json"


def publish_acknowledgment(
    store: ArtifactStore, receipt: AcknowledgmentReceipt
) -> Path:
    """Durably bind accepted identities before the registry acknowledgment."""
    try:
        receipt = AcknowledgmentReceipt.model_validate_json(canonical_bytes(receipt))
    except (ValueError, TypeError):
        raise ArtifactIntegrityError("acknowledgment metadata is invalid") from None
    identity = store.put(canonical_bytes(receipt), "application/json")
    return store.publish_reference(
        identity, _acknowledgment_path(receipt.submission_id)
    )


def read_acknowledgment(
    store: ArtifactStore, submission_id: str
) -> AcknowledgmentReceipt | None:
    """Read verified immutable acknowledged identities for conservative recovery."""
    path = store.reference_path(_acknowledgment_path(submission_id))
    try:
        with path.open("rb") as stream:
            encoded = stream.read(65537)
    except FileNotFoundError:
        return None
    except OSError:
        raise ArtifactIntegrityError("acknowledgment receipt is unavailable") from None
    if len(encoded) > 65536:
        raise ArtifactIntegrityError("acknowledgment metadata is oversized")
    try:
        receipt = AcknowledgmentReceipt.model_validate_json(encoded)
        if (
            canonical_bytes(receipt) != encoded
            or receipt.submission_id != submission_id
        ):
            raise ValueError("acknowledgment identity does not match")
    except (ValueError, TypeError):
        raise ArtifactIntegrityError("acknowledgment metadata is invalid") from None
    digest = bytes_digest(encoded)
    store.verify(
        ArtifactIdentity(
            sha256=digest,
            byte_length=len(encoded),
            media_type="application/json",
            relative_path=f"objects/{digest[:2]}/{digest}",
        )
    )
    return receipt


def _relative_path(submission_id: str) -> str:
    return f"receipts/{bytes_digest(submission_id.encode('utf-8'))}.json"


def publish_receipt(store: ArtifactStore, receipt: ResponseReceipt) -> Path:
    """Publish identifiable evidence before its observation transaction commits."""
    try:
        receipt = ResponseReceipt.model_validate_json(canonical_bytes(receipt))
    except (ValueError, TypeError):
        raise ArtifactIntegrityError("response receipt metadata is invalid") from None
    store.verify(receipt.raw)
    identity = store.put(canonical_bytes(receipt), "application/json")
    return store.publish_reference(identity, _relative_path(receipt.submission_id))


def read_receipt(store: ArtifactStore, submission_id: str) -> ResponseReceipt | None:
    """Verify an existing named receipt and retained bytes; absent means absent."""
    path = store.reference_path(_relative_path(submission_id))
    try:
        with path.open("rb") as stream:
            # Metadata is intentionally bounded, including when a sidecar was
            # corrupted. No response bytes occur inside this representation.
            encoded = stream.read(65537)
    except FileNotFoundError:
        return None
    except OSError:
        raise ArtifactIntegrityError("response receipt is unavailable") from None
    if len(encoded) > 65536:
        raise ArtifactIntegrityError("response receipt metadata is oversized")
    try:
        receipt = ResponseReceipt.model_validate_json(encoded)
        if (
            canonical_bytes(receipt) != encoded
            or receipt.submission_id != submission_id
        ):
            raise ValueError("receipt identity does not match")
    except (ValueError, TypeError):
        raise ArtifactIntegrityError("response receipt metadata is invalid") from None
    # The named reference must agree with its original immutable object as well
    # as its internally consistent metadata. A rewritten sidecar is not evidence.
    identity = ArtifactIdentity(
        sha256=bytes_digest(encoded),
        byte_length=len(encoded),
        media_type="application/json",
        relative_path=f"objects/{bytes_digest(encoded)[:2]}/{bytes_digest(encoded)}",
    )
    store.verify(identity)
    store.verify(receipt.raw)
    return receipt
