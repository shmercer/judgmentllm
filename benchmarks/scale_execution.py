"""Complete, prefix and preparation qualification using the unchanged mock engine."""

from __future__ import annotations

import asyncio
import cProfile
import gc
import json
import shutil
import sys
import time
import tracemalloc
import uuid
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import patch

from instrumentation import Metrics, per_thread_profiles_supported, verify_profile_scope
from provenance import (
    fixture_fingerprint,
    harness_fingerprint,
    host_identity,
    input_fingerprint,
    runtime_identity,
    source_fingerprint,
)
from storage import StorageSampler
from workloads import Workload, inputs

from judgmentllm import get_run
from judgmentllm.config import load_run_spec
from judgmentllm.execution import execute_run
from judgmentllm.execution.exports import export_run
from judgmentllm.persistence.models import ExecutionSummary
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.mock import MockAdapter, MockScenario


class PrefixComplete(Exception):
    """Stop after the specified durable physical result commit."""


def run_scale(
    directory: Path,
    workload: Workload,
    capture: str,
    mode: str,
    *,
    extent: str = "complete",
    measurement_scope: str = "whole",
    disk_budget_gib: float = 32,
    min_free_gib: float = 10,
) -> dict[str, Any]:
    from execution_worker import native_peak_bytes

    imported_root = Path(sys.modules["judgmentllm"].__file__ or "").resolve().parents[2]
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "inputs" / "run.json"
    fixture_identity = {
        "workload": asdict(workload),
        "capture": capture,
        "fixture_definition_sha256": fixture_fingerprint(),
    }
    fixture_record = directory / "fixture-identity.json"
    if not config.exists():
        config = inputs(directory / "inputs", workload, capture)
        fixture_record.write_text(
            json.dumps(
                {
                    **fixture_identity,
                    "input_sha256": input_fingerprint(directory / "inputs"),
                }
            ),
            encoding="utf-8",
        )
    else:
        if not fixture_record.exists():
            raise RuntimeError(
                "Existing scale inputs have no fixture identity; choose a fresh work root"
            )
        retained = json.loads(fixture_record.read_text(encoding="utf-8"))
        if any(
            retained.get(key) != value for key, value in fixture_identity.items()
        ) or retained.get("input_sha256") != input_fingerprint(directory / "inputs"):
            raise RuntimeError(
                "Existing scale fixture dimensions or input bytes differ; choose a fresh work root"
            )
    store = directory / "store"
    (directory / "progress.json").unlink(missing_ok=True)
    if store.exists():
        shutil.rmtree(store)
    for path in directory.glob("service.sqlite3*"):
        path.unlink()
    metrics = Metrics()

    class Adapter(MockAdapter):
        def prepare(self, *args: Any, **kwargs: Any) -> Any:
            with metrics.phase("provider_payload_preparation"):
                prepared = super().prepare(*args, **kwargs)
            if mode == "profile":
                with metrics.lock:
                    metrics.counts["prepared_payload_bytes"] += len(prepared.payload)
            return prepared

        async def submit(self, *args: Any, **kwargs: Any) -> Any:
            started_at = time.perf_counter_ns()
            try:
                return await super().submit(*args, **kwargs)
            finally:
                metrics.latency("submission", time.perf_counter_ns() - started_at)

        async def retrieve(self, *args: Any, **kwargs: Any) -> Any:
            started_at = time.perf_counter_ns()
            try:
                return await super().retrieve(*args, **kwargs)
            finally:
                metrics.latency("retrieval", time.perf_counter_ns() - started_at)

    with patch(
        "judgmentllm.providers.ledger.uuid.uuid4", return_value=uuid.UUID(int=42)
    ):
        adapter = Adapter(
            directory / "service.sqlite3",
            scenario=MockScenario(category_index=1, probabilities=True),
        )
    profile = mode == "profile"
    allocation = mode in {"tracemalloc", "combined"}
    if profile:
        metrics.thread_profiles_supported = per_thread_profiles_supported()
        verify_profile_scope(metrics.thread_profiles_supported)
    phases: dict[str, dict[str, float]] = {}
    completed = created = live = peak = executor_tasks = prepared_seen = (
        scheduling_lead
    ) = 0
    queues: dict[int, int] = {}
    baseline: dict[str, int] = {}
    allocations: dict[str, Any] = {}
    phase_allocations: dict[str, dict[str, int | float]] = {}
    overall_traced_peak = 0
    exports: list[dict[str, Any]] = []
    sampler = StorageSampler(
        directory, budget_gib=disk_budget_gib, min_free_gib=min_free_gib
    )

    @contextmanager
    def phase(name: str) -> Generator[None, None, None]:
        nonlocal overall_traced_peak
        measure_phase = (
            allocation and measurement_scope == "whole" and tracemalloc.is_tracing()
        )
        phase_baseline = 0
        if measure_phase:
            phase_baseline, prior_peak = tracemalloc.get_traced_memory()
            # Capture peaks between phases too, before resetting the tracer's
            # high-water mark. The whole-run maximum must never be lost.
            overall_traced_peak = max(overall_traced_peak, prior_peak)
            tracemalloc.reset_peak()
            if name == "execution":
                baseline["python_current_bytes"] = phase_baseline
                baseline["native_peak_bytes"] = native_peak_bytes()
        start_wall, start_cpu = time.perf_counter_ns(), time.process_time_ns()
        try:
            with metrics.phase(name):
                yield
        finally:
            phases[name] = {
                "wall_seconds": (time.perf_counter_ns() - start_wall) / 1e9,
                "cpu_seconds": (time.process_time_ns() - start_cpu) / 1e9,
            }
            if measure_phase:
                current, absolute_peak = tracemalloc.get_traced_memory()
                overall_traced_peak = max(overall_traced_peak, absolute_peak)
                gc_started = time.perf_counter()
                gc.collect()
                gc_wall = time.perf_counter() - gc_started
                retained = tracemalloc.get_traced_memory()[0]
                phase_allocations[name] = {
                    "baseline_live_bytes": phase_baseline,
                    "peak_absolute_bytes": absolute_peak,
                    "live_after_bytes": current,
                    "retained_after_gc_bytes": retained,
                    "peak_above_baseline_bytes": max(0, absolute_peak - phase_baseline),
                    "net_live_growth_bytes": current - phase_baseline,
                    "net_retained_growth_bytes": retained - phase_baseline,
                    "post_phase_gc_wall_seconds": gc_wall,
                }

    def begin_execution_allocations() -> None:
        if (
            allocation
            and measurement_scope == "execution"
            and not tracemalloc.is_tracing()
        ):
            gc.collect()
            baseline["native_peak_bytes"] = native_peak_bytes()
            tracemalloc.start()
            baseline["python_current_bytes"] = tracemalloc.get_traced_memory()[0]

    def fault(stage: str, _request_uid: str) -> None:
        nonlocal completed, prepared_seen, scheduling_lead
        sampler.check()
        if stage == "before_request_creation":
            begin_execution_allocations()
        if stage == "after_preparation":
            prepared_seen += 1
            scheduling_lead = max(scheduling_lead, prepared_seen - completed)
            assert scheduling_lead <= 4 * workload.concurrency + 3
        if stage == "after_result":
            completed += 1
            if completed % 64 == 0:
                progress = directory / "progress.json"
                pending = progress.with_suffix(".tmp")
                pending.write_text(
                    json.dumps(
                        {
                            "completed_physical_requests": completed,
                            "prepared_physical_requests_observed": prepared_seen,
                            "maximum_prepared_minus_completed": scheduling_lead,
                            "native_peak_bytes": native_peak_bytes(),
                            "storage": sampler.report(),
                            "updated_at": datetime.now(UTC).isoformat(),
                        }
                    ),
                    encoding="utf-8",
                )
                pending.replace(progress)
            if extent == "prefix" and completed == workload.requests:
                raise PrefixComplete

    async def execute() -> None:
        loop = asyncio.get_running_loop()

        def factory(
            loop: asyncio.AbstractEventLoop, coroutine: Any, **kwargs: Any
        ) -> asyncio.Task[Any]:
            nonlocal created, live, peak, executor_tasks
            created += 1
            name = getattr(getattr(coroutine, "cr_code", None), "co_qualname", "")
            executor_tasks += int(name.startswith("execute_run.<locals>."))
            live += 1
            peak = max(peak, live)
            task = asyncio.Task(coroutine, loop=loop, **kwargs)

            def done(_task: asyncio.Task[Any]) -> None:
                nonlocal live
                live -= 1

            task.add_done_callback(done)
            return task

        if allocation or profile:
            loop.set_task_factory(factory)

        class Queue(asyncio.Queue[Any]):
            async def put(self, item: Any) -> None:
                await super().put(item)
                queues[self.maxsize] = max(queues.get(self.maxsize, 0), self.qsize())

        try:
            with (
                patch("judgmentllm.execution.core.asyncio.Queue", Queue)
                if allocation or profile
                else ExitStack()
            ):
                try:
                    report = await execute_run(
                        "001", store=store, adapter=adapter, spec=spec, fault=fault
                    )
                    allocations["worker_count"] = report.worker_count
                    allocations["request_queue_high_water"] = (
                        report.request_queue_high_water
                    )
                    allocations["result_queue_high_water"] = (
                        report.result_queue_high_water
                    )
                except* PrefixComplete:
                    pass
        finally:
            loop.set_task_factory(None)

    def record_allocations() -> None:
        nonlocal overall_traced_peak
        current, high = tracemalloc.get_traced_memory()
        overall_traced_peak = max(overall_traced_peak, high)
        gc.collect()
        allocations.update(
            python_peak_bytes=overall_traced_peak
            if measurement_scope == "whole"
            else high,
            python_live_bytes=current,
            python_retained_bytes=tracemalloc.get_traced_memory()[0],
        )

    start_wall, start_cpu = time.perf_counter_ns(), time.process_time_ns()
    started = datetime.now(UTC).isoformat()
    profiler = cProfile.Profile(subcalls=False, builtins=True) if profile else None
    failure: dict[str, str] | None = None
    try:
        with sampler, ExitStack() as stack:
            metrics.install(stack, profile=profile)
            if profiler is not None:
                metrics.profiles.append(profiler)
                profiler.enable()
            if allocation and measurement_scope == "whole":
                tracemalloc.start()
            with phase("configuration"):
                spec = load_run_spec(config)
            with phase("preparation"):
                with prepare_run(
                    spec,
                    store=store,
                    run_id="001",
                    capabilities=adapter.capabilities(spec),
                ):
                    pass
            sampler.phase("after_preparation")
            if extent != "prepare":
                with phase("execution"):
                    asyncio.run(execute())
                if allocation and measurement_scope == "execution":
                    record_allocations()
                    tracemalloc.stop()
                sampler.phase("after_execution")
            with phase("inspection"):
                status = get_run("001", store=store)
            allocations["inspection_results"] = status.results
            if extent != "prepare":
                for format, view in (
                    ("jsonl", "canonical"),
                    ("csv", "canonical"),
                    ("csv", "r"),
                ):
                    if allocation and measurement_scope == "execution":
                        tracemalloc.start()
                    with phase(f"export_{format}_{view}"):
                        exported = export_run(
                            "001",
                            store=store,
                            format=cast(Literal["jsonl", "csv"], format),
                            view=cast(Literal["canonical", "r"], view),
                            fault=lambda stage, selected_format=format, selected_view=view: (
                                sampler.phase(
                                    selected_format + "_" + selected_view + "_" + stage
                                )
                                if stage
                                in {
                                    "after_export_serialization",
                                    "after_export_artifact",
                                }
                                else sampler.check()
                            ),
                        )
                    if allocation and measurement_scope == "execution":
                        allocations["export_python_peak_bytes"] = max(
                            allocations.get("export_python_peak_bytes", 0),
                            tracemalloc.get_traced_memory()[1],
                        )
                        tracemalloc.stop()
                    exports.append(
                        {
                            "format": format,
                            "view": view,
                            "rows": exported.row_count,
                            "bytes": exported.artifact.byte_length,
                            "sha256": exported.artifact.sha256,
                            "path": exported.artifact_path,
                            "result_high_water_mark": exported.result_high_water_mark,
                        }
                    )
                sampler.phase("after_exports")
            if allocation and measurement_scope == "whole":
                record_allocations()
    except Exception as error:
        failure = {"type": type(error).__name__, "message": str(error)}
    finally:
        if profiler is not None:
            profiler.disable()
        if tracemalloc.is_tracing():
            if "python_peak_bytes" not in allocations:
                record_allocations()
            tracemalloc.stop()
    wall, cpu = (
        (time.perf_counter_ns() - start_wall) / 1e9,
        (time.process_time_ns() - start_cpu) / 1e9,
    )
    if (store / "registry.sqlite3").exists():
        with ReadOnlyRepository(store) as repository:
            summary = repository.summary("001")
            requests = repository.connection.execute(
                "SELECT COUNT(*) FROM requests"
            ).fetchone()[0]
    else:
        summary = ExecutionSummary(
            run_id="001",
            state="created",
            tasks=0,
            attempts=0,
            results=0,
            successful_results=0,
            unresolved_submissions=0,
        )
        requests = 0
    accepted = adapter.ledger.acceptance_count()
    result: dict[str, Any] = {
        "workload": workload.name,
        "workload_config": asdict(workload),
        "working_directory": str(directory.resolve()),
        "extent": extent,
        "measurement_scope": measurement_scope,
        "capture": capture,
        "mode": mode,
        "source_root": str(imported_root),
        "source_tree_sha256": source_fingerprint(imported_root),
        "harness_sha256": harness_fingerprint(),
        "fixture_definition_sha256": fixture_fingerprint(),
        "input_sha256": input_fingerprint(directory / "inputs"),
        "runtime_identity": runtime_identity(),
        "host_identity": host_identity(imported_root),
        "timing_started_at": started,
        "timing_finished_at": datetime.now(UTC).isoformat(),
        "wall_seconds": wall,
        "cpu_seconds": cpu,
        "phase_timings": phases,
        "phases": metrics.phases,
        "counters": dict(metrics.counts),
        "logical_task_count": workload.logical_tasks,
        "planned_physical_requests": workload.physical_requests,
        "completed_physical_requests": completed,
        "prepared_requests": requests,
        "prepared_attempts": summary.attempts,
        "accepted_physical_requests": accepted,
        "result_rows": summary.results,
        "prefix_only": extent == "prefix",
        "concurrency": workload.concurrency,
        "created_asyncio_tasks": created if allocation or profile else None,
        "execution_asyncio_tasks": executor_tasks if allocation or profile else None,
        "peak_asyncio_tasks": peak if allocation or profile else None,
        "request_queue_high_water": allocations.get(
            "request_queue_high_water", queues.get(2 * workload.concurrency)
        ),
        "result_queue_high_water": allocations.get(
            "result_queue_high_water", queues.get(workload.concurrency)
        ),
        "prepared_physical_requests_observed": prepared_seen,
        "maximum_prepared_minus_completed": scheduling_lead,
        "python_peak_bytes": allocations.get("python_peak_bytes"),
        "python_live_bytes": allocations.get("python_live_bytes"),
        "python_retained_bytes": allocations.get("python_retained_bytes"),
        "export_python_peak_bytes": allocations.get("export_python_peak_bytes"),
        "allocation_baseline": baseline,
        "phase_allocations": phase_allocations,
        "allocation_measurement_note": "whole-scope phase peaks are absolute traced live bytes, including initialized survivors; baseline deltas are explanatory only; diagnostic wall time includes post-phase garbage collection"
        if allocation and measurement_scope == "whole"
        else None,
        "phase_envelope_proof": {
            "execution_below_32_mib": phase_allocations.get("execution", {}).get(
                "peak_absolute_bytes", float("inf")
            )
            < 32 * 1024**2,
            "all_exports_below_8_mib": len(
                [name for name in phase_allocations if name.startswith("export_")]
            )
            == 3
            and all(
                value["peak_absolute_bytes"] < 8 * 1024**2
                for name, value in phase_allocations.items()
                if name.startswith("export_")
            ),
            "method": "absolute whole-scope live peaks include fixed survivors; true supplies a conservative envelope proof; false requires a separate execution-only diagnostic and does not establish failure of the original incremental envelope",
        }
        if allocation and measurement_scope == "whole" and extent != "prepare"
        else None,
        "native_peak_bytes": native_peak_bytes(),
        "storage": sampler.report(),
        "export_files": exports,
        "cpu_ms_per_completed_physical_request": cpu * 1000 / completed
        if completed
        else None,
        "physical_requests_per_second": completed / phases["execution"]["wall_seconds"]
        if completed
        else None,
        "logical_judgments_per_second": summary.results
        / phases["execution"]["wall_seconds"]
        if completed
        else None,
        "status": "failed"
        if failure
        else "passed"
        if extent == "prepare"
        else "execution_passed",
        "failure": failure,
    }
    if profile:
        result["profile"] = metrics.profile_rows()
    if failure is None:
        try:
            assert result["native_peak_bytes"] < 256 * 1024**2, (
                "native process peak exceeds 256 MiB"
            )
            assert completed == workload.requests
            if extent == "complete":
                assert (
                    requests
                    == summary.attempts
                    == accepted
                    == workload.physical_requests
                )
                assert (
                    summary.results
                    == summary.successful_results
                    == workload.logical_tasks
                )
            elif extent == "prepare":
                assert requests == summary.attempts == accepted == summary.results == 0
            if extent != "prepare" and (allocation or profile):
                assert executor_tasks == workload.concurrency + 2
                assert peak <= 2 * workload.concurrency + 3
                assert created < completed + 4 * workload.concurrency + 3
            if extent != "prepare":
                if result["request_queue_high_water"] is not None:
                    assert (
                        0
                        < result["request_queue_high_water"]
                        <= 2 * workload.concurrency
                    )
                    assert 0 < result["result_queue_high_water"] <= workload.concurrency
            if allocation and measurement_scope == "execution" and extent != "prepare":
                assert allocations["python_peak_bytes"] < 32 * 1024**2, (
                    "execution Python peak exceeds 32 MiB"
                )
                assert allocations["export_python_peak_bytes"] < 8 * 1024**2, (
                    "export Python peak exceeds 8 MiB"
                )
        except Exception as error:
            result["status"] = "failed"
            result["failure"] = {"type": type(error).__name__, "message": str(error)}
    return result
