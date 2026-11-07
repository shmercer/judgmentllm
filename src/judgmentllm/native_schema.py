"""Internal, detached native-schema candidates; no execution or persistence hooks.

Schema and fragment text are request content, not safe minimal-capture metadata.
A projection is immutable in-memory evidence and does not establish model support.
"""

from __future__ import annotations

import warnings
from hashlib import sha256
from typing import Any, Literal, Self, TypeAlias, cast

from pydantic import Field, ModelWrapValidatorHandler, model_validator

from judgmentllm.evidence._base import (
    EvidenceModel,
    _invalid_evidence,  # pyright: ignore[reportPrivateUsage]
)
from judgmentllm.evidence.scope import EvidenceProvenance, VersionIdentifier
from judgmentllm.models import ID, Level, PairTask, Trait
from judgmentllm.output_commitments import ordered_json_bytes
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    OutputOrder,
    PairwiseOutputContract,
    RubricOutputContract,
    decode_assessment_output,
    serialize_output_schema,
)
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.models import SHA256

NativeMechanism: TypeAlias = Literal[
    "openai.responses.text.format", "openai.chat.response_format"
]
NativeSchemaDialect: TypeAlias = Literal["openai.strict-json-schema.v1"]
NativeIncompatibilityReason: TypeAlias = Literal[
    "unsupported_scope",
    "unsupported_schema",
    "schema_limit_exceeded",
    "invalid_contract",
]
SchemaLimit: TypeAlias = Literal[
    "properties", "nesting", "string_characters", "enum_values", "enum_characters"
]


class NativeModel(EvidenceModel):
    """Reject duplicate JSON keys and literal coercion before model adaptation."""

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, **kwargs: Any
    ) -> Self:
        try:
            # Pydantic has already discarded duplicate key pairs by the time a
            # model validator runs. Check the complete original document first;
            # then retain JSON mode for dates, tuples and exact version tokens.
            decode_assessment_output(
                bytes(json_data) if isinstance(json_data, bytearray) else json_data
            )
            return super().model_validate_json(json_data, **kwargs)
        except Exception:
            pass
        raise _invalid_evidence() from None

    @model_validator(mode="wrap")
    @classmethod
    def exact_version_tokens(
        cls, value: object, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        if isinstance(value, dict):
            values = cast(dict[str, object], value)
            for key in (
                "schema_version",
                "projection_version",
                "implementation_version",
                "source_contract_version",
                "contract_version",
                "decision_version",
                "profile_version",
                "support_version",
                "selection_version",
            ):
                if (
                    key in values
                    and values[key] is not None
                    and type(values[key]) is not int
                ):
                    raise ValueError("version tokens must be integers")
            if (
                "local_validation_required" in values
                and type(values["local_validation_required"]) is not bool
            ):
                raise ValueError("validation requirement must be boolean")
        return handler(value)


class NativeProjectionDiagnosticV1(NativeModel):
    """Only controlled codes; never source IDs, categories, text or exception data."""

    reason: NativeIncompatibilityReason
    limit: SchemaLimit | None = None

    @model_validator(mode="after")
    def consistent(self) -> NativeProjectionDiagnosticV1:
        if (self.reason == "schema_limit_exceeded") != (self.limit is not None):
            raise ValueError("inconsistent projection diagnostic")
        return self


class NativeProjectionError(ValueError):
    """A structured, content-free incompatibility, never a schema repair request."""

    def __init__(
        self, reason: NativeIncompatibilityReason, *, limit: SchemaLimit | None = None
    ) -> None:
        self.diagnostic = NativeProjectionDiagnosticV1(reason=reason, limit=limit)
        self.reason = reason
        super().__init__(f"Native schema projection unavailable: {reason}.")


class NativeProjectionScopeV1(NativeModel):
    """Exact candidate scope; unresolved model identity is explicit."""

    provider: ID
    endpoint: ID
    model: ID | None = None
    model_scope: Literal["exact", "unresolved"] = "unresolved"
    sdk_package: str | None = None
    sdk_version: VersionIdentifier | None = None
    api_version: VersionIdentifier | None = None

    @model_validator(mode="after")
    def consistent(self) -> NativeProjectionScopeV1:
        if (self.model_scope == "exact") != (self.model is not None):
            raise ValueError("model scope differs")
        if (self.sdk_package is None) != (self.sdk_version is None):
            raise ValueError("SDK package and version must be paired")
        return self


class NativeTraitMembershipV1(NativeModel):
    trait_id: ID = Field(repr=False)
    category_labels: tuple[str, ...] = Field(min_length=2, repr=False)


class NativeRubricSourceV1(NativeModel):
    """Schema semantics only; no rubric descriptions, scoring codes or item text."""

    contract_version: Literal[2] = 2
    task_kind: Literal["rubric"] = "rubric"
    output_order: OutputOrder = "together"
    explanations: bool = False
    traits: tuple[NativeTraitMembershipV1, ...] = Field(min_length=1, repr=False)

    @model_validator(mode="after")
    def consistent(self) -> NativeRubricSourceV1:
        source_contract(self)
        return self


class NativePairwiseSourceV1(NativeModel):
    """Preserve pair occurrence and both response orientations without metadata."""

    contract_version: Literal[2] = 2
    task_kind: Literal["pairwise"] = "pairwise"
    output_order: Literal["together"] = "together"
    explanations: bool = False
    pair_id: ID = Field(repr=False)
    pair_uid: ID = Field(repr=False)
    trait_id: ID = Field(repr=False)
    original_order: tuple[ID, ID] = Field(repr=False)
    presented_order: tuple[ID, ID] = Field(repr=False)
    allow_ties: bool = False

    @model_validator(mode="after")
    def consistent(self) -> NativePairwiseSourceV1:
        source_contract(self)
        return self


NativeAssessmentSourceV1: TypeAlias = NativeRubricSourceV1 | NativePairwiseSourceV1


def source_contract(source: NativeAssessmentSourceV1) -> AssessmentOutputContract:
    """Reconstitute schema semantics through existing contracts and validators."""
    if isinstance(source, NativeRubricSourceV1):
        return RubricOutputContract(
            contract_version=source.contract_version,
            traits=tuple(
                Trait(
                    trait_id=trait.trait_id,
                    name="",
                    description="",
                    levels=[
                        Level(label=label, numeric_value=float(index))
                        for index, label in enumerate(trait.category_labels)
                    ],
                )
                for trait in source.traits
            ),
            output_order=source.output_order,
            explanations=source.explanations,
        )
    return PairwiseOutputContract(
        contract_version=source.contract_version,
        pair=PairTask(
            pair_id=source.pair_id,
            pair_uid=source.pair_uid,
            trait_id=source.trait_id,
            left_item_id=source.original_order[0],
            right_item_id=source.original_order[1],
            original_order=list(source.original_order),
            presented_order=list(source.presented_order),
        ),
        allow_ties=source.allow_ties,
        explanations=source.explanations,
    )


def snapshot_source(contract: AssessmentOutputContract) -> NativeAssessmentSourceV1:
    """Validate canonical source, then detach only its immutable schema semantics."""
    # Canonical serialization also catches copied/injected models and nested mutation.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        raw = serialize_output_schema(contract)
    if isinstance(contract, RubricOutputContract):
        source: NativeAssessmentSourceV1 = NativeRubricSourceV1(
            output_order=contract.output_order,
            explanations=contract.explanations,
            traits=tuple(
                NativeTraitMembershipV1(
                    trait_id=trait.trait_id,
                    category_labels=tuple(level.label for level in trait.levels),
                )
                for trait in contract.traits
            ),
        )
    else:
        source = NativePairwiseSourceV1(
            explanations=contract.explanations,
            pair_id=contract.pair.pair_id,
            pair_uid=contract.pair.pair_uid,
            trait_id=contract.pair.trait_id,
            original_order=(
                contract.pair.original_order[0],
                contract.pair.original_order[1],
            ),
            presented_order=(
                contract.pair.presented_order[0],
                contract.pair.presented_order[1],
            ),
            allow_ties=contract.allow_ties,
        )
    if serialize_output_schema(source_contract(source)) != raw:
        raise ValueError("source schema differs")
    return source


ConstraintIdentifier: TypeAlias = Literal[
    "object_shape",
    "required_properties",
    "closed_object",
    "array_type",
    "trait_membership",
    "trait_categories",
    "disjoint_trait_alternatives",
    "array_cardinality",
    "trait_uniqueness_completeness",
    "explanation_join",
    "string_type",
    "nonblank_explanation",
    "winner_membership",
    "tie_type",
    "winner_tie_relation",
    "unique_json_keys",
]
ConstraintPath: TypeAlias = Literal[
    "root",
    "scores",
    "scores.items",
    "scores.items.trait_id",
    "scores.items.category_label",
    "scores.items.explanation",
    "explanations",
    "explanations.items",
    "explanations.items.trait_id",
    "explanations.items.text",
    "winner_id",
    "tie",
    "explanation",
    "output",
]


class NativeConstraintV1(NativeModel):
    """Stable constraint/path IDs; member indexes never echo trait/category text."""

    constraint: ConstraintIdentifier
    path: ConstraintPath
    member_index: int | None = Field(default=None, ge=0)
    operation: Literal[
        "preserve",
        "const_to_enum",
        "oneof_to_anyof",
        "add_explicit_type",
        "omit_bounds",
        "omit_min_length",
        "omit_root_relation",
        "local_validation",
    ] = "preserve"
    reason: Literal[
        "supported_core",
        "equivalent_construct",
        "conservative_core",
        "unsupported_root_composition",
        "relational_requirement",
        "decoder_requirement",
    ] = "supported_core"


PropertyName: TypeAlias = Literal[
    "scores",
    "explanations",
    "trait_id",
    "category_label",
    "explanation",
    "text",
    "winner_id",
    "tie",
]


class NativeMemberOrderV1(NativeModel):
    array: Literal["scores", "explanations"]
    trait_id: ID | None = Field(default=None, repr=False)
    properties: tuple[PropertyName, ...]


class NativeProjectionContentV1(NativeModel):
    """Self-verifying candidate bytes, exact scope and mandatory residual checks.

    Revalidation regenerates the candidate through its fixed implementation.
    These content-bearing records are not minimal-capture evidence or run seals.
    """

    projection_version: Literal[1] = 1
    implementation_version: Literal[1] = 1
    scope: NativeProjectionScopeV1
    mechanism: NativeMechanism
    schema_dialect: NativeSchemaDialect = "openai.strict-json-schema.v1"
    source: NativeAssessmentSourceV1 = Field(repr=False)
    source_contract_version: Literal[2] = 2
    source_sha256: SHA256
    neutral_schema_text: str = Field(repr=False)
    neutral_schema_sha256: SHA256
    native_schema_text: str = Field(repr=False)
    native_schema_sha256: SHA256
    format_fragment_text: str = Field(repr=False)
    format_fragment_sha256: SHA256
    schema_name: Literal["judgmentllm_assessment_v1"] = "judgmentllm_assessment_v1"
    root_properties: tuple[PropertyName, ...]
    member_properties: tuple[NativeMemberOrderV1, ...] = Field(repr=False)
    native_constraints: tuple[NativeConstraintV1, ...]
    local_constraints: tuple[NativeConstraintV1, ...]
    provenance: tuple[EvidenceProvenance, ...] = Field(min_length=1, max_length=8)


class NativeSchemaProjectionV1(NativeProjectionContentV1):
    projection_sha256: SHA256

    @model_validator(mode="after")
    def exact_representation(self) -> NativeSchemaProjectionV1:
        from judgmentllm.providers.openai_schema import _projection_parts  # pyright: ignore[reportPrivateUsage]

        expected = _projection_parts(self.source, self.scope)
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError("native projection differs from its implementation")
        if self.projection_sha256 != projection_digest(self):
            raise ValueError("native projection identity differs")
        return self


def projection_digest(projection: NativeSchemaProjectionV1) -> str:
    """Semantic identity includes exact ordered-byte digests and constraint order."""
    return sha256(
        canonical_bytes(
            projection.model_dump(mode="json", exclude={"projection_sha256"})
        )
    ).hexdigest()


def schema_text(value: object) -> str:
    return ordered_json_bytes(value).decode("utf-8")
