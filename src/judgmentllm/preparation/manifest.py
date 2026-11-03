"""Executable preparation commitments, distinct from generation-only fixtures."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from judgmentllm.config import ExecutionSpec, InputSpec, RunSpec
from judgmentllm.models import ID
from judgmentllm.persistence.generation import GenerationCommitment
from judgmentllm.persistence.hashing import semantic_digest
from judgmentllm.persistence.models import SHA256, ArtifactIdentity, ExecutionModel
from judgmentllm.planning import CapabilityProfile
from judgmentllm.preparation.freezing import FileSignature
from judgmentllm.preparation.index import ParsingContext

SourceFormat = Literal["yaml", "yml", "json", "csv", "jsonl", "text", "txt"]


class SourceCommitment(ExecutionModel):
    """Original file provenance, separately committed parsing/index context."""

    source_id: ID
    role: Literal[
        "configuration",
        "rubric",
        "criteria",
        "exemplars",
        "template",
        "response",
        "pair",
    ]
    ordinal: int = Field(ge=0)
    original_location: str
    raw_sha256: SHA256
    byte_length: int = Field(ge=0)
    canonical_sha256: SHA256 | None = None
    generation: FileSignature
    format: SourceFormat
    snapshot: ArtifactIdentity | None = None
    parsing: ParsingContext | None = None
    record_count: int = Field(default=0, ge=0)
    index_sha256: SHA256 | None = None

    @model_validator(mode="after")
    def check_snapshot(self) -> SourceCommitment:
        if self.snapshot is not None and (
            self.snapshot.sha256 != self.raw_sha256
            or self.snapshot.byte_length != self.byte_length
            or self.snapshot.relative_path is None
        ):
            raise ValueError("snapshot must retain the exact original bytes")
        return self


class CategoryCommitment(ExecutionModel):
    """Ordered category identities/coding without retaining assessment text."""

    label_sha256: SHA256
    numeric_value: float


class TraitCommitment(ExecutionModel):
    trait_id: ID
    sha256: SHA256
    coding_kind: Literal["explicit", "numeric", "indices"] | None = None
    categories: tuple[CategoryCommitment, ...] = ()


class ExemplarCommitment(ExecutionModel):
    exemplar_id: ID
    sha256: SHA256


class TemplateCommitment(ExecutionModel):
    sha256: SHA256
    preset: str | None
    advanced: bool
    limits_sha256: SHA256
    renderer_version: Literal[1] = 1


class ExecutableRunManifest(ExecutionModel):
    """Complete executable *input* foundation; contains no provider payloads."""

    kind: Literal["executable_run_manifest"] = "executable_run_manifest"
    run_id: ID
    task_kind: Literal["rubric", "pairwise"]
    capture: Literal["full", "minimal"]
    configuration_origin: Literal["file", "python"]
    configuration_location: str | None
    configuration_sha256: SHA256
    assessment_sha256: SHA256
    specification: ArtifactIdentity
    sources: tuple[SourceCommitment, ...]
    input: InputSpec
    pairs: InputSpec | None
    traits: tuple[TraitCommitment, ...]
    exemplars: tuple[ExemplarCommitment, ...]
    template: TemplateCommitment
    execution: ExecutionSpec
    trait_groups: tuple[tuple[str, ...], ...]
    seed: int | None
    allow_ties: bool
    strict_capabilities: bool
    generation: GenerationCommitment
    capabilities: CapabilityProfile | None
    recovery_policy: Literal["explicit_verified_snapshot", "unchanged_originals"]
    package_version: str
    assessment_schema_version: Literal[1] = 1
    canonicalization_version: Literal[1] = 1
    serializer_version: Literal[1] = 1
    index_version: Literal[1] = 1
    request_identity_version: Literal[1] = 1

    @model_validator(mode="after")
    def complete(self) -> ExecutableRunManifest:
        if (self.configuration_origin == "file") != (
            self.configuration_location is not None
        ):
            raise ValueError("configuration origin is inconsistent")
        if self.capture == "full":
            if self.specification.relative_path is None or any(
                source.snapshot is None for source in self.sources
            ):
                raise ValueError("full capture requires all retained snapshots")
            if self.recovery_policy != "explicit_verified_snapshot":
                raise ValueError("capture/recovery policy mismatch")
        elif (
            self.specification.relative_path is not None
            or any(source.snapshot is not None for source in self.sources)
            or self.recovery_policy != "unchanged_originals"
        ):
            raise ValueError("minimal capture must not retain assessment snapshots")
        if len({s.source_id for s in self.sources}) != len(self.sources):
            raise ValueError("duplicate source identity")
        if sum(s.role == "response" for s in self.sources) != 1:
            raise ValueError("complete manifest requires response input")
        if sum(s.role == "pair" for s in self.sources) != (
            1 if self.task_kind == "pairwise" else 0
        ):
            raise ValueError("complete manifest has inconsistent pair input")
        if not self.traits or not self.trait_groups:
            raise ValueError(
                "complete manifest requires assessment/grouping commitments"
            )
        return self


class SourceDiscrepancy(ExecutionModel):
    source_id: ID
    reason: Literal["missing_or_unreadable", "generation_or_content_changed"]
    expected_sha256: SHA256


class FrozenSnapshotAuthorization(ExecutionModel):
    """Explicit, operation-scoped decision; retained evidence never grants reuse."""

    kind: Literal["frozen_snapshot_authorization"] = "frozen_snapshot_authorization"
    operation_id: ID
    run_id: ID
    manifest_sha256: SHA256
    explicit_caller_decision: Literal[True] = True
    scope: Literal["this_prepared_run_session"] = "this_prepared_run_session"
    discrepancies: tuple[SourceDiscrepancy, ...]
    occurred_at: str


def configuration_digest(spec: RunSpec) -> str:
    return semantic_digest("run-configuration", spec)


def assessment_digest(spec: RunSpec) -> str:
    return semantic_digest(
        "assessment-context",
        {
            "rubric": spec.rubric.model_dump(mode="json") if spec.rubric else None,
            "criteria": [c.model_dump(mode="json") for c in spec.criteria],
            "exemplars": [e.model_dump(mode="json") for e in spec.exemplars],
        },
    )
