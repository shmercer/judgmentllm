"""Typed service intent and observation; no credentials or qualification grants.

Absent policy records mean legacy behavior. Only new preparations create V2;
receipts and subset retries reconstruct their original version instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from pydantic import Field, field_validator, model_validator

from judgmentllm.models import ID
from judgmentllm.native_schema import NativeModel
from judgmentllm.native_selection import NativeFeature

if TYPE_CHECKING:
    from judgmentllm.config import RunSpecV3

ServicePolicyVersion = Literal[1, 2]
ServiceClass = Literal["standard", "flex", "priority", "auto"]

_TIERS_V2: dict[str, dict[str, ServiceClass]] = {
    "openai": {
        "default": "standard",
        "auto": "auto",
        "flex": "flex",
        "fast": "priority",
        "priority": "priority",
    },
    "gemini": {"standard": "standard", "flex": "flex", "priority": "priority"},
    "anthropic": {"standard_only": "standard", "auto": "auto"},
}
_DEFAULTS_V2 = {"openai": "default", "gemini": "standard", "anthropic": "standard_only"}


class ServiceQualificationScope(NativeModel):
    """Exact declared account/project binding and tier, within a model profile.

    An opaque account scope is descriptive, never proof of authentication. Only
    independently reviewed live evidence can establish a matching profile.
    """

    service_policy_version: Literal[2] = 2
    account_scope: ID
    tier: ServiceClass
    features: tuple[NativeFeature, ...] = ()

    @field_validator("features")
    @classmethod
    def distinct_features(
        cls, value: tuple[NativeFeature, ...]
    ) -> tuple[NativeFeature, ...]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate service qualification feature")
        return value

    @field_validator("service_policy_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("unsupported service policy")
        return value


class ServicePolicyV2(NativeModel):
    """Immutable adapter-owned wire intent and locally enforced I/O bounds."""

    service_policy_version: Literal[2] = 2
    provider: Literal["openai", "gemini", "anthropic"]
    requested_tier: str | None
    wire_tier: str
    account_scope: ID | None
    read_timeout_seconds: Literal[60, 930]
    operation_deadline_seconds: Literal[90, 960]
    server_timeout_hint_seconds: Literal[900] | None = None

    @field_validator("service_policy_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("unsupported service policy")
        return value

    @property
    def tier(self) -> ServiceClass:
        return _TIERS_V2[self.provider][self.wire_tier]

    @property
    def qualification_scope(self) -> ServiceQualificationScope | None:
        return (
            ServiceQualificationScope(account_scope=self.account_scope, tier=self.tier)
            if self.account_scope is not None
            else None
        )

    @field_validator(
        "read_timeout_seconds",
        "operation_deadline_seconds",
        "server_timeout_hint_seconds",
        mode="before",
    )
    @classmethod
    def strict_deadline(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("service deadlines must be exact integers")
        return value

    @model_validator(mode="after")
    def exact_policy(self) -> ServicePolicyV2:
        wire = (
            _DEFAULTS_V2[self.provider]
            if self.requested_tier is None
            else self.requested_tier
        )
        if wire not in _TIERS_V2[self.provider] or self.wire_tier != wire:
            raise ValueError("unsupported service tier")
        flex = self.tier == "flex"
        if (
            self.read_timeout_seconds != (930 if flex else 60)
            or self.operation_deadline_seconds != (960 if flex else 90)
            or self.server_timeout_hint_seconds
            != (900 if flex and self.provider == "gemini" else None)
        ):
            raise ValueError("service timeout policy differs")
        return self


def service_policy(
    spec: RunSpecV3, version: ServicePolicyVersion = 2
) -> ServicePolicyV2 | None:
    """Resolve explicit standard defaults only for new provider preparations."""
    if type(version) is not int or version not in (1, 2):
        raise ValueError("unsupported service policy")
    if version == 1:
        if spec.provider in {"openai", "gemini"} and spec.execution.tier is not None:
            raise ValueError("legacy provider requests do not support explicit tiers")
        if spec.account_scope is not None:
            raise ValueError("legacy requests cannot acquire an account scope")
        return None
    if spec.provider not in _TIERS_V2:
        raise ValueError("unsupported service provider")
    wire = (
        _DEFAULTS_V2[spec.provider]
        if spec.execution.tier is None
        else spec.execution.tier
    )
    flex = wire == "flex"
    return ServicePolicyV2.model_validate(
        {
            "provider": spec.provider,
            "requested_tier": spec.execution.tier,
            "wire_tier": wire,
            "account_scope": spec.account_scope,
            "read_timeout_seconds": 930 if flex else 60,
            "operation_deadline_seconds": 960 if flex else 90,
            "server_timeout_hint_seconds": 900
            if flex and spec.provider == "gemini"
            else None,
        }
    )


def qualified_service(
    policy: ServicePolicyV2 | None,
    scopes: tuple[ServiceQualificationScope, ...],
    features: tuple[NativeFeature, ...] = (),
) -> bool:
    """Legacy model evidence never authorizes a new account/tier combination."""
    return policy is None or any(
        scope.account_scope == policy.account_scope
        and scope.tier == policy.tier
        and set(features) <= set(scope.features)
        for scope in scopes
    )


def validate_service_support(
    policy: ServicePolicyV2 | None,
    scopes: tuple[ServiceQualificationScope, ...],
    *,
    strict: bool,
    qualification: bool,
) -> None:
    """Unknown tier entitlement cannot pass strict ordinary submission preflight."""
    if (
        policy is not None
        and policy.tier != "standard"
        and strict
        and not qualification
        and not qualified_service(policy, scopes)
    ):
        raise ValueError("execution.tier requires reviewed exact-account/tier evidence")


class ServiceTierObservation(NativeModel):
    """Safe metadata once per physical observation, separate from assessment validity."""

    policy: ServicePolicyV2
    reported_tier: (
        Literal["default", "standard", "flex", "priority", "fast", "scale", "ultrafast"]
        | None
    )
    source: Literal[
        "openai.service_tier",
        "anthropic.usage.service_tier",
        "gemini.x-gemini-service-tier",
    ]
    outcome: Literal["matched", "allocated", "downgraded", "unknown", "unexpected"]
    latency_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def consistent(self) -> ServiceTierObservation:
        if self.source != {
            "openai": "openai.service_tier",
            "gemini": "gemini.x-gemini-service-tier",
            "anthropic": "anthropic.usage.service_tier",
        }[self.policy.provider] or self.outcome != _tier_outcome_v2(
            self.policy, self.reported_tier
        ):
            raise ValueError("service observation differs")
        return self


def _tier_outcome_v2(policy: ServicePolicyV2, reported: str | None) -> str:
    """Frozen V2 interpretation; new semantics require a separate policy version."""
    if reported is None:
        return "unknown"
    allowed = {
        "openai": {"default", "flex", "priority", "fast", "scale", "ultrafast"},
        "gemini": {"standard", "flex", "priority"},
        "anthropic": {"standard", "priority"},
    }[policy.provider]
    if reported not in allowed:
        return "unexpected"
    normalized = (
        "standard"
        if policy.provider == "openai" and reported == "default"
        else reported
    )
    if policy.provider == "openai" and normalized == "fast":
        normalized = "priority"
    if policy.tier == normalized:
        return "matched"
    if policy.tier == "auto" and (
        policy.provider == "openai" or normalized in {"standard", "priority"}
    ):
        return "allocated"
    # Both providers document premium traffic shedding to standard. Flex never upgrades.
    if (
        policy.provider in {"openai", "gemini"}
        and policy.tier == "priority"
        and normalized == "standard"
    ):
        return "downgraded"
    return "unexpected"


def observe_service_tier(
    policy: ServicePolicyV2, reported: object, *, latency_seconds: float | None = None
) -> ServiceTierObservation:
    """Whitelist provider values before metadata retention; unknown text is withheld."""
    value = (
        reported
        if type(reported) is str
        and reported
        in {"default", "standard", "flex", "priority", "fast", "scale", "ultrafast"}
        else None
    )
    return ServiceTierObservation.model_validate(
        {
            "policy": policy,
            "reported_tier": value,
            "source": {
                "openai": "openai.service_tier",
                "gemini": "gemini.x-gemini-service-tier",
                "anthropic": "anthropic.usage.service_tier",
            }[policy.provider],
            "outcome": _tier_outcome_v2(policy, value),
            "latency_seconds": latency_seconds,
        }
    )


def terminal_rate_rejection(provider: str, raw: bytes | None) -> bool:
    """A 429 alone cannot establish nonacceptance for a new service policy."""
    from judgmentllm.output_contracts import decode_assessment_output

    try:
        document = decode_assessment_output(raw) if raw is not None else None
        if not isinstance(document, dict):
            return False
        value = cast(dict[str, object], document).get("error")
        if not isinstance(value, dict):
            return False
        error = cast(dict[str, object], value)
        if provider == "openai":
            return error.get("type") in (
                "rate_limit_error",
                "insufficient_quota",
                "resource_unavailable",
            ) or error.get("code") in (
                "rate_limit_exceeded",
                "insufficient_quota",
                "resource_unavailable",
            )
        if provider == "anthropic":
            return error.get("type") == "rate_limit_error"
        return (
            provider == "gemini"
            and type(error.get("code")) is int
            and error.get("code") == 429
            and error.get("status") == "RESOURCE_EXHAUSTED"
        )
    except (ValueError, TypeError, RecursionError):
        return False
