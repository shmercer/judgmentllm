"""Real-file storage measurements and concurrent diagnostic sampling."""

from __future__ import annotations

import importlib
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def storage_tool(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(
        sys,
        "path",
        [str(Path(__file__).resolve().parents[1] / "benchmarks"), *sys.path],
    )
    return importlib.import_module("storage")


def test_inventory_reports_actual_logical_and_allocated_bytes(
    tmp_path: Path, storage_tool: Any
) -> None:
    # Import through the development-tool namespace, outside installed package.
    storage = storage_tool

    objects = tmp_path / "objects"
    temporary = tmp_path / ".export-owned"
    objects.mkdir()
    temporary.mkdir()
    paths = (
        tmp_path / "registry.sqlite3",
        objects / "hash",
        temporary / "results",
        tmp_path / ".pending-owned",
    )
    for index, path in enumerate(paths):
        path.write_bytes(b"x" * (1024 + index))
    alias = tmp_path / "registry-alias"
    os.link(paths[0], alias)
    paths = (*paths, alias)
    measured = storage.inventory(tmp_path)
    assert measured["files"] == 5
    assert measured["bytes"] == sum(path.stat().st_size for path in paths)
    assert measured["object_bytes"] == paths[1].stat().st_size
    assert (
        measured["temporary_bytes"] == paths[2].stat().st_size + paths[3].stat().st_size
    )
    if os.name != "nt" and all(hasattr(path.stat(), "st_blocks") for path in paths):
        assert measured["allocated_bytes"] == sum(
            path.stat().st_blocks * 512 for path in paths
        )
        assert measured["object_allocated_bytes"] == paths[1].stat().st_blocks * 512
        assert (
            measured["temporary_allocated_bytes"]
            == (paths[2].stat().st_blocks + paths[3].stat().st_blocks) * 512
        )
    else:
        assert measured["allocated_bytes"] is None


def test_concurrent_observers_preserve_high_water_and_report_snapshots(
    tmp_path: Path, storage_tool: Any
) -> None:
    storage = storage_tool

    (tmp_path / "store").mkdir()
    wal = tmp_path / "store" / "registry.sqlite3-wal"
    wal.write_bytes(b"x" * 4096)
    sampler = storage.StorageSampler(tmp_path, min_free_gib=0)
    with sampler:
        sampler.phase("initial")
        initial = sampler.report()
        wal.write_bytes(b"x" * 65536)
        with ThreadPoolExecutor(max_workers=4) as workers:
            futures = [
                workers.submit(sampler.phase, f"observer-{index}")
                for index in range(32)
            ]
            for future in futures:
                future.result(timeout=5)
        wal.write_bytes(b"x")
        deadline = time.monotonic() + 3
        # Require at least one real periodic read as well as explicit observers.
        observed = sampler.report()["samples"]
        while sampler.report()["samples"] <= observed:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    final = sampler.report()
    assert "hardlink aliases" in final["inventory_accounting"]
    assert not sampler.thread.is_alive()
    assert (
        final["observed_file_high_water_bytes"]["store/registry.sqlite3-wal"] == 65536
    )
    assert (
        initial["observed_file_high_water_bytes"]["store/registry.sqlite3-wal"] == 4096
    )
    assert len(initial["phase_inventories"]) == 1
    assert len(final["phase_inventories"]) == 33
    assert final["samples"] >= 36


def test_context_exit_checks_final_resource_state_and_preserves_body_error(
    tmp_path: Path, storage_tool: Any
) -> None:
    storage = storage_tool

    source = tmp_path / "retained"
    source.write_bytes(b"x" * 4096)
    sampler = storage.StorageSampler(tmp_path, min_free_gib=0)
    with pytest.raises(RuntimeError, match="sample disk budget was crossed"):
        with sampler:
            # Tighten the operational budget after entry. The actual retained
            # initial file already exceeds it, independently of other writers.
            sampler.budget = source.stat().st_size - 1
    assert not sampler.thread.is_alive()
    assert sampler.report()["sampling_error"] == "sample disk budget was crossed"
    second = storage.StorageSampler(tmp_path, min_free_gib=0)
    with pytest.raises(ValueError, match="original body failure"):
        with second:
            second.budget = source.stat().st_size - 1
            raise ValueError("original body failure")
    assert not second.thread.is_alive()
