"""Pinned SDK HTTP tests: exact committed bodies and conservative outcomes."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import logging
import socket
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast

import httpx2
import pytest
from test_openai_lifecycle import envelope
from test_production_transport import wire_for

from judgmentllm import production_registry
from judgmentllm.config import GenerationSpec
from judgmentllm.evidence.scope import EvidenceProvenance
from judgmentllm.production_registry import _QualifiedProfile
from judgmentllm.providers.openai import OpenAIAdapter, OpenAIRequestRejected
from judgmentllm.providers.protocol import (
    ProviderIntegrityError,
    RetrievalUnavailable,
    SubmissionUncertain,
)
from judgmentllm.service_tiers import ServiceQualificationScope


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-selected-key")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Network unavailable in offline SDK tests")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def invocation(wire: Any) -> dict[str, str]:
    return {
        "submission_id": wire.transport_plan.identity.submission_id,
        "correlation_id": wire.transport_plan.identity.correlation_id,
    }


def authorize_fixture(
    monkeypatch: pytest.MonkeyPatch, context: Any, features: Any
) -> None:
    """Patch only the private repository registry, never caller-supplied evidence."""
    monkeypatch.setattr(
        production_registry,
        "_QUALIFIED_PROFILES",
        (
            _QualifiedProfile(
                qualification_date=date(2026, 11, 7),
                source_commit="a" * 40,
                support_status="supported",
                request_acceptance="accepted",
                assessment_behavior="verified",
                native_schema_behavior="verified",
                provenance=tuple(
                    EvidenceProvenance(
                        kind=cast(Any, kind),
                        source_sha256=digest * 64,
                        recorded_on=date(2026, 11, 7),
                    )
                    for kind, digest in (
                        ("documentation", "a"),
                        ("offline_fixture", "b"),
                        ("independent_live", "c"),
                    )
                ),
                scope=context.scope,
                service_scopes=(
                    ServiceQualificationScope(
                        account_scope="synthetic-account",
                        tier="standard",
                        features=tuple(features),
                    ),
                ),
                supported_features=features,
                documentation_sha256="a" * 64,
                offline_fixture_sha256="b" * 64,
                independent_live_sha256="c" * 64,
            ),
        ),
    )


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("policy", ["text", "native", "auto"])
@pytest.mark.parametrize(
    "scenario",
    [
        "scores",
        "single",
        "together",
        "scores_first",
        "explanations_first",
        "pairwise",
        "ties",
    ],
)
@pytest.mark.asyncio
async def test_actual_sdk_bytes_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    policy: Literal["text", "native", "auto"],
    scenario: str,
) -> None:
    options: dict[str, Any] = dict(
        endpoint=endpoint,
        policy=policy,
        grouped=scenario != "single",
        pairwise=scenario in {"pairwise", "ties"},
        ties=scenario == "ties",
        explanations=scenario not in {"scores", "single", "pairwise"},
        layout=scenario
        if scenario in {"scores_first", "explanations_first"}
        else "together",
        generation=GenerationSpec(temperature=0, top_p=1.0, max_output_tokens=64),
    )
    candidate, _, _, context = wire_for(tmp_path, probe=True, **options)
    authorize_fixture(
        monkeypatch, context, candidate.output_commitment.selection.features
    )
    wire, spec, _, context = wire_for(tmp_path, **options)
    seen: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.content == wire.payload
        assert request.headers["Authorization"] == "Bearer synthetic-selected-key"
        seen.append(request.content)
        return httpx2.Response(
            200,
            json=envelope(
                endpoint,
                '{"winner_id":"essay_02"}'
                if scenario in {"pairwise", "ties"}
                else '{"scores":[]}',
            ),
        )

    adapter = OpenAIAdapter(
        endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(handler),
    )
    try:
        ack = await adapter.submit(wire, **invocation(wire))
        response = await adapter.retrieve(wire, **invocation(wire))
        assert ack.remote_id == response.observation_key
        assert adapter.parse(response).kind == "response"
        assert seen == [wire.payload]
        assert adapter.actual_call_count == 1
        assert adapter._client.max_retries == 0
        assert adapter._client.timeout.read == 60
        assert adapter._client.timeout.write == 10
        assert adapter._client.timeout.connect == 10
        assert json.loads(wire.payload)["store"] is False
        assert response.evidence is not None
        assert response.evidence.usage is not None
        assert response.evidence.usage.cache_read_tokens == 3
        assert response.evidence.usage.reasoning_tokens == 2
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(wire, **invocation(wire))
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize(
    "status, expected",
    [
        (400, "request_rejected"),
        (401, "credentials_rejected"),
        (403, "permission_denied"),
        (404, "model_or_endpoint_unavailable"),
        (422, "request_rejected"),
        (429, "rate_limited"),
        (409, None),
        (500, None),
        (503, None),
    ],
)
@pytest.mark.asyncio
async def test_http_rejections_and_unknown_acceptance(
    tmp_path: Path, endpoint: str, status: int, expected: str | None
) -> None:
    wire, spec, _, context = wire_for(tmp_path, endpoint=endpoint)
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(
            status,
            json={
                "error": {
                    "message": "private-provider-error-canary",
                    "type": "rate_limit_error"
                    if status == 429
                    else "invalid_request_error",
                }
            },
        )

    adapter = OpenAIAdapter(
        endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(handler),
    )
    try:
        with pytest.raises(
            OpenAIRequestRejected if expected else SubmissionUncertain
        ) as caught:
            await adapter.submit(wire, **invocation(wire))
        assert "private-provider-error-canary" not in str(caught.value)
        if expected:
            assert isinstance(caught.value, OpenAIRequestRejected)
            assert caught.value.diagnostic_code == expected
        assert calls == adapter.actual_call_count == 1
        assert (
            await adapter.reconcile(wire, **invocation(wire))
        ).outcome == "unresolved"
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("failure", ["timeout", "connection", "cancel"])
@pytest.mark.asyncio
async def test_transport_failure_never_retries(tmp_path: Path, failure: str) -> None:
    import asyncio

    wire, spec, _, context = wire_for(tmp_path)

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if failure == "cancel":
            raise asyncio.CancelledError
        raise (
            httpx2.ReadTimeout("private-network-error-canary")
            if failure == "timeout"
            else httpx2.ConnectError("private-network-error-canary")
        )

    adapter = OpenAIAdapter(
        "responses",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(handler),
    )
    try:
        with pytest.raises(
            asyncio.CancelledError if failure == "cancel" else SubmissionUncertain
        ):
            await adapter.submit(wire, **invocation(wire))
        assert adapter.actual_call_count == 1
        assert (
            await adapter.reconcile(wire, **invocation(wire))
        ).outcome == "unresolved"
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_ambient_credentials_debug_and_bytes_are_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import openai._base_client as base

    wire, spec, _, context = wire_for(tmp_path)
    for name in (
        "OPENAI_ADMIN_KEY",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "OPENAI_WEBHOOK_SECRET",
    ):
        monkeypatch.setenv(name, "ambient-private-canary")
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS",
        "Authorization: Bearer ambient-private-canary\nX-Private: ambient-private-canary",
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unapproved.invalid/v1")
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer synthetic-selected-key"
        assert "ambient-private-canary" not in str(request.headers)
        return httpx2.Response(200, json=envelope("responses", '{"scores":[]}'))

    caplog.set_level(logging.DEBUG)
    adapter = OpenAIAdapter(
        "responses",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(handler),
    )
    try:
        await adapter.submit(wire, **invocation(wire))
        assert "synthetic-selected-key" not in caplog.text
        assert "ambient-private-canary" not in caplog.text
        assert "Synthetic response" not in caplog.text
    finally:
        await adapter.aclose()

    def altered_encoding(value: Any) -> bytes:
        return b"{}"

    monkeypatch.setattr(base, "openapi_dumps", altered_encoding)
    adapter = OpenAIAdapter(
        "responses",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(handler),
    )
    try:
        with pytest.raises(ProviderIntegrityError):
            await adapter.submit(wire, **invocation(wire))
        assert adapter.actual_call_count == 0
        assert len(seen) == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_candidate_and_changed_context_cannot_execute(tmp_path: Path) -> None:
    wire, spec, _, context = wire_for(tmp_path, probe=True, policy="native")
    adapter = OpenAIAdapter("responses", spec.model, context=context)
    with pytest.raises(ProviderIntegrityError):
        await adapter.submit(wire, **invocation(wire))
    ordinary = OpenAIAdapter("responses", spec.model)
    with pytest.raises(ProviderIntegrityError):
        await ordinary.submit(wire, **invocation(wire))
    final = replace(
        context, kind="openai-qualification-v1", qualification_plan_sha256="b" * 64
    )
    forged = OpenAIAdapter(
        "responses", spec.model, context=final, submission_guard=lambda request: None
    )
    with pytest.raises(ProviderIntegrityError):
        forged._check_authority()
    assert (
        adapter.actual_call_count
        == ordinary.actual_call_count
        == forged.actual_call_count
        == 0
    )


@pytest.mark.asyncio
async def test_oversized_http_response_is_bounded_uncertainty(tmp_path: Path) -> None:
    wire, spec, _, context = wire_for(tmp_path)

    class HugeResponse(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(10):
                yield b"x" * 1_048_576

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=HugeResponse())

    adapter = OpenAIAdapter(
        "responses",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(handler),
    )
    try:
        with pytest.raises(SubmissionUncertain):
            await adapter.submit(wire, **invocation(wire))
        assert adapter.actual_call_count == 1
        assert not adapter._responses
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_missing_returned_id_is_uncertain(tmp_path: Path) -> None:
    wire, spec, _, context = wire_for(tmp_path)
    adapter = OpenAIAdapter(
        "responses",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json={"output": []})
        ),
    )
    try:
        with pytest.raises(SubmissionUncertain):
            await adapter.submit(wire, **invocation(wire))
        assert adapter.actual_call_count == 1
        assert (
            await adapter.reconcile(wire, **invocation(wire))
        ).outcome == "unresolved"
    finally:
        await adapter.aclose()


def test_readiness_errors_are_actionable_and_network_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from importlib.metadata import PackageNotFoundError

    import judgmentllm.providers.openai as implementation
    from judgmentllm.providers.openai import OpenAIReadinessError

    adapter = OpenAIAdapter("responses", "unqualified-model")
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(OpenAIReadinessError) as caught:
        adapter.validate_credentials()
    assert caught.value.diagnostic_code == "missing_credentials"
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-selected-key")

    def missing(package: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(implementation, "version", missing)
    with pytest.raises(OpenAIReadinessError) as caught:
        adapter.validate_credentials()
    assert caught.value.diagnostic_code == "missing_extra"

    def wrong_version(package: str) -> str:
        return "99.0.0"

    monkeypatch.setattr(implementation, "version", wrong_version)
    with pytest.raises(OpenAIReadinessError) as caught:
        adapter.validate_credentials()
    assert caught.value.diagnostic_code == "sdk_scope_mismatch"


@pytest.mark.asyncio
async def test_public_resume_uses_sealed_concurrency_for_connection_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_openai_lifecycle import production_spec

    from judgmentllm.lifecycle import aresume_run
    from judgmentllm.preparation import prepare_run
    from judgmentllm.production_transport import production_context

    spec = production_spec(tmp_path / "inputs", explanations=False)
    spec.execution.concurrency = 1
    store = tmp_path / "runtime"
    with prepare_run(
        spec,
        store=store,
        run_id="001",
        capabilities=OpenAIAdapter.capabilities_for("openai", "responses", spec.model),
        transport_context=production_context(spec),
    ):
        pass
    connections: list[int | None] = []

    def make_transport(*, retries: int, limits: httpx2.Limits) -> httpx2.MockTransport:
        assert retries == 0
        connections.append(limits.max_connections)
        assert limits.max_keepalive_connections == 1
        return httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, json=envelope("responses", '{"scores":[]}')
            )
        )

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", make_transport)
    await aresume_run("001", store=store, spec=spec)
    assert connections == [1]


@pytest.mark.asyncio
async def test_client_concurrency_cannot_change_after_creation(tmp_path: Path) -> None:
    wire, spec, _, context = wire_for(tmp_path)
    adapter = OpenAIAdapter(
        "responses",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, json=envelope("responses", '{"scores":[]}')
            )
        ),
    )
    adapter.validate(spec)
    assert adapter._concurrency == spec.execution.concurrency
    try:
        await adapter.submit(wire, **invocation(wire))
        spec.execution.concurrency += 1
        with pytest.raises(ProviderIntegrityError):
            adapter.validate(spec)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_unused_sdk_platform_probe_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openai._base_client as base

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Unused platform metadata must not be probed")

    monkeypatch.setattr(base, "asyncify", forbidden)
    wire, spec, _, context = wire_for(tmp_path)
    adapter = OpenAIAdapter(
        "responses",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, json=envelope("responses", '{"scores":[]}')
            )
        ),
    )
    try:
        await adapter.submit(wire, **invocation(wire))
        assert adapter.actual_call_count == 1
    finally:
        await adapter.aclose()
