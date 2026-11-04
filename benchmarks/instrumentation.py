"""Opt-in standard-library profiling; counters never retain assessment content."""

from __future__ import annotations

import cProfile
import json
import sqlite3
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from functools import wraps
from typing import Any, cast
from unittest.mock import patch


def per_thread_profiles_supported() -> bool:
    """Probe cProfile scope without assuming identical CPython implementations."""
    outer = cProfile.Profile(subcalls=False, builtins=True)
    supported: list[bool] = []

    def probe() -> None:
        inner = cProfile.Profile(subcalls=False, builtins=True)
        try:
            inner.enable()
        except ValueError:
            supported.append(False)
        else:
            inner.disable()
            supported.append(True)

    outer.enable()
    try:
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
    finally:
        outer.disable()
    return supported[0]


def verify_profile_scope(per_thread: bool) -> dict[str, int]:
    """Require known main/I/O/provider markers and native validation entries."""
    from pydantic import BaseModel

    class Probe(BaseModel):
        value: int

    profiles: list[cProfile.Profile] = []

    def main_marker() -> None:
        Probe.model_validate({"value": 1})

    def io_marker() -> None:
        Probe.model_validate({"value": 1})

    def provider_marker() -> None:
        Probe.model_validate({"value": 1})

    def work(function: Callable[[], None]) -> None:
        instance = (
            cProfile.Profile(subcalls=False, builtins=True) if per_thread else None
        )
        if instance is not None:
            profiles.append(instance)
            instance.enable()
        try:
            function()
        finally:
            if instance is not None:
                instance.disable()

    main = cProfile.Profile(subcalls=False, builtins=True)
    profiles.append(main)
    main.enable()
    try:
        main_marker()
        with ThreadPoolExecutor(max_workers=1) as io:
            io.submit(work, io_marker).result()
        with ThreadPoolExecutor(max_workers=1) as provider:
            provider.submit(work, provider_marker).result()
        main_marker()
    finally:
        main.disable()
    entries: list[Any] = [entry for profile in profiles for entry in profile.getstats()]
    counts = {
        name: sum(
            entry.callcount for entry in entries if entry.code is function.__code__
        )
        for name, function in (
            ("main", main_marker),
            ("io", io_marker),
            ("provider", provider_marker),
        )
    }
    counts["pydantic_core_validation_entries"] = sum(
        entry.callcount
        for entry in entries
        if isinstance(entry.code, str)
        and "SchemaValidator" in entry.code
        and "validate_python" in entry.code
    )
    expected = {
        "main": 2,
        "io": 1,
        "provider": 1,
        "pydantic_core_validation_entries": 4,
    }
    if counts != expected or any(
        entry.totaltime < -1e-6 or entry.inlinetime < -1e-6 for entry in entries
    ):
        raise RuntimeError(f"Profiler scope/clock probe failed: {counts}")
    return counts


class Metrics:
    def __init__(self) -> None:
        self.phases: dict[str, dict[str, int]] = {}
        self.counts: Counter[str] = Counter()
        self.local = threading.local()
        self.profiles: list[cProfile.Profile] = []
        self.thread_profiles_supported = False
        self.lock = threading.Lock()

    @contextmanager
    def phase(self, name: str) -> Generator[None, None, None]:
        # CPU accounting is exclusive within each thread. Async provider latency
        # is recorded separately; a thread clock spanning await is not attribution.
        stack: list[list[int]] = getattr(self.local, "stack", [])
        self.local.stack = stack
        frame = [time.perf_counter_ns(), time.thread_time_ns(), 0]
        stack.append(frame)
        try:
            yield
        finally:
            elapsed, cpu = (
                time.perf_counter_ns() - frame[0],
                time.thread_time_ns() - frame[1],
            )
            stack.pop()
            if stack:
                stack[-1][2] += cpu
            with self.lock:
                record = self.phases.setdefault(
                    name, {"calls": 0, "wall_ns": 0, "exclusive_thread_cpu_ns": 0}
                )
                record["calls"] += 1
                record["wall_ns"] += elapsed
                record["exclusive_thread_cpu_ns"] += cpu - frame[2]

    def latency(self, name: str, elapsed: int) -> None:
        with self.lock:
            record = self.phases.setdefault(name, {"calls": 0, "wall_ns": 0})
            record["calls"] += 1
            record["wall_ns"] += elapsed

    def timed(self, name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(function)
        def measured(*args: Any, **kwargs: Any) -> Any:
            active: set[str] = getattr(self.local, "active_phases", set())
            self.local.active_phases = active
            if name in active:
                return function(*args, **kwargs)
            active.add(name)
            try:
                with self.phase(name):
                    return function(*args, **kwargs)
            finally:
                active.remove(name)

        return measured

    def install(self, stack: ExitStack, *, profile: bool) -> None:
        from judgmentllm.execution.session import ExecutionSession
        from judgmentllm.preparation.session import PreparedRun

        # Keep these light phase hooks identical in ordinary and instrumented runs.
        for owner, name, phase in (
            (PreparedRun, "render", "selected_request_rendering"),
            (PreparedRun, "render_attempt", "selected_request_rendering"),
            (ExecutionSession, "process", "result_processing"),
        ):
            stack.enter_context(
                patch.object(owner, name, self.timed(phase, getattr(owner, name)))
            )
        if not profile:
            return
        stack.enter_context(
            patch.object(
                ExecutionSession,
                "__init__",
                self.timed("session_reopening", ExecutionSession.__init__),
            )
        )
        self._sql(stack)
        self._cpu_boundaries(stack)
        self._byte_counters(stack)
        metrics = self

        class ProfiledExecutor(ThreadPoolExecutor):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                def initialize() -> None:
                    instance = cProfile.Profile(subcalls=False, builtins=True)
                    metrics.local.profile = instance
                    metrics.profiles.append(instance)
                    instance.enable()

                super().__init__(*args, initializer=initialize, **kwargs)

        original = ExecutionSession.close
        original_checkpoint = ExecutionSession.checkpoint

        def checkpoint(
            session: ExecutionSession, stage: str, request_uid: str = ""
        ) -> None:
            if stage == "after_result":
                path = session.repository.root / "registry.sqlite3-wal"
                size = path.stat().st_size if path.exists() else 0
                with metrics.lock:
                    metrics.counts["registry_wal_observed_high_water_bytes"] = max(
                        metrics.counts["registry_wal_observed_high_water_bytes"], size
                    )
            original_checkpoint(session, stage, request_uid)

        def close(session: ExecutionSession) -> None:
            try:
                original(session)
            finally:
                instance = getattr(metrics.local, "profile", None)
                if instance is not None:
                    instance.disable()

        if self.thread_profiles_supported:
            stack.enter_context(
                patch("judgmentllm.execution.core.ThreadPoolExecutor", ProfiledExecutor)
            )
            # asyncio.to_thread also validates payloads and ledger evidence.
            stack.enter_context(
                patch("concurrent.futures.ThreadPoolExecutor", ProfiledExecutor)
            )
            stack.enter_context(patch.object(ExecutionSession, "close", close))
        stack.enter_context(patch.object(ExecutionSession, "checkpoint", checkpoint))

    def _cpu_boundaries(self, stack: ExitStack) -> None:
        from judgmentllm import io, planning
        from judgmentllm.persistence import generation
        from judgmentllm.preparation import manifest, session
        from judgmentllm.preparation.session import PreparedRun
        from judgmentllm.providers.mock import MockAdapter

        stack.enter_context(
            patch.object(
                MockAdapter,
                "validate",
                self.timed("cpu_mock_validation", MockAdapter.validate),
            )
        )

        stack.enter_context(
            patch.object(
                PreparedRun,
                "_verify_compatibility",
                self.timed("cpu_compatibility", PreparedRun._verify_compatibility),  # pyright: ignore[reportPrivateUsage]
            )
        )
        stack.enter_context(
            patch.object(
                PreparedRun,
                "_source_guard",
                self.timed("cpu_source_guard", PreparedRun._source_guard),  # pyright: ignore[reportPrivateUsage]
            )
        )
        for original, name in (
            (io._revalidate_record, "cpu_record_revalidation"),  # pyright: ignore[reportPrivateUsage]
            (manifest.configuration_digest, "cpu_configuration_digest"),
            (
                generation.validate_generation_compatibility,
                "cpu_generation_compatibility",
            ),
            (planning.plan_run, "cpu_planning"),
            (session._render_request, "cpu_actual_request_rendering"),  # pyright: ignore[reportPrivateUsage]
        ):
            replacement = self.timed(name, original)
            for module_name, module in tuple(sys.modules.items()):
                if module_name.startswith("judgmentllm"):
                    for attribute, value in tuple(vars(module).items()):
                        if value is original:
                            stack.enter_context(
                                patch.object(module, attribute, replacement)
                            )

    def _byte_counters(self, stack: ExitStack) -> None:
        from pydantic import BaseModel

        from judgmentllm.persistence import hashing

        def json_serialized(
            function: Callable[..., str], name: str
        ) -> Callable[..., str]:
            @wraps(function)
            def encode(*args: Any, **kwargs: Any) -> str:
                result = function(*args, **kwargs)
                byte_length = len(result.encode("utf-8"))
                with self.lock:
                    self.counts["json_serialization_calls"] += 1
                    self.counts["json_serialized_bytes"] += byte_length
                    self.counts[f"{name}_calls"] += 1
                    self.counts[f"{name}_bytes"] += byte_length
                return result

            return encode

        stack.enter_context(
            patch.object(json, "dumps", json_serialized(json.dumps, "json_dumps"))
        )
        stack.enter_context(
            patch.object(
                BaseModel,
                "model_dump_json",
                json_serialized(BaseModel.model_dump_json, "pydantic_json"),
            )
        )

        def counted(function: Callable[..., Any], name: str) -> Callable[..., Any]:
            @wraps(function)
            def invoke(value: Any) -> Any:
                result = function(value)
                with self.lock:
                    self.counts[f"{name}_calls"] += 1
                    self.counts[f"{name}_bytes"] += len(
                        result if name == "canonical_serialization" else value
                    )
                    if (
                        name == "bytes_digest"
                        and b'"protocol":"judgmentllm-mock-v1"' in value
                    ):
                        # These fixed synthetic inputs never contain this marker
                        # in source/response text. Inspect, count, then release;
                        # never retain a payload or change its authoritative hash.
                        self.counts["payload_hash_calls"] += 1
                        self.counts["payload_hash_bytes"] += len(value)
                return result

            return invoke

        for original, name in (
            (hashing.canonical_bytes, "canonical_serialization"),
            (hashing.bytes_digest, "bytes_digest"),
        ):
            replacement = counted(original, name)
            # Patch imported aliases too. No bytes or text enter the counters.
            for module_name, module in tuple(sys.modules.items()):
                if module_name.startswith("judgmentllm"):
                    for attribute, value in tuple(vars(module).items()):
                        if value is original:
                            stack.enter_context(
                                patch.object(module, attribute, replacement)
                            )

    def _sql(self, stack: ExitStack) -> None:
        metrics = self
        connect = sqlite3.connect

        class Connection(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                start = time.perf_counter_ns()
                try:
                    return super().execute(sql, parameters)
                finally:
                    with metrics.lock:
                        metrics.counts["sqlite_execute_calls"] += 1
                        metrics.counts["sqlite_execute_ns"] += (
                            time.perf_counter_ns() - start
                        )

            def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
                start = time.perf_counter_ns()
                try:
                    return super().executemany(sql, parameters)
                finally:
                    with metrics.lock:
                        metrics.counts["sqlite_executemany_calls"] += 1
                        metrics.counts["sqlite_execute_ns"] += (
                            time.perf_counter_ns() - start
                        )

            def commit(self) -> None:
                start = time.perf_counter_ns()
                try:
                    super().commit()
                finally:
                    with metrics.lock:
                        metrics.counts["sqlite_explicit_commit_ns"] += (
                            time.perf_counter_ns() - start
                        )

            def rollback(self) -> None:
                start = time.perf_counter_ns()
                try:
                    super().rollback()
                finally:
                    with metrics.lock:
                        metrics.counts["sqlite_explicit_rollback_ns"] += (
                            time.perf_counter_ns() - start
                        )

        def measured_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            kwargs.setdefault("factory", Connection)
            connection = connect(*args, **kwargs)

            def trace(sql: str) -> None:
                # Count implicit ledger context transactions too. Never retain
                # SQL, parameters, source text, or raw provider bodies.
                operation = sql.lstrip().split(None, 1)[0].upper()
                if operation in {"BEGIN", "COMMIT", "ROLLBACK"}:
                    with metrics.lock:
                        metrics.counts[f"sqlite_{operation.lower()}"] += 1

            connection.set_trace_callback(trace)
            return connection

        stack.enter_context(patch.object(sqlite3, "connect", measured_connect))

    def profile_rows(self) -> list[dict[str, Any]]:
        import pstats

        stats = pstats.Stats()
        for instance in self.profiles:
            stats.add(instance)
        entries: dict[tuple[str, int, str], tuple[int, int, float, float, object]] = (
            cast(Any, stats).stats
        )
        rows: list[dict[str, Any]] = [
            {
                "file": path,
                "line": line,
                "function": name,
                "primitive_calls": primitive,
                "calls": calls,
                "self_seconds": own,
                "cumulative_seconds": cumulative,
            }
            for (path, line, name), (
                primitive,
                calls,
                own,
                cumulative,
                _,
            ) in entries.items()
        ]
        if any(
            row["self_seconds"] < -1e-6 or row["cumulative_seconds"] < -1e-6
            for row in rows
        ):
            raise RuntimeError(
                "Invalid negative profiler durations; discard this diagnostic"
            )
        return rows
