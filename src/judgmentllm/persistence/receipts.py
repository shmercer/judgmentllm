"""Durable response receipt identities, independent of observation transactions.

Full capture retains exact response bytes in the existing artifact store. Minimal
capture publishes only strict identity/hash metadata. Neither a receipt nor its
hash establishes acceptance: the executor must reconcile provider evidence before
binding it to an accepted submission in the registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

from pydantic import Field, field_validator, model_validator

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


class CompleteResponseEvidenceV1(ExecutionModel):
    """A complete received response bound to one guarded invocation."""

    acceptance_evidence_version: Literal[1] = 1
    run_id: ID
    request_uid: ID
    attempt_id: ID
    submission_id: ID
    correlation_id: ID
    payload_sha256: SHA256
    response_sha256: SHA256
    response_byte_length: int = Field(ge=0)
    http_status: int = Field(ge=200, lt=300)
    protocol: Literal["judgmentllm-decisions-offline-v1"]

    @field_validator("acceptance_evidence_version", mode="before")
    @classmethod
    def strict_evidence_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported acceptance evidence version")
        return value


class AcknowledgmentReceiptV2(AcknowledgmentReceipt):
    """Nullable provider ID requires independent invocation-bound evidence."""

    acknowledgment_version: Literal[2] = 2
    remote_id: ID | None  # pyright: ignore[reportIncompatibleVariableOverride]
    response_evidence: CompleteResponseEvidenceV1

    @field_validator("acknowledgment_version", mode="before")
    @classmethod
    def strict_ack_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("unsupported acknowledgment version")
        return value

    @model_validator(mode="after")
    def binding(self) -> AcknowledgmentReceiptV2:
        evidence = self.response_evidence
        if (
            self.run_id,
            self.attempt_id,
            self.submission_id,
            self.correlation_id,
            self.payload_sha256,
        ) != (
            evidence.run_id,
            evidence.attempt_id,
            evidence.submission_id,
            evidence.correlation_id,
            evidence.payload_sha256,
        ):
            raise ValueError("complete response evidence differs from invocation")
        return self


class CompleteResponseEvidenceV2(CompleteResponseEvidenceV1):
    acceptance_evidence_version: Literal[2] = 2  # pyright: ignore[reportIncompatibleVariableOverride]
    protocol: Literal["openai-decisions-production-v1"] = (  # pyright: ignore[reportIncompatibleVariableOverride]
        "openai-decisions-production-v1"  # pyright: ignore[reportIncompatibleVariableOverride]
    )

    @field_validator("acceptance_evidence_version", mode="before")
    @classmethod
    def strict_evidence_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("unsupported production acceptance evidence version")
        return value


class AcknowledgmentReceiptV3(AcknowledgmentReceiptV2):
    acknowledgment_version: Literal[3] = 3  # pyright: ignore[reportIncompatibleVariableOverride]
    response_evidence: CompleteResponseEvidenceV2  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("acknowledgment_version", mode="before")
    @classmethod
    def strict_ack_version(cls, value: object) -> object:
        if type(value) is not int or value != 3:
            raise ValueError("unsupported production acknowledgment version")
        return value


class CompleteResponseEvidenceV3(CompleteResponseEvidenceV2):
    acceptance_evidence_version: Literal[3] = 3  # pyright: ignore[reportIncompatibleVariableOverride]
    protocol: Literal["typesafe-systemone-production-v1"] = (  # pyright: ignore[reportIncompatibleVariableOverride]
        "typesafe-systemone-production-v1"
    )
    http_status: Literal[200] = 200  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("acceptance_evidence_version", mode="before")
    @classmethod
    def strict_evidence_version(cls, value: object) -> object:
        if type(value) is not int or value != 3:
            raise ValueError("unsupported TypeSafe acceptance evidence version")
        return value


class AcknowledgmentReceiptV4(AcknowledgmentReceiptV3):
    acknowledgment_version: Literal[4] = 4  # pyright: ignore[reportIncompatibleVariableOverride]
    response_evidence: CompleteResponseEvidenceV3  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("acknowledgment_version", mode="before")
    @classmethod
    def strict_ack_version(cls, value: object) -> object:
        if type(value) is not int or value != 4:
            raise ValueError("unsupported TypeSafe acknowledgment version")
        return value


class CompleteResponseEvidenceV4(CompleteResponseEvidenceV2):
    acceptance_evidence_version: Literal[4] = 4  # pyright: ignore[reportIncompatibleVariableOverride]
    protocol: Literal["gemini-decisions-production-v1"] = (  # pyright: ignore[reportIncompatibleVariableOverride]
        "gemini-decisions-production-v1"
    )
    http_status: Literal[200] = 200  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("acceptance_evidence_version", mode="before")
    @classmethod
    def strict_evidence_version(cls, value: object) -> object:
        if type(value) is not int or value != 4:
            raise ValueError("unsupported Gemini Decisions acceptance evidence version")
        return value


class AcknowledgmentReceiptV5(AcknowledgmentReceiptV3):
    acknowledgment_version: Literal[5] = 5  # pyright: ignore[reportIncompatibleVariableOverride]
    response_evidence: CompleteResponseEvidenceV4  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("acknowledgment_version", mode="before")
    @classmethod
    def strict_ack_version(cls, value: object) -> object:
        if type(value) is not int or value != 5:
            raise ValueError("unsupported Gemini Decisions acknowledgment version")
        return value


class DecisionsRejectionEvidence(ExecutionModel):
    """The identity of a complete rejection without provider error text."""

    run_id: ID
    request_uid: ID
    attempt_id: ID
    submission_id: ID
    correlation_id: ID
    payload_sha256: SHA256
    response_sha256: SHA256
    response_byte_length: int = Field(ge=0, le=8_388_608)


class TypeSafeRejectionEvidenceV1(DecisionsRejectionEvidence):
    http_status: Literal[400, 401, 413, 415, 422, 429]
    reason: Literal["max_tokens_exceeded", "payload_too_large", "request_rejected"]

    @model_validator(mode="after")
    def status_matches_reason(self) -> TypeSafeRejectionEvidenceV1:
        if (self.http_status == 413) != (self.reason == "payload_too_large") or (
            self.reason == "max_tokens_exceeded" and self.http_status != 400
        ):
            raise ValueError("TypeSafe rejection reason conflicts with HTTP status")
        return self


class GeminiDecisionsRejectionEvidenceV1(DecisionsRejectionEvidence):
    http_status: Literal[400, 401, 403, 404, 413, 429]
    reason: Literal[
        "max_tokens_exceeded", "payload_too_large", "safety_refusal", "request_rejected"
    ]

    @model_validator(mode="after")
    def status_matches_reason(self) -> GeminiDecisionsRejectionEvidenceV1:
        if (self.http_status == 413) != (self.reason == "payload_too_large") or (
            self.reason in {"max_tokens_exceeded", "safety_refusal"}
            and self.http_status != 400
        ):
            raise ValueError(
                "Gemini Decisions rejection reason conflicts with HTTP status"
            )
        return self


def _acknowledgment_path(submission_id: str) -> str:
    return f"acknowledgments/{bytes_digest(submission_id.encode('utf-8'))}.json"


def publish_acknowledgment(
    store: ArtifactStore, receipt: AcknowledgmentReceipt
) -> Path:
    """Durably bind accepted identities before the registry acknowledgment."""
    try:
        receipt_type = (
            AcknowledgmentReceiptV5
            if type(receipt) is AcknowledgmentReceiptV5
            else AcknowledgmentReceiptV4
            if type(receipt) is AcknowledgmentReceiptV4
            else AcknowledgmentReceiptV3
            if type(receipt) is AcknowledgmentReceiptV3
            else AcknowledgmentReceiptV2
            if isinstance(receipt, AcknowledgmentReceiptV2)
            else AcknowledgmentReceipt
        )
        receipt = receipt_type.model_validate_json(canonical_bytes(receipt))
    except (ValueError, TypeError):
        raise ArtifactIntegrityError("acknowledgment metadata is invalid") from None
    identity = store.put(canonical_bytes(receipt), "application/json")
    return store.publish_reference(
        identity, _acknowledgment_path(receipt.submission_id)
    )


def read_acknowledgment(
    store: ArtifactStore, submission_id: str
) -> AcknowledgmentReceipt | AcknowledgmentReceiptV2 | None:
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
        from judgmentllm.config import parse_json

        data = cast(dict[str, object], parse_json(encoded.decode("utf-8")))
        version = (
            data.get("acknowledgment_version")
            if isinstance(cast(object, data), dict)
            else None
        )
        receipt_type = (
            AcknowledgmentReceiptV5
            if type(version) is int and version == 5
            else AcknowledgmentReceiptV4
            if type(version) is int and version == 4
            else AcknowledgmentReceiptV3
            if type(version) is int and version == 3
            else AcknowledgmentReceiptV2
            if isinstance(cast(object, data), dict) and "acknowledgment_version" in data
            else AcknowledgmentReceipt
        )
        receipt = receipt_type.model_validate_json(encoded)
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
