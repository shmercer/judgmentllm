"""Typed generation capability facts; no provider catalog or request builders.

Facts apply only to their exact provider/endpoint/model scope. They are supplied
by callers today and by adapter qualification later, never inferred from IDs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, model_validator

from judgmentllm.models import ID, StrictModel

GenerationParameter = Literal["temperature", "top_p", "max_output_tokens"]


class GenerationParameterCapability(StrictModel):
    """Tri-state support and optional inclusive bounds for one control."""

    model_config = ConfigDict(frozen=True)

    supported: bool | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None

    @model_validator(mode="after")
    def ordered_bounds(self) -> GenerationParameterCapability:
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("minimum must not exceed maximum")
        return self


class GenerationCapabilities(StrictModel):
    """Frozen scoped facts, independently labeled qualification evidence.

    A combination lists controls that cannot all be explicitly requested
    together, regardless of their values. More detailed adapter restrictions
    belong to the adapter's preflight when that adapter is implemented.
    """

    model_config = ConfigDict(frozen=True)

    provider: ID
    endpoint: ID
    model: ID
    temperature: GenerationParameterCapability = GenerationParameterCapability()
    top_p: GenerationParameterCapability = GenerationParameterCapability()
    max_output_tokens: GenerationParameterCapability = GenerationParameterCapability()
    incompatible_combinations: tuple[tuple[GenerationParameter, ...], ...] = ()
    source: ID | None = None
    fixture_tested: bool = False
    live_qualified: bool = False

    @model_validator(mode="after")
    def distinct_combinations(self) -> GenerationCapabilities:
        for combination in self.incompatible_combinations:
            if len(combination) < 2 or len(set(combination)) != len(combination):
                raise ValueError(
                    "incompatible combinations require at least two distinct controls"
                )
        return self
