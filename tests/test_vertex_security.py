"""Adversarial final forwarding, pinned constructors and source identity."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from vertex_fixture import wire_for
from vertex_offline import adc, envelope, isolate

from judgmentllm.config import GenerationSpec
from judgmentllm.providers import vertex as p
from judgmentllm.providers.protocol import ProviderIntegrityError, SubmissionUncertain
from judgmentllm.vertex_config import (
    VertexRuntimeConfiguration,
    VertexSetupError,
    runtime_configuration,
)
from judgmentllm.vertex_transport import build_vertex_request_body, production_context


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


def invocation(wire: Any) -> dict[str, str]:
    return {
        "submission_id": wire.transport_plan.identity.submission_id,
        "correlation_id": wire.transport_plan.identity.correlation_id,
    }


def test_pinned_sdk_source_profile() -> None:
    from importlib.metadata import distribution

    profile = json.loads(
        (
            Path(__file__).parent / "fixtures/production_vertex/sdk-profile.json"
        ).read_text()
    )
    sdk = distribution("google-genai")
    assert sdk.version == profile["sdk_version"] == "2.34.0"
    for name, digest in profile["source_sha256"].items():
        assert (
            hashlib.sha256(
                Path(str(sdk.locate_file("google/genai/" + name))).read_bytes()
            ).hexdigest()
            == digest
        )


@pytest.mark.asyncio
async def test_enterprise_and_legacy_constructor_contracts_and_express_difference() -> (
    None
):
    import ssl

    import certifi
    from google.genai.client import DebugConfig

    tls = ssl.create_default_context(cafile=certifi.where())
    sdk, http = p._sdk()
    runtime = runtime_configuration()

    def deny(request: Any) -> Any:
        raise AssertionError("Only intercepted async forwarding is permitted")

    for flags in (
        {"enterprise": True},
        {"vertexai": True},
        {"enterprise": True, "vertexai": True},
    ):
        sync = http.Client(transport=http.MockTransport(deny), trust_env=False)
        async_client = http.AsyncClient(
            transport=http.MockTransport(deny), trust_env=False
        )
        client = sdk.Client(
            **flags,
            debug_config=DebugConfig(
                client_mode=None, replays_directory=None, replay_id=None
            ),
            project=runtime.project,
            location=runtime.location,
            api_key="SYNTHETIC_KEY",
            http_options=sdk.types.HttpOptions(
                base_url=runtime.base_url,
                client_args={"verify": tls, "trust_env": False},
                async_client_args={"verify": tls, "ssl": tls, "trust_env": False},
                httpx_client=sync,
                httpx_async_client=async_client,
            ),
        )
        try:
            assert (
                client.vertexai
                and client._api_client._http_options.api_version == "v1beta1"
            )
            assert client._api_client.api_key == "SYNTHETIC_KEY"
        finally:
            client.close()
            await client.aio.aclose()
            sync.close()
            await async_client.aclose()
    with pytest.raises(ValueError, match="conflicting"):
        sdk.Client(enterprise=True, vertexai=False, api_key="SYNTHETIC_KEY")
    sent: list[str] = []

    def intercepted(request: httpx2.Request) -> httpx2.Response:
        sent.append(str(request.url))
        return httpx2.Response(200, json=envelope("{}"))

    sync = http.Client(transport=http.MockTransport(deny), trust_env=False)
    async_client = http.AsyncClient(
        transport=http.MockTransport(intercepted), trust_env=False
    )
    client = sdk.Client(
        enterprise=True,
        debug_config=DebugConfig(
            client_mode=None, replays_directory=None, replay_id=None
        ),
        api_key="SYNTHETIC_EXPRESS_KEY",
        http_options=sdk.types.HttpOptions(
            base_url="https://aiplatform.googleapis.com/",
            client_args={"verify": tls, "trust_env": False},
            async_client_args={"verify": tls, "ssl": tls, "trust_env": False},
            api_version="v1beta1",
            httpx_client=sync,
            httpx_async_client=async_client,
            retry_options=sdk.types.HttpRetryOptions(attempts=1),
        ),
    )
    try:
        assert client._api_client.project is client._api_client.location is None
        await client.aio.models.generate_content(
            model="fixture-model",
            contents="synthetic",
            config=sdk.types.GenerateContentConfig(
                automatic_continuation=False,
                automatic_function_calling=sdk.types.AutomaticFunctionCallingConfig(
                    disable=True
                ),
            ),
        )
        assert sent == [
            "https://aiplatform.googleapis.com/v1beta1/publishers/google/models/fixture-model:generateContent"
        ]
    finally:
        client.close()
        await client.aio.aclose()
        sync.close()
        await async_client.aclose()


@pytest.mark.asyncio
async def test_protected_client_denies_sync_and_wrong_route() -> None:
    sdk, http = p._sdk()
    runtime = runtime_configuration()

    def synthetic(request: Any) -> Any:
        return http.Response(200, json={})

    client_http = http.AsyncClient(
        transport=http.MockTransport(synthetic),
        trust_env=False,
    )
    client = p._protected_client(
        sdk, http, runtime=runtime, key="SYNTHETIC_KEY", http_client=client_http
    )
    try:
        with pytest.raises(ProviderIntegrityError):
            client.models.list()
    finally:
        await p._close_client(client)
    async_client = http.AsyncClient(
        transport=http.MockTransport(synthetic),
        trust_env=False,
    )

    def wrong_client(**kwargs: Any) -> Any:
        return SimpleNamespace(vertexai=False)

    try:
        with pytest.raises(ProviderIntegrityError):
            p._protected_client(
                SimpleNamespace(Client=wrong_client, types=sdk.types),
                http,
                runtime=runtime,
                key="SYNTHETIC_KEY",
                http_client=async_client,
            )
    finally:
        await async_client.aclose()


@pytest.mark.parametrize("mode", ["adc", "api_key"])
@pytest.mark.parametrize(
    "change",
    ["url", "authorization", "x-goog-api-key", "x-goog-user-project", "extra-header"],
)
@pytest.mark.asyncio
async def test_conflicting_auth_and_endpoint_headers_cannot_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, change: str
) -> None:
    if mode == "adc":
        adc(monkeypatch)
    wire, spec, _, context = wire_for(tmp_path)

    def forbidden(request: Any) -> Any:
        raise AssertionError("Poisoned authenticated transport must never forward")

    adapter = p.VertexAdapter(
        spec.endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(forbidden),
    )
    try:
        client = await adapter._execution_client()

        async def mutate(request: Any) -> None:
            if change == "url":
                request.url = httpx2.URL(
                    "https://generativelanguage.googleapis.com/v1beta/models/fixture:generateContent"
                )
            else:
                request.headers[change] = "PRIVATE_CANARY"

        client._judgmentllm_http_client.event_hooks["request"].append(mutate)
        with pytest.raises(ProviderIntegrityError) as error:
            await adapter.submit(wire, **invocation(wire))
        assert adapter.actual_call_count == 0 and "PRIVATE_CANARY" not in str(
            error.value
        )
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "finish", ["CONTINUATION", "MAX_TOKENS", "UNEXPECTED_TOOL_CALL"]
)
@pytest.mark.asyncio
async def test_no_hidden_continuation_or_function_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish: str
) -> None:
    import google.genai.models as sdk_models

    original = cast(Any, sdk_models.AsyncModels).generate_content

    async def inspect(self: Any, **kwargs: Any) -> Any:
        config = kwargs["config"]
        assert (
            config.automatic_continuation is False
            and config.automatic_function_calling.disable is True
        )
        assert config.tools is None and config.continuation_token is None
        return await original(self, **kwargs)

    monkeypatch.setattr(sdk_models.AsyncModels, "generate_content", inspect)
    wire, spec, _, _ = wire_for(tmp_path)
    body = envelope("{}")
    body["candidates"][0].update(finishReason=finish, continuationToken="synthetic")
    adapter = p.VertexAdapter(
        spec.endpoint,
        spec.model,
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json=body)
        ),
    )
    try:
        await adapter.submit(wire, **invocation(wire))
        response = await adapter.retrieve(wire, **invocation(wire))
        assert response.kind == "provider_error" and adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "model",
    ["gemini-3.6-flash", "gemini-3.8-flash", "gemini-4-carbon", "gemini-4-argon"],
)
def test_known_ignored_generation_cannot_reach_prepared_body(
    tmp_path: Path, model: str
) -> None:
    _, spec, dep, _ = wire_for(tmp_path)
    spec.model = model
    context = production_context(spec)
    from judgmentllm.vertex_transport import build_output_commitment_v7

    output = build_output_commitment_v7(spec, dep.request, context)
    with pytest.raises(ValueError):
        build_vertex_request_body(
            endpoint=spec.endpoint,
            model=spec.model,
            prompt="synthetic",
            output=output,
            generation=GenerationSpec(temperature=1),
        )
    spec.generation = GenerationSpec(top_p=0.9)
    with pytest.raises(p.VertexConfigurationError):
        p.VertexAdapter(spec.endpoint, model, context=context).validate(spec)


@pytest.mark.asyncio
async def test_response_bound_and_documented_invalid_key_do_not_leak_or_retry(
    tmp_path: Path,
) -> None:
    wire, spec, _, _ = wire_for(tmp_path)
    for oversized in (True, False):
        raw = (
            b"x" * (8_388_609)
            if oversized
            else json.dumps(
                {
                    "error": {
                        "code": 400,
                        "status": "INVALID_ARGUMENT",
                        "message": "PRIVATE_PROSE",
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                "reason": "API_KEY_INVALID",
                                "domain": "googleapis.com",
                            }
                        ],
                    }
                }
            ).encode()
        )
        adapter = p.VertexAdapter(
            spec.endpoint,
            spec.model,
            http_transport=httpx2.MockTransport(
                lambda request, status=200 if oversized else 400, body=raw: (
                    httpx2.Response(status, content=body)
                )
            ),
        )
        try:
            with pytest.raises(
                SubmissionUncertain if oversized else p.VertexRequestRejected
            ) as error:
                await adapter.submit(wire, **invocation(wire))
            assert (
                "PRIVATE_PROSE" not in str(error.value)
                and adapter.actual_call_count == 1
                and not adapter._responses
            )
            if not oversized:
                assert (
                    isinstance(error.value, p.VertexRequestRejected)
                    and error.value.diagnostic_code == "credentials_rejected"
                )
        finally:
            await adapter.aclose()


def test_runtime_configuration_rejects_unapproved_mode_and_resource_path() -> None:
    with pytest.raises(VertexSetupError):
        VertexRuntimeConfiguration(
            "synthetic-project", "global", auth_mode=cast(Any, "unsupported")
        )
    with pytest.raises(ValueError):
        runtime_configuration().resource_path("../private")


@pytest.mark.parametrize("echo", [False, True])
@pytest.mark.asyncio
async def test_adc_refresh_cannot_erase_the_forwarded_secret_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, echo: bool
) -> None:
    credentials, _ = adc(monkeypatch)
    credentials._quota_project_id = None
    wire, spec, _, _ = wire_for(tmp_path)

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == "Bearer SYNTHETIC_ADC_ACCESS_TOKEN"
        assert "x-goog-user-project" not in request.headers
        credentials.token = None
        return httpx2.Response(
            200, json=envelope("SYNTHETIC_ADC_ACCESS_TOKEN" if echo else "{}")
        )

    adapter = p.VertexAdapter(
        spec.endpoint, spec.model, http_transport=httpx2.MockTransport(handler)
    )
    try:
        if echo:
            with pytest.raises(SubmissionUncertain):
                await adapter.submit(wire, **invocation(wire))
            assert not adapter._responses
        else:
            await adapter.submit(wire, **invocation(wire))
            assert (await adapter.retrieve(wire, **invocation(wire))).kind == "response"
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_key_disappearance_cannot_trigger_adc_or_developer_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.providers.protocol import TransportNotSubmitted

    wire, spec, _, _ = wire_for(tmp_path)
    monkeypatch.delenv("JUDGMENTLLM_VERTEX_API_KEY")
    adapter = p.VertexAdapter(
        spec.endpoint,
        spec.model,
        http_transport=httpx2.MockTransport(
            lambda request: pytest.fail("No key implies no forward")
        ),
    )
    try:
        with pytest.raises(TransportNotSubmitted):
            await adapter.submit(wire, **invocation(wire))
        assert adapter.actual_call_count == 0
    finally:
        await adapter.aclose()


def test_untrusted_runtime_selection_and_frozen_identity_cannot_be_substituted(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from judgmentllm.transport import physical_identity
    from judgmentllm.vertex_transport import (
        VertexSelectionV1,
        prepare_production_request,
        production_context_digest,
    )

    wire, spec, dep, context = wire_for(tmp_path)
    with pytest.raises(ValueError):
        production_context_digest(
            replace(
                context, runtime=cast(Any, SimpleNamespace(**vars(context.runtime)))
            )
        )
    with pytest.raises(ValueError):
        VertexSelectionV1.model_validate(
            dict(wire.output_commitment.selection) | {"features": ()}
        )
    different = physical_identity(
        "different-run", dep.request.request_uid, "different-attempt"
    )
    with pytest.raises(ValueError):
        prepare_production_request(
            spec, dep, different, context, frozen=wire.transport_commitment
        )
    candidate, _, _, _ = wire_for(tmp_path, probe=True, policy="native")
    artifacts = candidate.content_artifacts()
    assert {"native_schema", "sdk_configuration", "format_fragment"} <= set(artifacts)
    for mode in ("bulk_live", "batch_native"):
        spec.execution = spec.execution.model_copy(update={"mode": mode})
        with pytest.raises(ValueError):
            prepare_production_request(spec, dep, wire.transport_plan.identity, context)
