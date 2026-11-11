"""Reviewed exact-scope production authority, separate from descriptive evidence.

Only reviewed repository changes may add
qualified scopes; neither callers, credentials, stored receipts nor a live probe
can register a model. Qualification probes never write this registry.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from judgmentllm._auth import VertexAuthMode
from judgmentllm.evidence.scope import EvidenceProvenance
from judgmentllm.generation import GenerationCapabilities, GenerationParameterCapability
from judgmentllm.native_schema import NativeModel, NativeProjectionScopeV1
from judgmentllm.native_selection import NativeFeature
from judgmentllm.persistence.hashing import semantic_digest
from judgmentllm.persistence.models import SHA256

__all__ = ["_profile_for", "production_generation_capabilities"]

VERTEX_PRODUCTION_SDK_VERSION = "2.34.0"
VERTEX_API_VERSION = "v1beta1"
VERTEX_API_SCOPE = "vertex-generate-content-v1beta1-v1"
VertexMechanism = Literal[
    "vertex.generate_content.response_schema",
    "vertex.generate_content.response_json_schema",
]


class _QualifiedProfile(NativeModel):
    """Private reviewed authority with independent provenance for exact features."""

    profile_version: Literal[1] = 1
    scope: NativeProjectionScopeV1 = Field(repr=False)
    api_scope: Literal["vertex-generate-content-v1beta1-v1"] = (
        "vertex-generate-content-v1beta1-v1"
    )
    mechanism: VertexMechanism
    authentication_mode: Literal["adc", "api_key"]
    location: Literal["global"] = "global"
    route_sha256: SHA256
    plan_sha256: SHA256
    qualification_date: date
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    translator_version: Literal[2] = 2
    schema_dialect: Literal["vertex.cloud-schema.v1", "vertex.json-schema.v1"]
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
    generation: Literal[None] = None

    @field_validator("translator_version", mode="before")
    @classmethod
    def exact_translator_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("production translator version conflicts")
        return value

    @model_validator(mode="after")
    def exact_scope(self) -> _QualifiedProfile:
        if (
            self.scope.provider != "vertex"
            or self.scope.endpoint != "models.generate_content"
            or self.scope.model_scope != "exact"
            or self.scope.model != "gemini-3.1-flash-lite"
            or self.scope.sdk_package != "google-genai"
            or self.scope.sdk_version != VERTEX_PRODUCTION_SDK_VERSION
            or self.scope.api_version != VERTEX_API_VERSION
            or self.schema_dialect
            != (
                "vertex.cloud-schema.v1"
                if self.mechanism.endswith(".response_schema")
                else "vertex.json-schema.v1"
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
        return self

    @property
    def authority_sha256(self) -> str:
        return semantic_digest("vertex-reviewed-production-profile-v1", self)


# Historical byte provenance and independent audit are recorded in
# docs/vertex-live-qualification-acceptance.md. Route digests commit private
# project + authentication + location without retaining project identifiers.
_DOCUMENTATION_SHA256 = (
    "9b423e04ed0c7d9c569c4fe73b2dd5b3bc13380856a2bc53f49760d622cec8b7"
)
_OFFLINE_FIXTURE_SHA256 = (
    "918cd2571aef737985c658a7faa45d5b455d424119b2c1493711883c208f2fcb"
)
_SCOPE = NativeProjectionScopeV1(
    provider="vertex",
    endpoint="models.generate_content",
    model_scope="exact",
    model="gemini-3.1-flash-lite",
    sdk_package="google-genai",
    sdk_version="2.34.0",
    api_version="v1beta1",
)
_ACCEPTED_ON = date(2026, 11, 11)


def _provenance(report_sha256: str) -> tuple[EvidenceProvenance, ...]:
    return (
        EvidenceProvenance(
            kind="documentation",
            source_sha256=_DOCUMENTATION_SHA256,
            recorded_on=_ACCEPTED_ON,
        ),
        EvidenceProvenance(
            kind="offline_fixture",
            source_sha256=_OFFLINE_FIXTURE_SHA256,
            recorded_on=_ACCEPTED_ON,
        ),
        EvidenceProvenance(
            kind="independent_live",
            source_sha256=report_sha256,
            recorded_on=_ACCEPTED_ON,
        ),
    )


# Explicit reviewed repository authority only; no runtime registration hook.
# Neither record supplies authority to any other mode/project/region/route.
_QUALIFIED_PROFILES: tuple[_QualifiedProfile, ...] = (
    _QualifiedProfile(
        scope=_SCOPE,
        mechanism="vertex.generate_content.response_json_schema",
        authentication_mode="adc",
        route_sha256="079f2786c63e61bd84aacba6c6b11ba59c332101fb2e0827a5a685360cf611a4",
        plan_sha256="ff2fd19488f11524ee0aed165e588a3a13581d12127cc7f25a9ca5f7836fa5aa",
        qualification_date=_ACCEPTED_ON,
        source_commit="59a45ec31e53de7ed9eb833f5876c0000d982bca",
        schema_dialect="vertex.json-schema.v1",
        support_status="supported",
        request_acceptance="accepted",
        assessment_behavior="verified",
        native_schema_behavior="verified",
        supported_features=("rubric_scores", "pairwise_forced_choice"),
        unsupported_features=(),
        documentation_sha256=_DOCUMENTATION_SHA256,
        offline_fixture_sha256=_OFFLINE_FIXTURE_SHA256,
        independent_live_sha256="6843a1b81b0cca2bea08bcae6a5b1e5788fde8f557768f90272a8ecdfb3837ae",
        provenance=_provenance(
            "6843a1b81b0cca2bea08bcae6a5b1e5788fde8f557768f90272a8ecdfb3837ae"
        ),
    ),
    _QualifiedProfile(
        scope=_SCOPE,
        mechanism="vertex.generate_content.response_json_schema",
        authentication_mode="api_key",
        route_sha256="b57981a23b79e46d517cca532d8fea81cc0a9bb662e9843fd401fcb4ffec49e1",
        plan_sha256="6707041fa6b0eb2945dc82593b7d5e975a019d0655e81aee7a25f4fba6b776c7",
        qualification_date=_ACCEPTED_ON,
        source_commit="db98be97654d8e918516618399d57ec8c28e4ec6",
        schema_dialect="vertex.json-schema.v1",
        support_status="supported",
        request_acceptance="accepted",
        assessment_behavior="verified",
        native_schema_behavior="verified",
        supported_features=("rubric_scores", "pairwise_forced_choice"),
        unsupported_features=(),
        documentation_sha256=_DOCUMENTATION_SHA256,
        offline_fixture_sha256=_OFFLINE_FIXTURE_SHA256,
        independent_live_sha256="1a523c431b795e7e1d4b4ded26688f8d4cb9ea2746ed409f64b5331e5de84fff",
        provenance=_provenance(
            "1a523c431b795e7e1d4b4ded26688f8d4cb9ea2746ed409f64b5331e5de84fff"
        ),
    ),
)


def _profile_for(
    scope: NativeProjectionScopeV1,
    mechanism: str | None = None,
    *,
    route_sha256: str,
    authentication_mode: VertexAuthMode,
    location: str,
) -> _QualifiedProfile | None:
    """Admit complete route facts supplied by a checked production context."""
    matches = [
        profile
        for profile in _QUALIFIED_PROFILES
        if profile.scope == scope
        and profile.mechanism == mechanism
        and profile.route_sha256 == route_sha256
        and profile.authentication_mode == authentication_mode
        and profile.location == location
    ]
    if len(matches) > 1:
        raise ValueError("Vertex production qualification registry conflicts")
    return _QualifiedProfile.model_validate(dict(matches[0])) if matches else None


def production_generation_capabilities(
    scope: NativeProjectionScopeV1,
) -> GenerationCapabilities:
    """Documented mappings and known incompatibilities, with no live qualification.

    Unknown model limits remain unknown. These facts do not establish model
    eligibility, thinking, caching, tier or candidate support.
    """
    # Current serving documentation supersedes older sampling examples for
    # these exact named models. Ignored controls violate omission/value fidelity.
    sampling_ignored = scope.model in {
        "gemini-3.6-flash",
        "gemini-3.8-flash",
        "gemini-4-carbon",
        "gemini-4-argon",
    }
    sampling_documented = scope.model in {"gemini-2.5-flash", "gemini-2.5-pro"}
    sampling = False if sampling_ignored else True if sampling_documented else None
    return GenerationCapabilities(
        provider="vertex",
        endpoint="models.generate_content",
        model=scope.model or "unknown",
        temperature=GenerationParameterCapability(
            supported=sampling, minimum=0, maximum=2
        ),
        top_p=GenerationParameterCapability(supported=sampling, minimum=0, maximum=1),
        max_output_tokens=GenerationParameterCapability(supported=True, minimum=1),
        source="vertex.generate-content.v1beta1",
        fixture_tested=True,
        live_qualified=False,
    )
