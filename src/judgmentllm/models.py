"""Provider-independent assessment contracts for schema version 1.

Containers preserve supplied order. Numeric category coding is resolved per trait;
no endpoint-specific limits or provider objects belong in these models.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


def _identifier(value: str) -> str:
    if (
        not value.strip()
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("identifier must be nonempty with no surrounding whitespace")
    return value


ID = Annotated[str, AfterValidator(_identifier)]


def canonical_label(value: object) -> str:
    """Retain string labels and stringify finite numeric scalar labels."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(
            "category label must be a string or finite number, not boolean"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("category label must be finite")
    label = str(value)
    if not label.strip():
        raise ValueError("category label must not be empty")
    return label


Label = Annotated[str, BeforeValidator(canonical_label)]


class StrictModel(BaseModel):
    """Reject unknown fields and implicit scalar conversions."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class VersionedModel(StrictModel):
    """Root documents carry the supported integer schema version."""

    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the supported integer 1")
        return value


class ResponseItem(VersionedModel):
    """Complete source response; input readers enforce their empty-text policy."""

    item_id: ID
    text: str
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class Level(StrictModel):
    """An ordered category, with the original scalar label retained."""

    label: Label
    source_label: str | int | float | None = None
    description: str = ""
    numeric_value: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _source(cls, value: object) -> object:
        if isinstance(value, Mapping):
            data = dict(cast(Mapping[str, object], value))
            if "label" in data:
                original = data.get("source_label", data["label"])
                if original is None:
                    original = data["label"]
                if canonical_label(original) != canonical_label(data["label"]):
                    raise ValueError(
                        "source_label must represent the same category as label"
                    )
                data["source_label"] = original
            return data
        return value

    @field_validator("numeric_value", mode="before")
    @classmethod
    def _numeric(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("numeric_value must be a finite number, not boolean")
        return value


class ResolvedLevel(StrictModel):
    """A category label and its effective numeric code."""

    label: Label
    numeric_value: float


def _numeric_label(label: str) -> float | None:
    try:
        number = float(label)
    except ValueError:
        return None
    if not math.isfinite(number):
        raise ValueError(f"numeric category label {label!r} must be finite")
    return number


class Trait(StrictModel):
    """A scoring dimension with at least two ordered categories."""

    trait_id: ID
    name: str
    description: str
    levels: list[Level] = Field(min_length=2)

    @model_validator(mode="after")
    def _levels(self) -> Trait:
        labels = [level.label for level in self.levels]
        if len(set(labels)) != len(labels):
            raise ValueError(f"trait {self.trait_id!r} has duplicate category labels")
        explicit = [level.numeric_value for level in self.levels]
        if any(value is not None for value in explicit):
            if not all(value is not None for value in explicit):
                raise ValueError("supply numeric_value for every level or for none")
            values = [cast(float, value) for value in explicit]
        else:
            numeric = [_numeric_label(label) for label in labels]
            if all(value is not None for value in numeric):
                values = [cast(float, value) for value in numeric]
            else:
                values = [float(index) for index in range(len(labels))]
                if any(
                    value is not None and value != index
                    for index, value in enumerate(numeric)
                ):
                    warnings.warn(
                        f"trait {self.trait_id!r} mixes numeric and textual labels; "
                        "using zero-based indices. Supply explicit numeric_value "
                        "for each level to choose another mapping.",
                        UserWarning,
                        stacklevel=2,
                    )
        if any(right <= left for left, right in zip(values, values[1:], strict=False)):
            raise ValueError(
                "numeric category mapping must be unique and strictly increasing"
            )
        return self

    @property
    def coding_kind(self) -> Literal["explicit", "numeric", "indices"]:
        """Identify the coding rule used for this trait."""
        if self.levels[0].numeric_value is not None:
            return "explicit"
        if all(_numeric_label(level.label) is not None for level in self.levels):
            return "numeric"
        return "indices"

    @property
    def numeric_values(self) -> list[float]:
        """Return the resolved codes in the original category order."""
        if self.levels[0].numeric_value is not None:
            return [cast(float, level.numeric_value) for level in self.levels]
        numeric = [_numeric_label(level.label) for level in self.levels]
        if all(value is not None for value in numeric):
            return [cast(float, value) for value in numeric]
        return [float(index) for index in range(len(self.levels))]

    @property
    def resolved_levels(self) -> list[ResolvedLevel]:
        """Return an explicit, serializable label-to-code mapping."""
        return [
            ResolvedLevel(label=level.label, numeric_value=value)
            for level, value in zip(self.levels, self.numeric_values, strict=True)
        ]


class Rubric(VersionedModel):
    """A nonempty collection of unique traits and optional shared guidance."""

    rubric_id: ID
    name: str
    description: str = ""
    traits: list[Trait] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_traits(self) -> Rubric:
        _unique([trait.trait_id for trait in self.traits], "trait")
        return self


class Exemplar(VersionedModel):
    """Reference text with independently optional scores and rationales."""

    exemplar_id: ID
    text: str
    scores: dict[ID, Label] = Field(default_factory=dict)
    rationales: dict[ID, str] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class PairwiseCriterion(StrictModel):
    """Comparison instructions, without imposing an ordinal score scale."""

    trait_id: ID
    name: str
    description: str


class PairTask(VersionedModel):
    """One scheduled comparison occurrence; texts are referenced by item ID."""

    pair_id: ID
    pair_uid: ID
    left_item_id: ID
    right_item_id: ID
    trait_id: ID
    original_order: list[ID] = Field(default_factory=list)
    presented_order: list[ID] = Field(default_factory=list)
    source_pair_uid: ID | None = None
    repetition_index: int = Field(default=0, ge=0)
    is_reversal: bool = False
    reversal_group: ID | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _order(self) -> PairTask:
        expected = [self.left_item_id, self.right_item_id]
        if self.left_item_id == self.right_item_id:
            raise ValueError("pair left_item_id and right_item_id must be distinct")
        if "original_order" not in self.model_fields_set:
            self.original_order = expected.copy()
        if self.original_order != expected:
            raise ValueError("original_order must equal [left_item_id, right_item_id]")
        if "presented_order" not in self.model_fields_set:
            self.presented_order = expected.copy()
        if len(self.presented_order) != 2 or set(self.presented_order) != set(expected):
            raise ValueError("presented_order must be a permutation of original_order")
        return self


def _unique(values: Sequence[str], kind: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {kind} ID: {value!r}")
        seen.add(value)


def validate_exemplars(
    exemplars: Sequence[Exemplar],
    rubric: Rubric | None = None,
    criteria: Sequence[PairwiseCriterion] = (),
) -> None:
    """Validate selected IDs and annotations without changing inclusion order."""
    _unique([exemplar.exemplar_id for exemplar in exemplars], "exemplar")
    _unique([criterion.trait_id for criterion in criteria], "criterion")
    trait_levels = (
        {
            trait.trait_id: {level.label for level in trait.levels}
            for trait in rubric.traits
        }
        if rubric is not None
        else {}
    )
    known = set(trait_levels) | {criterion.trait_id for criterion in criteria}
    for exemplar in exemplars:
        unknown = (set(exemplar.scores) | set(exemplar.rationales)) - known
        if unknown:
            raise ValueError(
                f"exemplar {exemplar.exemplar_id!r}: unknown traits {sorted(unknown)}"
            )
        for trait_id, label in exemplar.scores.items():
            if trait_id not in trait_levels:
                raise ValueError("pairwise exemplars cannot have ordinal scores")
            if label not in trait_levels[trait_id]:
                raise ValueError(
                    f"exemplar {exemplar.exemplar_id!r}: unknown category {label!r} "
                    f"for trait {trait_id!r}"
                )
