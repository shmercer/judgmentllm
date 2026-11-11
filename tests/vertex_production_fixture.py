"""Private synthetic route binding for offline production acceptance only.

The real reviewed records contain only private-route hashes. Tests substitute a
fixed synthetic project, preserving each mode's independent source/provenance.
There is no production registration interface or private project in this fixture.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from typing import Literal

import pytest
from vertex_offline import adc

from judgmentllm import vertex_registry as registry
from judgmentllm.vertex_config import VertexRuntimeConfiguration

PROJECT = "synthetic-accepted-project"
MODEL = "gemini-3.1-flash-lite"
MECHANISM = "vertex.generate_content.response_json_schema"


def profiles() -> tuple[registry._QualifiedProfile, ...]:
    return tuple(
        profile.model_copy(
            update={
                "route_sha256": VertexRuntimeConfiguration(
                    project=PROJECT,
                    location="global",
                    auth_mode=profile.authentication_mode,
                ).route_sha256
            }
        )
        for profile in registry._QUALIFIED_PROFILES
    )


def activate(monkeypatch: pytest.MonkeyPatch, mode: Literal["adc", "api_key"]) -> None:
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", profiles())
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.setenv("JUDGMENTLLM_VERTEX_AUTH_MODE", mode)
    if mode == "adc":
        credentials, _ = adc(monkeypatch)
        credentials._quota_project_id = PROJECT
