"""Crash recovery journal for the two existing CI guests, not a VM manager.

Controls are supplied by an experiment only after a supported per-run mechanism
has been confirmed. There is deliberately no arbitrary shell/configuration API.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

Guest = Literal["windows", "macos"]
State = Literal[
    "planned", "applying", "applied", "restoring", "restored", "needs_review"
]


@dataclass(frozen=True)
class Allocation:
    cpus: int
    ram_mib: int

    def __post_init__(self) -> None:
        if (
            type(self.cpus) is not int
            or type(self.ram_mib) is not int
            or not 1 <= self.cpus <= 32
            or not 1 <= self.ram_mib <= 131072
        ):
            raise ValueError("Invalid CPU/RAM allocation")


@dataclass(frozen=True)
class Observation:
    guest_identity: str
    allocation: Allocation
    idle: bool
    exclusively_owned: bool


class Control(Protocol):
    """One reviewed, reversible per-run control for one existing guest."""

    mechanism: str

    def inspect(self) -> Observation: ...

    def apply(self, allocation: Allocation) -> None: ...


def save(path: Path, value: dict[str, object]) -> None:
    """Publish durable intent before touching a runtime allocation."""
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class RecoveryJournal:
    def __init__(self, path: Path, value: dict[str, object]) -> None:
        self.path = path
        self.value = value

    @classmethod
    def begin(
        cls,
        root: Path,
        *,
        guest: Guest,
        experiment: str,
        identity: str,
        original: Allocation,
        temporary: Allocation,
        mechanism: str,
        owner: str,
        lock_identity: str,
    ) -> RecoveryJournal:
        if (
            guest not in {"windows", "macos"}
            or re.fullmatch(r"[A-Za-z0-9-]{1,100}", experiment) is None
        ):
            raise ValueError("Invalid guest or experiment identity")
        if not all((identity, mechanism, owner, lock_identity)):
            raise ValueError("Guest, mechanism, owner and lock identities are required")
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{guest}-{experiment}.json"
        # The adapter's existing exclusive lock must cover journal creation and
        # all controls. Exclusive creation also prevents accidental replacement.
        with path.open("x", encoding="utf-8"):
            pass
        value: dict[str, object] = {
            "schema_version": 1,
            "guest": guest,
            "experiment": experiment,
            "guest_identity": identity,
            "original": asdict(original),
            "temporary": asdict(temporary),
            "mechanism": mechanism,
            "owner_identity": owner,
            "lock_identity": lock_identity,
            "state": "planned",
            "history": ["planned"],
            "restoration_verified": False,
        }
        save(path, value)
        return cls(path, value)

    @classmethod
    def load(cls, path: Path) -> RecoveryJournal:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Malformed allocation recovery journal")
        record = cast(dict[str, object], value)
        expected = {
            "schema_version",
            "guest",
            "experiment",
            "guest_identity",
            "original",
            "temporary",
            "mechanism",
            "owner_identity",
            "lock_identity",
            "state",
            "history",
            "restoration_verified",
        }
        if (
            set(record) != expected
            or type(record["schema_version"]) is not int
            or record["schema_version"] != 1
            or record["guest"] not in {"windows", "macos"}
        ):
            raise ValueError("Unsupported allocation recovery journal")
        for key in ("original", "temporary"):
            raw = record[key]
            if not isinstance(raw, dict) or set(cast(dict[str, object], raw)) != {
                "cpus",
                "ram_mib",
            }:
                raise ValueError("Malformed journal allocation")
            Allocation(**cast(dict[str, int], raw))
        states = {
            "planned",
            "applying",
            "applied",
            "restoring",
            "restored",
            "needs_review",
        }
        history = record["history"]
        if not isinstance(history, list):
            raise ValueError("Malformed allocation state history")
        entries = cast(list[object], history)
        if (
            not entries
            or entries[0] != "planned"
            or entries[-1] != record["state"]
            or not all(isinstance(entry, str) and entry in states for entry in entries)
        ):
            raise ValueError("Malformed allocation state history")
        if type(record["restoration_verified"]) is not bool or record[
            "restoration_verified"
        ] != (record["state"] == "restored"):
            raise ValueError("Inconsistent restoration evidence")
        if record["state"] == "restored" and (
            len(entries) < 3 or entries[-2] != "restoring"
        ):
            raise ValueError("Missing restoration verification history")
        for key in (
            "guest_identity",
            "mechanism",
            "experiment",
            "owner_identity",
            "lock_identity",
        ):
            if not isinstance(record[key], str) or not record[key]:
                raise ValueError("Missing guest or ownership identity")
        return cls(path, record)

    def transition(self, state: State, *, verified: bool = False) -> None:
        self.value["state"] = state
        self.value["restoration_verified"] = verified
        history = cast(list[str], self.value["history"])
        history.append(state)
        save(self.path, self.value)

    def allocation(self, key: str) -> Allocation:
        return Allocation(**cast(dict[str, int], self.value[key]))

    def inspect(self, control: Control, check_lock: Callable[[], None]) -> Observation:
        check_lock()
        observed = control.inspect()
        if (
            observed.guest_identity != self.value["guest_identity"]
            or not observed.idle
            or not observed.exclusively_owned
            or control.mechanism != self.value["mechanism"]
        ):
            raise RuntimeError(
                "Guest identity, idle state, ownership or control differs"
            )
        return observed

    def restore(self, control: Control, check_lock: Callable[[], None]) -> None:
        """Reconcile real settings; process termination is never restoration proof."""
        observed = self.inspect(control, check_lock)
        original = self.allocation("original")
        self.transition("restoring")
        try:
            if observed.allocation != original:
                control.apply(original)
            if self.inspect(control, check_lock).allocation != original:
                raise RuntimeError("Original guest allocation was not restored")
        except BaseException:
            self.transition("needs_review")
            raise
        self.transition("restored", verified=True)

    def execute(
        self,
        control: Control,
        check_lock: Callable[[], None],
        experiment: Callable[[], None],
    ) -> None:
        if self.inspect(control, check_lock).allocation != self.allocation("original"):
            self.transition("needs_review")
            raise RuntimeError("Original guest allocation differs before override")
        try:
            self.transition("applying")
            control.apply(self.allocation("temporary"))
            if self.inspect(control, check_lock).allocation != self.allocation(
                "temporary"
            ):
                raise RuntimeError("Temporary allocation was only partially applied")
            self.transition("applied")
            experiment()
        finally:
            self.restore(control, check_lock)


def require_restored(root: Path, guests: set[str] | None = None) -> None:
    """Block new experiments until every previous override is verified restored.

    An experiment with a supported control must call journal.restore() under the
    existing guest lock to reconcile actual settings before retrying admission.
    A journal cannot be accepted just because its previous owner disappeared.
    """
    if not root.exists():
        return
    for path in sorted(root.glob("*.json")):
        journal = RecoveryJournal.load(path)
        if guests is not None and journal.value["guest"] not in guests:
            continue
        if (
            journal.value["state"] != "restored"
            or journal.value["restoration_verified"] is not True
        ):
            raise RuntimeError(
                f"Unrestored guest allocation: {path}. Reconcile actual settings using the recorded control under the existing guest lock before benchmarking."
            )
