"""Shared Google candidate, part and completion rules; provider errors stay local."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
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
_JSON_STRING_TOKEN = re.compile(r'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})|["\\]')
_UNICODE_ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}")
_BACKSLASH_RUN = re.compile(r"\\{2,}")
_SHORT_ESCAPES = tuple(zip('"/bfnrt', '"/\b\f\n\r\t', strict=True))


def _decoded_fragments(text: str) -> Iterator[str]:
    # Valid escape tokens stay within a fragment. Quotes and invalid escapes
    # delimit fragments even when the enclosing document is malformed.
    start = 0
    escaped = False
    for token in _JSON_STRING_TOKEN.finditer(text):
        if token.end() - token.start() > 1:
            escaped = True
            continue
        if escaped:
            yield cast(
                str, json.loads(f'"{text[start : token.start()]}"', strict=False)
            )
        start = token.end()
        escaped = False
    if escaped:
        yield cast(str, json.loads(f'"{text[start:]}"', strict=False))


def _credential_escape_remains(text: str, key: str) -> bool:
    # Deeper Unicode escapes are opaque at the limit, even after a slash run.
    if _UNICODE_ESCAPE.search(text):
        return True
    if len(text) < len(key):
        return False
    if any(char in key and "\\" + code in text for code, char in _SHORT_ESCAPES):
        return True
    # A bare run can only shorten. Compare the surrounding text as well.
    return (
        r"\\" in text
        and "\\" in key
        and _BACKSLASH_RUN.sub(r"\\", key) in _BACKSLASH_RUN.sub(r"\\", text)
    )


def _contains_escaped_credential(text: str, key: str, layers: int) -> bool:
    if layers == 0:
        return _credential_escape_remains(text, key)
    return any(
        key in decoded or _contains_escaped_credential(decoded, key, layers - 1)
        for decoded in _decoded_fragments(text)
    )


def contains_credential(raw: bytes, key: str) -> bool:
    """Screen raw and JSON-escaped credentials before retaining or hashing."""
    return key.encode("utf-8") in raw or (
        b"\\" in raw
        and _contains_escaped_credential(raw.decode("utf-8", errors="replace"), key, 4)
    )


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
