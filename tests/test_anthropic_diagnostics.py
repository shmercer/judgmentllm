"""One-read Anthropic diagnostics use the pinned SDK without generation."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import json
import logging
import socket
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from typer.testing import CliRunner

from judgmentllm import anthropic_diagnostics as diagnostics
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


def _cleanup_waits(
    monkeypatch: pytest.MonkeyPatch, *, shorten: bool = False
) -> tuple[list[float], list[asyncio.Task[Any]]]:
    waits: list[float] = []
    owners: list[asyncio.Task[Any]] = []

    async def observed(awaitable: Any, timeout: float) -> Any:
        waits.append(timeout)
        assert timeout in {10.0, 20.0}
        if timeout == 10.0:
            task = asyncio.current_task()
            assert task is not None
            owners.append(task)
        return await asyncio.wait_for(
            awaitable, timeout=0.01 if shorten and timeout == 10.0 else timeout
        )

    monkeypatch.setattr(
        diagnostics,
        "asyncio",
        SimpleNamespace(**(vars(asyncio) | {"wait_for": observed})),
    )
    return waits, owners


@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.parametrize(
    "constructor,closing",
    [
        (constructor, closing)
        for constructor in ["value", "runtime", "cancel", "success"]
        for closing in ["success", "error", "self-cancel", "timeout"]
    ]
    + [("missing", "success")],
)
@pytest.mark.asyncio
async def test_diagnostic_owned_cleanup_and_status_priority(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    prior: bool,
    constructor: str,
    closing: str,
) -> None:
    original = {
        "value": ValueError("PRIVATE_CONSTRUCTOR"),
        "runtime": RuntimeError("PRIVATE_CONSTRUCTOR"),
        "cancel": asyncio.CancelledError("original-constructor"),
    }.get(constructor)
    cleanup_cancel = asyncio.CancelledError("cleanup-self-cancel")
    supplied: list[Any] = []
    closed: list[asyncio.Task[Any]] = []
    finished = asyncio.Event()
    forwarded: list[Any] = []
    waits, owners = _cleanup_waits(monkeypatch, shorten=closing == "timeout")
    protected = diagnostics._protected_client
    caplog.set_level(logging.DEBUG)

    class TrackedTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: Any) -> Any:
            forwarded.append(request)
            return httpx2.Response(200, json={"data": [], "has_more": False})

        async def aclose(self) -> None:
            task = asyncio.current_task()
            assert task is not None
            closed.append(task)
            assert diagnostics._SDK_LOGGING.get() is True
            logging.getLogger("anthropic._base_client").debug("PRIVATE_CLOSE")
            try:
                if closing == "timeout":
                    await asyncio.Event().wait()
                if closing == "error":
                    raise RuntimeError("PRIVATE_CLOSE")
                if closing == "self-cancel":
                    raise cleanup_cancel
            finally:
                finished.set()

    def capture(*args: Any, **kwargs: Any) -> Any:
        supplied.append(kwargs["http_client"])
        assert not supplied[-1].is_closed and not closed
        if original is not None:
            raise original
        client = protected(*args, **kwargs)
        assert client._client is supplied[-1]
        return client

    monkeypatch.setattr(diagnostics, "_protected_client", capture)
    if constructor == "missing":
        monkeypatch.delenv("ANTHROPIC_API_KEY")
    token = diagnostics._SDK_LOGGING.set(prior)
    try:
        if constructor == "cancel" or closing == "self-cancel":
            expected = original if constructor == "cancel" else cleanup_cancel
            with pytest.raises(asyncio.CancelledError) as caught:
                await diagnostics.adiscover_models(http_transport=TrackedTransport())
            assert caught.value is expected
        else:
            result = await diagnostics.adiscover_models(
                http_transport=TrackedTransport()
            )
            expected_status = (
                "inconclusive"
                if constructor in {"value", "missing"} and closing == "success"
                else "authentication_accepted"
                if constructor == closing == "success"
                else "network_unavailable"
            )
            assert result.verification.verification_status == expected_status
            assert result.sdk_calls == len(forwarded)
            assert "PRIVATE" not in result.model_dump_json()
        assert diagnostics._SDK_LOGGING.get() is prior
    finally:
        diagnostics._SDK_LOGGING.reset(token)
    expected_calls = int(constructor == "success")
    assert len(forwarded) == expected_calls
    assert len(supplied) == len(closed) == int(constructor != "missing")
    assert finished.is_set() is (constructor != "missing")
    assert waits == ([20.0] if expected_calls else []) + (
        [] if constructor == "missing" else [10.0]
    )
    assert len(owners) == int(constructor != "missing")
    assert all(task.done() for task in [*closed, *owners])
    assert "PRIVATE" not in caplog.text and "synthetic-selected-key" not in caplog.text


@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.parametrize("constructor", ["runtime", "cancel", "success"])
@pytest.mark.parametrize("closing", ["success", "error", "self-cancel"])
@pytest.mark.asyncio
async def test_real_cancellation_during_diagnostic_close_is_drained(
    monkeypatch: pytest.MonkeyPatch, prior: bool, constructor: str, closing: str
) -> None:
    original = (
        asyncio.CancelledError("original-constructor")
        if constructor == "cancel"
        else RuntimeError("PRIVATE_CONSTRUCTOR")
    )
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closed: list[asyncio.Task[Any]] = []
    forwarded: list[Any] = []
    restored: list[bool] = []
    waits, owners = _cleanup_waits(monkeypatch)
    protected = diagnostics._protected_client

    class HeldTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: Any) -> Any:
            forwarded.append(request)
            return httpx2.Response(200, json={"data": [], "has_more": False})

        async def aclose(self) -> None:
            task = asyncio.current_task()
            assert task is not None
            closed.append(task)
            assert diagnostics._SDK_LOGGING.get() is True
            entered.set()
            try:
                await release.wait()
                if closing == "error":
                    raise RuntimeError("PRIVATE_CLOSE")
                if closing == "self-cancel":
                    raise asyncio.CancelledError("cleanup-self-cancel")
            finally:
                finished.set()

    def capture(*args: Any, **kwargs: Any) -> Any:
        if constructor != "success":
            raise original
        return protected(*args, **kwargs)

    monkeypatch.setattr(diagnostics, "_protected_client", capture)

    async def invoke() -> Any:
        token = diagnostics._SDK_LOGGING.set(prior)
        try:
            return await diagnostics.adiscover_models(http_transport=HeldTransport())
        finally:
            restored.append(diagnostics._SDK_LOGGING.get())
            diagnostics._SDK_LOGGING.reset(token)

    task = asyncio.create_task(invoke())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        task.cancel("caller-first")
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
        task.cancel("caller-second")
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, timeout=1.0)
    if constructor == "cancel":
        assert caught.value is original
    else:
        assert caught.value.args == ("caller-first",)
    assert finished.is_set() and len(closed) == 1
    assert restored == [prior]
    assert len(forwarded) == int(constructor == "success")
    assert waits == ([20.0] if constructor == "success" else []) + [10.0]
    assert len(owners) == 1
    assert all(owned.done() for owned in [task, *closed, *owners])


@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.asyncio
async def test_request_cancellation_closes_sdk_owned_http(
    monkeypatch: pytest.MonkeyPatch, prior: bool
) -> None:
    entered, finished = asyncio.Event(), asyncio.Event()
    tasks: list[asyncio.Task[Any]] = []
    closed: list[bool] = []
    restored: list[bool] = []
    waits, owners = _cleanup_waits(monkeypatch)

    class HeldRequest(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: Any) -> Any:
            task = asyncio.current_task()
            assert task is not None
            tasks.append(task)
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("Cancelled diagnostic must not resume")

        async def aclose(self) -> None:
            assert diagnostics._SDK_LOGGING.get() is True
            closed.append(True)
            finished.set()

    async def invoke() -> Any:
        token = diagnostics._SDK_LOGGING.set(prior)
        try:
            return await diagnostics.adiscover_models(http_transport=HeldRequest())
        finally:
            restored.append(diagnostics._SDK_LOGGING.get())
            diagnostics._SDK_LOGGING.reset(token)

    task = asyncio.create_task(invoke())
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    task.cancel("request-cancel")
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, timeout=1.0)
    assert caught.value.args == ("request-cancel",)
    assert closed == [True] and finished.is_set()
    assert restored == [prior]
    assert waits == [20.0, 10.0] and len(owners) == 1
    assert all(owned.done() for owned in [task, *tasks, *owners])


@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.parametrize("constructor", ["runtime", "cancel", "success"])
@pytest.mark.asyncio
async def test_earlier_handled_cancellation_preserves_cleanup_identity(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    prior: bool,
    constructor: str,
) -> None:
    async def invoke() -> None:
        owner = asyncio.current_task()
        assert owner is not None
        owner.cancel("earlier-handled")
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        assert owner.cancelling() == 1
        await test_diagnostic_owned_cleanup_and_status_priority(
            monkeypatch, caplog, prior, constructor, "self-cancel"
        )

    await asyncio.create_task(invoke())


@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.parametrize("constructor", ["runtime", "cancel", "success"])
@pytest.mark.asyncio
async def test_earlier_handled_cancellation_keeps_first_new_caller(
    monkeypatch: pytest.MonkeyPatch, prior: bool, constructor: str
) -> None:
    original = diagnostics.adiscover_models

    async def with_history(**kwargs: Any) -> Any:
        owner = asyncio.current_task()
        assert owner is not None
        owner.cancel("earlier-handled")
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        assert owner.cancelling() == 1
        return await original(**kwargs)

    monkeypatch.setattr(diagnostics, "adiscover_models", with_history)
    await test_real_cancellation_during_diagnostic_close_is_drained(
        monkeypatch, prior, constructor, "self-cancel"
    )


@pytest.mark.parametrize("prior", [False, True])
@pytest.mark.parametrize("caller_first", [False, True])
@pytest.mark.parametrize("constructor", ["runtime", "cancel", "success"])
@pytest.mark.asyncio
async def test_cleanup_callback_order_preserves_caller_and_original(
    monkeypatch: pytest.MonkeyPatch, prior: bool, caller_first: bool, constructor: str
) -> None:
    original = (
        asyncio.CancelledError("original-constructor")
        if constructor == "cancel"
        else RuntimeError("PRIVATE_CONSTRUCTOR")
    )
    cleanup_cancel = asyncio.CancelledError("cleanup-self-cancel")
    result_readers: list[asyncio.Task[Any] | None] = []
    retained: list[asyncio.Task[Any]] = []
    states: list[tuple[bool, bool]] = []
    restored: list[bool] = []
    owner: asyncio.Task[Any] | None = None
    closed: list[bool] = []
    forwarded: list[Any] = []
    waits, owners = _cleanup_waits(monkeypatch)
    protected = diagnostics._protected_client

    class ObservedTask(asyncio.Task[Any]):
        def result(self) -> Any:
            result_readers.append(asyncio.current_task())
            return super().result()

    def create_retained(coro: Any) -> asyncio.Task[Any]:
        pending = ObservedTask(coro)
        retained.append(pending)
        return pending

    def ordered_shield(pending: asyncio.Future[Any]) -> asyncio.Future[Any]:
        assert not pending.done()

        def cancel_owner(_: asyncio.Future[Any]) -> None:
            assert owner is not None
            states.append((outer.done(), outer.cancelled()))
            owner.cancel("caller-first")

        if caller_first:
            pending.add_done_callback(cancel_owner)
        outer = asyncio.shield(pending)
        if not caller_first:
            pending.add_done_callback(cancel_owner)
        return outer

    monkeypatch.setattr(diagnostics.asyncio, "create_task", create_retained)
    monkeypatch.setattr(diagnostics.asyncio, "shield", ordered_shield)

    class SelfCancellingTransport(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request: Any) -> Any:
            forwarded.append(request)
            return httpx2.Response(200, json={"data": [], "has_more": False})

        async def aclose(self) -> None:
            assert diagnostics._SDK_LOGGING.get() is True
            closed.append(True)
            raise cleanup_cancel

    def capture(*args: Any, **kwargs: Any) -> Any:
        if constructor != "success":
            raise original
        return protected(*args, **kwargs)

    monkeypatch.setattr(diagnostics, "_protected_client", capture)

    async def invoke() -> Any:
        nonlocal owner
        owner = asyncio.current_task()
        token = diagnostics._SDK_LOGGING.set(prior)
        try:
            return await diagnostics.adiscover_models(
                http_transport=SelfCancellingTransport()
            )
        finally:
            restored.append(diagnostics._SDK_LOGGING.get())
            diagnostics._SDK_LOGGING.reset(token)
            assert owner in result_readers

    task = asyncio.create_task(invoke())
    with pytest.raises(asyncio.CancelledError) as caught:
        await asyncio.wait_for(task, timeout=1.0)
    if constructor == "cancel":
        assert caught.value is original
    else:
        assert caught.value.args == ("caller-first",)
    assert states == [(not caller_first, False)]
    assert closed == [True] and restored == [prior]
    assert len(forwarded) == int(constructor == "success")
    assert waits == ([20.0] if constructor == "success" else []) + [10.0]
    assert len(owners) == len(retained) == 1
    assert all(pending.done() for pending in [task, *owners, *retained])
    assert not retained[0].cancelled()
    assert retained[0].result() is cleanup_cancel


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
