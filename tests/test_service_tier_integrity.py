"""Offline attacks on service-policy bindings and historical execution."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import importlib
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from test_service_tiers import ROUTES, request
from test_service_tiers import offline as offline

from judgmentllm.evidence import EvidenceIdentity, UsageContext
from judgmentllm.execution import execute_run
from judgmentllm.execution.session import ExecutionSession, ExecutionWork
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.planning import stable_id
from judgmentllm.preparation import PreparationError, prepare_run
from judgmentllm.providers.evidence import ProviderEvidence, prepare_evidence
from judgmentllm.providers.protocol import ProviderError, ProviderResponse
from judgmentllm.service_tiers import observe_service_tier, service_policy
from judgmentllm.transport import physical_identity


@pytest.mark.parametrize("provider,endpoint", ROUTES)
def test_duplicate_qualification_scopes_and_foreign_output_policy_fail(
    tmp_path: Path, provider: str, endpoint: str
) -> None:
    wire, spec, _, module = request(tmp_path, provider, endpoint, None)
    fixture = importlib.import_module(
        {
            "openai": "test_production_transport",
            "anthropic": "test_anthropic_transport",
            "gemini": "test_gemini_transport",
        }[provider]
    )
    profile = fixture._reviewed_profile(module.production_context(spec))
    with pytest.raises(ValueError):
        cast(Any, type(profile)).model_validate(
            dict(profile) | {"service_scopes": profile.service_scopes * 2}
        )
    foreign = service_policy(
        spec.model_copy(
            update={"provider": "gemini" if provider != "gemini" else "openai"}
        )
    )
    with pytest.raises(ValueError):
        cast(Any, type(wire.output_commitment)).model_validate(
            dict(wire.output_commitment) | {"service_policy": foreign}
        )


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_request_policy_cannot_disagree_with_service_policy(
    tmp_path: Path, provider: str
) -> None:
    endpoint = "messages" if provider == "anthropic" else "models.generate_content"
    wire, spec, _, module = request(tmp_path, provider, endpoint, None)
    output = wire.output_commitment
    changes: list[dict[str, Any]]
    if provider == "anthropic":
        alternate = service_policy(
            spec.model_copy(
                update={"execution": spec.execution.model_copy(update={"tier": "auto"})}
            )
        )
        changes = [{"service_policy": alternate}]
    else:
        changes = [
            {"service_policy": None},
            {
                "request_policy": module.GeminiRequestPolicyV1(
                    cache=output.request_policy.cache
                )
            },
            {
                "request_policy": module.GeminiRequestPolicyV2(
                    cache=output.request_policy.cache, read_timeout_seconds=930
                )
            },
            {
                "request_policy": module.GeminiRequestPolicyV2(
                    cache=output.request_policy.cache, operation_deadline_seconds=960
                )
            },
        ]
    for changed in changes:
        with pytest.raises(ValueError):
            cast(Any, type(output)).model_validate(dict(output) | changed)


@pytest.mark.parametrize("conflict", ["missing_commitment", "policy", "provider"])
def test_provider_evidence_requires_exact_service_commitment(
    tmp_path: Path, conflict: str
) -> None:
    wire, spec, _, _ = request(tmp_path, "openai", "responses", None)
    policy = (
        service_policy(spec.model_copy(update={"account_scope": "another-account"}))
        if conflict == "policy"
        else wire.output_commitment.service_policy
    )
    assert policy is not None
    response = ProviderResponse(
        observation_key="observation-001",
        kind="response",
        raw=b"{}",
        evidence=ProviderEvidence(
            provenance="provider_documented",
            service_tier=observe_service_tier(policy, "default"),
        ),
    )
    with pytest.raises(ValueError, match="service evidence conflicts"):
        prepare_evidence(
            identity=EvidenceIdentity(
                run_id="001",
                request_uid=wire.request_uid,
                attempt_id="01",
                submission_id="submission-001",
            ),
            context=UsageContext(
                provider="gemini" if conflict == "provider" else "openai",
                endpoint=spec.endpoint,
                requested_model=spec.model,
                observation_kind="response",
            ),
            commitment=None
            if conflict == "missing_commitment"
            else wire.output_commitment,
            response=response,
            capture="full",
        )


def test_openai_validation_rejects_unknown_tiers_and_explicit_cache(
    tmp_path: Path,
) -> None:
    from judgmentllm.providers.openai import OpenAIAdapter

    _, spec, _, _ = request(tmp_path, "openai", "responses", None)
    adapter = OpenAIAdapter(spec.endpoint, spec.model)
    for changed in ({"tier": "unsupported"}, {"cache_policy": "explicit"}):
        with pytest.raises(ProviderError):
            adapter.validate(
                spec.model_copy(
                    update={"execution": spec.execution.model_copy(update=changed)}
                )
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,endpoint", ROUTES)
async def test_legacy_sdk_execution_keeps_original_body_and_no_tier_observation(
    tmp_path: Path, provider: str, endpoint: str
) -> None:
    wire, spec, _, _ = request(tmp_path, provider, endpoint, None, revision=1)
    module = importlib.import_module(f"judgmentllm.providers.{provider}")
    adapter_type = getattr(
        module,
        {
            "openai": "OpenAIAdapter",
            "anthropic": "AnthropicAdapter",
            "gemini": "GeminiAdapter",
        }[provider],
    )
    calls: list[bytes] = []

    def respond(http_request: httpx2.Request) -> httpx2.Response:
        assert http_request.extensions["timeout"]["read"] == 60
        calls.append(http_request.content)
        fixture = importlib.import_module(
            {
                "openai": "test_openai_lifecycle",
                "anthropic": "test_anthropic_lifecycle",
                "gemini": "gemini_offline",
            }[provider]
        )
        body = (
            fixture.envelope('{"scores":[]}', modelVersion=spec.model)
            if provider == "gemini"
            else fixture.envelope(endpoint, '{"scores":[]}')
        )
        return httpx2.Response(200, json=body)

    adapter = adapter_type(
        endpoint, spec.model, http_transport=httpx2.MockTransport(respond)
    )
    invocation = {
        "submission_id": wire.transport_plan.identity.submission_id,
        "correlation_id": wire.transport_plan.identity.correlation_id,
    }
    try:
        await adapter.submit(wire, **invocation)
        response = await adapter.retrieve(wire, **invocation)
        assert response.evidence.service_tier is None
        assert "service_tier" not in response.evidence.model_dump()
        assert calls == [wire.payload] and adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_preparation_and_durable_evidence_reject_rebound_service_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import transport_dispatch
    from judgmentllm.production_transport import production_context
    from judgmentllm.providers.openai import OpenAIAdapter
    from scripts import openai_offline_fixture as fixture

    spec = fixture.specification(
        tmp_path / "inputs", "responses", "rubric", capture="full"
    )
    store = tmp_path / "runtime"
    source = fixture.FixtureTransport("responses")
    adapter = OpenAIAdapter(
        spec.endpoint,
        spec.model,
        concurrency=spec.execution.concurrency,
        http_transport=httpx2.MockTransport(source.respond),
    )
    alternate = service_policy(
        spec.model_copy(update={"account_scope": "another-account"})
    )
    assert alternate is not None
    try:
        with fixture.offline():
            with prepare_run(
                spec,
                store=store,
                run_id="001",
                capabilities=adapter.capabilities(spec),
                transport_context=production_context(spec),
            ) as run:
                dep = next(run.iter_request_dependencies())
                ident = physical_identity(
                    "001",
                    dep.request.request_uid,
                    stable_id("attempt", "001", dep.request.request_uid, 1),
                )
                original = transport_dispatch.prepare_request

                def changed(*args: Any, **kwargs: Any) -> Any:
                    wire = original(*args, **kwargs)
                    # Simulate corruption after the prepared object's validation.
                    object.__setattr__(
                        wire.output_commitment, "service_policy", alternate
                    )
                    return wire

                with monkeypatch.context() as patch:
                    patch.setattr(transport_dispatch, "prepare_request", changed)
                    with pytest.raises(
                        PreparationError, match="service policy differs"
                    ):
                        run.prepare_transport_attempt(dep, ident)
            report = await execute_run("001", store=store, adapter=adapter, spec=spec)
            assert report.summary.successful_results == 2
            with ReadOnlyRepository(store) as repository:
                receipt = next(repository.iter_evidence("001"))
                assert receipt.identity.observation_id is not None
            session = ExecutionSession("001", store, adapter, spec, False, None)
            try:
                assert session.prepared is not None
                wire = session.prepared.prepare_transport_attempt(dep, ident)
                work = ExecutionWork(
                    dep,
                    wire,
                    ident.attempt_id,
                    ident.submission_id,
                    ident.correlation_id,
                )
                session.evidence.verify(receipt, work)
                forged = receipt.model_copy(
                    update={"service_tier": observe_service_tier(alternate, "default")}
                )
                with pytest.raises(IntegrityError, match="service evidence differs"):
                    session.evidence.verify(forged, work)
            finally:
                session.close()
            assert source.calls == adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.asyncio
async def test_unaccepted_invocation_does_not_invent_served_tier_review(
    tmp_path: Path, status: int
) -> None:
    from judgmentllm.execution import recover_run
    from judgmentllm.execution.retry import RetryPolicy
    from judgmentllm.production_transport import production_context
    from judgmentllm.providers.openai import OpenAIAdapter
    from scripts import openai_offline_fixture as fixture

    spec = fixture.specification(tmp_path / "inputs", "responses", "rubric")
    spec.execution.tier, spec.execution.concurrency = "flex", 1
    calls: list[bytes] = []

    def respond(http_request: httpx2.Request) -> httpx2.Response:
        calls.append(http_request.content)
        return httpx2.Response(status, json={"error": {"type": "rate_limit_error"}})

    adapter = OpenAIAdapter(
        spec.endpoint,
        spec.model,
        concurrency=1,
        http_transport=httpx2.MockTransport(respond),
    )
    store = tmp_path / "runtime"
    retry = RetryPolicy(max_attempts=1)
    try:
        with fixture.offline():
            with prepare_run(
                spec,
                store=store,
                run_id="001",
                capabilities=adapter.capabilities(spec),
                transport_context=production_context(spec),
            ):
                pass
            report = await execute_run(
                "001", store=store, adapter=adapter, spec=spec, retry_policy=retry
            )
            assert report.recovery_required == (status == 503)
            session = ExecutionSession("001", store, adapter, spec, False, None)
            try:
                assert session.tier_review_required is False
                receipt = next(session.repository.iter_evidence("001"))
                assert (
                    receipt.service_tier is None
                    and receipt.identity.observation_id is None
                )
            finally:
                session.close()
            await adapter.aclose()
            adapter = OpenAIAdapter(
                spec.endpoint,
                spec.model,
                concurrency=1,
                http_transport=httpx2.MockTransport(respond),
            )
            await recover_run(
                "001", store=store, adapter=adapter, spec=spec, retry_policy=retry
            )
            assert len(calls) == 1 and adapter.actual_call_count == 0
    finally:
        await adapter.aclose()
