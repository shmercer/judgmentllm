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
from judgmentllm.providers.google_response import (
    BLOCK_REASONS,
    FINISH_REASONS,
    generate_content_output,
    part_kind,
)
from judgmentllm.providers.google_response import (
    SAFETY_REASONS as _SAFETY_REASONS,
)
from judgmentllm.providers.google_response import (
    parts as _parts,
)

__all__ = [
    "_mapping",
    "_encoded",
    "_usage",
    "_parts",
    "_generate_content_output",
    "_extract_assessment_component",
    "_safe_original_envelope",
]


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


def _part_kind(value: object) -> str:
    return part_kind(_mapping(value))


def _generate_content_output(body: dict[str, Any]) -> tuple[str, str | None, str]:
    return generate_content_output(
        body, mapping=_mapping, blocked_ratings=_blocked_ratings
    )


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
