"""Cache revisions are sealed policy, independent of assessment interpretation.

Persisted omission means revision 1. New OFF runs use revision 2; the scoped
explicit candidate uses revision 3.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, ConfigDict, Field, model_validator

from judgmentllm.models import ID, StrictModel


def _version(value: object) -> object:
    if type(value) is not int:
        raise ValueError("cache_policy_version must be an integer")
    return value


CachePolicyVersion = Annotated[Literal[1, 2, 3], BeforeValidator(_version)]
CacheAnchor = Literal["assessment", "exemplar", "output_instructions"]
OPENAI_EXPLICIT_ROUTE = ("openai", "responses", "gpt-6-luna")
OPENAI_CACHE_MECHANISM = "openai.responses.text.format"
OPENAI_CACHE_TTL = "30m"


class CacheBreakpoint(StrictModel):
    """End of a protected semantic block, never a byte offset or provider option."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    after: CacheAnchor
    exemplar_id: ID | None = Field(
        default=None, repr=False, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def exemplar_selector(self) -> CacheBreakpoint:
        if (self.after == "exemplar") != (self.exemplar_id is not None):
            raise ValueError("exactly exemplar breakpoints require exemplar_id")
        return self


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


def validate_openai_explicit_scope(scope: object) -> None:
    """Offline compatibility only. This check supplies no serving authority."""
    from judgmentllm.native_schema import NativeProjectionScopeV1

    if not isinstance(scope, NativeProjectionScopeV1) or (
        (scope.provider, scope.endpoint, scope.model) != OPENAI_EXPLICIT_ROUTE
        or scope.model_scope != "exact"
        or (scope.sdk_package, scope.sdk_version, scope.api_version)
        != ("openai", "3.42.1", None)
    ):
        raise ValueError("explicit caching requires exact Luna Responses SDK 3.42.1")
