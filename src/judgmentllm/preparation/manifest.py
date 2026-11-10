"""Executable preparation commitments, distinct from generation-only fixtures."""

from __future__ import annotations

from typing import Any, Literal, Self, cast

from pydantic import (
    Field,
    ModelWrapValidatorHandler,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)

from judgmentllm.config import ExecutionSpec, InputSpec, RunSpecification
from judgmentllm.models import ID
from judgmentllm.output_commitments import SchemaSelectionV2
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


def configuration_digest(spec: RunSpecification) -> str:
    return semantic_digest("run-configuration", spec)


def assessment_digest(spec: RunSpecification) -> str:
    return semantic_digest(
        "assessment-context",
        {
            "rubric": spec.rubric.model_dump(mode="json") if spec.rubric else None,
            "criteria": [c.model_dump(mode="json") for c in spec.criteria],
            "exemplars": [e.model_dump(mode="json") for e in spec.exemplars],
        },
    )


class TemplateCommitmentV2(TemplateCommitment):
    """Explicit v2 renderer identity; historical templates retain version 1."""

    renderer_version: Literal[2] = 2  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("renderer_version", mode="before")
    @classmethod
    def strict_renderer_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("renderer_version must be integer 2")
        return value


class ExecutableRunManifestV2(ExecutableRunManifest):
    """V2 run commitments, using the unchanged registry and source contracts."""

    schema_version: Literal[2] = 2  # pyright: ignore[reportIncompatibleVariableOverride]
    run_spec_version: Literal[2] = 2
    output_contract_version: Literal[2] = 2
    request_identity_version: Literal[2] = 2  # pyright: ignore[reportIncompatibleVariableOverride]
    template: TemplateCommitmentV2  # pyright: ignore[reportIncompatibleVariableOverride]
    output_selection: SchemaSelectionV2

    @field_validator(
        "schema_version",
        "run_spec_version",
        "output_contract_version",
        "request_identity_version",
        mode="before",
    )
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("schema_version must be the supported integer 2")
        return value


class TemplateCommitmentV3(TemplateCommitment):
    """Native-capable renderer; independent of neutral output contract version."""

    renderer_version: Literal[3] = 3  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("renderer_version", mode="before")
    @classmethod
    def strict_renderer_version(cls, value: object) -> object:
        if type(value) is not int or value != 3:
            raise ValueError("renderer_version must be integer 3")
        return value


class ExecutableRunManifestV3(ExecutableRunManifest):
    """Additive native transport boundary; no historical run-level selection."""

    schema_version: Literal[3] = 3  # pyright: ignore[reportIncompatibleVariableOverride]
    run_spec_version: Literal[3] = 3
    output_contract_version: Literal[2] = 2
    output_commitment_version: Literal[3] = 3
    transport_commitment_version: Literal[1] = 1
    request_identity_version: Literal[3] = 3  # pyright: ignore[reportIncompatibleVariableOverride]
    template: TemplateCommitmentV3  # pyright: ignore[reportIncompatibleVariableOverride]
    transport_context_sha256: SHA256
    initial_transport_count: int = Field(ge=0)
    initial_transport_sha256: SHA256

    @model_validator(mode="wrap")
    @classmethod
    def safe_manifest(
        cls,
        value: object,
        handler: ModelWrapValidatorHandler[Self],
        info: ValidationInfo,
    ) -> Self:
        from judgmentllm.evidence._base import _invalid_evidence  # pyright: ignore[reportPrivateUsage]

        try:
            if info.mode == "json" and isinstance(value, dict):
                import json

                values = cast(dict[str, object], value)
                value = {
                    key: TypeAdapter(
                        cls.model_fields[key].annotation or Any
                    ).validate_json(json.dumps(item), strict=True)
                    if key in cls.model_fields and not key.endswith("_version")
                    else item
                    for key, item in values.items()
                }
            return handler(value)
        except Exception:
            raise _invalid_evidence() from None

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, **kwargs: Any
    ) -> Self:
        from judgmentllm.evidence._base import _invalid_evidence  # pyright: ignore[reportPrivateUsage]
        from judgmentllm.output_contracts import decode_assessment_output

        try:
            if type(json_data) is bytearray:
                raw: str | bytes = bytes(json_data)
            elif type(json_data) in (str, bytes):
                raw = cast(str | bytes, json_data)
            else:
                raise ValueError("invalid JSON input")
            decode_assessment_output(raw)
            return super().model_validate_json(raw, **kwargs)
        except Exception:
            raise _invalid_evidence() from None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int or value != 3:
            raise ValueError("schema_version must be the supported integer 3")
        return value

    @field_validator(
        "schema_version",
        "run_spec_version",
        "output_contract_version",
        "output_commitment_version",
        "transport_commitment_version",
        "request_identity_version",
        "assessment_schema_version",
        "canonicalization_version",
        "serializer_version",
        "index_version",
        mode="before",
    )
    @classmethod
    def strict_version_tokens(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("manifest versions must be integers")
        return value


class ExecutableRunManifestV4(ExecutableRunManifest):
    """Production OpenAI boundary; never a synthetic V3 manifest."""

    schema_version: Literal[4] = 4  # pyright: ignore[reportIncompatibleVariableOverride]
    transport_protocol: Literal["openai-production-v1"] = "openai-production-v1"
    run_spec_version: Literal[3] = 3
    output_contract_version: Literal[2] = 2
    output_commitment_version: Literal[4] = 4
    transport_commitment_version: Literal[2] = 2
    request_identity_version: Literal[3] = 3  # pyright: ignore[reportIncompatibleVariableOverride]
    template: TemplateCommitmentV3  # pyright: ignore[reportIncompatibleVariableOverride]
    transport_context_sha256: SHA256
    initial_transport_count: int = Field(ge=0)
    initial_transport_sha256: SHA256

    @model_validator(mode="wrap")
    @classmethod
    def safe_manifest(
        cls,
        value: object,
        handler: ModelWrapValidatorHandler[Self],
        info: ValidationInfo,
    ) -> Self:
        from judgmentllm.evidence._base import _invalid_evidence  # pyright: ignore[reportPrivateUsage]

        try:
            if info.mode == "json" and isinstance(value, dict):
                import json

                values = cast(dict[str, object], value)
                value = {
                    key: TypeAdapter(
                        cls.model_fields[key].annotation or Any
                    ).validate_json(json.dumps(item), strict=True)
                    if key in cls.model_fields and not key.endswith("_version")
                    else item
                    for key, item in values.items()
                }
            return handler(value)
        except Exception:
            raise _invalid_evidence() from None

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, **kwargs: Any
    ) -> Self:
        from judgmentllm.evidence._base import _invalid_evidence  # pyright: ignore[reportPrivateUsage]
        from judgmentllm.output_contracts import decode_assessment_output

        try:
            if type(json_data) is bytearray:
                raw: str | bytes = bytes(json_data)
            elif type(json_data) in (str, bytes):
                raw = cast(str | bytes, json_data)
            else:
                raise ValueError("invalid JSON input")
            decode_assessment_output(raw)
            return super().model_validate_json(raw, **kwargs)
        except Exception:
            raise _invalid_evidence() from None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int or value != 4:
            raise ValueError("schema_version must be the supported integer 4")
        return value

    @field_validator(
        "schema_version",
        "run_spec_version",
        "output_contract_version",
        "output_commitment_version",
        "transport_commitment_version",
        "request_identity_version",
        "assessment_schema_version",
        "canonicalization_version",
        "serializer_version",
        "index_version",
        mode="before",
    )
    @classmethod
    def strict_version_tokens(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("manifest versions must be integers")
        return value


class ExecutableRunManifestV5(ExecutableRunManifest):
    """Production Anthropic boundary; never a synthetic V3 manifest."""

    schema_version: Literal[5] = 5  # pyright: ignore[reportIncompatibleVariableOverride]
    transport_protocol: Literal["anthropic-production-v1"] = "anthropic-production-v1"
    run_spec_version: Literal[3] = 3
    output_contract_version: Literal[2] = 2
    output_commitment_version: Literal[5] = 5
    transport_commitment_version: Literal[3] = 3
    request_identity_version: Literal[3] = 3  # pyright: ignore[reportIncompatibleVariableOverride]
    template: TemplateCommitmentV3  # pyright: ignore[reportIncompatibleVariableOverride]
    transport_context_sha256: SHA256
    initial_transport_count: int = Field(ge=0)
    initial_transport_sha256: SHA256

    @model_validator(mode="wrap")
    @classmethod
    def safe_manifest(
        cls,
        value: object,
        handler: ModelWrapValidatorHandler[Self],
        info: ValidationInfo,
    ) -> Self:
        from judgmentllm.evidence._base import _invalid_evidence  # pyright: ignore[reportPrivateUsage]

        try:
            if info.mode == "json" and isinstance(value, dict):
                import json

                values = cast(dict[str, object], value)
                value = {
                    key: TypeAdapter(
                        cls.model_fields[key].annotation or Any
                    ).validate_json(json.dumps(item), strict=True)
                    if key in cls.model_fields and not key.endswith("_version")
                    else item
                    for key, item in values.items()
                }
            return handler(value)
        except Exception:
            raise _invalid_evidence() from None

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, **kwargs: Any
    ) -> Self:
        from judgmentllm.evidence._base import _invalid_evidence  # pyright: ignore[reportPrivateUsage]
        from judgmentllm.output_contracts import decode_assessment_output

        try:
            if type(json_data) is bytearray:
                raw: str | bytes = bytes(json_data)
            elif type(json_data) in (str, bytes):
                raw = cast(str | bytes, json_data)
            else:
                raise ValueError("invalid JSON input")
            decode_assessment_output(raw)
            return super().model_validate_json(raw, **kwargs)
        except Exception:
            raise _invalid_evidence() from None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int or value != 5:
            raise ValueError("schema_version must be the supported integer 5")
        return value

    @field_validator(
        "schema_version",
        "run_spec_version",
        "output_contract_version",
        "output_commitment_version",
        "transport_commitment_version",
        "request_identity_version",
        "assessment_schema_version",
        "canonicalization_version",
        "serializer_version",
        "index_version",
        mode="before",
    )
    @classmethod
    def strict_version_tokens(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("manifest versions must be integers")
        return value


class ExecutableRunManifestV6(ExecutableRunManifest):
    """Production Gemini Developer boundary; never a synthetic V3 manifest."""

    schema_version: Literal[6] = 6  # pyright: ignore[reportIncompatibleVariableOverride]
    transport_protocol: Literal["gemini-production-v1"] = "gemini-production-v1"
    run_spec_version: Literal[3] = 3
    output_contract_version: Literal[2] = 2
    output_commitment_version: Literal[6] = 6
    transport_commitment_version: Literal[4] = 4
    request_identity_version: Literal[3] = 3  # pyright: ignore[reportIncompatibleVariableOverride]
    template: TemplateCommitmentV3  # pyright: ignore[reportIncompatibleVariableOverride]
    transport_context_sha256: SHA256
    initial_transport_count: int = Field(ge=0)
    initial_transport_sha256: SHA256

    @model_validator(mode="wrap")
    @classmethod
    def safe_manifest(
        cls,
        value: object,
        handler: ModelWrapValidatorHandler[Self],
        info: ValidationInfo,
    ) -> Self:
        from judgmentllm.evidence._base import _invalid_evidence  # pyright: ignore[reportPrivateUsage]

        try:
            if info.mode == "json" and isinstance(value, dict):
                import json

                values = cast(dict[str, object], value)
                value = {
                    key: TypeAdapter(
                        cls.model_fields[key].annotation or Any
                    ).validate_json(json.dumps(item), strict=True)
                    if key in cls.model_fields and not key.endswith("_version")
                    else item
                    for key, item in values.items()
                }
            return handler(value)
        except Exception:
            raise _invalid_evidence() from None

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, **kwargs: Any
    ) -> Self:
        from judgmentllm.evidence._base import _invalid_evidence  # pyright: ignore[reportPrivateUsage]
        from judgmentllm.output_contracts import decode_assessment_output

        try:
            if type(json_data) is bytearray:
                raw: str | bytes = bytes(json_data)
            elif type(json_data) in (str, bytes):
                raw = cast(str | bytes, json_data)
            else:
                raise ValueError("invalid JSON input")
            decode_assessment_output(raw)
            return super().model_validate_json(raw, **kwargs)
        except Exception:
            raise _invalid_evidence() from None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int or value != 6:
            raise ValueError("schema_version must be the supported integer 6")
        return value

    @field_validator(
        "schema_version",
        "run_spec_version",
        "output_contract_version",
        "output_commitment_version",
        "transport_commitment_version",
        "request_identity_version",
        "assessment_schema_version",
        "canonicalization_version",
        "serializer_version",
        "index_version",
        mode="before",
    )
    @classmethod
    def strict_version_tokens(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("manifest versions must be integers")
        return value


class ExecutableRunManifestV7(ExecutableRunManifest):
    """Production Vertex AI boundary; never a synthetic V3 manifest."""

    schema_version: Literal[7] = 7  # pyright: ignore[reportIncompatibleVariableOverride]
    transport_protocol: Literal["vertex-production-v1"] = "vertex-production-v1"
    run_spec_version: Literal[3] = 3
    output_contract_version: Literal[2] = 2
    output_commitment_version: Literal[7] = 7
    transport_commitment_version: Literal[5] = 5
    request_identity_version: Literal[3] = 3  # pyright: ignore[reportIncompatibleVariableOverride]
    template: TemplateCommitmentV3  # pyright: ignore[reportIncompatibleVariableOverride]
    transport_context_sha256: SHA256
    initial_transport_count: int = Field(ge=0)
    initial_transport_sha256: SHA256

    @model_validator(mode="wrap")
    @classmethod
    def safe_manifest(
        cls,
        value: object,
        handler: ModelWrapValidatorHandler[Self],
        info: ValidationInfo,
    ) -> Self:
        from judgmentllm.evidence._base import _invalid_evidence  # pyright: ignore[reportPrivateUsage]

        try:
            if info.mode == "json" and isinstance(value, dict):
                import json

                values = cast(dict[str, object], value)
                value = {
                    key: TypeAdapter(
                        cls.model_fields[key].annotation or Any
                    ).validate_json(json.dumps(item), strict=True)
                    if key in cls.model_fields and not key.endswith("_version")
                    else item
                    for key, item in values.items()
                }
            return handler(value)
        except Exception:
            raise _invalid_evidence() from None

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, **kwargs: Any
    ) -> Self:
        from judgmentllm.evidence._base import _invalid_evidence  # pyright: ignore[reportPrivateUsage]
        from judgmentllm.output_contracts import decode_assessment_output

        try:
            if type(json_data) is bytearray:
                raw: str | bytes = bytes(json_data)
            elif type(json_data) in (str, bytes):
                raw = cast(str | bytes, json_data)
            else:
                raise ValueError("invalid JSON input")
            decode_assessment_output(raw)
            return super().model_validate_json(raw, **kwargs)
        except Exception:
            raise _invalid_evidence() from None

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int or value != 7:
            raise ValueError("schema_version must be the supported integer 7")
        return value

    @field_validator(
        "schema_version",
        "run_spec_version",
        "output_contract_version",
        "output_commitment_version",
        "transport_commitment_version",
        "request_identity_version",
        "assessment_schema_version",
        "canonicalization_version",
        "serializer_version",
        "index_version",
        mode="before",
    )
    @classmethod
    def strict_version_tokens(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("manifest versions must be integers")
        return value


ExecutableManifest = (
    ExecutableRunManifest
    | ExecutableRunManifestV2
    | ExecutableRunManifestV3
    | ExecutableRunManifestV4
    | ExecutableRunManifestV5
    | ExecutableRunManifestV6
    | ExecutableRunManifestV7
)


def parse_executable_manifest(raw: bytes) -> ExecutableManifest:
    from judgmentllm.output_contracts import decode_assessment_output

    # Inspect original pairs before dispatch so a duplicate discriminator cannot
    # route native commitments into a historical parser. Valid old bytes retain
    # their established typed records and exact round-trip rules.
    value: object = decode_assessment_output(raw)
    version = (
        cast(dict[str, object], value).get("schema_version", 1)
        if isinstance(value, dict)
        else None
    )
    if type(version) is int and version == 7:
        return ExecutableRunManifestV7.model_validate_json(raw)
    if type(version) is int and version == 6:
        return ExecutableRunManifestV6.model_validate_json(raw)
    if type(version) is int and version == 5:
        return ExecutableRunManifestV5.model_validate_json(raw)
    if type(version) is int and version == 4:
        return ExecutableRunManifestV4.model_validate_json(raw)
    if type(version) is int and version == 3:
        return ExecutableRunManifestV3.model_validate_json(raw)
    if type(version) is int and version == 2:
        return ExecutableRunManifestV2.model_validate_json(raw)
    return ExecutableRunManifest.model_validate_json(raw)
