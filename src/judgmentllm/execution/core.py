"""Bounded execution and evidence-based recovery; internal Subphase 3 services.

All preparation/repository/receipt operations live on one dedicated I/O thread.
The event loop holds only bounded queues and a fixed set of provider workers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import ParamSpec, TypeVar

from pydantic import Field

from judgmentllm.config import RunSpecification
from judgmentllm.execution.retry import DEFAULT_RETRY_POLICY, RetryPolicy
from judgmentllm.execution.session import ExecutionSession as _Session
from judgmentllm.execution.session import ExecutionWork as _Work
from judgmentllm.execution.session import Fault
from judgmentllm.persistence.models import (
    ExecutionModel,
    ExecutionSummary,
    IntegrityError,
)
from judgmentllm.providers import (
    ProviderAcknowledgment,
    ProviderAdapter,
    ProviderResponse,
    RetrievalUnavailable,
    SubmissionUncertain,
    TransientNotSubmitted,
    TransportNotSubmitted,
)

T = TypeVar("T")
P = ParamSpec("P")


class ExecutionReport(ExecutionModel):
    """Content-free execution diagnostics; unresolved work never claims completion."""

    summary: ExecutionSummary
    recovery_required: bool
    worker_count: int = Field(ge=1)
    request_queue_high_water: int = Field(ge=0)
    result_queue_high_water: int = Field(ge=0)


async def execute_run(
    run_id: str,
    *,
    store: str | Path,
    adapter: ProviderAdapter,
    spec: RunSpecification | None = None,
    use_frozen_snapshot: bool = False,
    fault: Fault | None = None,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> ExecutionReport:
    """Execute/recover prepared work and durable eligible retry operations.

    Caller must have prepared a durable run. Failures expose content-free
    exceptions; a later call reopens and reconciles original work under ownership.
    """
    loop = asyncio.get_running_loop()
    io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="judgmentllm-io")

    async def call(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        return await loop.run_in_executor(io, partial(function, *args, **kwargs))

    holder: list[_Session] = []

    def initialize() -> _Session:
        created = _Session(run_id, store, adapter, spec, use_frozen_snapshot, fault)
        holder.append(created)
        return created

    def close() -> None:
        if holder:
            holder.pop().close()

    def cancel() -> None:
        if holder:
            holder[0].cancel()

    async def drain(function: Callable[[], None]) -> None:
        # A cancelled caller must not cancel a queued ownership cleanup. Queue
        # it behind initialization and all durable writes on the same I/O thread.
        pending_cleanup = loop.run_in_executor(io, function)
        while not pending_cleanup.done():
            try:
                await asyncio.shield(pending_cleanup)
            except asyncio.CancelledError:
                continue
        pending_cleanup.result()

    try:
        session = await call(initialize)
        session.retry_policy = retry_policy
        count = session.concurrency
        pending: asyncio.Queue[_Work | None] = asyncio.Queue(maxsize=2 * count)
        responses: asyncio.Queue[tuple[_Work, ProviderResponse] | None] = asyncio.Queue(
            maxsize=count
        )
        request_high = result_high = 0

        async def produce() -> None:
            nonlocal request_high
            while (work := await call(session.next_work)) is not None:
                await pending.put(work)
                request_high = max(request_high, pending.qsize())
            for _ in range(count):
                await pending.put(None)

        async def worker() -> None:
            nonlocal result_high
            while (work := await pending.get()) is not None:
                try:
                    if await call(session.boundary, work):
                        acknowledgment = await adapter.submit(
                            work.prepared,
                            submission_id=work.submission_id,
                            correlation_id=work.correlation_id,
                        )
                        await call(
                            session.checkpoint,
                            "after_submit",
                            work.prepared.request_uid,
                        )
                        await call(session.acknowledge, work, acknowledgment)
                    else:
                        saved = await call(session.saved_response, work)
                        if saved is not None:
                            saved_ack = await call(session.saved_acknowledgment, work)
                            if saved_ack is None:
                                raise IntegrityError(
                                    "retained response lacks acknowledgment evidence"
                                )
                            await call(session.acknowledge, work, saved_ack)
                            await responses.put((work, saved))
                            result_high = max(result_high, responses.qsize())
                            continue
                        if await call(session.has_results, work):
                            continue
                        acknowledgment = await call(session.saved_acknowledgment, work)
                        if acknowledgment is None:
                            evidence = await adapter.reconcile(
                                work.prepared,
                                submission_id=work.submission_id,
                                correlation_id=work.correlation_id,
                            )
                            if evidence.outcome == "not_submitted":
                                await call(session.not_submitted, work, invoked=False)
                                continue
                            if evidence.outcome == "unresolved":
                                await call(session.uncertain, work)
                                continue
                            if (
                                evidence.remote_id is None
                                or evidence.observation_key is None
                            ):
                                raise IntegrityError(
                                    "accepted reconciliation lacks identity"
                                )
                            acknowledgment = ProviderAcknowledgment(
                                remote_id=evidence.remote_id,
                                observation_key=evidence.observation_key,
                            )
                        await call(session.acknowledge, work, acknowledgment)
                    response = await adapter.retrieve(
                        work.prepared,
                        submission_id=work.submission_id,
                        correlation_id=work.correlation_id,
                    )
                    if response.observation_key != acknowledgment.observation_key:
                        raise IntegrityError(
                            "retrieved observation differs from accepted identity"
                        )
                    await responses.put((work, response))
                    result_high = max(result_high, responses.qsize())
                except TransientNotSubmitted as exc:
                    await call(
                        session.not_submitted,
                        work,
                        invoked=True,
                        transient=True,
                        retry_after=exc.retry_after,
                        usage=exc.usage,
                    )
                except TransportNotSubmitted as exc:
                    await call(
                        session.not_submitted, work, invoked=True, usage=exc.usage
                    )
                except (SubmissionUncertain, RetrievalUnavailable, TimeoutError):
                    await call(session.uncertain, work)
            await responses.put(None)

        async def consume() -> None:
            finished = 0
            while finished < count:
                item = await responses.get()
                if item is None:
                    finished += 1
                else:
                    await call(session.process, *item)

        while True:
            async with asyncio.TaskGroup() as group:
                group.create_task(produce())
                group.create_task(consume())
                for _ in range(count):
                    group.create_task(worker())
            delay = await call(session.retry_delay)
            if delay is None:
                break
            await asyncio.sleep(delay)
            await call(session.restart_retry_wave)
        summary = await call(session.finish)
        return ExecutionReport(
            summary=summary,
            recovery_required=bool(summary.unresolved_submissions),
            worker_count=count,
            request_queue_high_water=request_high,
            result_queue_high_water=result_high,
        )
    except asyncio.CancelledError:
        await drain(cancel)
        raise
    finally:
        # Queued behind any initialization/in-flight write on the same thread.
        await drain(close)
        io.shutdown(wait=True, cancel_futures=True)


async def recover_run(
    run_id: str,
    *,
    store: str | Path,
    adapter: ProviderAdapter,
    spec: RunSpecification | None = None,
    use_frozen_snapshot: bool = False,
    fault: Fault | None = None,
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> ExecutionReport:
    """Reconcile original work and authorized retries; never resubmit uncertainty."""
    return await execute_run(
        run_id,
        store=store,
        adapter=adapter,
        spec=spec,
        use_frozen_snapshot=use_frozen_snapshot,
        fault=fault,
        retry_policy=retry_policy,
    )
