"""Read CPU quotas and physical/guest memory without changing allocations."""

from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path
from typing import TypedDict


class Resources(TypedDict):
    logical_cpus: int
    effective_cpus: int
    quota_cpus: float | None
    memory_bytes: int | None
    available_memory_bytes: int | None


def read_resources() -> Resources:
    count = os.cpu_count() or 1
    quota: float | None = None
    memory: int | None = None
    available: int | None = None
    if sys.platform == "linux":
        count = min(count, len(os.sched_getaffinity(0)))
        path = Path("/sys/fs/cgroup/cpu.max")
        if path.is_file():
            values = path.read_text().split()
            if values[0] != "max":
                quota = int(values[0]) / int(values[1])
        else:
            maximum = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
            period = maximum.with_name("cpu.cfs_period_us")
            if maximum.is_file() and int(maximum.read_text()) > 0:
                quota = int(maximum.read_text()) / int(period.read_text())
        values = {
            line.split(":")[0]: int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith(("MemTotal:", "MemAvailable:"))
        }
        memory, available = values["MemTotal"], values["MemAvailable"]
        limit = Path("/sys/fs/cgroup/memory.max")
        if limit.is_file() and limit.read_text().strip() != "max":
            ceiling = int(limit.read_text())
            memory = min(memory, ceiling)
            current = limit.with_name("memory.current")
            remaining = (
                max(0, ceiling - int(current.read_text()))
                if current.is_file()
                else ceiling
            )
            available = min(available, remaining)
    elif sys.platform == "darwin":
        memory = int(
            subprocess.check_output(
                ["/usr/sbin/sysctl", "-n", "hw.memsize"], text=True
            ).strip()
        )
        try:
            import psutil
        except ModuleNotFoundError:
            pass  # Bare bootstrap; installed suite runners have locked psutil.
        else:
            available = psutil.virtual_memory().available
    elif sys.platform == "win32":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page", ctypes.c_ulonglong),
                ("available_page", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("extended", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("Cannot inspect guest memory allocation")
        memory, available = int(status.total_physical), int(status.available_physical)
    return {
        "logical_cpus": count,
        "effective_cpus": min(count, max(1, math.floor(quota)))
        if quota is not None
        else count,
        "quota_cpus": quota,
        "memory_bytes": memory,
        "available_memory_bytes": available,
    }
