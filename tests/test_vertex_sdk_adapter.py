"""Pinned Vertex SDK routing, physical bytes and authentication isolation.

Every transport and ADC result is synthetic. Native candidates use the SDK
serialization seam only; the production adapter continues to refuse them.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from vertex_fixture import wire_for
from vertex_offline import adc, envelope, isolate

from judgmentllm.config import GenerationSpec
from judgmentllm.providers import vertex as p
from judgmentllm.providers.protocol import (
    ProviderIntegrityError,
    ProviderInvocationCancelled,
    RetrievalUnavailable,
    SubmissionUncertain,
    TransportNotSubmitted,
)
from judgmentllm.vertex_transport import PreparedRequestV7


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


def invocation(wire: PreparedRequestV7) -> dict[str, str]:
    return {
        "submission_id": wire.transport_plan.identity.submission_id,
        "correlation_id": wire.transport_plan.identity.correlation_id,
    }


@pytest.mark.parametrize("auth_mode", ["adc", "api_key"])
@pytest.mark.parametrize("location", ["global", "us-central1", "europe-west4"])
@pytest.mark.parametrize(
    "scenario",
    ["single", "grouped", "scores_first", "explanations_first", "pairwise", "ties"],
)
@pytest.mark.parametrize(
    "generation", [None, GenerationSpec(temperature=0, top_p=1, max_output_tokens=64)]
)
@pytest.mark.asyncio
async def test_exact_vertex_sdk_bytes_and_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: str,
    location: str,
    scenario: str,
    generation: GenerationSpec | None,
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", location)
    calls: list[object] = []
    if auth_mode == "adc":
        _, calls = adc(monkeypatch)
    wire, spec, _, context = wire_for(
        tmp_path,
        grouped=scenario != "single",
        pairwise=scenario in {"pairwise", "ties"},
        ties=scenario == "ties",
        explanations=scenario in {"scores_first", "explanations_first"},
        layout=scenario
        if scenario in {"scores_first", "explanations_first"}
        else "together",
        generation=generation,
    )
    bodies: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert (
            str(request.url)
            == context.runtime.base_url
            + "v1beta1/"
            + context.runtime.resource_path(spec.model)
            + ":generateContent"
        )
        assert request.content == wire.payload
        if auth_mode == "adc":
            assert (
                request.headers["authorization"] == "Bearer SYNTHETIC_ADC_ACCESS_TOKEN"
            )
            assert request.headers["x-goog-user-project"] == "synthetic-quota-project"
            assert "x-goog-api-key" not in request.headers
        else:
            assert request.headers["x-goog-api-key"] == "SYNTHETIC_SELECTED_VERTEX_KEY"
            assert (
                "authorization" not in request.headers
                and "x-goog-user-project" not in request.headers
            )
        assert request.extensions["timeout"] == {
            "connect": 10.0,
            "write": 10.0,
            "pool": 10.0,
            "read": 60.0,
        }
        bodies.append(request.content)
        return httpx2.Response(
            200,
            json=envelope(
                '{"winner_id":"001"}'
                if spec.task_kind == "pairwise"
                else '{"scores":[]}'
            ),
        )

    adapter = p.VertexAdapter(
        spec.endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(handler),
    )
    try:
        adapter.validate(spec)
        adapter.validate_credentials()
        assert not calls  # ADC status/readiness never discovers credentials.
        await adapter.submit(wire, **invocation(wire))
        response = await adapter.retrieve(wire, **invocation(wire))
        assert adapter.parse(response).kind == "response"
        assert adapter.actual_call_count == len(bodies) == 1
        assert len(calls) == (1 if auth_mode == "adc" else 0)
        if calls:
            assert calls == [
                {"scopes": ["https://www.googleapis.com/auth/cloud-platform"]}
            ]
        config = json.loads(wire.payload)["generationConfig"]
        assert config["candidateCount"] == 1
        if generation is None:
            assert not {"temperature", "topP", "maxOutputTokens"}.intersection(config)
        else:
            assert config["temperature"] == 0.0 and type(config["temperature"]) is float
            assert config["topP"] == 1.0 and config["maxOutputTokens"] == 64
        with pytest.raises(ProviderIntegrityError):
            await adapter.submit(wire, **invocation(wire))
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(wire, **invocation(wire))
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("mechanism", ["response_schema", "response_json_schema"])
@pytest.mark.parametrize(
    "scenario",
    [
        "single",
        "grouped",
        "together",
        "scores_first",
        "explanations_first",
        "pairwise",
        "ties",
    ],
)
@pytest.mark.asyncio
async def test_native_candidate_full_http_bytes_without_production_authority(
    tmp_path: Path, mechanism: str, scenario: str
) -> None:
    wire, spec, _, context = wire_for(
        tmp_path,
        probe=True,
        policy="native",
        mechanism="vertex.generate_content." + mechanism,
        grouped=scenario != "single",
        pairwise=scenario in {"pairwise", "ties"},
        ties=scenario == "ties",
        explanations=scenario in {"together", "scores_first", "explanations_first"},
        layout=scenario
        if scenario in {"scores_first", "explanations_first"}
        else "together",
    )
    adapter = p.VertexAdapter(spec.endpoint, spec.model, context=context)
    with pytest.raises(ProviderIntegrityError, match="candidates cannot execute"):
        await adapter.submit(wire, **invocation(wire))
    assert adapter._client is None
    sdk, http = p._sdk()
    sent: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.content == wire.payload
        assert (
            str(request.url)
            == context.runtime.base_url
            + "v1beta1/"
            + context.runtime.resource_path(spec.model)
            + ":generateContent"
        )
        sent.append(request.content)
        return httpx2.Response(
            200,
            json=envelope(
                '{"winner_id":"001"}'
                if spec.task_kind == "pairwise"
                else '{"scores":[]}'
            ),
        )

    client = p._protected_client(
        sdk,
        http,
        runtime=context.runtime,
        key="SYNTHETIC_VERTEX_SCHEMA_KEY",
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(handler),
            trust_env=False,
            follow_redirects=False,
        ),
    )
    try:
        await client.aio.models.generate_content(**p._sdk_arguments(sdk, wire))
        assert sent == [wire.payload]
        projection = wire.output_commitment.projection
        assert projection is not None
        config = json.loads(sent[0])["generationConfig"]
        assert config[
            "responseSchema" if mechanism == "response_schema" else "responseJsonSchema"
        ] == json.loads(projection.native_schema_text)
        assert ("Response schema:" in wire.rendered_text) is False
        assert all(
            c.enforcement_verified is None for c in projection.native_constraints
        )
    finally:
        await p._close_client(client)


@pytest.mark.parametrize("mode", ["adc", "api_key"])
@pytest.mark.parametrize(
    "status,diagnostic",
    [
        (400, "request_rejected"),
        (401, "credentials_rejected"),
        (403, "permission_denied"),
        (404, "model_or_endpoint_unavailable"),
        (422, "request_rejected"),
        (429, "rate_limited"),
        (500, None),
        (503, None),
        (307, None),
    ],
)
@pytest.mark.asyncio
async def test_rejection_uncertainty_and_redirect_never_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    status: int,
    diagnostic: str | None,
) -> None:
    if mode == "adc":
        adc(monkeypatch)
    wire, spec, _, context = wire_for(tmp_path)
    sent: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.content)
        return httpx2.Response(
            status,
            headers={"location": "https://forbidden.invalid"},
            json={"error": {"code": status, "message": "PRIVATE_PROVIDER_DETAIL"}},
        )

    adapter = p.VertexAdapter(
        spec.endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(handler),
    )
    try:
        with pytest.raises(
            p.VertexRequestRejected if diagnostic else SubmissionUncertain
        ) as failure:
            await adapter.submit(wire, **invocation(wire))
        assert "PRIVATE_PROVIDER_DETAIL" not in str(failure.value)
        if diagnostic:
            assert isinstance(failure.value, p.VertexRequestRejected)
            assert failure.value.diagnostic_code == diagnostic
        assert len(sent) == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "mode,secret",
    [
        ("adc", "SYNTHETIC_ADC_ACCESS_TOKEN"),
        ("api_key", "SYNTHETIC_SELECTED_VERTEX_KEY"),
    ],
)
@pytest.mark.parametrize("escape", [False, True])
@pytest.mark.asyncio
async def test_no_credential_echo_enters_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    secret: str,
    escape: bool,
) -> None:
    if mode == "adc":
        adc(monkeypatch)
    wire, spec, _, context = wire_for(tmp_path)
    raw = json.dumps(envelope(secret)).encode()
    if escape:
        raw = raw.replace(
            secret.encode(), "".join(f"\\u{ord(c):04x}" for c in secret).encode()
        )
    adapter = p.VertexAdapter(
        spec.endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, content=raw)
        ),
    )
    try:
        with pytest.raises(SubmissionUncertain) as failure:
            await adapter.submit(wire, **invocation(wire))
        assert secret not in str(failure.value) and not adapter._responses
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.asyncio
async def test_cancellation_preserves_forwarding_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    wire, spec, _, _ = wire_for(tmp_path)
    called = asyncio.Event()

    async def handler(request: httpx2.Request) -> httpx2.Response:
        called.set()
        await asyncio.Event().wait()
        raise AssertionError

    adapter = p.VertexAdapter(
        spec.endpoint, spec.model, http_transport=httpx2.MockTransport(handler)
    )
    if when == "before":

        def cancel(*args: Any, **kwargs: Any) -> Any:
            raise asyncio.CancelledError

        monkeypatch.setattr(p, "_sdk_arguments", cancel)
    try:
        task = asyncio.create_task(adapter.submit(wire, **invocation(wire)))
        if when == "after":
            await asyncio.wait_for(called.wait(), timeout=5)
            task.cancel()
        with pytest.raises(ProviderInvocationCancelled) as failure:
            await task
        assert failure.value.invoked is (when == "after")
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_adc_unavailable_is_distinct_and_does_not_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google import auth
    from google.auth.exceptions import DefaultCredentialsError

    monkeypatch.setenv("JUDGMENTLLM_VERTEX_AUTH_MODE", "adc")

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise DefaultCredentialsError("PRIVATE_ADC_PATH_AND_IDENTITY")

    monkeypatch.setattr(auth, "default", unavailable)
    wire, spec, _, _ = wire_for(tmp_path)
    adapter = p.VertexAdapter(
        spec.endpoint,
        spec.model,
        http_transport=httpx2.MockTransport(
            lambda request: pytest.fail("No inference forwarding permitted")
        ),
    )
    try:
        with pytest.raises(TransportNotSubmitted) as failure:
            await adapter.submit(wire, **invocation(wire))
        assert "PRIVATE" not in str(failure.value) and adapter.actual_call_count == 0
    finally:
        await adapter.aclose()
