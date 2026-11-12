"""Pinned Vertex SDK routing, physical bytes and authentication isolation.

Every transport and ADC result is synthetic. Native candidates use the SDK
serialization seam only; the production adapter continues to refuse them.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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
                '{"winner_id":"essay_02"}'
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


@pytest.mark.parametrize("mode", ["api_key", "adc"])
@pytest.mark.parametrize("close_failure", [False, True])
@pytest.mark.asyncio
async def test_confirmed_permission_denial_discards_unread_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: str,
    close_failure: bool,
) -> None:
    """Even an unreadable body or failed close cannot erase confirmed rejection."""
    if mode == "adc":
        adc(monkeypatch)
    wire, spec, _, context = wire_for(tmp_path)
    closed: list[bool] = []
    reads: list[bool] = []
    states: list[dict[str, Any]] = []

    class PrivateStream(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            reads.append(True)
            yield b"PRIVATE_UNREAD_CANARY"
            raise AssertionError("A rejected response body must never be read")

        async def aclose(self) -> None:
            closed.append(True)
            state = p._INVOCATION_STATE.get()
            assert state is not None
            states.append(state)
            if close_failure:
                raise RuntimeError("PRIVATE_CLOSE_CANARY: 403 PERMISSION_DENIED")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A rejected body must not enter privacy parsing")

    monkeypatch.setattr(p, "_contains_credential", forbidden)
    response = httpx2.Response(403, stream=PrivateStream())
    adapter = p.VertexAdapter(
        spec.endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(lambda request: response),
    )
    try:
        with pytest.raises(p.VertexRequestRejected) as failure:
            await adapter.submit(wire, **invocation(wire))
        assert failure.value.diagnostic_code == "permission_denied"
        assert response.is_closed and closed == [True] and not reads
        assert states == [{"invoked": True}]  # No raw body or body hash handoff.
        assert not adapter._responses and adapter.actual_call_count == 1
        assert "PRIVATE_CLOSE_CANARY" not in str(failure.value) + caplog.text
        with pytest.raises(ProviderIntegrityError):
            await adapter.submit(wire, **invocation(wire))
        assert adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("mode", ["api_key", "adc"])
@pytest.mark.parametrize("status", [400, 401, 404, 422, 429, 500, 503, 307, 418])
@pytest.mark.asyncio
async def test_other_statuses_keep_private_bodies_withheld(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, status: int
) -> None:
    if mode == "adc":
        adc(monkeypatch)
    wire, spec, _, context = wire_for(tmp_path)
    response = httpx2.Response(
        status,
        json={"error": {"code": 403, "message": context.runtime.project}},
    )
    adapter = p.VertexAdapter(
        spec.endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(lambda request: response),
    )
    try:
        with pytest.raises(SubmissionUncertain) as failure:
            await adapter.submit(wire, **invocation(wire))
        assert failure.value.invoked and response.is_closed
        assert str(failure.value) == "Vertex response cannot be retained safely"
        assert not adapter._responses and adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("mode", ["api_key", "adc"])
@pytest.mark.parametrize("kind", ["missing_response", "status_only", "connection"])
@pytest.mark.asyncio
async def test_permission_classification_requires_an_http_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, kind: str
) -> None:
    if mode == "adc":
        adc(monkeypatch)
    wire, spec, _, context = wire_for(tmp_path)

    def handler(request: httpx2.Request) -> Any:
        if kind == "connection":
            raise httpx2.ConnectError("PRIVATE_CONNECTION_CANARY: HTTP 403")
        return SimpleNamespace(status_code=403) if kind == "status_only" else None

    adapter = p.VertexAdapter(
        spec.endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(handler),
    )
    try:
        with pytest.raises(SubmissionUncertain) as failure:
            await adapter.submit(wire, **invocation(wire))
        assert failure.value.invoked
        assert "403" not in str(failure.value)
        assert not adapter._responses and adapter.actual_call_count == 1
        with pytest.raises(ProviderIntegrityError):
            await adapter.submit(wire, **invocation(wire))
        assert adapter.actual_call_count == 1
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
                '{"winner_id":"essay_02"}'
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


@pytest.mark.asyncio
async def test_pinned_sdk_permission_denial_has_integer_http_code() -> None:
    """The SDK exposes HTTP 403 as .code, independently of private error prose."""
    from google.genai import errors

    response = httpx2.Response(
        403,
        json={
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "message": (
                    "Permission 'aiplatform.endpoints.predict' denied on resource "
                    "'projects/synthetic-project/locations/global/publishers/google/"
                    "models/synthetic-model' (or it may not exist)."
                ),
            }
        },
    )
    assert p.vertex_sdk_version() == "2.34.0"
    with pytest.raises(errors.ClientError) as failure:
        await cast(Any, errors.APIError).raise_for_async_response(response)
    assert type(failure.value.code) is int and failure.value.code == 403
    assert failure.value.status == "PERMISSION_DENIED"
    assert cast(Any, failure.value).response is response


@pytest.mark.parametrize("mode", ["api_key", "adc"])
@pytest.mark.parametrize("sensitive", ["project", "credential"])
@pytest.mark.parametrize("escaped", [False, True])
@pytest.mark.asyncio
async def test_private_permission_denial_is_withheld_before_sdk_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: str,
    sensitive: str,
    escaped: bool,
) -> None:
    """Preserve confirmed 403 status while withholding private/escaped bodies."""
    from google.genai import errors

    if mode == "adc":
        adc(monkeypatch)
    wire, spec, _, context = wire_for(tmp_path)
    secret = (
        context.runtime.project
        if sensitive == "project"
        else "SYNTHETIC_SELECTED_VERTEX_KEY"
        if mode == "api_key"
        else "SYNTHETIC_ADC_ACCESS_TOKEN"
    )
    raw = json.dumps(
        {
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "message": (
                    "Permission 'aiplatform.endpoints.predict' denied on resource "
                    f"'projects/{secret}/locations/global/publishers/google/models/"
                    "synthetic-model' (or it may not exist)."
                ),
            }
        }
    ).encode()
    if escaped:
        raw = raw.replace(
            secret.encode(), "".join(f"\\u{ord(c):04x}" for c in secret).encode()
        )
    classified: list[int] = []
    original = cast(Any, errors.APIError).raise_for_async_response

    async def classify(response: Any) -> None:
        classified.append(response.status_code)
        await original(response)

    monkeypatch.setattr(errors.APIError, "raise_for_async_response", classify)
    response = httpx2.Response(403, content=raw)
    adapter = p.VertexAdapter(
        spec.endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(lambda request: response),
    )
    try:
        with pytest.raises(p.VertexRequestRejected) as failure:
            await adapter.submit(wire, **invocation(wire))
        assert failure.value.diagnostic_code == "permission_denied"
        assert str(failure.value) == (
            "Vertex rejected the request before assessment acceptance"
        )
        assert secret not in str(failure.value)
        assert secret not in caplog.text and response.is_closed
        assert not classified and not adapter._responses
        assert adapter.actual_call_count == 1
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(wire, **invocation(wire))
        with pytest.raises(ProviderIntegrityError):
            await adapter.submit(wire, **invocation(wire))
        assert adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("mode", ["api_key", "adc"])
@pytest.mark.parametrize("when", ["before_forward", "after_forward"])
@pytest.mark.parametrize("kind", ["runtime", "value", "timeout", "unknown_sdk"])
@pytest.mark.asyncio
async def test_unclassified_errors_preserve_forwarding_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    when: str,
    kind: str,
) -> None:
    from google.genai import errors

    if mode == "adc":
        adc(monkeypatch)
    wire, spec, _, context = wire_for(tmp_path)
    detail = "PRIVATE_EXCEPTION_CANARY: 403 PERMISSION_DENIED"
    error = {
        "runtime": RuntimeError(detail),
        "value": ValueError(detail),
        "timeout": httpx2.ReadTimeout(detail),
        "unknown_sdk": errors.APIError(418, {"message": detail}),
    }[kind]

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    if when == "before_forward":
        monkeypatch.setattr(p, "_sdk_arguments", fail)
    adapter = p.VertexAdapter(
        spec.endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(fail),
    )
    forwarded = when == "after_forward"
    try:
        with pytest.raises(
            SubmissionUncertain if forwarded else TransportNotSubmitted
        ) as failure:
            await adapter.submit(wire, **invocation(wire))
        if isinstance(failure.value, SubmissionUncertain):
            assert failure.value.invoked
        else:
            assert failure.value.diagnostic_code is None
        assert "PRIVATE_EXCEPTION_CANARY" not in str(failure.value)
        assert "403" not in str(failure.value)
        assert not adapter._responses
        assert adapter.actual_call_count == int(forwarded)
        with pytest.raises(ProviderIntegrityError):
            await adapter.submit(wire, **invocation(wire))
        assert adapter.actual_call_count == int(forwarded)
    finally:
        await adapter.aclose()
