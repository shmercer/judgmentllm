"""Fixed grouped buffers as response and rubric trait products increase."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from test_execution_resources import FIXTURE, MIB


def measured(directory: Path, responses: int, traits: int) -> dict[str, int | float]:
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
            "--group-size",
            "5",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert process.returncode == 0, process.stderr
    assert not process.stderr
    result = cast(dict[str, int | float], json.loads(process.stdout))
    concurrency, sample, group = (
        result["concurrency"],
        result["sample_count"],
        result["group_size"],
    )
    assert sample == 64 and group == 5, result
    assert result["result_count"] == sample * group, result
    assert result["execution_asyncio_tasks"] == concurrency + 2, result
    assert result["peak_asyncio_tasks"] <= 2 * concurrency + 3, result
    assert result["created_asyncio_tasks"] < sample + 4 * concurrency + 3, result
    assert 0 < result["request_queue_high_water"] <= 2 * concurrency, result
    assert 0 < result["result_queue_high_water"] <= concurrency, result
    assert sample <= result["prepared_request_count"] <= sample + 4 * concurrency + 3, (
        result
    )
    assert result["prepared_count"] == group * result["prepared_request_count"], result
    assert sample <= result["acceptance_count"] <= result["prepared_request_count"], (
        result
    )
    assert result["prepared_count"] < result["logical_task_count"], result
    assert 0 < result["native_peak_bytes"] < 256 * MIB, result
    assert 0 < result["python_peak_bytes"] < 32 * MIB, result
    assert result["python_after_gc_bytes"] < result["python_peak_bytes"], result
    assert result["export_row_count"] == result["result_count"], result
    assert 0 < result["export_python_peak_bytes"] < 8 * MIB, result
    return result


@pytest.mark.resource
def test_grouped_memory_and_buffers_ignore_total_pending_product(
    tmp_path: Path,
) -> None:
    small = measured(tmp_path / "small", 200, 20)
    many_responses = measured(tmp_path / "many-responses", 2000, 20)
    many_traits = measured(tmp_path / "many-traits", 200, 200)
    for large in (many_responses, many_traits):
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
        assert (
            large["export_python_peak_bytes"] < small["export_python_peak_bytes"] + MIB
        ), (small, large)
