"""Lazy concrete Decisions types, following the existing Gemini/Vertex pattern."""

from __future__ import annotations

from importlib import import_module

_TYPES = {"DecisionsCommitmentV1", "PreparedDecisionsRequestV1"}


def __getattr__(name: str) -> object:
    if name not in _TYPES:
        raise AttributeError("Unknown Decisions type")
    return getattr(import_module("judgmentllm.decisions.transport"), name)
