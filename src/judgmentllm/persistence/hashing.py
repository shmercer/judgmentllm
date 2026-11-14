"""Version-one semantic canonicalization, separate from exact byte identity.

These digests do not replace Milestone 1 structural task/request identifiers.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import cast

from pydantic import BaseModel, JsonValue

# Reuse the established deep export boundary rather than a second validator.
from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]


def _snapshot(value: object) -> JsonValue:
    if value is None or type(value) in (str, bool, int):
        return cast(JsonValue, value)
    if type(value) is float:
        number = value
        if not math.isfinite(number):
            raise ValueError("canonical data must contain only finite numbers")
        return number
    if isinstance(value, BaseModel):
        # As with export, model_copy mutations and nested mutable data must be
        # revalidated before a Pydantic serializer can omit undeclared fields.
        return _snapshot(_revalidate_record(value).model_dump(mode="json"))
    if isinstance(value, list):
        return [_snapshot(item) for item in cast(list[object], value)]
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        if any(type(key) is not str for key in mapping):
            raise ValueError("canonical object keys must be strings")
        return {cast(str, key): _snapshot(item) for key, item in mapping.items()}
    raise ValueError("canonical data must be JSON values or validated models")


def canonical_bytes(value: object) -> bytes:
    """Encode validated semantic data without trimming or Unicode normalization.

    Model defaults and nulls are included. Object keys are sorted and arrays
    retain their order. Arbitrary objects, non-string keys and nonfinite values
    are rejected rather than converted using a default string serializer.
    """
    try:
        return json.dumps(
            _snapshot(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        # Validation errors can otherwise include private input values.
        raise ValueError("value cannot be canonically encoded as valid JSON") from None


def semantic_digest(domain: str, value: object) -> str:
    """Hash the versioned, domain-separated canonical envelope [1, domain, data]."""
    if (
        type(domain) is not str
        or not domain.strip()
        or domain != domain.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in domain)
    ):
        raise ValueError("semantic hash domain must be a nonempty identifier")
    return bytes_digest(canonical_bytes([1, domain, value]))


def bytes_digest(data: bytes) -> str:
    """Return plain SHA-256 of the exact supplied bytes, with no normalization."""
    if type(data) is not bytes:
        raise TypeError("exact byte hashing requires bytes")
    return hashlib.sha256(data).hexdigest()
