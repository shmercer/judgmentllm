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

__all__ = ["_profile_for", "production_generation_capabilities"]

GEMINI_PRODUCTION_SDK_VERSION = "2.34.0"
GEMINI_API_VERSION = "v1beta"
GEMINI_API_SCOPE = "gemini-developer-generate-content-v1beta-v1"
GeminiMechanism = Literal[
    "gemini.generate_content.response_schema",
    "gemini.generate_content.response_json_schema",
]


class _QualifiedProfile(NativeModel):
    """Private reviewed authority with independent provenance for exact features."""

    profile_version: Literal[1] = 1
    scope: NativeProjectionScopeV1 = Field(repr=False)
    api_scope: Literal["gemini-developer-generate-content-v1beta-v1"] = (
        "gemini-developer-generate-content-v1beta-v1"
    )
    mechanism: GeminiMechanism
    qualification_date: date
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    translator_version: Literal[2] = 2
    schema_dialect: Literal["gemini.cloud-schema.v1", "gemini.json-schema.v1"]
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

    @field_validator("translator_version", mode="before")
    @classmethod
    def exact_translator_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("production translator version conflicts")
        return value

    @model_validator(mode="after")
    def exact_scope(self) -> _QualifiedProfile:
        if (
            self.scope.provider != "gemini"
            or self.scope.endpoint != "models.generate_content"
            or self.scope.model_scope != "exact"
            or self.scope.model is None
            or self.scope.model.startswith("judgmentllm-synthetic-")
            or self.scope.sdk_package != "google-genai"
            or self.scope.sdk_version != GEMINI_PRODUCTION_SDK_VERSION
            or self.scope.api_version != GEMINI_API_VERSION
            or self.schema_dialect
            != (
                "gemini.cloud-schema.v1"
                if self.mechanism.endswith(".response_schema")
                else "gemini.json-schema.v1"
            )
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
        return semantic_digest("gemini-reviewed-production-profile-v1", self)


# No publicly callable registration/import hook. The initial implementation has
# no serving-model qualification, including models visible through models.list.
_QUALIFIED_PROFILES: tuple[_QualifiedProfile, ...] = ()


def _profile_for(
    scope: NativeProjectionScopeV1, mechanism: str | None = None
) -> _QualifiedProfile | None:
    matches = [
        profile
        for profile in _QUALIFIED_PROFILES
        if profile.scope == scope and profile.mechanism == mechanism
    ]
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
    known_limit = 65_536 if scope.model == "gemini-3.1-flash-lite" else None
    return GenerationCapabilities(
        provider="gemini",
        endpoint="models.generate_content",
        model=scope.model or "unknown",
        temperature=GenerationParameterCapability(supported=True, minimum=0, maximum=2),
        top_p=GenerationParameterCapability(supported=True, minimum=0, maximum=1),
        max_output_tokens=GenerationParameterCapability(
            supported=True, minimum=1, maximum=known_limit
        ),
        source="gemini.developer.generate-content.v1beta",
        fixture_tested=True,
        live_qualified=False,
    )
