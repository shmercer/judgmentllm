"""Lossless Together v3 schema representation; serving support is unqualified."""

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

TOGETHER_SDK_VERSION = "3.0.0"
TOGETHER_SCHEMA_NAME = "judgmentllm_assessment_v2"
TOGETHER_DOCUMENTATION_SHA256 = (
    "23388e40bab4c910f4637ff9b8ffda869b26a0184ac5024c28f7023c572adc3c"
)
TOGETHER_FIXTURE_PROFILE_SHA256 = (
    "18c0615fbf072d656909a83bab1e12ade391e5e60c215b9c2a52b05413f2c090"
)


def _projection_parts(
    source: NativeAssessmentSourceV1,
    scope: NativeProjectionScopeV1,
    mechanism: NativeMechanismV2,
) -> dict[str, object]:
    if (
        scope.provider != "together"
        or scope.endpoint != "chat.completions"
        or scope.sdk_package != "together"
        or scope.sdk_version != TOGETHER_SDK_VERSION
        or scope.api_version is not None
        or mechanism != "together.chat.response_format"
    ):
        raise NativeProjectionErrorV2("unsupported_scope")
    schema, root, members, native, local = assessment_parts(source, conservative=False)
    fragment = schema_text(
        {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": TOGETHER_SCHEMA_NAME,
                    "schema": schema,
                },
            }
        }
    )
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
                source_sha256=TOGETHER_DOCUMENTATION_SHA256,
                recorded_on=date(2026, 11, 7),
            ),
            EvidenceProvenance(
                kind="offline_fixture",
                source_sha256=TOGETHER_FIXTURE_PROFILE_SHA256,
                recorded_on=date(2026, 11, 7),
            ),
        ),
    }
