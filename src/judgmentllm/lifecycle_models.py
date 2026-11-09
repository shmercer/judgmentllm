"""Typed, content-free public execution controls and summaries, version 1."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from judgmentllm.models import ID, StrictModel
from judgmentllm.output_commitments import SchemaSelectionV2
from judgmentllm.persistence.models import ExecutionModel, RunState
from judgmentllm.planning import CapabilityProfile, ValidationIssue

Count = Annotated[int, Field(ge=0)]

ErrorCategory = Literal[
    "usage",
    "validation",
    "unsupported_capability",
    "not_found",
    "execution",
    "partial_completion",
    "interrupted",
    "internal",
]


class LifecycleError(RuntimeError):
    """Safe provider-neutral error; never carries assessment or exception text."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        retryable: bool = False,
        run_id: str | None = None,
        store: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category: ErrorCategory = category
        self.message = message
        self.retryable = retryable
        self.run_id = run_id
        self.store = store


class PublicModel(StrictModel):
    """Immutable nested public control record."""

    model_config = ConfigDict(frozen=True)


class MockExecutionOptions(PublicModel):
    """Explicit synthetic service inputs; fixtures remain caller-owned files."""

    ledger_path: Path | None = None
    fixture_path: Path | None = None
    retrieval_available: bool = True
    reconciliation_available: bool = True


class RetrySelection(PublicModel):
    """One original request's exact selected tasks and durable operation ID."""

    task_ids: tuple[ID, ...]
    operation_id: ID
    allow_completed: bool = False

    @model_validator(mode="after")
    def unique_members(self) -> RetrySelection:
        if not self.task_ids or len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("retry selection requires unique task IDs")
        return self


class ProgressEvent(ExecutionModel):
    """Coarse lifecycle event; no assessment material or arbitrary messages."""

    stage: Literal["started", "prepared", "finished"]
    run_id: str
    store: str


Progress = Callable[[ProgressEvent], None]


class InputValidationReport(ExecutionModel):
    """Complete input validation with bounded, safe issue reporting."""

    valid: bool
    response_count: Count | None = Field(default=None, ge=0)
    pair_count: Count | None = Field(default=None, ge=0)
    source_count: Count | None = Field(default=None, ge=0)
    issues: tuple[ValidationIssue, ...] = ()
    total_issues: Count = 0
    truncated: bool = False
    output_selection: SchemaSelectionV2 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ExecutionPlan(ExecutionModel):
    """Exact prepared structural counts, with unknown estimates explicit."""

    task_kind: Literal["rubric", "pairwise"]
    response_count: Count
    pair_count: Count
    source_count: Count
    logical_tasks: Count
    requests: Count
    trait_groups: tuple[tuple[str, ...], ...]
    capabilities: CapabilityProfile | None
    cache_policy: str
    cache_report: Literal["uncached", "unknown"]
    estimated_input_tokens: Count | None = None
    estimated_output_tokens: Count | None = None
    estimated_cost: float | None = Field(default=None, ge=0)
    pricing: Literal["not_applicable", "unknown"]
    output_selection: SchemaSelectionV2 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    transport_protocol: (
        Literal["openai-production-v1", "anthropic-production-v1"] | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    selected_transports: (
        dict[
            Literal[
                "text",
                "openai.responses.text.format",
                "openai.chat.response_format",
                "anthropic.messages.output_config.format",
            ],
            Count,
        ]
        | None
    ) = Field(default=None, exclude_if=lambda value: value is None)


class ArtifactPaths(PublicModel):
    """Absolute durable paths; unavailable artifacts are explicitly null."""

    store: str | None = None
    run_directory: str | None = None
    registry: str | None = None
    manifest: str | None = None
    results_jsonl: str | None = None
    results_csv: str | None = None
    results_r_csv: str | None = None
    export: str | None = None


class SubmissionCounts(PublicModel):
    not_started: Count = 0
    not_submitted: Count = 0
    unknown: Count = 0
    accepted: Count = 0


class UsageSummary(PublicModel):
    """Unreported provider usage stays unknown, including for synthetic billing."""

    input_tokens: Count | None = None
    output_tokens: Count | None = None
    cost: float | None = Field(default=None, ge=0)


class RunStatus(ExecutionModel):
    """Bounded persisted inspection; historical outcomes differ from tasks."""

    run_id: str
    run_status: RunState
    task_kind: Literal["rubric", "pairwise"]
    provider: str
    endpoint: str
    model: str
    capture: Literal["full", "minimal"]
    created_at: str
    updated_at: str
    planned_tasks: Count | None
    planned_requests: Count | None
    materialized_tasks: Count
    completed_tasks: Count
    failed_tasks: Count
    cancelled_tasks: Count
    blocked_tasks: Count
    pending_tasks: Count | None
    requests: Count
    attempts: Count
    results: Count
    successful_results: Count
    unsuccessful_results: Count
    unresolved_submissions: Count
    retry_operations: Count
    submission_certainty: SubmissionCounts
    executor_active: bool | None
    recovery_required: bool
    usage: UsageSummary = Field(default_factory=UsageSummary)
    artifacts: ArtifactPaths
    output_selection: SchemaSelectionV2 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class RunSummary(RunStatus):
    """A blocking processing operation's outcome and final persisted status."""

    outcome: Literal["ok", "partial", "error"]
    retry_operation_id: str | None = None


class RunPage(ExecutionModel):
    """Bounded newest-first run summaries and an opaque continuation cursor."""

    runs: tuple[RunStatus, ...]
    next_cursor: str | None


class ExportFile(PublicModel):
    path: str
    sha256: str
    byte_length: Count


class ResultManifest(ExecutionModel):
    """Immutable all-attempt export identity and current source completeness."""

    run_id: str
    run_status: RunState
    export_id: str
    format: Literal["jsonl", "csv"]
    view: Literal["canonical", "r"]
    row_count: Count
    result_high_water_mark: Count
    incomplete_source: bool
    files: tuple[ExportFile, ...]
    artifacts: ArtifactPaths
