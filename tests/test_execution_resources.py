"""Fresh-process regression of bounded live scheduling and response processing."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

FIXTURE = (
    Path(__file__).resolve().parents[1] / "scripts" / "execution_memory_fixture.py"
)
MIB = 1024 * 1024


def measurement(directory: Path, responses: int, traits: int) -> dict[str, int | float]:
    process = subprocess.run(
        [
            sys.executable,
            str(FIXTURE),
            "--directory",
            str(directory),
            "--responses",
            str(responses),
            "--traits",
            str(traits),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert process.returncode == 0, process.stderr
    assert not process.stderr
    result = cast(dict[str, int | float], json.loads(process.stdout))
    concurrency, sample = result["concurrency"], result["sample_count"]
    assert sample == 64, result
    assert result["execution_asyncio_tasks"] == concurrency + 2, result
    # The mock may briefly shield one acceptance-journal write per worker.
    assert result["peak_asyncio_tasks"] <= 2 * concurrency + 3, result
    assert result["created_asyncio_tasks"] < sample + 4 * concurrency + 3, result
    # Queues hold 2C pending requests and C responses, with C workers plus
    # bounded producer/consumer handoff. Completed records live only in SQLite.
    assert sample <= result["prepared_count"] <= sample + 4 * concurrency + 3, result
    assert sample <= result["acceptance_count"] <= result["prepared_count"], result
    assert result["prepared_count"] < result["logical_task_count"], result
    assert 0 < result["native_peak_bytes"] < 256 * MIB, result
    assert 0 < result["python_peak_bytes"] < 32 * MIB, result
    assert result["python_after_gc_bytes"] < result["python_peak_bytes"], result
    return result


def test_execution_memory_does_not_grow_with_response_count(tmp_path: Path) -> None:
    small = measurement(tmp_path / "small", 200, 20)
    large = measurement(tmp_path / "large", 2000, 20)
    assert large["source_byte_length"] > 9 * small["source_byte_length"]
    assert large["logical_task_count"] == 10 * small["logical_task_count"]
    assert large["python_peak_bytes"] < small["python_peak_bytes"] + 3 * MIB, (
        small,
        large,
    )
    assert large["native_peak_bytes"] < small["native_peak_bytes"] + 48 * MIB, (
        small,
        large,
    )
    assert large["python_after_gc_bytes"] < small["python_after_gc_bytes"] + MIB, (
        small,
        large,
    )


def test_execution_memory_does_not_materialize_response_trait_product(
    tmp_path: Path,
) -> None:
    small = measurement(tmp_path / "few-traits", 200, 20)
    large = measurement(tmp_path / "many-traits", 200, 200)
    assert large["logical_task_count"] == 10 * small["logical_task_count"]
    assert large["python_peak_bytes"] < small["python_peak_bytes"] + 3 * MIB, (
        small,
        large,
    )
    assert large["native_peak_bytes"] < small["native_peak_bytes"] + 48 * MIB, (
        small,
        large,
    )
    assert large["python_after_gc_bytes"] < small["python_after_gc_bytes"] + MIB, (
        small,
        large,
    )
