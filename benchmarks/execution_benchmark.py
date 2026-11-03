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
            values = [float(row[field]) for row in rows if row[field] is not None]
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
        choices=[item.name for item in WORKLOADS],
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
    root = options.source_root.resolve()
    if not (root / "src" / "judgmentllm").is_dir():
        parser.error("source-root must contain src/judgmentllm")
    output = options.output.resolve()
    work_root = (options.work_root or output / "work").resolve()
    raw = output / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    worker = Path(__file__).with_name("execution_worker.py").resolve()
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
        "initial_state": "fresh process; unchanged source files/paths; SQLite backup restores pristine empty registry and empty ledger with fixed synthetic service identity42 before timing",
        "filesystem_cache": "not flushed; same filesystem/path; one plain warm-up per case/capture; OS cache state uncontrolled",
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
                    if options.resume and destination.exists():
                        previous = json.loads(destination.read_text(encoding="utf-8"))
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
                    result = subprocess.run(
                        [
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
                        ],
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=options.timeout,
                    )
                    if result.returncode or result.stderr:
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
