"""Shared public lifecycle services backed by the existing execution engine.

No provider request bodies, scheduling, result interpretation or export writers
are implemented here. Public controls remain independent of the CLI transport.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import Field

from judgmentllm._lifecycle_store import (
    decode_cursor,
    discover_store,
    encode_cursor,
    inspect_run,
    require_store,
    resolve_run,
)
from judgmentllm.config import (
    ConfigurationError,
    RunSpec,
    RunSpecification,
    RunSpecV2,
    load_run_spec,
    parse_json,
    validate_run_spec,
)
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.exports import export_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.lifecycle_models import (
    ErrorCategory,
    ExecutionPlan,
    ExportFile,
    InputValidationReport,
    LifecycleError,
    MockExecutionOptions,
    Progress,
    ProgressEvent,
    ResultManifest,
    RetrySelection,
    RunPage,
    RunStatus,
    RunSummary,
)
from judgmentllm.models import VersionedModel
from judgmentllm.output_commitments import resolve_schema_selection
from judgmentllm.persistence.models import (
    IntegrityError,
    NotFoundError,
    OwnershipError,
    PersistenceError,
    RetryAuthorizationError,
)
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.planning import (
    CapabilityProfile,
    PreflightError,
    ValidationIssue,
    validate_run,
)
from judgmentllm.preparation import PreparationError, prepare_run
from judgmentllm.providers import MockAdapter, MockScenario, ProviderError


class _MockFixture(VersionedModel):
    """Synthetic adapter scenarios, never arbitrary provider request bodies."""

    scenario: MockScenario = Field(default_factory=MockScenario)
    scenarios: dict[str, MockScenario] = Field(default_factory=dict)


def _checked_spec(spec_or_path: RunSpecification | str | Path) -> RunSpecification:
    return (
        validate_run_spec(spec_or_path)
        if isinstance(spec_or_path, (RunSpec, RunSpecV2))
        else load_run_spec(spec_or_path)
    )


@contextmanager
def _errors(run_id: str | None = None, store: Path | None = None) -> Generator[None]:
    """Translate only safe categories; exception bodies never enter diagnostics."""
    try:
        yield
    except LifecycleError as error:
        raise LifecycleError(
            error.category,
            error.message,
            retryable=error.retryable,
            run_id=error.run_id or run_id,
            store=error.store or (str(store) if store else None),
        ) from None
    except Exception as error:
        category: ErrorCategory
        message: str
        if isinstance(error, NotFoundError):
            category, message = "not_found", "durable run or resource was not found"
        elif isinstance(error, OwnershipError):
            category, message = "execution", "run executor ownership is unavailable"
        elif isinstance(error, RetryAuthorizationError):
            category, message = (
                "validation",
                "retry selection, authorization, or submission budget is incompatible",
            )
        elif isinstance(error, (IntegrityError, PersistenceError, sqlite3.Error)):
            category, message = (
                "execution",
                "durable run integrity or storage operation failed",
            )
        elif isinstance(error, (ProviderError, OSError, ExceptionGroup)):
            category, message = (
                "execution",
                "provider evidence or execution operation is unavailable",
            )
        elif isinstance(
            error,
            (PreparationError, ConfigurationError, PreflightError, ValueError),
        ):
            category, message = (
                "validation",
                "configuration, source, or recovery inputs are invalid or incompatible",
            )
        else:
            category, message = "internal", "lifecycle operation could not be completed"
        raise LifecycleError(
            category, message, run_id=run_id, store=str(store) if store else None
        ) from None


def _mock_adapter(
    provider: str,
    endpoint: str,
    store: Path,
    options: MockExecutionOptions | None,
    *,
    resume: bool,
) -> MockAdapter:
    # This exact dispatch is deliberate: unknown/real providers must never use
    # synthetic execution, regardless of supplied mock settings.
    if (provider, endpoint) != ("mock", "synthetic"):
        raise LifecycleError(
            "unsupported_capability",
            "this release executes only provider mock with endpoint synthetic",
        )
    configured = (
        MockExecutionOptions.model_validate(dict(options))
        if options is not None
        else MockExecutionOptions()
    )
    ledger = (configured.ledger_path or store / "mock-service.sqlite3").resolve()
    if resume and not ledger.is_file():
        raise LifecycleError(
            "execution", "original mock acceptance ledger is unavailable"
        )
    fixture = _MockFixture()
    if configured.fixture_path is not None:
        fixture = _MockFixture.model_validate(
            parse_json(configured.fixture_path.read_text(encoding="utf-8"))
        )
    adapter = MockAdapter(
        ledger,
        scenario=fixture.scenario,
        scenarios=fixture.scenarios,
        retrieval_available=configured.retrieval_available,
        reconciliation_available=configured.reconciliation_available,
    )
    return adapter


def _require_presentation(spec: RunSpecification) -> None:
    """Reject unavailable v2 mechanisms before creating a runtime store."""
    if isinstance(spec, RunSpecV2):
        try:
            resolve_schema_selection(spec)
        except PreflightError as error:
            # Selection diagnostics contain only fixed configuration/capability text.
            raise LifecycleError("unsupported_capability", str(error)) from None


def _profile(
    spec: RunSpecification, capabilities: CapabilityProfile | None
) -> CapabilityProfile | None:
    profile = (
        CapabilityProfile.model_validate(dict(capabilities))
        if capabilities is not None
        else None
    )
    if (spec.provider, spec.endpoint) == ("mock", "synthetic"):
        profile = profile or MockAdapter.capabilities_for(
            spec.provider, spec.endpoint, spec.model
        )
        if (
            isinstance(spec, RunSpecV2)
            and not validate_run(spec, capabilities=profile).valid
        ):
            return profile
        try:
            MockAdapter.validate_configuration(spec, capabilities=profile)
        except ProviderError:
            raise LifecycleError(
                "unsupported_capability", "synthetic provider settings are unsupported"
            ) from None
    return profile


def validate_inputs(
    spec_or_path: RunSpecification | str | Path,
    *,
    capabilities: CapabilityProfile | None = None,
    exhaustive_rendering: bool = False,
) -> InputValidationReport:
    """Validate all inputs offline; return at most 100 safe issues, with totals."""
    issues: tuple[ValidationIssue, ...] = ()
    try:
        with (
            _errors(),
            tempfile.TemporaryDirectory(prefix="judgmentllm-validation-") as temporary,
        ):
            spec = _checked_spec(spec_or_path)
            profile = _profile(spec, capabilities)
            structural = validate_run(spec, capabilities=profile)
            issues = structural.issues
            if not structural.valid:
                return InputValidationReport(
                    valid=False,
                    issues=issues[:100],
                    total_issues=len(issues),
                    truncated=len(issues) > 100,
                )
            with prepare_run(
                spec_or_path,
                store=Path(temporary) / "store",
                run_id="validation",
                capabilities=profile,
                exhaustive_rendering=exhaustive_rendering,
            ) as prepared:
                sources = prepared.manifest.sources
                return InputValidationReport(
                    valid=True,
                    response_count=sum(
                        s.record_count for s in sources if s.role == "response"
                    ),
                    pair_count=sum(s.record_count for s in sources if s.role == "pair"),
                    source_count=len(sources),
                    issues=issues[:100],
                    total_issues=len(issues),
                    truncated=len(issues) > 100,
                    output_selection=resolve_schema_selection(spec, profile)
                    if isinstance(spec, RunSpecV2)
                    else None,
                )
    except LifecycleError as error:
        if error.category not in {"validation", "unsupported_capability"}:
            raise
        issues += (
            ValidationIssue(
                code="unsupported_capability"
                if error.category == "unsupported_capability"
                else "invalid_inputs",
                field="input",
                message="source/configuration validation failed",
                severity="error",
            ),
        )
        return InputValidationReport(
            valid=False,
            issues=issues[:100],
            total_issues=len(issues),
            truncated=len(issues) > 100,
        )


def estimate_run(
    spec_or_path: RunSpecification | str | Path,
    *,
    capabilities: CapabilityProfile | None = None,
) -> ExecutionPlan:
    """Prepare once privately and derive exact counts without a task product."""
    with (
        _errors(),
        tempfile.TemporaryDirectory(prefix="judgmentllm-plan-") as temporary,
    ):
        spec = _checked_spec(spec_or_path)
        _require_presentation(spec)
        profile = _profile(spec, capabilities)
        with prepare_run(
            spec_or_path,
            store=Path(temporary) / "store",
            run_id="plan",
            capabilities=profile,
        ) as prepared:
            manifest = prepared.manifest
            responses = sum(
                s.record_count for s in manifest.sources if s.role == "response"
            )
            pairs = sum(s.record_count for s in manifest.sources if s.role == "pair")
            mock = (spec.provider, spec.endpoint) == ("mock", "synthetic")
            return ExecutionPlan(
                task_kind=spec.task_kind,
                response_count=responses,
                pair_count=pairs,
                source_count=len(manifest.sources),
                logical_tasks=responses * len(manifest.traits)
                if spec.task_kind == "rubric"
                else pairs,
                requests=responses * len(manifest.trait_groups)
                if spec.task_kind == "rubric"
                else pairs,
                trait_groups=manifest.trait_groups,
                capabilities=profile,
                cache_policy=spec.execution.cache_policy or "auto",
                cache_report="uncached" if mock else "unknown",
                pricing="not_applicable" if mock else "unknown",
                output_selection=resolve_schema_selection(spec, profile)
                if isinstance(spec, RunSpecV2)
                else None,
            )


def _progress(
    callback: Progress | None,
    stage: Literal["started", "prepared", "finished"],
    run_id: str,
    store: Path,
) -> None:
    if callback is not None:
        callback(ProgressEvent(stage=stage, run_id=run_id, store=str(store)))


def _summary(
    run_id: str, store: Path, *, retry_operation_id: str | None = None
) -> RunSummary:
    status = get_run(run_id, store=store)
    outcome: Literal["ok", "partial", "error"] = (
        "ok"
        if status.run_status == "completed" and not status.unresolved_submissions
        else "partial"
        if status.results or status.run_status in {"partial", "cancelled"}
        else "error"
    )
    return RunSummary(
        **status.model_dump(), outcome=outcome, retry_operation_id=retry_operation_id
    )


async def arun_live(
    spec_or_path: RunSpecification | str | Path,
    *,
    store: str | Path | None = None,
    progress: Progress | None = None,
    mock: MockExecutionOptions | None = None,
) -> RunSummary:
    """Execute in this process until drained; cancellation preserves durable work."""
    with _errors():
        spec = _checked_spec(spec_or_path)
        _require_presentation(spec)
        root = discover_store(store, spec)
        adapter = _mock_adapter(spec.provider, spec.endpoint, root, mock, resume=False)
        try:
            adapter.validate(spec)
        except ProviderError:
            raise LifecycleError(
                "unsupported_capability", "synthetic provider settings are unsupported"
            ) from None
    run_id = uuid.uuid4().hex
    with _errors(run_id, root):
        _progress(progress, "started", run_id, root)
        # Source preparation stays on its own thread. Shield cancellation until
        # its durable writes and ownership release finish; never orphan a writer.
        preparation = asyncio.create_task(
            asyncio.to_thread(
                _prepare,
                spec_or_path,
                root,
                run_id,
                adapter.capabilities(spec),
            )
        )
        try:
            await asyncio.shield(preparation)
        except asyncio.CancelledError:
            while not preparation.done():
                try:
                    await asyncio.shield(preparation)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not preparation.cancelled():
                preparation.exception()
            raise
        _progress(progress, "prepared", run_id, root)
        await execute_run(run_id, store=root, adapter=adapter, spec=spec)
        result = _summary(run_id, root)
        _progress(progress, "finished", run_id, root)
        return result


def _prepare(
    spec_or_path: RunSpecification | str | Path,
    root: Path,
    run_id: str,
    profile: CapabilityProfile,
) -> None:
    with prepare_run(spec_or_path, store=root, run_id=run_id, capabilities=profile):
        pass


def _no_event_loop(operation: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise LifecycleError(
        "usage",
        f"{operation} cannot run inside an active event loop; use a{operation} instead",
    )


def run_live(
    spec_or_path: RunSpecification | str | Path,
    *,
    store: str | Path | None = None,
    progress: Progress | None = None,
    mock: MockExecutionOptions | None = None,
) -> RunSummary:
    """Synchronous counterpart; use arun_live from an active event loop."""
    _no_event_loop("run_live")
    return asyncio.run(
        arun_live(spec_or_path, store=store, progress=progress, mock=mock)
    )


async def aresume_run(
    run_id: str | Path,
    *,
    store: str | Path | None = None,
    spec: RunSpecification | None = None,
    use_frozen_snapshot: bool = False,
    retry: RetrySelection | None = None,
    progress: Progress | None = None,
    mock: MockExecutionOptions | None = None,
) -> RunSummary:
    """Recover original work and optionally authorize one exact retry subset."""
    with _errors():
        identifier, root = resolve_run(run_id, store, spec)
    with _errors(identifier, root):
        # Inspect immutable identity without requiring original source files. The
        # execution/retry services perform complete source and context checks.
        with ReadOnlyRepository(root) as repository:
            run = repository.get_run(identifier)
        adapter = _mock_adapter(run.provider, run.endpoint, root, mock, resume=True)
        _progress(progress, "started", identifier, root)
        if retry is not None:
            selection = RetrySelection.model_validate(dict(retry))
            authorization = asyncio.create_task(
                asyncio.to_thread(
                    authorize_subset_retry,
                    identifier,
                    store=root,
                    adapter=adapter,
                    task_ids=selection.task_ids,
                    operation_id=selection.operation_id,
                    spec=spec,
                    allow_completed=selection.allow_completed,
                    use_frozen_snapshot=use_frozen_snapshot,
                )
            )
            try:
                await asyncio.shield(authorization)
            except asyncio.CancelledError:
                while not authorization.done():
                    try:
                        await asyncio.shield(authorization)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not authorization.cancelled():
                    authorization.exception()
                raise
        await recover_run(
            identifier,
            store=root,
            adapter=adapter,
            spec=spec,
            use_frozen_snapshot=use_frozen_snapshot,
        )
        result = _summary(
            identifier,
            root,
            retry_operation_id=retry.operation_id if retry is not None else None,
        )
        _progress(progress, "finished", identifier, root)
        return result


def resume_run(
    run_id: str | Path,
    *,
    store: str | Path | None = None,
    spec: RunSpecification | None = None,
    use_frozen_snapshot: bool = False,
    retry: RetrySelection | None = None,
    progress: Progress | None = None,
    mock: MockExecutionOptions | None = None,
) -> RunSummary:
    """Synchronous counterpart; use aresume_run from an active event loop."""
    _no_event_loop("resume_run")
    return asyncio.run(
        aresume_run(
            run_id,
            store=store,
            spec=spec,
            use_frozen_snapshot=use_frozen_snapshot,
            retry=retry,
            progress=progress,
            mock=mock,
        )
    )


def get_run(run_id: str | Path, *, store: str | Path | None = None) -> RunStatus:
    """Inspect persisted state without submitting, retrieving, or reading sources."""
    with _errors():
        identifier, root = resolve_run(run_id, store)
    with _errors(identifier, root), ReadOnlyRepository(root) as repository:
        return inspect_run(repository, identifier)


def list_runs(
    *, store: str | Path | None = None, limit: int = 100, cursor: str | None = None
) -> RunPage:
    """List at most 1,000 runs, using stable newest-first keyset pagination."""
    with _errors():
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise LifecycleError("usage", "limit must be an integer between 1 and 1000")
        root = discover_store(store)
        boundary = decode_cursor(cursor, root) if cursor is not None else None
        if not (root / "registry.sqlite3").exists():
            return RunPage(runs=(), next_cursor=None)
        require_store(root)
        with ReadOnlyRepository(root) as repository:
            rows = repository.connection.execute(
                "SELECT run_id,created_at FROM runs "
                + ("WHERE (created_at,run_id)<(?,?) " if boundary else "")
                + "ORDER BY created_at DESC,run_id DESC LIMIT ?",
                (*boundary, limit + 1) if boundary else (limit + 1,),
            ).fetchall()
            selected = rows[:limit]
            runs = tuple(
                inspect_run(repository, str(row["run_id"])) for row in selected
            )
            next_cursor = (
                encode_cursor(
                    root, str(selected[-1]["created_at"]), str(selected[-1]["run_id"])
                )
                if len(rows) > limit
                else None
            )
            return RunPage(runs=runs, next_cursor=next_cursor)


def export_run_results(
    run_id: str | Path,
    *,
    store: str | Path | None = None,
    format: Literal["jsonl", "csv"] = "jsonl",
    view: Literal["canonical", "r"] = "canonical",
    na_string: str = "NA",
    high_water_mark: int | None = None,
) -> ResultManifest:
    """Publish all recorded attempts via existing immutable export serializers."""
    with _errors():
        identifier, root = resolve_run(run_id, store)
    with _errors(identifier, root):
        report = export_run(
            identifier,
            store=root,
            format=format,
            view=view,
            na_string=na_string,
            high_water_mark=high_water_mark,
        )
        status = get_run(identifier, store=root)
        slot = (
            "results_jsonl"
            if report.format == "jsonl"
            else ("results_r_csv" if report.view == "r" else "results_csv")
        )
        artifacts = status.artifacts.model_copy(
            update={"export": report.artifact_path, slot: report.artifact_path}
        )
        return ResultManifest(
            run_id=identifier,
            run_status=report.run_state,
            export_id=report.export_id,
            format=report.format,
            view=report.view,
            row_count=report.row_count,
            result_high_water_mark=report.result_high_water_mark,
            incomplete_source=not report.run_complete,
            files=(
                ExportFile(
                    path=report.artifact_path,
                    sha256=report.artifact.sha256,
                    byte_length=report.artifact.byte_length,
                ),
            ),
            artifacts=artifacts,
        )
