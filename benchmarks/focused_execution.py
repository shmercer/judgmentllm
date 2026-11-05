"""Fresh bounded execution prefixes and separate traced initialization costs.

Default qualification compares the same 64 completed physical requests at 200
and 5,000 responses, for both capture policies. It does not execute the complete
100,000-judgment workload or supply ordinary throughput measurements.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import subprocess
import sys
import time
import tracemalloc
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from provenance import (
    fixture_fingerprint,
    harness_fingerprint,
    host_identity,
    input_fingerprint,
    runtime_identity,
    source_fingerprint,
)

MIB = 1024**2
DEADLINE = 180


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def document(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def identity(root: Path) -> dict[str, Any]:
    return {
        "source_root": str(root),
        "source_tree_sha256": source_fingerprint(root),
        "harness_sha256": harness_fingerprint(root / "benchmarks"),
        "fixture_definition_sha256": fixture_fingerprint(),
        "focused_tool_sha256": digest(Path(__file__)),
        "runtime_identity": runtime_identity(),
        "host_identity": host_identity(root),
    }


def initialization_sample(
    root: Path, configuration: Path, output: Path, responses: int, capture: str
) -> dict[str, Any]:
    """Trace real initialization in a fresh process without preparing any work."""
    import judgmentllm
    from judgmentllm.config import load_run_spec
    from judgmentllm.execution.session import ExecutionSession
    from judgmentllm.persistence.repository import ReadOnlyRepository
    from judgmentllm.preparation import prepare_run
    from judgmentllm.providers.mock import MockAdapter, MockScenario

    # Use the existing portable OS lifetime high-water measurement. Imports are
    # before Python tracing and are still included in native process RSS.
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts.preparation_memory_fixture import native_peak_bytes

    imported = Path(judgmentllm.__file__).resolve().parents[2]
    if imported != root:
        raise ValueError("Initialization imported a different production source")
    if output.exists() or output.is_relative_to(configuration.parent):
        raise ValueError(
            "Initialization requires fresh scratch outside the input directory"
        )
    output.mkdir(parents=True)
    original_inputs = input_fingerprint(configuration.parent)
    metadata = identity(root)
    store, ledger = output / "store", output / "service.sqlite3"
    phases: dict[str, dict[str, int | float]] = {}
    baselines: dict[str, dict[str, int]] = {}
    overall_peak = 0
    session: ExecutionSession | None = None

    @contextmanager
    def phase(name: str) -> Generator[None, None, None]:
        nonlocal overall_peak
        baseline, previous_peak = tracemalloc.get_traced_memory()
        overall_peak = max(overall_peak, previous_peak)
        tracemalloc.reset_peak()
        wall, cpu = time.perf_counter(), time.process_time()
        try:
            yield
        finally:
            live, peak = tracemalloc.get_traced_memory()
            overall_peak = max(overall_peak, peak)
            gc.collect()
            retained = tracemalloc.get_traced_memory()[0]
            native = native_peak_bytes()
            phases[name] = {
                "baseline_live_bytes": baseline,
                "peak_absolute_bytes": peak,
                "live_after_bytes": live,
                "retained_after_gc_bytes": retained,
                "wall_seconds": time.perf_counter() - wall,
                "cpu_seconds": time.process_time() - cpu,
            }
            baselines[name] = {
                "python_live_bytes": retained,
                "native_peak_bytes": native,
            }

    started = datetime.now(UTC).isoformat()
    tracemalloc.start()
    try:
        with phase("configuration"):
            spec = load_run_spec(configuration)
            if spec.capture != capture:
                raise ValueError("Initialization capture does not match frozen inputs")
            assert spec.task_kind == "rubric" and spec.rubric is not None
            assert len(spec.rubric.traits) == 20 and spec.execution.concurrency == 4
            assert spec.execution.max_traits_per_request == 5
        with phase("adapter"):
            with patch(
                "judgmentllm.providers.ledger.uuid.uuid4",
                return_value=uuid.UUID(int=42),
            ):
                adapter = MockAdapter(
                    ledger, scenario=MockScenario(category_index=1, probabilities=True)
                )
        with phase("preparation"):
            with prepare_run(
                spec, store=store, run_id="001", capabilities=adapter.capabilities(spec)
            ) as prepared:
                manifest_sha256 = prepared.manifest_sha256
        with phase("session_initialization"):
            session = ExecutionSession("001", store, adapter, spec, False, None)
        # Do not advance next_work, iterators, or provider submission. Reading
        # counts proves construction/reopening did not materialize the product.
        tables = (
            "tasks",
            "requests",
            "attempts",
            "submissions",
            "observations",
            "results",
            "request_tasks",
            "attempt_tasks",
        )
        counts = {
            name: int(
                session.repository.connection.execute(
                    f"SELECT COUNT(*) FROM {name} WHERE run_id='001'"
                ).fetchone()[0]
            )
            for name in tables
        }
        indexed = int(
            session.repository.connection.execute(
                "SELECT COUNT(*) FROM input_records WHERE run_id='001' AND record_kind='response'"
            ).fetchone()[0]
        )
        accepted = adapter.ledger.acceptance_count()
        assert indexed == responses and all(value == 0 for value in counts.values())
        assert accepted == adapter.submit_calls == 0
        with phase("session_close"):
            session.close()
            session = None
        overall_peak = max(overall_peak, tracemalloc.get_traced_memory()[1])
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0]
    finally:
        try:
            if session is not None:
                session.close()
        finally:
            tracemalloc.stop()
    with ReadOnlyRepository(store) as repository:
        assert all(
            repository.connection.execute(
                f"SELECT COUNT(*) FROM {name} WHERE run_id='001'"
            ).fetchone()[0]
            == 0
            for name in tables
        )
    assert input_fingerprint(configuration.parent) == original_inputs
    native = native_peak_bytes()
    assert 0 < native < 256 * MIB, "Initialization process exceeds 256 MiB"
    result = {
        "schema_version": 1,
        "status": "passed",
        "kind": "initialization_only",
        "process_id": os.getpid(),
        "responses": responses,
        "traits": 20,
        "group_size": 5,
        "concurrency": 4,
        "capture": capture,
        "configuration": str(configuration),
        "working_directory": str(output),
        "timing_started_at": started,
        "timing_finished_at": datetime.now(UTC).isoformat(),
        "phases": phases,
        "baselines": baselines,
        "whole_traced_peak_bytes": overall_peak,
        "python_retained_bytes": retained,
        "native_peak_bytes": native,
        "counts": counts,
        "indexed_responses": indexed,
        "accepted_physical_requests": accepted,
        "provider_submit_calls": adapter.submit_calls,
        "manifest_sha256": manifest_sha256,
        "input_sha256": original_inputs,
        **metadata,
        "measurement_note": "Python tracing starts before load_run_spec; imports precede tracing. Absolute phase peaks include initialized survivors. Lifetime native RSS includes imports, configuration, index and session construction. No next_work/provider submission is invoked.",
    }
    document(output / "report.json", result)
    return result


def validate_prefix(
    sample: dict[str, Any], responses: int, capture: str, completed_requests: int = 64
) -> None:
    """Keep the established fixed-prefix resource and scheduler bounds."""
    completed = min(completed_requests, responses * 4)
    assert sample["status"] == "passed" and sample["verification_complete"] is True
    assert sample["capture"] == capture and sample["extent"] == "prefix"
    assert (
        sample["mode"] == "tracemalloc" and sample["measurement_scope"] == "execution"
    )
    assert sample["logical_task_count"] == responses * 20
    assert sample["planned_physical_requests"] == responses * 4
    assert sample["completed_physical_requests"] == completed
    assert sample["result_rows"] == completed * 5
    assert sample["concurrency"] == 4 and sample["execution_asyncio_tasks"] == 6
    assert sample["peak_asyncio_tasks"] <= 11
    assert sample["created_asyncio_tasks"] < completed + 19
    assert 0 < sample["request_queue_high_water"] <= 8
    assert 0 < sample["result_queue_high_water"] <= 4
    assert completed <= sample["prepared_requests"] <= completed + 19
    assert sample["prepared_physical_requests_observed"] == sample["prepared_requests"]
    assert sample["maximum_prepared_minus_completed"] <= 19
    assert (
        completed <= sample["accepted_physical_requests"] <= sample["prepared_requests"]
    )
    assert sample["prepared_requests"] < sample["logical_task_count"]
    assert sample["verification"]["counts"]["tasks"] == 5 * sample["prepared_requests"]
    assert 0 < sample["native_peak_bytes"] < 256 * MIB
    assert 0 < sample["python_peak_bytes"] < 32 * MIB
    assert sample["python_retained_bytes"] < sample["python_peak_bytes"]
    assert 0 < sample["export_python_peak_bytes"] < 8 * MIB


def compare_prefixes(small: dict[str, Any], large: dict[str, Any]) -> dict[str, Any]:
    growth = {
        "python_peak_bytes": large["python_peak_bytes"] - small["python_peak_bytes"],
        "python_retained_bytes": large["python_retained_bytes"]
        - small["python_retained_bytes"],
        "native_peak_bytes": large["native_peak_bytes"] - small["native_peak_bytes"],
    }
    eligible = (
        small["completed_physical_requests"]
        == large["completed_physical_requests"]
        == 64
    )
    if eligible:
        assert growth["python_peak_bytes"] < 3 * MIB
        assert growth["python_retained_bytes"] < MIB
        assert growth["native_peak_bytes"] < 48 * MIB
    return {
        "eligible": eligible,
        "same_64_request_prefix": eligible,
        "growth_bytes": growth,
        "limits_bytes": {
            "python_peak_bytes": 3 * MIB,
            "python_retained_bytes": MIB,
            "native_peak_bytes": 48 * MIB,
        },
        "note": None
        if eligible
        else "Tiny test dimensions do not establish the required same-64-prefix comparison",
    }


def run_command(
    arguments: list[str],
    environment: dict[str, str],
    log: Path,
    *,
    timeout: int = DEADLINE,
) -> None:
    started = time.perf_counter()
    try:
        process = subprocess.run(
            arguments, env=environment, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        document(
            log,
            {
                "arguments": arguments,
                "exit_status": None,
                "timeout_seconds": timeout,
                "stdout": str(error.stdout or ""),
                "stderr": str(error.stderr or ""),
            },
        )
        raise
    document(
        log,
        {
            "arguments": arguments,
            "exit_status": process.returncode,
            "timeout_seconds": timeout,
            "wall_seconds": time.perf_counter() - started,
            "stdout": process.stdout,
            "stderr": process.stderr,
        },
    )
    if process.returncode or process.stderr:
        raise RuntimeError(f"Focused diagnostic child failed; see {log}")


def collect(
    root: Path,
    output: Path,
    small_responses: int,
    large_responses: int,
    completed_requests: int = 64,
) -> dict[str, Any]:
    if output.exists() or output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("Choose a new external output directory outside source")
    if digest(root / "benchmarks/focused_execution.py") != digest(Path(__file__)):
        raise ValueError("Focused tool differs from selected source-root")
    output.mkdir(parents=True)
    environment = {
        **os.environ,
        "PYTHONPATH": str(root / "src"),
        "PYTHONHASHSEED": "42",
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "failed",
        "qualified": False,
        "kind": "focused_execution_prefix_and_initialization",
        **identity(root),
        "small_responses": small_responses,
        "large_responses": large_responses,
        "completed_request_target": completed_requests,
        "qualification_scope": "default bounded-prefix qualification"
        if (small_responses, large_responses, completed_requests) == (200, 5000, 64)
        else "test-only",
        "deadline_seconds": DEADLINE,
        "prefix_controller_timeout_seconds": DEADLINE + 30,
        "child_environment": {"PYTHONHASHSEED": environment["PYTHONHASHSEED"]},
        "cases": [],
        "growth": [],
        "note": "Bounded prefix diagnostics, not complete-run timing or full-workload allocation tracing. Initialization uses separate fresh stores and includes overall native costs.",
    }
    try:
        for capture in ("minimal", "full"):
            prefixes: list[dict[str, Any]] = []
            for responses in (small_responses, large_responses):
                case = output / f"{capture}-{responses}"
                case.mkdir()
                prefix = case / "prefix"
                arguments = [
                    sys.executable,
                    str(root / "benchmarks/execution_benchmark.py"),
                    "--source-root",
                    str(root),
                    "--output",
                    str(prefix),
                    "--work-root",
                    str(case / "prefix-work"),
                    "--workloads",
                    "scale-rubric",
                    "--responses",
                    str(responses),
                    "--traits",
                    "20",
                    "--group-size",
                    "5",
                    "--concurrency",
                    "4",
                    "--response-bytes",
                    "4096",
                    "--fixture-variant",
                    "default",
                    "--captures",
                    capture,
                    "--extent",
                    "prefix",
                    "--completed-requests",
                    str(completed_requests),
                    "--modes",
                    "tracemalloc",
                    "--measurement-scope",
                    "execution",
                    "--warmups",
                    "0",
                    "--repetitions",
                    "1",
                    "--instrumented-repetitions",
                    "1",
                    "--timeout",
                    str(DEADLINE),
                ]
                # Let the existing 180-second sample watchdog finish and retain
                # its failure/cleanup evidence before the outer controller dies.
                run_command(
                    arguments,
                    environment,
                    case / "prefix-command.json",
                    timeout=DEADLINE + 30,
                )
                raw = (
                    prefix
                    / "raw"
                    / f"scale-rubric-{capture}-tracemalloc-measured-0.json"
                )
                sample = json.loads(raw.read_text(encoding="utf-8"))
                validate_prefix(sample, responses, capture, completed_requests)
                for key in (
                    "source_tree_sha256",
                    "harness_sha256",
                    "fixture_definition_sha256",
                    "runtime_identity",
                ):
                    assert sample[key] == report[key], f"Prefix identity differs: {key}"
                configuration = Path(sample["working_directory"]) / "inputs/run.json"
                initialization = case / "initialization"
                run_command(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--source-root",
                        str(root),
                        "--output",
                        str(initialization),
                        "--initialization-child",
                        "--configuration",
                        str(configuration),
                        "--responses",
                        str(responses),
                        "--capture",
                        capture,
                    ],
                    environment,
                    case / "initialization-command.json",
                )
                init_path = initialization / "report.json"
                initialized = json.loads(init_path.read_text(encoding="utf-8"))
                assert (
                    initialized["status"] == "passed"
                    and initialized["input_sha256"] == sample["input_sha256"]
                )
                for key in (
                    "source_tree_sha256",
                    "harness_sha256",
                    "fixture_definition_sha256",
                    "runtime_identity",
                ):
                    assert initialized[key] == report[key], (
                        f"Initialization identity differs: {key}"
                    )
                report["cases"].append(
                    {
                        "capture": capture,
                        "responses": responses,
                        "completed_physical_requests": sample[
                            "completed_physical_requests"
                        ],
                        "prefix_report_path": str(raw),
                        "prefix_report_sha256": digest(raw),
                        "initialization_report_path": str(init_path),
                        "initialization_report_sha256": digest(init_path),
                    }
                )
                prefixes.append(sample)
                document(output / "report.json", report)
            report["growth"].append(
                {"capture": capture, **compare_prefixes(prefixes[0], prefixes[1])}
            )
        report["status"] = "passed"
        report["qualified"] = (
            small_responses,
            large_responses,
            completed_requests,
        ) == (200, 5000, 64) and all(row["eligible"] for row in report["growth"])
    except Exception as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        document(output / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--small-responses", type=int, default=200)
    parser.add_argument("--large-responses", type=int, default=5000)
    parser.add_argument("--completed-requests", type=int, default=64)
    parser.add_argument(
        "--initialization-child", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument("--configuration", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--responses", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--capture", choices=("minimal", "full"), help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    root, output = args.source_root.resolve(), args.output.resolve()
    if not (root / "src/judgmentllm").is_dir():
        parser.error("source-root must contain src/judgmentllm")
    if args.initialization_child:
        if args.configuration is None or args.responses is None or args.capture is None:
            parser.error("Initialization requires configuration, responses and capture")
        initialization_sample(
            root, args.configuration.resolve(), output, args.responses, args.capture
        )
        print(json.dumps({"status": "passed", "report": str(output / "report.json")}))
    else:
        if (
            not 0 < args.small_responses < args.large_responses
            or args.completed_requests < 1
        ):
            parser.error(
                "response counts must be positive/increasing and completion target positive"
            )
        result = collect(
            root,
            output,
            args.small_responses,
            args.large_responses,
            args.completed_requests,
        )
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "qualified": result["qualified"],
                    "report": str(output / "report.json"),
                }
            )
        )


if __name__ == "__main__":
    main()
