"""Bounded execution and evidence-based recovery; internal Subphase 3 services.

All preparation/repository/receipt operations live on one dedicated I/O thread.
The event loop holds only bounded queues and a fixed set of provider workers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import ParamSpec, TypeVar

from pydantic import Field

from judgmentllm._bulk_dispatch import (
    BulkDispatch,
    BulkPermit,
    BulkQuotaDeferred,
    presenting,
)
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
from judgmentllm.providers.protocol import (
    GeminiDecisionsRejectionCancelled,
    GeminiDecisionsRequestRejected,
    ProviderInvocationCancelled,
    TypeSafeRejectionCancelled,
    TypeSafeRequestRejected,
)


@dataclass
class _CancellationWork:
    work: _Work
    acknowledgment: ProviderAcknowledgment | None = None
    response: ProviderResponse | None = None
    invoked: bool = False
    rejection_write: asyncio.Future[None] | None = None
    drained_failure: TransportNotSubmitted | SubmissionUncertain | None = None


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
    execution_task = asyncio.current_task()
    io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="judgmentllm-io")

    async def call(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        return await loop.run_in_executor(io, partial(function, *args, **kwargs))

    holder: list[_Session] = []
    # Values below alias native Decisions workers/queue objects until processing;
    # the map is bounded by those existing buffers, never the corpus size.
    cancellation_handoff = getattr(adapter, "_received_response_for_cancellation", None)
    cancellation_work: dict[str, _CancellationWork] = {}
    bulk: BulkDispatch | None = None
    bulk_drained = False
    drain_cancellation: asyncio.CancelledError | None = None

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

    async def drain_pending(pending_cleanup: asyncio.Future[T]) -> T:
        nonlocal drain_cancellation
        while not pending_cleanup.done():
            try:
                await asyncio.shield(pending_cleanup)
            except asyncio.CancelledError as error:
                if asyncio.current_task() is execution_task:
                    drain_cancellation = error
                continue
        return pending_cleanup.result()

    async def drain(function: Callable[[], None]) -> None:
        # Queue cleanup behind initialization and durable writes on the owned thread.
        await drain_pending(loop.run_in_executor(io, function))

    def queue_rejection(
        session: _Session, work: _Work, error: TransportNotSubmitted, *, invoked: bool
    ) -> asyncio.Future[None]:
        return loop.run_in_executor(
            io,
            partial(
                session.not_submitted,
                work,
                invoked=invoked,
                usage=error.usage,
                diagnostic_code=error.diagnostic_code,
                transient=isinstance(error, TransientNotSubmitted),
                retry_after=error.retry_after
                if isinstance(error, TransientNotSubmitted)
                else None,
                rejection=error.rejection
                if isinstance(
                    error, (TypeSafeRequestRejected, GeminiDecisionsRequestRejected)
                )
                else None,
            ),
        )

    async def retain_known_work(*, preserve_original_error: bool = False) -> None:
        """Drain bounded native Decisions handoffs after TaskGroup stops workers.

        A worker fault can cancel another already invoked request just as caller
        cancellation can. Cleanup never submits work or invents an invocation.
        If durable storage itself failed, keep the original worker error while
        attempting independent retained work where possible.
        """
        session = holder[0]
        for submission_id, retained in tuple(cancellation_work.items()):
            if retained.rejection_write is not None:
                try:
                    await drain_pending(retained.rejection_write)
                except Exception:
                    # Keep the initiating cancellation or worker error.
                    pass
                cancellation_work.pop(submission_id)
        for retained in tuple(cancellation_work.values()):
            try:
                work = retained.work
                failure = retained.drained_failure
                if isinstance(failure, TransportNotSubmitted):
                    await drain_pending(
                        queue_rejection(
                            session, work, failure, invoked=retained.invoked
                        )
                    )
                    continue
                if isinstance(failure, SubmissionUncertain):
                    await drain(
                        partial(
                            session.uncertain,
                            work,
                            invoked=retained.invoked,
                            usage=failure.usage,
                        )
                    )
                    continue
                response = retained.response
                ack_handoff = getattr(
                    adapter, "_received_acknowledgment_for_cancellation", None
                )
                if retained.acknowledgment is None and ack_handoff is not None:
                    retained.acknowledgment = ack_handoff(
                        work.prepared,
                        submission_id=work.submission_id,
                        correlation_id=work.correlation_id,
                    )
                    retained.invoked |= retained.acknowledgment is not None
                if response is None and retained.acknowledgment is not None:
                    if cancellation_handoff is not None:
                        response = cancellation_handoff(
                            work.prepared,
                            submission_id=work.submission_id,
                            correlation_id=work.correlation_id,
                        )
                    else:
                        try:
                            response = await adapter.retrieve(
                                work.prepared,
                                submission_id=work.submission_id,
                                correlation_id=work.correlation_id,
                            )
                        except RetrievalUnavailable:
                            pass
                if response is not None and retained.acknowledgment is not None:
                    if (
                        response.observation_key
                        != retained.acknowledgment.observation_key
                    ):
                        raise IntegrityError(
                            "drained response differs from acknowledgment"
                        )
                elif bulk is not None and retained.acknowledgment is not None:
                    await drain(
                        partial(session.acknowledge, work, retained.acknowledgment)
                    )
                await drain(
                    partial(
                        session.cancelled_invocation,
                        work,
                        acknowledgment=retained.acknowledgment,
                        response=response,
                        known_invoked=retained.invoked,
                    )
                )
            except Exception:
                if not preserve_original_error:
                    raise
        cancellation_work.clear()

    async def settle_bulk() -> None:
        nonlocal bulk_drained
        if bulk is not None and not bulk_drained:
            bulk.stop()
            await drain(lambda: None)
            await drain_pending(asyncio.create_task(bulk.drain()))
            bulk_drained = True

    async def submit_bulk(
        permit: BulkPermit, retained: _CancellationWork
    ) -> ProviderAcknowledgment:
        work = retained.work
        with presenting(permit):
            permit.owner.require_open()
            flight = asyncio.create_task(
                adapter.submit(
                    work.prepared,
                    submission_id=work.submission_id,
                    correlation_id=work.correlation_id,
                )
            )
            try:
                return await asyncio.shield(flight)
            except asyncio.CancelledError:
                permit.owner.stop()
                if not permit.used and not flight.done():
                    flight.cancel()
                try:
                    retained.acknowledgment = await drain_pending(flight)
                except (
                    TypeSafeRejectionCancelled,
                    GeminiDecisionsRejectionCancelled,
                ) as exc:
                    retained.drained_failure = exc.rejection
                except (TransportNotSubmitted, SubmissionUncertain) as exc:
                    retained.drained_failure = exc
                except Exception:
                    # Preserve the initiating failure. The owned debit remains unresolved.
                    pass
                except asyncio.CancelledError:
                    pass
                retained.invoked = permit.used
                worker_task = asyncio.current_task()
                if (
                    worker_task is not None
                    and not worker_task.cancelling()
                    and execution_task is not None
                    and not execution_task.cancelling()
                ):
                    execution_task.cancel()
                raise

    try:
        session = await call(initialize)
        if session.bulk_live:
            bulk = BulkDispatch(
                session.token,
                session.spec.execution.bulk_limits,
                prior_debits=session.prior_debits,
            )
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

        async def deliver_response(work: _Work, response: ProviderResponse) -> None:
            nonlocal result_high
            tier = response.evidence.service_tier if response.evidence else None
            # Persist and interpret this observation before admitting more work.
            if session.tier_review_required or (
                tier is not None
                and (tier.requires_tier_review or tier.outcome == "unexpected")
            ):
                await call(session.process, work, response)
            else:
                await responses.put((work, response))
                result_high = max(result_high, responses.qsize())

        async def worker() -> None:
            while (work := await pending.get()) is not None:
                retained = (
                    _CancellationWork(work)
                    if cancellation_handoff is not None or bulk is not None
                    else None
                )
                if retained is not None:
                    cancellation_work[work.submission_id] = retained
                permit = None
                try:
                    if bulk is None:
                        admitted = await call(session.boundary, work)
                    else:
                        while True:
                            changed = bulk.changed
                            try:
                                admitted = await call(session.boundary, work, bulk)
                            except BulkQuotaDeferred as deferred:
                                await bulk.wait(deferred.wait, changed)
                            else:
                                break
                    if admitted:
                        if isinstance(admitted, BulkPermit):
                            assert retained is not None
                            permit = admitted
                            acknowledgment = await submit_bulk(permit, retained)
                        else:
                            acknowledgment = await adapter.submit(
                                work.prepared,
                                submission_id=work.submission_id,
                                correlation_id=work.correlation_id,
                            )
                        if retained is not None:
                            retained.acknowledgment = acknowledgment
                            retained.invoked = True
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
                            if retained is not None:
                                retained.acknowledgment = saved_ack
                                retained.response = saved
                            await deliver_response(work, saved)
                            continue
                        if await call(session.has_results, work):
                            cancellation_work.pop(work.submission_id, None)
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
                                cancellation_work.pop(work.submission_id, None)
                                continue
                            if evidence.outcome == "unresolved":
                                await call(session.uncertain, work)
                                cancellation_work.pop(work.submission_id, None)
                                continue
                            from judgmentllm.providers.protocol import (
                                DecisionsReconciliationEvidenceV1,
                            )

                            if (
                                isinstance(evidence, DecisionsReconciliationEvidenceV1)
                                and evidence.acknowledgment is not None
                            ):
                                acknowledgment = evidence.acknowledgment
                            elif (
                                evidence.remote_id is None
                                or evidence.observation_key is None
                            ):
                                raise IntegrityError(
                                    "accepted reconciliation lacks identity"
                                )
                            else:
                                acknowledgment = ProviderAcknowledgment(
                                    remote_id=evidence.remote_id,
                                    observation_key=evidence.observation_key,
                                )
                        await call(session.acknowledge, work, acknowledgment)
                    if retained is not None:
                        retained.acknowledgment = acknowledgment
                    response = await adapter.retrieve(
                        work.prepared,
                        submission_id=work.submission_id,
                        correlation_id=work.correlation_id,
                    )
                    if retained is not None:
                        retained.response = response
                    if response.observation_key != acknowledgment.observation_key:
                        raise IntegrityError(
                            "retrieved observation differs from accepted identity"
                        )
                    await deliver_response(work, response)
                except ProviderInvocationCancelled as exc:
                    if retained is not None:
                        retained.invoked = exc.invoked
                    if isinstance(
                        exc,
                        (TypeSafeRejectionCancelled, GeminiDecisionsRejectionCancelled),
                    ):
                        assert retained is not None
                        retained.rejection_write = queue_rejection(
                            session, work, exc.rejection, invoked=exc.invoked
                        )
                    # A provider task may itself be cancelled. TaskGroup treats
                    # child cancellation as normal, so propagate it to the owning
                    # execution task rather than leaving its consumer waiting.
                    if execution_task is not None and not execution_task.cancelling():
                        execution_task.cancel()
                    raise
                except TransportNotSubmitted as exc:
                    write = queue_rejection(
                        session,
                        work,
                        exc,
                        invoked=permit.used if permit is not None else True,
                    )
                    if bulk is not None or isinstance(
                        exc, (TypeSafeRequestRejected, GeminiDecisionsRequestRejected)
                    ):
                        assert retained is not None
                        retained.rejection_write = write
                        await asyncio.shield(write)
                    else:
                        await write
                    cancellation_work.pop(work.submission_id, None)
                except SubmissionUncertain as exc:
                    await call(
                        session.uncertain, work, invoked=exc.invoked, usage=exc.usage
                    )
                    cancellation_work.pop(work.submission_id, None)
                except (RetrievalUnavailable, TimeoutError):
                    await call(session.uncertain, work)
                    cancellation_work.pop(work.submission_id, None)
            await responses.put(None)

        async def consume() -> None:
            finished = 0
            while finished < count:
                item = await responses.get()
                if item is None:
                    finished += 1
                else:
                    await call(session.process, *item)
                    cancellation_work.pop(item[0].submission_id, None)

        async def stop_on_failure(
            operation: Callable[[], Coroutine[None, None, None]],
        ) -> None:
            try:
                await operation()
            except BaseException:
                if bulk is not None:
                    bulk.stop()
                raise

        while True:
            async with asyncio.TaskGroup() as group:
                group.create_task(stop_on_failure(produce))
                group.create_task(stop_on_failure(consume))
                for _ in range(count):
                    group.create_task(stop_on_failure(worker))
            delay = await call(session.retry_delay)
            if delay is None:
                break
            await asyncio.sleep(delay)
            await call(session.restart_retry_wave)
        summary = await call(session.finish)
        report = ExecutionReport(
            summary=summary,
            recovery_required=bool(summary.unresolved_submissions),
            worker_count=count,
            request_queue_high_water=request_high,
            result_queue_high_water=result_high,
        )
    except asyncio.CancelledError:
        await settle_bulk()
        if cancellation_work:
            await drain_pending(asyncio.create_task(retain_known_work()))
        await drain(cancel)
        raise
    except ExceptionGroup:
        # Ordinary worker errors also cancel sibling SDK calls. Preserve their
        # established evidence without changing the original failure into a
        # successful report or classifying the run as caller-cancelled.
        await settle_bulk()
        if cancellation_work:
            await drain_pending(
                asyncio.create_task(retain_known_work(preserve_original_error=True))
            )
        raise
    finally:
        await settle_bulk()
        try:
            if bulk is not None:
                from judgmentllm.lifecycle import _close_adapter  # pyright: ignore[reportPrivateUsage]

                await drain_pending(asyncio.create_task(_close_adapter(adapter)))
        finally:
            # All possible scoring sends have ended or are fenced before unlock.
            await drain(close)
            io.shutdown(wait=True, cancel_futures=True)
    if bulk is not None and drain_cancellation is not None:
        raise drain_cancellation
    return report


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
