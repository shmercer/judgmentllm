"""Known invoked calls retain unknown usage without manufacturing nonacceptance."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import socket
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import httpx2
import pytest
from test_openai_lifecycle import setup_case

from judgmentllm.execution import execute_run, recover_run
from judgmentllm.persistence.evidence import EvidenceReceipt, _validate_commit_links
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.providers.openai import OpenAIAdapter


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-selected-key")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("No external network in uncertain usage tests")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("failure", ["timeout", "connection", "server", "missing_id"])
@pytest.mark.asyncio
async def test_uncertain_invocation_evidence_is_immutable_and_never_retried(
    tmp_path: Path, endpoint: str, capture: Literal["full", "minimal"], failure: str
) -> None:
    spec, store, adapter, _ = setup_case(tmp_path, endpoint, capture=capture)
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx2.ReadTimeout("private-timeout-canary")
        if failure == "connection":
            raise httpx2.ConnectError("private-connection-canary")
        return (
            httpx2.Response(503, json={"error": {"message": "private-service-canary"}})
            if failure == "server"
            else httpx2.Response(200, json={"output": []})
        )

    adapter._http_transport = httpx2.MockTransport(handler)
    try:
        result = await execute_run("001", store=store, adapter=adapter, spec=spec)
        assert result.recovery_required and calls == 1
    finally:
        await adapter.aclose()
    with ReadOnlyRepository(store) as repository:
        receipts = tuple(repository.iter_evidence("001"))
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.invocation_acceptance == "unresolved"
        assert receipt.identity.observation_id is None
        assert receipt.usage.definitive_failure_id is None
        assert receipt.usage.observation_kind == "invocation"
        assert (
            receipt.usage.input_tokens
            is receipt.usage.output_tokens
            is receipt.usage.total_tokens
            is None
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM submission_failures"
            ).fetchone()[0]
            == 0
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM usage_revisions"
            ).fetchone()[0]
            == 1
        )
        submission = repository.get_submission("001", receipt.identity.submission_id)
        assert submission.state == "uncertain" and submission.certainty == "unknown"
        encoded = canonical_bytes(receipt)

    def forbidden(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Uncertain submission must not be resubmitted")

    fresh = OpenAIAdapter(
        endpoint, spec.model, http_transport=httpx2.MockTransport(forbidden)
    )
    try:
        resumed = await recover_run("001", store=store, adapter=fresh, spec=spec)
        assert resumed.recovery_required and fresh.actual_call_count == 0
    finally:
        await fresh.aclose()
    with ReadOnlyRepository(store) as repository:
        replayed = tuple(repository.iter_evidence("001"))
        assert len(replayed) == 1 and canonical_bytes(replayed[0]) == encoded
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM usage_revisions"
            ).fetchone()[0]
            == 1
        )
    with pytest.raises(ValueError):
        receipt.model_copy(update={"failure_transient": True})
    with pytest.raises(ValueError):
        receipt.model_copy(
            update={
                "usage": receipt.usage.model_copy(
                    update={"definitive_failure_id": "forged-failure"}
                )
            }
        )
    with pytest.raises(ValueError):
        receipt.model_copy(
            update={
                "identity": receipt.identity.model_copy(
                    update={"observation_id": "forged-observation"}
                )
            }
        )
    changed = receipt.model_copy(update={"invocation_acceptance": None})
    # A syntactically legacy no-observation receipt still cannot satisfy the
    # repository links without a real definitive failure.
    assert changed.usage.definitive_failure_id is None
    with ReadOnlyRepository(store) as repository:
        verified = repository.prepare_evidence_commit(receipt)
        with pytest.raises(IntegrityError, match="definitive submission failure"):
            _validate_commit_links(repository, replace(verified, receipt=changed))
    assert EvidenceReceipt.model_validate_json(encoded) == receipt


@pytest.mark.asyncio
async def test_pre_call_fault_does_not_invent_known_invocation(tmp_path: Path) -> None:
    spec, store, adapter, bodies = setup_case(tmp_path, "responses")

    def interrupt(stage: str, request: str) -> None:
        if stage == "after_submitting":
            raise RuntimeError("synthetic pre-call interruption")

    try:
        with pytest.raises(ExceptionGroup):
            await execute_run(
                "001", store=store, adapter=adapter, spec=spec, fault=interrupt
            )
    finally:
        await adapter.aclose()
    assert not bodies
    with ReadOnlyRepository(store) as repository:
        assert not tuple(repository.iter_evidence("001"))


@pytest.mark.asyncio
async def test_uncertain_receipt_before_transaction_recovers_as_uncertain(
    tmp_path: Path,
) -> None:
    spec, store, adapter, _ = setup_case(tmp_path, "responses")

    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("synthetic timeout")

    adapter._http_transport = httpx2.MockTransport(timeout)

    def interrupt(stage: str, request: str) -> None:
        if stage == "after_uncertain_evidence_receipt":
            raise RuntimeError("synthetic durable interruption")

    try:
        with pytest.raises(ExceptionGroup):
            await execute_run(
                "001", store=store, adapter=adapter, spec=spec, fault=interrupt
            )
    finally:
        await adapter.aclose()

    def forbidden(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("No request may replace uncertain work")

    fresh = OpenAIAdapter(
        "responses", spec.model, http_transport=httpx2.MockTransport(forbidden)
    )
    try:
        resumed = await recover_run("001", store=store, adapter=fresh, spec=spec)
        assert resumed.recovery_required and fresh.actual_call_count == 0
    finally:
        await fresh.aclose()
    with ReadOnlyRepository(store) as repository:
        evidence = tuple(repository.iter_evidence("001"))
        assert len(evidence) == 1 and evidence[0].invocation_acceptance == "unresolved"
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM submission_failures"
            ).fetchone()[0]
            == 0
        )
