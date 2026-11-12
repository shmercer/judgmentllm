"""Reviewed canonical assessment shapes shared by three detached projections.

This is deliberately not a general JSON Schema converter. Every accepted node
is checked against the assessment contract before its reviewed transformation.
"""

from __future__ import annotations

from typing import Literal, cast

from pydantic import JsonValue

from judgmentllm.native_schema import (
    ConstraintPath,
    NativeAssessmentSourceV1,
    NativeMemberOrderV1,
    NativeRubricSourceV1,
    PropertyName,
    schema_text,
    source_contract,
)
from judgmentllm.native_schema_v2 import (
    ConstraintIdentifierV2,
    NativeConstraintCoverageV2,
    NativeProjectionErrorV2,
)
from judgmentllm.output_contracts import (
    PairwiseOutputContract,
    generate_output_schema,
    serialize_output_schema,
    wire_winner_ids,
)


def _unsupported() -> NativeProjectionErrorV2:
    return NativeProjectionErrorV2("unsupported_schema")


def _mapping(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise _unsupported()
    return value


def _object(
    node: dict[str, JsonValue], fields: list[str], *, relation: bool = False
) -> dict[str, JsonValue]:
    keys = {"type", "properties", "required", "additionalProperties"}
    if relation:
        keys.add("oneOf")
    if (
        set(node) != keys
        or node.get("type") != "object"
        or node.get("additionalProperties") is not False
    ):
        raise _unsupported()
    properties = _mapping(node["properties"])
    if list(properties) != fields or node["required"] != fields:
        raise _unsupported()
    return properties


def _coverage(
    constraint: ConstraintIdentifierV2,
    path: ConstraintPath,
    *,
    index: int | None = None,
    native: bool = True,
    documented: bool | None = None,
    operation: str = "preserve",
    reason: str = "supported_core",
) -> NativeConstraintCoverageV2:
    return NativeConstraintCoverageV2.model_validate(
        {
            "constraint": constraint,
            "path": path,
            "member_index": index,
            "operation": operation,
            "reason": reason,
            "represented": native,
            "sdk_retained": True if native else None,
            "documented_support": documented,
            "enforcement_verified": None,
        }
    )


def assessment_parts(
    source: NativeAssessmentSourceV1, *, conservative: bool
) -> tuple[
    dict[str, JsonValue],
    tuple[PropertyName, ...],
    tuple[NativeMemberOrderV1, ...],
    tuple[NativeConstraintCoverageV2, ...],
    tuple[NativeConstraintCoverageV2, ...],
]:
    """Anthropic's conservative subset or a lossless JSON Schema representation.

    ``conservative`` names the Anthropic profile, not an automatic fallback.
    Together/Ollama preserve every canonical keyword; their serving subset is
    unknown. Explicit types added to constants/enums are equivalent constraints.
    """
    contract = source_contract(source)
    schema = generate_output_schema(contract)
    if schema_text(schema).encode() != serialize_output_schema(contract):
        raise _unsupported()
    documented: bool | None = True if conservative else None
    native: list[NativeConstraintCoverageV2] = []
    local = [
        _coverage(
            "unique_json_keys",
            "output",
            native=False,
            operation="local_validation",
            reason="decoder_requirement",
        )
    ]
    members: list[NativeMemberOrderV1] = []

    def closed(path: ConstraintPath, index: int | None = None) -> None:
        for constraint in ("object_shape", "required_properties", "closed_object"):
            native.append(
                _coverage(constraint, path, index=index, documented=documented)
            )

    def nonblank(path: ConstraintPath, index: int | None = None) -> None:
        local.append(
            _coverage(
                "nonblank_explanation",
                path,
                index=index,
                native=False,
                operation="omit_min_length" if conservative else "local_validation",
                reason="unsupported_keyword"
                if conservative
                else "relational_requirement",
                documented=False if conservative else None,
            )
        )

    def explanation(
        node: JsonValue, path: ConstraintPath, index: int | None = None
    ) -> None:
        checked = _mapping(node)
        if checked != {"type": "string", "minLength": 1}:
            raise _unsupported()
        if conservative:
            del checked["minLength"]
        else:
            native.append(_coverage("string_length", path, index=index))
        native.append(
            _coverage("string_type", path, index=index, documented=documented)
        )
        nonblank(path, index)

    def array(
        node: JsonValue, count: int, path: Literal["scores", "explanations"]
    ) -> dict[str, JsonValue]:
        checked = _mapping(node)
        if (
            set(checked) != {"type", "minItems", "maxItems", "items"}
            or checked["type"] != "array"
            or checked["minItems"] != count
            or checked["maxItems"] != count
        ):
            raise _unsupported()
        native.append(_coverage("array_type", path, documented=documented))
        if conservative:
            del checked["minItems"]
            del checked["maxItems"]
            local.append(
                _coverage(
                    "array_cardinality",
                    path,
                    native=False,
                    operation="omit_bounds",
                    reason="unsupported_keyword",
                    documented=False,
                )
            )
        else:
            native.append(_coverage("array_cardinality", path))
        local.append(
            _coverage(
                "trait_uniqueness_completeness",
                path,
                native=False,
                operation="local_validation",
                reason="relational_requirement",
            )
        )
        return _mapping(checked["items"])

    closed("root")
    if isinstance(source, NativeRubricSourceV1):
        expected = (
            ["scores"]
            if source.output_order == "together"
            else (
                ["scores", "explanations"]
                if source.output_order == "scores_first"
                else ["explanations", "scores"]
            )
        )
        fields = _object(schema, expected)
        items = array(fields["scores"], len(source.traits), "scores")
        if set(items) != {"oneOf"} or not isinstance(items["oneOf"], list):
            raise _unsupported()
        alternatives = items["oneOf"]
        if len(alternatives) != len(source.traits):
            raise _unsupported()
        for index, (alternative, trait) in enumerate(
            zip(alternatives, source.traits, strict=True)
        ):
            names = ["trait_id", "category_label"]
            if source.explanations and source.output_order == "together":
                names.append("explanation")
            props = _object(_mapping(alternative), names)
            if props["trait_id"] != {"const": trait.trait_id} or props[
                "category_label"
            ] != {
                "type": "string",
                "enum": list(trait.category_labels),
            }:
                raise _unsupported()
            props["trait_id"] = (
                {"type": "string", "enum": [trait.trait_id]}
                if conservative
                else {
                    "type": "string",
                    "const": trait.trait_id,
                }
            )
            closed("scores.items", index)
            native.extend(
                (
                    _coverage(
                        "trait_membership",
                        "scores.items.trait_id",
                        index=index,
                        documented=documented,
                        operation="const_to_enum"
                        if conservative
                        else "add_explicit_type",
                        reason="equivalent_construct",
                    ),
                    _coverage(
                        "trait_categories",
                        "scores.items.category_label",
                        index=index,
                        documented=documented,
                    ),
                )
            )
            if "explanation" in props:
                explanation(props["explanation"], "scores.items.explanation", index)
            members.append(
                NativeMemberOrderV1(
                    array="scores",
                    trait_id=trait.trait_id,
                    properties=tuple(cast(PropertyName, name) for name in names),
                )
            )
        if conservative:
            del items["oneOf"]
            items["anyOf"] = alternatives
        native.append(
            _coverage(
                "disjoint_trait_alternatives",
                "scores.items",
                documented=documented,
                operation="oneof_to_anyof" if conservative else "preserve",
                reason="equivalent_construct" if conservative else "supported_core",
            )
        )
        if source.output_order != "together":
            props = _object(
                array(fields["explanations"], len(source.traits), "explanations"),
                ["trait_id", "text"],
            )
            if props["trait_id"] != {
                "type": "string",
                "enum": [trait.trait_id for trait in source.traits],
            }:
                raise _unsupported()
            closed("explanations.items")
            native.append(
                _coverage(
                    "trait_membership",
                    "explanations.items.trait_id",
                    documented=documented,
                )
            )
            explanation(props["text"], "explanations.items.text")
            local.append(
                _coverage(
                    "explanation_join",
                    "explanations",
                    native=False,
                    operation="local_validation",
                    reason="relational_requirement",
                )
            )
            members.append(
                NativeMemberOrderV1(
                    array="explanations", properties=("trait_id", "text")
                )
            )
    else:
        names = (
            ["winner_id"]
            + (["tie"] if source.allow_ties else [])
            + (["explanation"] if source.explanations else [])
        )
        fields = _object(schema, names, relation=source.allow_ties)
        contract = source_contract(source)
        assert isinstance(contract, PairwiseOutputContract)
        winners: list[JsonValue] = list(wire_winner_ids(contract))
        values = [*winners, None] if source.allow_ties else winners
        if fields["winner_id"] != {"enum": values}:
            raise _unsupported()
        fields["winner_id"] = {
            "type": ["string", "null"] if source.allow_ties else "string",
            "enum": values,
        }
        native.append(
            _coverage(
                "winner_membership",
                "winner_id",
                documented=documented,
                operation="add_explicit_type",
                reason="equivalent_construct",
            )
        )
        if source.allow_ties:
            if fields["tie"] != {"type": "boolean"} or schema["oneOf"] != [
                {"properties": {"winner_id": {"const": None}, "tie": {"const": True}}},
                {
                    "properties": {
                        "winner_id": {"enum": winners},
                        "tie": {"const": False},
                    }
                },
            ]:
                raise _unsupported()
            native.append(_coverage("tie_type", "tie", documented=documented))
            if conservative:
                del schema["oneOf"]
                local.append(
                    _coverage(
                        "winner_tie_relation",
                        "root",
                        native=False,
                        operation="omit_root_relation",
                        reason="conservative_core",
                    )
                )
            else:
                schema["oneOf"] = [
                    {
                        "type": "object",
                        "properties": {
                            "winner_id": {"type": "null", "const": None},
                            "tie": {"type": "boolean", "const": True},
                        },
                    },
                    {
                        "type": "object",
                        "properties": {
                            "winner_id": {"type": "string", "enum": winners},
                            "tie": {"type": "boolean", "const": False},
                        },
                    },
                ]
                native.append(_coverage("winner_tie_relation", "root"))
        if source.explanations:
            explanation(fields["explanation"], "explanation")
    native.append(_coverage("requested_property_order", "root", documented=documented))
    if conservative:
        # The provider documents a capitalization caveat. Exact local labels
        # remain authoritative; never normalize or compare them case-insensitively.
        for index, item in enumerate(native):
            if item.constraint in {
                "trait_membership",
                "trait_categories",
                "winner_membership",
            }:
                native[index] = _coverage(
                    item.constraint,
                    item.path,
                    index=item.member_index,
                    operation=item.operation,
                    reason="enum_casing_unqualified",
                    documented=None,
                )
    return (
        schema,
        tuple(cast(PropertyName, key) for key in _mapping(schema["properties"])),
        tuple(members),
        tuple(native),
        tuple(local),
    )
