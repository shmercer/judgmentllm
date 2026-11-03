"""Internal persistence contracts, version 1; no execution or provider behavior.

These models are deliberately separate from assessment schema version 1.
Execution metadata has no arbitrary text/metadata dictionary. Normalized results
remain the authorized location for assessment outputs, including explanations.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from judgmentllm.models import ID, VersionedModel

SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RunState = Literal[
    "created", "submitted", "running", "completed", "partial", "failed", "cancelled"
]
RequestState = Literal[
    "planned",
    "prepared",
    "active",
    "completed",
    "partial",
    "failed",
    "blocked",
    "cancelled",
]
TaskState = Literal[
    "pending",
    "active",
    "complete",
    "invalid",
    "refused",
    "errored",
    "cancelled",
    "blocked",
]
AttemptState = Literal[
    "prepared",
    "claimed",
    "submitting",
    "acknowledged",
    "response_saved",
    "normalized",
    "uncertain",
    "not_submitted",
    "errored",
    "cancelled",
]
SubmissionState = Literal[
    "prepared",
    "submitting",
    "acknowledged",
    "confirmed_not_submitted",
    "uncertain",
    "resolved",
]
SubmissionCertainty = Literal["not_started", "not_submitted", "unknown", "accepted"]
ReconciliationOutcome = Literal[
    "not_submitted", "accepted", "observation_available", "unresolved"
]
FailureKind = Literal["transport_not_submitted", "confirmed_cancellation"]
EventKind = Literal[
    "created",
    "transition",
    "manifest_sealed",
    "artifact_registered",
    "observation_recorded",
    "result_recorded",
    "failure_recorded",
    "retry_authorized",
    "owner_acquired",
    "owner_recovered",
    "owner_released",
]


def utc_now() -> str:
    """Return an unambiguous UTC timestamp for diagnostic audit ordering."""
    return datetime.now(UTC).isoformat()


class PersistenceError(RuntimeError):
    """Safe internal persistence failure; never contains assessment content."""


class IntegrityError(PersistenceError):
    """A constraint or persisted identity failed validation."""


class ArtifactIntegrityError(IntegrityError):
    """Retained artifact bytes are absent or inconsistent."""


class StateTransitionError(IntegrityError):
    """A lifecycle transition is not permitted."""


class SchemaVersionError(PersistenceError):
    """An unsupported or modified database schema was encountered."""


class OwnershipError(PersistenceError):
    """A run is owned, or its ownership token no longer matches."""


class NotFoundError(PersistenceError):
    """An identified durable record does not exist."""


class ExecutionModel(VersionedModel):
    """Frozen, strict persistence record; revalidated at repository boundaries."""

    model_config = ConfigDict(frozen=True)


class ArtifactIdentity(ExecutionModel):
    """Exact original-byte identity; null path explicitly means hash-only."""

    sha256: SHA256
    byte_length: int = Field(ge=0)
    media_type: ID = "application/octet-stream"
    relative_path: str | None = None

    @field_validator("relative_path")
    @classmethod
    def safe_path(cls, value: str | None) -> str | None:
        if value is not None:
            path = PurePosixPath(value)
            if (
                not value
                or path.is_absolute()
                or "\\" in value
                or any(p in {".", "..", ""} for p in value.split("/"))
                or ":" in value
                or any(
                    ord(character) < 32 or ord(character) == 127 for character in value
                )
            ):
                raise ValueError("artifact location must be a safe relative POSIX path")
        return value


class ManifestReference(ExecutionModel):
    """Immutable retained manifest identity, without an assessment-body schema."""

    artifact: ArtifactIdentity
    canonicalization_version: Literal[1] = 1

    @field_validator("canonicalization_version", mode="before")
    @classmethod
    def supported_canonicalization(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("canonicalization_version must be the supported integer 1")
        return value

    @model_validator(mode="after")
    def retained(self) -> ManifestReference:
        if self.artifact.relative_path is None:
            raise ValueError("manifest reference requires retained manifest bytes")
        return self


class OwnerInfo(ExecutionModel):
    """Diagnostics only: heartbeat age is never authority to take a lock."""

    token: ID
    pid: int = Field(gt=0)
    host: ID
    acquired_at: str
    heartbeat_at: str


class RunRecord(ExecutionModel):
    run_id: ID
    task_kind: Literal["rubric", "pairwise"]
    provider: ID
    endpoint: ID
    model: ID
    capture: Literal["full", "minimal"] = "full"
    state: RunState = "created"
    manifest_sha256: SHA256 | None = None
    created_at: str = Field(default_factory=utc_now)


class TaskRecord(ExecutionModel):
    run_id: ID
    task_id: ID
    trait_id: ID
    item_id: ID | None = None
    pair_uid: ID | None = None
    pair_id: ID | None = None
    scheduling_ordinal: int = Field(ge=0)
    state: TaskState = "pending"

    @model_validator(mode="after")
    def target(self) -> TaskRecord:
        if (self.item_id is None) == (self.pair_uid is None):
            raise ValueError("task needs exactly one item or pair occurrence")
        if (self.pair_uid is None) != (self.pair_id is None):
            raise ValueError("pair task needs pair identity and occurrence")
        return self


class RequestRecord(ExecutionModel):
    run_id: ID
    request_uid: ID
    scheduling_ordinal: int = Field(ge=0)
    dependency_sha256: SHA256
    state: RequestState = "planned"


class AttemptRecord(ExecutionModel):
    run_id: ID
    attempt_id: ID
    request_uid: ID
    attempt_number: int = Field(ge=1)
    prompt_sha256: SHA256
    payload_sha256: SHA256
    membership_sha256: SHA256
    authorization_id: ID | None = None
    state: AttemptState = "prepared"
    next_eligible_at: str | None = None


class SubmissionRecord(ExecutionModel):
    run_id: ID
    submission_id: ID
    correlation_id: ID
    payload_sha256: SHA256
    attempt_id: ID | None = None
    batch_uid: ID | None = None
    state: SubmissionState = "prepared"
    certainty: SubmissionCertainty = "not_started"
    remote_id: ID | None = None

    @model_validator(mode="after")
    def target(self) -> SubmissionRecord:
        if (self.attempt_id is None) == (self.batch_uid is None):
            raise ValueError("submission needs exactly one attempt or batch target")
        return self


class ObservationRecord(ExecutionModel):
    run_id: ID
    observation_id: ID
    submission_id: ID
    attempt_id: ID
    observation_key: ID
    kind: Literal["response", "refusal", "provider_error", "confirmed_cancellation"]
    raw_sha256: SHA256 | None = None
    received_at: str = Field(default_factory=utc_now)


class FailureEvidence(ExecutionModel):
    """Conclusive outcome of an invoked submission; not a provider observation."""

    run_id: ID
    attempt_id: ID
    submission_id: ID
    kind: FailureKind


class RetryAuthorization(ExecutionModel):
    run_id: ID
    operation_id: ID
    selector_sha256: SHA256
    allow_completed: bool = False


class ExecutionEvent(ExecutionModel):
    event_seq: int = Field(ge=1)
    run_id: ID
    kind: EventKind
    entity_kind: Literal[
        "run",
        "request",
        "task",
        "attempt",
        "submission",
        "artifact",
        "observation",
        "result",
        "retry",
    ]
    entity_id: ID
    previous_state: str | None = None
    new_state: str | None = None
    occurred_at: str
    owner_token: ID | None = None


class ExecutionSummary(ExecutionModel):
    run_id: ID
    state: RunState
    tasks: int = Field(ge=0)
    attempts: int = Field(ge=0)
    results: int = Field(ge=0)
    successful_results: int = Field(ge=0)
    unresolved_submissions: int = Field(ge=0)
    logical_completed_tasks: int = Field(default=0, ge=0)
    unsuccessful_results: int = Field(default=0, ge=0)
    retry_operations: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def result_counts(self) -> ExecutionSummary:
        if self.successful_results > self.results:
            raise ValueError("successful results cannot exceed total results")
        return self


TRANSITIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "run": {
        "created": ("submitted", "failed", "cancelled"),
        "submitted": ("running", "failed", "cancelled", "partial"),
        "running": ("completed", "partial", "failed", "cancelled"),
        "completed": ("running",),
        "partial": ("running", "submitted", "failed", "cancelled"),
        "failed": ("running", "submitted"),
        "cancelled": ("running", "submitted"),
    },
    "request": {
        "planned": ("prepared", "failed", "cancelled"),
        "prepared": ("active", "cancelled"),
        "active": ("completed", "partial", "failed", "blocked", "cancelled"),
        "blocked": ("active", "completed", "partial", "failed", "cancelled"),
        "completed": ("active",),
        "partial": ("active",),
        "failed": ("active",),
        "cancelled": ("prepared", "active"),
    },
    "task": {
        "pending": ("active", "cancelled"),
        "active": ("complete", "invalid", "refused", "errored", "cancelled", "blocked"),
        "blocked": ("active", "complete", "invalid", "refused", "errored", "cancelled"),
        "complete": ("active",),
        "invalid": ("active",),
        "refused": ("active",),
        "errored": ("active",),
        "cancelled": ("active",),
    },
    "attempt": {
        "prepared": ("claimed", "cancelled"),
        "claimed": ("prepared", "submitting", "cancelled"),
        "submitting": (
            "acknowledged",
            "response_saved",
            "uncertain",
            "not_submitted",
            "cancelled",
        ),
        "acknowledged": ("response_saved", "uncertain", "cancelled"),
        "response_saved": ("normalized", "errored"),
        "uncertain": ("acknowledged", "response_saved", "not_submitted", "cancelled"),
        "not_submitted": (),
        "normalized": (),
        "errored": (),
        "cancelled": (),
    },
    "submission": {
        "prepared": ("submitting",),
        "submitting": (
            "acknowledged",
            "confirmed_not_submitted",
            "uncertain",
            "resolved",
        ),
        "acknowledged": ("resolved", "uncertain"),
        "uncertain": ("acknowledged", "confirmed_not_submitted", "resolved"),
        "confirmed_not_submitted": (),
        "resolved": (),
    },
}


def validate_transition(entity: str, previous: str, target: str) -> None:
    """Validate a forward transition without echoing caller-supplied data."""
    if target not in TRANSITIONS.get(entity, {}).get(previous, ()):
        raise StateTransitionError("state transition is not permitted")
