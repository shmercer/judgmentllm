"""Shared public services preserve durable identities and execution boundaries."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError
from test_preparation_rendering import specification

from judgmentllm import (
    MockExecutionOptions,
    RetrySelection,
    aresume_run,
    arun_live,
    estimate_run,
    export_run_results,
    get_run,
    lifecycle,
    list_runs,
    resume_run,
    run_live,
    validate_inputs,
)
from judgmentllm._lifecycle_store import discover_store
from judgmentllm.config import RunSpec
from judgmentllm.lifecycle_models import LifecycleError, ProgressEvent
from judgmentllm.persistence.models import RunRecord
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
from judgmentllm.providers import MockAdapter


def config(
    tmp_path: Path,
    *,
    capture: Literal["full", "minimal"] = "minimal",
    grouped: bool = False,
) -> tuple[RunSpec, Path]:
    spec = specification(tmp_path / "input", responses=2, traits=3)
    spec.provider, spec.endpoint, spec.capture = "mock", "synthetic", capture
    spec.output_path = tmp_path / "store"
    spec.execution.concurrency = 1
    if grouped:
        spec.execution.grouping = "grouped"
    spec = RunSpec.model_validate(spec.model_dump())
    path = tmp_path / "run.json"
    path.write_text(spec.model_dump_json(), encoding="utf-8")
    return spec, path


def test_complete_validation_planning_and_unknown_counts(tmp_path: Path) -> None:
    _, path = config(tmp_path, grouped=True)
    valid = validate_inputs(path)
    assert valid.valid and valid.response_count == 2 and valid.pair_count == 0
    plan = estimate_run(path)
    assert plan.logical_tasks == 6 and plan.requests == 2
    assert plan.estimated_cost is None and plan.estimated_input_tokens is None
    assert plan.pricing == "not_applicable" and plan.cache_report == "uncached"
    assert not (tmp_path / "store").exists()
    invalid = validate_inputs(tmp_path / "absent.json")
    assert not invalid.valid and invalid.response_count is None
    assert invalid.total_issues == 1 and not invalid.truncated
    with pytest.raises(LifecycleError, match="invalid or incompatible"):
        estimate_run(tmp_path / "absent.json")


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_shared_run_resume_export_and_durable_counts(
    tmp_path: Path, capture: Literal["full", "minimal"]
) -> None:
    _, path = config(tmp_path, capture=capture)
    events: list[ProgressEvent] = []
    first = run_live(path, progress=events.append)
    assert [e.stage for e in events] == ["started", "prepared", "finished"]
    assert all(Path(e.store).is_absolute() for e in events)
    assert first.outcome == "ok" and first.run_status == "completed"
    assert first.completed_tasks == first.planned_tasks == first.materialized_tasks == 6
    assert first.attempts == first.results == 6 and first.pending_tasks == 0
    assert not first.executor_active and not first.recovery_required
    assert first.usage.input_tokens is None
    store = Path(events[0].store)
    ledger = MockAdapter(store / "mock-service.sqlite3").ledger
    accepted = ledger.acceptance_count()
    repeated = resume_run(first.run_id, store=store)
    assert repeated.attempts == 6 and ledger.acceptance_count() == accepted == 6
    directory = first.artifacts.run_directory
    assert directory is not None
    assert get_run(Path(directory)).run_id == first.run_id
    formats: tuple[tuple[Literal["jsonl", "csv"], Literal["canonical", "r"]], ...] = (
        ("jsonl", "canonical"),
        ("csv", "canonical"),
        ("csv", "r"),
    )
    for format, view in formats:
        exported = export_run_results(
            first.run_id, store=store, format=format, view=view
        )
        old = Path(exported.files[0].path).read_bytes()
        again = export_run_results(first.run_id, store=store, format=format, view=view)
        assert (
            exported.export_id == again.export_id
            and Path(again.files[0].path).read_bytes() == old
        )
        assert exported.row_count == 6 and not exported.incomplete_source
    status = get_run(first.run_id, store=store)
    assert all(
        value is None or Path(value).is_absolute()
        for value in status.artifacts.model_dump().values()
    )


def test_store_precedence_and_empty_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _ = config(tmp_path)
    explicit, environment = tmp_path / "explicit", tmp_path / "env"
    assert discover_store(None, spec) == spec.output_path.resolve()
    monkeypatch.setenv("JUDGMENTLLM_RUN_HOME", str(environment))
    assert discover_store(None, spec) == environment.resolve()
    assert discover_store(explicit, spec) == explicit.resolve()
    assert list_runs(store=explicit, limit=1000).runs == ()
    assert not explicit.exists()
    with pytest.raises(LifecycleError) as missing:
        get_run("absent", store=explicit)
    assert missing.value.category == "not_found" and not explicit.exists()
    with pytest.raises(LifecycleError) as invalid:
        list_runs(store=explicit, limit=1001)
    assert invalid.value.category == "usage"


def test_keyset_cursor_is_store_bound_and_created_runs_are_inspectable(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    with Repository(store) as repository:
        for identifier in ("001", "002", "003"):
            repository.create_run(
                RunRecord(
                    run_id=identifier,
                    task_kind="rubric",
                    provider="mock",
                    endpoint="synthetic",
                    model="unknown",
                    created_at="2026-11-01T00:00:00+00:00",
                )
            )
        directory = repository.run_directory("001")
    first = list_runs(store=store, limit=2)
    assert [run.run_id for run in first.runs] == ["003", "002"]
    assert first.next_cursor is not None
    second = list_runs(store=store, limit=2, cursor=first.next_cursor)
    assert [run.run_id for run in second.runs] == ["001"] and second.next_cursor is None
    assert second.runs[0].planned_tasks is None
    assert get_run(directory).run_id == "001"
    other = tmp_path / "other"
    with Repository(other):
        pass
    for token in ("invalid", first.next_cursor):
        with pytest.raises(LifecycleError) as error:
            list_runs(store=other, cursor=token)
        assert error.value.category == "usage"


def test_non_mock_provider_never_creates_or_submits_mock_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _ = config(tmp_path)
    spec.provider, spec.endpoint = "unimplemented-provider", "unknown-endpoint"
    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> None:
        calls.append("called")
        raise AssertionError("must not construct a mock service")

    monkeypatch.setattr(lifecycle, "MockAdapter", forbidden)
    with pytest.raises(LifecycleError) as error:
        run_live(spec)
    assert error.value.category == "unsupported_capability" and not calls
    assert not spec.output_path.exists()
    with Repository(spec.output_path) as repository:
        repository.create_run(
            RunRecord(
                run_id="001",
                task_kind="rubric",
                provider=spec.provider,
                endpoint=spec.endpoint,
                model=spec.model,
            )
        )
    with pytest.raises(LifecycleError) as error:
        resume_run("001", store=spec.output_path)
    assert error.value.category == "unsupported_capability" and not calls
    assert not (spec.output_path / "mock-service.sqlite3").exists()


@pytest.mark.asyncio
async def test_sync_wrappers_reject_active_event_loop_before_io(tmp_path: Path) -> None:
    for operation, args in (
        (run_live, (tmp_path / "absent",)),
        (resume_run, ("absent",)),
    ):
        with pytest.raises(LifecycleError, match="active event loop") as error:
            operation(*args)
        assert error.value.category == "usage"
    assert list(tmp_path.iterdir()) == []


def test_public_models_are_strict_and_frozen() -> None:
    options = MockExecutionOptions()
    with pytest.raises(ValidationError):
        options.retrieval_available = False
    with pytest.raises(ValidationError):
        RetrySelection(task_ids=("one", "one"), operation_id="retry")
    with pytest.raises(ValidationError):
        RetrySelection(task_ids=(), operation_id="retry")
    with pytest.raises(ValidationError):
        MockExecutionOptions(retrieval_available="false")  # pyright: ignore[reportArgumentType]


def test_missing_ledger_cannot_be_recreated_on_resume(tmp_path: Path) -> None:
    _, path = config(tmp_path)
    result = run_live(path)
    ledger = tmp_path / "store" / "mock-service.sqlite3"
    ledger.unlink()
    with pytest.raises(LifecycleError) as error:
        resume_run(result.run_id, store=tmp_path / "store")
    assert error.value.category == "execution" and not ledger.exists()


def test_subset_retry_requires_permission_and_has_durable_identity(
    tmp_path: Path,
) -> None:
    _, path = config(tmp_path, grouped=True)
    fixture = tmp_path / "partial.json"
    fixture.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenario": {"members": {"trait-1": {"outcome": "invalid_category"}}},
            }
        ),
        encoding="utf-8",
    )
    # Fixture trait IDs come from the established helper, without rewriting run inputs.
    rubric = lifecycle.load_run_spec(path).rubric
    assert rubric is not None
    trait = rubric.traits[-1].trait_id
    fixture.write_text(
        json.dumps({"scenario": {"members": {trait: {"outcome": "invalid_category"}}}}),
        encoding="utf-8",
    )
    first = run_live(path, mock=MockExecutionOptions(fixture_path=fixture))
    assert first.outcome == "partial" and first.failed_tasks == 2
    store = tmp_path / "store"
    with Repository(store) as repository:
        results = list(repository.iter_results(first.run_id))
    failed = next(record for record in results if record.status != "complete")
    selection = RetrySelection(task_ids=(failed.task_id,), operation_id="retry-001")
    retried = resume_run(first.run_id, store=store, retry=selection)
    assert (
        retried.attempts == 3
        and retried.results == 7
        and retried.unsuccessful_results == 2
    )
    again = resume_run(first.run_id, store=store, retry=selection)
    assert again.attempts == retried.attempts and again.results == retried.results
    completed = next(
        record
        for record in results
        if record.status == "complete" and record.request_uid == failed.request_uid
    )
    for denied in (
        RetrySelection(task_ids=(completed.task_id,), operation_id="retry-001"),
        RetrySelection(task_ids=(completed.task_id,), operation_id="retry-completed"),
    ):
        with pytest.raises(LifecycleError) as error:
            resume_run(first.run_id, store=store, retry=denied)
        assert error.value.category == "validation"
    authorized = resume_run(
        first.run_id,
        store=store,
        retry=RetrySelection(
            task_ids=(completed.task_id,),
            operation_id="retry-completed",
            allow_completed=True,
        ),
    )
    assert authorized.attempts == 4


@pytest.mark.asyncio
async def test_cancellation_drains_retry_authorization_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, path = config(tmp_path, grouped=True)
    summary = await arun_live(path)
    store = tmp_path / "store"
    with Repository(store) as repository:
        task = next(repository.iter_results(summary.run_id)).task_id
    entered, released = threading.Event(), threading.Event()
    original = lifecycle.authorize_subset_retry

    def blocked(*args: object, **kwargs: object) -> object:
        with (
            Repository(store) as repository,
            ExecutorOwnership(repository, summary.run_id),
        ):
            entered.set()
            assert released.wait(10)
        return original(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(lifecycle, "authorize_subset_retry", blocked)
    pending = asyncio.create_task(
        aresume_run(
            summary.run_id,
            store=store,
            retry=RetrySelection(
                task_ids=(task,), operation_id="retry", allow_completed=True
            ),
        )
    )
    assert await asyncio.to_thread(entered.wait, 10)
    pending.cancel()
    await asyncio.sleep(0.01)
    assert not pending.done()
    with Repository(store) as repository:
        assert repository.get_owner(summary.run_id) is not None
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    with Repository(store) as repository:
        assert repository.get_owner(summary.run_id) is None
        assert len(list(repository.iter_attempts(summary.run_id))) == 3


@pytest.mark.parametrize("setting", ["cache", "tier"])
def test_mock_offline_preflight_enforces_unsupported_settings(
    tmp_path: Path, setting: str
) -> None:
    spec, _ = config(tmp_path)
    if setting == "cache":
        spec.execution.cache_policy = "explicit"
    else:
        spec.execution.tier = "unsupported"
    report = validate_inputs(spec)
    assert not report.valid and report.issues[0].code == "unsupported_capability"
    with pytest.raises(LifecycleError) as error:
        estimate_run(spec)
    assert error.value.category == "unsupported_capability"
    assert not spec.output_path.exists()


def test_path_like_original_run_ids_keep_registry_identity(tmp_path: Path) -> None:
    store = tmp_path / "store"
    with Repository(store) as repository:
        for identifier in ("/absolute-looking-id", "relative/with/slashes"):
            repository.create_run(
                RunRecord(
                    run_id=identifier,
                    task_kind="rubric",
                    provider="mock",
                    endpoint="synthetic",
                    model="fixture",
                )
            )
    assert get_run("/absolute-looking-id", store=store).run_id == "/absolute-looking-id"
    assert (
        get_run("relative/with/slashes", store=store).run_id == "relative/with/slashes"
    )


def test_unavailable_run_directory_is_null_without_creating_paths(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    with Repository(store) as repository:
        repository.create_run(
            RunRecord(
                run_id="unsealed",
                task_kind="rubric",
                provider="mock",
                endpoint="synthetic",
                model="fixture",
            )
        )
        directory = repository.run_directory("unsealed")
    status = get_run("unsealed", store=store)
    assert status.artifacts.run_directory is None and status.artifacts.manifest is None
    assert status.artifacts.store == str(store.resolve())
    assert status.artifacts.registry == str((store / "registry.sqlite3").resolve())
    assert not directory.exists() and not (store / "runs").exists()


@pytest.mark.parametrize("component", ["runs", "directory", "lock"])
def test_status_lock_probe_rejects_each_symlink_component_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    store = tmp_path / "store"
    with Repository(store) as repository:
        repository.create_run(
            RunRecord(
                run_id="unsealed",
                task_kind="rubric",
                provider="mock",
                endpoint="synthetic",
                model="fixture",
            )
        )
        directory = repository.run_directory("unsealed")
    selected = {
        "runs": store / "runs",
        "directory": directory,
        "lock": directory / "executor.lock",
    }[component]
    original = Path.is_symlink

    def synthetic_symlink(path: Path) -> bool:
        return path == selected or original(path)

    monkeypatch.setattr(Path, "is_symlink", synthetic_symlink)
    with pytest.raises(LifecycleError) as error:
        get_run("unsealed", store=store)
    assert error.value.category == "execution"
    assert not (store / "runs").exists()
