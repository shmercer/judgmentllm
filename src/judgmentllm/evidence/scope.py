"""Scoped provenance composed with existing generation capability facts.

These descriptors register no endpoint support and cannot select a transport.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from judgmentllm.generation import GenerationCapabilities
from judgmentllm.models import ID
from judgmentllm.persistence.models import SHA256

from ._base import EvidenceModel

EvidenceFeature = Literal[
    "usage", "property_order", "reasoning", "native_schema", "generation"
]
EvidenceStage = Literal["discovered", "implemented", "fixture_tested", "live_qualified"]
VersionIdentifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")]


class EvidenceProvenance(EvidenceModel):
    """Nontext provenance. Hashes identify docs/fixtures without embedding URLs."""

    kind: Literal["documentation", "offline_fixture", "independent_live"]
    source_sha256: SHA256
    recorded_on: date


class ScopedProviderEvidence(EvidenceModel):
    """Exact feature scope and separately stated tri-state implementation facts.

    Null is unknown, including implementation/fixture/live state. An explicit
    independent live record is necessary for any live qualification assertion.
    This is descriptive evidence supplied by callers, never inferred from SDKs,
    credentials, a successful offline fixture or a model identifier.
    """

    provider: ID
    endpoint: ID
    model: ID | None = None
    model_scope: Literal["exact", "unresolved"] = "unresolved"
    sdk_package: (
        Literal[
            "openai", "anthropic", "google-genai", "together", "ollama", "typesafe-sdk"
        ]
        | None
    ) = None
    sdk_version: VersionIdentifier | None = None
    api_version: VersionIdentifier | None = None
    feature: EvidenceFeature
    supported: bool | None = None
    stage: EvidenceStage = "discovered"
    implemented: bool | None = None
    fixture_tested: bool | None = None
    live_qualified: bool | None = None
    provenance: tuple[EvidenceProvenance, ...] = Field(min_length=1)
    generation: GenerationCapabilities | None = None

    @field_validator("generation", mode="before")
    @classmethod
    def validate_generation(cls, value: object) -> object:
        if isinstance(value, GenerationCapabilities):
            return GenerationCapabilities.model_validate(value.model_dump())
        return value

    @model_validator(mode="after")
    def consistent(self) -> ScopedProviderEvidence:
        if (self.model_scope == "exact") != (self.model is not None):
            raise ValueError("model scope differs")
        if (self.sdk_package is None) != (self.sdk_version is None):
            raise ValueError("SDK package and version must be paired")
        kinds = {source.kind for source in self.provenance}
        if self.fixture_tested is True and (
            self.implemented is not True or "offline_fixture" not in kinds
        ):
            raise ValueError("fixtures require implemented code and fixture provenance")
        if self.live_qualified is True and (
            self.implemented is not True
            or self.fixture_tested is not True
            or "independent_live" not in kinds
            or self.model is None
            or self.sdk_version is None
            or self.supported is not True
        ):
            raise ValueError(
                "live qualification requires independent exact-scope evidence"
            )
        actual_stage = (
            "live_qualified"
            if self.live_qualified is True
            else "fixture_tested"
            if self.fixture_tested is True
            else "implemented"
            if self.implemented is True
            else "discovered"
        )
        if self.stage != actual_stage:
            raise ValueError("evidence stage differs from established facts")
        if self.generation is not None:
            facts = self.generation
            if self.feature != "generation" or (
                self.provider,
                self.endpoint,
                self.model,
            ) != (facts.provider, facts.endpoint, facts.model):
                raise ValueError("generation capability scope differs")
            if facts.fixture_tested != (
                self.fixture_tested is True
            ) or facts.live_qualified != (self.live_qualified is True):
                raise ValueError("generation qualification differs")
        return self
