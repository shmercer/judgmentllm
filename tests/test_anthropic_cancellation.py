"""Cancellation preserves known calls without making replacement requests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal, cast

import httpx2
import pytest
from test_anthropic_lifecycle import setup_case

from judgmentllm.execution import execute_run, recover_run
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.providers.anthropic import AnthropicAdapter

# pyright: reportPrivateUsage=false


@pytest.mark.parametrize("endpoint", ["messages"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.asyncio
async def test_cancellation_after_http_forward_retains_unknown_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    capture: Literal["full", "minimal"],
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-cancellation-key")
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

    fresh = AnthropicAdapter(
        endpoint, spec.model, http_transport=httpx2.MockTransport(forbidden)
    )
    try:
        resumed = await recover_run("001", store=store, adapter=fresh, spec=spec)
        assert resumed.recovery_required
        assert fresh.actual_call_count == 0
    finally:
        await fresh.aclose()


@pytest.mark.parametrize("endpoint", ["messages"])
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

    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-cancellation-key")
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
        assert receipts[0].usage.total_tokens is None
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

    fresh = AnthropicAdapter(
        endpoint, spec.model, http_transport=httpx2.MockTransport(forbidden)
    )
    try:
        report = await recover_run("001", store=store, adapter=fresh, spec=spec)
        assert not report.recovery_required
        assert fresh.actual_call_count == 0
    finally:
        await fresh.aclose()


@pytest.mark.parametrize("endpoint", ["messages"])
@pytest.mark.asyncio
async def test_cancel_before_http_forward_does_not_create_invocation_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-cancellation-key")
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


@pytest.mark.parametrize("endpoint", ["messages"])
@pytest.mark.asyncio
async def test_transport_self_cancellation_propagates_without_deadlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-cancellation-key")
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


class _SyntheticWorkerFailure(RuntimeError):
    pass


def _concurrent_case(
    directory: Path, endpoint: str, capture: Literal["full", "minimal"]
) -> tuple[object, Path, str]:
    import json

    from test_anthropic_lifecycle import production_spec

    from judgmentllm.anthropic_transport import production_context
    from judgmentllm.preparation import prepare_run

    spec = production_spec(
        directory / "inputs", endpoint, capture=capture, explanations=False
    )
    spec.execution.concurrency = 2
    spec.input.path.write_text(
        "".join(
            json.dumps(
                {"item_id": identifier, "text": "Synthetic assessment " + identifier}
            )
            + "\n"
            for identifier in ("001", "002")
        ),
        encoding="utf-8",
    )
    assert spec.rubric is not None
    output = json.dumps(
        {
            "scores": [
                {"trait_id": trait.trait_id, "category_label": trait.levels[0].label}
                for trait in spec.rubric.traits
            ]
        }
    )
    store = directory / "runtime"
    with prepare_run(
        spec,
        store=store,
        run_id="001",
        capabilities=AnthropicAdapter.capabilities_for(
            "anthropic", endpoint, spec.model
        ),
        transport_context=production_context(spec),
    ):
        pass
    return spec, store, output


@pytest.mark.parametrize("endpoint", ["messages"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.asyncio
async def test_worker_failure_retains_response_and_cancelled_sibling_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    capture: Literal["full", "minimal"],
) -> None:
    from test_anthropic_lifecycle import envelope

    from judgmentllm.config import RunSpecV3

    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-worker-failure-key")
    spec, store, output = _concurrent_case(tmp_path, endpoint, capture)
    assert isinstance(spec, RunSpecV3)
    sibling_forwarded = asyncio.Event()
    calls = 0

    async def response(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.wait_for(sibling_forwarded.wait(), timeout=5)
            return httpx2.Response(200, json=envelope(endpoint, output))
        sibling_forwarded.set()
        await asyncio.Event().wait()
        raise AssertionError("TaskGroup must cancel the in-flight sibling")

    def fail_after_submit(stage: str, request: str) -> None:
        if stage == "after_submit":
            raise _SyntheticWorkerFailure("synthetic worker failure")

    adapter = AnthropicAdapter(
        endpoint,
        spec.model,
        concurrency=2,
        http_transport=httpx2.MockTransport(response),
    )
    try:
        with pytest.raises(ExceptionGroup) as caught:
            await asyncio.wait_for(
                execute_run(
                    "001",
                    store=store,
                    adapter=adapter,
                    spec=spec,
                    fault=fail_after_submit,
                ),
                timeout=10,
            )
        assert len(caught.value.exceptions) == 1
        assert isinstance(caught.value.exceptions[0], _SyntheticWorkerFailure)
        assert adapter.actual_call_count == calls == 2
    finally:
        await adapter.aclose()

    with ReadOnlyRepository(store) as repository:
        evidence = tuple(repository.iter_evidence("001"))
        assert len(evidence) == 2
        received = tuple(
            receipt
            for receipt in evidence
            if receipt.identity.observation_id is not None
        )
        unknown = tuple(
            receipt
            for receipt in evidence
            if receipt.invocation_acceptance == "unresolved"
        )
        assert len(received) == len(unknown) == 1
        assert received[0].usage.total_tokens is None
        assert unknown[0].usage.total_tokens is None
        assert unknown[0].usage.definitive_failure_id is None
        assert tuple(repository.iter_results("001"))
        assert repository.get_run("001").state != "cancelled"

    def forbidden(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Recovery cannot replace either prior SDK invocation")

    fresh = AnthropicAdapter(
        endpoint, spec.model, http_transport=httpx2.MockTransport(forbidden)
    )
    try:
        report = await asyncio.wait_for(
            recover_run("001", store=store, adapter=fresh, spec=spec), timeout=10
        )
        assert report.recovery_required and fresh.actual_call_count == 0
    finally:
        await fresh.aclose()
    with ReadOnlyRepository(store) as repository:
        assert len(tuple(repository.iter_evidence("001"))) == 2
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM usage_revisions"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("endpoint", ["messages"])
@pytest.mark.asyncio
async def test_worker_failure_before_forward_does_not_invent_sibling_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    from judgmentllm.config import RunSpecV3

    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-worker-failure-key")
    spec, store, _ = _concurrent_case(tmp_path, endpoint, "full")
    assert isinstance(spec, RunSpecV3)
    entered = 0
    sibling_entered = asyncio.Event()

    async def blocked_client() -> None:
        nonlocal entered
        entered += 1
        if entered == 1:
            await asyncio.wait_for(sibling_entered.wait(), timeout=5)
            raise _SyntheticWorkerFailure("synthetic before-forward failure")
        sibling_entered.set()
        await asyncio.Event().wait()

    adapter = AnthropicAdapter(endpoint, spec.model, concurrency=2)
    monkeypatch.setattr(adapter, "_execution_client", blocked_client)
    try:
        with pytest.raises(ExceptionGroup) as caught:
            await asyncio.wait_for(
                execute_run("001", store=store, adapter=adapter, spec=spec), timeout=10
            )
        assert isinstance(caught.value.exceptions[0], _SyntheticWorkerFailure)
        assert adapter.actual_call_count == 0
    finally:
        await adapter.aclose()
    with ReadOnlyRepository(store) as repository:
        assert not tuple(repository.iter_evidence("001"))
        assert not tuple(repository.iter_results("001"))
