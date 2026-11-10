"""Adversarial diagnostic identities, readiness and authority fail closed."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any

import httpx2
import pytest
from test_vertex_lifecycle import envelope
from vertex_fixture import wire_for

from judgmentllm import vertex_assessment as a
from judgmentllm.evidence._base import EvidenceIdentity
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import ArtifactIdentity
from judgmentllm.providers import vertex as p
from judgmentllm.providers.protocol import (
    ProviderError,
    ProviderIntegrityError,
    SubmissionUncertain,
)
from judgmentllm.providers.vertex_response import _extract_assessment_component


def diagnostic() -> a.VertexAssessmentDiagnosticV4:
    component = _extract_assessment_component(
        envelope("models.generate_content", '{"winner_id":"001"}'),
        "models.generate_content",
    )
    assert component.output_text is not None
    digest = bytes_digest(component.output_text)
    return a.VertexAssessmentDiagnosticV4(
        identity=EvidenceIdentity(
            run_id="001",
            request_uid="request-001",
            attempt_id="attempt-001",
            submission_id="submission-001",
            observation_id="observation-001",
        ),
        endpoint="models.generate_content",
        requested_model="offline-model",
        remote_response_id="msg-offline",
        provider_outcome="completed",
        component=a.VertexAssessmentComponentMetadataV4.model_validate_json(
            component.model_dump_json()
        ),
        parser_diagnostic="valid",
        canonical_valid=True,
        capture="full",
        retention="retained",
        artifact=ArtifactIdentity(
            relative_path=f"objects/{digest[:2]}/{digest}",
            sha256=digest,
            byte_length=len(component.output_text),
            media_type="text/plain; charset=utf-8",
        ),
        content_sha256=digest,
        byte_length=len(component.output_text),
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"component_version": 4.0},
        {"component_version": True},
        {"source": "unavailable"},
        {"message_count": 2},
        {"item_index": 1},
        {"content_index": 1},
        {"text_component_count": 0},
        {"item_types": ["tool"]},
        {"content_types": ["output_text", "output_text"]},
        {"extraction_status": "no_text"},
        {"extraction_status": "extracted", "byte_length": None},
        {"extraction_status": "oversized", "byte_length": 1},
        {"extraction_status": "invalid_utf8"},
    ],
)
def test_forged_component_metadata_cannot_be_replayed(changed: dict[str, Any]) -> None:
    component = diagnostic().component.model_dump(mode="json")
    with pytest.raises(ValueError):
        a.VertexAssessmentComponentMetadataV4.model_validate_json(
            canonical_bytes(component | changed)
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"diagnostic_version": 4.0},
        {"diagnostic_version": True},
        {"byte_length": 1},
        {"content_sha256": "f" * 64},
        {"retention": "withheld"},
        {"capture": "minimal"},
        {"parser_diagnostic": "invalid_json"},
        {"artifact": None},
        {"retention": "hash_only"},
    ],
)
def test_forged_diagnostic_cannot_change_original_text_identity(
    changed: dict[str, Any],
) -> None:
    value = diagnostic().model_dump(mode="json")
    with pytest.raises(ValueError):
        a.VertexAssessmentDiagnosticV4.model_validate_json(
            canonical_bytes(value | changed)
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("relative_path", "objects/ff/" + "f" * 64),
        ("media_type", "application/json"),
        ("byte_length", 1),
    ],
)
def test_forged_diagnostic_artifact_cannot_change_retention(
    field: str, value: Any
) -> None:
    document = diagnostic().model_dump(mode="json")
    document["artifact"][field] = value
    with pytest.raises(ValueError):
        a.VertexAssessmentDiagnosticV4.model_validate_json(canonical_bytes(document))


def test_diagnostic_requires_a_real_observation_identity() -> None:
    document = diagnostic().model_dump(mode="json")
    document["identity"]["observation_id"] = None
    with pytest.raises(ValueError):
        a.VertexAssessmentDiagnosticV4.model_validate_json(canonical_bytes(document))


def test_missing_provider_extra_has_no_sensitive_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = p.importlib.import_module

    def missing(name: str) -> Any:
        if name == "google.genai":
            raise ImportError("PRIVATE_IMPORT_CANARY")
        return original(name)

    monkeypatch.setattr(p.importlib, "import_module", missing)
    with pytest.raises(ProviderError) as error:
        p._sdk()
    assert "PRIVATE" not in str(error.value)

    def missing_distribution(name: str) -> str:
        raise PackageNotFoundError("PRIVATE_DISTRIBUTION_CANARY")

    monkeypatch.setattr(p, "version", missing_distribution)
    with pytest.raises(ProviderError) as error:
        p.vertex_sdk_version()
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "endpoint,concurrency", [("responses", 1), ("models.generate_content", 0)]
)
def test_invalid_endpoint_and_connection_bounds_fail_before_sdk(
    endpoint: str, concurrency: int
) -> None:
    with pytest.raises(ProviderError):
        p.VertexAdapter(endpoint, "offline-model", concurrency=concurrency)


@pytest.mark.asyncio
async def test_client_constructor_failure_is_sanitized_and_does_not_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "SYNTHETIC_READINESS")
    wire, spec, _, context = wire_for(tmp_path)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("PRIVATE_CLIENT_CANARY")

    monkeypatch.setattr(p, "_protected_client", fail)
    adapter = p.VertexAdapter(
        "models.generate_content",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(fail),
    )
    try:
        with pytest.raises(ProviderIntegrityError) as error:
            await adapter.submit(
                wire,
                submission_id=wire.transport_plan.identity.submission_id,
                correlation_id=wire.transport_plan.identity.correlation_id,
            )
        assert "PRIVATE" not in str(error.value)
        assert adapter.actual_call_count == 0
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("body", [b"not JSON", b'{"type":"message"}', b"\xff"])
@pytest.mark.asyncio
async def test_missing_or_unreadable_remote_identity_remains_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "SYNTHETIC_ENVELOPE")
    wire, spec, _, context = wire_for(tmp_path)
    adapter = p.VertexAdapter(
        "models.generate_content",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, content=body, headers={"content-type": "application/json"}
            )
        ),
    )
    try:
        with pytest.raises(SubmissionUncertain):
            await adapter.submit(
                wire,
                submission_id=wire.transport_plan.identity.submission_id,
                correlation_id=wire.transport_plan.identity.correlation_id,
            )
        assert adapter.actual_call_count == 1
        assert (
            await adapter.reconcile(
                wire,
                submission_id=wire.transport_plan.identity.submission_id,
                correlation_id=wire.transport_plan.identity.correlation_id,
            )
        ).outcome == "unresolved"
    finally:
        await adapter.aclose()


def test_scope_and_current_generation_restrictions_fail_before_submission(
    tmp_path: Path,
) -> None:
    from judgmentllm.config import GenerationSpec

    wire, spec, dependency, context = wire_for(tmp_path)
    with pytest.raises(ProviderIntegrityError):
        p.VertexAdapter("models.generate_content", "another-model", context=context)
    with pytest.raises(ProviderError):
        p.VertexAdapter.capabilities_for(
            "openai", "models.generate_content", spec.model
        )
    adapter = p.VertexAdapter("models.generate_content", spec.model, context=context)
    for changed in (
        {"model": "another-model"},
        {"schema_version": 2},
        {"generation": GenerationSpec(temperature=3)},
        {"execution": spec.execution.model_copy(update={"cache_policy": "explicit"})},
        {"execution": spec.execution.model_copy(update={"tier": "priority"})},
    ):
        with pytest.raises(ProviderError):
            adapter.validate(spec.model_copy(update=changed))
    from judgmentllm.vertex_prompts import render_vertex_request

    response = dependency.request.response
    assert response is not None
    rendered = render_vertex_request(
        spec,
        None,
        dependency.request,
        lambda kind, identifier: response,
        wire.output_commitment,
    )
    with pytest.raises(ProviderIntegrityError):
        adapter.prepare(spec, dependency, rendered)
    assert adapter.actual_call_count == 0


def test_credential_preflight_missing_sdk_extra_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "SYNTHETIC_READINESS")

    def missing() -> str:
        raise ProviderError("PRIVATE_EXTRA_CANARY")

    monkeypatch.setattr(p, "vertex_sdk_version", missing)
    with pytest.raises(p.VertexReadinessError) as error:
        p.VertexAdapter(
            "models.generate_content", "offline-model"
        ).validate_credentials()
    assert error.value.diagnostic_code == "missing_extra"
    assert "PRIVATE" not in str(error.value)


@pytest.mark.asyncio
async def test_response_handoff_capacity_identity_and_parse_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "SYNTHETIC_HANDOFF")
    wire, spec, _, context = wire_for(tmp_path)
    identity = wire.transport_plan.identity
    adapter = p.VertexAdapter(
        "models.generate_content",
        spec.model,
        context=context,
        concurrency=1,
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, json=envelope("models.generate_content", "{}")
            )
        ),
    )
    adapter.validate(spec)
    try:
        await adapter.submit(
            wire,
            submission_id=identity.submission_id,
            correlation_id=identity.correlation_id,
        )
        with pytest.raises(ProviderIntegrityError):
            await adapter.submit(
                wire,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
        with pytest.raises(ProviderIntegrityError):
            await adapter.reconcile(
                wire, submission_id=identity.submission_id, correlation_id="foreign"
            )
        with pytest.raises(ProviderIntegrityError):
            await adapter.retrieve(
                wire, submission_id=identity.submission_id, correlation_id="foreign"
            )
        # A wrong identity cannot consume the original received observation.
        assert (
            await adapter.reconcile(
                wire,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
        ).outcome == "observation_available"
        observed = await adapter.retrieve(
            wire,
            submission_id=identity.submission_id,
            correlation_id=identity.correlation_id,
        )
        forged = observed.model_copy(update={"observation_key": "foreign"})
        with pytest.raises(ValueError):
            adapter.parse(forged)
        assert adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_client_connection_bounds_cannot_change_after_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "SYNTHETIC_BOUNDS")
    _, spec, _, context = wire_for(tmp_path)
    adapter = p.VertexAdapter(
        "models.generate_content",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(lambda request: httpx2.Response(500)),
    )
    adapter.validate(spec)
    try:
        await adapter._execution_client()
        changed = spec.model_copy(
            update={
                "execution": spec.execution.model_copy(
                    update={"concurrency": spec.execution.concurrency + 1}
                )
            }
        )
        with pytest.raises(ProviderIntegrityError):
            adapter.validate(changed)
        assert adapter.actual_call_count == 0
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_oversized_http_response_consumes_one_uncertain_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "SYNTHETIC_RESPONSE_BOUND")
    wire, spec, _, context = wire_for(tmp_path)

    class LargeResponse(httpx2.AsyncByteStream):
        async def __aiter__(self):
            chunk = b" " * 1_048_576
            for _ in range(9):
                yield chunk

    adapter = p.VertexAdapter(
        "models.generate_content",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, stream=LargeResponse())
        ),
    )
    try:
        with pytest.raises(SubmissionUncertain):
            await adapter.submit(
                wire,
                submission_id=wire.transport_plan.identity.submission_id,
                correlation_id=wire.transport_plan.identity.correlation_id,
            )
        assert adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    from vertex_offline import isolate

    isolate(monkeypatch)
