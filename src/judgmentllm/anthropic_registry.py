"""Reviewed exact-scope production authority, separate from descriptive evidence.

The registry deliberately starts empty. Only reviewed repository changes may add
qualified scopes; neither callers, credentials, stored receipts nor a live probe
can register a model. Qualification probes never write this registry.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from judgmentllm.evidence.scope import EvidenceProvenance
from judgmentllm.generation import GenerationCapabilities
from judgmentllm.native_schema import NativeModel, NativeProjectionScopeV1
from judgmentllm.native_selection import NativeFeature
from judgmentllm.persistence.hashing import semantic_digest
from judgmentllm.persistence.models import SHA256
from judgmentllm.service_tiers import ServiceQualificationScope

__all__ = ["_profile_for", "production_generation_capabilities"]

ANTHROPIC_PRODUCTION_SDK_VERSION = "2.3.0"
ANTHROPIC_API_SCOPE = "anthropic-messages-2026-10-21-v1"


class _QualifiedProfile(NativeModel):
    """Private reviewed authority with independent provenance for exact features."""

    profile_version: Literal[1] = 1
    scope: NativeProjectionScopeV1 = Field(repr=False)
    api_scope: Literal["anthropic-messages-2026-10-21-v1"] = (
        "anthropic-messages-2026-10-21-v1"
    )
    qualification_date: date
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    translator_version: Literal[2] = 2
    schema_dialect: Literal["anthropic.json-schema.v1"] = "anthropic.json-schema.v1"
    support_status: Literal["supported", "unsupported", "unknown"]
    request_acceptance: Literal["accepted", "rejected", "unknown"]
    assessment_behavior: Literal["verified", "failed", "unknown"]
    native_schema_behavior: Literal["verified", "unsupported", "unknown"]
    provenance: tuple[EvidenceProvenance, ...] = Field(min_length=3)
    supported_features: tuple[NativeFeature, ...] = ()
    unsupported_features: tuple[NativeFeature, ...] = ()
    documentation_sha256: SHA256
    offline_fixture_sha256: SHA256
    independent_live_sha256: SHA256
    generation: GenerationCapabilities | None = None
    service_scopes: tuple[ServiceQualificationScope, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @field_validator("translator_version", mode="before")
    @classmethod
    def exact_translator_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("production translator version conflicts")
        return value

    @model_validator(mode="after")
    def exact_scope(self) -> _QualifiedProfile:
        if len(
            {(scope.account_scope, scope.tier) for scope in self.service_scopes}
        ) != len(self.service_scopes):
            raise ValueError("duplicate service qualification scope")
        if (
            self.scope.provider != "anthropic"
            or self.scope.endpoint != "messages"
            or self.scope.model_scope != "exact"
            or self.scope.model is None
            or self.scope.model.startswith("judgmentllm-synthetic-")
            or self.scope.sdk_package != "anthropic"
            or self.scope.sdk_version != ANTHROPIC_PRODUCTION_SDK_VERSION
            or self.scope.api_version != "2026-10-21"
            or len(set(self.supported_features)) != len(self.supported_features)
            or len(set(self.unsupported_features)) != len(self.unsupported_features)
            or set(self.supported_features) & set(self.unsupported_features)
        ):
            raise ValueError("production profile scope conflicts")
        sources = {(source.kind, source.source_sha256) for source in self.provenance}
        if (
            not {
                ("documentation", self.documentation_sha256),
                ("offline_fixture", self.offline_fixture_sha256),
                ("independent_live", self.independent_live_sha256),
            }
            <= sources
            or any(
                source.recorded_on > self.qualification_date
                for source in self.provenance
            )
            or (self.supported_features and self.support_status != "supported")
            or (
                self.support_status == "supported"
                and (
                    self.request_acceptance != "accepted"
                    or self.assessment_behavior != "verified"
                    or self.native_schema_behavior != "verified"
                )
            )
        ):
            raise ValueError("production qualification conclusions conflict")
        if self.generation is not None and (
            (
                self.generation.provider,
                self.generation.endpoint,
                self.generation.model,
            )
            != (self.scope.provider, self.scope.endpoint, self.scope.model)
            or not self.generation.fixture_tested
            or not self.generation.live_qualified
        ):
            raise ValueError("generation qualification scope conflicts")
        return self

    @property
    def authority_sha256(self) -> str:
        return semantic_digest("anthropic-reviewed-production-profile-v1", self)


# No publicly callable registration/import hook. The initial implementation has
# no serving-model qualification, including models visible through models.list.
_QUALIFIED_PROFILES: tuple[_QualifiedProfile, ...] = ()


def _profile_for(scope: NativeProjectionScopeV1) -> _QualifiedProfile | None:
    matches = [profile for profile in _QUALIFIED_PROFILES if profile.scope == scope]
    if len(matches) > 1:
        raise ValueError("production qualification registry conflicts")
    return _QualifiedProfile.model_validate(dict(matches[0])) if matches else None


def production_generation_capabilities(
    scope: NativeProjectionScopeV1,
) -> GenerationCapabilities | None:
    """Return only reviewed facts; an unknown model remains unknown."""
    from judgmentllm.generation import GenerationParameterCapability

    # Endpoint documentation establishes request syntax, not live model support.
    # Unknown IDs remain available in text mode with unknown model limits.
    known_limit = 128_000 if scope.model == "claude-sonnet-4-6" else None
    return GenerationCapabilities(
        provider="anthropic",
        endpoint="messages",
        model=scope.model or "unknown",
        temperature=GenerationParameterCapability(supported=False),
        top_p=GenerationParameterCapability(supported=False),
        max_output_tokens=GenerationParameterCapability(
            supported=True, minimum=1, maximum=known_limit
        ),
        source="anthropic.messages.api-2026-10-21",
        fixture_tested=True,
        live_qualified=False,
    )
