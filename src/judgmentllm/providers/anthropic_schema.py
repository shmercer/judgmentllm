"""Pure Anthropic Messages schema representation; no SDK import or submission."""

from __future__ import annotations

from datetime import date

from pydantic import JsonValue

from judgmentllm.evidence.scope import EvidenceProvenance
from judgmentllm.native_schema import (
    NativeAssessmentSourceV1,
    NativeProjectionScopeV1,
    schema_text,
)
from judgmentllm.native_schema_v2 import NativeMechanismV2, NativeProjectionErrorV2
from judgmentllm.providers._assessment_schema import assessment_parts

__all__ = ["_projection_parts"]

ANTHROPIC_SDK_VERSION = "2.3.0"
ANTHROPIC_API_VERSION = "2026-10-21"
ANTHROPIC_DOCUMENTATION_SHA256 = (
    "27cf6618ce095a22d983802a59027dbbc8574614277d8e2fce2e8810a827034c"
)
ANTHROPIC_FIXTURE_PROFILE_SHA256 = (
    "4459cef4ba678eef37a2e5e9849225be8b378b18adfd11a7c47add6defc08e06"
)


def _limits(schema: dict[str, JsonValue]) -> None:
    """Documented union-parameter limit; internal grammar limits remain unknown."""
    unions = 0
    stack: list[JsonValue] = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "anyOf" in node or isinstance(node.get("type"), list):
                unions += 1
                if unions > 16:
                    raise NativeProjectionErrorV2(
                        "schema_limit_exceeded", limit="alternatives"
                    )
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _projection_parts(
    source: NativeAssessmentSourceV1,
    scope: NativeProjectionScopeV1,
    mechanism: NativeMechanismV2,
) -> dict[str, object]:
    if (
        scope.provider != "anthropic"
        or scope.endpoint != "messages"
        or scope.sdk_package != "anthropic"
        or scope.sdk_version != ANTHROPIC_SDK_VERSION
        or scope.api_version != ANTHROPIC_API_VERSION
        or mechanism != "anthropic.messages.output_config.format"
    ):
        raise NativeProjectionErrorV2("unsupported_scope")
    schema, root, members, native, local = assessment_parts(source, conservative=True)
    _limits(schema)
    fragment = schema_text(
        {"output_config": {"format": {"type": "json_schema", "schema": schema}}}
    )
    return {
        "native_schema_text": schema_text(schema),
        "sdk_config_fragment_text": fragment,
        "format_fragment_text": fragment,
        "root_properties": root,
        "member_properties": members,
        "native_constraints": native,
        "local_constraints": local,
        "prompt_schema_policy": "none",
        "provenance": (
            EvidenceProvenance(
                kind="documentation",
                source_sha256=ANTHROPIC_DOCUMENTATION_SHA256,
                recorded_on=date(2026, 11, 7),
            ),
            EvidenceProvenance(
                kind="offline_fixture",
                source_sha256=ANTHROPIC_FIXTURE_PROFILE_SHA256,
                recorded_on=date(2026, 11, 7),
            ),
        ),
    }
