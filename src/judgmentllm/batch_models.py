"""Version-one native batch contracts, independent of provider wire formats."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from judgmentllm.evidence._base import EvidenceIdentity, EvidenceModel
from judgmentllm.evidence.usage import ProviderUsage
from judgmentllm.models import ID
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
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
    index_sha256: SHA256 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    exhausted: bool = Field(default=False, exclude_if=lambda value: value is False)


class BatchSourceCommitmentV1(EvidenceModel):
    """Original service's immutable, job/payload-scoped source commitment."""

    source_id: SHA256
    scope_sha256: SHA256
    index_sha256: SHA256


class BatchSourceIndexV1(EvidenceModel):
    """Screened prefix hashes, including the empty prefix and final position."""

    scope_sha256: SHA256
    prefix_sha256: tuple[SHA256, ...] = Field(min_length=1)


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
    collection: OpenAIBatchCollectionV1 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class OpenAIBatchContextV1(EvidenceModel):
    """Generative Batch authority, independent of live model qualification."""

    protocol: Literal["openai-native-batch-v1"] = "openai-native-batch-v1"
    service_id: Literal["openai-v1"] = "openai-v1"
    endpoint: Literal["responses", "chat.completions"]
    model: ID
    sdk_version: Literal["3.42.1"] = "3.42.1"
    limits: BatchLimitsV1 = Field(default_factory=BatchLimitsV1)
    max_source_bytes: Literal[8_388_608] = 8_388_608


class OpenAIBatchShardV1(BatchShardV1):
    record_type: Literal["openai_batch_shard_v1"] = "openai_batch_shard_v1"
    context: OpenAIBatchContextV1  # pyright: ignore[reportIncompatibleVariableOverride]


class OpenAIBatchStatusV1(BatchStatusEvidenceV1):
    record_type: Literal["openai_batch_status_v1"] = "openai_batch_status_v1"
    input_file_id: ID
    endpoint: Literal["/v1/responses", "/v1/chat/completions"]
    provider_state: Literal[
        "validating",
        "failed",
        "in_progress",
        "finalizing",
        "completed",
        "expired",
        "cancelling",
        "cancelled",
    ]
    output_file_id: ID | None
    error_file_id: ID | None


class OpenAIBatchReadyV1(EvidenceModel):
    record_type: Literal["openai_batch_ready_v1"] = "openai_batch_ready_v1"
    run_id: ID
    manifest_sha256: SHA256
    shard_id: ID
    submission_id: ID
    source_id: SHA256
    role: Literal["output", "error"]
    status_sha256: SHA256


class OpenAIBatchSourceBindingV1(EvidenceModel):
    record_type: Literal["openai_batch_source_binding_v1"] = (
        "openai_batch_source_binding_v1"
    )
    ready: OpenAIBatchReadyV1
    create_receipt_sha256: SHA256
    job: OpenAIBatchStatusV1
    file_id: ID
    purpose: Literal["batch_output"]
    byte_size: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def exact_association(self) -> Self:
        if (
            self.ready.submission_id != self.job.submission_id
            or self.ready.status_sha256 != bytes_digest(canonical_bytes(self.job))
            or self.file_id != getattr(self.job, self.ready.role + "_file_id")
            or self.job.state == "running"
        ):
            raise ValueError("result file differs from its accepted job/source")
        return self


class OpenAIBatchSelectionV1(EvidenceModel):
    record_type: Literal["openai_batch_selection_v1"] = "openai_batch_selection_v1"
    run_id: ID
    sequence: int = Field(default=1, gt=0)
    after: int = Field(ge=0)
    cut: int = Field(ge=0)


class OpenAIBatchAdmissionV1(EvidenceModel):
    """A debit survives death; only a verified frontier or EOF resets stalls."""

    record_type: Literal["openai_batch_admission_v1"] = "openai_batch_admission_v1"
    run_id: ID
    source_id: SHA256
    sequence: int = Field(gt=0)
    frontier: int = Field(ge=0)
    stalls: int = Field(ge=0, le=3)
    code: Literal[
        "admitted",
        "collection_partial",
        "collection_no_progress",
        "collection_budget_unfit",
        "collection_prelude_failed",
        "source_limit",
        "source_conflict",
        "source_overrun",
        "source_underrun",
        "source_framing",
        "unsafe_metadata",
        "source_exhausted",
    ]


class OpenAIBatchCollectionV1(EvidenceModel):
    failed_sources: int = Field(ge=0)
    pending_sources: int = Field(ge=0)
    selection_pending: bool = False
    prelude_failed: bool = False
    codes: tuple[str, ...] = ()


class AnthropicBatchContextV1(EvidenceModel):
    protocol: Literal["anthropic-native-batch-v1"] = "anthropic-native-batch-v1"
    service_id: Literal["anthropic-v1"] = "anthropic-v1"
    endpoint: Literal["messages"] = "messages"
    model: ID
    sdk_version: Literal["2.3.0"] = "2.3.0"
    http_version: Literal["2.16.0"] = "2.16.0"
    api_version: Literal["2026-10-21"] = "2026-10-21"
    limits: BatchLimitsV1 = Field(default_factory=BatchLimitsV1)
    max_source_bytes: Literal[8_388_608] = 8_388_608

    @model_validator(mode="after")
    def exact_limits(self) -> Self:
        if self.limits != BatchLimitsV1():
            raise ValueError("Anthropic Batch limits differ from the route")
        return self


class AnthropicBatchShardV1(BatchShardV1):
    record_type: Literal["anthropic_batch_shard_v1"] = "anthropic_batch_shard_v1"
    context: AnthropicBatchContextV1  # pyright: ignore[reportIncompatibleVariableOverride]


AnthropicBatchResult = Literal["succeeded", "errored", "canceled", "expired"]


class AnthropicBatchMemberReceiptV1(BatchMemberReceiptV1):
    provider_result: AnthropicBatchResult


class AnthropicBatchCountsV1(EvidenceModel):
    processing: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    errored: int = Field(ge=0)
    canceled: int = Field(ge=0)
    expired: int = Field(ge=0)

    @property
    def total(self) -> int:
        return (
            self.processing
            + self.succeeded
            + self.errored
            + self.canceled
            + self.expired
        )


def _anthropic_timestamp(value: str) -> str:
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})",
        value,
    ):
        raise ValueError("invalid Batch timestamp")
    datetime.fromisoformat(value)
    return value


_AnthropicTimestamp = Annotated[str, AfterValidator(_anthropic_timestamp)]


class AnthropicBatchStatusV1(BatchStatusEvidenceV1):
    provider_state: Literal["in_progress", "canceling", "ended"]
    created_at: _AnthropicTimestamp
    expires_at: _AnthropicTimestamp
    ended_at: _AnthropicTimestamp | None
    archived_at: _AnthropicTimestamp | None
    cancel_initiated_at: _AnthropicTimestamp | None
    results_url: str | None
    counts: AnthropicBatchCountsV1


class AnthropicBatchCreateReceiptV1(BatchOperationReceiptV1):
    outcome: Literal["accepted"] = "accepted"  # pyright: ignore[reportIncompatibleVariableOverride]
    remote_id: ID = Field()  # pyright: ignore[reportIncompatibleVariableOverride]
    created_at: _AnthropicTimestamp
    expires_at: _AnthropicTimestamp


class AnthropicBatchReadyV1(EvidenceModel):
    record_type: Literal["anthropic_batch_ready_v1"] = "anthropic_batch_ready_v1"
    shard_id: ID
    source_id: SHA256
    manifest_sha256: SHA256
    receipt_sha256: SHA256
    status: AnthropicBatchStatusV1


class AnthropicBatchSelectionV1(EvidenceModel):
    record_type: Literal["anthropic_batch_selection_v1"] = (
        "anthropic_batch_selection_v1"
    )
    sequence: int = Field(gt=0)
    after: int = Field(ge=0)


class AnthropicBatchAdmissionV1(EvidenceModel):
    record_type: Literal["anthropic_batch_admission_v1"] = (
        "anthropic_batch_admission_v1"
    )
    sequence: int = Field(gt=0)
    frontier: int = Field(ge=0)
    stalls: int = Field(ge=0, le=3)
    code: Literal[
        "admitted",
        "collection_no_progress",
        "source_limit",
        "source_conflict",
        "source_framing",
        "source_unavailable",
        "unsafe_metadata",
    ]


class AnthropicBatchCancelObservedV1(EvidenceModel):
    record_type: Literal["anthropic_batch_cancel_observed_v1"] = (
        "anthropic_batch_cancel_observed_v1"
    )
    intent_id: SHA256
    status: AnthropicBatchStatusV1


AnthropicBatchHTTPStage = Literal[
    "create",
    "cancel",
    "poll",
    "cancel_reconciliation",
    "results_prelude",
    "results_content",
    "results_confirmation",
]
AnthropicBatchError = Literal[
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    "not_found_error",
    "rate_limit_error",
    "timeout_error",
    "overloaded_error",
    "api_error",
    "billing_error",
]
AnthropicBatchInterruption = Literal[
    "transport_error",
    "deadline",
    "budget",
    "body_limit",
    "unsafe_response",
    "row_quota",
    "cancelled",
    "cleanup_failed",
]


class AnthropicBatchHTTPDiagnosticV1(EvidenceModel):
    record_type: Literal["anthropic_batch_http_diagnostic_v1"] = (
        "anthropic_batch_http_diagnostic_v1"
    )
    call_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    stage: AnthropicBatchHTTPStage
    phase: Literal["headers", "completion"]
    intent_id: SHA256 | None = None
    submission_id: SHA256 | None = None
    job_id: str | None = Field(default=None, pattern=r"^msgbatch_[A-Za-z0-9_-]{1,160}$")
    source_id: SHA256 | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    request_id: str | None = Field(default=None, pattern=r"^req_[A-Za-z0-9_-]{1,160}$")
    headers_sha256: SHA256 | None = None
    error_type: AnthropicBatchError | None = None
    interruption: AnthropicBatchInterruption | None = None
    disposition: Literal["exact", "redacted", "withheld"] | None = None
    body: ArtifactIdentity | None = None

    @model_validator(mode="after")
    def evidence_scope(self) -> Self:
        mutation = self.stage in ("create", "cancel", "cancel_reconciliation")
        results = self.stage.startswith("results_")
        if (
            mutation != (self.intent_id is not None)
            or (self.stage != "create")
            != (self.submission_id is not None and self.job_id is not None)
            or (self.submission_id is None) != (self.job_id is None)
            or results != (self.source_id is not None)
            or self.http_status is None
            and self.request_id is not None
            or (self.disposition in ("exact", "redacted")) != (self.body is not None)
            or self.body is not None
            and self.body.byte_length > 1_048_576
        ):
            raise ValueError("Anthropic Batch HTTP diagnostic binding differs")
        if self.phase == "headers":
            if self.http_status is None or any(
                value is not None
                for value in (
                    self.headers_sha256,
                    self.error_type,
                    self.interruption,
                    self.disposition,
                )
            ):
                raise ValueError(
                    "Anthropic Batch HTTP headers contain completion evidence"
                )
        elif (self.http_status is None) != (self.headers_sha256 is None):
            raise ValueError("Anthropic Batch HTTP completion lacks its headers proof")
        return self


BatchContext = BatchContextV1 | OpenAIBatchContextV1 | AnthropicBatchContextV1

BatchRunStatusV1.model_rebuild()


def shard_model(
    context: BatchContext,
) -> type[BatchShardV1]:
    if isinstance(context, AnthropicBatchContextV1):
        return AnthropicBatchShardV1
    return (
        OpenAIBatchShardV1
        if isinstance(context, OpenAIBatchContextV1)
        else BatchShardV1
    )


def member_receipt_model(context: BatchContext) -> type[BatchMemberReceiptV1]:
    if isinstance(context, AnthropicBatchContextV1):
        return AnthropicBatchMemberReceiptV1
    return BatchMemberReceiptV1


def operation_receipt_model(
    context: BatchContext, kind: str
) -> type[BatchOperationReceiptV1]:
    if isinstance(context, AnthropicBatchContextV1) and kind == "create":
        return AnthropicBatchCreateReceiptV1
    return BatchOperationReceiptV1


class OpenAIBatchAbsentV1(EvidenceModel):
    record_type: Literal["openai_batch_absent_v1"] = "openai_batch_absent_v1"
    ready: OpenAIBatchReadyV1


class OpenAIBatchProjectionV1(EvidenceModel):
    record_type: Literal["openai_batch_projection_v1"] = "openai_batch_projection_v1"
    run_id: ID
    sequence: int = Field(default=1, gt=0)
    after: int = Field(ge=0)
    cut: int = Field(ge=0)


class OpenAIBatchPreludeFailureV1(EvidenceModel):
    record_type: Literal["openai_batch_prelude_failure_v1"] = (
        "openai_batch_prelude_failure_v1"
    )
    run_id: ID
    code: Literal["collection_prelude_failed"] = "collection_prelude_failed"
