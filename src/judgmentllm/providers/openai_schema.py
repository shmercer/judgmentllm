"""Pure OpenAI strict-schema projections, qualified only by offline fixtures.

No SDK import, client, credential discovery, filesystem access or provider call.
The conservative core intentionally leaves bounds and nonblank checks local.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from hashlib import sha256
from typing import cast

from pydantic import JsonValue

from judgmentllm.evidence.scope import EvidenceProvenance
from judgmentllm.native_schema import (
    ConstraintPath,
    NativeAssessmentSourceV1,
    NativeConstraintV1,
    NativeMemberOrderV1,
    NativePairwiseSourceV1,
    NativeProjectionContentV1,
    NativeProjectionError,
    NativeProjectionScopeV1,
    NativeRubricSourceV1,
    NativeSchemaProjectionV1,
    PropertyName,
    schema_text,
    snapshot_source,
    source_contract,
)
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    PairwiseOutputContract,
    RubricOutputContract,
    generate_output_schema,
    serialize_output_schema,
)
from judgmentllm.persistence.hashing import canonical_bytes

OPENAI_SDK_VERSION = "3.42.1"
OPENAI_DOCUMENTATION_SHA256 = (
    "e74cfa4abb72d9ae8e874217c1ab71b9a837dc87fee276dcfdda92f4c1705c1e"
)
OPENAI_FIXTURE_PROFILE_SHA256 = (
    "31f76758956b8c26c86a175ba03f23ee97df24b7e70f0a4a2f4ae5b3ba518197"
)


def _unsupported() -> NativeProjectionError:
    return NativeProjectionError("unsupported_schema")


def _mapping(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise _unsupported()
    return value


def _object(
    node: dict[str, JsonValue], *, tie_root: bool = False
) -> dict[str, JsonValue]:
    keys = {"type", "properties", "required", "additionalProperties"}
    if tie_root:
        keys.add("oneOf")
    if (
        set(node) != keys
        or node.get("type") != "object"
        or node.get("additionalProperties") is not False
    ):
        raise _unsupported()
    properties = _mapping(node["properties"])
    if node["required"] != list(properties):
        raise _unsupported()
    return properties


def _pre_expansion_budget(contract: object) -> None:
    """Bound known source collections before canonical validation/expansion."""
    if isinstance(contract, RubricOutputContract):
        if type(contract.traits) is not tuple:
            raise NativeProjectionError("invalid_contract")
        if len(contract.traits) > 5000:
            raise NativeProjectionError("schema_limit_exceeded", limit="properties")
        enums = 0
        characters = 0
        for trait in contract.traits:
            # Invalid copied models still go through the canonical validator below.
            if type(trait.levels) is not list or type(trait.trait_id) is not str:
                raise NativeProjectionError("invalid_contract")
            enums += len(trait.levels) + 1
            if enums > 1000:
                raise NativeProjectionError(
                    "schema_limit_exceeded", limit="enum_values"
                )
            characters += len(trait.trait_id)
            for level in trait.levels:
                if type(level.label) is not str:
                    raise NativeProjectionError("invalid_contract")
                characters += len(level.label)
                if characters > 120000:
                    raise NativeProjectionError(
                        "schema_limit_exceeded", limit="string_characters"
                    )
    elif isinstance(contract, PairwiseOutputContract):
        if sum(len(item) for item in contract.pair.presented_order) > 120000:
            raise NativeProjectionError(
                "schema_limit_exceeded", limit="string_characters"
            )
    else:
        raise NativeProjectionError("invalid_contract")


def _scope(scope: NativeProjectionScopeV1) -> NativeProjectionScopeV1:
    checked = NativeProjectionScopeV1.model_validate(scope.model_dump())
    if (
        checked.provider != "openai"
        or checked.endpoint not in {"responses", "chat.completions"}
        or checked.sdk_package != "openai"
        or checked.sdk_version != OPENAI_SDK_VERSION
        or checked.api_version is not None
    ):
        raise NativeProjectionError("unsupported_scope")
    return checked


def _limits(schema: dict[str, JsonValue]) -> None:
    """Documented general limits, independent of unknown model-specific limits."""
    stack = [(schema, 0)]
    properties_count = enum_count = character_count = 0
    while stack:
        node, ancestors = stack.pop()
        # Count object/array containers, excluding scalar leaves and anyOf wrappers.
        # This is an offline interpretation of the documented general ceiling.
        depth = ancestors + int(node.get("type") in ("object", "array"))
        if depth > 10:
            raise NativeProjectionError("schema_limit_exceeded", limit="nesting")
        properties = node.get("properties")
        if isinstance(properties, dict):
            properties_count += len(properties)
            character_count += sum(len(key) for key in properties)
            if properties_count > 5000:
                raise NativeProjectionError("schema_limit_exceeded", limit="properties")
            for child in properties.values():
                stack.append((_mapping(child), depth))
        items = node.get("items")
        if items is not None:
            stack.append((_mapping(items), depth))
        alternatives = node.get("anyOf")
        if isinstance(alternatives, list):
            for child in alternatives:
                stack.append((_mapping(child), depth))
        values = node.get("enum")
        if isinstance(values, list):
            enum_count += len(values)
            enum_characters = sum(
                len(value) for value in values if isinstance(value, str)
            )
            character_count += enum_characters
            if enum_count > 1000:
                raise NativeProjectionError(
                    "schema_limit_exceeded", limit="enum_values"
                )
            if len(values) > 250 and enum_characters > 15000:
                raise NativeProjectionError(
                    "schema_limit_exceeded", limit="enum_characters"
                )
        if character_count > 120000:
            raise NativeProjectionError(
                "schema_limit_exceeded", limit="string_characters"
            )


def _closed_coverage(
    path: ConstraintPath, index: int | None = None
) -> tuple[NativeConstraintV1, ...]:
    return tuple(
        NativeConstraintV1(constraint=constraint, path=path, member_index=index)
        for constraint in ("object_shape", "required_properties", "closed_object")
    )


def _nonblank(path: ConstraintPath, index: int | None = None) -> NativeConstraintV1:
    return NativeConstraintV1(
        constraint="nonblank_explanation",
        path=path,
        member_index=index,
        operation="omit_min_length",
        reason="conservative_core",
    )


def _array(
    node: dict[str, JsonValue],
    count: int,
    path: ConstraintPath,
    native: list[NativeConstraintV1],
    local: list[NativeConstraintV1],
) -> dict[str, JsonValue]:
    if (
        set(node) != {"type", "minItems", "maxItems", "items"}
        or node["type"] != "array"
        or node["minItems"] != count
        or node["maxItems"] != count
    ):
        raise _unsupported()
    del node["minItems"]
    del node["maxItems"]
    native.append(NativeConstraintV1(constraint="array_type", path=path))
    local.extend(
        (
            NativeConstraintV1(
                constraint="array_cardinality",
                path=path,
                operation="omit_bounds",
                reason="conservative_core",
            ),
            NativeConstraintV1(
                constraint="trait_uniqueness_completeness",
                path=path,
                operation="local_validation",
                reason="relational_requirement",
            ),
        )
    )
    return _mapping(node["items"])


def _rubric(
    schema: dict[str, JsonValue],
    source: NativeRubricSourceV1,
    native: list[NativeConstraintV1],
    local: list[NativeConstraintV1],
) -> tuple[NativeMemberOrderV1, ...]:
    properties = _object(schema)
    expected_root = (
        ["scores"]
        if source.output_order == "together"
        else (
            ["scores", "explanations"]
            if source.output_order == "scores_first"
            else ["explanations", "scores"]
        )
    )
    if list(properties) != expected_root:
        raise _unsupported()
    items = _array(
        _mapping(properties["scores"]), len(source.traits), "scores", native, local
    )
    if set(items) != {"oneOf"} or not isinstance(items["oneOf"], list):
        raise _unsupported()
    choices = items.pop("oneOf")
    if not isinstance(choices, list) or len(choices) != len(source.traits):
        raise _unsupported()
    members: list[NativeMemberOrderV1] = []
    for index, (choice, trait) in enumerate(zip(choices, source.traits, strict=True)):
        fields = _object(_mapping(choice))
        expected = ["trait_id", "category_label"]
        if source.explanations and source.output_order == "together":
            expected.append("explanation")
        if (
            list(fields) != expected
            or fields["trait_id"] != {"const": trait.trait_id}
            or fields["category_label"]
            != {"type": "string", "enum": list(trait.category_labels)}
        ):
            raise _unsupported()
        fields["trait_id"] = {"type": "string", "enum": [trait.trait_id]}
        native.extend(_closed_coverage("scores.items", index))
        native.extend(
            (
                NativeConstraintV1(
                    constraint="trait_membership",
                    path="scores.items.trait_id",
                    member_index=index,
                    operation="const_to_enum",
                    reason="equivalent_construct",
                ),
                NativeConstraintV1(
                    constraint="trait_categories",
                    path="scores.items.category_label",
                    member_index=index,
                ),
            )
        )
        if "explanation" in fields:
            if fields["explanation"] != {"type": "string", "minLength": 1}:
                raise _unsupported()
            fields["explanation"] = {"type": "string"}
            native.append(
                NativeConstraintV1(
                    constraint="string_type",
                    path="scores.items.explanation",
                    member_index=index,
                )
            )
            local.append(_nonblank("scores.items.explanation", index))
        members.append(
            NativeMemberOrderV1(
                array="scores",
                trait_id=trait.trait_id,
                properties=tuple(cast(PropertyName, name) for name in fields),
            )
        )
    # Canonical contract guarantees unique trait IDs; disjoint alternatives make this equivalent.
    items["anyOf"] = choices
    native.append(
        NativeConstraintV1(
            constraint="disjoint_trait_alternatives",
            path="scores.items",
            operation="oneof_to_anyof",
            reason="equivalent_construct",
        )
    )
    if source.output_order != "together":
        explanation_items = _array(
            _mapping(properties["explanations"]),
            len(source.traits),
            "explanations",
            native,
            local,
        )
        fields = _object(explanation_items)
        if (
            list(fields) != ["trait_id", "text"]
            or fields["trait_id"]
            != {"type": "string", "enum": [trait.trait_id for trait in source.traits]}
            or fields["text"] != {"type": "string", "minLength": 1}
        ):
            raise _unsupported()
        fields["text"] = {"type": "string"}
        native.extend(_closed_coverage("explanations.items"))
        native.extend(
            (
                NativeConstraintV1(
                    constraint="trait_membership", path="explanations.items.trait_id"
                ),
                NativeConstraintV1(
                    constraint="string_type", path="explanations.items.text"
                ),
            )
        )
        local.extend(
            (
                _nonblank("explanations.items.text"),
                NativeConstraintV1(
                    constraint="explanation_join",
                    path="explanations",
                    operation="local_validation",
                    reason="relational_requirement",
                ),
            )
        )
        members.append(
            NativeMemberOrderV1(array="explanations", properties=("trait_id", "text"))
        )
    return tuple(members)


def _pairwise(
    schema: dict[str, JsonValue],
    source: NativePairwiseSourceV1,
    native: list[NativeConstraintV1],
    local: list[NativeConstraintV1],
) -> None:
    fields = _object(schema, tie_root=source.allow_ties)
    expected = (
        ["winner_id"]
        + (["tie"] if source.allow_ties else [])
        + (["explanation"] if source.explanations else [])
    )
    winners: list[JsonValue] = list(source.presented_order)
    values = [*winners, None] if source.allow_ties else winners
    if list(fields) != expected or fields["winner_id"] != {"enum": values}:
        raise _unsupported()
    fields["winner_id"] = {
        "type": ["string", "null"] if source.allow_ties else "string",
        "enum": values,
    }
    native.append(
        NativeConstraintV1(
            constraint="winner_membership",
            path="winner_id",
            operation="add_explicit_type",
            reason="equivalent_construct",
        )
    )
    if source.allow_ties:
        if fields["tie"] != {"type": "boolean"} or schema["oneOf"] != [
            {"properties": {"winner_id": {"const": None}, "tie": {"const": True}}},
            {"properties": {"winner_id": {"enum": winners}, "tie": {"const": False}}},
        ]:
            raise _unsupported()
        del schema["oneOf"]
        native.append(NativeConstraintV1(constraint="tie_type", path="tie"))
        local.append(
            NativeConstraintV1(
                constraint="winner_tie_relation",
                path="root",
                operation="omit_root_relation",
                reason="unsupported_root_composition",
            )
        )
    if source.explanations:
        if fields["explanation"] != {"type": "string", "minLength": 1}:
            raise _unsupported()
        fields["explanation"] = {"type": "string"}
        native.append(NativeConstraintV1(constraint="string_type", path="explanation"))
        local.append(_nonblank("explanation"))


def _projection_parts(
    source: NativeAssessmentSourceV1,
    scope: NativeProjectionScopeV1,
) -> dict[str, object]:
    scope = _scope(scope)
    contract = source_contract(source)
    neutral_raw = serialize_output_schema(contract)
    schema = deepcopy(generate_output_schema(contract))
    # Detect changes to the canonical generator rather than projecting a different source.
    if schema_text(schema).encode("utf-8") != neutral_raw:
        raise _unsupported()
    native = list(_closed_coverage("root"))
    local = [
        NativeConstraintV1(
            constraint="unique_json_keys",
            path="output",
            operation="local_validation",
            reason="decoder_requirement",
        )
    ]
    if isinstance(source, NativeRubricSourceV1):
        members = _rubric(schema, source, native, local)
    else:
        _pairwise(schema, source, native, local)
        members = ()
    _limits(schema)
    raw = schema_text(schema)
    schema_name = "judgmentllm_assessment_v1"
    if scope.endpoint == "responses":
        fragment = {
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            }
        }
        mechanism = "openai.responses.text.format"
    else:
        fragment = {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        }
        mechanism = "openai.chat.response_format"
    fragment_text = schema_text(fragment)
    return {
        "schema_version": 1,
        "projection_version": 1,
        "implementation_version": 1,
        "scope": scope,
        "mechanism": mechanism,
        "schema_dialect": "openai.strict-json-schema.v1",
        "source": source,
        "source_contract_version": 2,
        "source_sha256": sha256(canonical_bytes(source)).hexdigest(),
        "neutral_schema_text": neutral_raw.decode("utf-8"),
        "neutral_schema_sha256": sha256(neutral_raw).hexdigest(),
        "native_schema_text": raw,
        "native_schema_sha256": sha256(raw.encode("utf-8")).hexdigest(),
        "format_fragment_text": fragment_text,
        "format_fragment_sha256": sha256(fragment_text.encode("utf-8")).hexdigest(),
        "schema_name": schema_name,
        "root_properties": tuple(_mapping(schema["properties"])),
        "member_properties": members,
        "native_constraints": tuple(native),
        "local_constraints": tuple(local),
        "provenance": (
            EvidenceProvenance(
                kind="documentation",
                source_sha256=OPENAI_DOCUMENTATION_SHA256,
                recorded_on=date(2026, 11, 7),
            ),
            EvidenceProvenance(
                kind="offline_fixture",
                source_sha256=OPENAI_FIXTURE_PROFILE_SHA256,
                recorded_on=date(2026, 11, 7),
            ),
        ),
    }


def project_openai_schema(
    contract: AssessmentOutputContract,
    *,
    scope: NativeProjectionScopeV1,
) -> NativeSchemaProjectionV1:
    """Build an offline candidate; acceptance requires separate scoped evidence."""
    try:
        _pre_expansion_budget(contract)
        source = snapshot_source(contract)
        content = NativeProjectionContentV1.model_validate(
            _projection_parts(source, scope)
        )
        identity = sha256(canonical_bytes(content)).hexdigest()
        return NativeSchemaProjectionV1.model_validate(
            {**content.model_dump(), "projection_sha256": identity}
        )
    except NativeProjectionError:
        raise
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
        RecursionError,
    ):
        raise NativeProjectionError("invalid_contract") from None
