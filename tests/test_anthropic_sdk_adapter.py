"""Actual pinned SDK, exact Messages bytes, privacy and failure boundaries."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import logging
import socket
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast

import httpx2
import pytest
from anthropic_fixture import wire_for
from test_anthropic_lifecycle import envelope

from judgmentllm import anthropic_registry
from judgmentllm.anthropic_registry import _QualifiedProfile
from judgmentllm.anthropic_transport import (
    PreparedRequestV5,
    production_context_for_identity,
)
from judgmentllm.config import GenerationSpec
from judgmentllm.evidence.scope import EvidenceProvenance
from judgmentllm.providers.anthropic import (
    AnthropicAdapter,
    AnthropicReadinessError,
    AnthropicRequestRejected,
)
from judgmentllm.providers.protocol import (
    RetrievalUnavailable,
    SubmissionUncertain,
)
from judgmentllm.transport import TransportIntegrityError


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_SELECTED_CREDENTIAL")

    def deny(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External provider access forbidden")

    original_connect = socket.socket.connect

    def connect(current: socket.socket, address: Any) -> Any:
        # Windows ProactorEventLoop creates a private loopback socket pair.
        # Permit that local control channel while denying every external IP.
        if current.family in {socket.AF_INET, socket.AF_INET6} and address[0] not in {
            "127.0.0.1",
            "::1",
        }:
            return deny()
        return original_connect(current, address)

    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", connect)


def invocation(wire: PreparedRequestV5) -> dict[str, str]:
    return {
        "submission_id": wire.transport_plan.identity.submission_id,
        "correlation_id": wire.transport_plan.identity.correlation_id,
    }


def authorize_fixture(
    monkeypatch: pytest.MonkeyPatch, context: Any, features: Any
) -> None:
    monkeypatch.setattr(
        anthropic_registry,
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
                supported_features=features,
                documentation_sha256="a" * 64,
                offline_fixture_sha256="b" * 64,
                independent_live_sha256="c" * 64,
            ),
        ),
    )


@pytest.mark.parametrize("policy", ["text", "native", "auto"])
@pytest.mark.parametrize(
    "scenario",
    ["scores", "single", "together", "scores_first", "explanations_first", "pairwise"],
)
@pytest.mark.parametrize("explicit_limit", [False, True])
@pytest.mark.asyncio
async def test_actual_sdk_exact_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: Literal["text", "native", "auto"],
    scenario: str,
    explicit_limit: bool,
) -> None:
    options: dict[str, Any] = dict(
        policy=policy,
        grouped=scenario != "single",
        pairwise=scenario == "pairwise",
        explanations=scenario not in {"scores", "single", "pairwise"},
        layout=scenario
        if scenario in {"scores_first", "explanations_first"}
        else "together",
        generation=GenerationSpec(max_output_tokens=64) if explicit_limit else None,
    )
    candidate, _, _, context = wire_for(tmp_path, probe=True, **options)
    authorize_fixture(
        monkeypatch, context, candidate.output_commitment.selection.features
    )
    wire, spec, _, context = wire_for(tmp_path, **options)
    seen: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.content == wire.payload
        assert (
            request.headers["authorization"] == "Bearer SYNTHETIC_SELECTED_CREDENTIAL"
        )
        assert request.headers["anthropic-version"] == "2026-10-21"
        assert (
            "x-api-key" not in request.headers
            and "anthropic-beta" not in request.headers
        )
        seen.append(request.content)
        return httpx2.Response(
            200,
            json=envelope(
                "messages",
                '{"winner_id":"001"}' if scenario == "pairwise" else '{"scores":[]}',
            ),
        )

    adapter = AnthropicAdapter(
        "messages",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(handler),
    )
    try:
        ack = await adapter.submit(wire, **invocation(wire))
        response = await adapter.retrieve(wire, **invocation(wire))
        assert ack.remote_id == response.observation_key
        assert adapter.parse(response).kind == "response"
        assert seen == [wire.payload] and adapter.actual_call_count == 1
        assert adapter._client.max_retries == 0
        assert adapter._client.timeout.read == 60
        body = json.loads(wire.payload)
        assert body["max_tokens"] == (64 if explicit_limit else 4096)
        assert wire.generation == (
            GenerationSpec(max_output_tokens=64) if explicit_limit else None
        )
        assert "system" not in body and "store" not in body
        assert ("output_config" in body) == (policy != "text")
        assert response.evidence is not None and response.evidence.usage is not None
        assert response.evidence.usage.total_tokens is None
        assert response.evidence.usage.cache_read_tokens == 3
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(wire, **invocation(wire))
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, "request_rejected"),
        (401, "credentials_rejected"),
        (403, "permission_denied"),
        (404, "model_or_endpoint_unavailable"),
        (422, "request_rejected"),
        (429, "rate_limited"),
        (409, None),
        (500, None),
        (529, None),
        (307, None),
    ],
)
@pytest.mark.asyncio
async def test_rejection_uncertainty_and_no_retry(
    tmp_path: Path, status: int, expected: str | None
) -> None:
    wire, spec, _, _ = wire_for(tmp_path)
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(
            status,
            json={"error": {"message": "PRIVATE_ERROR_CANARY"}},
            headers={"location": "https://attacker.invalid"},
        )

    adapter = AnthropicAdapter(
        "messages", spec.model, http_transport=httpx2.MockTransport(handler)
    )
    try:
        with pytest.raises(
            AnthropicRequestRejected if expected else SubmissionUncertain
        ) as info:
            await adapter.submit(wire, **invocation(wire))
        if expected:
            assert isinstance(info.value, AnthropicRequestRejected)
            assert info.value.diagnostic_code == expected
        assert "PRIVATE_ERROR_CANARY" not in str(info.value)
        assert calls == [1] and adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "error", [httpx2.ReadTimeout, httpx2.ConnectError, httpx2.ReadError]
)
@pytest.mark.asyncio
async def test_ambiguous_network_failure_is_not_retried(
    tmp_path: Path, error: type[Exception]
) -> None:
    wire, spec, _, _ = wire_for(tmp_path)
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        raise error("PRIVATE_NETWORK_CANARY")

    adapter = AnthropicAdapter(
        "messages", spec.model, http_transport=httpx2.MockTransport(handler)
    )
    try:
        with pytest.raises(SubmissionUncertain) as info:
            await adapter.submit(wire, **invocation(wire))
        assert "PRIVATE_NETWORK_CANARY" not in str(info.value) and calls == [1]
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("setting", ["temperature", "top_p"])
@pytest.mark.parametrize("value", [0, 0.0, 1, 1.0])
def test_removed_sampling_rejected_before_sdk(
    tmp_path: Path, setting: str, value: int | float
) -> None:
    with pytest.raises(TransportIntegrityError):
        wire_for(tmp_path, generation=GenerationSpec.model_validate({setting: value}))


@pytest.mark.asyncio
async def test_ambient_identity_routing_headers_logging_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for name in (
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_IDENTITY_TOKEN",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_WEBHOOK_SIGNING_KEY",
    ):
        monkeypatch.setenv(name, "PRIVATE_AMBIENT_CANARY")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://attacker.invalid")
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS",
        "Authorization: Bearer PRIVATE_AMBIENT_CANARY\nanthropic-version: wrong\nx-private: PRIVATE_AMBIENT_CANARY",
    )
    wire, spec, _, _ = wire_for(tmp_path)

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url == httpx2.URL("https://api.anthropic.com/v1/messages")
        assert "PRIVATE_AMBIENT_CANARY" not in str(request.headers)
        return httpx2.Response(200, json=envelope("messages", "{}"))

    adapter = AnthropicAdapter(
        "messages", spec.model, http_transport=httpx2.MockTransport(handler)
    )
    try:
        with caplog.at_level(logging.DEBUG):
            await adapter.submit(wire, **invocation(wire))
        assert "SYNTHETIC_SELECTED_CREDENTIAL" not in caplog.text
        assert "PRIVATE_AMBIENT_CANARY" not in caplog.text
        assert wire.rendered_text not in caplog.text
        assert adapter._client.open_telemetry["traces"]["enabled"] is False
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("policy", ["native", "auto"])
def test_native_authority_cannot_be_borrowed(tmp_path: Path, policy: str) -> None:
    if policy == "native":
        with pytest.raises(TransportIntegrityError):
            wire_for(tmp_path, policy=policy)
    else:
        wire, *_ = wire_for(tmp_path, policy=policy)
        assert wire.transport_commitment.mechanism == "text"
        assert wire.transport_commitment.selection.native_support == "unknown"
    with pytest.raises(TransportIntegrityError):
        production_context_for_identity("openai", "messages", "model")


def test_preflight_sdk_missing_and_version_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.providers.anthropic as provider

    adapter = AnthropicAdapter("messages", "offline-model")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(AnthropicReadinessError):
        adapter.validate_credentials()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic")
    monkeypatch.setattr(provider, "anthropic_sdk_version", lambda: "wrong")
    with pytest.raises(AnthropicReadinessError):
        adapter.validate_credentials()


def test_inspected_sdk_source_hashes_match_pinned_distribution() -> None:
    import hashlib
    import importlib.metadata

    root = (
        Path(__file__).parent / "fixtures" / "production_anthropic" / "sdk-profile.json"
    )
    profile = json.loads(root.read_text(encoding="utf-8"))
    distribution = importlib.metadata.distribution("anthropic")
    assert distribution.version == profile["sdk_version"] == "2.3.0"
    for name, digest in profile["sources"].items():
        assert (
            hashlib.sha256(
                Path(str(distribution.locate_file("anthropic/" + name))).read_bytes()
            ).hexdigest()
            == digest
        )
    http_distribution = importlib.metadata.distribution("httpx2")
    assert http_distribution.version == profile["http_version"] == "2.16.0"
    for name, digest in profile["http_sources"].items():
        assert (
            hashlib.sha256(
                Path(str(http_distribution.locate_file("httpx2/" + name))).read_bytes()
            ).hexdigest()
            == digest
        )
    retained = json.loads(
        (root.parent.parent / "native_anthropic" / "profile.json").read_text()
    )
    assert all(
        profile["sources"][name] == digest
        for name, digest in retained["sdk_sources"].items()
    )
    assert profile["api_version"] == "2026-10-21"
    assert profile["live_qualified"] is False


@pytest.mark.parametrize(
    "change",
    [
        "body",
        "url",
        "authorization",
        "anthropic-version",
        "x-api-key",
        "anthropic-beta",
    ],
)
@pytest.mark.asyncio
async def test_forwarding_boundary_rejects_mutated_sdk_requests(
    tmp_path: Path, change: str
) -> None:
    from judgmentllm.providers.protocol import ProviderIntegrityError

    wire, spec, _, context = wire_for(tmp_path)
    sent = 0

    def forbidden(request: httpx2.Request) -> httpx2.Response:
        nonlocal sent
        sent += 1
        raise AssertionError("A changed sealed request must never be forwarded")

    adapter = AnthropicAdapter(
        "messages",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(forbidden),
    )
    try:
        client = await adapter._execution_client()

        async def mutate(request: Any) -> None:
            if change == "body":
                request._content = b'{"PRIVATE_PROMPT":"changed"}'
            elif change == "url":
                request.url = httpx2.URL("https://attacker.invalid/v1/messages")
            else:
                request.headers[change] = "PRIVATE_AUTH_CANARY"

        client._client.event_hooks["request"].append(mutate)
        with pytest.raises(ProviderIntegrityError) as error:
            await adapter.submit(wire, **invocation(wire))
        assert sent == adapter.actual_call_count == 0
        assert "PRIVATE" not in str(error.value)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_pinned_sdk_child_loggers_do_not_expose_transport_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    wire, spec, _, context = wire_for(tmp_path)
    caplog.set_level(logging.DEBUG)

    def response(request: httpx2.Request) -> httpx2.Response:
        for name in (
            "anthropic._response",
            "anthropic.lib.credentials._auth",
            "httpcore2.http11",
            "httpcore2.connection",
        ):
            logging.getLogger(name).debug("PRIVATE_HTTP_CANARY %s", request.headers)
        return httpx2.Response(200, json=envelope("messages", "{}"))

    adapter = AnthropicAdapter(
        "messages",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(response),
    )
    try:
        await adapter.submit(wire, **invocation(wire))
    finally:
        await adapter.aclose()
    assert "PRIVATE_HTTP_CANARY" not in caplog.text
    assert "SYNTHETIC_SELECTED_CREDENTIAL" not in caplog.text


@pytest.mark.asyncio
async def test_provider_requests_and_recovery_authority_cannot_cross(
    tmp_path: Path,
) -> None:
    from test_production_transport import wire_for as openai_wire_for

    from judgmentllm.providers.openai import OpenAIAdapter
    from judgmentllm.providers.protocol import ProviderIntegrityError

    anthropic_wire, spec, _, context = wire_for(tmp_path / "anthropic")
    openai_wire, openai_spec, _, openai_context = openai_wire_for(tmp_path / "openai")
    anthropic = AnthropicAdapter("messages", spec.model, context=context)
    openai = OpenAIAdapter(
        openai_spec.endpoint, openai_spec.model, context=openai_context
    )
    try:
        with pytest.raises(ProviderIntegrityError):
            await anthropic.submit(openai_wire, **invocation(anthropic_wire))
        identity = openai_wire.transport_plan.identity
        with pytest.raises(ProviderIntegrityError):
            await openai.submit(
                anthropic_wire,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
        assert anthropic.actual_call_count == openai.actual_call_count == 0
    finally:
        await anthropic.aclose()
        await openai.aclose()


def test_offline_guard_allows_event_loop_socketpair_and_denies_external_ips() -> None:
    first, second = socket.socketpair()
    try:
        first.sendall(b"local control")
        assert second.recv(13) == b"local control"
    finally:
        first.close()
        second.close()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as current:
        with pytest.raises(AssertionError, match="External provider access forbidden"):
            current.connect(("203.0.113.1", 443))
