"""One fresh-process offline assessment; launched by execution_benchmark.py."""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import gc
import json
import platform
import shutil
import sqlite3
import sys
import time
import tracemalloc
import uuid
from collections.abc import Callable
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import patch

from instrumentation import Metrics, per_thread_profiles_supported, verify_profile_scope
from provenance import (
    fixture_fingerprint,
    harness_fingerprint,
    input_fingerprint,
    runtime_identity,
    source_fingerprint,
)
from workloads import WORKLOADS, Workload, inputs, scale_workload

from judgmentllm.config import load_run_spec
from judgmentllm.execution import execute_run
from judgmentllm.execution.exports import export_run
from judgmentllm.execution.retry import prepare_subset_retry
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.providers.mock import MockAdapter, MockMemberScenario, MockScenario


class PrefixComplete(Exception):
    """End the established prefix only after its 64th durable physical result."""


def native_peak_bytes() -> int:
    # Share the existing portable OS measurement without shipping it in the core.
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from scripts.preparation_memory_fixture import native_peak_bytes as measure

    return measure()


def run(directory: Path, workload: Workload, capture: str, mode: str) -> dict[str, Any]:
    imported_root = Path(sys.modules["judgmentllm"].__file__ or "").resolve().parents[2]
    source_identity = source_fingerprint(imported_root)
    environment_identity = runtime_identity()
    tool_identity, fixture_identity = harness_fingerprint(), fixture_fingerprint()
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "inputs" / "run.json"
    if not config.exists():
        config = inputs(directory / "inputs", workload, capture)
    spec = load_run_spec(config)
    metrics = Metrics()
    profile = mode == "profile"
    scope_probe: dict[str, int] = {}
    if profile:
        metrics.thread_profiles_supported = per_thread_profiles_supported()
        scope_probe = verify_profile_scope(metrics.thread_profiles_supported)
    allocation = mode in {"tracemalloc", "combined"}
    coverage = mode in {"coverage", "combined"}
    completed = 0
    created = live = peak = executor_tasks = 0
    queues: dict[int, int] = {}
    scenario = (
        MockScenario()
        if workload.prefix
        else MockScenario(
            category_index=1,
            probabilities=True,
            members={
                f"{trait:03d}": MockMemberScenario(outcome="invalid_category")
                for trait in (4, 9, 14, 19)
            }
            if workload.kind == "retry"
            else {},
        )
    )

    class Adapter(MockAdapter):
        def prepare(self, *args: Any, **kwargs: Any) -> Any:
            with metrics.phase("provider_payload_preparation"):
                prepared = super().prepare(*args, **kwargs)
            if profile:
                with metrics.lock:
                    metrics.counts["prepared_payload_bytes"] += len(prepared.payload)
            return prepared

        async def submit(self, *args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter_ns()
            try:
                return await super().submit(*args, **kwargs)
            finally:
                metrics.latency("submission", time.perf_counter_ns() - start)

        async def retrieve(self, *args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter_ns()
            try:
                return await super().retrieve(*args, **kwargs)
            finally:
                metrics.latency("retrieval", time.perf_counter_ns() - start)

    store = directory / "store"
    seed = directory / "pristine"
    if not seed.exists():
        with Repository(seed / "store"):
            pass
        with patch(
            "judgmentllm.providers.ledger.uuid.uuid4", return_value=uuid.UUID(int=42)
        ):
            Adapter(seed / "service.sqlite3", scenario=scenario)
    # Restore pristine DB contents outside measured phases. Source files stay
    # untouched between repetitions. Backup avoids copying live WAL/SHM files.
    if store.exists():
        shutil.rmtree(store)
    store.mkdir()
    for path in directory.glob("service.sqlite3*"):
        path.unlink()
    for original, destination in (
        (seed / "store" / "registry.sqlite3", store / "registry.sqlite3"),
        (seed / "service.sqlite3", directory / "service.sqlite3"),
    ):
        with (
            closing(sqlite3.connect(original)) as source,
            closing(sqlite3.connect(destination)) as target,
        ):
            source.backup(target)
    adapter = Adapter(directory / "service.sqlite3", scenario=scenario)

    def fault(stage: str, request_uid: str) -> None:
        nonlocal completed
        if stage == "after_result":
            completed += 1
            if workload.prefix and completed == workload.requests:
                raise PrefixComplete

    class Queue(asyncio.Queue[Any]):
        async def put(self, item: Any) -> None:
            await super().put(item)
            queues[self.maxsize] = max(queues.get(self.maxsize, 0), self.qsize())

    async def execute(provider: Adapter) -> None:
        nonlocal created, live, peak, executor_tasks
        loop = asyncio.get_running_loop()

        def factory(
            loop: asyncio.AbstractEventLoop, coroutine: Any, **kwargs: Any
        ) -> asyncio.Task[Any]:
            nonlocal created, live, peak, executor_tasks
            created += 1
            name = getattr(getattr(coroutine, "cr_code", None), "co_qualname", "")
            if name.startswith("execute_run.<locals>."):
                executor_tasks += 1
            live += 1
            peak = max(peak, live)
            task = asyncio.Task(coroutine, loop=loop, **kwargs)

            def done(task: asyncio.Task[Any]) -> None:
                nonlocal live
                live -= 1

            task.add_done_callback(done)
            return task

        if profile or allocation:
            loop.set_task_factory(factory)
        try:
            with (
                patch("judgmentllm.execution.core.asyncio.Queue", Queue)
                if profile or allocation
                else ExitStack()
            ):
                try:
                    await execute_run(
                        "001", store=store, adapter=provider, spec=spec, fault=fault
                    )
                except* PrefixComplete:
                    pass
        finally:
            loop.set_task_factory(None)

    tracer: Any = None
    if coverage:
        import coverage as coverage_module

        tracer = coverage_module.Coverage(
            data_file=str(directory / ".coverage"), source=["judgmentllm"], branch=True
        )
        tracer.start()
    if allocation:
        tracemalloc.start()
    main_profile = cProfile.Profile(subcalls=False, builtins=True) if profile else None
    gc.collect()
    timing_started_at = datetime.now(UTC).isoformat()
    start_wall, start_cpu = time.perf_counter_ns(), time.process_time_ns()
    try:
        with ExitStack() as stack:
            metrics.install(stack, profile=profile)
            if main_profile is not None:
                metrics.profiles.append(main_profile)
                main_profile.enable()
            with metrics.phase("preparation"):
                with prepare_run(
                    spec,
                    store=store,
                    run_id="001",
                    capabilities=adapter.capabilities(spec),
                ):
                    pass
            with metrics.phase("execution"):
                asyncio.run(execute(adapter))
                if workload.kind == "retry":
                    retry_adapter = Adapter(
                        directory / "service.sqlite3",
                        scenario=MockScenario(category_index=1, probabilities=True),
                    )
                    with metrics.phase("retry_authorization_preparation"):
                        with (
                            Repository(store) as repository,
                            ExecutorOwnership(repository, "001") as ownership,
                        ):
                            assert ownership.owner is not None
                            token = ownership.owner.token
                            with open_prepared_run(
                                "001",
                                store=store,
                                spec=spec,
                                capabilities=retry_adapter.capabilities(spec),
                                owner_token=token,
                            ) as prepared:
                                for request in repository.iter_requests("001"):
                                    dependency = prepared.dependency_for_request(
                                        request.request_uid
                                    )
                                    first = repository.get_attempt_for_request(
                                        "001", request.request_uid
                                    )
                                    assert first is not None
                                    failed = tuple(
                                        result.task_id
                                        for result in repository.get_results_for_attempt(
                                            "001", first.attempt_id
                                        )
                                        if result.status != "complete"
                                    )
                                    assert len(failed) == 1
                                    prepare_subset_retry(
                                        prepared,
                                        retry_adapter,
                                        dependency=dependency,
                                        task_ids=failed,
                                        operation_id=f"retry-{request.scheduling_ordinal:04d}",
                                        owner_token=token,
                                    )
                    asyncio.run(execute(retry_adapter))
            with metrics.phase("export"):
                exports = [
                    export_run(
                        "001",
                        store=store,
                        format=cast(Literal["jsonl", "csv"], format),
                        view=cast(Literal["canonical", "r"], view),
                    )
                    for format, view in (
                        ("jsonl", "canonical"),
                        ("csv", "canonical"),
                        ("csv", "r"),
                    )
                ]
            if main_profile is not None:
                main_profile.disable()
        wall_ns, cpu_ns = (
            time.perf_counter_ns() - start_wall,
            time.process_time_ns() - start_cpu,
        )
        timing_finished_at = datetime.now(UTC).isoformat()
        if allocation:
            live_bytes, peak_bytes = tracemalloc.get_traced_memory()
            gc.collect()
            retained_bytes = tracemalloc.get_traced_memory()[0]
        else:
            live_bytes = peak_bytes = retained_bytes = None
    finally:
        if main_profile is not None:
            main_profile.disable()
        if allocation:
            tracemalloc.stop()
        if tracer is not None:
            tracer.stop()
            tracer.save()
    assert completed == workload.requests, (completed, workload)
    with Repository(store) as repository:
        summary = repository.summary("001")
        requests = repository.connection.execute(
            "SELECT COUNT(*) FROM requests"
        ).fetchone()[0]
        attempts = repository.connection.execute(
            "SELECT COUNT(*) FROM attempts"
        ).fetchone()[0]
    acceptances = adapter.ledger.acceptance_count()
    expected_rows = (
        workload.requests * workload.group_size
        if workload.prefix
        else 4800
        if workload.kind == "retry"
        else 1200
        if workload.kind == "pairwise"
        else 200 * workload.traits
    )
    expected_original_requests = 800 if workload.kind == "retry" else workload.requests
    assert summary.results == expected_rows
    assert summary.successful_results == (
        4000 if workload.kind == "retry" else expected_rows
    )
    if workload.prefix:
        assert (
            workload.requests
            <= acceptances
            <= requests
            <= workload.requests + 4 * 4 + 3
        )
        assert attempts == requests
        assert summary.tasks == requests * workload.group_size
    else:
        assert requests == expected_original_requests
        assert attempts == acceptances == workload.requests
        assert summary.tasks == (
            1200 if workload.kind == "pairwise" else 200 * workload.traits
        )
    if profile or allocation:
        waves = 2 if workload.kind == "retry" else 1
        assert executor_tasks == (4 + 2) * waves
        assert peak <= 2 * 4 + 3
        assert created < workload.requests + (4 * 4 + 3) * waves
        assert 0 < queues.get(8, 0) <= 8
        assert 0 < queues.get(4, 0) <= 4
    end_file_sizes = {
        name: path.stat().st_size if path.exists() else 0
        for name, path in {
            "registry_db_bytes": store / "registry.sqlite3",
            "registry_wal_bytes_after_close": store / "registry.sqlite3-wal",
            "ledger_db_bytes": directory / "service.sqlite3",
            "ledger_wal_bytes_after_close": directory / "service.sqlite3-wal",
        }.items()
    }
    result = {
        "workload": workload.name,
        "working_directory": str(directory.resolve()),
        "capture": capture,
        "mode": mode,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "source_root": str(
            Path(sys.modules["judgmentllm"].__file__ or "").resolve().parents[2]
        ),
        "source_tree_sha256": source_identity,
        "runtime_identity": environment_identity,
        "harness_sha256": tool_identity,
        "fixture_definition_sha256": fixture_identity,
        "input_sha256": input_fingerprint(directory / "inputs"),
        "wall_seconds": wall_ns / 1e9,
        "timing_started_at": timing_started_at,
        "timing_finished_at": timing_finished_at,
        "cpu_seconds": cpu_ns / 1e9,
        "cpu_ms_per_completed_physical_request": cpu_ns / completed / 1e6,
        "completed_physical_requests": completed,
        "prepared_requests": requests,
        "prepared_attempts": attempts,
        "accepted_physical_requests": acceptances,
        "result_rows": summary.results,
        "prefix_only": workload.prefix,
        "concurrency": 4,
        "phases": metrics.phases,
        "counters": dict(metrics.counts),
        "python_peak_bytes": peak_bytes,
        "python_live_bytes": live_bytes,
        "python_retained_bytes": retained_bytes,
        "native_peak_bytes": native_peak_bytes(),
        "end_file_sizes": end_file_sizes,
        "created_asyncio_tasks": created if profile or allocation else None,
        "execution_asyncio_tasks": executor_tasks if profile or allocation else None,
        "peak_asyncio_tasks": peak if profile or allocation else None,
        "request_queue_high_water": queues.get(8) if profile or allocation else None,
        "result_queue_high_water": queues.get(4) if profile or allocation else None,
        "export_files": [
            {
                "format": item.format,
                "view": item.view,
                "rows": item.row_count,
                "bytes": item.artifact.byte_length,
                "sha256": item.artifact.sha256,
            }
            for item in exports
        ],
    }
    if profile:
        result["profiler_scope_probe"] = scope_probe
        result["profiler_scope"] = (
            "per-thread" if metrics.thread_profiles_supported else "interpreter"
        )
        rows = metrics.profile_rows()
        result["profile"] = rows
        selectors: dict[str, Callable[[dict[str, Any]], bool]] = {
            "pydantic_core_validation_entries": lambda row: (
                "SchemaValidator" in row["function"] and "validate_" in row["function"]
            ),
            "pydantic_model_validate": lambda row: row["function"] == "model_validate",
            "plan_run": lambda row: row["function"] == "plan_run",
            "prepared_render": lambda row: (
                row["function"] == "render"
                and row["file"].endswith("preparation/session.py")
            ),
            "render_attempt": lambda row: row["function"] == "render_attempt",
            "render_prompt": lambda row: row["function"] == "render_prompt",
            "source_guard": lambda row: row["function"] == "_source_guard",
            "source_signature_checks": lambda row: (
                row["function"] == "require_signature"
            ),
            "artifact_verification": lambda row: (
                row["function"] == "verify"
                and row["file"].endswith("persistence/artifacts.py")
            ),
            "compatibility_verification": lambda row: (
                row["function"] == "_verify_compatibility"
            ),
            "record_revalidation": lambda row: row["function"] == "_revalidate_record",
            "mock_validate": lambda row: (
                row["function"] == "validate"
                and row["file"].endswith("providers/mock.py")
            ),
            "mock_prepare": lambda row: (
                row["function"] == "prepare"
                and row["file"].endswith("providers/mock.py")
            ),
            "sha256_constructions": lambda row: (
                row["function"] == "<built-in method _hashlib.openssl_sha256>"
            ),
        }
        result["call_counts"] = {
            name: sum(row["calls"] for row in rows if predicate(row))
            for name, predicate in selectors.items()
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--expected-harness-sha256")
    parser.add_argument("--expected-fixture-sha256")
    parser.add_argument(
        "--workload",
        choices=[item.name for item in WORKLOADS] + ["scale-rubric"],
        required=True,
    )
    parser.add_argument("--capture", choices=["full", "minimal"], required=True)
    parser.add_argument(
        "--mode",
        choices=["plain", "profile", "tracemalloc", "coverage", "combined"],
        default="plain",
    )
    parser.add_argument("--responses", type=int, default=5000)
    parser.add_argument("--traits", type=int, default=20)
    parser.add_argument("--group-size", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--response-bytes", type=int, default=4096)
    parser.add_argument(
        "--fixture-variant", choices=["default", "rich"], default="default"
    )
    parser.add_argument(
        "--extent", choices=["complete", "prefix", "prepare"], default="complete"
    )
    parser.add_argument("--completed-requests", type=int, default=64)
    parser.add_argument(
        "--measurement-scope", choices=["whole", "execution"], default="whole"
    )
    parser.add_argument("--disk-budget-gib", type=float, default=32)
    parser.add_argument("--min-free-gib", type=float, default=10)
    options = parser.parse_args()
    if options.expected_source_sha256 is not None:
        imported_root = (
            Path(sys.modules["judgmentllm"].__file__ or "").resolve().parents[2]
        )
        if source_fingerprint(imported_root) != options.expected_source_sha256:
            raise RuntimeError(
                "Source changed before sample; use an immutable checkout/archive"
            )
    if (
        options.expected_harness_sha256 is not None
        and harness_fingerprint() != options.expected_harness_sha256
    ):
        raise RuntimeError("Harness changed before sample")
    if (
        options.expected_fixture_sha256 is not None
        and fixture_fingerprint() != options.expected_fixture_sha256
    ):
        raise RuntimeError("Fixture definition changed before sample")
    if options.workload == "scale-rubric":
        from scale_execution import run_scale

        workload = scale_workload(
            responses=options.responses,
            traits=options.traits,
            group_size=options.group_size,
            concurrency=options.concurrency,
            response_bytes=options.response_bytes,
            variant=options.fixture_variant,
            extent=options.extent,
            completed_requests=options.completed_requests,
        )
        result = run_scale(
            options.directory,
            workload,
            options.capture,
            options.mode,
            extent=options.extent,
            measurement_scope=options.measurement_scope,
            disk_budget_gib=options.disk_budget_gib,
            min_free_gib=options.min_free_gib,
        )
    else:
        workload = next(item for item in WORKLOADS if item.name == options.workload)
        result = run(
            options.directory,
            workload,
            cast(Literal["full", "minimal"], options.capture),
            options.mode,
        )
    imported_root = Path(sys.modules["judgmentllm"].__file__ or "").resolve().parents[2]
    if result["source_tree_sha256"] != source_fingerprint(imported_root):
        raise RuntimeError("Source changed during sample; discard this measurement")
    if (
        options.expected_harness_sha256 is not None
        and result["harness_sha256"] != harness_fingerprint()
    ):
        raise RuntimeError("Harness changed during sample; discard this measurement")
    if (
        options.expected_fixture_sha256 is not None
        and result["fixture_definition_sha256"] != fixture_fingerprint()
    ):
        raise RuntimeError(
            "Harness/fixture changed during sample; discard this measurement"
        )
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "workload",
                    "capture",
                    "mode",
                    "wall_seconds",
                    "cpu_seconds",
                    "completed_physical_requests",
                )
            }
        )
    )
    if result.get("status") == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
