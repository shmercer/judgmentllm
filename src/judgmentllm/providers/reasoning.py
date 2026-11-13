"""Adapter-owned, version-1 reasoning mappings and documented control facts.

These six exact scopes are fixture-tested parameter facts, not a model allowlist
or production native qualification. Unknown models remain unknown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from judgmentllm.generation import GenerationCapabilities, GenerationParameterCapability
from judgmentllm.reasoning import (
    ReasoningCapabilities,
    ReasoningEffort,
    ReasoningMode,
    ReasoningPolicyV1,
    ReasoningQualificationScopeV1,
    control_errors,
)

if TYPE_CHECKING:
    from judgmentllm.config import GenerationSpec, RunSpecification, RunSpecV3
    from judgmentllm.native_schema import NativeProjectionScopeV1
    from judgmentllm.service_tiers import ServicePolicyV2

_OPENAI = ("gpt-6-luna", "gpt-6.1-terra")
_ANTHROPIC = ("claude-haiku-5-5", "claude-sonnet-5-5")
_GEMINI = ("gemini-3.5-flash-lite", "gemini-3.8-flash")


def documented_controls(
    spec: RunSpecification, scope: NativeProjectionScopeV1
) -> ReasoningCapabilities | None:
    """Exact route/SDK facts; never infer support from a model family."""
    identity = (spec.provider, spec.endpoint, spec.model)
    return _documented_controls(
        identity,
        scope,
        getattr(spec.prompt, "native_mechanism", None),
        getattr(spec, "account_scope", None),
        spec.execution.tier,
    )


def _documented_controls(
    identity: tuple[str, str, str],
    scope: NativeProjectionScopeV1,
    mechanism: str | None,
    account_scope: str | None,
    requested_tier: str | None,
) -> ReasoningCapabilities | None:
    if identity != (scope.provider, scope.endpoint, scope.model):
        raise ValueError("reasoning capability scope differs")
    provider, _, model = identity
    efforts: tuple[ReasoningEffort, ...]
    modes: tuple[ReasoningMode, ...]
    restricted: tuple[ReasoningMode, ...] = ()
    if identity[:2] == ("openai", "responses") and model in _OPENAI:
        expected = ("openai", "3.42.1", None)
        api = "openai-v1"
        efforts = ("none", "low", "medium", "high", "xhigh", "max")
        modes = ("standard", "pro")
        default_effort: ReasoningEffort = "medium"
        default_mode: ReasoningMode | None = "standard"
        sampling: tuple[ReasoningEffort, ...] = ("none",)
        api_field = sdk_field = "reasoning.effort"
        urls = (
            "https://developers.openai.com/api/docs/models/" + model,
            "https://developers.openai.com/api/docs/guides/reasoning",
            "https://developers.openai.com/api/docs/guides/latest-model",
        )
    elif identity[:2] == ("anthropic", "messages") and model in _ANTHROPIC:
        expected = ("anthropic", "2.3.0", "2026-10-21")
        api = "2026-10-21"
        efforts = ("low", "medium", "high", "xhigh", "max")
        modes = (
            "adaptive",
            "disabled" if model == _ANTHROPIC[0] else "between_tools",
        )
        restricted = modes[1:]
        default_effort = "medium" if model == _ANTHROPIC[0] else "high"
        default_mode = "adaptive"
        sampling = ()
        api_field = sdk_field = "output_config.effort"
        urls = (
            "https://platform.claude.com/docs/en/build-with-claude/effort",
            "https://platform.claude.com/docs/en/build-with-claude/thinking",
            "https://platform.claude.com/docs/en/api/messages/create",
        )
    elif identity[:2] == ("gemini", "models.generate_content") and model in _GEMINI:
        expected = ("google-genai", "2.34.0", "v1beta")
        api = "v1beta"
        efforts = (
            ("minimal", "low", "medium", "high")
            if model == _GEMINI[0]
            else ("low", "medium", "high")
        )
        modes = ()
        default_effort = "minimal" if model == _GEMINI[0] else "medium"
        default_mode = None
        sampling = efforts
        api_field = "generationConfig.thinkingConfig.thinkingLevel"
        sdk_field = "generationConfig.thinkingConfig.thinking_level"
        urls = (
            "https://ai.google.dev/gemini-api/docs/thinking?generate-content=true",
            "https://ai.google.dev/gemini-api/docs/gemini-3",
            "https://protobuf.dev/programming-guides/json/#field-names-as-json-keys",
        )
    else:
        return None
    if (scope.sdk_package, scope.sdk_version, scope.api_version) != expected:
        return None
    allowed = {
        "openai": (None, "openai.responses.text.format"),
        "anthropic": (None, "anthropic.messages.output_config.format"),
        "gemini": (
            None,
            "gemini.generate_content.response_schema",
            "gemini.generate_content.response_json_schema",
        ),
    }
    if mechanism not in allowed[provider]:
        return None
    return ReasoningCapabilities(
        sdk_package=expected[0],
        sdk_version=expected[1],
        api_version=api,
        native_mechanism=mechanism,
        account_scope=account_scope,
        requested_tier=requested_tier,
        efforts=efforts,
        modes=modes,
        documented_default_effort=default_effort,
        documented_default_mode=default_mode,
        api_effort_field=api_field,
        sdk_effort_field=sdk_field,
        sampling_efforts=sampling,
        restricted_modes=restricted,
        documentation_urls=urls,
    )


def generation_capabilities(
    spec: RunSpecification,
    scope: NativeProjectionScopeV1,
    existing: GenerationCapabilities | None,
) -> GenerationCapabilities | None:
    """Augment only requests involving the new control or conditional sampling.

    Omitted-control requests retain their original capability serialization.
    Native feature/account/tier authority is checked independently.
    """
    settings = spec.generation
    if settings is None or (
        settings.reasoning is None
        and settings.temperature is None
        and settings.top_p is None
    ):
        return existing
    facts = documented_controls(spec, scope)
    if facts is None:
        return existing
    data: dict[str, Any] = (
        dict(existing)
        if existing is not None
        else dict(provider=spec.provider, endpoint=spec.endpoint, model=spec.model)
    )
    data["reasoning"] = facts
    if settings.reasoning is not None:
        data["max_output_tokens"] = GenerationParameterCapability(
            supported=True,
            minimum=1,
            maximum=65536 if spec.provider == "gemini" else 128000,
        )
        data["temperature"] = GenerationParameterCapability(
            supported=spec.provider != "anthropic", minimum=0, maximum=2
        )
        data["top_p"] = GenerationParameterCapability(
            supported=spec.provider != "anthropic", minimum=0, maximum=1
        )
    return GenerationCapabilities.model_validate(data)


def build_reasoning_policy(
    spec: RunSpecV3, scope: NativeProjectionScopeV1
) -> ReasoningPolicyV1 | None:
    requested = spec.generation.reasoning if spec.generation else None
    if requested is None:
        return None
    facts = documented_controls(spec, scope)
    if facts is None:
        raise ValueError("generation.reasoning support is unknown for the exact scope")
    return ReasoningPolicyV1(
        provider=spec.provider,
        endpoint=spec.endpoint,
        model=spec.model,
        requested=requested,
        capabilities=facts,
    )


def wire_controls(
    settings: GenerationSpec | None, policy: ReasoningPolicyV1 | None
) -> dict[str, Any]:
    """Frozen V1 mapping, used solely by adapter-owned SDK/body builders."""
    requested = settings.reasoning if settings else None
    if requested is None:
        if policy is not None:
            raise ValueError("unexpected reasoning policy")
        return {}
    if policy is None or requested != policy.requested:
        raise ValueError("reasoning controls differ from the sealed policy")
    if control_errors(
        requested,
        policy.capabilities,
        sampling=bool(
            settings
            and (settings.temperature is not None or settings.top_p is not None)
        ),
    ):
        raise ValueError("incompatible reasoning controls")
    effort, mode = requested.effort, requested.mode
    if policy.provider == "openai":
        return {"reasoning": requested.model_dump(mode="json")}
    if policy.provider == "anthropic":
        result: dict[str, Any] = {}
        if effort is not None:
            result["output_config"] = {"effort": effort}
        if mode is not None:
            result["thinking"] = {"type": mode}
        return result
    if policy.provider == "gemini":
        if mode is not None or effort is None:
            raise ValueError("Gemini requires a thinking level without a mode")
        return {"thinking_config": {"thinking_level": effort.upper()}}
    raise ValueError("unsupported reasoning provider")


def verify_policy_scope(
    policy: ReasoningPolicyV1 | None,
    scope: NativeProjectionScopeV1,
    service: ServicePolicyV2 | None,
    mechanism: str | None,
) -> None:
    """Reconstruct frozen V1 facts; rehashed caller data cannot widen support."""
    if policy is None:
        return
    if service is None:
        raise ValueError("reasoning controls require an explicit service policy")
    identity = (policy.provider, policy.endpoint, policy.model)
    facts = _documented_controls(
        identity,
        scope,
        mechanism,
        service.account_scope,
        service.requested_tier,
    )
    if facts is None or policy.capabilities != facts:
        raise ValueError("reasoning policy scope or interpretation differs")


def qualified_reasoning(
    scopes: tuple[ReasoningQualificationScopeV1, ...],
    settings: GenerationSpec | None,
    service: ServicePolicyV2 | None,
    features: tuple[str, ...],
) -> bool:
    """Default native evidence never authorizes an untested explicit control."""
    if settings is None or settings.reasoning is None:
        return True
    return service is not None and any(
        scope.account_scope == service.account_scope
        and scope.tier == service.tier
        and scope.reasoning == settings.reasoning
        and scope.temperature == settings.temperature
        and scope.top_p == settings.top_p
        and set(features) <= set(scope.features)
        for scope in scopes
    )
