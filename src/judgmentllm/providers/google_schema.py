"""Reviewed SDK-free Google schema projections; no client or authentication.

Cloud Schema serialization freezes pinned SDK field order, uppercase types and
snake-case names. Representation and SDK preservation do not prove enforcement.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Literal, cast

from pydantic import JsonValue

from judgmentllm.evidence.scope import EvidenceProvenance
from judgmentllm.native_schema import (
    ConstraintPath,
    NativeAssessmentSourceV1,
    NativeMemberOrderV1,
    NativePairwiseSourceV1,
    NativeProjectionScopeV1,
    NativeRubricSourceV1,
    PropertyName,
    schema_text,
    source_contract,
)
from judgmentllm.native_schema_v2 import (
    NativeConstraintCoverageV2,
    NativeMechanismV2,
    NativeProjectionErrorV2,
)
from judgmentllm.output_contracts import (
    PairwiseOutputContract,
    generate_output_schema,
    wire_winner_ids,
)

GOOGLE_SDK_VERSION = "2.34.0"
GOOGLE_DOCUMENTATION_SHA256 = (
    "48a6231496bcd889c2dab228c636ac8374e73fbb1e4e5bb17a0745a545458f6c"
)
GOOGLE_FIXTURE_PROFILE_SHA256 = (
    "36d2f738d6a6ecc890a47298b86437a76ce54d7cc7380183dccef2bfe461a356"
)

__all__ = ["GOOGLE_SDK_VERSION", "_projection_parts"]

# Pinned google.genai.types.Schema.model_fields order; None fields omitted.
_CLOUD_FIELDS = (
    "additionalProperties",
    "$defs",
    "$ref",
    "anyOf",
    "default",
    "description",
    "enum",
    "example",
    "format",
    "items",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "minItems",
    "minLength",
    "minProperties",
    "minimum",
    "nullable",
    "pattern",
    "properties",
    "propertyOrdering",
    "required",
    "title",
    "type",
)
_CLOUD_NAMES = {
    "additionalProperties": "additional_properties",
    "$defs": "defs",
    "$ref": "ref",
    "anyOf": "any_of",
    "maxItems": "max_items",
    "maxLength": "max_length",
    "maxProperties": "max_properties",
    "minItems": "min_items",
    "minLength": "min_length",
    "minProperties": "min_properties",
    "propertyOrdering": "property_ordering",
}


def _unsupported() -> NativeProjectionErrorV2:
    return NativeProjectionErrorV2("unsupported_schema")


def _mapping(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise _unsupported()
    return value


def _scope(scope: NativeProjectionScopeV1, mechanism: NativeMechanismV2) -> bool:
    checked = NativeProjectionScopeV1.model_validate(scope.model_dump())
    if (
        checked.provider not in {"gemini", "vertex"}
        or checked.endpoint != "models.generate_content"
        or checked.sdk_package != "google-genai"
        or checked.sdk_version != GOOGLE_SDK_VERSION
        or checked.api_version
        != ("v1beta" if checked.provider == "gemini" else "v1beta1")
        or mechanism
        not in {
            f"{checked.provider}.generate_content.response_schema",
            f"{checked.provider}.generate_content.response_json_schema",
        }
    ):
        raise NativeProjectionErrorV2("unsupported_scope")
    return mechanism.endswith(".response_schema")


class _Coverage:
    def __init__(self, cloud: bool) -> None:
        self.cloud = cloud
        self.native: list[NativeConstraintCoverageV2] = []
        self.local: list[NativeConstraintCoverageV2] = []

    def add(
        self,
        constraint: str,
        path: ConstraintPath,
        index: int | None = None,
        *,
        local: bool = False,
        operation: str = "preserve",
        reason: str = "supported_core",
        documented: bool | None = True,
    ) -> None:
        record = NativeConstraintCoverageV2.model_validate(
            {
                "constraint": constraint,
                "path": path,
                "member_index": index,
                "operation": operation,
                "reason": reason,
                "represented": not local,
                "sdk_retained": None if local else True,
                "documented_support": None if local else documented,
                "enforcement_verified": None,
            }
        )
        (self.local if local else self.native).append(record)

    def closed(self, path: ConstraintPath, index: int | None = None) -> None:
        for constraint in ("object_shape", "required_properties", "closed_object"):
            # SDK/REST representation does not establish Cloud serving enforcement.
            self.add(
                constraint,
                path,
                index,
                documented=None
                if self.cloud and constraint == "closed_object"
                else True,
            )
        self.add(
            "requested_property_order",
            path,
            index,
            operation="add_property_ordering",
            reason="equivalent_construct",
        )

    def nonblank(self, path: ConstraintPath, index: int | None = None) -> None:
        self.add("string_type", path, index)
        self.add(
            "nonblank_explanation",
            path,
            index,
            local=True,
            operation="omit_min_length",
            reason="conservative_core",
        )


def _closed(node: dict[str, JsonValue], *, ties: bool = False) -> dict[str, JsonValue]:
    expected = {"type", "properties", "required", "additionalProperties"}
    if ties:
        expected.add("oneOf")
    if (
        set(node) != expected
        or node.get("type") != "object"
        or node.get("additionalProperties") is not False
    ):
        raise _unsupported()
    fields = _mapping(node["properties"])
    if node["required"] != list(fields):
        raise _unsupported()
    node["propertyOrdering"] = list(fields)
    return fields


def _array(
    node: dict[str, JsonValue], count: int, path: ConstraintPath, coverage: _Coverage
) -> dict[str, JsonValue]:
    if (
        set(node) != {"type", "minItems", "maxItems", "items"}
        or node["type"] != "array"
        or node["minItems"] != count
        or node["maxItems"] != count
    ):
        raise _unsupported()
    coverage.add("array_type", path)
    coverage.add("array_cardinality", path, operation="preserve_bounds")
    coverage.add(
        "trait_uniqueness_completeness",
        path,
        local=True,
        operation="local_validation",
        reason="relational_requirement",
    )
    return _mapping(node["items"])


def _rubric(
    schema: dict[str, JsonValue], source: NativeRubricSourceV1, coverage: _Coverage
) -> tuple[NativeMemberOrderV1, ...]:
    fields = _closed(schema)
    expected = (
        ["scores"]
        if source.output_order == "together"
        else ["scores", "explanations"]
        if source.output_order == "scores_first"
        else ["explanations", "scores"]
    )
    if list(fields) != expected:
        raise _unsupported()
    items = _array(_mapping(fields["scores"]), len(source.traits), "scores", coverage)
    if set(items) != {"oneOf"} or not isinstance(items["oneOf"], list):
        raise _unsupported()
    choices = items.pop("oneOf")
    if not isinstance(choices, list) or len(choices) != len(source.traits):
        raise _unsupported()
    orders: list[NativeMemberOrderV1] = []
    for index, (choice, trait) in enumerate(zip(choices, source.traits, strict=True)):
        member = _closed(_mapping(choice))
        expected_member = ["trait_id", "category_label"]
        if source.explanations and source.output_order == "together":
            expected_member.append("explanation")
        if (
            list(member) != expected_member
            or member["trait_id"] != {"const": trait.trait_id}
            or member["category_label"]
            != {"type": "string", "enum": list(trait.category_labels)}
        ):
            raise _unsupported()
        member["trait_id"] = {"type": "string", "enum": [trait.trait_id]}
        coverage.closed("scores.items", index)
        coverage.add(
            "trait_membership",
            "scores.items.trait_id",
            index,
            operation="const_to_enum",
            reason="equivalent_construct",
        )
        coverage.add("trait_categories", "scores.items.category_label", index)
        if "explanation" in member:
            if member["explanation"] != {"type": "string", "minLength": 1}:
                raise _unsupported()
            member["explanation"] = {"type": "string"}
            coverage.nonblank("scores.items.explanation", index)
        orders.append(
            NativeMemberOrderV1(
                array="scores",
                trait_id=trait.trait_id,
                properties=tuple(cast(PropertyName, key) for key in member),
            )
        )
    items["anyOf"] = choices
    coverage.add(
        "disjoint_trait_alternatives",
        "scores.items",
        operation="oneof_to_anyof",
        reason="equivalent_construct",
    )
    if source.output_order != "together":
        explanation_items = _array(
            _mapping(fields["explanations"]),
            len(source.traits),
            "explanations",
            coverage,
        )
        member = _closed(explanation_items)
        if (
            list(member) != ["trait_id", "text"]
            or member["trait_id"]
            != {"type": "string", "enum": [trait.trait_id for trait in source.traits]}
            or member["text"] != {"type": "string", "minLength": 1}
        ):
            raise _unsupported()
        member["text"] = {"type": "string"}
        coverage.closed("explanations.items")
        coverage.add("trait_membership", "explanations.items.trait_id")
        coverage.nonblank("explanations.items.text")
        coverage.add(
            "explanation_join",
            "explanations",
            local=True,
            operation="local_validation",
            reason="relational_requirement",
        )
        orders.append(
            NativeMemberOrderV1(array="explanations", properties=("trait_id", "text"))
        )
    return tuple(orders)


def _pairwise(
    schema: dict[str, JsonValue], source: NativePairwiseSourceV1, coverage: _Coverage
) -> None:
    fields = _closed(schema, ties=source.allow_ties)
    expected = (
        ["winner_id"]
        + (["tie"] if source.allow_ties else [])
        + (["explanation"] if source.explanations else [])
    )
    contract = source_contract(source)
    assert isinstance(contract, PairwiseOutputContract)
    winners: list[JsonValue] = list(wire_winner_ids(contract))
    if list(fields) != expected or fields["winner_id"] != {
        "enum": [*winners, None] if source.allow_ties else winners
    }:
        raise _unsupported()
    winner: dict[str, JsonValue] = {"type": "string", "enum": winners}
    fields["winner_id"] = winner
    operation = "add_explicit_type"
    if source.allow_ties:
        if coverage.cloud:
            winner["nullable"] = True
            operation = "nullable_to_flag"
        else:
            fields["winner_id"] = {"anyOf": [winner, {"type": "null"}]}
            operation = "nullable_to_anyof"
        if fields["tie"] != {"type": "boolean"} or schema["oneOf"] != [
            {"properties": {"winner_id": {"const": None}, "tie": {"const": True}}},
            {"properties": {"winner_id": {"enum": winners}, "tie": {"const": False}}},
        ]:
            raise _unsupported()
        del schema["oneOf"]
        coverage.add("tie_type", "tie")
        coverage.add(
            "winner_tie_relation",
            "root",
            local=True,
            operation="omit_root_relation",
            reason="conservative_core",
        )
    coverage.add(
        "winner_membership",
        "winner_id",
        operation=operation,
        reason="equivalent_construct",
    )
    if source.explanations:
        if fields["explanation"] != {"type": "string", "minLength": 1}:
            raise _unsupported()
        fields["explanation"] = {"type": "string"}
        coverage.nonblank("explanation")


def _cloud_schema(
    node: dict[str, JsonValue], *, reject_empty: bool
) -> dict[str, JsonValue]:
    """Match pinned Schema serialization without importing a Google package."""
    if set(node) - set(_CLOUD_FIELDS):
        raise _unsupported()
    result: dict[str, JsonValue] = {}
    for key in _CLOUD_FIELDS:
        if key not in node:
            continue
        value = node[key]
        if key == "properties":
            value = {
                name: _cloud_schema(_mapping(child), reject_empty=reject_empty)
                for name, child in _mapping(value).items()
            }
        elif key == "items":
            value = _cloud_schema(_mapping(value), reject_empty=reject_empty)
        elif key == "anyOf":
            if not isinstance(value, list):
                raise _unsupported()
            value = [
                _cloud_schema(_mapping(child), reject_empty=reject_empty)
                for child in value
            ]
        elif key == "type":
            if not isinstance(value, str) or value not in {
                "object",
                "array",
                "string",
                "boolean",
            }:
                raise _unsupported()
            value = value.upper()
        elif key == "enum":
            if (
                not isinstance(value, list)
                or not value
                or any(
                    type(item) is not str or (reject_empty and item == "")
                    for item in value
                )
            ):
                raise _unsupported()
        result[_CLOUD_NAMES.get(key, key)] = cast(JsonValue, value)
    return result


def _projection_parts(
    source: NativeAssessmentSourceV1,
    scope: NativeProjectionScopeV1,
    mechanism: NativeMechanismV2,
) -> dict[str, object]:
    cloud = _scope(scope, mechanism)
    schema = deepcopy(generate_output_schema(source_contract(source)))
    coverage = _Coverage(cloud)
    coverage.closed("root")
    coverage.add(
        "unique_json_keys",
        "output",
        local=True,
        operation="local_validation",
        reason="decoder_requirement",
    )
    if isinstance(source, NativeRubricSourceV1):
        members = _rubric(schema, source, coverage)
    else:
        _pairwise(schema, source, coverage)
        members = ()
    root_properties = tuple(_mapping(schema["properties"]))
    native_schema = (
        _cloud_schema(schema, reject_empty=scope.provider == "vertex")
        if cloud
        else schema
    )
    route: Literal["response_schema", "response_json_schema"] = (
        "response_schema" if cloud else "response_json_schema"
    )
    configuration: dict[str, JsonValue] = {
        "response_mime_type": "application/json",
        route: native_schema,
    }
    fragment: dict[str, JsonValue] = {
        "responseMimeType": "application/json",
        "responseSchema" if cloud else "responseJsonSchema": native_schema,
    }
    return {
        "native_schema_text": schema_text(native_schema),
        "sdk_config_fragment_text": schema_text(configuration),
        "format_fragment_text": schema_text(fragment),
        "root_properties": root_properties,
        "member_properties": members,
        "native_constraints": tuple(coverage.native),
        "local_constraints": tuple(coverage.local),
        "provenance": (
            EvidenceProvenance(
                kind="documentation",
                source_sha256=GOOGLE_DOCUMENTATION_SHA256,
                recorded_on=date(2026, 11, 7),
            ),
            EvidenceProvenance(
                kind="offline_fixture",
                source_sha256=GOOGLE_FIXTURE_PROFILE_SHA256,
                recorded_on=date(2026, 11, 7),
            ),
        ),
        "prompt_schema_policy": (
            "schema_in_prompt_discouraged"
            if cloud and scope.provider == "vertex"
            else "none"
        ),
    }
