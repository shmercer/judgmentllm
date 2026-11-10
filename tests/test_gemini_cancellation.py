"""Cancellation preserves known calls without making replacement requests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal, cast

import httpx2
import pytest
from test_gemini_lifecycle import setup_case

from judgmentllm.execution import execute_run, recover_run
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.providers.gemini import GeminiAdapter

# pyright: reportPrivateUsage=false


@pytest.mark.parametrize("endpoint", ["models.generate_content"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.asyncio
async def test_cancellation_after_http_forward_retains_unknown_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    capture: Literal["full", "minimal"],
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-cancellation-key")
    spec, store, adapter, _ = setup_case(tmp_path, endpoint, capture=capture)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def response(request: httpx2.Request) -> httpx2.Response:
        entered.set()
        await release.wait()
        raise AssertionError("Cancelled transport must not finish")

    adapter._http_transport = httpx2.MockTransport(response)
    task = asyncio.create_task(
        execute_run("001", store=store, adapter=adapter, spec=spec)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        assert adapter.actual_call_count == 1
    finally:
        await adapter.aclose()
    with ReadOnlyRepository(store) as repository:
        records = tuple(repository.iter_evidence("001"))
        assert len(records) == 1
        assert records[0].invocation_acceptance == "unresolved"
        assert records[0].usage.input_tokens is None
        assert records[0].usage.output_tokens is None
        assert records[0].usage.total_tokens is None
        assert records[0].identity.observation_id is None
        assert records[0].usage.definitive_failure_id is None
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM usage_revisions"
            ).fetchone()[0]
            == 1
        )

    def forbidden(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Recovery must never replace a cancelled submission")

    fresh = GeminiAdapter(
        endpoint, spec.model, http_transport=httpx2.MockTransport(forbidden)
    )
    try:
        resumed = await recover_run("001", store=store, adapter=fresh, spec=spec)
        assert resumed.recovery_required
        assert fresh.actual_call_count == 0
    finally:
        await fresh.aclose()


@pytest.mark.parametrize("endpoint", ["models.generate_content"])
@pytest.mark.parametrize(
    "window",
    [
        "after_submit",
        "after_acknowledgment",
        "queued_response",
        "after_evidence_commit",
        "after_evidence_extraction",
        "after_evidence_artifacts",
        "after_evidence_receipt",
        "before_parsing",
        "before_validation",
    ],
)
@pytest.mark.asyncio
async def test_cancellation_drains_received_response_and_preserves_real_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    window: str,
) -> None:
    from typing import Any

    from judgmentllm.providers.protocol import ProviderResponse

    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-cancellation-key")
    spec, store, adapter, bodies = setup_case(tmp_path, endpoint, capture="minimal")
    loop = asyncio.get_running_loop()
    cancelled = False

    def checkpoint(stage: str, request: str) -> None:
        nonlocal cancelled
        if stage == window and not cancelled:
            cancelled = True
            loop.call_soon_threadsafe(task.cancel)

    if window == "queued_response":
        original = asyncio.Queue

        class GatedQueue(original[Any]):
            async def get(self) -> Any:
                item = await super().get()
                if (
                    isinstance(item, tuple)
                    and len(cast(tuple[object, ...], item)) == 2
                    and isinstance(cast(tuple[object, ...], item)[1], ProviderResponse)
                ):
                    task.cancel()
                    await asyncio.Event().wait()
                return cast(Any, item)

        monkeypatch.setattr("judgmentllm.execution.core.asyncio.Queue", GatedQueue)
    task = asyncio.create_task(
        execute_run("001", store=store, adapter=adapter, spec=spec, fault=checkpoint)
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        assert adapter.actual_call_count == len(bodies) == 1
    finally:
        await adapter.aclose()
    with ReadOnlyRepository(store) as repository:
        receipts = tuple(repository.iter_evidence("001"))
        assert len(receipts) == 1
        assert receipts[0].invocation_acceptance is None
        assert receipts[0].identity.observation_id is not None
        assert receipts[0].usage.input_tokens == 20
        assert receipts[0].usage.output_tokens == 10
        assert receipts[0].usage.total_tokens == 34
        results = tuple(repository.iter_results("001"))
        assert results and all(row.status == "complete" for row in results)
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM usage_revisions"
            ).fetchone()[0]
            == 1
        )

    def forbidden(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Completed response recovery must not repeat the request")

    fresh = GeminiAdapter(
        endpoint, spec.model, http_transport=httpx2.MockTransport(forbidden)
    )
    try:
        report = await recover_run("001", store=store, adapter=fresh, spec=spec)
        assert not report.recovery_required
        assert fresh.actual_call_count == 0
    finally:
        await fresh.aclose()


@pytest.mark.parametrize("endpoint", ["models.generate_content"])
@pytest.mark.asyncio
async def test_cancel_before_http_forward_does_not_create_invocation_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-cancellation-key")
    spec, store, adapter, bodies = setup_case(tmp_path, endpoint)
    entered = asyncio.Event()

    async def blocked_client() -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(adapter, "_execution_client", blocked_client)
    task = asyncio.create_task(
        execute_run("001", store=store, adapter=adapter, spec=spec)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        assert adapter.actual_call_count == 0 and not bodies
    finally:
        await adapter.aclose()
    with ReadOnlyRepository(store) as repository:
        assert not tuple(repository.iter_evidence("001"))


@pytest.mark.parametrize("endpoint", ["models.generate_content"])
@pytest.mark.asyncio
async def test_transport_self_cancellation_propagates_without_deadlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-cancellation-key")
    spec, store, adapter, _ = setup_case(tmp_path, endpoint)

    def cancelled(request: httpx2.Request) -> httpx2.Response:
        raise asyncio.CancelledError

    adapter._http_transport = httpx2.MockTransport(cancelled)
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                execute_run("001", store=store, adapter=adapter, spec=spec), timeout=10
            )
    finally:
        await adapter.aclose()
    with ReadOnlyRepository(store) as repository:
        (receipt,) = tuple(repository.iter_evidence("001"))
        assert receipt.invocation_acceptance == "unresolved"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    from gemini_offline import isolate

    isolate(monkeypatch)
