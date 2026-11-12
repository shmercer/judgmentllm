"""Shared Google candidate, part and completion rules; provider errors stay local."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

FINISH_REASONS = {
    "STOP",
    "MAX_TOKENS",
    "SAFETY",
    "RECITATION",
    "LANGUAGE",
    "OTHER",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "MALFORMED_FUNCTION_CALL",
    "IMAGE_SAFETY",
    "UNEXPECTED_TOOL_CALL",
    "TOO_MANY_TOOL_CALLS",
    "IMAGE_PROHIBITED_CONTENT",
    "NO_IMAGE",
    "IMAGE_RECITATION",
    "IMAGE_OTHER",
    "CONTINUATION",
    "FINISH_REASON_UNSPECIFIED",
}
BLOCK_REASONS = {
    "BLOCKED_REASON_UNSPECIFIED",
    "SAFETY",
    "OTHER",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "IMAGE_SAFETY",
}
SAFETY_REASONS = {
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT",
    "IMAGE_RECITATION",
}
_TOOL_FIELDS = {
    "functionCall",
    "functionResponse",
    "executableCode",
    "codeExecutionResult",
}


def parts(body: dict[str, Any]) -> tuple[dict[str, Any] | None, list[object]]:
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or len(cast(list[object], candidates)) != 1:
        return None, []
    value = cast(list[object], candidates)[0]
    if not isinstance(value, dict):
        return None, []
    candidate = cast(dict[str, Any], value)
    index = candidate.get("index", 0)
    if type(index) is not int or index != 0:
        return None, []
    content = candidate.get("content")
    if not isinstance(content, dict):
        return candidate, []
    content = cast(dict[str, Any], content)
    if content.get("role") != "model" or set(content) - {"parts", "role"}:
        return candidate, []
    parts = content.get("parts")
    return candidate, cast(list[object], parts) if isinstance(parts, list) else []


def part_kind(part: dict[str, Any]) -> str:
    if _TOOL_FIELDS.intersection(part):
        return "tool"
    if set(part) - {"text", "thought", "thoughtSignature"}:
        return "unsupported"
    if not isinstance(part.get("text"), str):
        return "unsupported"
    if part.get("thought") is not None and type(part["thought"]) is not bool:
        return "unsupported"
    if part.get("thoughtSignature") is not None and not isinstance(
        part["thoughtSignature"], str
    ):
        return "unsupported"
    return "reasoning" if part.get("thought") is True else "output_text"


def generate_content_output(
    body: dict[str, Any],
    *,
    mapping: Callable[[object], dict[str, Any]],
    blocked_ratings: Callable[[object], bool],
) -> tuple[str, str | None, str]:
    """Use provider-owned validators to preserve safe errors and their precedence."""
    feedback = body.get("promptFeedback")
    if feedback is not None:
        feedback = mapping(feedback)
        reason = feedback.get("blockReason")
        if reason not in (None, "BLOCKED_REASON_UNSPECIFIED") or blocked_ratings(
            feedback.get("safetyRatings")
        ):
            return "refusal", None, "content_filter"
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return "provider_error", None, "missing_output"
    if len(cast(list[object], candidates)) != 1:
        return "provider_error", None, "multiple_outputs"
    candidate, content_parts = parts(body)
    if candidate is None:
        return "provider_error", None, "unsupported_output"
    reason = candidate.get("finishReason")
    if reason in SAFETY_REASONS or blocked_ratings(candidate.get("safetyRatings")):
        return "refusal", None, "content_filter"
    if reason == "MAX_TOKENS":
        return "provider_error", None, "output_limit"
    if (
        reason in (None, "FINISH_REASON_UNSPECIFIED", "CONTINUATION")
        or candidate.get("continuationToken") is not None
    ):
        return "provider_error", None, "incomplete"
    if reason in {
        "MALFORMED_FUNCTION_CALL",
        "UNEXPECTED_TOOL_CALL",
        "TOO_MANY_TOOL_CALLS",
    }:
        return "provider_error", None, "tool_call"
    if reason != "STOP":
        return "provider_error", None, "unsupported_output"
    texts: list[str] = []
    for value in content_parts:
        kind = part_kind(mapping(value))
        if kind == "reasoning":
            continue
        if kind == "tool":
            return "provider_error", None, "tool_call"
        if kind != "output_text":
            return "provider_error", None, "unsupported_output"
        texts.append(mapping(value)["text"])
    if len(texts) != 1:
        return "provider_error", None, "multiple_outputs" if texts else "missing_output"
    if not texts[0].strip():
        return "provider_error", None, "missing_output"
    return "response", texts[0], "completed"
