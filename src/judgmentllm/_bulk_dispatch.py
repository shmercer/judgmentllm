"""One-shot scoring authority and the per-owner rolling quota window."""

from __future__ import annotations

import asyncio
import os
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from time import monotonic

from judgmentllm.config import BulkLimits
from judgmentllm.persistence.bulk import BulkReservation
from judgmentllm.providers.protocol import PreparedRequest, ProviderIntegrityError

_MINUTE = 60.0


class BulkTokenBoundError(ValueError):
    """A synthetic request could not fit under its sealed token cap."""


@dataclass(frozen=True)
class QuotaWait:
    deadline: float | None


class BulkQuotaDeferred(Exception):
    def __init__(self, wait: QuotaWait) -> None:
        super().__init__("bulk quota unavailable")
        self.wait = wait


@dataclass(eq=False)
class BulkPermit:
    owner: BulkDispatch
    reservation: BulkReservation
    used: bool = False
    sending: bool = False
    retired: bool = False


_permit: ContextVar[BulkPermit | None] = ContextVar("bulk_permit", default=None)


@contextmanager
def presenting(permit: BulkPermit) -> Generator[None]:
    token = _permit.set(permit)
    try:
        yield
    finally:
        _permit.reset(token)
        if os.getpid() == permit.owner.pid:
            permit.owner.retire(permit)


@contextmanager
def scoring_forward(prepared: PreparedRequest, submission_id: str) -> Generator[None]:
    """Wrap the delegated send and its response stream, including cancellation."""
    permit = _permit.get()
    if not prepared.bulk_live and permit is None:
        yield
        return
    if not prepared.bulk_live or permit is None:
        raise ProviderIntegrityError("bulk-live requires a committed send permit")
    owner = permit.owner
    if os.getpid() != owner.pid:
        raise ProviderIntegrityError("bulk-live permit belongs to another process")
    with owner.lock:
        receipt = permit.reservation
        if (
            not owner.open
            or permit not in owner.pending
            or permit.retired
            or permit.used
            or receipt.submission_id != submission_id
            or receipt.request_uid != prepared.request_uid
            or receipt.payload_sha256 != prepared.payload_sha256
        ):
            raise ProviderIntegrityError("bulk-live send permit differs or has closed")
        permit.used = permit.sending = True
    try:
        yield
    finally:
        owner.end_send(permit)


class BulkDispatch:
    """The session is the sole journal writer. Guards settle charges on the loop."""

    def __init__(
        self,
        owner_token: str,
        limits: BulkLimits | None,
        *,
        prior_debits: bool,
    ) -> None:
        self.owner_token = owner_token
        self.pid = os.getpid()
        self.rpm = limits.requests_per_minute if limits else None
        self.tpm = limits.tokens_per_minute if limits else None
        self.loop = asyncio.get_running_loop()
        self.lock = Lock()
        self.pending: set[BulkPermit] = set()
        self.open = True
        self.changed = asyncio.Event()
        self._held_until = (
            monotonic() + _MINUTE if prior_debits and self.capped else 0.0
        )
        self._cooling: deque[tuple[float, int]] = deque()
        self._requests = 0
        self._tokens = 0

    @property
    def capped(self) -> bool:
        return self.rpm is not None or self.tpm is not None

    def require_open(self) -> None:
        if not self.open:
            raise asyncio.CancelledError

    def check(self, tokens: int) -> QuotaWait | None:
        """The final check runs inside the serialized, owner-checked transaction."""
        with self.lock:
            self.require_open()
            now = monotonic()
            if now < self._held_until:
                return QuotaWait(self._held_until)
            while self._cooling and self._cooling[0][0] <= now:
                _, expired_tokens = self._cooling.popleft()
                self._requests -= 1
                self._tokens -= expired_tokens
            if (self.rpm is not None and self._requests + 1 > self.rpm) or (
                self.tpm is not None and self._tokens + tokens > self.tpm
            ):
                return QuotaWait(self._cooling[0][0] if self._cooling else None)
            return None

    def require_capacity(self, tokens: int) -> None:
        if (wait := self.check(tokens)) is not None:
            raise BulkQuotaDeferred(wait)

    def committed(self, receipt: BulkReservation) -> BulkPermit:
        with self.lock:
            permit = BulkPermit(self, receipt)
            self.pending.add(permit)
            self._requests += 1
            self._tokens += receipt.tokens or 0
            if not self.open:
                # The I/O thread can finish a commit after shutdown began.
                permit.retired = True
                self._settle(permit)
            return permit

    def _signal(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def _settle(self, permit: BulkPermit) -> None:
        self.pending.remove(permit)
        tokens = permit.reservation.tokens or 0
        if self.capped:
            self._cooling.append((monotonic() + _MINUTE, tokens))
        else:
            self._requests -= 1
            self._tokens -= tokens
        self.loop.call_soon_threadsafe(self._signal)

    def retire(self, permit: BulkPermit) -> None:
        with self.lock:
            if permit in self.pending:
                permit.retired = True
                if not permit.sending:
                    self._settle(permit)

    def end_send(self, permit: BulkPermit) -> None:
        with self.lock:
            permit.sending = False
            if permit.retired:
                self._settle(permit)

    def stop(self) -> None:
        with self.lock:
            self.open = False

    async def drain(self) -> None:
        """Call only after all queued debit writes are settled."""
        with self.lock:
            for permit in tuple(self.pending):
                permit.retired = True
                if not permit.sending:
                    self._settle(permit)
        while True:
            changed = self.changed
            with self.lock:
                if not self.pending:
                    return
            await changed.wait()

    async def wait(self, capacity: QuotaWait, changed: asyncio.Event) -> None:
        if capacity.deadline is None:
            await changed.wait()
        else:
            try:
                async with asyncio.timeout(max(0.0, capacity.deadline - monotonic())):
                    await changed.wait()
            except TimeoutError:
                pass
