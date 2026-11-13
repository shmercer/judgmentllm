"""Requested controls, exact SDK bytes, scope isolation and historical identities."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import importlib
import json
import socket
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from pydantic import ValidationError
from reasoning_fixture import MODELS, authorize, case, response_body, wire

from judgmentllm import GenerationSpec, ReasoningSpec
from judgmentllm.config import RunSpec, RunSpecV2
from judgmentllm.persistence.generation import (
    build_generation_commitment,
    validate_generation_compatibility,
)
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.planning import validate_generation
from judgmentllm.providers.reasoning import (
    documented_controls,
    generation_capabilities,
    qualified_reasoning,
)


def adapter_for(spec: Any, context: Any, handler: Any) -> Any:
    adapter_type = getattr(
        importlib.import_module("judgmentllm.providers." + spec.provider),
        {
            "openai": "OpenAIAdapter",
            "anthropic": "AnthropicAdapter",
            "gemini": "GeminiAdapter",
        }[spec.provider],
    )
    return adapter_type(
        spec.endpoint,
        spec.model,
        context=context,
        concurrency=spec.execution.concurrency,
        http_transport=httpx2.MockTransport(handler),
    )


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.setenv(key, "SYNTHETIC_REASONING_KEY")

    def deny(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("external network forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.mark.parametrize("value", [None, {}, {"effort": None}, {"mode": None}])
def test_omission_is_sparse_and_explicit_none_is_a_string(value: Any) -> None:
    settings = GenerationSpec.model_validate({"reasoning": value})
    assert settings.model_dump() == {}
    assert settings.model_dump_json() == "{}"
    explicit = GenerationSpec(reasoning=ReasoningSpec(effort="none"))
    assert explicit.model_dump() == {"reasoning": {"effort": "none"}}
    assert explicit != settings


@pytest.mark.parametrize(
    "value",
    [
        True,
        0,
        "none",
        [],
        {"effort": 1},
        {"effort": True},
        {"effort": "default"},
        {"effort": "HIGH"},
        {"mode": "none"},
        {"thinking_budget": 0},
    ],
)
def test_invalid_controls_are_not_sdk_kwargs(value: Any) -> None:
    with pytest.raises(ValidationError):
        GenerationSpec.model_validate({"reasoning": value})


@pytest.mark.parametrize("provider,endpoint,model,effort", MODELS)
@pytest.mark.parametrize("pairwise", [False, True])
def test_control_commitments_and_unchanged_contract(
    tmp_path: Path,
    provider: str,
    endpoint: str,
    model: str,
    effort: str,
    pairwise: bool,
) -> None:
    spec, dep, module, context, _ = case(
        tmp_path, provider, model, reasoning=effort, pairwise=pairwise
    )
    current = wire(spec, dep, module, context)
    assert current.output_commitment.reasoning_policy.requested.effort == effort
    restored = cast(Any, type(current)).model_validate_json(canonical_bytes(current))
    assert canonical_bytes(restored) == canonical_bytes(current)
    rebuilt = wire(spec, dep, module, context, frozen=current.transport_commitment)
    assert canonical_bytes(rebuilt) == canonical_bytes(current)
    caps = generation_capabilities(spec, context.scope, None)
    commitment = build_generation_commitment(spec, capabilities=caps)
    assert commitment.settings is not None
    assert spec.generation is not None
    assert commitment.settings.reasoning == spec.generation.reasoning
    omitted = spec.model_copy(
        update={"generation": GenerationSpec(max_output_tokens=256)}
    )
    assert any(
        i.code == "generation_settings_changed"
        for i in validate_generation_compatibility(commitment, omitted).issues
    )
    before, before_dep, before_module, before_context, _ = case(
        tmp_path, provider, model, pairwise=pairwise
    )
    old = wire(before, before_dep, before_module, before_context)
    assert old.output_commitment.reasoning_policy is None
    assert b'"reasoning_policy"' not in canonical_bytes(old)
    assert old.output_commitment.schema_text == current.output_commitment.schema_text
    assert old.output_commitment.projection == current.output_commitment.projection
    assert old.payload_sha256 != current.payload_sha256
    assert old.request_uid != current.request_uid


@pytest.mark.parametrize("provider,endpoint,model,effort", MODELS)
def test_control_evidence_scope_and_versions(
    tmp_path: Path,
    provider: str,
    endpoint: str,
    model: str,
    effort: str,
) -> None:
    spec, _, _, context, _ = case(tmp_path, provider, model, reasoning=effort)
    caps = generation_capabilities(spec, context.scope, None)
    assert caps is not None and caps.reasoning is not None
    assert validate_generation(spec, capabilities=caps).valid
    assert not validate_generation(spec).valid
    for change in ({"sdk_version": "other"}, {"api_version": "other"}):
        scope = context.scope.model_copy(update=change)
        assert documented_controls(spec, scope) is None
    for attribute, value in (
        ("account_scope", "other"),
        ("requested_tier", "unknown"),
        ("native_mechanism", "other"),
    ):
        bad = caps.model_copy(
            update={"reasoning": caps.reasoning.model_copy(update={attribute: value})}
        )
        assert not validate_generation(spec, capabilities=bad).valid
    for value in (True, 2, "1"):
        with pytest.raises(ValidationError):
            type(caps.reasoning).model_validate(
                {**dict(caps.reasoning), "control_version": value}
            )
    for version in (RunSpec, RunSpecV2):
        data = spec.model_dump()
        data.pop("account_scope", None)
        data["schema_version"] = 1 if version is RunSpec else 2
        data["prompt"] = {
            "preset": "pairwise_forced_choice"
            if spec.task_kind == "pairwise"
            else "rubric_scores_only"
        }
        with pytest.raises(ValidationError, match="generation.reasoning"):
            version.model_validate(data)


@pytest.mark.parametrize("provider,endpoint,model,effort", MODELS)
def test_default_native_evidence_does_not_authorize_explicit_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    endpoint: str,
    model: str,
    effort: str,
) -> None:
    spec, dep, module, context, _ = case(tmp_path, provider, model, reasoning=effort)
    candidate = wire(spec, dep, module, context)
    production = authorize(monkeypatch, spec, candidate, module, reasoning_scopes=False)
    with pytest.raises(ValueError):
        wire(spec, dep, module, production)
    production = authorize(monkeypatch, spec, candidate, module)
    assert (
        wire(spec, dep, module, production).output_commitment.selection.native_support
        == "supported"
    )
    registry = importlib.import_module(
        "judgmentllm."
        + {
            "openai": "production_registry",
            "anthropic": "anthropic_registry",
            "gemini": "gemini_registry",
        }[provider]
    )
    profile = registry._QUALIFIED_PROFILES[0]
    original = profile.reasoning_scopes[0]
    for changes in (
        {"account_scope": "other"},
        {"tier": "flex"},
        {"temperature": 0.2},
        {"features": ()},
    ):
        altered = original.model_copy(update=changes)
        assert not qualified_reasoning(
            (altered,),
            spec.generation,
            candidate.output_commitment.service_policy,
            candidate.output_commitment.selection.features,
        )


@pytest.mark.parametrize(
    "provider,model,effort,mode",
    [
        ("openai", "gpt-6-luna", "minimal", None),
        ("openai", "gpt-6-luna", "low", "adaptive"),
        ("anthropic", "claude-haiku-5-5", "none", None),
        ("anthropic", "claude-sonnet-5-5", "low", "disabled"),
        ("anthropic", "claude-haiku-5-5", "max", "disabled"),
        ("gemini", "gemini-3.8-flash", "minimal", None),
        ("gemini", "gemini-3.8-flash", "none", None),
        ("gemini", "gemini-3.5-flash-lite", "high", "disabled"),
    ],
)
def test_incompatible_efforts_and_modes_fail_offline(
    tmp_path: Path, provider: str, model: str, effort: str, mode: str | None
) -> None:
    with pytest.raises(ValueError):
        case(tmp_path, provider, model, reasoning=effort, mode=mode)


@pytest.mark.parametrize(
    "provider,model,effort,mode",
    [
        ("openai", "gpt-6-luna", None, None),
        ("openai", "gpt-6-luna", "medium", None),
        ("openai", "gpt-6.1-terra", "high", None),
        ("anthropic", "claude-haiku-5-5", "low", "disabled"),
        ("anthropic", "claude-sonnet-5-5", "low", "between_tools"),
    ],
)
def test_sampling_restrictions_fail_before_credentials(
    tmp_path: Path, provider: str, model: str, effort: str | None, mode: str | None
) -> None:
    with pytest.raises(ValueError):
        case(tmp_path, provider, model, reasoning=effort, mode=mode, temperature=0.2)


@pytest.mark.parametrize("provider,endpoint,model,effort", MODELS)
@pytest.mark.parametrize("pairwise", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.asyncio
async def test_actual_pinned_sdk_native_tier_and_effort_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    endpoint: str,
    model: str,
    effort: str,
    pairwise: bool,
    explicit: bool,
) -> None:
    spec, dep, module, context, _ = case(
        tmp_path,
        provider,
        model,
        reasoning=effort if explicit else None,
        tier="standard_only" if provider == "anthropic" else "flex",
        pairwise=pairwise,
    )
    candidate = wire(spec, dep, module, context)
    production = authorize(monkeypatch, spec, candidate, module)
    prepared = wire(spec, dep, module, production)
    seen: list[bytes] = []
    output = '{"winner_id":"essay_01"}' if pairwise else '{"scores":[]}'

    def response(request: httpx2.Request) -> httpx2.Response:
        assert request.content == prepared.payload
        seen.append(request.content)
        if provider == "gemini":
            from gemini_offline import envelope

            raw = envelope(output, modelVersion=model)
            headers = {"x-gemini-service-tier": "flex"}
        else:
            envelope = importlib.import_module(
                "test_" + provider + "_lifecycle"
            ).envelope
            raw = envelope(endpoint, output)
            raw["model"] = model
            if provider == "openai":
                raw["service_tier"] = "flex"
            else:
                raw["usage"]["service_tier"] = "standard"
            headers = {}
        return httpx2.Response(200, json=raw, headers=headers)

    adapter_type = getattr(
        importlib.import_module("judgmentllm.providers." + provider),
        {
            "openai": "OpenAIAdapter",
            "anthropic": "AnthropicAdapter",
            "gemini": "GeminiAdapter",
        }[provider],
    )
    adapter = adapter_type(
        endpoint,
        model,
        context=production,
        http_transport=httpx2.MockTransport(response),
    )
    try:
        await adapter.submit(
            prepared,
            submission_id=prepared.transport_plan.identity.submission_id,
            correlation_id=prepared.transport_plan.identity.correlation_id,
        )
        assert len(seen) == 1
        body = json.loads(seen[0])
        if provider == "openai":
            assert body.get("reasoning") == ({"effort": effort} if explicit else None)
            assert body["text"]["format"]["type"] == "json_schema"
        elif provider == "anthropic":
            assert body["output_config"].get("effort") == (effort if explicit else None)
            assert "thinking" not in body
            assert body["output_config"]["format"]["type"] == "json_schema"
        else:
            assert body["generationConfig"].get("thinkingConfig") == (
                {"thinking_level": effort.upper()} if explicit else None
            )
            assert "responseJsonSchema" in body["generationConfig"]
        assert "temperature" not in body and "top_p" not in body
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "provider,model,effort,mode,temperature",
    [
        ("openai", "gpt-6-luna", "low", "standard", None),
        ("openai", "gpt-6.1-terra", "high", "pro", None),
        ("openai", "gpt-6-luna", "none", None, 0.2),
        ("openai", "gpt-6.1-terra", "none", None, 0.2),
        ("anthropic", "claude-haiku-5-5", "low", "adaptive", None),
        ("anthropic", "claude-haiku-5-5", "low", "disabled", None),
        ("anthropic", "claude-sonnet-5-5", "low", "between_tools", None),
        ("gemini", "gemini-3.5-flash-lite", "minimal", None, 0.2),
        ("gemini", "gemini-3.8-flash", "low", None, 0.2),
    ],
)
@pytest.mark.asyncio
async def test_modes_and_sampling_are_distinct_typed_sdk_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model: str,
    effort: str,
    mode: str | None,
    temperature: float | None,
) -> None:
    spec, dep, module, context, _ = case(
        tmp_path,
        provider,
        model,
        reasoning=effort,
        mode=mode,
        temperature=temperature,
        tier="standard_only" if provider == "anthropic" else "flex",
    )
    candidate = wire(spec, dep, module, context)
    production = authorize(monkeypatch, spec, candidate, module)
    prepared = wire(spec, dep, module, production)
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.content == prepared.payload
        seen.append(json.loads(request.content))
        body, headers = response_body(spec, '{"scores":[]}')
        return httpx2.Response(200, json=body, headers=headers)

    adapter = adapter_for(spec, production, handler)
    try:
        await adapter.submit(
            prepared,
            submission_id=prepared.transport_plan.identity.submission_id,
            correlation_id=prepared.transport_plan.identity.correlation_id,
        )
    finally:
        await adapter.aclose()
    assert len(seen) == 1
    body = seen[0]
    if provider == "openai":
        assert body["reasoning"] == {
            "effort": effort,
            **({"mode": mode} if mode else {}),
        }
        assert body.get("temperature") == temperature
    elif provider == "anthropic":
        assert body["thinking"] == {"type": mode}
        assert body["output_config"]["effort"] == effort
        assert "format" in body["output_config"]
    else:
        assert body["generationConfig"]["thinkingConfig"] == {
            "thinking_level": effort.upper()
        }
        assert body["generationConfig"]["temperature"] == temperature
        assert "topP" not in body["generationConfig"]
    assert "top_p" not in body


@pytest.mark.parametrize("provider,endpoint,model,effort", MODELS)
def test_changed_control_cannot_reconstruct_or_retry_old_attempt(
    tmp_path: Path,
    provider: str,
    endpoint: str,
    model: str,
    effort: str,
) -> None:
    spec, dep, module, context, _ = case(tmp_path, provider, model, reasoning=effort)
    prepared = wire(spec, dep, module, context)
    changed = spec.model_copy(
        update={
            "generation": GenerationSpec(
                max_output_tokens=256, reasoning=ReasoningSpec(effort="medium")
            )
        }
    )
    with pytest.raises(ValueError):
        wire(changed, dep, module, context, frozen=prepared.transport_commitment)
    from judgmentllm.transport import physical_identity

    with pytest.raises(ValueError):
        module.prepare_production_request(
            changed,
            dep,
            physical_identity("001", dep.request.request_uid, "02"),
            context,
            parent=prepared.transport_commitment,
        )


@pytest.mark.parametrize("provider,endpoint,model,effort", MODELS[::2])
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.asyncio
async def test_actual_engine_reopening_preserves_safe_reasoning_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    endpoint: str,
    model: str,
    effort: str,
    capture: str,
) -> None:
    from judgmentllm.execution import execute_run
    from judgmentllm.execution.retry import RetryPolicy
    from judgmentllm.persistence.repository import ReadOnlyRepository
    from judgmentllm.preparation import open_prepared_run, prepare_run

    spec, dep, module, context, _ = case(
        tmp_path / "inputs", provider, model, reasoning=effort
    )
    spec.capture = cast(Any, capture)
    spec.execution.concurrency = 1
    candidate = wire(spec, dep, module, context)
    production = authorize(monkeypatch, spec, candidate, module)
    assert spec.rubric is not None
    output = json.dumps(
        {
            "scores": [
                {"trait_id": t.trait_id, "category_label": t.levels[0].label}
                for t in spec.rubric.traits
            ]
        }
    )
    seen: list[bytes] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.content)
        body, headers = response_body(spec, output)
        if provider == "gemini":
            body["candidates"][0]["content"]["parts"].insert(
                0,
                {
                    "thought": True,
                    "text": "PRIVATE_REASONING_CANARY",
                    "thoughtSignature": "PRIVATE_SIGNATURE_CANARY",
                },
            )
        elif provider == "anthropic":
            body["content"].insert(
                0,
                {
                    "type": "thinking",
                    "thinking": "PRIVATE_REASONING_CANARY",
                    "signature": "PRIVATE_SIGNATURE_CANARY",
                },
            )
        else:
            body["output"].insert(
                0,
                {
                    "type": "reasoning",
                    "id": "rs-private",
                    "summary": [
                        {"type": "summary_text", "text": "PRIVATE_REASONING_CANARY"}
                    ],
                },
            )
        return httpx2.Response(200, json=body, headers=headers)

    adapter = adapter_for(spec, production, handler)
    store = tmp_path / "store"
    with prepare_run(
        spec,
        run_id="001",
        store=store,
        transport_context=production,
        capabilities=adapter.capabilities(spec),
    ):
        pass
    try:
        report = await execute_run(
            "001",
            store=store,
            spec=spec,
            adapter=adapter,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        assert not report.recovery_required
    finally:
        await adapter.aclose()
    with open_prepared_run(
        "001",
        spec=spec,
        store=store,
        transport_context=production,
        capabilities=adapter.capabilities(spec),
    ) as reopened:
        settings = reopened.manifest.generation.settings
        assert settings is not None and settings.reasoning is not None
        assert settings.reasoning.effort == effort
    with ReadOnlyRepository(store) as repository:
        results = list(repository.iter_results("001"))
        from judgmentllm.results import RubricResult

        assert results and all(
            isinstance(row, RubricResult) and row.item_id in ("001", "002")
            for row in results
        )
    assert len(seen) == 2
    for file in store.rglob("*"):
        if file.is_file():
            raw = file.read_bytes()
            assert (
                b"PRIVATE_REASONING_CANARY" not in raw
                and b"PRIVATE_SIGNATURE_CANARY" not in raw
            )
