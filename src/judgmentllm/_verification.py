"""Exact change detection for private, session-scoped validation reuse.

These tokens are an in-memory fast path, never a replacement for canonical
identities or validation. Unsupported caller values require the legacy path.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import PosixPath, WindowsPath
from typing import cast

from pydantic import BaseModel

ExactToken = tuple[object, ...]


def exact_token(value: object) -> ExactToken | None:
    """Snapshot supported current values without validation or serialization.

    Preserve presentation order and exact types: Python equality otherwise
    equates booleans, integers and floats, and ignores dictionary order. A None
    return means uncacheable, including custom classes with unknown behavior.
    Tokens retain no source records and must remain private to their session.
    """
    return _token(value, set())


def _token(value: object, active: set[int]) -> ExactToken | None:
    kind = type(value)
    if value is None or kind in (str, bool, int):
        return (kind, value)
    if kind is float:
        return (kind, cast(float, value).hex())
    if kind in (PosixPath, WindowsPath):
        return (kind, str(value))
    model = (
        isinstance(value, BaseModel)
        and kind.__module__.startswith("judgmentllm.")
        and getattr(sys.modules.get(kind.__module__), kind.__name__, None) is kind
    )
    if not model and kind not in (dict, list, tuple):
        return None
    identity = id(value)
    if identity in active:
        raise ValueError("cyclic current configuration cannot be verified")
    active.add(identity)
    try:
        if model:
            # Iteration includes undeclared fields injected by model_copy;
            # model_dump can silently discard those fields.
            entries = cast(Iterable[tuple[object, object]], value)
        elif kind is dict:
            entries = cast(dict[object, object], value).items()
        else:
            children = tuple(
                _token(item, active)
                for item in cast(list[object] | tuple[object, ...], value)
            )
            return None if any(item is None for item in children) else (kind, children)
        pairs: list[tuple[ExactToken, ExactToken]] = []
        for key, item in entries:
            key_token, item_token = _token(key, active), _token(item, active)
            if key_token is None or item_token is None:
                return None
            pairs.append((key_token, item_token))
        return (kind, tuple(pairs))
    finally:
        active.remove(identity)
