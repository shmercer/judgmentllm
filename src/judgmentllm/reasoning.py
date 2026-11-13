"""Typed reasoning intent and safe, exact-scope control evidence.

Effort labels are provider controls, not a common measurement scale. Nothing in
these records authorizes provider access or retention of reasoning text.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from judgmentllm.models import ID, StrictModel

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
ReasoningMode = Literal["standard", "pro", "adaptive", "disabled", "between_tools"]


class ReasoningSpec(StrictModel):
    """Optional requested effort/mode; null means unspecified, not effort none."""

    model_config = ConfigDict(frozen=True)

    effort: ReasoningEffort | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    mode: ReasoningMode | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ReasoningCapabilities(StrictModel):
    """Exact documented/fixture scope, independent of live qualification.

    The enclosing GenerationCapabilities supplies provider, endpoint and model.
    Omitted defaults describe documentation; they never populate request intent.
    """

    model_config = ConfigDict(frozen=True)

    control_version: Literal[1] = 1
    sdk_package: ID
    sdk_version: ID
    api_version: ID
    native_mechanism: ID | None
    account_scope: ID | None
    requested_tier: str | None
    efforts: tuple[ReasoningEffort, ...]
    modes: tuple[ReasoningMode, ...]
    documented_default_effort: ReasoningEffort
    documented_default_mode: ReasoningMode | None
    api_effort_field: ID
    sdk_effort_field: ID
    sampling_efforts: tuple[ReasoningEffort, ...]
    restricted_modes: tuple[ReasoningMode, ...] = ()
    documented_on: Literal["2026-11-13"] = "2026-11-13"
    documentation_urls: tuple[str, ...]
    fixture_tested: Literal[True] = True
    live_qualified: Literal[False] = False

    @field_validator("control_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported reasoning control version")
        return value

    @model_validator(mode="after")
    def consistent(self) -> ReasoningCapabilities:
        for values in (
            self.efforts,
            self.modes,
            self.sampling_efforts,
            self.restricted_modes,
        ):
            if len(values) != len(set(values)):
                raise ValueError("duplicate reasoning capability")
        if (
            self.documented_default_effort not in self.efforts
            or not set(self.sampling_efforts) <= set(self.efforts)
            or not set(self.restricted_modes) <= set(self.modes)
            or (
                self.documented_default_mode is not None
                and self.documented_default_mode not in self.modes
            )
            or not self.documentation_urls
        ):
            raise ValueError("inconsistent reasoning capability")
        return self


class ReasoningPolicyV1(StrictModel):
    """Frozen request-control interpretation, containing no private reasoning."""

    model_config = ConfigDict(frozen=True)

    reasoning_policy_version: Literal[1] = 1
    provider: ID
    endpoint: ID
    model: ID
    requested: ReasoningSpec
    capabilities: ReasoningCapabilities

    @field_validator("reasoning_policy_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported reasoning policy")
        return value

    @model_validator(mode="after")
    def valid_intent(self) -> ReasoningPolicyV1:
        if not self.requested.model_dump() or control_errors(
            self.requested, self.capabilities
        ):
            raise ValueError("reasoning policy conflicts with requested controls")
        return self


class ReasoningQualificationScopeV1(StrictModel):
    """Reviewed controls and native features within an enclosing exact profile.

    No such scopes are installed by the reasoning-control implementation.
    Sampling omission and explicit values are separate qualifications.
    """

    model_config = ConfigDict(frozen=True)

    account_scope: ID
    tier: Literal["standard", "flex", "priority", "auto"]
    reasoning: ReasoningSpec
    temperature: int | float | None = Field(default=None, ge=0)
    top_p: int | float | None = Field(default=None, ge=0, le=1)
    features: tuple[ID, ...]

    @model_validator(mode="after")
    def nonempty(self) -> ReasoningQualificationScopeV1:
        if not self.reasoning.model_dump() or len(set(self.features)) != len(
            self.features
        ):
            raise ValueError("invalid reasoning qualification scope")
        return self


def control_errors(
    requested: ReasoningSpec | None,
    capabilities: ReasoningCapabilities,
    *,
    sampling: bool = False,
) -> tuple[str, ...]:
    """Return offending setting paths without provider/model/source content."""
    errors: list[str] = []
    effort = requested.effort if requested else None
    mode = requested.mode if requested else None
    if effort is not None and effort not in capabilities.efforts:
        errors.append("generation.reasoning.effort")
    if mode is not None and mode not in capabilities.modes:
        errors.append("generation.reasoning.mode")
    effective = effort or capabilities.documented_default_effort
    if mode in capabilities.restricted_modes and effective in ("xhigh", "max"):
        errors.append("generation.reasoning.mode")
    if sampling and effective not in capabilities.sampling_efforts:
        errors.append("generation.temperature/top_p")
    return tuple(errors)
