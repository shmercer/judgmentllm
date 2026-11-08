"""Explicit qualification allocations for the existing 32-thread workstation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Allocation:
    workers: int
    lanes: int
    resource_slots: int
    cpus: int
    ram_gib: int

    def __post_init__(self) -> None:
        if (
            not 0 <= self.workers <= self.cpus
            or not 1 <= self.lanes <= 3
            or not 1 <= self.resource_slots <= self.lanes
            or self.ram_gib < 1
        ):
            raise ValueError("Invalid bounded qualification allocation")


@dataclass(frozen=True)
class Schedule:
    name: str
    linux: Allocation
    windows: Allocation
    macos: Allocation
    platforms: int = 3

    def report(self) -> dict[str, Any]:
        return asdict(self)

    def budget(
        self, selected: list[str], logical_cpus: int, memory_bytes: int
    ) -> dict[str, int]:
        """Count a shared guest once; Linux limits apply to each disposable lane."""
        allocations = [getattr(self, platform) for platform in selected]
        cpu = [
            a.cpus * a.lanes if p == "linux" else a.cpus
            for p, a in zip(selected, allocations, strict=True)
        ]
        ram = [
            a.ram_gib * a.lanes if p == "linux" else a.ram_gib
            for p, a in zip(selected, allocations, strict=True)
        ]
        cpus = max(cpu) if self.platforms == 1 else sum(cpu)
        ram_gib = max(ram) if self.platforms == 1 else sum(ram)
        # Explicit host headroom. Quotas are CPU time caps, not affinity or
        # physical-core reservations; utilization/variability still need measurement.
        if cpus + 2 > logical_cpus or (ram_gib + 16) * 1024**3 > memory_bytes:
            raise ValueError("Qualification allocation exceeds host CPU/RAM budget")
        return {
            "cpu_cap_total": cpus,
            "ram_cap_total_gib": ram_gib,
            "host_cpu_headroom": logical_cpus - cpus,
            "host_ram_headroom_bytes": memory_bytes - ram_gib * 1024**3,
        }


SCHEDULES = {
    "baseline": Schedule(
        "baseline",
        Allocation(0, 3, 3, 6, 16),
        Allocation(0, 2, 2, 6, 12),
        Allocation(0, 2, 2, 6, 16),
    ),
    "parallel": Schedule(
        "parallel",
        Allocation(2, 3, 2, 4, 16),
        Allocation(2, 2, 1, 6, 12),
        Allocation(2, 2, 1, 6, 16),
    ),
    "staged": Schedule(
        "staged",
        Allocation(4, 1, 1, 6, 16),
        Allocation(4, 1, 1, 6, 12),
        Allocation(4, 1, 1, 6, 16),
    ),
    "resource-aware": Schedule(
        "resource-aware",
        Allocation(3, 2, 2, 3, 16),
        Allocation(4, 1, 1, 6, 12),
        Allocation(4, 1, 1, 6, 16),
    ),
    "isolated": Schedule(
        "isolated",
        Allocation(4, 1, 1, 6, 16),
        Allocation(4, 1, 1, 6, 12),
        Allocation(4, 1, 1, 6, 16),
        platforms=1,
    ),
}


def get_schedule(name: str) -> Schedule:
    try:
        return SCHEDULES[name]
    except KeyError:
        raise ValueError(f"Unknown qualification schedule: {name}") from None
