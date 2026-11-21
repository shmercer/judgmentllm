"""Version-one native batch contracts, independent of provider wire formats."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from judgmentllm.evidence._base import EvidenceIdentity, EvidenceModel
from judgmentllm.evidence.usage import ProviderUsage
from judgmentllm.models import ID
from judgmentllm.persistence.models import (
    SHA256,
    ArtifactIdentity,
    ExecutionModel,
    RunState,
)


class BatchLimitsV1(EvidenceModel):
    """Frozen synthetic limits; these declare no real provider capability."""

    max_members: int = Field(default=1000, gt=0)
    max_input_bytes: int = Field(default=16_777_216, gt=0)
    max_record_bytes: int = Field(default=1_048_576, gt=0)


class BatchControlOptionsV1(EvidenceModel):
    """One invocation's finite admission budget, never a spend guarantee."""

    deadline_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    max_polls: int = Field(default=1, gt=0)
    poll_interval_seconds: float = Field(default=1, ge=0, allow_inf_nan=False)
    max_io_steps: int = Field(default=1024, gt=0)
    max_result_rows: int = Field(default=128, gt=0)


class BatchMockOptions(ExecutionModel):
    """Caller-owned offline fixture and original independent service ledger."""

    ledger_path: Path | None = None
    fixture_path: Path | None = None
    reconciliation_available: bool = True
    retrieval_available: bool = True
    limits: BatchLimitsV1 = Field(default_factory=BatchLimitsV1)


class BatchContextV1(EvidenceModel):
    protocol: Literal["mock-native-batch-v1"] = "mock-native-batch-v1"
    service_id: ID
    fixture_sha256: SHA256
    limits: BatchLimitsV1


class BatchMemberV1(EvidenceModel):
    run_id: ID
    shard_id: ID
    member_key: SHA256
    request_uid: ID
    attempt_id: ID
    position: int = Field(ge=0)
    dependency_sha256: SHA256
    membership_sha256: SHA256
    prompt: ArtifactIdentity
    payload: ArtifactIdentity
    row_sha256: SHA256
    row_bytes: int = Field(gt=0)
    parent_receipt_sha256: SHA256 | None = None


class BatchShardV1(EvidenceModel):
    run_id: ID
    shard_id: ID
    ordinal: int = Field(ge=0)
    member_count: int = Field(gt=0)
    membership_sha256: SHA256
    input: ArtifactIdentity
    context: BatchContextV1


class BatchOperationIntentV1(EvidenceModel):
    run_id: ID
    shard_id: ID
    logical_id: SHA256
    instance_id: SHA256
    sequence: int = Field(ge=1, le=3)
    kind: Literal["upload", "create", "cancel"]
    service_id: ID
    manifest_sha256: SHA256
    payload_sha256: SHA256
    owner_token: ID
    remote_id: ID | None = None
    predecessor_sha256: SHA256 | None = None

    @model_validator(mode="after")
    def successor(self) -> Self:
        if (self.sequence == 1) != (self.predecessor_sha256 is None):
            raise ValueError("successor requires its predecessor proof")
        if self.kind == "cancel" and self.remote_id is None:
            raise ValueError("cancel requires the exact known job")
        return self


class BatchFenceV1(EvidenceModel):
    service_id: ID
    instance_id: SHA256
    intent_sha256: SHA256
    sender_token: ID
    proof_sha256: SHA256


class BatchOperationReceiptV1(EvidenceModel):
    instance_id: SHA256
    intent_sha256: SHA256
    outcome: Literal["accepted", "not_accepted", "unresolved"]
    remote_id: ID | None = None
    fence: BatchFenceV1 | None = None

    @model_validator(mode="after")
    def authoritative(self) -> Self:
        if self.outcome == "accepted" and self.remote_id is None:
            raise ValueError("acceptance requires a real service identity")
        if self.outcome == "not_accepted" and self.fence is None:
            raise ValueError("nonacceptance requires a fenced sender")
        if self.outcome != "not_accepted" and self.fence is not None:
            raise ValueError("fence cannot imply acceptance")
        return self


class BatchStopV1(EvidenceModel):
    run_id: ID
    kind: Literal["stop", "cancel_all"]


class BatchDiagnosticV1(EvidenceModel):
    code: Literal[
        "conflicting_terminal",
        "conflicting_member",
        "unsafe_evidence",
        "operation_unresolved",
        "limit_exhausted",
    ]


class BatchStatusEvidenceV1(EvidenceModel):
    submission_id: SHA256
    remote_id: ID
    state: Literal["running", "completed", "failed", "cancelled"]


class BatchCursorV1(EvidenceModel):
    run_id: ID
    submission_id: ID
    source_id: SHA256
    position: int = Field(ge=0)
    prefix_sha256: SHA256
    exhausted: bool = Field(default=False, exclude_if=lambda value: value is False)


class BatchMemberReceiptV1(EvidenceModel):
    identity: EvidenceIdentity
    member_key: SHA256
    source_id: SHA256
    source_position: int = Field(ge=0)
    observation_key: ID
    kind: Literal["response", "refusal", "provider_error", "confirmed_cancellation"]
    capture: Literal["full", "minimal"]
    original: ArtifactIdentity | None
    retained: ArtifactIdentity
    disposition: Literal["exact", "redacted", "withheld"]
    usage: ProviderUsage

    @model_validator(mode="after")
    def retention(self) -> Self:
        if self.original is not None and self.original.relative_path is not None:
            raise ValueError("original commitment is metadata only")
        if self.capture == "minimal" and self.retained.relative_path is not None:
            raise ValueError("minimal capture cannot retain raw bytes")
        if self.disposition != "exact" and self.original is not None:
            raise ValueError("unsafe original cannot acquire a digest")
        if self.usage.submission_id != self.identity.submission_id or (
            self.usage.attempt_id != self.identity.attempt_id
            or self.usage.observation_id != self.identity.observation_id
        ):
            raise ValueError("physical usage identity conflicts")
        return self


class BatchDetailV1(EvidenceModel):
    local_stop: bool
    cancel_all: bool
    jobs: int = Field(ge=0)
    accepted_jobs: int = Field(ge=0)
    unresolved_operations: int = Field(ge=0)
    missing_members: int = Field(ge=0)
    conflicts: int = Field(ge=0)
    recovery_required: bool


class BatchRunStatusV1(EvidenceModel):
    run_id: ID
    run_status: RunState
    outcome: Literal["ok", "partial", "error"]
    batch: BatchDetailV1
    artifacts: dict[str, str | None]
