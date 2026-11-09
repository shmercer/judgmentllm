"""Documented Messages envelope interpretation and privacy-safe capture."""

from __future__ import annotations

import json
from typing import Any, cast

from judgmentllm.anthropic_assessment import (
    MAX_ASSESSMENT_COMPONENT_BYTES,
    AnthropicAssessmentComponentV2,
)
from judgmentllm.evidence.usage import (
    AnthropicMessagesUsageInput,
    ReportedUsageInput,
    interpret_anthropic_messages_usage,
)

__all__ = [
    "_mapping",
    "_encoded",
    "_usage",
    "_messages_output",
    "_extract_assessment_component",
    "_safe_original_envelope",
]

_STOP_REASONS = {
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "pause_turn",
    "refusal",
    "model_context_window_exceeded",
}
_USAGE_FIELDS = {
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cache_creation",
}


def _encoded(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Invalid Messages response structure")
    return cast(dict[str, Any], value)


def _usage(endpoint: str, value: object) -> ReportedUsageInput | None:
    if endpoint != "messages":
        raise ValueError("Unsupported Anthropic usage endpoint")
    if value is None:
        return None
    fields = _mapping(value)
    selected = {name: fields[name] for name in _USAGE_FIELDS if name in fields}
    if selected.get("cache_creation") is not None:
        detail = _mapping(selected["cache_creation"])
        selected["cache_creation"] = {
            name: detail[name]
            for name in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
            if name in detail
        }
    return interpret_anthropic_messages_usage(
        AnthropicMessagesUsageInput.model_validate(selected)
    )


def _messages_output(body: dict[str, Any]) -> tuple[str, str | None, str]:
    if body.get("type") != "message" or body.get("role") != "assistant":
        return "provider_error", None, "unsupported_output"
    reason = body.get("stop_reason")
    if reason is not None and not isinstance(reason, str):
        reason = "unknown"
    if reason == "refusal":
        return "refusal", None, "refusal"
    if reason == "max_tokens":
        return "provider_error", None, "output_limit"
    if reason in {None, "pause_turn", "model_context_window_exceeded"}:
        return "provider_error", None, "incomplete"
    if reason == "tool_use":
        return "provider_error", None, "tool_call"
    # No custom stop sequences are accepted by this adapter.
    if reason != "end_turn" or body.get("stop_sequence") is not None:
        return "provider_error", None, "unsupported_output"
    content = body.get("content")
    if not isinstance(content, list):
        return "provider_error", None, "missing_output"
    texts: list[str] = []
    for value in cast(list[object], content):
        part = _mapping(value)
        kind = part.get("type")
        if not isinstance(kind, str):
            kind = "unknown"
        if kind in {"thinking", "redacted_thinking"}:
            continue
        if kind in {"tool_use", "server_tool_use"}:
            return "provider_error", None, "tool_call"
        if kind != "text" or not isinstance(part.get("text"), str):
            return "provider_error", None, "unsupported_output"
        texts.append(part["text"])
    if len(texts) != 1:
        return "provider_error", None, "multiple_outputs" if texts else "missing_output"
    if not texts[0].strip():
        return "provider_error", None, "missing_output"
    return "response", texts[0], "completed"


def _extract_assessment_component(
    body: dict[str, Any], endpoint: str
) -> AnthropicAssessmentComponentV2:
    if endpoint != "messages":
        raise ValueError("Unsupported assessment endpoint")
    contents = body.get("content")
    parts = cast(list[object], contents) if isinstance(contents, list) else []
    candidates: list[tuple[int, str]] = []
    thinking = tools = unsupported = False
    content_types: set[str] = set()
    is_message = body.get("type") == "message" and body.get("role") == "assistant"
    for index, value in enumerate(parts):
        part: dict[str, Any] = (
            _mapping(cast(object, value)) if isinstance(value, dict) else {}
        )
        kind = part.get("type")
        if not isinstance(kind, str):
            kind = "unknown"
        if kind in {"thinking", "redacted_thinking"}:
            thinking = True
        elif kind in {"tool_use", "server_tool_use"}:
            tools = True
            content_types.add("tool")
        elif kind == "text" and isinstance(part.get("text"), str):
            content_types.add("output_text")
            candidates.append((index, part["text"]))
        else:
            unsupported = True
            content_types.add("unsupported")
    reason = body.get("stop_reason")
    if reason is not None and not isinstance(reason, str):
        reason = "unknown"
    refusal = reason == "refusal"
    metadata: dict[str, object] = {
        "endpoint": "messages",
        "stop_reason": reason
        if reason in _STOP_REASONS
        else None
        if reason is None
        else "unknown",
        "item_count": 1,
        "message_count": 1 if is_message else 0,
        "item_types": ("message",),
        "content_types": tuple(sorted(content_types)),
        "content_component_count": len(parts),
        "text_component_count": len(candidates),
        "reasoning_present": thinking,
        "refusal_present": refusal,
        "tool_present": tools,
        "unsupported_present": unsupported or not is_message,
    }
    if len(candidates) > 1:
        return AnthropicAssessmentComponentV2.model_validate(
            metadata | {"extraction_status": "multiple_outputs"}
        )
    if not candidates or not is_message or refusal:
        return AnthropicAssessmentComponentV2.model_validate(
            metadata
            | {
                "extraction_status": "unsupported_components"
                if unsupported or not is_message or refusal
                else "no_text"
            }
        )
    index, text = candidates[0]
    location = {
        "source": "anthropic.messages.content.text",
        "item_index": 0,
        "content_index": index,
    }
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        return AnthropicAssessmentComponentV2.model_validate(
            metadata | location | {"extraction_status": "invalid_utf8"}
        )
    if len(raw) > MAX_ASSESSMENT_COMPONENT_BYTES:
        return AnthropicAssessmentComponentV2.model_validate(
            metadata
            | location
            | {"extraction_status": "oversized", "byte_length": len(raw)}
        )
    return AnthropicAssessmentComponentV2.model_validate(
        metadata
        | location
        | {
            "extraction_status": "extracted",
            "byte_length": len(raw),
            "output_text": raw,
        }
    )


def _safe_original_envelope(
    body: dict[str, Any], endpoint: str, committed_body: dict[str, Any] | None = None
) -> bool:
    if (
        endpoint != "messages"
        or set(body)
        - {
            "id",
            "type",
            "role",
            "model",
            "content",
            "stop_reason",
            "stop_sequence",
            "usage",
            "stop_details",
        }
        or not isinstance(body.get("stop_reason"), str)
        or body.get("stop_reason") not in _STOP_REASONS
        or body.get("stop_details") is not None
    ):
        return False
    if body.get("stop_sequence") is not None or body.get("stop_reason") == "refusal":
        return False
    content = body.get("content")
    if not isinstance(content, list):
        return False
    for value in cast(list[object], content):
        part = _mapping(value)
        if (
            set(part) - {"type", "text", "citations"}
            or part.get("type") != "text"
            or part.get("citations") not in (None, [])
        ):
            return False
    counters = body.get("usage")
    if counters is not None:
        counters = _mapping(counters)
        if set(counters) - _USAGE_FIELDS:
            return False
        creation = counters.get("cache_creation")
        if creation is not None and set(_mapping(creation)) - {
            "ephemeral_5m_input_tokens",
            "ephemeral_1h_input_tokens",
        }:
            return False
        _usage(endpoint, counters)
    return True
