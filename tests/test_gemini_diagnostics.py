"""Bounded non-generative actual-SDK reads and ordinary CLI dispatch."""

from __future__ import annotations

import asyncio

# pyright: reportPrivateUsage=false
import json
from typing import Any

import httpx2
import pytest
from gemini_offline import isolate
from typer.testing import CliRunner

from judgmentllm import gemini_diagnostics as diagnostics
from judgmentllm import provider_diagnostics
from judgmentllm.auth import credential_status
from judgmentllm.cli import app


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


@pytest.mark.parametrize(
    "status,expected",
    [
        (200, "authentication_accepted"),
        (401, "credentials_rejected"),
        (403, "permission_denied"),
        (429, "rate_limited"),
        (500, "network_unavailable"),
        (503, "network_unavailable"),
        (307, "network_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_one_bounded_sdk_read_without_pagination(
    status: int, expected: str
) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"
        assert (
            str(request.url)
            == "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
        )
        assert request.headers["x-goog-api-key"] == "SYNTHETIC_SELECTED_GEMINI_KEY"
        seen.append(request)
        return httpx2.Response(
            status,
            json={
                "models": [
                    {"name": "models/visible", "displayName": "PRIVATE_ACCOUNT"}
                ],
                "nextPageToken": "PRIVATE_TOKEN",
            }
            if status == 200
            else {"error": {"code": status, "message": "PRIVATE_PROVIDER_DETAIL"}},
        )

    result = await provider_diagnostics.adiscover_models(
        provider="gemini", model="visible", http_transport=httpx2.MockTransport(handler)
    )
    assert result.verification.verification_status == expected
    assert result.sdk_calls == len(seen) == 1 and not result.complete
    assert result.model_visibility == ("visible" if status == 200 else "unknown")
    assert all(
        value not in result.model_dump_json()
        for value in (
            "PRIVATE_ACCOUNT",
            "PRIVATE_TOKEN",
            "PRIVATE_PROVIDER_DETAIL",
            "SYNTHETIC_SELECTED",
        )
    )


@pytest.mark.parametrize(
    "body,visibility,accepted",
    [
        ({"models": []}, "not_visible", True),
        ({"models": [], "nextPageToken": "more"}, "unknown", True),
        ({"models": [{"name": "models/visible"}]}, "visible", True),
        ({"models": None}, "unknown", False),
        ({"models": [{"name": None}]}, "unknown", False),
        ({"models": [{"name": "not-a-resource"}]}, "unknown", False),
        ({"models": [], "nextPageToken": False}, "unknown", False),
        ({"models": [{"name": "models/../forbidden"}]}, "unknown", False),
    ],
)
@pytest.mark.asyncio
async def test_visibility_and_invalid_model_metadata(
    body: Any, visibility: str, accepted: bool
) -> None:
    result = await diagnostics.adiscover_models(
        model="visible",
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json=body)
        ),
    )
    assert result.model_visibility == visibility and result.sdk_calls == 1
    assert (
        result.verification.verification_status == "authentication_accepted"
    ) is accepted


@pytest.mark.asyncio
async def test_verify_and_credential_precedence_are_shared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        assert request.url.query == b"pageSize=1"
        return httpx2.Response(200, json={"models": []})

    result = await provider_diagnostics.averify_authentication(
        provider="gemini", http_transport=httpx2.MockTransport(handler)
    )
    assert (
        result.source == "GOOGLE_API_KEY"
        and result.verification_status == "authentication_accepted"
    )
    monkeypatch.delenv("GOOGLE_API_KEY")
    result = await diagnostics.averify_authentication(
        http_transport=httpx2.MockTransport(handler)
    )
    assert (
        result.source == "GEMINI_API_KEY"
        and calls[-1].headers["x-goog-api-key"] == "SYNTHETIC_SECONDARY_GEMINI_KEY"
    )
    monkeypatch.delenv("GEMINI_API_KEY")
    result = await diagnostics.averify_authentication(
        http_transport=httpx2.MockTransport(handler)
    )
    assert result.diagnostic_code == "missing_api_key" and len(calls) == 2
    assert credential_status(providers=("gemini",))[0].status == "missing"
    with pytest.raises(ValueError):
        await diagnostics.adiscover_models(provider="vertex")
    with pytest.raises(ValueError):
        diagnostics.verify_authentication()
    with pytest.raises(ValueError):
        diagnostics.discover_models()


def test_synchronous_services_and_cli_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx2.MockTransport(
        lambda request: httpx2.Response(200, json={"models": []})
    )
    verification = provider_diagnostics.verify_authentication(
        provider="gemini", http_transport=transport
    )
    listing = provider_diagnostics.discover_models(
        provider="gemini", http_transport=transport
    )
    monkeypatch.setattr(diagnostics, "verify_authentication", lambda: verification)

    def saved_listing(**kwargs: Any) -> diagnostics.ModelDiscovery:
        return listing

    monkeypatch.setattr(diagnostics, "discover_models", saved_listing)
    for command in (("auth", "verify"), ("providers", "models")):
        result = CliRunner().invoke(app, [*command, "--provider", "gemini", "--json"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.stdout)
        assert body["schema_version"] == 1 and body["data"]["provider"] == "gemini"


@pytest.mark.parametrize(
    "failure",
    ["version", "extra", "network", "credential_echo", "duplicate", "oversized"],
)
@pytest.mark.asyncio
async def test_diagnostic_unavailability_is_safe(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "version":
        monkeypatch.setattr(diagnostics, "gemini_sdk_version", lambda: "wrong")
    if failure == "extra":

        def missing() -> Any:
            raise RuntimeError("PRIVATE_EXCEPTION")

        monkeypatch.setattr(diagnostics, "_sdk", missing)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if failure == "network":
            raise httpx2.ConnectError("PRIVATE_EXCEPTION")
        content = {
            "credential_echo": b'{"models":[],"private":"SYNTHETIC_SELECTED_GEMINI_KEY"}',
            "duplicate": b'{"models":[],"models":[]}',
            "oversized": b"x" * 1_048_577,
        }.get(failure, b'{"models":[]}')
        return httpx2.Response(200, content=content)

    result = await diagnostics.adiscover_models(
        http_transport=httpx2.MockTransport(handler)
    )
    assert result.verification.verification_status in {
        "unsupported",
        "network_unavailable",
        "inconclusive",
    }
    assert (
        "PRIVATE_EXCEPTION" not in result.model_dump_json()
        and "SYNTHETIC_SELECTED" not in result.model_dump_json()
    )


@pytest.mark.asyncio
async def test_diagnostic_transport_guards_run_before_any_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = diagnostics._protected_client
    forwarded: list[httpx2.Request] = []

    def guarded(*args: Any, **kwargs: Any) -> Any:
        client = original(*args, **kwargs)

        async def tamper(request: Any) -> None:
            request.headers["authorization"] = "PRIVATE_AMBIENT_AUTH"

        client._judgmentllm_http_client.event_hooks["request"].append(tamper)
        return client

    def forbidden(request: httpx2.Request) -> httpx2.Response:
        forwarded.append(request)
        raise AssertionError("Diagnostic guard forwarded unauthorized authentication")

    monkeypatch.setattr(diagnostics, "_protected_client", guarded)
    result = await diagnostics.adiscover_models(
        http_transport=httpx2.MockTransport(forbidden)
    )
    assert not forwarded and result.sdk_calls == 0
    assert result.verification.verification_status != "authentication_accepted"
    assert "PRIVATE_AMBIENT_AUTH" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_diagnostic_cancellation_closes_owned_transport() -> None:
    entered = asyncio.Event()
    closed: list[bool] = []

    class HeldTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx2.Request
        ) -> httpx2.Response:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("Cancelled diagnostic resumed")

        async def aclose(self) -> None:
            closed.append(True)

    task = asyncio.create_task(
        diagnostics.adiscover_models(http_transport=HeldTransport())
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


@pytest.mark.parametrize("phase", ["construction", "closing", "opaque_pagination"])
@pytest.mark.asyncio
async def test_diagnostic_failures_are_closed_and_sanitized(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    closed: list[bool] = []

    class TrackedTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx2.Request
        ) -> httpx2.Response:
            return httpx2.Response(
                200,
                json={
                    "models": [],
                    "nextPageToken": False if phase == "opaque_pagination" else None,
                },
            )

        async def aclose(self) -> None:
            closed.append(True)

    if phase == "construction":

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("PRIVATE_DETAIL")

        monkeypatch.setattr(diagnostics, "_protected_client", fail)
    elif phase == "closing":
        original_close = diagnostics._close_client

        async def fail_close(client: Any) -> None:
            await original_close(client)
            raise RuntimeError("PRIVATE_DETAIL")

        monkeypatch.setattr(diagnostics, "_close_client", fail_close)
    else:
        # Preserve the actual SDK request, while simulating a future parser that
        # tolerates a malformed pagination token. Local validation stays strict.
        original = diagnostics._protected_client

        def tolerant(*args: Any, **kwargs: Any) -> Any:
            client = original(*args, **kwargs)
            original_list = client.aio.models.list

            async def list_models(**settings: Any) -> Any:
                try:
                    return await original_list(**settings)
                except ValueError:
                    return None

            client.aio.models.list = list_models
            return client

        monkeypatch.setattr(diagnostics, "_protected_client", tolerant)
    result = await diagnostics.adiscover_models(http_transport=TrackedTransport())
    assert closed == [True]
    assert result.verification.verification_status in {
        "network_unavailable",
        "inconclusive",
    }
    assert "PRIVATE_DETAIL" not in result.model_dump_json()


@pytest.mark.parametrize(
    "status", ["unsupported", "inconclusive", "credentials_rejected"]
)
def test_cli_reports_safe_failures_with_established_exit_codes(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    original = diagnostics.verify_authentication(
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json={"models": []})
        )
    )
    report = original.model_copy(update={"verification_status": status})

    def verification(**kwargs: Any) -> diagnostics.AuthVerification:
        return report

    monkeypatch.setattr(diagnostics, "verify_authentication", verification)
    result = CliRunner().invoke(
        app, ["auth", "verify", "--provider", "gemini", "--json"]
    )
    assert (
        result.exit_code
        == {"unsupported": 2, "inconclusive": 3, "credentials_rejected": 1}[status]
    )
    assert json.loads(result.stdout)["error"]["retryable"] is False
