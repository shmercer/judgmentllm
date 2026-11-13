"""Private exact-scope candidate facts. These never qualify production traffic."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import importlib
from dataclasses import replace
from typing import Any

from judgmentllm.config import RunSpecification, RunSpecV3, validate_run_spec
from judgmentllm.generation import GenerationCapabilities, GenerationParameterCapability
from judgmentllm.native_schema import NativeProjectionScopeV1
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.planning import CapabilityProfile
from judgmentllm.providers.protocol import ProviderIntegrityError
from judgmentllm.providers.reasoning import documented_controls, generation_capabilities
from judgmentllm.study.definitions import CONTRASTS, MODELS, ROUTES


def study_generation(
    spec: RunSpecification, scope: NativeProjectionScopeV1
) -> GenerationCapabilities:
    """Original omitted controls or the exact effort-only/temperature contrasts."""
    checked = validate_run_spec(spec)
    facts = documented_controls(checked, scope)
    if (
        not isinstance(checked, RunSpecV3)
        or facts is None
        or (checked.provider, checked.model) not in MODELS
        or (
            checked.prompt.native_mechanism != ROUTES[checked.provider][1]
            or checked.prompt.structured_output != "native"
            or checked.prompt.output_order != "together"
            or not checked.strict_capabilities
            or checked.account_scope is None
            or checked.execution.mode != "live"
            or checked.execution.concurrency != 1
            or checked.allow_ties
            or checked.generation is None
            or checked.generation.top_p is not None
            or checked.generation.max_output_tokens not in (2048, 4096, 8192)
        )
    ):
        raise ProviderIntegrityError("Study candidate scope or controls differ")
    settings = checked.generation
    assert settings is not None
    if settings.reasoning is not None or settings.temperature is not None:
        if (
            settings.reasoning is None
            or settings.reasoning.mode is not None
            or settings.reasoning.effort != CONTRASTS[checked.model]
            or settings.temperature not in (None, 0.2)
            or (settings.temperature is not None and checked.provider != "openai")
            or checked.execution.tier
            != ("standard_only" if checked.provider == "anthropic" else "flex")
            or checked.execution.explanations
            or settings.max_output_tokens
            != (8192 if checked.task_kind == "rubric" else 4096)
            or (
                checked.task_kind == "rubric"
                and checked.execution.grouping != "grouped"
            )
        ):
            raise ProviderIntegrityError("Study experimental controls differ")
    profile = GenerationCapabilities(
        provider=checked.provider,
        endpoint=checked.endpoint,
        model=checked.model,
        max_output_tokens=GenerationParameterCapability(
            supported=True,
            minimum=1,
            maximum=65536 if checked.provider == "gemini" else 128000,
        ),
        source="study-native-candidate-facts-v1",
        fixture_tested=True,
        live_qualified=False,
    )
    return generation_capabilities(checked, scope, profile) or profile


def candidate_context(spec: RunSpecV3, source: str) -> Any:
    module = importlib.import_module(
        "judgmentllm."
        + {
            "openai": "production_transport",
            "anthropic": "anthropic_transport",
            "gemini": "gemini_transport",
        }[spec.provider]
    )
    context = replace(
        module._qualification_context(spec, source_commit=source),
        kind=spec.provider + "-study-candidate-v1",
    )
    study_generation(spec, context.scope)
    return module._checked_context(context)


def guarded_capabilities(
    guard: object, context: Any, spec: RunSpecification
) -> CapabilityProfile:
    from judgmentllm.study.execution import _StudyGuard

    if type(guard) is not _StudyGuard:
        raise ProviderIntegrityError("Study candidate requires its private guard")
    guard.verify_spec(context, spec)
    return CapabilityProfile(
        modes=("live",),
        grouped=True,
        explanations=True,
        ties=False,
        generation=study_generation(spec, context.scope),
    )


def guarded_authority(guard: object, context: Any) -> None:
    from judgmentllm.study.execution import _StudyGuard

    if type(guard) is not _StudyGuard:
        raise ProviderIntegrityError("Study execution requires its private guard")
    guard.verify_execution(context)


def spec_digest(spec: RunSpecification) -> str:
    return bytes_digest(canonical_bytes(validate_run_spec(spec)))
