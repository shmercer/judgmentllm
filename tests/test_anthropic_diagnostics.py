"""One-read Anthropic diagnostics use the pinned SDK without generation."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import socket
from typing import Any

import httpx2
import pytest
from typer.testing import CliRunner

from judgmentllm.anthropic_diagnostics import (
    AuthVerification,
    ModelDiscovery,
    adiscover_models,
    averify_authentication,
    discover_models,
    verify_authentication,
)
from judgmentllm.auth import credential_status
from judgmentllm.cli import app


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-selected-key")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Unexpected network access")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize(
    "status,expected",
    [
        (200, "authentication_accepted"),
        (401, "credentials_rejected"),
        (403, "permission_denied"),
        (429, "rate_limited"),
        (500, "network_unavailable"),
        (503, "network_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_one_sdk_read_authentication_status(
    monkeypatch: pytest.MonkeyPatch, status: int, expected: str
) -> None:
    requests: list[httpx2.Request] = []
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS",
        "Authorization: Bearer ambient-private-canary\nX-Private: ambient-private-canary",
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/v1/models"
        assert request.headers["Authorization"] == "Bearer synthetic-selected-key"
        assert "ambient-private-canary" not in str(request.headers)
        return httpx2.Response(
            status,
            json={
                "object": "list",
                "data": [{"id": "model-visible", "owned_by": "private-account"}],
            }
            if status == 200
            else {"error": {"message": "private-api-error-canary"}},
        )

    result = await adiscover_models(
        model="model-visible", http_transport=httpx2.MockTransport(handler)
    )
    assert len(requests) == result.sdk_calls == 1
    assert result.verification.verification_status == expected
    assert result.model_visibility == ("visible" if status == 200 else "unknown")
    assert "private-api-error-canary" not in result.model_dump_json()
    assert "private-account" not in result.model_dump_json()
    assert "synthetic-selected-key" not in result.model_dump_json()


@pytest.mark.parametrize(
    "body, visibility, accepted",
    [
        ({"data": [], "has_more": False}, "not_visible", True),
        ({"data": [], "has_more": True}, "unknown", True),
        ({"data": [{"id": "visible"}], "has_more": True}, "visible", True),
        ({"data": "invalid"}, "unknown", False),
        ({"data": [{"id": None}]}, "unknown", False),
    ],
)
@pytest.mark.asyncio
async def test_models_visibility_no_pagination(
    body: Any, visibility: str, accepted: bool
) -> None:
    result = await adiscover_models(
        model="visible",
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json=body)
        ),
    )
    assert result.sdk_calls == 1 and result.model_visibility == visibility
    assert (
        result.verification.verification_status == "authentication_accepted"
    ) is accepted


@pytest.mark.asyncio
async def test_missing_credentials_and_wrong_provider_make_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")

    def forbidden(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("No SDK call permitted")

    result = await averify_authentication(
        http_transport=httpx2.MockTransport(forbidden)
    )
    assert result.diagnostic_code == "missing_api_key"
    with pytest.raises(ValueError):
        await adiscover_models(
            provider="unsupported", http_transport=httpx2.MockTransport(forbidden)
        )
    assert credential_status(providers=("anthropic",))[0].status == "missing"
    with pytest.raises(ValueError):
        verify_authentication()
    with pytest.raises(ValueError):
        discover_models()


def test_sync_diagnostics_and_cli_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx2.MockTransport(
        lambda request: httpx2.Response(200, json={"data": [{"id": "model-visible"}]})
    )
    report = verify_authentication(http_transport=transport)
    assert report.verification_status == "authentication_accepted"
    import judgmentllm.anthropic_diagnostics as command

    def saved_verification(**kwargs: Any) -> AuthVerification:
        return report

    monkeypatch.setattr(command, "verify_authentication", saved_verification)
    result = CliRunner().invoke(
        app, ["auth", "verify", "--provider", "anthropic", "--json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["command"] == "auth verify" and data["schema_version"] == 1
    listing = discover_models(
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json={"data": []})
        )
    )

    def saved_listing(**kwargs: Any) -> ModelDiscovery:
        return listing

    monkeypatch.setattr(command, "discover_models", saved_listing)
    result = CliRunner().invoke(
        app, ["providers", "models", "--provider", "anthropic", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["command"] == "providers models"
    unknown = CliRunner().invoke(
        app, ["auth", "verify", "--provider", "unsupported", "--json"]
    )
    assert unknown.exit_code == 2


@pytest.mark.asyncio
async def test_diagnostic_debug_logs_suppress_remote_identifiers_and_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            401,
            json={"error": {"message": "private-error-canary"}},
            headers={"x-request-id": "private-request-identifier-canary"},
        )

    report = await averify_authentication(http_transport=httpx2.MockTransport(handler))
    assert report.verification_status == "credentials_rejected"
    assert "private-request-identifier-canary" not in caplog.text
    assert "private-error-canary" not in caplog.text
    assert "synthetic-selected-key" not in caplog.text


@pytest.mark.asyncio
async def test_missing_sdk_is_sanitized_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import judgmentllm.anthropic_diagnostics as implementation
    from judgmentllm.providers.protocol import ProviderError

    def missing() -> Any:
        raise ProviderError("private-import-canary")

    monkeypatch.setattr(implementation, "_sdk", missing)
    report = await averify_authentication()
    assert (
        report.verification_status == "unsupported"
        and report.diagnostic_code == "missing_anthropic_extra"
    )
    assert "private-import-canary" not in report.model_dump_json()


@pytest.mark.asyncio
async def test_diagnostics_sdk_mismatch_never_initializes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import judgmentllm.anthropic_diagnostics as diagnostics

    monkeypatch.setattr(diagnostics, "anthropic_sdk_version", lambda: "2.3.1")

    def forbidden() -> Any:
        raise AssertionError("A mismatched SDK cannot initialize a diagnostics client")

    monkeypatch.setattr(diagnostics, "_sdk", forbidden)
    result = await diagnostics.adiscover_models()
    assert result.sdk_calls == 0
    assert result.verification.verification_status == "unsupported"
    assert result.verification.diagnostic_code == "sdk_scope_mismatch"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_public_sync_diagnostics_preserve_provider_specific_record_types(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import discover_models as public_models
    from judgmentllm import verify_authentication as public_auth
    from judgmentllm.openai_diagnostics import AuthVerification as OpenAIAuth
    from judgmentllm.openai_diagnostics import ModelDiscovery as OpenAIModels

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-public-dispatch")
    transport = httpx2.MockTransport(
        lambda request: httpx2.Response(
            200, json={"data": [{"id": "visible"}], "has_more": False}
        )
    )
    auth = public_auth(provider=provider, http_transport=transport)
    models = public_models(provider=provider, model="visible", http_transport=transport)
    assert type(auth) is (AuthVerification if provider == "anthropic" else OpenAIAuth)
    assert type(models) is (ModelDiscovery if provider == "anthropic" else OpenAIModels)
    assert auth.verification_status == "authentication_accepted"
    assert models.model_visibility == "visible" and models.sdk_calls == 1


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.asyncio
async def test_public_async_diagnostics_preserve_provider_specific_record_types(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import adiscover_models as public_models
    from judgmentllm import averify_authentication as public_auth

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-public-dispatch")
    transport = httpx2.MockTransport(
        lambda request: httpx2.Response(200, json={"data": [], "has_more": False})
    )
    auth = await public_auth(provider=provider, http_transport=transport)
    models = await public_models(
        provider=provider, model="missing", http_transport=transport
    )
    assert auth.provider == models.provider == provider
    assert auth.verification_status == "authentication_accepted"
    assert models.model_visibility == "not_visible" and models.sdk_calls == 1


@pytest.mark.parametrize("escaped", [False, True])
@pytest.mark.asyncio
async def test_model_list_credential_echo_cannot_enter_reports(escaped: bool) -> None:
    key = "synthetic-selected-key"
    encoded = (
        "".join("\\u" + format(ord(character), "04x") for character in key)
        if escaped
        else key
    )
    raw = ('{"data":[{"id":"' + encoded + '"}],"has_more":false}').encode()
    report = await adiscover_models(
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, content=raw, headers={"content-type": "application/json"}
            )
        )
    )
    assert report.models == ()
    assert report.verification.verification_status != "authentication_accepted"
    assert key not in report.model_dump_json()
    assert report.sdk_calls == 1
