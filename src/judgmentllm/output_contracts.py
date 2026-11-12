"""Standalone assessment-output semantics, contract version 2.

This is not RunSpec schema version 2 and does not activate rendering, preparation,
submission or persistence. Contract metadata never belongs in model output.
Raw JSON must be decoded here before an SDK discards duplicate-key evidence.
"""

from __future__ import annotations

import json
import math
from typing import Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from judgmentllm.assessment_errors import AssessmentOutputError, contract_error_code
from judgmentllm.models import ID, PairTask, StrictModel, Trait
from judgmentllm.presentation import ESSAY_ALIASES, PresentationVersion
from judgmentllm.results import (
    CategoricalOutput,
    PairwiseOutput,
    validate_categorical_output,
    validate_pairwise_output,
)

__all__ = [
    "RubricOutputContract",
    "PairwiseOutputContract",
    "AssessmentOutputContract",
    "generate_output_schema",
    "serialize_output_schema",
    "decode_assessment_output",
    "validate_rubric_contract_output",
    "validate_pairwise_contract_output",
]

OutputOrder = Literal["together", "scores_first", "explanations_first"]


class _Contract(StrictModel):
    model_config = ConfigDict(frozen=True)
    contract_version: Literal[2] = 2
    presentation_version: PresentationVersion = Field(
        default=1, exclude_if=lambda value: value == 1
    )
    output_order: OutputOrder = "together"
    explanations: bool = False

    @field_validator("contract_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("contract_version must be the supported integer 2")
        return value


class RubricOutputContract(_Contract):
    """Exact requested traits and wire layout; independent of run documents."""

    task_kind: Literal["rubric"] = "rubric"
    traits: tuple[Trait, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _settings(self) -> RubricOutputContract:
        if len({trait.trait_id for trait in self.traits}) != len(self.traits):
            raise ValueError("requested traits must have unique IDs")
        if self.output_order != "together" and not self.explanations:
            raise ValueError("separated rubric layouts require explanations=true")
        return self


class PairwiseOutputContract(_Contract):
    """Presented pair and generative winner/tie policy, never a native decision."""

    task_kind: Literal["pairwise"] = "pairwise"
    pair: PairTask
    allow_ties: bool = False

    @model_validator(mode="after")
    def _settings(self) -> PairwiseOutputContract:
        if self.output_order != "together":
            raise ValueError("pairwise contracts require output_order=together")
        return self


AssessmentOutputContract = RubricOutputContract | PairwiseOutputContract


def _snapshot(value: object) -> object:
    if isinstance(value, BaseModel):
        return {key: _snapshot(item) for key, item in value}
    if isinstance(value, tuple):
        return tuple(_snapshot(item) for item in cast(tuple[object, ...], value))
    if isinstance(value, list):
        return [_snapshot(item) for item in cast(list[object], value)]
    if isinstance(value, dict):
        return {
            key: _snapshot(item)
            for key, item in cast(dict[object, object], value).items()
        }
    return value


def _checked_contract(contract: object) -> AssessmentOutputContract:
    """Detach and revalidate nested mutable data and model_copy injections."""
    if not isinstance(contract, (RubricOutputContract, PairwiseOutputContract)):
        raise ValueError("expected a supported assessment output contract")
    try:
        current = _snapshot(contract)
        checked = type(contract).model_validate(current)
        if current != _snapshot(checked):
            raise ValueError("contract snapshot would require repair")
        return checked
    except (ValueError, TypeError, RecursionError):
        raise ValueError("invalid assessment output contract") from None


def _json_value(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in cast(list[object], value):
            _json_value(item)
        return
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ValueError("JSON object keys must be strings")
            _json_value(item)
        return
    raise ValueError("output must contain only finite JSON values")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AssessmentOutputError("duplicate JSON object key", "duplicate_keys")
        result[key] = value
    return result


def _nonfinite(token: str) -> object:
    raise AssessmentOutputError("nonfinite JSON number", "json_syntax")


def decode_assessment_output(raw: str | bytes) -> object:
    """Decode UTF-8 bytes or JSON text, rejecting duplicates and nonfinite values.

    Duplicate keys are checked at every object depth. Previously decoded SDK
    objects cannot recover the original duplicate keys; validators recursively
    check their JSON values but do not establish original key uniqueness.
    """
    try:
        if type(raw) not in (str, bytes):
            raise ValueError("expected JSON text or UTF-8 bytes")
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        value: object = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_nonfinite
        )
        _json_value(value)
        return value
    except UnicodeError:
        raise AssessmentOutputError(
            "invalid assessment output JSON", "invalid_utf8"
        ) from None
    except (ValueError, TypeError, RecursionError) as error:
        code = (
            error.diagnostic_code
            if isinstance(error, AssessmentOutputError)
            else "json_syntax"
        )
        raise AssessmentOutputError("invalid assessment output JSON", code) from None


def _checked_output(data: object) -> object:
    if isinstance(data, (str, bytes)):
        return decode_assessment_output(data)
    try:
        _json_value(data)
        return data
    except (ValueError, TypeError, RecursionError):
        raise ValueError("invalid decoded assessment output") from None


def _closed(properties: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _rubric_schema(contract: RubricOutputContract) -> dict[str, JsonValue]:
    choices: list[JsonValue] = []
    for trait in contract.traits:
        fields: dict[str, JsonValue] = {
            "trait_id": {"const": trait.trait_id},
            "category_label": {
                "type": "string",
                "enum": [level.label for level in trait.levels],
            },
        }
        if contract.explanations and contract.output_order == "together":
            fields["explanation"] = {"type": "string", "minLength": 1}
        choices.append(_closed(fields))
    count = len(contract.traits)
    scores: JsonValue = {
        "type": "array",
        "minItems": count,
        "maxItems": count,
        "items": {"oneOf": choices},
    }
    if contract.output_order == "together":
        return _closed({"scores": scores})
    explanations: JsonValue = {
        "type": "array",
        "minItems": count,
        "maxItems": count,
        "items": _closed(
            {
                "trait_id": {
                    "type": "string",
                    "enum": [trait.trait_id for trait in contract.traits],
                },
                "text": {"type": "string", "minLength": 1},
            }
        ),
    }
    return _closed(
        {"scores": scores, "explanations": explanations}
        if contract.output_order == "scores_first"
        else {"explanations": explanations, "scores": scores}
    )


def _pairwise_schema(contract: PairwiseOutputContract) -> dict[str, JsonValue]:
    winners: list[JsonValue] = list(wire_winner_ids(contract))
    fields: dict[str, JsonValue] = {"winner_id": {"enum": winners}}
    if contract.allow_ties:
        fields["winner_id"] = {"enum": [*winners, None]}
        fields["tie"] = {"type": "boolean"}
    if contract.explanations:
        fields["explanation"] = {"type": "string", "minLength": 1}
    schema = _closed(fields)
    if contract.allow_ties:
        schema["oneOf"] = [
            {"properties": {"winner_id": {"const": None}, "tie": {"const": True}}},
            {"properties": {"winner_id": {"enum": winners}, "tie": {"const": False}}},
        ]
    return schema


def wire_winner_ids(contract: PairwiseOutputContract) -> tuple[str, str]:
    """The exact wire identifiers, never inferred from source-ID spelling."""
    if contract.presentation_version == 2:
        return ESSAY_ALIASES
    return (contract.pair.presented_order[0], contract.pair.presented_order[1])


def generate_output_schema(contract: AssessmentOutputContract) -> dict[str, JsonValue]:
    """Generate a closed JSON Schema using 2020-12 semantics and exact membership.

    As in the approved examples, the dialect is documented rather than inserted
    into the root. Local relational validation remains mandatory. No user schema
    or provider-specific representation is accepted.
    """
    checked = _checked_contract(contract)
    return (
        _rubric_schema(checked)
        if isinstance(checked, RubricOutputContract)
        else _pairwise_schema(checked)
    )


def serialize_output_schema(contract: AssessmentOutputContract) -> bytes:
    """Serialize intentional property order; never substitute canonical hashing."""
    return json.dumps(
        generate_output_schema(contract),
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class _Score(StrictModel):
    trait_id: ID
    category_label: str


class _Explanation(StrictModel):
    trait_id: ID
    text: str


class _SeparatedOutput(StrictModel):
    scores: list[_Score] = Field(min_length=1)
    explanations: list[_Explanation] = Field(min_length=1)


def validate_rubric_contract_output(
    data: object, contract: RubricOutputContract
) -> CategoricalOutput:
    """Validate the complete group; successful sibling salvage is separate.

    Inputs may be raw JSON text/UTF-8 bytes or already decoded JSON values. Only
    the raw decoding path can detect original duplicate object keys.
    """
    checked = _checked_contract(contract)
    if not isinstance(checked, RubricOutputContract):
        raise ValueError("rubric validation requires a rubric output contract")
    data = _checked_output(data)
    try:
        if checked.output_order == "together":
            return validate_categorical_output(
                data, checked.traits, explanations=checked.explanations
            )
        separated = _SeparatedOutput.model_validate(data)
        explanations = {entry.trait_id: entry.text for entry in separated.explanations}
        expected = {trait.trait_id for trait in checked.traits}
        if (
            len(explanations) != len(separated.explanations)
            or set(explanations) != expected
        ):
            raise ValueError(
                "explanations must cover each requested trait exactly once"
            )
        joined = {
            "scores": [
                {
                    "trait_id": entry.trait_id,
                    "category_label": entry.category_label,
                    "explanation": explanations[entry.trait_id],
                }
                for entry in separated.scores
                if entry.trait_id in explanations
            ]
        }
        # Unknown scores cannot disappear through the ID join.
        if len(joined["scores"]) != len(separated.scores):
            raise ValueError("unknown score trait")
        return validate_categorical_output(joined, checked.traits, explanations=True)
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        raise AssessmentOutputError(
            "invalid rubric assessment output",
            contract_error_code(error, task_kind=checked.task_kind),
        ) from None


def validate_pairwise_contract_output(
    data: object, contract: PairwiseOutputContract
) -> PairwiseOutput:
    """Validate winner-only or compatible historical forced choice, or explicit tie.

    An omitted tie becomes false only for this new forced-choice contract.
    """
    checked = _checked_contract(contract)
    if not isinstance(checked, PairwiseOutputContract):
        raise ValueError("pairwise validation requires a pairwise output contract")
    data = _checked_output(data)
    try:
        if checked.presentation_version == 2 and isinstance(data, dict):
            fields = cast(dict[str, object], data)
            if not checked.allow_ties and "tie" in fields:
                raise ValueError("unexpected tie field")
            winner = fields.get("winner_id")
            if winner is not None:
                if type(winner) is not str or winner not in ESSAY_ALIASES:
                    raise ValueError("winner_id must be an exact presentation alias")
                # Resolve exactly once, before original-ID validation/normalization.
                # An original ID equal to an alias has no special wire meaning.
                data = {
                    **fields,
                    "winner_id": checked.pair.presented_order[
                        ESSAY_ALIASES.index(winner)
                    ],
                }
        if isinstance(data, dict) and not checked.allow_ties and "tie" not in data:
            data = {**cast(dict[str, object], data), "tie": False}
        return validate_pairwise_output(
            cast(object, data),
            checked.pair,
            allow_ties=checked.allow_ties,
            explanations=checked.explanations,
        )
    except (ValueError, TypeError, RecursionError) as error:
        raise AssessmentOutputError(
            "invalid pairwise assessment output",
            contract_error_code(error, task_kind=checked.task_kind),
        ) from None
