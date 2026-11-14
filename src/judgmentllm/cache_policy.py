"""Cache revisions are sealed policy, independent of assessment interpretation.

Persisted omission means revision 1. New runs use revision 2; no configuration
field can select historical implicit caching.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator


def _version(value: object) -> object:
    if type(value) is not int:
        raise ValueError("cache_policy_version must be an integer")
    return value


CachePolicyVersion = Annotated[Literal[1, 2], BeforeValidator(_version)]

# Exact documented scopes, independently exercised through the pinned SDK.
# These request controls grant no assessment or native-output qualification.
_OPENAI_OFF_SCOPES = frozenset(
    (endpoint, model, "openai", "3.42.1", None)
    for endpoint in ("responses", "chat.completions")
    for model in ("gpt-6-luna", "gpt-6.1-terra")
)


def validate_openai_off(scope: object) -> None:
    """Refuse unknown/older scopes instead of silently omitting the OFF control."""
    from judgmentllm.native_schema import NativeProjectionScopeV1

    if not isinstance(scope, NativeProjectionScopeV1) or (
        scope.provider != "openai"
        or scope.model_scope != "exact"
        or (
            scope.endpoint,
            scope.model,
            scope.sdk_package,
            scope.sdk_version,
            scope.api_version,
        )
        not in _OPENAI_OFF_SCOPES
    ):
        raise ValueError(
            "execution.cache_policy: off requires a supported exact OpenAI "
            "model/endpoint/SDK scope; use a documented supported route"
        )
