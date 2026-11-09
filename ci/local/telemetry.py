"""Sample only an owned process tree; never inspect commands or environments."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType

try:
    import psutil
except ModuleNotFoundError:
    # Native adapters bootstrap the locked environment with a bare interpreter.
    # Detailed process sampling starts inside the installed suite runner; the
    # bootstrap still records wall time and Unix waited-child CPU accurately.
    psutil = None


class ProcessSampler(AbstractContextManager["ProcessSampler"]):
    """CPU lower bounds and observed aggregate RSS, including owned children.

    RSS sums can count shared pages more than once. Processes living entirely
    between samples are not observed; resource fixtures retain their independent
    fresh-process native/Python peak measurements and original limits.
    """

    def __init__(
        self, *, interval: float = 0.25, wall_clock: Callable[[], float] = time.time
    ) -> None:
        if not 0.01 <= interval <= 5:
            raise ValueError("Invalid process sampling interval")
        self.interval = interval
        self.wall_clock = wall_clock
        self.root = psutil.Process(os.getpid()) if psutil is not None else None
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.initial: dict[tuple[int, float], float] = {}
        self.cpu: dict[tuple[int, float], float] = {}
        self.peak_rss = 0
        self.peak_children_rss = 0
        self.peak_processes = 0
        self.samples = 0
        self.races = 0
        self.discarded_descendants = 0
        self.visible_memory_total = 0
        self.minimum_visible_available: int | None = None
        self.peak_visible_swap = 0
        self.peak_cgroup_memory = 0
        self.started = 0.0
        self.started_wall = 0.0
        self.elapsed = 0.0
        self.own_cpu_started = 0.0
        self.own_cpu_elapsed = 0.0
        self.children_started: tuple[float, float] | None = None
        self.children_elapsed: tuple[float, float] | None = None

    @staticmethod
    def _children_cpu() -> tuple[float, float] | None:
        if os.name == "nt":
            return None
        import resource

        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return usage.ru_utime, usage.ru_stime

    def sample(self, *, initial: bool = False) -> None:
        if psutil is None or self.root is None:
            return
        memory_state = psutil.virtual_memory()
        self.visible_memory_total = memory_state.total
        self.minimum_visible_available = min(
            self.minimum_visible_available
            if self.minimum_visible_available is not None
            else memory_state.available,
            memory_state.available,
        )
        self.peak_visible_swap = max(self.peak_visible_swap, psutil.swap_memory().used)
        try:
            self.peak_cgroup_memory = max(
                self.peak_cgroup_memory,
                int(Path("/sys/fs/cgroup/memory.current").read_text()),
            )
        except (OSError, ValueError):
            pass  # Not exposed on Windows/macOS or legacy cgroup layouts.
        rss = children_rss = count = 0
        try:
            processes = [self.root, *self.root.children(recursive=True)]
        except psutil.Error:
            self.races += 1
            return
        for process in processes:
            try:
                with process.oneshot():
                    identity = (process.pid, process.create_time())
                    # Windows can retain future creation times after its boot
                    # clock resynchronizes. Reused parent PIDs can then attach
                    # older unrelated processes to psutil's descendant tree.
                    # Only this command's new children contribute. Allow one
                    # second for platform creation-time/boot-time precision.
                    if process.pid != self.root.pid and not (
                        self.started_wall - 1 <= identity[1] <= self.wall_clock() + 1
                    ):
                        self.discarded_descendants += 1
                        continue
                    cpu = process.cpu_times()
                    used = cpu.user + cpu.system
                    memory = process.memory_info().rss
                if initial:
                    self.initial[identity] = used
                self.cpu[identity] = max(self.cpu.get(identity, 0), used)
                rss += memory
                if process.pid != self.root.pid:
                    children_rss += memory
                count += 1
            except psutil.Error:
                self.races += 1
        self.peak_rss = max(self.peak_rss, rss)
        self.peak_children_rss = max(self.peak_children_rss, children_rss)
        self.peak_processes = max(self.peak_processes, count)
        self.samples += 1

    def _run(self) -> None:
        while not self.stop.wait(self.interval):
            self.sample()

    def __enter__(self) -> ProcessSampler:
        self.started = time.monotonic()
        self.started_wall = self.wall_clock()
        self.own_cpu_started = time.process_time()
        self.children_started = self._children_cpu()
        self.sample(initial=True)
        self.thread = threading.Thread(
            target=self._run, name="ci-process-sampler", daemon=True
        )
        self.thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=2 * self.interval + 1)
            if self.thread.is_alive():
                raise RuntimeError("Owned process sampler did not stop")
        self.sample()
        self.elapsed = time.monotonic() - self.started
        self.own_cpu_elapsed = time.process_time() - self.own_cpu_started
        end = self._children_cpu()
        if end is not None and self.children_started is not None:
            self.children_elapsed = (
                end[0] - self.children_started[0],
                end[1] - self.children_started[1],
            )

    def report(self) -> dict[str, int | float | str]:
        cpu = sum(
            max(0, value - self.initial.get(identity, 0))
            for identity, value in self.cpu.items()
        )
        result: dict[str, int | float | str] = {
            "method": "sampled command-created PID/birth-time tree; CPU lower bound; RSS includes shared-page duplication; preexisting/future descendant samples discarded"
            if psutil is not None
            else "bootstrap interpreter: wall/owner CPU and Unix waited-child CPU only; detailed sampling in installed suite",
            "interval_seconds": self.interval,
            "wall_seconds": self.elapsed,
            "sampled_cpu_seconds": cpu,
            "peak_tree_rss_bytes": self.peak_rss,
            "peak_descendant_rss_bytes": self.peak_children_rss,
            "peak_processes": self.peak_processes,
            "observed_processes": len(self.cpu),
            "samples": self.samples,
            "process_exit_races": self.races,
            "discarded_descendant_samples": self.discarded_descendants,
            "owner_cpu_seconds": self.own_cpu_elapsed,
            "visible_memory_scope": "OS-wide, shared guest counted once; Linux OS view may expose host memory; cgroup usage is separate",
            "visible_memory_total_bytes": self.visible_memory_total,
            "minimum_visible_memory_available_bytes": self.minimum_visible_available
            if self.minimum_visible_available is not None
            else 0,
            "peak_visible_swap_used_bytes": self.peak_visible_swap,
            "peak_visible_cgroup_memory_bytes": self.peak_cgroup_memory,
        }
        if self.children_elapsed is not None:
            result["waited_children_cpu_seconds"] = sum(self.children_elapsed)
        return result
