"""Adversarial diagnostic identities, readiness and authority fail closed."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any

import httpx2
import pytest
from anthropic_fixture import wire_for
from test_anthropic_lifecycle import envelope

from judgmentllm import anthropic_assessment as a
from judgmentllm.evidence._base import EvidenceIdentity
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import ArtifactIdentity
from judgmentllm.providers import anthropic as p
from judgmentllm.providers.anthropic_response import _extract_assessment_component
from judgmentllm.providers.protocol import (
    ProviderError,
    ProviderIntegrityError,
    SubmissionUncertain,
)


def diagnostic() -> a.AnthropicAssessmentDiagnosticV2:
    component = _extract_assessment_component(
        envelope("messages", '{"winner_id":"001"}'), "messages"
    )
    assert component.output_text is not None
    digest = bytes_digest(component.output_text)
    return a.AnthropicAssessmentDiagnosticV2(
        identity=EvidenceIdentity(
            run_id="001",
            request_uid="request-001",
            attempt_id="attempt-001",
            submission_id="submission-001",
            observation_id="observation-001",
        ),
        endpoint="messages",
        requested_model="offline-model",
        remote_response_id="msg-offline",
        provider_outcome="completed",
        component=a.AnthropicAssessmentComponentMetadataV2.model_validate_json(
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
        {"component_version": 2.0},
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
        a.AnthropicAssessmentComponentMetadataV2.model_validate_json(
            canonical_bytes(component | changed)
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"diagnostic_version": 2.0},
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
        a.AnthropicAssessmentDiagnosticV2.model_validate_json(
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
        a.AnthropicAssessmentDiagnosticV2.model_validate_json(canonical_bytes(document))


def test_diagnostic_requires_a_real_observation_identity() -> None:
    document = diagnostic().model_dump(mode="json")
    document["identity"]["observation_id"] = None
    with pytest.raises(ValueError):
        a.AnthropicAssessmentDiagnosticV2.model_validate_json(canonical_bytes(document))


def test_missing_provider_extra_has_no_sensitive_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = p.importlib.import_module

    def missing(name: str) -> Any:
        if name == "anthropic":
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
        p.anthropic_sdk_version()
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("endpoint,concurrency", [("responses", 1), ("messages", 0)])
def test_invalid_endpoint_and_connection_bounds_fail_before_sdk(
    endpoint: str, concurrency: int
) -> None:
    with pytest.raises(ProviderError):
        p.AnthropicAdapter(endpoint, "offline-model", concurrency=concurrency)


@pytest.mark.asyncio
async def test_client_constructor_failure_is_sanitized_and_does_not_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_READINESS")
    wire, spec, _, context = wire_for(tmp_path)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("PRIVATE_CLIENT_CANARY")

    monkeypatch.setattr(p, "_protected_client", fail)
    adapter = p.AnthropicAdapter(
        "messages",
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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_ENVELOPE")
    wire, spec, _, context = wire_for(tmp_path)
    adapter = p.AnthropicAdapter(
        "messages",
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
        p.AnthropicAdapter("messages", "another-model", context=context)
    with pytest.raises(ProviderError):
        p.AnthropicAdapter.capabilities_for("openai", "messages", spec.model)
    adapter = p.AnthropicAdapter("messages", spec.model, context=context)
    for changed in (
        {"model": "another-model"},
        {"schema_version": 2},
        {"generation": GenerationSpec(temperature=0)},
        {"execution": spec.execution.model_copy(update={"cache_policy": "explicit"})},
        {"execution": spec.execution.model_copy(update={"tier": "priority"})},
    ):
        with pytest.raises(ProviderError):
            adapter.validate(spec.model_copy(update=changed))
    from judgmentllm.anthropic_prompts import render_anthropic_request

    response = dependency.request.response
    assert response is not None
    rendered = render_anthropic_request(
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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_READINESS")

    def missing() -> str:
        raise ProviderError("PRIVATE_EXTRA_CANARY")

    monkeypatch.setattr(p, "anthropic_sdk_version", missing)
    with pytest.raises(p.AnthropicReadinessError) as error:
        p.AnthropicAdapter("messages", "offline-model").validate_credentials()
    assert error.value.diagnostic_code == "missing_extra"
    assert "PRIVATE" not in str(error.value)


def test_sdk_alternate_authentication_is_rejected_at_construction() -> None:
    from types import SimpleNamespace

    class AmbientCredentialClient:
        def __init__(self, **kwargs: Any) -> None:
            self.auth_token = "PRIVATE_AMBIENT_CANARY"
            self.credentials = self._token_cache = None

    with pytest.raises(ProviderIntegrityError) as error:
        p._protected_client(
            SimpleNamespace(AsyncAnthropic=AmbientCredentialClient), api_key="SYNTHETIC"
        )
    assert "PRIVATE" not in str(error.value)


@pytest.mark.asyncio
async def test_response_handoff_capacity_identity_and_parse_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_HANDOFF")
    wire, spec, _, context = wire_for(tmp_path)
    identity = wire.transport_plan.identity
    adapter = p.AnthropicAdapter(
        "messages",
        spec.model,
        context=context,
        concurrency=1,
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json=envelope("messages", "{}"))
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


def test_closed_qualification_contexts_never_borrow_ordinary_authority(
    tmp_path: Path,
) -> None:
    from judgmentllm.anthropic_transport import _qualification_context

    wire, spec, _, _ = wire_for(tmp_path)
    candidate = p.AnthropicAdapter(
        "messages",
        spec.model,
        context=_qualification_context(spec, source_commit="a" * 40),
    )
    with pytest.raises(ProviderIntegrityError):
        candidate._check_authority()
    bound = p.AnthropicAdapter(
        "messages",
        spec.model,
        context=_qualification_context(
            spec, source_commit="a" * 40, plan_sha256="b" * 64
        ),
        submission_guard=lambda prepared: None,
    )
    with pytest.raises(ProviderIntegrityError):
        bound._check_authority()
    ordinary = p.AnthropicAdapter(
        "messages", spec.model, submission_guard=lambda prepared: None
    )
    with pytest.raises(ProviderIntegrityError):
        ordinary._check_authority()
    assert wire.output_commitment.scope.provider == "anthropic"


@pytest.mark.parametrize("support", ["all", "none", "partial"])
def test_reviewed_feature_facts_remain_exact_and_conservative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, support: str
) -> None:
    from test_anthropic_sdk_adapter import authorize_fixture

    from judgmentllm import anthropic_registry as registry

    _, spec, _, context = wire_for(tmp_path)
    features = (
        "rubric_explanations_together",
        "rubric_scores_first",
        "rubric_explanations_first",
        "pairwise_explanations",
        "rubric_grouped",
        "pairwise_ties",
    )
    authorize_fixture(
        monkeypatch, context, features if support != "partial" else features[:1]
    )
    if support == "none":
        profile = registry._QUALIFIED_PROFILES[0].model_copy(
            update={
                "supported_features": (),
                "unsupported_features": features,
                "support_status": "unsupported",
                "request_acceptance": "unknown",
                "assessment_behavior": "unknown",
                "native_schema_behavior": "unsupported",
            }
        )
        monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", (profile,))
    capabilities = p.AnthropicAdapter.capabilities_for(
        "anthropic", "messages", spec.model
    )
    expected = True if support == "all" else False if support == "none" else None
    assert capabilities.explanations is expected
    assert capabilities.grouped is expected
    assert capabilities.ties is expected


@pytest.mark.asyncio
async def test_client_connection_bounds_cannot_change_after_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_BOUNDS")
    _, spec, _, context = wire_for(tmp_path)
    adapter = p.AnthropicAdapter(
        "messages",
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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_RESPONSE_BOUND")
    wire, spec, _, context = wire_for(tmp_path)

    class LargeResponse(httpx2.AsyncByteStream):
        async def __aiter__(self):
            chunk = b" " * 1_048_576
            for _ in range(9):
                yield chunk

    adapter = p.AnthropicAdapter(
        "messages",
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
