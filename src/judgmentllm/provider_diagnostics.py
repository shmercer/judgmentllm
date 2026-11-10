"""Explicit provider dispatch for non-generative diagnostics; status stays offline."""

from __future__ import annotations

from judgmentllm import anthropic_diagnostics as anthropic
from judgmentllm import gemini_diagnostics as gemini
from judgmentllm import openai_diagnostics as openai


async def adiscover_models(
    *,
    provider: str = "openai",
    model: str | None = None,
    http_transport: object | None = None,
) -> openai.ModelDiscovery | anthropic.ModelDiscovery | gemini.ModelDiscovery:
    implementation = (
        gemini
        if provider == "gemini"
        else anthropic
        if provider == "anthropic"
        else openai
    )
    return await implementation.adiscover_models(
        provider=provider, model=model, http_transport=http_transport
    )


async def averify_authentication(
    *, provider: str = "openai", http_transport: object | None = None
) -> openai.AuthVerification | anthropic.AuthVerification | gemini.AuthVerification:
    implementation = (
        gemini
        if provider == "gemini"
        else anthropic
        if provider == "anthropic"
        else openai
    )
    return await implementation.averify_authentication(
        provider=provider, http_transport=http_transport
    )


def discover_models(
    *,
    provider: str = "openai",
    model: str | None = None,
    http_transport: object | None = None,
) -> openai.ModelDiscovery | anthropic.ModelDiscovery | gemini.ModelDiscovery:
    implementation = (
        gemini
        if provider == "gemini"
        else anthropic
        if provider == "anthropic"
        else openai
    )
    return implementation.discover_models(
        provider=provider, model=model, http_transport=http_transport
    )


def verify_authentication(
    *, provider: str = "openai", http_transport: object | None = None
) -> openai.AuthVerification | anthropic.AuthVerification | gemini.AuthVerification:
    implementation = (
        gemini
        if provider == "gemini"
        else anthropic
        if provider == "anthropic"
        else openai
    )
    return implementation.verify_authentication(
        provider=provider, http_transport=http_transport
    )
