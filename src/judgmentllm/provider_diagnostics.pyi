"""Provider-specific static overloads for the small runtime dispatcher."""

from typing import Literal, overload

from judgmentllm import anthropic_diagnostics as anthropic
from judgmentllm import gemini_diagnostics as gemini
from judgmentllm import openai_diagnostics as openai
from judgmentllm import vertex_diagnostics as vertex

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
) -> (
    openai.ModelDiscovery
    | anthropic.ModelDiscovery
    | gemini.ModelDiscovery
    | vertex.ModelDiscovery
): ...
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
) -> (
    openai.AuthVerification
    | anthropic.AuthVerification
    | gemini.AuthVerification
    | vertex.AuthVerification
): ...
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
) -> (
    openai.ModelDiscovery
    | anthropic.ModelDiscovery
    | gemini.ModelDiscovery
    | vertex.ModelDiscovery
): ...
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
) -> (
    openai.AuthVerification
    | anthropic.AuthVerification
    | gemini.AuthVerification
    | vertex.AuthVerification
): ...
