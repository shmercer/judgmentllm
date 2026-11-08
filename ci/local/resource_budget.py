"""Bound resource batches inside one owned container/guest run.

The existing adapters own VM lifecycle locks. These byte locks only coordinate
Python lanes sharing that allocation; the OS releases them after process death.
"""

from __future__ import annotations

import json
import math
import os
import time
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


def _lock(stream: BinaryIO, *, release: bool = False) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(
            stream.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1
        )
    else:
        import fcntl

        fcntl.flock(
            stream.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB
        )


class ResourceLease(AbstractContextManager["ResourceLease"]):
    """Acquire one bounded batch slot without changing workload limits."""

    def __init__(self, root: Path, slots: int, *, timeout: float = 2700) -> None:
        if type(slots) is not int or not 1 <= slots <= 7:
            raise ValueError("resource slots must be an integer from 1 through 7")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("resource lease timeout must be nonnegative")
        self.root = root
        self.slots = slots
        self.timeout = timeout
        self.slot: int | None = None
        self._stream: BinaryIO | None = None
        self.wait_seconds = 0.0

    def __enter__(self) -> ResourceLease:
        self.root.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        while True:
            for slot in range(self.slots):
                stream = (self.root / f"slot-{slot}.lock").open("a+b")
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"0")
                    stream.flush()
                try:
                    _lock(stream)
                except OSError:
                    stream.close()
                    continue
                self._stream, self.slot = stream, slot
                self.wait_seconds = time.monotonic() - started
                return self
            if time.monotonic() - started >= self.timeout:
                raise TimeoutError("Timed out waiting for the owned resource budget")
            time.sleep(min(0.2, self.timeout))

    def record(self, path: Path) -> None:
        if self._stream is None:
            raise RuntimeError("Resource lease is not held")
        path.write_text(
            json.dumps(
                {
                    "slot": self.slot,
                    "slots": self.slots,
                    "wait_seconds": self.wait_seconds,
                    "pid": os.getpid(),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            try:
                _lock(self._stream, release=True)
            finally:
                self._stream.close()
                self._stream = None
