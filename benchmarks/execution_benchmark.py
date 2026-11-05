"""Run serial, fresh-process performance samples against a specified checkout.

Example (run baseline before editing production code)::

    python benchmarks/execution_benchmark.py --source-root /tmp/baseline \
        --output /tmp/performance/baseline --modes plain

Each child restores the same empty-store/empty-ledger/source starting state.
OS filesystem caches are intentionally not flushed; warm-up and measured runs
use the same paths on the same local filesystem. This is a warm-cache benchmark,
not a claim about cold disks or hosted runner hardware.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from provenance import (
    fixture_fingerprint,
    harness_fingerprint,
    runtime_identity,
    source_fingerprint,
)
from workloads import WORKLOADS


def distribution(values: list[float]) -> dict[str, float]:
    median = statistics.median(values)
    return {
        "median": median,
        "min": min(values),
        "max": max(values),
        "median_absolute_deviation": statistics.median(
            abs(value - median) for value in values
        ),
    }


def summarize(raw: Path) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    identities: set[str] = set()
    for path in sorted(raw.glob("*.json")):
        if "warmup" in path.name:
            continue
        row: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if row.get("status") == "failed":
            raise RuntimeError(f"Cannot summarize a failed sample: {path}")
        if row.get("workload") == "scale-rubric" and (
            row.get("status") != "passed"
            or (row.get("extent") != "prepare" and not row.get("verification_complete"))
        ):
            raise RuntimeError(
                f"Scale sample lacks completed independent verification: {path}"
            )
        identity = row.get("source_tree_sha256")
        if not isinstance(identity, str):
            raise RuntimeError(f"Sample lacks source identity: {path}")
        identities.add(identity)
        groups.setdefault(
            f"{row['workload']}/{row['capture']}/{row['mode']}", []
        ).append(row)
    if len(identities) > 1:
        raise RuntimeError("Cannot summarize mixed source revisions")
    summaries: dict[str, Any] = {}
    for name, rows in groups.items():
        measured = {}
        for field in (
            "wall_seconds",
            "cpu_seconds",
            "cpu_ms_per_completed_physical_request",
            "python_peak_bytes",
            "python_live_bytes",
            "python_retained_bytes",
            "native_peak_bytes",
        ):
            values = [float(row[field]) for row in rows if row.get(field) is not None]
            measured[field] = distribution(values) if values else None
        phases = {
            name: {
                clock: distribution(
                    [
                        row["phases"][name][clock] / 1e9
                        for row in rows
                        if name in row["phases"]
                    ]
                )
                for clock in ("wall_ns", "exclusive_thread_cpu_ns")
                if clock in first
            }
            for name, first in rows[0]["phases"].items()
        }
        summaries[name] = {
            "samples": len(rows),
            "measurements": measured,
            "phases": phases,
            "completed_physical_requests": rows[0]["completed_physical_requests"],
            "prefix_only": rows[0]["prefix_only"],
            "counters": {
                key: distribution([row["counters"].get(key, 0) for row in rows])
                for key in rows[0]["counters"]
            },
            "call_counts": rows[0].get("call_counts"),
            "concurrency": {
                key: distribution(
                    [float(row[key]) for row in rows if row[key] is not None]
                )
                for key in (
                    "created_asyncio_tasks",
                    "execution_asyncio_tasks",
                    "peak_asyncio_tasks",
                    "request_queue_high_water",
                    "result_queue_high_water",
                )
                if rows[0][key] is not None
            },
        }
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--work-root",
        type=Path,
        help="Shared restored fixture root; defaults to OUTPUT/work",
    )
    parser.add_argument(
        "--workloads",
        nargs="+",
        choices=[item.name for item in WORKLOADS] + ["scale-rubric"],
        default=[item.name for item in WORKLOADS],
    )
    parser.add_argument(
        "--captures",
        nargs="+",
        choices=["full", "minimal"],
        default=["full", "minimal"],
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=["plain", "profile", "tracemalloc", "coverage", "combined"],
        default=["plain", "profile", "tracemalloc", "coverage", "combined"],
    )
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--instrumented-repetitions", type=int, default=1)
    parser.add_argument("--warmups", type=int, default=1)
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
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Per full benchmark workload; existing resource-test timeout is unchanged",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse completed JSON samples; each remaining child still starts empty",
    )
    options = parser.parse_args()
    if (
        min(options.repetitions, options.instrumented_repetitions, options.timeout) < 1
        or options.warmups < 0
    ):
        parser.error("repetitions and timeout must be positive; warmups nonnegative")
    if (
        min(
            options.responses,
            options.traits,
            options.group_size,
            options.concurrency,
            options.response_bytes,
            options.completed_requests,
            options.disk_budget_gib,
        )
        <= 0
        or options.min_free_gib < 0
    ):
        parser.error(
            "scale dimensions and disk budget must be positive; free reserve nonnegative"
        )
    if "scale-rubric" in options.workloads and any(
        mode in {"coverage", "combined"} for mode in options.modes
    ):
        parser.error(
            "scale qualification separates plain/profile/tracemalloc; coverage uses the required test suite"
        )
    root = options.source_root.resolve()
    if not (root / "src" / "judgmentllm").is_dir():
        parser.error("source-root must contain src/judgmentllm")
    output = options.output.resolve()
    work_root = (options.work_root or output / "work").resolve()
    raw = output / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    worker = Path(__file__).with_name("execution_worker.py").resolve()

    def failure_destination(name: str) -> Path:
        folder = output / "failures"
        folder.mkdir(exist_ok=True)
        return folder / f"{Path(name).stem}-{time.time_ns()}.json"

    environment = {
        **os.environ,
        "PYTHONPATH": str(root / "src"),
        "PYTHONHASHSEED": "42",
    }
    source_identity = source_fingerprint(root)
    environment_identity = runtime_identity()
    tool_identity, fixture_identity = harness_fingerprint(), fixture_fingerprint()
    metadata_path = output / "methodology.json"
    if metadata_path.exists():
        previous = json.loads(metadata_path.read_text(encoding="utf-8"))
        if (previous.get("source_tree_sha256"), previous.get("runtime_identity")) != (
            source_identity,
            environment_identity,
        ):
            raise RuntimeError(
                "Output belongs to different source/environment (or lacks identity); choose a fresh output directory"
            )
        if "scale-rubric" in options.workloads:
            keys = (
                "responses",
                "traits",
                "group_size",
                "concurrency",
                "response_bytes",
                "fixture_variant",
                "extent",
                "completed_requests",
                "measurement_scope",
                "disk_budget_gib",
                "min_free_gib",
            )
            if any(
                previous["options"].get(key) != getattr(options, key) for key in keys
            ):
                raise RuntimeError(
                    "Scale configuration changed; choose a fresh output directory"
                )
        expected_tool = previous.get("harness_sha256") or previous.get(
            "compatible_harness_sha256"
        )
        if previous.get("work_root", str(output / "work")) != str(work_root):
            raise RuntimeError(
                "Working fixture root changed; choose a fresh output directory"
            )
        expected_fixture = previous.get("fixture_definition_sha256") or previous.get(
            "compatible_fixture_definition_sha256"
        )
        if (expected_tool, expected_fixture) != (tool_identity, fixture_identity):
            raise RuntimeError(
                "Harness or fixture definition changed; choose a fresh output directory"
            )
    metadata: dict[str, Any] = {
        "source_root": str(root),
        "work_root": str(work_root),
        "source_tree_sha256": source_identity,
        "runtime_identity": environment_identity,
        "harness_sha256": tool_identity,
        "fixture_definition_sha256": fixture_identity,
        "python": sys.executable,
        "started_at": datetime.now(UTC).isoformat(),
        "options": vars(options).copy(),
        "initial_state": "fresh process; unchanged source files/paths; empty registry and ledger with fixed synthetic service identity42 recreated before timing"
        if "scale-rubric" in options.workloads
        else "fresh process; unchanged source files/paths; SQLite backup restores pristine empty registry and empty ledger with fixed synthetic service identity42 before timing",
        "filesystem_cache": "not flushed; same filesystem/path; configured fresh-process plain warm-ups; OS cache state uncontrolled",
        "phase_accounting": "phase wall durations inclusive/overlapping; synchronous thread CPU exclusive within each thread; async submission/retrieval report latency without CPU attribution",
    }
    metadata["options"] = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in metadata["options"].items()
    }
    if not metadata_path.exists():
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    # Preserve original collection options/start time; later diagnostics append
    # their actual invocation rather than rewriting the ordinary methodology.
    with (output / "invocations.jsonl").open(
        "a", encoding="utf-8", newline=""
    ) as stream:
        stream.write(json.dumps(metadata) + "\n")
    for mode in options.modes:
        for capture in options.captures:
            for workload in options.workloads:
                repetitions = (
                    options.repetitions
                    if mode == "plain"
                    else options.instrumented_repetitions
                )
                samples = [
                    ("warmup", index)
                    for index in range(options.warmups if mode == "plain" else 0)
                ] + [("measured", index) for index in range(repetitions)]
                for kind, index in samples:
                    destination = (
                        raw / f"{workload}-{capture}-{mode}-{kind}-{index}.json"
                    )
                    if destination.exists() and not options.resume:
                        raise RuntimeError(
                            f"Sample already exists at {destination}; use --resume or a fresh output directory"
                        )
                    if options.resume and destination.exists():
                        previous = json.loads(destination.read_text(encoding="utf-8"))
                        if previous.get("status") == "failed":
                            raise RuntimeError(
                                f"Failed sample retained at {destination}; choose a fresh output directory"
                            )
                        if workload == "scale-rubric" and (
                            previous.get("status") != "passed"
                            or (
                                previous.get("extent") != "prepare"
                                and not previous.get("verification_complete")
                            )
                        ):
                            raise RuntimeError(
                                f"Scale sample is not independently verified at {destination}; choose a fresh output directory"
                            )
                        if (
                            previous.get("source_tree_sha256"),
                            previous.get("runtime_identity"),
                        ) != (source_identity, environment_identity):
                            raise RuntimeError(
                                f"Sample identity conflicts with this run: {destination}"
                            )
                        compatible_legacy = (
                            previous.get("mode") == "plain"
                            and previous.get("legacy_ordinary_hook_compatibility")
                            is True
                        )
                        sample_tool = previous.get("harness_sha256") or (
                            previous.get("compatible_harness_sha256")
                            if compatible_legacy
                            else None
                        )
                        sample_fixture = previous.get("fixture_definition_sha256") or (
                            previous.get("compatible_fixture_definition_sha256")
                            if compatible_legacy
                            else None
                        )
                        if (sample_tool, sample_fixture) != (
                            tool_identity,
                            fixture_identity,
                        ):
                            raise RuntimeError(
                                f"Sample harness/fixture conflicts with this run: {destination}"
                            )
                        continue
                    pause = output / "PAUSE"
                    paused = output / "PAUSED"
                    if pause.exists():
                        paused.write_text(
                            json.dumps(
                                {
                                    "pid": os.getpid(),
                                    "paused_at": datetime.now(UTC).isoformat(),
                                }
                            ),
                            encoding="utf-8",
                        )
                        print(
                            json.dumps({"event": "paused", "marker": str(paused)}),
                            flush=True,
                        )
                        try:
                            while pause.exists():
                                time.sleep(0.25)
                        finally:
                            paused.unlink(missing_ok=True)
                    print(
                        json.dumps(
                            {
                                "event": "start",
                                "workload": workload,
                                "capture": capture,
                                "mode": mode,
                                "sample": kind,
                                "index": index,
                            }
                        ),
                        flush=True,
                    )
                    arguments = [
                        sys.executable,
                        str(worker),
                        "--directory",
                        str(work_root / f"{workload}-{capture}"),
                        "--output",
                        str(destination),
                        "--workload",
                        workload,
                        "--capture",
                        capture,
                        "--mode",
                        mode,
                        "--expected-source-sha256",
                        source_identity,
                        "--expected-harness-sha256",
                        tool_identity,
                        "--expected-fixture-sha256",
                        fixture_identity,
                    ]
                    if workload == "scale-rubric":
                        for flag in (
                            "responses",
                            "traits",
                            "group_size",
                            "concurrency",
                            "response_bytes",
                            "fixture_variant",
                            "extent",
                            "completed_requests",
                            "measurement_scope",
                            "disk_budget_gib",
                            "min_free_gib",
                        ):
                            arguments.extend(
                                [
                                    "--" + flag.replace("_", "-"),
                                    str(getattr(options, flag)),
                                ]
                            )
                    attempt_started = datetime.now(UTC).isoformat()
                    attempt_clock = time.monotonic()
                    try:
                        result = subprocess.run(
                            arguments,
                            env=environment,
                            capture_output=True,
                            text=True,
                            timeout=options.timeout,
                        )
                        if (
                            workload == "scale-rubric"
                            and result.returncode == 0
                            and not result.stderr
                            and options.extent != "prepare"
                        ):
                            sample = json.loads(destination.read_text(encoding="utf-8"))
                            remaining = options.timeout - (
                                time.monotonic() - attempt_clock
                            )
                            if remaining <= 0:
                                raise subprocess.TimeoutExpired(
                                    arguments, options.timeout
                                )
                            verify_start = time.monotonic()
                            verified = subprocess.run(
                                [
                                    sys.executable,
                                    str(worker.with_name("scale_verify.py")),
                                    "--report",
                                    str(destination),
                                ],
                                env=environment,
                                capture_output=True,
                                text=True,
                                timeout=remaining,
                            )
                            if verified.returncode or verified.stderr:
                                sample["status"] = "failed"
                                sample["failure"] = {
                                    "type": "IndependentVerificationError",
                                    "message": verified.stderr,
                                }
                                result = verified
                            else:
                                sample["verification"] = json.loads(verified.stdout)
                                sample["verification_complete"] = True
                                sample["status"] = "passed"
                                sample["verification_wall_seconds"] = (
                                    time.monotonic() - verify_start
                                )
                                sample["qualification_total_wall_seconds"] = (
                                    sample["wall_seconds"]
                                    + sample["verification_wall_seconds"]
                                )
                            destination.write_text(
                                json.dumps(sample, indent=2), encoding="utf-8"
                            )
                    except subprocess.TimeoutExpired as error:
                        progress = work_root / f"{workload}-{capture}" / "progress.json"
                        if destination.exists():
                            partial = json.loads(
                                destination.read_text(encoding="utf-8")
                            )
                            partial["status"] = "failed"
                            partial["failure"] = {
                                "type": "WatchdogTimeout",
                                "message": "worker or independent verification exceeded the configured deadline",
                            }
                            destination.write_text(
                                json.dumps(partial, indent=2), encoding="utf-8"
                            )
                        failed = {
                            "status": "failed",
                            "returncode": None,
                            "reason": "watchdog_timeout",
                            "timeout_seconds": options.timeout,
                            "started_at": attempt_started,
                            "finished_at": datetime.now(UTC).isoformat(),
                            "stdout": str(error.stdout or ""),
                            "stderr": str(error.stderr or ""),
                            "arguments": arguments,
                            "source_tree_sha256": source_identity,
                            "harness_sha256": tool_identity,
                            "fixture_definition_sha256": fixture_identity,
                            "partial_progress": json.loads(progress.read_text())
                            if progress.exists()
                            else None,
                            "partial_report": json.loads(destination.read_text())
                            if destination.exists()
                            else None,
                        }
                        failure_path = failure_destination(destination.name)
                        failure_path.write_text(
                            json.dumps(failed, indent=2), encoding="utf-8"
                        )
                        raise RuntimeError(
                            f"Benchmark watchdog timeout; retained evidence: {failure_path}"
                        ) from error
                    if result.returncode or result.stderr:
                        failure_path = failure_destination(destination.name)
                        failed = {
                            "status": "failed",
                            "returncode": result.returncode,
                            "started_at": attempt_started,
                            "finished_at": datetime.now(UTC).isoformat(),
                            "stdout": result.stdout,
                            "stderr": result.stderr,
                            "arguments": arguments,
                            "source_tree_sha256": source_identity,
                            "harness_sha256": tool_identity,
                            "fixture_definition_sha256": fixture_identity,
                            "partial_report": json.loads(destination.read_text())
                            if destination.exists()
                            else None,
                        }
                        failure_path.write_text(
                            json.dumps(failed, indent=2), encoding="utf-8"
                        )
                        raise RuntimeError(
                            f"Benchmark child failed ({result.returncode}): {result.stdout}\n{result.stderr}"
                        )
                    print(result.stdout.strip(), flush=True)
                    (output / "summary.json").write_text(
                        json.dumps(summarize(raw), indent=2), encoding="utf-8"
                    )
    print(
        json.dumps({"event": "complete", "summary": str(output / "summary.json")}),
        flush=True,
    )


if __name__ == "__main__":
    main()
