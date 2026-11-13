"""One permanent campaign lock and an append-only, crash-conservative budget journal."""

from __future__ import annotations

import json

# pyright: reportPrivateUsage=false
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field

from judgmentllm.native_schema import NativeModel
from judgmentllm.persistence.artifacts import _fsync_directory
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.ownership import ExecutorOwnership, _lock, _unlock
from judgmentllm.qualification import _immutable
from judgmentllm.study.definitions import SUITES
from judgmentllm.study.records import Authorization, Case, Proposal


class BudgetStop(ValueError):
    """Safe incomplete coverage, never permission to change a sealed case."""


def _global_lock_path() -> Path:
    return Path.home() / ".judgmentllm" / "study-campaign-v1.lock"


@contextmanager
def _exclusive(path: Path) -> Generator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = ExecutorOwnership._open_file(path)
    try:
        _lock(stream)
        try:
            yield
        finally:
            _unlock(stream)
    finally:
        stream.close()


class Event(NativeModel):
    sequence: int = Field(ge=0)
    previous_sha256: str
    at: datetime
    kind: Literal["start", "reserve", "terminal", "hold", "cancel"]
    suite: str
    proposal_sha256: str
    case_id: str
    reserved_usd: Decimal = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    seconds: int = Field(ge=0)
    reason: str
    evidence_sha256: str | None = None


class Checkpoint(NativeModel):
    sequence: int = Field(ge=0)
    previous_sha256: str
    relative_path: str
    artifact_sha256: str


def immutable(path: Path, raw: bytes) -> None:
    """Idempotent exact bytes; no replacement, including after interruption."""
    if path.is_symlink():
        raise ValueError("Campaign files cannot be symlinks")
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Immutable campaign artifact differs")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _immutable(path, raw)
    _fsync_directory(path.parent)


class BudgetLedger:
    """Reservations remain consumed after uncertainty, cancellation and restart.

    Money is not reclaimed from token estimates. Only independently demonstrated
    non-billing can justify a later reviewed reconciliation; this executor never
    increases its allocation. Suite wall deadlines include restart downtime.
    The campaign counts bounded active call time, including unresolved calls.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if self.root.is_relative_to(Path(__file__).resolve().parents[3]):
            raise ValueError("Campaign state must be outside the source checkout")
        self._held = False
        key = bytes_digest(canonical_bytes(str(self.root)))
        self._root_marker = (
            _global_lock_path().parent
            / "study-campaign-bindings"
            / ("root-" + key + ".json")
        )
        self._checkpoints = (
            _global_lock_path().parent / "study-campaign-checkpoints" / key
        )

    def _checkpoint(self) -> list[Checkpoint]:
        if self._checkpoints.is_symlink():
            raise BudgetStop("Independent campaign checkpoint directory is a symlink")
        records: list[Checkpoint] = []
        committed: dict[str, str] = {}
        previous = "0" * 64
        for index, path in enumerate(sorted(self._checkpoints.glob("*.json"))):
            if path.is_symlink() or not path.is_file():
                raise BudgetStop(
                    "Independent campaign checkpoint is not a regular file"
                )
            raw = path.read_bytes()
            record = Checkpoint.model_validate_json(raw)
            relative = Path(record.relative_path)
            if (
                path.is_symlink()
                or path.name != f"{index:06d}.json"
                or canonical_bytes(record) != raw
                or record.sequence != index
                or record.previous_sha256 != previous
                or relative.is_absolute()
                or ".." in relative.parts
                or record.relative_path in committed
            ):
                raise BudgetStop(
                    "Independent campaign checkpoint inventory or commitment differs"
                )
            committed[record.relative_path] = record.artifact_sha256
            previous = bytes_digest(raw)
            records.append(record)
        if self._root_marker.is_symlink() or (
            self._root_marker.exists() and not records
        ):
            raise BudgetStop(
                "Independent campaign checkpoint is missing; reconciliation required"
            )
        if self._root_marker.exists() and not self._root_marker.is_file():
            raise BudgetStop("Independent campaign root marker is not a regular file")
        if records and (
            not self._root_marker.exists()
            or json.loads(self._root_marker.read_bytes())["campaign_root"]
            != str(self.root)
        ):
            raise BudgetStop("Independent campaign root identity differs")
        actual: dict[str, str] = {}
        for path in self.root.rglob("*"):
            if path.is_symlink():
                raise BudgetStop("Campaign journal inventory contains a symlink")
            if path.is_file() and path.name != "campaign.lock":
                actual[path.relative_to(self.root).as_posix()] = bytes_digest(
                    path.read_bytes()
                )
        if actual != committed:
            raise BudgetStop(
                "Campaign checkpoint/journal inventory is missing, damaged or rolled back"
            )
        return records

    def _publish(self, relative: str, raw: bytes, *, initial: bool = False) -> None:
        records = [] if initial else self._checkpoint()
        digest = bytes_digest(raw)
        prior = next((r for r in records if r.relative_path == relative), None)
        if prior is not None:
            if prior.artifact_sha256 != digest:
                raise ValueError("Immutable campaign artifact differs")
            return
        record = Checkpoint(
            sequence=len(records),
            previous_sha256=bytes_digest(canonical_bytes(records[-1]))
            if records
            else "0" * 64,
            relative_path=relative,
            artifact_sha256=digest,
        )
        immutable(
            self._checkpoints / f"{record.sequence:06d}.json", canonical_bytes(record)
        )
        immutable(self.root / relative, raw)

    @contextmanager
    def locked(self) -> Generator[BudgetLedger]:
        if self._held:
            raise ValueError("Campaign ownership is already held")
        self.root.mkdir(parents=True, exist_ok=True)
        with _exclusive(_global_lock_path()), _exclusive(self.root / "campaign.lock"):
            self._held = True
            try:
                yield self
            finally:
                self._held = False

    def authorize(
        self,
        proposal: Proposal,
        authorization: Authorization,
        *,
        now: datetime | None = None,
    ) -> None:
        self._require_lock()
        proposal = Proposal.model_validate_json(canonical_bytes(proposal))
        authorization = Authorization.model_validate_json(
            canonical_bytes(authorization)
        )
        authorization.verify(proposal)
        started_at = now or datetime.now(UTC)
        source_marker = (
            _global_lock_path().parent
            / "study-campaign-bindings"
            / (
                bytes_digest(
                    canonical_bytes(
                        [
                            "original-study-campaign-v1",
                            proposal.source_commit,
                            proposal.study_version,
                        ]
                    )
                )
                + ".json"
            )
        )
        identity = canonical_bytes(
            {
                "study_version": proposal.study_version,
                "campaign_root": str(self.root),
                "source_commit": proposal.source_commit,
            }
        )
        fresh = not self._root_marker.exists() and not source_marker.exists()
        if source_marker.is_symlink() or (
            source_marker.exists() and not source_marker.is_file()
        ):
            raise BudgetStop("Independent source binding is not a regular file")
        if source_marker.exists() and source_marker.read_bytes() != identity:
            raise ValueError("Immutable campaign artifact differs")
        if not fresh and (
            not source_marker.exists()
            or not self._root_marker.exists()
            or not any(self._checkpoints.glob("*.json"))
        ):
            raise BudgetStop(
                "Previously authorized campaign state is missing; reconciliation required"
            )
        prior = self.events()
        immutable(source_marker, identity)
        immutable(self._root_marker, identity)
        starting = not any(
            e.kind == "start" and e.suite == proposal.suite for e in prior
        )
        if starting:
            self._publish(
                "suite-intents/" + proposal.suite + ".json",
                canonical_bytes(
                    {
                        "proposal_sha256": proposal.digest,
                        "authorization_sha256": bytes_digest(
                            canonical_bytes(authorization)
                        ),
                        "started_at": started_at.isoformat(),
                    }
                ),
                initial=fresh,
            )
        self._publish(
            "campaign.json",
            canonical_bytes(
                {
                    "campaign_version": 1,
                    "source_commit": proposal.source_commit,
                    "max_calls": 66,
                    "usd": "10",
                    "active_seconds": 39600,
                    "concurrency": 1,
                    "retries": 0,
                }
            ),
        )
        self._publish(
            "authorizations/" + proposal.suite + ".json",
            canonical_bytes(authorization),
        )
        self._publish("plans/" + proposal.suite + ".json", canonical_bytes(proposal))
        if starting:
            self._append(
                proposal=proposal,
                case=proposal.calls[0],
                kind="start",
                reason="suite_started",
                now=started_at,
                prior=prior,
            )

    def events(self) -> list[Event]:
        directory = self.root / "events"
        records: list[Event] = []
        previous = "0" * 64
        for index, path in enumerate(sorted(directory.glob("*.json"))):
            if (
                path.is_symlink()
                or not path.is_file()
                or path.name != f"{index:06d}.json"
            ):
                raise ValueError("Budget journal inventory differs")
            raw = path.read_bytes()
            event = Event.model_validate_json(raw)
            if (
                canonical_bytes(event) != raw
                or event.sequence != index
                or event.previous_sha256 != previous
                or (records and event.at < records[-1].at)
            ):
                raise ValueError("Budget journal commitment differs")
            previous = bytes_digest(raw)
            records.append(event)
        reservations = [e for e in records if e.kind == "reserve"]
        for suite in {e.suite for e in records}:
            if suite not in SUITES:
                raise ValueError("Unknown campaign suite")
            proposal = Proposal.model_validate_json(
                (self.root / "plans" / (suite + ".json")).read_bytes()
            )
            authorization = Authorization.model_validate_json(
                (self.root / "authorizations" / (suite + ".json")).read_bytes()
            )
            authorization.verify(proposal)
            digest = authorization.proposal_sha256
            starts = [e for e in records if e.suite == suite and e.kind == "start"]
            if len(starts) != 1:
                raise ValueError("Suite start/deadline evidence differs")
            if (
                self.root / "suite-intents" / (suite + ".json")
            ).read_bytes() != canonical_bytes(
                {
                    "proposal_sha256": digest,
                    "authorization_sha256": bytes_digest(
                        canonical_bytes(authorization)
                    ),
                    "started_at": starts[0].at.isoformat(),
                }
            ):
                raise ValueError("Suite start/deadline evidence differs")
            by_id = {c.case_id: c for c in proposal.calls}
            for event in (e for e in records if e.suite == suite):
                case = by_id.get(event.case_id)
                if case is None or (
                    event.proposal_sha256,
                    event.reserved_usd,
                    event.input_tokens,
                    event.output_tokens,
                    event.seconds,
                ) != (
                    digest,
                    case.reserved_usd,
                    case.reserved_input_tokens,
                    case.reserved_output_tokens,
                    case.operation_deadline_seconds,
                ):
                    raise ValueError("Budget event differs from its immutable proposal")
            selected = [e for e in reservations if e.suite == suite]
            if len(selected) > len(proposal.calls):
                raise ValueError("Budget call count differs")
            for event, case in zip(selected, proposal.calls, strict=False):
                if (
                    event.proposal_sha256,
                    event.case_id,
                    event.reserved_usd,
                    event.input_tokens,
                    event.output_tokens,
                    event.seconds,
                ) != (
                    digest,
                    case.case_id,
                    case.reserved_usd,
                    case.reserved_input_tokens,
                    case.reserved_output_tokens,
                    case.operation_deadline_seconds,
                ):
                    raise ValueError(
                        "Budget reservation differs from its immutable proposal"
                    )
        expected = {
            self._intent_name(e.suite, e.case_id): canonical_bytes(e)
            for e in reservations
        }
        intents = {
            p.name: p.read_bytes()
            for p in (self.root / "forward-intents").glob("*.json")
            if not p.is_symlink()
        }
        if expected != intents:
            raise BudgetStop("Unreconciled forwarding intent; reservations remain held")
        if {p.stem for p in (self.root / "suite-intents").glob("*.json")} != {
            e.suite for e in records
        }:
            raise BudgetStop(
                "Authorized suite start is incomplete; reconciliation required"
            )
        self._checkpoint()
        return records

    def _append(
        self,
        *,
        proposal: Proposal,
        case: Case,
        kind: Literal["start", "reserve", "terminal", "hold", "cancel"],
        reason: str,
        now: datetime,
        evidence: str | None = None,
        prior: list[Event] | None = None,
    ) -> Event:
        self._require_lock()
        prior = self.events() if prior is None else prior
        event = Event(
            sequence=len(prior),
            previous_sha256=bytes_digest(canonical_bytes(prior[-1]))
            if prior
            else "0" * 64,
            at=now,
            kind=kind,
            suite=proposal.suite,
            proposal_sha256=proposal.digest,
            case_id=case.case_id,
            reserved_usd=case.reserved_usd or Decimal(0),
            input_tokens=case.reserved_input_tokens,
            output_tokens=case.reserved_output_tokens,
            seconds=case.operation_deadline_seconds,
            reason=reason,
            evidence_sha256=evidence,
        )
        if kind == "reserve":
            # A crash between intent and journal consumes authority and blocks replay.
            self._publish(
                "forward-intents/" + self._intent_name(proposal.suite, case.case_id),
                canonical_bytes(event),
            )
        self._publish(f"events/{event.sequence:06d}.json", canonical_bytes(event))
        return event

    @staticmethod
    def _intent_name(suite: str, case_id: str) -> str:
        return bytes_digest(canonical_bytes([suite, case_id])) + ".json"

    def reserve(
        self,
        proposal: Proposal,
        case: Case,
        *,
        now: datetime | None = None,
        commit: bool = True,
    ) -> Event | None:
        self._require_lock()
        now = now or datetime.now(UTC)
        authorization = Authorization.model_validate_json(
            (self.root / "authorizations" / (proposal.suite + ".json")).read_bytes()
        )
        authorization.verify(proposal)
        digest = authorization.proposal_sha256
        if case not in proposal.calls or case.reserved_usd is None or case.blockers:
            raise BudgetStop("Case is outside the authorized eligible plan")
        events = self.events()
        if events and now < events[-1].at:
            raise BudgetStop("Clock moved backwards")
        if any(e.kind in {"hold", "cancel"} for e in events):
            raise BudgetStop("Campaign has a durable review hold or cancellation")
        reserved = [e for e in events if e.kind == "reserve"]
        terminal = {(e.suite, e.case_id) for e in events if e.kind == "terminal"}
        if any((e.suite, e.case_id) not in terminal for e in reserved):
            raise BudgetStop("Prior submission remains uncertain")
        suite = [e for e in reserved if e.suite == proposal.suite]
        if (
            len(suite) >= len(proposal.calls)
            or case != proposal.calls[len(suite)]
            or any(e.proposal_sha256 != digest for e in suite)
        ):
            raise BudgetStop("Case order, identity or call allocation differs")
        started = next(
            e.at for e in events if e.kind == "start" and e.suite == proposal.suite
        )
        if now + timedelta(
            seconds=case.operation_deadline_seconds
        ) > started + timedelta(seconds=proposal.elapsed_seconds):
            raise BudgetStop("Suite deadline cannot cover the next request")
        limits: tuple[tuple[int | Decimal, int | Decimal, str], ...] = (
            (len(reserved) + 1, 66, "Campaign calls"),
            (len(suite) + 1, proposal.max_calls, "Suite calls"),
            (
                sum(e.seconds for e in reserved) + case.operation_deadline_seconds,
                39600,
                "Campaign active time",
            ),
            (
                sum(e.reserved_usd for e in reserved) + case.reserved_usd,
                Decimal(10),
                "Campaign USD",
            ),
            (
                sum(e.reserved_usd for e in suite) + case.reserved_usd,
                proposal.ceiling_usd,
                "Suite USD",
            ),
            (
                sum(e.input_tokens + e.output_tokens for e in suite)
                + case.reserved_input_tokens
                + case.reserved_output_tokens,
                proposal.max_calls * (32768 + 8192),
                "Suite tokens",
            ),
        )
        for consumed, ceiling, label in limits:
            if consumed > ceiling:
                raise BudgetStop(label + " ceiling cannot cover the next request")
        if not commit:
            return None
        return self._append(
            proposal=proposal,
            case=case,
            kind="reserve",
            reason="handoff_reserved",
            now=now,
        )

    def finish(
        self,
        proposal: Proposal,
        case: Case,
        *,
        terminal: bool,
        reason: str,
        evidence: str,
        now: datetime | None = None,
    ) -> None:
        events = self.events()
        selected = [
            e
            for e in events
            if e.suite == proposal.suite
            and e.case_id == case.case_id
            and e.kind != "start"
        ]
        if (
            not selected
            or selected[0].kind != "reserve"
            or any(e.kind == "terminal" for e in selected)
        ):
            raise BudgetStop("Reservation cannot be completed twice")
        if len(selected) > 1 and not terminal:
            return
        now = now or datetime.now(UTC)
        if now < selected[0].at or now > selected[0].at + timedelta(
            seconds=case.operation_deadline_seconds
        ):
            terminal, reason = False, "operation_deadline_exceeded"
        self._append(
            proposal=proposal,
            case=case,
            kind="terminal" if terminal else "hold",
            reason=reason,
            now=now,
            evidence=evidence,
        )

    def cancel(self, proposal: Proposal, *, now: datetime | None = None) -> None:
        authorization = Authorization.model_validate_json(
            (self.root / "authorizations" / (proposal.suite + ".json")).read_bytes()
        )
        authorization.verify(proposal)
        self._append(
            proposal=proposal,
            case=proposal.calls[0],
            kind="cancel",
            reason="operator_cancelled",
            now=now or datetime.now(UTC),
        )

    def stop(self, proposal: Proposal, case: Case, reason: str) -> None:
        self._append(
            proposal=proposal,
            case=case,
            kind="hold",
            reason=reason,
            now=datetime.now(UTC),
        )

    def _require_lock(self) -> None:
        if not self._held:
            raise ValueError("Campaign operation requires exclusive ownership")
