"""Measure offline preparation in a fresh process using Python and native peaks.

The small required cases are source/index regressions, not qualification of the
later executor's 5,000-response by 20-trait or 100,000-response stress targets.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
import sys
import time
import tracemalloc
from itertools import islice
from pathlib import Path
from typing import Any, cast

from judgmentllm.preparation import prepare_run

_linux_process_peak: tuple[int, int] | None = None


def native_peak_bytes() -> int:
    """Measure the fresh workload process, including non-Python allocations.

    Linux getrusage can retain the RSS high-water mark from the forked parent's
    address space across exec. /proc/self/status reports the new address space's
    VmHWM instead. Never replace a missing/malformed measurement with a fabricated
    zero or the inherited counter. All existing acceptance bounds stay unchanged.
    """
    if sys.platform == "linux":
        global _linux_process_peak
        with Path("/proc/self/status").open("r", encoding="ascii") as stream:
            for line in stream:
                if line.startswith("VmHWM:"):
                    parts = line.split()
                    if len(parts) == 3 and parts[2] == "kB" and parts[1].isdecimal():
                        peak_kib = int(parts[1])
                        if peak_kib > 0:
                            process_id = os.getpid()
                            measured = peak_kib * 1024
                            # Kernel RSS accounting is asynchronous on SMP. Keep
                            # the largest observed high-water mark for this PID;
                            # forked children never inherit a Python-side peak.
                            if (
                                _linux_process_peak is not None
                                and _linux_process_peak[0] == process_id
                            ):
                                measured = max(measured, _linux_process_peak[1])
                            _linux_process_peak = process_id, measured
                            return measured
                    break
        raise OSError("native process memory measurement failed")

    if sys.platform != "win32":
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)

    from ctypes import wintypes

    class MemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]
        cb: int
        PeakWorkingSetSize: int

    # These standard-library loaders are intentionally available only on Windows.
    loader = cast(Any, ctypes).WinDLL
    kernel = loader("kernel32", use_last_error=True)
    process_api = loader("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    process_api.GetProcessMemoryInfo.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(MemoryCounters),
        wintypes.DWORD,
    )
    process_api.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not process_api.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise OSError("native process memory measurement failed")
    return counters.PeakWorkingSetSize


def measure(
    directory: Path, response_count: int, trait_count: int, *, diagnostics: bool = False
) -> dict[str, int | float]:
    directory.mkdir(parents=True)
    source = directory / "responses.jsonl"
    with source.open("w", encoding="utf-8", newline="") as stream:
        for ordinal in range(response_count):
            stream.write(
                json.dumps({"item_id": f"{ordinal:06d}", "text": "x" * 4096}) + "\n"
            )
    configuration = directory / "run.json"
    configuration.write_text(
        json.dumps(
            {
                "task_kind": "rubric",
                "input": source.name,
                "capture": "minimal",
                "provider": "synthetic",
                "endpoint": "offline",
                "model": "fixture",
                "rubric": {
                    "rubric_id": "001",
                    "name": "Synthetic memory fixture",
                    "traits": [
                        {
                            "trait_id": f"{index:03d}",
                            "name": "Quality",
                            "description": "Assess this synthetic response.",
                            "levels": [{"label": "low"}, {"label": "high"}],
                        }
                        for index in range(trait_count)
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    started = time.monotonic()
    tracemalloc.start()
    try:
        baseline = tracemalloc.take_snapshot() if diagnostics else None
        with prepare_run(
            configuration, store=directory / "runtime", run_id="001"
        ) as prepared:
            assert prepared.get_response(f"{response_count - 1:06d}").text == "x" * 4096
            assert prepared.get_response("000000").item_id == "000000"
            sample = list(islice(prepared.iter_request_dependencies(), 64))
            assert len(sample) == min(64, response_count * trait_count)
            assert len(
                {dependency.request.request_uid for dependency in sample}
            ) == len(sample)
            assert all(len(dependency.request.tasks) == 1 for dependency in sample)
            for table in ("tasks", "requests", "attempts", "results"):
                assert (
                    prepared.repository.connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE run_id=?", ("001",)
                    ).fetchone()[0]
                    == 0
                )
            python_peak = tracemalloc.get_traced_memory()[1]
            python_live = tracemalloc.get_traced_memory()[0]
            sample_count = len(sample)
        native_peak = native_peak_bytes()
        after_close = tracemalloc.get_traced_memory()[0]
        if baseline is not None:
            snapshot = tracemalloc.take_snapshot()
            allocations = [
                {
                    "filename": entry.traceback[0].filename,
                    "line": entry.traceback[0].lineno,
                    "bytes": entry.size_diff,
                    "count": entry.count_diff,
                }
                for entry in snapshot.compare_to(baseline, "lineno")[:20]
            ]
            (directory / "allocation-diagnostics.json").write_text(
                json.dumps(allocations), encoding="utf-8"
            )
            del snapshot, baseline
        # Report retained allocations separately. Collection happens after peak
        # measurement and never hides allocations from the required peak bounds.
        del sample, prepared
        gc.collect()
        after_gc = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    return {
        "response_count": response_count,
        "trait_count": trait_count,
        "logical_task_count": response_count * trait_count,
        "sample_count": sample_count,
        "source_byte_length": source.stat().st_size,
        "python_peak_bytes": python_peak,
        "python_live_bytes": python_live,
        "python_after_close_bytes": after_close,
        "python_after_gc_bytes": after_gc,
        "native_peak_bytes": native_peak,
        "elapsed_seconds": time.monotonic() - started,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--responses", type=int, required=True)
    parser.add_argument("--traits", type=int, default=1)
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Write top live allocation locations.",
    )
    options = parser.parse_args()
    if options.responses < 1 or options.traits < 1:
        parser.error("response and trait counts must be positive")
    result = measure(
        options.directory,
        options.responses,
        options.traits,
        diagnostics=options.diagnostics,
    )
    encoded = json.dumps(result)
    (options.directory / "measurement.json").write_text(encoded, encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
