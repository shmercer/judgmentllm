"""Test-only legacy seals for offline harness logic on the current Google SDK."""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from judgmentllm import (
    gemini_qualification,
    gemini_qualification_execution,
    vertex_qualification,
)
from judgmentllm.google_sdk import GOOGLE_SDK_RUNTIME
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.providers import gemini, vertex

LEGACY = "2.34.0"
FIXTURES = Path(__file__).parent / "fixtures"


def gemini_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    if importlib.metadata.version("google-genai") == LEGACY:
        return
    assert importlib.metadata.version("google-genai") == GOOGLE_SDK_RUNTIME

    # These cases exercise historical plan/recovery logic with intercepted HTTP.
    def version(_name: str) -> str:
        return LEGACY

    for module in (gemini_qualification, gemini_qualification_execution):
        monkeypatch.setattr(
            module,
            "importlib",
            SimpleNamespace(metadata=SimpleNamespace(version=version)),
        )
    monkeypatch.setattr(gemini, "gemini_sdk_version", lambda: LEGACY)


def study_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    from judgmentllm.study import plans

    installed = importlib.metadata.version
    if installed("google-genai") == LEGACY:
        return
    assert installed("google-genai") == GOOGLE_SDK_RUNTIME

    def version(name: str) -> str:
        return LEGACY if name == "google-genai" else installed(name)

    monkeypatch.setattr(
        plans, "importlib", SimpleNamespace(metadata=SimpleNamespace(version=version))
    )
    monkeypatch.setattr(gemini, "gemini_sdk_version", lambda: LEGACY)


def vertex_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    if importlib.metadata.version("google-genai") == LEGACY:
        return
    assert importlib.metadata.version("google-genai") == GOOGLE_SDK_RUNTIME
    raw = (FIXTURES / "production_vertex/sdk-profile.json").read_bytes()
    profile = json.loads(raw)
    assert profile["sdk_version"] == LEGACY
    monkeypatch.setattr(
        vertex_qualification,
        "installed_sdk_evidence",
        lambda: {"profile_sha256": bytes_digest(raw), **profile["source_sha256"]},
    )
    monkeypatch.setattr(vertex, "vertex_sdk_version", lambda: LEGACY)
