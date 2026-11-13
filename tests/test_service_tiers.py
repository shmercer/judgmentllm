"""Offline service policies, exact pinned SDK bytes, and immutable recovery."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import importlib
import json
import socket
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest

from judgmentllm.config import RunSpecV3
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.planning import plan_record
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.providers.protocol import (
    ProviderIntegrityError,
    ProviderInvocationCancelled,
    SubmissionUncertain,
)
from judgmentllm.service_tiers import (
    ServicePolicyV2,
    ServiceQualificationScope,
    ServiceTierObservation,
    observe_service_tier,
    qualified_service,
    service_policy,
    terminal_rate_rejection,
)
from judgmentllm.transport import physical_identity

ROUTES = [
    ("openai", "responses"),
    ("openai", "chat.completions"),
    ("anthropic", "messages"),
    ("gemini", "models.generate_content"),
]


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.setenv(name, "SYNTHETIC_SERVICE_TIER_KEY")

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("external network forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def request(
    tmp_path: Path,
    provider: str,
    endpoint: str,
    tier: str | None,
    *,
    revision: int = 2,
    native: bool = False,
    strict: bool = False,
) -> tuple[Any, RunSpecV3, Any, Any]:
    fixture = importlib.import_module(
        {
            "openai": "test_production_transport",
            "anthropic": "anthropic_fixture",
            "gemini": "gemini_fixture",
        }[provider]
    )
    spec, dependency, _ = fixture.case(
        tmp_path,
        endpoint=endpoint,
        policy="native" if native else "text",
        strict=False,
    )
    spec.execution.tier = tier
    if revision == 1:
        spec.account_scope = None
    record = dependency.request.response
    assert record is not None
    planned = next(plan_record(spec, record))
    spec.strict_capabilities = strict
    dependency = RequestDependency(
        request=planned, dependency_sha256=dependency.dependency_sha256
    )
    transport = importlib.import_module(
        "judgmentllm."
        + {
            "openai": "production_transport",
            "anthropic": "anthropic_transport",
            "gemini": "gemini_transport",
        }[provider]
    )
    context = (
        transport._qualification_context(spec, source_commit="a" * 40)
        if native
        else transport.production_context(spec)
    )
    wire = transport.prepare_production_request(
        spec,
        dependency,
        physical_identity("001", planned.request_uid, "01"),
        context,
        service_policy_version=revision,
    )
    return wire, spec, dependency, transport


@pytest.mark.parametrize("provider,endpoint", ROUTES)
def test_default_and_legacy_frozen_reconstruction(
    tmp_path: Path, provider: str, endpoint: str
) -> None:
    old, spec, dependency, module = request(
        tmp_path, provider, endpoint, None, revision=1
    )
    context = module.production_context(spec)
    before = canonical_bytes(old)
    assert old.output_commitment.service_policy is None
    assert b'"service_policy"' not in canonical_bytes(old.transport_commitment)
    rebuilt = module.prepare_production_request(
        spec,
        dependency,
        old.transport_plan.identity,
        context,
        frozen=old.transport_commitment,
    )
    assert canonical_bytes(rebuilt) == before
    retry = module.prepare_production_request(
        spec,
        dependency,
        physical_identity("001", old.request_uid, "02"),
        context,
        parent=old.transport_commitment,
    )
    assert (
        retry.output_commitment.service_policy is None and retry.payload == old.payload
    )
    new = module.prepare_production_request(
        spec, dependency, old.transport_plan.identity, context
    )
    policy = new.output_commitment.service_policy
    assert policy is not None and policy.service_policy_version == 2
    assert policy.requested_tier is None and policy.tier == "standard"
    assert new.payload != old.payload
    assert (
        new.transport_commitment.commitment_sha256
        != old.transport_commitment.commitment_sha256
    )
    model_type = cast(Any, type(new))
    assert model_type.model_validate_json(new.model_dump_json()) == new


@pytest.mark.parametrize(
    "provider,endpoint,tier",
    [
        (provider, endpoint, tier)
        for provider, endpoint in ROUTES
        if provider != "anthropic"
        for tier in (
            ("default", "auto", "flex", "fast", "priority")
            if provider == "openai"
            else ("standard", "flex", "priority")
        )
    ],
)
def test_legacy_policy_rejects_previously_unsupported_explicit_tiers(
    tmp_path: Path, provider: str, endpoint: str, tier: str
) -> None:
    _, spec, _, _ = request(tmp_path, provider, endpoint, tier)
    spec.account_scope = None
    with pytest.raises(ValueError, match="legacy"):
        service_policy(spec, version=1)
    with pytest.raises(ValueError):
        request(tmp_path, provider, endpoint, tier, revision=1)


@pytest.mark.parametrize("tier", ["auto", "standard_only"])
def test_legacy_anthropic_explicit_tiers_retain_exact_frozen_bytes(
    tmp_path: Path, tier: str
) -> None:
    old, spec, dependency, module = request(
        tmp_path, "anthropic", "messages", tier, revision=1
    )
    assert service_policy(spec, version=1) is None
    assert json.loads(old.payload)["service_tier"] == tier
    rebuilt = module.prepare_production_request(
        spec,
        dependency,
        old.transport_plan.identity,
        module.production_context(spec),
        frozen=old.transport_commitment,
    )
    assert canonical_bytes(rebuilt) == canonical_bytes(old)


@pytest.mark.parametrize("provider,endpoint", ROUTES)
def test_changed_tier_or_account_cannot_resume(
    tmp_path: Path, provider: str, endpoint: str
) -> None:
    wire, spec, dependency, module = request(tmp_path, provider, endpoint, None)
    for changed in (
        spec.model_copy(update={"account_scope": "another-account"}),
        spec.model_copy(
            update={
                "execution": spec.execution.model_copy(
                    update={"tier": "auto" if provider == "anthropic" else "flex"}
                )
            }
        ),
    ):
        with pytest.raises(ValueError):
            module.prepare_production_request(
                changed,
                dependency,
                wire.transport_plan.identity,
                module.production_context(changed),
                frozen=wire.transport_commitment,
            )


@pytest.mark.parametrize("provider,endpoint", ROUTES)
@pytest.mark.parametrize("tier", ["", " Flex", "unsupported", "batch", "ultrafast"])
def test_unknown_or_unimplemented_tier_fails_offline(
    tmp_path: Path, provider: str, endpoint: str, tier: str
) -> None:
    with pytest.raises(ValueError):
        request(tmp_path, provider, endpoint, tier)


@pytest.mark.parametrize(
    "provider,endpoint", [route for route in ROUTES if route[0] != "anthropic"]
)
def test_strict_unknown_flex_requires_evidence(
    tmp_path: Path, provider: str, endpoint: str
) -> None:
    with pytest.raises(ValueError):
        request(tmp_path, provider, endpoint, "flex", strict=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,endpoint", ROUTES)
async def test_new_strict_tier_fails_before_credential_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, endpoint: str
) -> None:
    from judgmentllm import lifecycle

    tier = "auto" if provider == "anthropic" else "flex"
    _, spec, _, _ = request(tmp_path, provider, endpoint, tier)
    spec.execution.grouping = "independent"
    spec.strict_capabilities = True
    module = importlib.import_module(f"judgmentllm.providers.{provider}")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("credential readiness must follow tier validation")

    monkeypatch.setattr(module, "_resolve_credentials", forbidden)
    with pytest.raises(lifecycle.LifecycleError, match="execution.tier"):
        await lifecycle.arun_live(spec, store=tmp_path / "no-store")
    assert not (tmp_path / "no-store").exists()


def test_historical_strict_anthropic_auto_remains_reconstructable(
    tmp_path: Path,
) -> None:
    from judgmentllm.providers.anthropic import AnthropicAdapter

    old, spec, dependency, module = request(
        tmp_path, "anthropic", "messages", "auto", revision=1
    )
    spec.execution.grouping = "independent"
    spec.strict_capabilities = True
    adapter = AnthropicAdapter(spec.endpoint, spec.model)
    adapter.validate(spec)
    old = module.prepare_production_request(
        spec,
        dependency,
        old.transport_plan.identity,
        module.production_context(spec),
        service_policy_version=1,
    )
    rebuilt = module.prepare_production_request(
        spec,
        dependency,
        old.transport_plan.identity,
        module.production_context(spec),
        frozen=old.transport_commitment,
    )
    assert canonical_bytes(rebuilt) == canonical_bytes(old)


@pytest.mark.parametrize("provider,endpoint", ROUTES)
def test_native_authority_is_exact_account_and_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, endpoint: str
) -> None:
    _, spec, dependency, module = request(
        tmp_path, provider, endpoint, None, native=True
    )
    profile_module = importlib.import_module(
        {
            "openai": "test_production_transport",
            "anthropic": "test_anthropic_transport",
            "gemini": "test_gemini_transport",
        }[provider]
    )
    registry = importlib.import_module(
        "judgmentllm."
        + {
            "openai": "production_registry",
            "anthropic": "anthropic_registry",
            "gemini": "gemini_registry",
        }[provider]
    )
    profile = profile_module._reviewed_profile(module.production_context(spec))
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", (profile,))
    build = getattr(
        module,
        "build_output_commitment_v"
        + {"openai": "4", "anthropic": "5", "gemini": "6"}[provider],
    )
    assert (
        build(
            spec, dependency.request, module.production_context(spec)
        ).selection.native_support
        == "supported"
    )
    for account, tier in (
        ("other-account", None),
        (None, None),
        (spec.account_scope, "auto" if provider == "anthropic" else "flex"),
    ):
        changed = spec.model_copy(
            update={
                "account_scope": account,
                "execution": spec.execution.model_copy(update={"tier": tier}),
            }
        )
        with pytest.raises(ValueError):
            build(changed, dependency.request, module.production_context(changed))
    scoped_tier = "auto" if provider == "anthropic" else "flex"
    limited = profile.model_copy(
        update={
            "service_scopes": (
                *profile.service_scopes,
                ServiceQualificationScope(
                    account_scope="synthetic-account",
                    tier=scoped_tier,
                    features=("rubric_scores", "assessment_presentation_v2"),
                ),
            )
        }
    )
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", (limited,))
    changed = spec.model_copy(
        update={"execution": spec.execution.model_copy(update={"tier": scoped_tier})}
    )
    # Grouped Standard evidence plus single-trait Flex evidence cannot authorize grouped Flex.
    with pytest.raises(ValueError):
        build(changed, dependency.request, module.production_context(changed))
    legacy = profile.model_copy(update={"service_scopes": ()})
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", (legacy,))
    with pytest.raises(ValueError):
        build(spec, dependency.request, module.production_context(spec))


@pytest.mark.parametrize(
    "provider,requested,reported,outcome",
    [
        ("openai", None, "default", "matched"),
        ("openai", "flex", "flex", "matched"),
        ("openai", "fast", "priority", "matched"),
        ("openai", "priority", "fast", "matched"),
        ("openai", "fast", "default", "downgraded"),
        ("openai", "auto", "priority", "allocated"),
        ("openai", "auto", "ultrafast", "allocated"),
        ("openai", "flex", "default", "unexpected"),
        ("openai", None, "standard", "unexpected"),
        ("gemini", None, "standard", "matched"),
        ("gemini", "priority", "standard", "downgraded"),
        ("gemini", "flex", "standard", "unexpected"),
        ("gemini", "priority", "priority", "matched"),
        ("anthropic", None, "standard", "matched"),
        ("anthropic", "auto", "priority", "allocated"),
        ("anthropic", "auto", "standard", "allocated"),
        ("anthropic", None, "priority", "unexpected"),
        ("gemini", "flex", None, "unknown"),
        ("gemini", "flex", "PRIVATE_UNKNOWN_CANARY", "unknown"),
    ],
)
def test_provider_specific_equivalence(
    tmp_path: Path,
    provider: str,
    requested: str | None,
    reported: str | None,
    outcome: str,
) -> None:
    endpoint = next(endpoint for item, endpoint in ROUTES if item == provider)
    wire, _, _, _ = request(tmp_path, provider, endpoint, requested)
    observation = observe_service_tier(
        wire.output_commitment.service_policy, reported, latency_seconds=0.25
    )
    assert observation.outcome == outcome
    assert "PRIVATE_UNKNOWN_CANARY" not in observation.model_dump_json()
    assert (
        ServiceTierObservation.model_validate_json(observation.model_dump_json())
        == observation
    )
    with pytest.raises(ValueError):
        ServiceTierObservation.model_validate(dict(observation) | {"outcome": "wrong"})


@pytest.mark.parametrize(
    "provider,requested,outcomes",
    [
        ("openai", None, {"default": "matched"}),
        ("openai", "default", {"default": "matched"}),
        ("openai", "flex", {"flex": "matched"}),
        (
            "openai",
            "fast",
            {"default": "downgraded", "priority": "matched", "fast": "matched"},
        ),
        (
            "openai",
            "priority",
            {"default": "downgraded", "priority": "matched", "fast": "matched"},
        ),
        (
            "openai",
            "auto",
            dict.fromkeys(
                ("default", "flex", "priority", "fast", "scale", "ultrafast"),
                "allocated",
            ),
        ),
        ("gemini", None, {"standard": "matched"}),
        ("gemini", "standard", {"standard": "matched"}),
        ("gemini", "flex", {"flex": "matched"}),
        ("gemini", "priority", {"standard": "downgraded", "priority": "matched"}),
        ("anthropic", None, {"standard": "matched"}),
        ("anthropic", "standard_only", {"standard": "matched"}),
        ("anthropic", "auto", {"standard": "allocated", "priority": "allocated"}),
    ],
)
def test_v2_outcome_contract_is_frozen_for_all_retained_tiers(
    tmp_path: Path, provider: str, requested: str | None, outcomes: dict[str, str]
) -> None:
    endpoint = next(route for name, route in ROUTES if name == provider)
    _, spec, _, _ = request(tmp_path, provider, endpoint, requested)
    policy = service_policy(spec)
    assert policy is not None and policy.service_policy_version == 2
    for reported in (
        "default",
        "standard",
        "flex",
        "priority",
        "fast",
        "scale",
        "ultrafast",
    ):
        original = observe_service_tier(policy, reported)
        assert original.outcome == outcomes.get(reported, "unexpected")
        assert (
            ServiceTierObservation.model_validate_json(original.model_dump_json())
            == original
        )
    assert observe_service_tier(policy, None).outcome == "unknown"


@pytest.mark.parametrize(
    "provider,endpoint,tier",
    [
        ("openai", "responses", "flex"),
        ("openai", "chat.completions", "flex"),
        ("openai", "responses", "fast"),
        ("anthropic", "messages", "auto"),
        ("gemini", "models.generate_content", "flex"),
        ("gemini", "models.generate_content", "priority"),
    ],
)
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.asyncio
async def test_pinned_sdk_exact_tier_and_timeout_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    endpoint: str,
    tier: str,
    native: bool,
) -> None:
    wire, spec, dependency, transport_module = request(
        tmp_path, provider, endpoint, tier, native=native
    )
    if native:
        profile_module = importlib.import_module(
            {
                "openai": "test_production_transport",
                "anthropic": "test_anthropic_transport",
                "gemini": "test_gemini_transport",
            }[provider]
        )
        registry = importlib.import_module(
            "judgmentllm."
            + {
                "openai": "production_registry",
                "anthropic": "anthropic_registry",
                "gemini": "gemini_registry",
            }[provider]
        )
        profile = profile_module._reviewed_profile(
            transport_module.production_context(spec),
            service_scopes=(
                wire.output_commitment.service_policy.qualification_scope.model_copy(
                    update={"features": wire.output_commitment.selection.features}
                ),
            ),
        )
        monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", (profile,))
        wire = transport_module.prepare_production_request(
            spec,
            dependency,
            wire.transport_plan.identity,
            transport_module.production_context(spec),
        )
    module = importlib.import_module("judgmentllm.providers." + provider)
    adapter_type = getattr(
        module,
        {
            "openai": "OpenAIAdapter",
            "anthropic": "AnthropicAdapter",
            "gemini": "GeminiAdapter",
        }[provider],
    )
    policy = wire.output_commitment.service_policy
    assert policy is not None
    timeouts: list[float] = []
    original_wait = asyncio.wait_for

    async def wait(awaitable: Any, timeout: float | None) -> Any:
        assert timeout is not None
        timeouts.append(timeout)
        return await original_wait(awaitable, timeout)

    monkeypatch.setattr(module.asyncio, "wait_for", wait)
    calls: list[bytes] = []

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        calls.append(http_request.content)
        assert http_request.content == wire.payload
        assert b"synthetic-account" not in http_request.content
        assert http_request.extensions["timeout"] == {
            "connect": 10.0,
            "write": 10.0,
            "pool": 10.0,
            "read": float(policy.read_timeout_seconds),
        }
        if provider == "gemini":
            assert json.loads(http_request.content)["serviceTier"] == tier
            assert (
                "serviceTier"
                not in json.loads(http_request.content)["generationConfig"]
            )
            assert http_request.headers["X-Server-Timeout"] == (
                "900" if tier == "flex" else "60"
            )
            from gemini_offline import envelope

            body = envelope('{"scores":[]}', modelVersion=spec.model)
            return httpx2.Response(
                200, json=body, headers={"x-gemini-service-tier": tier}
            )
        if provider == "openai":
            from test_openai_lifecycle import envelope

            body = envelope(endpoint, '{"scores":[]}')
            body.update(
                model=spec.model, service_tier="priority" if tier == "fast" else tier
            )
        else:
            from test_anthropic_lifecycle import envelope

            body = envelope(endpoint, '{"scores":[]}')
            body["usage"]["service_tier"] = "standard"
        return httpx2.Response(200, json=body)

    adapter = adapter_type(
        endpoint, spec.model, http_transport=httpx2.MockTransport(handler)
    )
    invocation = {
        "submission_id": wire.transport_plan.identity.submission_id,
        "correlation_id": wire.transport_plan.identity.correlation_id,
    }
    try:
        if provider == "gemini":
            sdk, _ = module._sdk()
            config = module._sdk_arguments(sdk, wire)["config"]
            assert config.service_tier.value == tier
            assert config.http_options.timeout == policy.read_timeout_seconds * 1000
        await adapter.submit(wire, **invocation)
        response = await adapter.retrieve(wire, **invocation)
        assert response.evidence.service_tier.policy == policy
        assert response.evidence.service_tier.latency_seconds >= 0
        assert response.evidence.service_tier.outcome in {"matched", "allocated"}
        assert timeouts == [policy.operation_deadline_seconds]
        assert calls == [wire.payload] and adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revision,tier", [(1, None), (2, None), (2, "flex"), (2, "priority")]
)
@pytest.mark.parametrize("mutation", ["missing", "changed", "duplicate"])
async def test_gemini_http_guard_verifies_v2_server_hint_before_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revision: int,
    tier: str | None,
    mutation: str,
) -> None:
    from gemini_offline import envelope

    from judgmentllm.providers.gemini import GeminiAdapter

    wire, spec, _, _ = request(
        tmp_path, "gemini", "models.generate_content", tier, revision=revision
    )
    original_send = httpx2.AsyncClient.send

    async def send(
        client: httpx2.AsyncClient, http_request: httpx2.Request, **kwargs: Any
    ) -> httpx2.Response:
        del http_request.headers["x-server-timeout"]
        if mutation == "changed":
            http_request.headers["x-server-timeout"] = "901"
        elif mutation == "duplicate":
            http_request.headers = httpx2.Headers(
                [
                    *http_request.headers.multi_items(),
                    ("x-server-timeout", "900"),
                    ("x-server-timeout", "900"),
                ]
            )
        return await original_send(client, http_request, **kwargs)

    monkeypatch.setattr(httpx2.AsyncClient, "send", send)
    calls: list[bytes] = []

    def handler(http_request: httpx2.Request) -> httpx2.Response:
        calls.append(http_request.content)
        return httpx2.Response(
            200, json=envelope('{"scores":[]}', modelVersion=spec.model)
        )

    adapter = GeminiAdapter(
        spec.endpoint, spec.model, http_transport=httpx2.MockTransport(handler)
    )
    invocation = {
        "submission_id": wire.transport_plan.identity.submission_id,
        "correlation_id": wire.transport_plan.identity.correlation_id,
    }
    try:
        if revision == 2:
            with pytest.raises(ProviderIntegrityError, match="server_timeout_hint"):
                await adapter.submit(wire, **invocation)
            assert not calls and adapter.actual_call_count == 0
        else:
            await adapter.submit(wire, **invocation)
            assert calls == [wire.payload] and adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "provider,endpoint", [route for route in ROUTES if route[0] != "anthropic"]
)
@pytest.mark.parametrize("failure", ["deadline", "read", "cancel", "429", "503"])
@pytest.mark.asyncio
async def test_flex_uncertainty_never_retries_or_switches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    endpoint: str,
    failure: str,
) -> None:
    wire, spec, _, _ = request(tmp_path, provider, endpoint, "flex")
    module = importlib.import_module("judgmentllm.providers." + provider)
    calls = 0

    async def handler(http_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        assert http_request.content == wire.payload
        if failure == "read":
            raise httpx2.ReadTimeout("PRIVATE_TIMEOUT", request=http_request)
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure in {"429", "503"}:
            return httpx2.Response(
                int(failure), json={"error": {"message": "PRIVATE_ERROR"}}
            )
        await asyncio.Event().wait()
        raise AssertionError

    if failure == "deadline":
        original = asyncio.wait_for

        async def short(awaitable: Any, timeout: float | None) -> Any:
            assert timeout == 960
            return await original(awaitable, 0.01)

        monkeypatch.setattr(module.asyncio, "wait_for", short)
    adapter = getattr(
        module, "OpenAIAdapter" if provider == "openai" else "GeminiAdapter"
    )(endpoint, spec.model, http_transport=httpx2.MockTransport(handler))
    try:
        with pytest.raises(
            ProviderInvocationCancelled if failure == "cancel" else SubmissionUncertain
        ) as caught:
            await adapter.submit(
                wire,
                submission_id=wire.transport_plan.identity.submission_id,
                correlation_id=wire.transport_plan.identity.correlation_id,
            )
        assert caught.value.invoked is True
        assert calls == adapter.actual_call_count == 1
        assert "PRIVATE" not in str(caught.value)
    finally:
        await adapter.aclose()


def test_strict_policy_versions_and_qualification_omission(tmp_path: Path) -> None:
    wire, spec, _, _ = request(tmp_path, "gemini", "models.generate_content", "flex")
    policy = wire.output_commitment.service_policy
    for changed in (
        {"service_policy_version": True},
        {"read_timeout_seconds": 60},
        {"read_timeout_seconds": 930.0},
        {"wire_tier": "standard"},
        {"unexpected": "private"},
    ):
        with pytest.raises(ValueError):
            ServicePolicyV2.model_validate(dict(policy) | changed)
    with pytest.raises(ValueError):
        ServicePolicyV2.model_validate_json(
            b'{"service_policy_version":2,' + policy.model_dump_json().encode()[1:]
        )
    assert not qualified_service(policy, ())
    assert not qualified_service(
        policy,
        (ServiceQualificationScope(account_scope="another-account", tier="flex"),),
    )
    assert qualified_service(policy, (policy.qualification_scope,))
    for revision in (True, 1.0, 3):
        with pytest.raises(ValueError):
            service_policy(spec, revision)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        service_policy(spec, 1)


def test_qualification_and_observation_ingress_fail_closed(tmp_path: Path) -> None:
    _, spec, _, _ = request(tmp_path, "openai", "responses", None)
    with pytest.raises(ValueError, match="account scope"):
        service_policy(spec, 1)
    with pytest.raises(ValueError, match="provider"):
        service_policy(spec.model_copy(update={"provider": "unsupported"}))
    for version in (True, 2.0, "2", 3):
        with pytest.raises(ValueError):
            ServiceQualificationScope.model_validate(
                {
                    "account_scope": "synthetic-account",
                    "tier": "standard",
                    "service_policy_version": version,
                }
            )
    with pytest.raises(ValueError, match="duplicate"):
        ServiceQualificationScope(
            account_scope="synthetic-account",
            tier="standard",
            features=("rubric_scores", "rubric_scores"),
        )
    policy = service_policy(spec.model_copy(update={"account_scope": None}))
    assert policy is not None and policy.qualification_scope is None
    original = observe_service_tier(policy, "default")
    for changed in (
        {"outcome": "downgraded"},
        {"source": "gemini.x-gemini-service-tier"},
    ):
        with pytest.raises(ValueError, match="observation differs"):
            ServiceTierObservation.model_validate(dict(original) | changed)


@pytest.mark.parametrize(
    "provider,error",
    [
        ("openai", {"type": "rate_limit_error"}),
        ("openai", {"code": "resource_unavailable"}),
        ("anthropic", {"type": "rate_limit_error"}),
        ("gemini", {"code": 429, "status": "RESOURCE_EXHAUSTED"}),
    ],
)
def test_documented_terminal_nonacceptance(
    provider: str, error: dict[str, object]
) -> None:
    assert terminal_rate_rejection(provider, json.dumps({"error": error}).encode())
    for raw in (None, b"invalid", b"[]", b'{"error":[]}', b'{"error":{"code":429}}'):
        assert not terminal_rate_rejection(provider, raw)


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "provider,tier,reported,outcome",
    [
        ("openai", "fast", "default", "downgraded"),
        ("gemini", "priority", "standard", "downgraded"),
        ("gemini", "flex", "standard", "unexpected"),
        ("openai", "flex", None, "unknown"),
    ],
)
@pytest.mark.asyncio
async def test_observation_survives_recovery_without_tier_switch(
    tmp_path: Path,
    provider: str,
    tier: str,
    reported: str | None,
    outcome: str,
    capture: str,
) -> None:
    from judgmentllm.execution import execute_run, recover_run
    from judgmentllm.persistence.repository import ReadOnlyRepository
    from judgmentllm.preparation import prepare_run

    fixture = importlib.import_module("scripts." + provider + "_offline_fixture")
    transport_module = importlib.import_module(
        "judgmentllm."
        + ("production_transport" if provider == "openai" else "gemini_transport")
    )
    endpoint = "responses" if provider == "openai" else "models.generate_content"
    spec = fixture.specification(
        tmp_path / "inputs", endpoint, "rubric", capture=capture
    )
    spec.execution.tier = tier
    spec.execution.concurrency = 1
    source = fixture.FixtureTransport(endpoint)

    def response(request: httpx2.Request) -> httpx2.Response:
        original = source.respond(request)
        body = original.json()
        headers: dict[str, str] = {}
        if provider == "openai":
            body.pop("service_tier", None)
            if reported is not None:
                body["service_tier"] = reported
        elif reported is not None:
            headers["x-gemini-service-tier"] = reported
        return httpx2.Response(200, json=body, headers=headers, request=request)

    module = importlib.import_module("judgmentllm.providers." + provider)
    adapter_type = getattr(
        module, "OpenAIAdapter" if provider == "openai" else "GeminiAdapter"
    )
    adapter = adapter_type(
        endpoint,
        spec.model,
        concurrency=1,
        http_transport=httpx2.MockTransport(response),
    )
    store = tmp_path / "runtime"
    with fixture.offline():
        with prepare_run(
            spec,
            store=store,
            run_id="001",
            capabilities=adapter.capabilities(spec),
            transport_context=transport_module.production_context(spec),
        ):
            pass
        try:
            if outcome == "downgraded":
                report = await execute_run(
                    "001", store=store, adapter=adapter, spec=spec
                )
                assert report.summary.successful_results == 2
            else:
                with pytest.raises(ExceptionGroup):
                    await execute_run("001", store=store, adapter=adapter, spec=spec)
            with ReadOnlyRepository(store) as repository:
                receipts = tuple(repository.iter_evidence("001"))
                assert len(receipts) == 1
                assert receipts[0].service_tier is not None
                assert receipts[0].service_tier.outcome == outcome
                before = canonical_bytes(receipts[0])
            try:
                await recover_run("001", store=store, adapter=adapter, spec=spec)
            except ExceptionGroup:
                assert outcome != "downgraded"
            with ReadOnlyRepository(store) as repository:
                assert canonical_bytes(next(repository.iter_evidence("001"))) == before
                results = tuple(repository.iter_results("001"))
                assert len(results) == (2 if outcome == "downgraded" else 0)
                assert all(result.tier == tier for result in results)
            assert source.calls == adapter.actual_call_count == 1
        finally:
            await adapter.aclose()
