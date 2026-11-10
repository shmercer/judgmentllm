"""Strict Developer generateContent interpretation before SDK object parsing.

No candidate/part concatenation, JSON repair, reasoning retention, or free-text
provider diagnostics. Components are distinct from completed assessments.
"""

from __future__ import annotations

import json
from typing import Any, cast

from judgmentllm.evidence.usage import ReportedUsageInput
from judgmentllm.gemini_assessment import (
    MAX_ASSESSMENT_COMPONENT_BYTES,
    GeminiAssessmentComponentV3,
)
from judgmentllm.gemini_usage import GeminiUsageInput, interpret_gemini_usage

__all__ = [
    "_mapping",
    "_encoded",
    "_usage",
    "_parts",
    "_generate_content_output",
    "_extract_assessment_component",
    "_safe_original_envelope",
]

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
_SAFETY_REASONS = {
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


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Invalid Gemini response structure")
    return cast(dict[str, Any], value)


def _encoded(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _usage(endpoint: str, value: object) -> ReportedUsageInput | None:
    if endpoint != "models.generate_content":
        raise ValueError("Unsupported Gemini usage endpoint")
    if value is None:
        return None
    fields = _mapping(value)
    return interpret_gemini_usage(
        GeminiUsageInput.model_validate(
            {
                name: fields[name]
                for name in GeminiUsageInput.model_fields
                if name in fields
            }
        )
    )


def _blocked_ratings(value: object) -> bool:
    if value is None:
        return False
    if not isinstance(value, list):
        raise ValueError("Invalid Gemini safety structure")
    blocked = False
    for entry in cast(list[object], value):
        entry = _mapping(entry)
        flag = entry.get("blocked")
        if flag is not None and type(flag) is not bool:
            raise ValueError("Invalid Gemini safety structure")
        blocked = blocked or flag is True
    return blocked


def _parts(body: dict[str, Any]) -> tuple[dict[str, Any] | None, list[object]]:
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or len(cast(list[object], candidates)) != 1:
        return None, []
    value = cast(list[object], candidates)[0]
    if not isinstance(value, dict):
        return None, []
    candidate = _mapping(cast(object, value))
    index = candidate.get("index", 0)
    if type(index) is not int or index != 0:
        return None, []
    content = candidate.get("content")
    if not isinstance(content, dict):
        return candidate, []
    content = _mapping(cast(object, content))
    if content.get("role") != "model" or set(content) - {"parts", "role"}:
        return candidate, []
    parts = content.get("parts")
    return candidate, cast(list[object], parts) if isinstance(parts, list) else []


def _part_kind(value: object) -> str:
    part = _mapping(value)
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


def _generate_content_output(body: dict[str, Any]) -> tuple[str, str | None, str]:
    feedback = body.get("promptFeedback")
    if feedback is not None:
        feedback = _mapping(feedback)
        reason = feedback.get("blockReason")
        if reason not in (None, "BLOCKED_REASON_UNSPECIFIED") or _blocked_ratings(
            feedback.get("safetyRatings")
        ):
            return "refusal", None, "content_filter"
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return "provider_error", None, "missing_output"
    if len(cast(list[object], candidates)) != 1:
        return "provider_error", None, "multiple_outputs"
    candidate, parts = _parts(body)
    if candidate is None:
        return "provider_error", None, "unsupported_output"
    reason = candidate.get("finishReason")
    if reason in _SAFETY_REASONS or _blocked_ratings(candidate.get("safetyRatings")):
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
    for value in parts:
        kind = _part_kind(value)
        if kind == "reasoning":
            continue
        if kind == "tool":
            return "provider_error", None, "tool_call"
        if kind != "output_text":
            return "provider_error", None, "unsupported_output"
        texts.append(_mapping(value)["text"])
    if len(texts) != 1:
        return "provider_error", None, "multiple_outputs" if texts else "missing_output"
    if not texts[0].strip():
        return "provider_error", None, "missing_output"
    return "response", texts[0], "completed"


def _extract_assessment_component(
    body: dict[str, Any], endpoint: str
) -> GeminiAssessmentComponentV3:
    if endpoint != "models.generate_content":
        raise ValueError("Unsupported Gemini assessment endpoint")
    candidate, parts = _parts(body)
    candidates = body.get("candidates")
    count = len(cast(list[object], candidates)) if isinstance(candidates, list) else 0
    kinds = [_part_kind(part) for part in parts]
    texts = [
        (index, _mapping(part)["text"])
        for index, part in enumerate(parts)
        if kinds[index] == "output_text"
    ]
    feedback = (
        _mapping(body["promptFeedback"])
        if body.get("promptFeedback") is not None
        else {}
    )
    reason = candidate.get("finishReason") if candidate is not None else None
    block_reason = feedback.get("blockReason")
    blocked = block_reason not in (
        None,
        "BLOCKED_REASON_UNSPECIFIED",
    ) or _blocked_ratings(feedback.get("safetyRatings"))
    refusal = (
        blocked
        or reason in _SAFETY_REASONS
        or (candidate is not None and _blocked_ratings(candidate.get("safetyRatings")))
    )
    unsafe = "tool" in kinds or "unsupported" in kinds
    metadata: dict[str, object] = {
        "endpoint": endpoint,
        "finish_reason": reason
        if reason in FINISH_REASONS
        else None
        if reason is None
        else "unknown",
        "prompt_block_reason": block_reason
        if block_reason in BLOCK_REASONS
        else None
        if block_reason is None
        else "unknown",
        "item_count": count,
        "item_types": ("message",) if count else (),
        "message_count": 1 if candidate is not None else 0,
        "content_types": tuple(sorted(set(kinds) - {"reasoning"})),
        "content_component_count": len(parts),
        "text_component_count": len(texts),
        "reasoning_present": "reasoning" in kinds,
        "refusal_present": refusal,
        "tool_present": "tool" in kinds,
        "unsupported_present": unsafe or (count == 1 and candidate is None),
    }
    if count > 1 or len(texts) > 1:
        return GeminiAssessmentComponentV3.model_validate(
            metadata | {"extraction_status": "multiple_outputs"}
        )
    if not texts or candidate is None or unsafe or refusal:
        return GeminiAssessmentComponentV3.model_validate(
            metadata
            | {
                "extraction_status": "unsupported_components"
                if unsafe or refusal
                else "no_text"
            }
        )
    index, text = texts[0]
    location = {
        "source": "gemini.generate_content.candidates.content.parts.text",
        "item_index": 0,
        "content_index": index,
    }
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        return GeminiAssessmentComponentV3.model_validate(
            metadata | location | {"extraction_status": "invalid_utf8"}
        )
    if len(raw) > MAX_ASSESSMENT_COMPONENT_BYTES:
        return GeminiAssessmentComponentV3.model_validate(
            metadata
            | location
            | {"extraction_status": "oversized", "byte_length": len(raw)}
        )
    return GeminiAssessmentComponentV3.model_validate(
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
    """Only a tiny known envelope may be retained verbatim; everything else derives."""
    if endpoint != "models.generate_content" or set(body) - {
        "responseId",
        "modelVersion",
        "candidates",
        "usageMetadata",
    }:
        return False
    candidate, parts = _parts(body)
    if (
        candidate is None
        or set(candidate) - {"index", "finishReason", "content"}
        or candidate.get("finishReason") != "STOP"
    ):
        return False
    content = candidate.get("content")
    if (
        not isinstance(content, dict)
        or _mapping(cast(object, content)).get("role") != "model"
        or set(cast(dict[str, object], content)) - {"parts", "role"}
        or len(parts) != 1
    ):
        return False
    if any(
        set(_mapping(part)) != {"text"} or _part_kind(part) != "output_text"
        for part in parts
    ):
        return False
    counters = body.get("usageMetadata")
    if counters is not None:
        if set(_mapping(counters)) - set(GeminiUsageInput.model_fields):
            return False
        _usage(endpoint, counters)
    return True
