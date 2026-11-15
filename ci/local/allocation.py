"""One host allocation across foreground checks, native grids and main work."""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any

import psutil
from resource_budget import ResourceLease


def state_root() -> Path:
    return Path(
        os.environ.get(
            "JUDGMENTLLM_ALLOCATION_ROOT",
            str(
                Path(
                    os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
                )
                / "judgmentllm-ci/allocation"
            ),
        )
    )


def alive(record: dict[str, Any]) -> bool:
    try:
        process = psutil.Process(record["pid"])
        return (
            process.create_time() == record["birth"]
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except (KeyError, TypeError, psutil.Error):
        return False


def foreground_pending(root: Path) -> bool:
    for path in root.glob("foreground-*.json"):
        try:
            if alive(json.loads(path.read_text())):
                return True
        except (OSError, ValueError):
            return True
    return False


class QualificationLease(AbstractContextManager["QualificationLease"]):
    def __init__(
        self,
        root: Path | None = None,
        *,
        background: bool = False,
        timeout: float = 2700,
    ) -> None:
        self.root = root or state_root()
        self.background = background
        self.timeout = timeout
        self.request: Path | None = None
        self.lease: ResourceLease | None = None
        self.record: dict[str, Any] = {}
        self.inherited = False

    def __enter__(self) -> QualificationLease:
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        owner_path = self.root / "owner.json"
        if os.environ.get("JUDGMENTLLM_ALLOCATION_TOKEN") and owner_path.is_file():
            record = json.loads(owner_path.read_text())
            if (
                record.get("active") is True
                and record.get("token") == os.environ["JUDGMENTLLM_ALLOCATION_TOKEN"]
                and alive(record)
            ):
                # An owned systemd stage has a different parent tree; the private
                # active token is the delegation capability, bound to PID/birth.
                self.record, self.inherited = record, True
                return self
        if self.background and foreground_pending(self.root):
            raise BlockingIOError("Foreground qualification has priority")
        self.record = {
            "pid": os.getpid(),
            "birth": psutil.Process().create_time(),
            "token": uuid.uuid4().hex,
            "role": "background" if self.background else "foreground",
            "started": time.time(),
            "active": True,
        }
        if not self.background:
            request = self.root / ("foreground-" + self.record["token"] + ".json")
            request.write_text(json.dumps(self.record))
            self.request = request
        try:
            self.lease = ResourceLease(
                self.root / "lock", 1, timeout=0 if self.background else self.timeout
            )
            self.lease.__enter__()
            if self.background and foreground_pending(self.root):
                raise BlockingIOError(
                    "Foreground qualification arrived during admission"
                )
            owner_path.write_text(json.dumps(self.record))
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def environment(self) -> dict[str, str]:
        return {
            "JUDGMENTLLM_ALLOCATION_ROOT": str(self.root.resolve()),
            "JUDGMENTLLM_ALLOCATION_TOKEN": self.record["token"],
        }

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.request is not None:
            self.request.unlink(missing_ok=True)
        if not self.inherited and self.lease is not None:
            owner = self.root / "owner.json"
            if owner.is_file() and json.loads(owner.read_text()).get(
                "token"
            ) == self.record.get("token"):
                self.record["active"] = False
                owner.write_text(json.dumps(self.record))
            self.lease.__exit__(exc_type, exc, traceback)
