"""Lossless ordinary local Ollama Chat schema representation, daemon unprobed."""

from __future__ import annotations

from datetime import date

from judgmentllm.evidence.scope import EvidenceProvenance
from judgmentllm.native_schema import (
    NativeAssessmentSourceV1,
    NativeProjectionScopeV1,
    schema_text,
)
from judgmentllm.native_schema_v2 import NativeMechanismV2, NativeProjectionErrorV2
from judgmentllm.providers._assessment_schema import assessment_parts

__all__ = ["_projection_parts"]

OLLAMA_SDK_VERSION = "0.6.3"
OLLAMA_DOCUMENTATION_SHA256 = (
    "d98a48d26094d39ffb47f2a941fb84de4f8deeebf0df423a7948d578ecd66c2c"
)
OLLAMA_FIXTURE_PROFILE_SHA256 = (
    "6339ce4abcff64aaf52c2c9a6210f99ec7e062c4450c5937f2a787e76bea3d5e"
)


def _projection_parts(
    source: NativeAssessmentSourceV1,
    scope: NativeProjectionScopeV1,
    mechanism: NativeMechanismV2,
) -> dict[str, object]:
    if (
        scope.provider != "ollama"
        or scope.endpoint != "chat"
        or scope.sdk_package != "ollama"
        or scope.sdk_version != OLLAMA_SDK_VERSION
        or scope.api_version is not None
        or mechanism != "ollama.chat.format"
    ):
        raise NativeProjectionErrorV2("unsupported_scope")
    schema, root, members, native, local = assessment_parts(source, conservative=False)
    fragment = schema_text({"format": schema})
    return {
        "native_schema_text": schema_text(schema),
        "sdk_config_fragment_text": fragment,
        "format_fragment_text": fragment,
        "root_properties": root,
        "member_properties": members,
        "native_constraints": native,
        "local_constraints": local,
        "prompt_schema_policy": "schema_in_prompt_recommended",
        "provenance": (
            EvidenceProvenance(
                kind="documentation",
                source_sha256=OLLAMA_DOCUMENTATION_SHA256,
                recorded_on=date(2026, 11, 7),
            ),
            EvidenceProvenance(
                kind="offline_fixture",
                source_sha256=OLLAMA_FIXTURE_PROFILE_SHA256,
                recorded_on=date(2026, 11, 7),
            ),
        ),
    }
