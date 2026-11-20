"""Real locked SDKs under the committed bulk-live transport guard."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import json
import multiprocessing
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Executor
from functools import partial
from pathlib import Path
from typing import Any

import certifi
import httpx2
import pytest
import test_anthropic_lifecycle as anthropic_fixture
import test_gemini_decisions as gemini_decisions_fixture
import test_gemini_lifecycle as gemini_fixture
import test_openai_decisions_sdk as openai_decisions_fixture
import test_openai_lifecycle as openai_fixture
import test_typesafe_decisions as jev_fixture
import test_vertex_lifecycle as vertex_fixture
import vertex_offline
from test_decisions_lifecycle import native_spec

from judgmentllm import _bulk_dispatch as clock
from judgmentllm._bulk_dispatch import BulkDispatch, BulkPermit, QuotaWait, presenting
from judgmentllm.config import (
    BulkLimits,
    GenerationSpec,
    RunSpecV3,
    RunSpecV4,
    validate_run_spec,
)
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.session import ExecutionSession, ExecutionWork
from judgmentllm.lifecycle import _offline_context
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.evidence import (
    DecisionsEvidenceReceiptV4,
    read_evidence_receipt,
)
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.ownership import executor_is_active
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.anthropic import AnthropicAdapter
from judgmentllm.providers.gemini import GeminiAdapter
from judgmentllm.providers.gemini_decisions import GeminiDecisionsAdapter
from judgmentllm.providers.gemini_decisions import decisions_context as gemini_context
from judgmentllm.providers.openai import OpenAIAdapter
from judgmentllm.providers.openai_decisions import OpenAIDecisionsAdapter
from judgmentllm.providers.openai_decisions import decisions_context as openai_context
from judgmentllm.providers.protocol import (
    ProviderAdapter,
    ProviderError,
    ProviderIntegrityError,
)
from judgmentllm.providers.typesafe_decisions import TypeSafeDecisionsAdapter
from judgmentllm.providers.typesafe_decisions import decisions_context as jev_context
from judgmentllm.providers.vertex import VertexAdapter


def probe_forked_permit(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A child inherits an unused grant, including its still-open owner flag."""
    spec = _bulk(_case(root / "inputs", "responses", monkeypatch))
    store = root / "runtime"

    def receiver(request: httpx2.Request) -> httpx2.Response:
        pytest.fail("the stale child reached the HTTP receiver")

    adapter = _adapter(spec, httpx2.MockTransport(receiver))
    _prepare(spec, store, adapter)
    session = ExecutionSession("001", store, adapter, spec, False, None)

    async def mint() -> tuple[BulkDispatch, BulkPermit, ExecutionWork]:
        work = session.next_work()
        assert work is not None
        budget = BulkDispatch(session.token, None, prior_debits=False)
        permit = session.boundary(work, budget)
        assert isinstance(permit, BulkPermit)
        return budget, permit, work

    with asyncio.Runner() as parent:
        budget, permit, work = parent.run(mint())
        ctx = multiprocessing.get_context("fork")
        released = ctx.Event()

        def child() -> None:
            assert released.wait(10)

            async def submit() -> None:
                instance = _adapter(spec, httpx2.MockTransport(receiver))
                try:
                    with (
                        presenting(permit),
                        pytest.raises(ProviderIntegrityError, match="another process"),
                    ):
                        await instance.submit(
                            work.prepared,
                            submission_id=work.submission_id,
                            correlation_id=work.correlation_id,
                        )
                finally:
                    await instance.aclose()

            asyncio.run(submit())

        process = ctx.Process(target=child)
        process.start()
        try:
            budget.stop()
            parent.run(budget.drain())
            session.close()
            released.set()
            process.join(20)
            assert process.exitcode == 0
        finally:
            if process.is_alive():
                process.terminate()
                process.join(10)
            process.close()


@pytest.mark.asyncio
async def test_bulk_sdk_self_cancellation_retains_complete_native_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _bulk(_case(tmp_path / "inputs", "jev", monkeypatch))
    root = tmp_path / "store"
    raw = b'{"detail":"Synthetic request was too large"}'
    forwards: list[bytes] = []

    class CancelAtClose(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield raw

        async def aclose(self) -> None:
            raise asyncio.CancelledError

    def receive(request: httpx2.Request) -> httpx2.Response:
        forwards.append(request.content)
        return httpx2.Response(413, stream=CancelAtClose(), request=request)

    adapter = _adapter(spec, httpx2.MockTransport(receive))
    _prepare(spec, root, adapter)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            execute_run("001", store=root, adapter=adapter, spec=spec), 10
        )
    assert len(forwards) == 1
    with ReadOnlyRepository(root) as repository:
        (receipt,) = repository.iter_evidence("001")
        assert isinstance(receipt, DecisionsEvidenceReceiptV4)
        assert receipt.rejection is not None
        assert receipt.rejection.response_sha256 == bytes_digest(raw)
        assert receipt.rejection.http_status == 413
        assert not executor_is_active(repository, "001")
        assert repository.get_run("001").state == "cancelled"


_ROUTES = (
    "responses",
    "chat.completions",
    "anthropic",
    "gemini",
    "vertex-key",
    "vertex-adc",
    "openai-decisions",
    "jev",
    "gemini-decisions",
)
_SDKAdapter = (
    OpenAIAdapter
    | AnthropicAdapter
    | GeminiAdapter
    | VertexAdapter
    | OpenAIDecisionsAdapter
    | TypeSafeDecisionsAdapter
    | GeminiDecisionsAdapter
)


def _case(
    root: Path, route: str, monkeypatch: pytest.MonkeyPatch
) -> RunSpecV3 | RunSpecV4:
    vertex_offline.isolate(monkeypatch)
    if route == "vertex-adc":
        vertex_offline.adc(monkeypatch)
    monkeypatch.setenv("SSL_CERT_FILE", certifi.where())
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    if route == "openai-decisions":
        return openai_decisions_fixture.spec_for(root, sources=2)
    if route == "jev":
        return jev_fixture.jev_spec(native_spec(root, sources=2))
    if route == "gemini-decisions":
        return gemini_decisions_fixture.gemini_spec(native_spec(root, sources=2))
    builder = {
        "anthropic": anthropic_fixture.production_spec,
        "gemini": gemini_fixture.production_spec,
        "vertex-key": vertex_fixture.production_spec,
        "vertex-adc": vertex_fixture.production_spec,
    }.get(route)
    if builder:
        return builder(root, explanations=False)
    return openai_fixture.production_spec(root, route, explanations=False)


def _bulk(spec: RunSpecV3 | RunSpecV4) -> RunSpecV3 | RunSpecV4:
    data = spec.model_dump()
    data["execution"]["mode"] = "bulk_live"
    checked = validate_run_spec(data)
    assert isinstance(checked, (RunSpecV3, RunSpecV4))
    return checked


def _adapter(
    spec: RunSpecV3 | RunSpecV4,
    transport: httpx2.AsyncBaseTransport,
) -> _SDKAdapter:
    kwargs: dict[str, Any] = dict(
        concurrency=spec.execution.concurrency, http_transport=transport
    )
    if isinstance(spec, RunSpecV4):
        if spec.provider == "openai":
            return OpenAIDecisionsAdapter(
                spec.model, context=openai_context(spec), **kwargs
            )
        if spec.provider == "gemini":
            return GeminiDecisionsAdapter(
                spec.model, context=gemini_context(spec), **kwargs
            )
        return TypeSafeDecisionsAdapter(spec.model, context=jev_context(spec), **kwargs)
    kind = {
        "openai": OpenAIAdapter,
        "anthropic": AnthropicAdapter,
        "gemini": GeminiAdapter,
        "vertex": VertexAdapter,
    }[spec.provider]
    return kind(spec.endpoint, spec.model, **kwargs)


def _prepare(spec: RunSpecV3 | RunSpecV4, root: Path, adapter: ProviderAdapter) -> None:
    with prepare_run(
        spec,
        run_id="001",
        store=root,
        capabilities=adapter.capabilities(spec),
        transport_context=_offline_context(spec),
    ):
        pass


def _response(
    spec: RunSpecV3 | RunSpecV4, request: httpx2.Request, *, partial: bool
) -> httpx2.Response:
    body: dict[str, Any]
    if isinstance(spec, RunSpecV4):
        if spec.provider == "openai":
            body = openai_decisions_fixture.envelope(json.loads(request.content))
            if partial:
                body["answers"][-1]["probabilities"][0]["probability"] = 0.9
        elif spec.provider == "gemini":
            body = gemini_decisions_fixture.envelope(json.loads(request.content))
            if partial:
                next(iter(body["answers"].values()))["probabilities"]["0"] = 0.9
        else:
            body = jev_fixture.jev_envelope(request)
            if partial:
                next(iter(body["answers"].values()))["probabilities"]["0"] = 0.9
    else:
        assert spec.rubric is not None
        scores = [
            {"trait_id": trait.trait_id, "category_label": trait.levels[0].label}
            for trait in spec.rubric.traits
        ]
        if partial:
            scores[-1]["category_label"] = "invalid"
        envelope = {
            "openai": openai_fixture.envelope,
            "anthropic": anthropic_fixture.envelope,
            "gemini": gemini_fixture.envelope,
            "vertex": vertex_fixture.envelope,
        }[spec.provider]
        body = envelope(spec.endpoint, json.dumps({"scores": scores}))
    return httpx2.Response(200, json=body, request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _ROUTES)
async def test_sdk_receiver_requires_committed_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    spec = _bulk(_case(tmp_path / "input", route, monkeypatch))
    receiver: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        receiver.append(request.content)
        return _response(spec, request, partial=False)

    def adapter() -> _SDKAdapter:
        return _adapter(spec, httpx2.MockTransport(respond))

    root = tmp_path / "runtime"
    first = adapter()
    _prepare(spec, root, first)
    session = ExecutionSession("001", root, first, spec, False, None)
    work = session.next_work()
    assert work is not None

    async def invoke(instance: _SDKAdapter, candidate: ExecutionWork) -> None:
        try:
            instance.validate_credentials()
            await instance.submit(
                candidate.prepared,
                submission_id=candidate.submission_id,
                correlation_id=candidate.correlation_id,
            )
        finally:
            await instance.aclose()

    try:
        with pytest.raises(ProviderError):
            await invoke(adapter(), work)
        assert receiver == []
        budget = BulkDispatch(session.token, None, prior_debits=False)
        permit = session.boundary(work, budget)
        assert isinstance(permit, BulkPermit)
        with ReadOnlyRepository(root) as repository:
            assert next(repository.iter_submissions("001")).state == "submitting"
        # Existing adapter duplicate guards see a fresh invocation each time.
        with presenting(permit):
            original = permit.reservation
            permit.reservation = original.model_copy(
                update={"payload_sha256": "0" * 64}
            )
            with pytest.raises(ProviderError):
                await invoke(adapter(), work)
            assert receiver == []
            permit.reservation = original
            await invoke(adapter(), work)
            with pytest.raises(ProviderError):
                await invoke(adapter(), work)
            assert receiver == [work.prepared.payload]
        budget.stop()
        await budget.drain()
    finally:
        session.close()
        await first.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("sibling_fails", [False, True])
async def test_sdk_drain_keeps_complete_responses_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sibling_fails: bool,
) -> None:
    spec = _bulk(_case(tmp_path / "inputs", "responses", monkeypatch))
    assert isinstance(spec, RunSpecV3)
    spec.execution.concurrency = 2
    spec.execution.bulk_limits = BulkLimits(requests_per_minute=2)
    with spec.input.path.open("a", encoding="utf-8") as source:
        source.write(
            json.dumps({"item_id": "9999", "text": "Second synthetic input."}) + "\n"
        )
    root = tmp_path / "runtime"
    entered = asyncio.Event()
    release = [asyncio.Event(), asyncio.Event()]
    failed = asyncio.Event()
    closed = asyncio.Event()
    returned: list[int] = []
    calls: list[bytes] = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        index = len(calls)
        calls.append(request.content)
        if len(calls) == 2:
            entered.set()
        await release[index].wait()
        returned.append(index)
        return _response(spec, request, partial=False)

    class Receiver(httpx2.MockTransport):
        async def aclose(self) -> None:
            with ReadOnlyRepository(root) as repository:
                assert executor_is_active(repository, "001") is True
                assert len(tuple(repository.iter_evidence("001"))) == 2
            closed.set()
            await super().aclose()

    adapter = _adapter(spec, Receiver(respond))
    _prepare(spec, root, adapter)
    loop = asyncio.get_running_loop()
    faulted = threading.Event()

    def fault(stage: str, _: str) -> None:
        if sibling_fails and stage == "after_submit" and not faulted.is_set():
            faulted.set()
            loop.call_soon_threadsafe(failed.set)
            raise RuntimeError("sibling failed after a complete response")

    run = asyncio.create_task(
        execute_run("001", spec=spec, store=root, adapter=adapter, fault=fault)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        if sibling_fails:
            release[0].set()
            await asyncio.wait_for(failed.wait(), timeout=10)
        else:
            run.cancel()
            await asyncio.sleep(0)
        assert not closed.is_set()
        with ReadOnlyRepository(root) as repository:
            assert executor_is_active(repository, "001") is True
    finally:
        for wait in release:
            wait.set()
    with pytest.raises(ExceptionGroup if sibling_fails else asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=15)
    assert closed.is_set() and set(returned) == {0, 1} and len(calls) == 2
    with ReadOnlyRepository(root) as repository:
        assert executor_is_active(repository, "001") is False
        assert all(row.status == "complete" for row in repository.iter_results("001"))


@pytest.mark.asyncio
async def test_sdk_queued_rejection_is_committed_before_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _bulk(_case(tmp_path / "inputs", "responses", monkeypatch))
    spec.execution.concurrency = 2
    with spec.input.path.open("a", encoding="utf-8") as source:
        source.write(
            json.dumps({"item_id": "9999", "text": "Second synthetic input."}) + "\n"
        )
    root = tmp_path / "runtime"
    both_sending = asyncio.Event()
    io_blocked = asyncio.Event()
    reject = asyncio.Event()
    write_queued = asyncio.Event()
    worker_stopped = asyncio.Event()
    release_io = threading.Event()
    calls: list[bytes] = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        first = not calls
        calls.append(request.content)
        if first:
            await both_sending.wait()
            return _response(spec, request, partial=False)
        both_sending.set()
        await reject.wait()
        return httpx2.Response(
            403, json={"error": {"message": "synthetic denial"}}, request=request
        )

    adapter = _adapter(spec, httpx2.MockTransport(respond))
    _prepare(spec, root, adapter)
    loop = asyncio.get_running_loop()
    run_in_executor = loop.run_in_executor

    def observe_enqueue(
        executor: Executor | None, function: Callable[..., Any], *args: Any
    ) -> asyncio.Future[Any]:
        future = run_in_executor(executor, function, *args)
        if (
            isinstance(function, partial)
            and getattr(function.func, "__func__", None)
            is ExecutionSession.not_submitted
        ):
            worker = asyncio.current_task()
            assert worker is not None
            worker.add_done_callback(lambda _: worker_stopped.set())
            write_queued.set()
        return future

    def block_io(stage: str, _: str) -> None:
        if stage == "after_submit":
            loop.call_soon_threadsafe(io_blocked.set)
            assert release_io.wait(15)

    monkeypatch.setattr(loop, "run_in_executor", observe_enqueue)
    run = asyncio.create_task(
        execute_run("001", spec=spec, store=root, adapter=adapter, fault=block_io)
    )
    try:
        await asyncio.wait_for(io_blocked.wait(), 10)
        reject.set()
        await asyncio.wait_for(write_queued.wait(), 10)
        run.cancel()
        await asyncio.wait_for(worker_stopped.wait(), 10)
    finally:
        both_sending.set()
        reject.set()
        release_io.set()
        if not run.done() and not run.cancelling():
            run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run, 10)

    assert len(calls) == 2
    with ReadOnlyRepository(root) as repository:
        assert {row.state for row in repository.iter_submissions("001")} == {
            "confirmed_not_submitted",
            "resolved",
        }
        receipts = list(repository.iter_evidence("001"))
        assert len(receipts) == 2
        (failure,) = (row for row in receipts if row.usage.definitive_failure_id)
        assert failure.usage.invocation_failure_code == "permission_denied"
        assert failure.usage.invocation_kind == "managed_bulk_live"
        assert not list(repository.connection.execute("SELECT * FROM retry_operations"))
        assert not executor_is_active(repository, "001")


@pytest.mark.asyncio
async def test_sdk_send_delay_stays_charged_past_one_minute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _bulk(_case(tmp_path / "inputs", "responses", monkeypatch))
    assert isinstance(spec, RunSpecV3)
    spec.execution.concurrency = 2
    spec.execution.bulk_limits = BulkLimits(requests_per_minute=1)
    with spec.input.path.open("a", encoding="utf-8") as source:
        source.write(
            json.dumps({"item_id": "9999", "text": "Second synthetic input."}) + "\n"
        )
    now = 100.0
    monkeypatch.setattr(clock, "monotonic", lambda: now)
    deadlines: asyncio.Queue[tuple[BulkDispatch, QuotaWait]] = asyncio.Queue()
    original_wait = BulkDispatch.wait

    async def observe_wait(
        owner: BulkDispatch, wait: QuotaWait, changed: asyncio.Event
    ) -> None:
        deadlines.put_nowait((owner, wait))
        await original_wait(owner, wait, changed)

    monkeypatch.setattr(BulkDispatch, "wait", observe_wait)
    entered = asyncio.Event()
    send = asyncio.Event()
    observed = asyncio.Event()
    finish = asyncio.Event()
    receiver: list[float] = []

    async def transport(request: httpx2.Request) -> httpx2.Response:
        if not receiver:
            entered.set()
            await send.wait()
            receiver.append(now)
            observed.set()
            await finish.wait()
        else:
            receiver.append(now)
        return _response(spec, request, partial=False)

    root = tmp_path / "store"
    adapter = _adapter(spec, httpx2.MockTransport(transport))
    _prepare(spec, root, adapter)
    run = asyncio.create_task(
        execute_run("001", spec=spec, store=root, adapter=adapter)
    )

    async def wait_for_charge() -> tuple[BulkDispatch, QuotaWait]:
        return await asyncio.wait_for(deadlines.get(), timeout=10)

    try:
        owner, wait = await wait_for_charge()
        assert wait == QuotaWait(None)
        await asyncio.wait_for(entered.wait(), timeout=10)
        now = 165.0
        owner._signal()
        assert (await wait_for_charge())[1] == QuotaWait(None)
        assert not receiver
        send.set()
        await asyncio.wait_for(observed.wait(), timeout=10)
        assert receiver == [165.0]
        now = 170.0
        finish.set()
        assert (await wait_for_charge())[1] == QuotaWait(230.0)
        now = 229.0
        owner._signal()
        assert (await wait_for_charge())[1] == QuotaWait(230.0)
        assert receiver == [165.0]
        now = 230.0
        owner._signal()
        await asyncio.wait_for(run, timeout=10)
        assert receiver == [165.0, 230.0]
    finally:
        send.set()
        finish.set()
        if not run.done():
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route,output_limit",
    [*((route, None) for route in _ROUTES), *((route, 128) for route in _ROUTES[:6])],
)
async def test_sdk_bulk_wire_parity_and_interrupted_partial_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    output_limit: int | None,
) -> None:
    live = _case(tmp_path / "inputs", route, monkeypatch)
    if output_limit is not None:
        assert isinstance(live, RunSpecV3)
        live.generation = GenerationSpec(max_output_tokens=output_limit)
    bodies: list[bytes] = []
    expected_wire: tuple[bytes, ...] = ()
    expected: list[tuple[str, str]] = []
    request_uids: list[set[str]] = []
    durable_uids: set[str] = set()
    interrupted_uid: str | None = None

    def fault(stage: str, request_uid: str) -> None:
        nonlocal interrupted_uid
        if stage == "after_evidence_receipt":
            durable_uids.add(request_uid)
            if interrupted_uid is None and len(durable_uids) == len(expected_wire):
                interrupted_uid = request_uid
        # Cleanup can revisit the same response before recovery starts.
        if request_uid == interrupted_uid and stage in (
            "after_evidence_receipt",
            "after_evidence_commit",
        ):
            raise RuntimeError("recover original partial response")

    for spec in (live, _bulk(live)):
        assert spec.execution.concurrency == 1
        root = tmp_path / spec.execution.mode

        def respond(
            request: httpx2.Request, current: RunSpecV3 | RunSpecV4 = spec
        ) -> httpx2.Response:
            bodies.append(request.content)
            return _response(current, request, partial=True)

        def new_adapter() -> _SDKAdapter:
            return _adapter(spec, httpx2.MockTransport(respond))  # noqa: B023

        adapter = new_adapter()
        _prepare(spec, root, adapter)
        try:
            if spec.execution.mode == "live":
                baseline = await execute_run(
                    "001", store=root, spec=spec, adapter=adapter
                )
                assert not baseline.recovery_required and bodies
                expected_wire = tuple(bodies)
            else:
                with pytest.raises(ExceptionGroup, match="TaskGroup") as stopped:
                    await execute_run(
                        "001", store=root, spec=spec, adapter=adapter, fault=fault
                    )
                assert stopped.group_contains(
                    RuntimeError, match="recover original partial response"
                )
                assert interrupted_uid is not None
                assert len(durable_uids) == len(expected_wire)
                assert tuple(bodies) == expected_wire + expected_wire
                with ReadOnlyRepository(root) as repository:
                    (interrupted,) = (
                        attempt
                        for attempt in repository.iter_attempts("001")
                        if attempt.request_uid == interrupted_uid
                    )
                    submission = repository.get_submission_for_attempt(
                        "001", interrupted.attempt_id
                    )
                    assert submission is not None
                    retained = read_evidence_receipt(
                        ArtifactStore(repository.run_directory("001")),
                        submission.submission_id,
                    )
                    assert (
                        retained is not None and retained.response_receipt is not None
                    )
                    retained.response_receipt.verify_submission(submission)
                    assert not repository.get_results_for_attempt(
                        "001", interrupted.attempt_id
                    )
                calls = len(bodies)
                for _ in range(2):
                    recovered = await recover_run(
                        "001", store=root, spec=spec, adapter=new_adapter()
                    )
                    assert not recovered.recovery_required and len(bodies) == calls
                    with ReadOnlyRepository(root) as repository:
                        assert [
                            (row.task_id, row.status)
                            for row in repository.iter_results("001")
                        ] == expected
            with ReadOnlyRepository(root) as repository:
                attempts = list(repository.iter_attempts("001"))
                request_uids.append({attempt.request_uid for attempt in attempts})
                assert len(request_uids[-1]) == len(attempts) == len(expected_wire)
                if spec.execution.mode == "live":
                    expected = [
                        (row.task_id, row.status)
                        for row in repository.iter_results("001")
                    ]
                    assert {status for _, status in expected} == {"complete", "invalid"}
                else:
                    evidence = list(repository.iter_evidence("001"))
                    assert len(evidence) == len(attempts)
                    assert {e.usage.invocation_kind for e in evidence} == {
                        "managed_bulk_live"
                    }
        finally:
            await adapter.aclose()
    assert request_uids[0].isdisjoint(request_uids[1])
