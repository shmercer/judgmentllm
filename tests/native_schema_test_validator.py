"""Small independent test oracle for the reviewed schema subset, never production.

This evaluates synthetic witness outputs. Unsupported keywords fail the oracle,
so a test cannot silently mistake a broader schema for an enforced constraint.
"""

from __future__ import annotations

from typing import Any, cast


def accepts(schema: dict[str, Any], value: Any) -> bool:
    aliases = {
        "additional_properties": "additionalProperties",
        "any_of": "anyOf",
        "min_items": "minItems",
        "max_items": "maxItems",
        "min_length": "minLength",
        "max_length": "maxLength",
        "property_ordering": "propertyOrdering",
    }
    schema = {aliases.get(key, key): item for key, item in schema.items()}
    unknown = set(schema) - {
        "type",
        "const",
        "enum",
        "anyOf",
        "oneOf",
        "allOf",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "nullable",
        "propertyOrdering",
    }
    assert not unknown, "test oracle encountered an unreviewed keyword"
    if value is None and schema.get("nullable") is True:
        return True
    if "const" in schema and (
        type(value) is not type(schema["const"]) or value != schema["const"]
    ):
        return False
    if "enum" in schema and not any(
        type(value) is type(item) and value == item for item in schema["enum"]
    ):
        return False
    if "anyOf" in schema and not any(
        accepts(child, value) for child in schema["anyOf"]
    ):
        return False
    if (
        "oneOf" in schema
        and sum(accepts(child, value) for child in schema["oneOf"]) != 1
    ):
        return False
    if "allOf" in schema and not all(
        accepts(child, value) for child in schema["allOf"]
    ):
        return False
    kind = schema.get("type")
    if isinstance(kind, list):
        return any(
            accepts({**schema, "type": item}, value) for item in cast(list[Any], kind)
        )
    if isinstance(kind, str):
        kinds = {
            "object": type(value) is dict,
            "array": type(value) is list,
            "string": type(value) is str,
            "boolean": type(value) is bool,
            "null": value is None,
            "integer": type(value) is int,
            "number": type(value) in (int, float),
        }
        if not kinds[kind.lower()]:
            return False
    if type(value) is dict:
        value = cast(dict[str, Any], value)
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and set(
            cast(dict[str, Any], value)
        ) - set(properties):
            return False
        if any(
            not accepts(child, value[key])
            for key, child in properties.items()
            if key in value
        ):
            return False
    elif type(value) is list:
        value = cast(list[Any], value)
        if len(cast(list[Any], value)) < schema.get("minItems", 0) or len(
            value
        ) > schema.get("maxItems", float("inf")):
            return False
        if "items" in schema and any(
            not accepts(schema["items"], item) for item in value
        ):
            return False
    elif type(value) is str:
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get(
            "maxLength", float("inf")
        ):
            return False
    return True
