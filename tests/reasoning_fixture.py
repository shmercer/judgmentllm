"""Synthetic exact-model control fixtures; authority exists only under monkeypatch."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import importlib
from pathlib import Path
from typing import Any

import pytest

from judgmentllm.config import GenerationSpec, RunSpecV3
from judgmentllm.io import read_responses, write_jsonl
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.planning import CapabilityProfile, plan_record
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.providers.reasoning import generation_capabilities
from judgmentllm.reasoning import ReasoningQualificationScopeV1
from judgmentllm.service_tiers import ServiceQualificationScope
from judgmentllm.transport import physical_identity

MODELS = (
    ("openai", "responses", "gpt-6-luna", "none"),
    ("openai", "responses", "gpt-6.1-terra", "none"),
    ("anthropic", "messages", "claude-haiku-5-5", "low"),
    ("anthropic", "messages", "claude-sonnet-5-5", "low"),
    ("gemini", "models.generate_content", "gemini-3.5-flash-lite", "high"),
    ("gemini", "models.generate_content", "gemini-3.8-flash", "low"),
)


def case(
    root: Path,
    provider: str,
    model: str,
    *,
    reasoning: object = None,
    temperature: float | None = None,
    top_p: float | None = None,
    tier: str | None = None,
    pairwise: bool = False,
    native: bool = True,
    mode: object = None,
) -> tuple[RunSpecV3, RequestDependency, Any, Any, dict[str, Any]]:
    fixture = importlib.import_module(
        {
            "openai": "test_production_transport",
            "anthropic": "anthropic_fixture",
            "gemini": "gemini_fixture",
        }[provider]
    )
    spec, dep, records = fixture.case(
        root,
        policy="native" if native else "text",
        pairwise=pairwise,
        grouped=not pairwise,
    )
    spec.model = model
    root.mkdir(parents=True, exist_ok=True)
    write_jsonl(spec.input.path, records.values())
    if spec.pairs is not None:
        assert dep.request.pair is not None
        write_jsonl(spec.pairs.path, (dep.request.pair,))
    settings: dict[str, Any] = {"max_output_tokens": 256}
    if reasoning is not None or mode is not None:
        settings["reasoning"] = {
            **({"effort": reasoning} if reasoning is not None else {}),
            **({"mode": mode} if mode is not None else {}),
        }
    if temperature is not None:
        settings["temperature"] = temperature
    if top_p is not None:
        settings["top_p"] = top_p
    spec.generation = GenerationSpec.model_validate(settings)
    spec.execution.tier = tier
    module = importlib.import_module(
        "judgmentllm."
        + {
            "openai": "production_transport",
            "anthropic": "anthropic_transport",
            "gemini": "gemini_transport",
        }[provider]
    )
    context = module._qualification_context(spec, source_commit="a" * 40)
    capabilities = CapabilityProfile(
        modes=("live",),
        grouped=True,
        explanations=True,
        generation=generation_capabilities(spec, context.scope, None),
    )
    record = dep.request.pair or dep.request.response
    assert record is not None
    planned = next(plan_record(spec, record, capabilities=capabilities))
    return (
        spec,
        RequestDependency(request=planned, dependency_sha256="0" * 64),
        module,
        context,
        records,
    )


def wire(
    spec: RunSpecV3, dep: RequestDependency, module: Any, context: Any, **kwargs: Any
) -> Any:
    records = {record.item_id: record for record in read_responses(spec.input)}

    def lookup(kind: str, identifier: str) -> ResponseItem | PairTask:
        if kind == "pair":
            assert dep.request.pair is not None
            return dep.request.pair
        return records[identifier]

    return module.prepare_production_request(
        spec,
        dep,
        physical_identity("001", dep.request.request_uid, "01"),
        context,
        lookup=lookup,
        **kwargs,
    )


def authorize(
    monkeypatch: pytest.MonkeyPatch,
    spec: RunSpecV3,
    candidate: Any,
    module: Any,
    *,
    reasoning_scopes: bool = True,
) -> Any:
    fixture = importlib.import_module(
        "test_"
        + {
            "openai": "production_transport",
            "anthropic": "anthropic_transport",
            "gemini": "gemini_transport",
        }[spec.provider]
    )
    context = module.production_context(spec)
    features = candidate.output_commitment.selection.features
    service = candidate.output_commitment.service_policy
    assert service is not None and spec.generation is not None
    changes: dict[str, Any] = {
        "supported_features": features,
        "service_scopes": (
            ServiceQualificationScope(
                account_scope="synthetic-account",
                tier=service.tier,
                features=features,
            ),
        ),
        "reasoning_scopes": (),
    }
    if reasoning_scopes and spec.generation.reasoning is not None:
        changes["reasoning_scopes"] = (
            ReasoningQualificationScopeV1(
                account_scope="synthetic-account",
                tier=service.tier,
                reasoning=spec.generation.reasoning,
                temperature=spec.generation.temperature,
                top_p=spec.generation.top_p,
                features=features,
            ),
        )
    profile = fixture._reviewed_profile(context, **changes)
    registry = importlib.import_module(
        "judgmentllm."
        + {
            "openai": "production_registry",
            "anthropic": "anthropic_registry",
            "gemini": "gemini_registry",
        }[spec.provider]
    )
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", (profile,))
    return context


def response_body(
    spec: RunSpecV3, output: str
) -> tuple[dict[str, Any], dict[str, str]]:
    """Reuse provider fixtures with their distinct usage counters."""
    if spec.provider == "gemini":
        from gemini_offline import envelope

        return envelope(output, modelVersion=spec.model), {
            "x-gemini-service-tier": spec.execution.tier or "standard"
        }
    envelope = importlib.import_module("test_" + spec.provider + "_lifecycle").envelope
    raw = envelope(spec.endpoint, output)
    raw["model"] = spec.model
    if spec.provider == "openai":
        raw["service_tier"] = spec.execution.tier or "default"
    else:
        raw["usage"]["service_tier"] = "standard"
    return raw, {}
