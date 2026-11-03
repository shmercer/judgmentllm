"""Fresh processes bound Python/native memory as sources and task products grow."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

FIXTURE = (
    Path(__file__).resolve().parents[1] / "scripts" / "preparation_memory_fixture.py"
)
MIB = 1024 * 1024


def measurement(directory: Path, responses: int, traits: int) -> dict[str, int | float]:
    completed = subprocess.run(
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
        timeout=90,
    )
    assert completed.returncode == 0, completed.stderr
    result = cast(dict[str, int | float], json.loads(completed.stdout))
    assert result["native_peak_bytes"] > 0, result
    assert result["native_peak_bytes"] < 256 * MIB, result
    assert result["python_peak_bytes"] < 32 * MIB, result
    assert result["sample_count"] == 64, result
    return result


def test_large_sources_use_bounded_indexing_and_lookup_memory(tmp_path: Path) -> None:
    small = measurement(tmp_path / "small", 200, 1)
    large = measurement(tmp_path / "large", 2_000, 1)
    assert large["source_byte_length"] > 9 * small["source_byte_length"]
    assert large["python_peak_bytes"] < small["python_peak_bytes"] + 3 * MIB, (
        small,
        large,
    )
    assert large["native_peak_bytes"] < small["native_peak_bytes"] + 48 * MIB, (
        small,
        large,
    )


def test_trait_product_remains_incremental_without_execution_rows(
    tmp_path: Path,
) -> None:
    small = measurement(tmp_path / "few-traits", 40, 20)
    large = measurement(tmp_path / "many-traits", 40, 200)
    assert large["logical_task_count"] == 10 * small["logical_task_count"]
    # Rubric/context memory may grow with trait count; full response/trait product
    # storage and execution rows are never needed for the bounded request sample.
    assert large["python_peak_bytes"] < small["python_peak_bytes"] + 8 * MIB, (
        small,
        large,
    )
    assert large["native_peak_bytes"] < small["native_peak_bytes"] + 48 * MIB, (
        small,
        large,
    )
