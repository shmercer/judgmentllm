"""Test-local virtual deadlines with an independent wall-clock wakeup."""

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from threading import Timer
from typing import TypeVar

import pytest

from judgmentllm.batch_models import BatchControlOptionsV1, BatchRunStatusV1
from judgmentllm.execution.native_batch import InvocationBudget

T = TypeVar("T")


class BatchClock:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.start = self.now = loop.time()
        self.budgets: list[InvocationBudget] = []

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        assert seconds >= 0
        self.now += seconds

    async def wait(
        self,
        waiting: asyncio.Future[T],
        control: asyncio.Task[BatchRunStatusV1] | None = None,
    ) -> T:
        watchdog: asyncio.Future[None] = self.loop.create_future()

        def wake() -> None:
            if not watchdog.done():
                watchdog.set_result(None)

        timer = Timer(5, self.loop.call_soon_threadsafe, args=(wake,))
        timer.start()
        try:
            await asyncio.wait(
                (waiting, watchdog)
                if control is None
                else (waiting, watchdog, control),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if control is not None and control.done():
                control.result()
                raise AssertionError("control returned before the required hook gate")
            assert waiting.done(), "real-time watchdog expired before the required gate"
            return waiting.result()
        finally:
            timer.cancel()
            timer.join(timeout=1)
            assert not timer.is_alive(), "test watchdog did not join"
            watchdog.cancel()
            if control is not None:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

    async def gate(
        self, event: asyncio.Event, control: asyncio.Task[BatchRunStatusV1]
    ) -> None:
        await self.wait(asyncio.create_task(event.wait()), control)

    async def cleanup(
        self, control: asyncio.Task[BatchRunStatusV1], release: asyncio.Event
    ) -> None:
        release.set()
        if not control.done():
            control.cancel()
        await self.wait(asyncio.gather(control, return_exceptions=True))


@contextmanager
def batch_clock(
    monkeypatch: pytest.MonkeyPatch, *, expire_before_entry: bool = False
) -> Generator[BatchClock]:
    clock = BatchClock(asyncio.get_running_loop())
    original = InvocationBudget.__init__

    def capture(self: InvocationBudget, options: BatchControlOptionsV1) -> None:
        original(self, options)
        clock.budgets.append(self)
        if expire_before_entry:
            clock.advance(self.options.deadline_seconds)

    with monkeypatch.context() as patch:
        patch.setattr(clock.loop, "time", clock.time)
        patch.setattr(InvocationBudget, "__init__", capture)
        yield clock
