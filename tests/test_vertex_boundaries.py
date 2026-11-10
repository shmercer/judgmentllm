"""Adversarial source, type, HTTP, retention and authorization boundaries."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import logging
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from test_vertex_integrity import diagnostic
from test_vertex_sdk_adapter import invocation
from vertex_fixture import wire_for
from vertex_offline import envelope, isolate

from judgmentllm import providers as providers
from judgmentllm import vertex_types
from judgmentllm.config import GenerationSpec, RunSpecV2
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.providers import vertex as adapter_module
from judgmentllm.providers.protocol import ProviderIntegrityError
from judgmentllm.providers.vertex_response import (
    _extract_assessment_component,
)
from judgmentllm.transport import TransportIntegrityError
from judgmentllm.vertex_assessment import (
    VertexAssessmentComponentV4,
    VertexAssessmentDiagnosticV4,
)
from judgmentllm.vertex_prompts import RenderedVertexPromptV1
from judgmentllm.vertex_transport import (
    OutputCommitmentV7,
    TransportCommitmentV5,
    _validate_scope,
    build_vertex_request_body,
    model_path,
    prepare_production_request,
    production_context,
    production_context_digest,
    production_context_for_identity,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


def test_lazy_concrete_types_keep_dispatch_identity() -> None:
    assert vertex_types.PreparedRequestV7.__name__ == "PreparedRequestV7"
    assert vertex_types.ExecutableRunManifestV7.__name__ == "ExecutableRunManifestV7"
    assert vertex_types.VertexAssessmentDiagnosticV4 is VertexAssessmentDiagnosticV4
    with pytest.raises(AttributeError, match="Unknown Vertex type"):
        _ = cast(Any, vertex_types).PRIVATE_UNREGISTERED_TYPE


@pytest.mark.parametrize(
    "model",
    [
        "../other",
        "model?key=x",
        "https://other",
        "tunedModels/private",
        "models/../private",
        "space model",
        "",
        "a" * 201,
    ],
)
def test_model_path_cannot_change_endpoint(model: str) -> None:
    with pytest.raises(ValueError):
        model_path(model)
    assert (
        model_path("unknown-model-001") == "publishers/google/models/unknown-model-001"
    )
    with pytest.raises(ValueError):
        model_path("models/unknown-model-001")


def test_context_is_descriptive_and_never_cloud_or_old_configuration(
    tmp_path: Path,
) -> None:
    _, spec, _, context = wire_for(tmp_path)
    assert (
        context.api_scope == "vertex-generate-content-v1beta1-v1"
        and not context.trusted_synthetic
    )
    assert "scoped production authority" in repr(context)
    for changed in (
        {"source_commit": "a" * 40},
        {"kind": "unsupported"},
        {"kind": "vertex-qualification-candidate-v1"},
        {"kind": "vertex-qualification-candidate-v1", "source_commit": "wrong"},
    ):
        with pytest.raises(ValueError):
            production_context_digest(replace(context, **changed))
    with pytest.raises(ValueError):
        production_context_for_identity("gemini", spec.endpoint, spec.model)
    with pytest.raises(ValueError):
        production_context_for_identity(
            "vertex", spec.endpoint, "judgmentllm-synthetic-native-v1"
        )
    with pytest.raises(ValueError):
        production_context_digest(cast(Any, object()))
    older = RunSpecV2.model_validate(
        dict(spec) | {"schema_version": 2, "prompt": {"structured_output": "text"}}
    )
    with pytest.raises(ValueError):
        production_context(cast(Any, older))


def test_request_body_has_no_json_or_endpoint_escape_hatch(tmp_path: Path) -> None:
    wire, _, _, _ = wire_for(tmp_path)
    values: dict[str, Any] = dict(
        endpoint=wire.endpoint,
        model=wire.model,
        prompt=wire.rendered_text,
        output=wire.output_commitment,
    )
    changes: tuple[dict[str, Any], ...] = (
        {"endpoint": "responses"},
        {"model": "different"},
        {"generation": {}},
        {"generation": GenerationSpec(temperature=3)},
    )
    for change in changes:
        with pytest.raises(ValueError):
            build_vertex_request_body(**(values | change))
    with pytest.raises(ValueError):
        OutputCommitmentV7.model_validate(
            dict(wire.output_commitment) | {"sdk_config_fragment_text": "{}"}
        )
    changed = dict(wire.transport_commitment) | {
        "scope": wire.output_commitment.scope.model_copy(update={"provider": "gemini"})
    }
    with pytest.raises(ValueError):
        TransportCommitmentV5.model_validate(changed)
    native, _, _, _ = wire_for(tmp_path, probe=True, policy="native")
    with pytest.raises(ValueError):
        TransportCommitmentV5.model_validate(
            dict(native.transport_commitment) | {"native_schema_bytes": None}
        )
    with pytest.raises(ValueError):
        RenderedVertexPromptV1(
            text="synthetic",
            exemplar_ids=[],
            output_schema={},
            output_commitment=wire.output_commitment,
        )


def test_original_component_metadata_and_validity_cannot_be_forged() -> None:
    component = _extract_assessment_component(envelope("{}"), "models.generate_content")
    for changed in (
        {"output_text": None},
        {"output_text": b"different"},
        {
            "source": "unavailable",
            "extraction_status": "no_text",
            "item_index": 0,
            "content_index": None,
            "byte_length": None,
            "text_component_count": 0,
        },
    ):
        with pytest.raises(ValueError):
            VertexAssessmentComponentV4.model_validate(dict(component) | changed)
    record = diagnostic().model_dump(mode="json")
    with pytest.raises(ValueError):
        VertexAssessmentDiagnosticV4.model_validate_json(
            canonical_bytes(record | {"canonical_valid": False})
        )
    none = _extract_assessment_component(
        envelope("", candidates=[]), "models.generate_content"
    )
    record.update(
        component=none.model_dump(mode="json"),
        retention="withheld",
        artifact=None,
        content_sha256=None,
        byte_length=None,
    )
    with pytest.raises(ValueError):
        VertexAssessmentDiagnosticV4.model_validate_json(canonical_bytes(record))


@pytest.mark.parametrize(
    "shape",
    [
        "role",
        "content_fields",
        "candidate_fields",
        "usage_fields",
        "usage_cache",
        "unknown_finish",
        "missing_content",
        "prompt_feedback",
    ],
)
def test_unrecognized_envelopes_cannot_retain_private_material(
    tmp_path: Path, shape: str
) -> None:
    wire, _, _, _ = wire_for(tmp_path)
    body = envelope("{}")
    if shape == "role":
        body["candidates"][0]["content"]["role"] = "user"
    elif shape == "content_fields":
        body["candidates"][0]["content"]["private"] = "PRIVATE_CONTENT"
    elif shape == "candidate_fields":
        body["candidates"][0]["finishMessage"] = "PRIVATE_CONTENT"
    elif shape == "usage_fields":
        body["usageMetadata"]["private"] = "PRIVATE_CONTENT"
    elif shape == "usage_cache":
        body["usageMetadata"]["cachedContentTokenCount"] = 999
    elif shape == "unknown_finish":
        body["candidates"][0]["finishReason"] = {"private": "PRIVATE_CONTENT"}
    elif shape == "missing_content":
        body["candidates"][0]["content"] = None
    else:
        body["promptFeedback"] = {"safetyRatings": [{"blocked": "invalid"}]}
    adapter = adapter_module.VertexAdapter(wire.endpoint, wire.model)
    response = adapter._response(
        json.dumps(body).encode(), body, body["responseId"], prepared=wire
    )
    assert response.evidence is not None and response.evidence.safe_response is not None
    assert b"PRIVATE_CONTENT" not in response.evidence.safe_response
    assert not response.evidence.original_body_safe
    if shape in {
        "role",
        "content_fields",
        "unknown_finish",
        "missing_content",
        "prompt_feedback",
    }:
        assert response.kind != "response"


@pytest.mark.parametrize(
    "change",
    ["body", "url", "method", "x-goog-api-key", "authorization", "extra-header"],
)
@pytest.mark.asyncio
async def test_sdk_tampering_is_rejected_before_forwarding(
    tmp_path: Path, change: str
) -> None:
    wire, _, _, _ = wire_for(tmp_path)
    sent: list[httpx2.Request] = []

    def forbidden(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(200, json={})

    adapter = adapter_module.VertexAdapter(
        wire.endpoint, wire.model, http_transport=httpx2.MockTransport(forbidden)
    )
    try:
        client = await adapter._execution_client()

        async def mutate(request: Any) -> None:
            if change == "body":
                request._content = b"{}"
            elif change == "url":
                request.url = httpx2.URL("https://forbidden.invalid")
            elif change == "method":
                request.method = "GET"
            else:
                request.headers[change] = "PRIVATE_CANARY"

        client._judgmentllm_http_client.event_hooks["request"].append(mutate)
        with pytest.raises(ProviderIntegrityError) as failure:
            await adapter.submit(wire, **invocation(wire))
        assert (
            not sent
            and adapter.actual_call_count == 0
            and "PRIVATE_CANARY" not in str(failure.value)
        )
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_sdk_logs_and_local_timeout_policy_are_enforced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    wire, _, _, _ = wire_for(tmp_path)
    caplog.set_level(logging.DEBUG)

    def response(request: httpx2.Request) -> httpx2.Response:
        assert request.extensions["timeout"] == {
            "connect": 10.0,
            "write": 10.0,
            "pool": 10.0,
            "read": 60.0,
        }
        for name in (
            "google_genai._api_client",
            "google_genai.models",
            "google_genai.types",
            "google_genai._transformers",
            "google_genai._extra_utils",
            "httpcore2.http11",
        ):
            logging.getLogger(name).debug("PRIVATE_LOG_CANARY %s", request.headers)
        return httpx2.Response(200, json=envelope("{}"))

    adapter = adapter_module.VertexAdapter(
        wire.endpoint, wire.model, http_transport=httpx2.MockTransport(response)
    )
    try:
        await adapter.submit(wire, **invocation(wire))
    finally:
        await adapter.aclose()
    assert (
        "PRIVATE_LOG_CANARY" not in caplog.text
        and "SYNTHETIC_SELECTED" not in caplog.text
    )


@pytest.mark.parametrize(
    "raw,contains",
    [
        (b"not JSON", False),
        (b"\xff", False),
        (b'{"a":[true,1,{"x":"safe"}]}', False),
        (b'{"text":"\\u0053\\u0045\\u0043\\u0052\\u0045\\u0054"}', True),
        (b'{"text":"\\\\u0053\\\\u0045\\\\u0043\\\\u0052\\\\u0045\\\\u0054"}', True),
    ],
)
def test_credential_echo_screening_precedes_component_capture(
    raw: bytes, contains: bool
) -> None:
    assert adapter_module._contains_credential(raw, "SECRET") is contains


@pytest.mark.parametrize(
    "raw",
    [
        None,
        b"not JSON",
        b"[]",
        b'{"error":{"code":400,"status":"INVALID_ARGUMENT","details":null}}',
        b'{"error":{"code":400,"status":"INVALID_ARGUMENT","details":[null]}}',
        b'{"error":{"code":400,"status":"INVALID_ARGUMENT","details":[{"reason":"API_KEY_INVALID"}]}}',
    ],
)
def test_invalid_key_diagnostic_requires_closed_structured_code(
    raw: bytes | None,
) -> None:
    assert not adapter_module._invalid_api_key(raw)


def test_nested_credential_escaping_fails_closed() -> None:
    assert adapter_module._contains_credential(b"\\u005cu005cu005cu005cu0041", "SECRET")
    assert adapter_module._contains_credential(b'{"key":"SE\\/CRET"}', "SE/CRET")


def test_unreviewed_sdk_version_cannot_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter_module, "vertex_sdk_version", lambda: "2.35.0")
    with pytest.raises(adapter_module.VertexReadinessError) as failure:
        adapter_module.VertexAdapter(
            "models.generate_content", "unknown-model"
        ).validate_credentials()
    assert failure.value.diagnostic_code == "sdk_scope_mismatch"


@pytest.mark.parametrize("change", ["scope", "mechanism", "generation"])
def test_scope_and_known_generation_limits_fail_before_submission(
    tmp_path: Path, change: str
) -> None:
    _, spec, _, context = wire_for(tmp_path)
    if change == "scope":
        spec = spec.model_copy(update={"model": "different"})
    elif change == "mechanism":
        spec.prompt = spec.prompt.model_copy(
            update={"native_mechanism": "anthropic.messages.output_config.format"}
        )
    else:
        spec.generation = GenerationSpec(temperature=3)
    with pytest.raises(ValueError):
        _validate_scope(spec, context)


def test_preparation_rejects_unstable_inputs_and_changed_committed_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import vertex_transport

    wire, spec, dep, context = wire_for(tmp_path)
    identity = wire.transport_plan.identity
    different, _, _, _ = wire_for(tmp_path / "different", probe=True, policy="native")
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec, dep, identity, context, committed_output=different.output_commitment
        )

    def unstable(value: object) -> None:
        return None

    monkeypatch.setattr(vertex_transport, "_preparation_token", unstable)
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(spec, dep, identity, context)


def test_inline_preparation_cannot_resolve_foreign_or_pair_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vertex_fixture import case

    from judgmentllm import vertex_prompts
    from judgmentllm.transport import physical_identity

    spec, dep, _ = case(tmp_path, pairwise=True)
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec,
            dep,
            physical_identity("001", dep.request.request_uid, "01"),
            production_context(spec),
        )
    wire, spec, dep, context = wire_for(tmp_path)

    def foreign(*args: Any) -> Any:
        return args[3]("response", "FOREIGN_ID")

    monkeypatch.setattr(vertex_prompts, "render_vertex_request", foreign)
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(spec, dep, wire.transport_plan.identity, context)


@pytest.mark.asyncio
async def test_submission_and_parse_reject_unsealed_or_missing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.providers.protocol import ProviderResponse, TransportNotSubmitted

    wire, spec, _, _ = wire_for(tmp_path)
    adapter = adapter_module.VertexAdapter(spec.endpoint, spec.model)
    with pytest.raises(ProviderIntegrityError):
        await adapter.submit(cast(Any, object()), **invocation(wire))
    with pytest.raises(ProviderIntegrityError):
        await adapter.submit(wire, submission_id="foreign", correlation_id="foreign")
    adapter.validate_credentials()
    monkeypatch.delenv("JUDGMENTLLM_VERTEX_API_KEY")
    monkeypatch.delenv("JUDGMENTLLM_VERTEX_API_KEY", raising=False)
    with pytest.raises(adapter_module.VertexReadinessError):
        adapter.validate_credentials()
    original = envelope("{}")
    response = ProviderResponse(
        observation_key=original["responseId"],
        kind="response",
        raw=json.dumps(original).encode(),
    )
    assert adapter.parse(response).output == {}
    with pytest.raises(ValueError):
        adapter.parse(response.model_copy(update={"observation_key": "foreign"}))
    derivative_body = original | {"private": "PRIVATE_PROVIDER_THOUGHTS"}
    derivative = adapter._response(
        json.dumps(derivative_body).encode(),
        derivative_body,
        response.observation_key,
        prepared=wire,
    )
    with pytest.raises(ValueError):
        adapter.parse(derivative.model_copy(update={"observation_key": "foreign"}))
    protected = adapter._response(
        response.raw, original, response.observation_key, prepared=wire
    )
    assert protected.evidence is not None
    with pytest.raises(ValueError):
        adapter.parse(
            protected.model_copy(
                update={
                    "evidence": protected.evidence.model_copy(
                        update={"safe_response": None}
                    )
                }
            )
        )

    async def raise_wrapped(**kwargs: Any) -> Any:
        raise RuntimeError("PRIVATE_DETAIL") from TransportNotSubmitted(
            "fixed rejection"
        )

    async def fake_client() -> Any:
        return SimpleNamespace(
            aio=SimpleNamespace(models=SimpleNamespace(generate_content=raise_wrapped))
        )

    monkeypatch.setattr(adapter, "_execution_client", fake_client)
    with pytest.raises(TransportNotSubmitted, match="fixed rejection"):
        await adapter.submit(wire, **invocation(wire))
