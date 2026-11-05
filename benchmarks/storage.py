"""External, bounded file-size sampling; no assessment bytes enter measurements."""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, TypedDict

GIB = 1024**3


class Inventory(TypedDict):
    bytes: int
    allocated_bytes: int | None
    files: int
    object_bytes: int
    object_allocated_bytes: int | None
    temporary_bytes: int
    temporary_allocated_bytes: int | None


def inventory(root: Path) -> Inventory:
    """Phase-boundary inventory, including temporary staging and object counts."""
    totals = {
        "bytes": 0,
        "allocated_bytes": 0,
        "files": 0,
        "object_bytes": 0,
        "object_allocated_bytes": 0,
        "temporary_bytes": 0,
        "temporary_allocated_bytes": 0,
    }
    allocation_available = True
    for directory, _, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            try:
                stat = path.stat()
                size = stat.st_size
            except FileNotFoundError:
                continue
            totals["bytes"] += size
            blocks = getattr(stat, "st_blocks", None)
            allocated = 0 if blocks is None else int(blocks) * 512
            allocation_available = allocation_available and blocks is not None
            totals["allocated_bytes"] += allocated
            totals["files"] += 1
            if "objects" in path.parts:
                totals["object_bytes"] += size
                totals["object_allocated_bytes"] += allocated
            if any(
                part.startswith((".export-", ".stage-", ".tmp")) for part in path.parts
            ) or name.startswith("tmp"):
                totals["temporary_bytes"] += size
                totals["temporary_allocated_bytes"] += allocated
    return Inventory(
        bytes=totals["bytes"],
        allocated_bytes=totals["allocated_bytes"] if allocation_available else None,
        files=totals["files"],
        object_bytes=totals["object_bytes"],
        object_allocated_bytes=totals["object_allocated_bytes"]
        if allocation_available
        else None,
        temporary_bytes=totals["temporary_bytes"],
        temporary_allocated_bytes=totals["temporary_allocated_bytes"]
        if allocation_available
        else None,
    )


class StorageSampler:
    """Sample DB/WAL/SHM and volume free space at 250 ms, outside execution threads.

    Free-space growth is volume-wide and may include unrelated writers; qualify
    samples only on an otherwise idle volume. It is a conservative budget guard,
    not an attribution of all disk writes to this child. Phase inventories provide
    exact retained file sizes without rescanning every object each interval.
    """

    def __init__(
        self, root: Path, *, budget_gib: float = 32, min_free_gib: float = 10
    ) -> None:
        self.root = root
        self.budget = int(budget_gib * GIB)
        self.minimum = int(min_free_gib * GIB)
        self.initial = inventory(root)
        self.initial_free = shutil.disk_usage(root).free
        self.minimum_free = self.initial_free
        self.samples = 0
        self.high_water: dict[str, int] = {}
        self.inventories: dict[str, Inventory] = {}
        self.error: str | None = None
        self.lock = threading.Lock()
        self.inventory_wall_seconds = 0.0
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name="benchmark-storage", daemon=True
        )

    def sample(self) -> None:
        with self.lock:
            self._sample()

    def _sample(self) -> None:
        self.samples += 1
        free = shutil.disk_usage(self.root).free
        self.minimum_free = min(self.minimum_free, free)
        for database in ("store/registry.sqlite3", "service.sqlite3"):
            for suffix in ("", "-wal", "-shm"):
                path = self.root / (database + suffix)
                try:
                    size = path.stat().st_size
                except FileNotFoundError:
                    size = 0
                self.high_water[database + suffix] = max(
                    self.high_water.get(database + suffix, 0), size
                )
        if free < self.minimum:
            self.error = "minimum free-space reserve was crossed"
        if self.initial["bytes"] + max(0, self.initial_free - free) > self.budget:
            self.error = "sample disk budget was crossed"

    def _run(self) -> None:
        while not self.stop_event.wait(0.25):
            try:
                self.sample()
            except OSError:
                with self.lock:
                    self.error = "storage sampling failed"
                return

    def __enter__(self) -> StorageSampler:
        self.sample()
        self.check()
        self.thread.start()
        return self

    def check(self) -> None:
        with self.lock:
            if self.error is not None:
                raise RuntimeError(self.error)

    def phase(self, name: str) -> None:
        started = time.perf_counter()
        observed = inventory(self.root)
        elapsed = time.perf_counter() - started
        with self.lock:
            self.inventories[name] = observed
            self.inventory_wall_seconds += elapsed
            if observed["bytes"] > self.budget:
                self.error = "sample disk budget was crossed"
        self.sample()
        self.check()

    def __exit__(self, exception_type: object, *_: object) -> None:
        self.stop_event.set()
        self.thread.join()
        try:
            self.sample()
        except OSError:
            with self.lock:
                self.error = "storage sampling failed"
        if exception_type is None:
            self.check()

    def report(self) -> dict[str, Any]:
        with self.lock:
            return {
                "interval_seconds": 0.25,
                "samples": self.samples,
                "observed_file_high_water_bytes": dict(self.high_water),
                "initial_free_bytes": self.initial_free,
                "minimum_free_bytes": self.minimum_free,
                "sampled_filesystem_used_growth_bytes": max(
                    0, self.initial_free - self.minimum_free
                ),
                "phase_inventories": {
                    name: dict(record) for name, record in self.inventories.items()
                },
                "disk_budget_bytes": self.budget,
                "minimum_free_reserve_bytes": self.minimum,
                "sampling_error": self.error,
                "phase_inventory_wall_seconds": self.inventory_wall_seconds,
                "timing_note": "ordinary wall times include sampling and phase-inventory overhead",
                "scope": "sampled file high-water; phase inventories; volume-wide free-space growth",
            }
