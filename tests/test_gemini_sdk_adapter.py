"""Actual pinned SDK boundaries with intercepted HTTP and synthetic credentials."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast

import httpx2
import pytest
from gemini_fixture import wire_for
from gemini_offline import authorize_fixture, envelope, isolate

from judgmentllm.config import GenerationSpec
from judgmentllm.gemini_transport import PreparedRequestV6, production_context_digest
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.providers.gemini import (
    GeminiAdapter,
    GeminiReadinessError,
    GeminiRequestRejected,
)
from judgmentllm.providers.protocol import (
    ProviderIntegrityError,
    ProviderInvocationCancelled,
    RetrievalUnavailable,
    SubmissionUncertain,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


def invocation(wire: PreparedRequestV6) -> dict[str, str]:
    return {
        "submission_id": wire.transport_plan.identity.submission_id,
        "correlation_id": wire.transport_plan.identity.correlation_id,
    }


@pytest.mark.parametrize("mode", ["text", "native"])
@pytest.mark.parametrize("advanced", [False, True])
@pytest.mark.asyncio
async def test_custom_templates_and_complete_exemplars_keep_exact_protected_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["text", "native"],
    advanced: bool,
) -> None:
    from gemini_fixture import case

    from judgmentllm.gemini_transport import (
        prepare_production_request,
        production_context,
    )
    from judgmentllm.models import Exemplar
    from judgmentllm.transport import physical_identity

    probe, _, _, probe_context = wire_for(
        tmp_path, grouped=False, probe=True, policy="native"
    )
    authorize_fixture(
        monkeypatch,
        probe_context,
        probe.output_commitment.selection.features,
        "gemini.generate_content.response_json_schema",
    )
    spec, dep, _ = case(tmp_path, grouped=False, policy=mode)
    spec.exemplars = [
        Exemplar(
            exemplar_id="0001",
            text="PROTECTED_EXEMPLAR_雪\nsecond line",
            scores={"01": "low 雪"},
            rationales={"01": "COMPLETE_EXEMPLAR_RATIONALE"},
        )
    ]
    spec.prompt = spec.prompt.model_copy(
        update={"advanced": advanced, "preset": None, "file": tmp_path / "custom.j2"}
    )
    template = (
        "{% for entry in traits %}{{ entry['block'] }}\n{% endfor %}"
        "{% for entry in exemplar_items %}{{ entry['block'] }}\n{% endfor %}"
        "{{ response }}\n{{ output_instructions }}"
        if advanced
        else "{{ trait }}\n{{ exemplars }}\n{{ response }}\n{{ output_instructions }}"
    )
    wire = prepare_production_request(
        spec,
        dep,
        physical_identity("001", dep.request.request_uid, "01"),
        production_context(spec),
        template=template,
    )
    seen: list[bytes] = []

    def response(request: httpx2.Request) -> httpx2.Response:
        assert request.content == wire.payload
        prompt = json.loads(request.content)["contents"][0]["parts"][0]["text"]
        assert "PROTECTED_EXEMPLAR_雪\nsecond line" in prompt
        assert "COMPLETE_EXEMPLAR_RATIONALE" in prompt and "0001" in prompt
        assert dep.request.response is not None and dep.request.response.text in prompt
        seen.append(request.content)
        return httpx2.Response(
            200,
            json=envelope('{"scores":[{"trait_id":"01","category_label":"low 雪"}]}'),
        )

    adapter = GeminiAdapter(
        spec.endpoint, spec.model, http_transport=httpx2.MockTransport(response)
    )
    try:
        await adapter.submit(wire, **invocation(wire))
        observed = await adapter.retrieve(wire, **invocation(wire))
        assert adapter.parse(observed).kind == "response" and len(seen) == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("mechanism", ["response_schema", "response_json_schema"])
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
@pytest.mark.parametrize(
    "generation", [None, GenerationSpec(temperature=0, top_p=1, max_output_tokens=64)]
)
@pytest.mark.asyncio
async def test_exact_physical_sdk_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mechanism: str,
    policy: str,
    scenario: str,
    generation: GenerationSpec | None,
) -> None:
    route = "gemini.generate_content." + mechanism
    options: dict[str, Any] = dict(
        policy=policy,
        grouped=scenario != "single",
        pairwise=scenario in {"pairwise", "ties"},
        ties=scenario == "ties",
        explanations=scenario in {"together", "scores_first", "explanations_first"},
        layout=scenario
        if scenario in {"scores_first", "explanations_first"}
        else "together",
        generation=generation,
        mechanism=route,
    )
    candidate, _, _, context = wire_for(tmp_path, probe=True, **options)
    authorize_fixture(
        monkeypatch,
        context,
        candidate.output_commitment.selection.features,
        route,
        generation=generation,
    )
    wire, spec, _, context = wire_for(tmp_path, **options)
    bodies: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert (
            str(request.url)
            == "https://generativelanguage.googleapis.com/v1beta/models/offline-qualified-model:generateContent"
        )
        assert request.headers["x-goog-api-key"] == "SYNTHETIC_SELECTED_GEMINI_KEY"
        assert "authorization" not in request.headers
        assert request.content == wire.payload
        bodies.append(request.content)
        return httpx2.Response(
            200,
            json=envelope(
                '{"winner_id":"essay_02"}'
                if spec.task_kind == "pairwise"
                else '{"scores":[]}'
            ),
        )

    adapter = GeminiAdapter(
        spec.endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(handler),
    )
    try:
        adapter.validate(spec)
        adapter.validate_credentials()
        await adapter.submit(wire, **invocation(wire))
        response = await adapter.retrieve(wire, **invocation(wire))
        assert adapter.parse(response).kind == "response"
        assert len(bodies) == adapter.actual_call_count == 1
        physical = json.loads(wire.payload)
        config = physical["generationConfig"]
        assert config["candidateCount"] == 1
        if generation is None:
            assert not {"temperature", "topP", "maxOutputTokens"}.intersection(config)
        else:
            assert config["temperature"] == 0.0 and type(config["temperature"]) is float
            assert config["topP"] == 1.0 and config["maxOutputTokens"] == 64
        if policy != "text":
            projected = wire.output_commitment.projection
            assert projected is not None
            assert config[
                "responseSchema"
                if mechanism == "response_schema"
                else "responseJsonSchema"
            ] == json.loads(projected.native_schema_text)
        # A physical identity cannot be used twice, even after handoff consumption.
        with pytest.raises(ProviderIntegrityError):
            await adapter.submit(wire, **invocation(wire))
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(wire, **invocation(wire))
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "finish", ["CONTINUATION", "MAX_TOKENS", "UNEXPECTED_TOOL_CALL"]
)
@pytest.mark.asyncio
async def test_no_continuation_or_automatic_tool_request(
    tmp_path: Path, finish: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import google.genai.models as sdk_models

    original = cast(Any, sdk_models.AsyncModels).generate_content

    async def checked(self: Any, **kwargs: Any) -> Any:
        config = kwargs["config"]
        assert config.automatic_continuation is False
        assert config.automatic_function_calling.disable is True
        assert config.tools is None and config.continuation_token is None
        assert config.should_return_http_response is True
        return await original(self, **kwargs)

    monkeypatch.setattr(sdk_models.AsyncModels, "generate_content", checked)
    wire, spec, _, context = wire_for(tmp_path)
    body = envelope('{"scores":[]}')
    body["candidates"][0].update(finishReason=finish, continuationToken="c3ludGhldGlj")
    calls: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.content)
        return httpx2.Response(200, json=body)

    adapter = GeminiAdapter(
        spec.endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(handler),
    )
    try:
        await adapter.submit(wire, **invocation(wire))
        response = await adapter.retrieve(wire, **invocation(wire))
        assert response.kind == "provider_error" and len(calls) == 1
    finally:
        await adapter.aclose()


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
async def test_http_failure_no_retry_or_redirect(
    tmp_path: Path, status: int, diagnostic: str | None
) -> None:
    wire, spec, _, _ = wire_for(tmp_path)
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        return httpx2.Response(
            status,
            headers={"location": "https://forbidden.invalid"},
            json={
                "error": {
                    "code": status,
                    "status": "RESOURCE_EXHAUSTED"
                    if status == 429
                    else "INVALID_ARGUMENT",
                    "message": "ARBITRARY_PRIVATE_PROVIDER_DETAIL",
                }
            },
        )

    adapter = GeminiAdapter(
        spec.endpoint, spec.model, http_transport=httpx2.MockTransport(handler)
    )
    try:
        with pytest.raises(
            GeminiRequestRejected if diagnostic else SubmissionUncertain
        ) as failure:
            await adapter.submit(wire, **invocation(wire))
        assert "ARBITRARY_PRIVATE_PROVIDER_DETAIL" not in str(failure.value)
        if diagnostic:
            assert (
                cast(GeminiRequestRejected, failure.value).diagnostic_code == diagnostic
            )
        else:
            assert cast(SubmissionUncertain, failure.value).invoked is True
        assert len(calls) == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("echo", ["direct", "escaped"])
@pytest.mark.asyncio
async def test_credential_echo_never_reaches_retained_envelope(
    tmp_path: Path, echo: str
) -> None:
    wire, spec, _, _ = wire_for(tmp_path)
    key = "SYNTHETIC_SELECTED_GEMINI_KEY"
    raw = json.dumps(envelope(key)).encode()
    if echo == "escaped":
        raw = raw.replace(
            key.encode(), "".join(f"\\u{ord(c):04x}" for c in key).encode()
        )
    adapter = GeminiAdapter(
        spec.endpoint,
        spec.model,
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, content=raw)
        ),
    )
    try:
        with pytest.raises(SubmissionUncertain) as failure:
            await adapter.submit(wire, **invocation(wire))
        assert key not in str(failure.value)
        assert not adapter._responses
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_documented_invalid_key_is_rejected_without_provider_prose(
    tmp_path: Path,
) -> None:
    wire, spec, _, _ = wire_for(tmp_path)
    body = {
        "error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "message": "PRIVATE_PROVIDER_PROSE",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "API_KEY_INVALID",
                    "domain": "googleapis.com",
                }
            ],
        }
    }
    from judgmentllm.gemini_diagnostics import averify_authentication

    transport = httpx2.MockTransport(lambda request: httpx2.Response(400, json=body))
    verified = await averify_authentication(http_transport=transport)
    assert verified.diagnostic_code == "credentials_rejected"
    assert "PRIVATE_PROVIDER_PROSE" not in verified.model_dump_json()
    adapter = GeminiAdapter(spec.endpoint, spec.model, http_transport=transport)
    try:
        with pytest.raises(GeminiRequestRejected) as failure:
            await adapter.submit(wire, **invocation(wire))
        assert failure.value.diagnostic_code == "credentials_rejected"
        assert "PRIVATE_PROVIDER_PROSE" not in str(failure.value)
        assert adapter.actual_call_count == 1 and not adapter._responses
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_credential_precedence_fallback_and_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire, spec, _, _ = wire_for(tmp_path)
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers["x-goog-api-key"])
        return httpx2.Response(200, json=envelope('{"scores":[]}'))

    for selected, expected in (
        (True, "SYNTHETIC_SELECTED_GEMINI_KEY"),
        (False, "SYNTHETIC_SECONDARY_GEMINI_KEY"),
    ):
        if not selected:
            monkeypatch.delenv("GOOGLE_API_KEY")
        adapter = GeminiAdapter(
            spec.endpoint, spec.model, http_transport=httpx2.MockTransport(handler)
        )
        try:
            await adapter.submit(wire, **invocation(wire))
            assert seen[-1] == expected
        finally:
            await adapter.aclose()
    monkeypatch.delenv("GEMINI_API_KEY")
    adapter = GeminiAdapter(
        spec.endpoint, spec.model, http_transport=httpx2.MockTransport(handler)
    )
    with pytest.raises(GeminiReadinessError, match="GOOGLE_API_KEY"):
        adapter.validate_credentials()
    # ADC is configured by the fixture, but cannot supply a Developer credential.
    with pytest.raises(Exception, match="GOOGLE_API_KEY"):
        await adapter.submit(wire, **invocation(wire))
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_candidate_and_wrong_scope_never_construct_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire, spec, _, context = wire_for(tmp_path, probe=True, policy="native")
    adapter = GeminiAdapter(spec.endpoint, spec.model, context=context)
    with pytest.raises(ProviderIntegrityError, match="candidates cannot execute"):
        await adapter.submit(wire, **invocation(wire))
    assert adapter._client is None
    with pytest.raises(ProviderIntegrityError):
        GeminiAdapter(spec.endpoint, "different-model", context=context)
    with pytest.raises(ValueError):
        production_context_digest(replace(context, source_commit="bad"))
    with pytest.raises(ValueError):
        PreparedRequestV6.model_validate(dict(wire) | {"payload": b"{}"})
    assert (
        "SYNTHETIC_SELECTED" not in canonical_bytes(wire.transport_commitment).decode()
    )


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.asyncio
async def test_cancellation_reports_physical_forwarding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    wire, spec, _, _ = wire_for(tmp_path)
    called = asyncio.Event()

    async def handler(request: httpx2.Request) -> httpx2.Response:
        called.set()
        await asyncio.Event().wait()
        raise AssertionError

    adapter = GeminiAdapter(
        spec.endpoint, spec.model, http_transport=httpx2.MockTransport(handler)
    )
    if when == "before":
        from judgmentllm.providers import gemini

        original = gemini._sdk_arguments

        def cancel(*args: Any, **kwargs: Any) -> Any:
            original(*args, **kwargs)
            raise asyncio.CancelledError

        monkeypatch.setattr(gemini, "_sdk_arguments", cancel)
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
