"""Adversarial diagnostic identities, readiness and authority fail closed."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from gemini_fixture import wire_for
from test_gemini_lifecycle import envelope

from judgmentllm import gemini_assessment as a
from judgmentllm.evidence._base import EvidenceIdentity
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import ArtifactIdentity
from judgmentllm.providers import gemini as p
from judgmentllm.providers.gemini_response import _extract_assessment_component
from judgmentllm.providers.protocol import (
    ProviderError,
    ProviderIntegrityError,
    SubmissionUncertain,
)


def diagnostic() -> a.GeminiAssessmentDiagnosticV3:
    component = _extract_assessment_component(
        envelope("models.generate_content", '{"winner_id":"001"}'),
        "models.generate_content",
    )
    assert component.output_text is not None
    digest = bytes_digest(component.output_text)
    return a.GeminiAssessmentDiagnosticV3(
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
        component=a.GeminiAssessmentComponentMetadataV3.model_validate_json(
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
        {"component_version": 3.0},
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
        a.GeminiAssessmentComponentMetadataV3.model_validate_json(
            canonical_bytes(component | changed)
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"diagnostic_version": 3.0},
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
        a.GeminiAssessmentDiagnosticV3.model_validate_json(
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
        a.GeminiAssessmentDiagnosticV3.model_validate_json(canonical_bytes(document))


def test_diagnostic_requires_a_real_observation_identity() -> None:
    document = diagnostic().model_dump(mode="json")
    document["identity"]["observation_id"] = None
    with pytest.raises(ValueError):
        a.GeminiAssessmentDiagnosticV3.model_validate_json(canonical_bytes(document))


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
        p.gemini_sdk_version()
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "endpoint,concurrency", [("responses", 1), ("models.generate_content", 0)]
)
def test_invalid_endpoint_and_connection_bounds_fail_before_sdk(
    endpoint: str, concurrency: int
) -> None:
    with pytest.raises(ProviderError):
        p.GeminiAdapter(endpoint, "offline-model", concurrency=concurrency)


@pytest.mark.parametrize("entry", ["direct", "adapter"])
@pytest.mark.parametrize("failure", ["roots", "tls", "sdk", "success"])
@pytest.mark.asyncio
async def test_tls_ordering_and_real_sdk_http_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, failure: str
) -> None:
    import certifi

    _, spec, _, context = wire_for(tmp_path)
    sdk, http = p._sdk()
    sync_clients: list[Any] = []
    async_clients: list[Any] = []
    forwarded: list[Any] = []

    class TrackedSync(httpx2.Client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            sync_clients.append(self)

        def close(self) -> None:
            self.close_calls += 1
            super().close()

    class TrackedAsync(httpx2.AsyncClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            async_clients.append(self)

        async def aclose(self) -> None:
            self.close_calls += 1
            await super().aclose()

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("PRIVATE_CONSTRUCTION")

    def forbidden(request: Any) -> Any:
        forwarded.append(request)
        raise AssertionError("Client setup/cleanup must never forward")

    http = SimpleNamespace(
        **(vars(http) | {"Client": TrackedSync, "AsyncClient": TrackedAsync})
    )
    if failure == "roots":
        monkeypatch.setattr(certifi, "where", fail)
    elif failure == "tls":
        monkeypatch.setattr(
            p,
            "ssl",
            SimpleNamespace(**(vars(p.ssl) | {"create_default_context": fail})),
        )
    elif failure == "sdk":
        sdk = SimpleNamespace(**(vars(sdk) | {"Client": fail}))
    monkeypatch.setattr(p, "_sdk", lambda: (sdk, http))
    adapter = p.GeminiAdapter(
        "models.generate_content",
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(forbidden),
    )
    outer = (
        http.AsyncClient(transport=http.MockTransport(forbidden), trust_env=False)
        if entry == "direct"
        else None
    )
    try:
        if failure == "success":
            client = (
                p._protected_client(sdk, http, key="SYNTHETIC_KEY", http_client=outer)
                if entry == "direct"
                else await adapter._execution_client()
            )
            assert len(sync_clients) == len(async_clients) == 1
            assert client._api_client._httpx_client is sync_clients[0]
            assert client._api_client._async_httpx_client is async_clients[0]
            assert sync_clients[0].close_calls == async_clients[0].close_calls == 0
            if entry == "direct":
                await p._close_client(client)
            else:
                await adapter.aclose()
                assert adapter._client is None
                await adapter.aclose()
            assert all(
                owned.close_calls == 1 for owned in [*sync_clients, *async_clients]
            )
        else:
            expected = (
                RuntimeError
                if entry == "direct" and failure != "sdk"
                else ProviderIntegrityError
            )
            with pytest.raises(expected) as caught:
                if entry == "direct":
                    p._protected_client(
                        sdk, http, key="SYNTHETIC_KEY", http_client=outer
                    )
                else:
                    await adapter._execution_client()
            if expected is ProviderIntegrityError:
                assert "PRIVATE" not in str(caught.value)
            assert len(sync_clients) == int(failure == "sdk")
            assert all(client.close_calls == 1 for client in sync_clients)
            if entry == "direct":
                assert async_clients[0].close_calls == 0
            else:
                assert async_clients[0].close_calls == 1
                assert adapter._client is None
    finally:
        if outer is not None and not outer.is_closed:
            await outer.aclose()
        if adapter._client is not None:
            await adapter.aclose()
        for owned in sync_clients:
            if not owned.is_closed:
                owned.close()
    assert len(async_clients) == 1
    assert all(client.close_calls == 1 for client in [*sync_clients, *async_clients])
    assert not forwarded and adapter.actual_call_count == 0


@pytest.mark.asyncio
async def test_client_constructor_failure_is_sanitized_and_does_not_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "SYNTHETIC_READINESS")
    wire, spec, _, context = wire_for(tmp_path)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("PRIVATE_CLIENT_CANARY")

    monkeypatch.setattr(p, "_protected_client", fail)
    adapter = p.GeminiAdapter(
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
    adapter = p.GeminiAdapter(
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
        p.GeminiAdapter("models.generate_content", "another-model", context=context)
    with pytest.raises(ProviderError):
        p.GeminiAdapter.capabilities_for(
            "openai", "models.generate_content", spec.model
        )
    adapter = p.GeminiAdapter("models.generate_content", spec.model, context=context)
    for changed in (
        {"model": "another-model"},
        {"schema_version": 2},
        {"generation": GenerationSpec(temperature=3)},
        {"execution": spec.execution.model_copy(update={"cache_policy": "explicit"})},
        {"execution": spec.execution.model_copy(update={"tier": "ultrafast"})},
    ):
        with pytest.raises(ProviderError):
            adapter.validate(spec.model_copy(update=changed))
    from judgmentllm.gemini_prompts import render_gemini_request

    response = dependency.request.response
    assert response is not None
    rendered = render_gemini_request(
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

    monkeypatch.setattr(p, "gemini_sdk_version", missing)
    with pytest.raises(p.GeminiReadinessError) as error:
        p.GeminiAdapter(
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
    adapter = p.GeminiAdapter(
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
    adapter = p.GeminiAdapter(
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

    adapter = p.GeminiAdapter(
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
    from gemini_offline import isolate

    isolate(monkeypatch)
