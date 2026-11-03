"""Measure offline preparation in a fresh process using Python and native peaks.

The small required cases are source/index regressions, not qualification of the
later executor's 5,000-response by 20-trait or 100,000-response stress targets.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
import tracemalloc
from itertools import islice
from pathlib import Path
from typing import Any, cast

from judgmentllm.preparation import prepare_run


def native_peak_bytes() -> int:
    """Use the OS process high-water mark, including non-Python allocations."""
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
    directory: Path, response_count: int, trait_count: int
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
    finally:
        tracemalloc.stop()
    return {
        "response_count": response_count,
        "trait_count": trait_count,
        "logical_task_count": response_count * trait_count,
        "sample_count": len(sample),
        "source_byte_length": source.stat().st_size,
        "python_peak_bytes": python_peak,
        "native_peak_bytes": native_peak_bytes(),
        "elapsed_seconds": time.monotonic() - started,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--responses", type=int, required=True)
    parser.add_argument("--traits", type=int, default=1)
    options = parser.parse_args()
    if options.responses < 1 or options.traits < 1:
        parser.error("response and trait counts must be positive")
    result = measure(options.directory, options.responses, options.traits)
    encoded = json.dumps(result)
    (options.directory / "measurement.json").write_text(encoded, encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
