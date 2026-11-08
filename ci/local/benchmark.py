"""Retain an exact-source grid experiment and workstation-wide utilization."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil
from scheduling import SCHEDULES


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--schedule", choices=tuple(SCHEDULES), required=True)
    parser.add_argument(
        "--stage", choices=("full", "resource", "ordinary"), default="full"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--platform", choices=("all", "linux", "windows", "macos"), default="all"
    )
    parser.add_argument("--benchmark-workers", default="")
    parser.add_argument(
        "--distribution", choices=("loadfile", "worksteal"), default="loadfile"
    )
    options = parser.parse_args()
    source = Path(__file__).resolve().parents[2]
    output = options.output.resolve()
    if output.is_relative_to(source):
        parser.error("benchmark evidence must be outside the worktree")
    output.mkdir(parents=True, exist_ok=False)
    grid_root = Path("/mnt/VM_Fast/judgmentllm-ci/benchmarks") / output.name
    command = [
        sys.executable,
        str(source / "ci/local/grid.py"),
        "--ref",
        options.ref,
        "--mode",
        "check",
        "--schedule",
        options.schedule,
        "--suite-stage",
        options.stage,
        "--platform",
        options.platform,
        "--result-root",
        str(grid_root),
        "--distribution",
        options.distribution,
    ]
    if options.benchmark_workers:
        command.extend(("--benchmark-workers", options.benchmark_workers))
    record: dict[str, Any] = {
        "schema_version": 1,
        "source_commit": options.ref,
        "schedule": options.schedule,
        "stage": options.stage,
        "command": command,
        "grid_root": str(grid_root),
        "physical_cpus": psutil.cpu_count(logical=False),
        "logical_cpus": psutil.cpu_count(),
        "memory_total_bytes": psutil.virtual_memory().total,
        "measurement_scope": "whole workstation, including VMs/containers and unrelated host activity; sampled peaks are lower bounds",
        "pid": os.getpid(),
    }
    (output / "experiment.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    stop = threading.Event()
    samples: list[dict[str, int | float]] = []
    started = time.monotonic()
    cpu_start = psutil.cpu_times()
    disk_start = psutil.disk_io_counters()

    def sample() -> None:
        psutil.cpu_percent(interval=None)
        with (output / "host-samples.jsonl").open("x", encoding="utf-8") as stream:
            while not stop.wait(0.5):
                memory = psutil.virtual_memory()
                swap = psutil.swap_memory()
                value = {
                    "elapsed_seconds": time.monotonic() - started,
                    "cpu_percent": psutil.cpu_percent(interval=None),
                    "memory_used_bytes": memory.total - memory.available,
                    "memory_available_bytes": memory.available,
                    "swap_used_bytes": swap.used,
                }
                samples.append(value)
                stream.write(json.dumps(value) + "\n")
                stream.flush()

    thread = threading.Thread(target=sample, name="ci-host-sampler", daemon=True)
    thread.start()
    try:
        with (output / "grid.log").open("x", encoding="utf-8") as stream:
            result = subprocess.run(
                command,
                cwd=source,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            )
        record["exit_code"] = result.returncode
    finally:
        stop.set()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("Host sampler failed to stop")
        cpu_end = psutil.cpu_times()
        total_cpu = sum(cpu_end) - sum(cpu_start)
        idle_cpu = (
            cpu_end.idle
            - cpu_start.idle
            + getattr(cpu_end, "iowait", 0)
            - getattr(cpu_start, "iowait", 0)
        )
        disk_end = psutil.disk_io_counters()
        record.update(
            wall_seconds=time.monotonic() - started,
            host_busy_cpu_seconds=total_cpu - idle_cpu,
            host_cpu_percent=100 * (total_cpu - idle_cpu) / total_cpu
            if total_cpu
            else 0,
            peak_host_used_bytes=max(
                (s["memory_used_bytes"] for s in samples), default=0
            ),
            peak_swap_used_bytes=max(
                (s["swap_used_bytes"] for s in samples), default=0
            ),
            samples=len(samples),
        )
        if disk_start is not None and disk_end is not None:
            record["host_disk_read_bytes"] = disk_end.read_bytes - disk_start.read_bytes
            record["host_disk_write_bytes"] = (
                disk_end.write_bytes - disk_start.write_bytes
            )
        summaries = sorted(grid_root.glob("*/summary.json"))
        record["grid_summaries"] = [str(path) for path in summaries]
        record["qualification"] = bool(
            record.get("exit_code") == 0
            and options.stage == "full"
            and options.platform == "all"
            and len(summaries) == 1
            and json.loads(summaries[0].read_text())["qualification"]
        )
        (output / "result.json").write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(record, indent=2), flush=True)
    return int(record["exit_code"] != 0)


if __name__ == "__main__":
    raise SystemExit(main())
