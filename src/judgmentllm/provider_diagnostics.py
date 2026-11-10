"""Explicit provider dispatch for non-generative diagnostics; status stays offline."""

from __future__ import annotations

from typing import Literal, overload

from judgmentllm import anthropic_diagnostics as anthropic
from judgmentllm import gemini_diagnostics as gemini
from judgmentllm import openai_diagnostics as openai


@overload
async def adiscover_models(
    *,
    provider: Literal["openai"] = "openai",
    model: str | None = None,
    http_transport: object | None = None,
) -> openai.ModelDiscovery: ...
@overload
async def adiscover_models(
    *,
    provider: Literal["anthropic"],
    model: str | None = None,
    http_transport: object | None = None,
) -> anthropic.ModelDiscovery: ...
@overload
async def adiscover_models(
    *,
    provider: Literal["gemini"],
    model: str | None = None,
    http_transport: object | None = None,
) -> gemini.ModelDiscovery: ...
@overload
async def adiscover_models(
    *, provider: str, model: str | None = None, http_transport: object | None = None
) -> openai.ModelDiscovery | anthropic.ModelDiscovery | gemini.ModelDiscovery: ...
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


@overload
async def averify_authentication(
    *, provider: Literal["openai"] = "openai", http_transport: object | None = None
) -> openai.AuthVerification: ...
@overload
async def averify_authentication(
    *, provider: Literal["anthropic"], http_transport: object | None = None
) -> anthropic.AuthVerification: ...
@overload
async def averify_authentication(
    *, provider: Literal["gemini"], http_transport: object | None = None
) -> gemini.AuthVerification: ...
@overload
async def averify_authentication(
    *, provider: str, http_transport: object | None = None
) -> openai.AuthVerification | anthropic.AuthVerification | gemini.AuthVerification: ...
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


@overload
def discover_models(
    *,
    provider: Literal["openai"] = "openai",
    model: str | None = None,
    http_transport: object | None = None,
) -> openai.ModelDiscovery: ...
@overload
def discover_models(
    *,
    provider: Literal["anthropic"],
    model: str | None = None,
    http_transport: object | None = None,
) -> anthropic.ModelDiscovery: ...
@overload
def discover_models(
    *,
    provider: Literal["gemini"],
    model: str | None = None,
    http_transport: object | None = None,
) -> gemini.ModelDiscovery: ...
@overload
def discover_models(
    *, provider: str, model: str | None = None, http_transport: object | None = None
) -> openai.ModelDiscovery | anthropic.ModelDiscovery | gemini.ModelDiscovery: ...
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


@overload
def verify_authentication(
    *, provider: Literal["openai"] = "openai", http_transport: object | None = None
) -> openai.AuthVerification: ...
@overload
def verify_authentication(
    *, provider: Literal["anthropic"], http_transport: object | None = None
) -> anthropic.AuthVerification: ...
@overload
def verify_authentication(
    *, provider: Literal["gemini"], http_transport: object | None = None
) -> gemini.AuthVerification: ...
@overload
def verify_authentication(
    *, provider: str, http_transport: object | None = None
) -> openai.AuthVerification | anthropic.AuthVerification | gemini.AuthVerification: ...
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
