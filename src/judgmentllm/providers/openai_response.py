"""Documented OpenAI envelope parsing, safe capture, and usage mappings."""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from judgmentllm.assessment_diagnostics import (
    MAX_ASSESSMENT_COMPONENT_BYTES,
    AssessmentComponent,
)
from judgmentllm.evidence.usage import (
    OpenAIChatUsageInput,
    OpenAIResponsesUsageInput,
    ReportedUsageInput,
    interpret_openai_chat_usage,
    interpret_openai_responses_usage,
)

__all__ = [
    "_encoded",
    "_mapping",
    "_usage",
    "_responses_output",
    "_chat_output",
    "_safe_original_envelope",
    "_extract_assessment_component",
]


def _encoded(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid documented OpenAI response structure")
    return cast(dict[str, Any], value)


def _usage(endpoint: str, value: object) -> ReportedUsageInput | None:
    if value is None:
        return None
    fields = _mapping(value)
    if endpoint == "responses":
        names = (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "input_tokens_details",
            "output_tokens_details",
        )
        model: Any = OpenAIResponsesUsageInput
        convert = interpret_openai_responses_usage
    else:
        names = (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "prompt_tokens_details",
            "completion_tokens_details",
        )
        model = OpenAIChatUsageInput
        convert = interpret_openai_chat_usage
    selected = {name: fields[name] for name in names if name in fields}
    for name, component in (
        (names[3], "cached_tokens"),
        (names[4], "reasoning_tokens"),
    ):
        if selected.get(name) is not None:
            detail = _mapping(selected[name])
            selected[name] = (
                {component: detail[component]} if component in detail else {}
            )
    return convert(model.model_validate(selected))


def _responses_output(body: dict[str, Any]) -> tuple[str, str | None, str]:
    status = body.get("status")
    if status == "incomplete":
        detail = body.get("incomplete_details")
        reason = (
            cast(dict[str, Any], detail).get("reason")
            if isinstance(detail, dict)
            else None
        )
        return (
            "provider_error",
            None,
            "output_limit"
            if reason == "max_output_tokens"
            else "content_filter"
            if reason == "content_filter"
            else "incomplete",
        )
    if status != "completed" or body.get("error") is not None:
        return "provider_error", None, "provider_failure"
    items = body.get("output")
    if not isinstance(items, list):
        return "provider_error", None, "missing_output"
    messages: list[dict[str, Any]] = []
    for item in cast(list[object], items):
        item = _mapping(item)
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message":
            return "provider_error", None, "unsupported_output"
        messages.append(item)
    if len(messages) != 1:
        return (
            "provider_error",
            None,
            "missing_output" if not messages else "multiple_outputs",
        )
    message = messages[0]
    if message.get("role") != "assistant" or message.get("status") != "completed":
        return "provider_error", None, "incomplete"
    content = message.get("content")
    if not isinstance(content, list) or len(cast(list[object], content)) != 1:
        return (
            "provider_error",
            None,
            "multiple_outputs" if content else "missing_output",
        )
    part = _mapping(cast(list[object], content)[0])
    if part.get("type") == "refusal":
        return "refusal", None, "refusal"
    if part.get("type") != "output_text":
        return "provider_error", None, "unsupported_output"
    if not isinstance(part.get("text"), str) or not part["text"].strip():
        return "provider_error", None, "missing_output"
    return "response", part["text"], "completed"


def _chat_output(body: dict[str, Any]) -> tuple[str, str | None, str]:
    choices = body.get("choices")
    if not isinstance(choices, list) or len(cast(list[object], choices)) != 1:
        return (
            "provider_error",
            None,
            "multiple_outputs" if choices else "missing_output",
        )
    choice = _mapping(cast(list[object], choices)[0])
    message = _mapping(choice.get("message"))
    if message.get("refusal") is not None:
        return "refusal", None, "refusal"
    reason = choice.get("finish_reason")
    if reason == "content_filter":
        return "refusal", None, "content_filter"
    if reason == "length":
        return "provider_error", None, "output_limit"
    if (
        message.get("tool_calls")
        or message.get("function_call")
        or reason in {"tool_calls", "function_call"}
    ):
        return "provider_error", None, "tool_call"
    if (
        choice.get("index") != 0
        or reason != "stop"
        or message.get("role") != "assistant"
    ):
        return "provider_error", None, "unsupported_output"
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        return "provider_error", None, "missing_output"
    return "response", text, "completed"


def _safe_original_envelope(
    body: dict[str, Any], endpoint: str, committed_body: dict[str, Any] | None = None
) -> bool:
    """Accept only documented response fields with separable sensitive content.

    Unknown envelope extensions, nonempty metadata, reasoning items, tool/audio
    payloads and annotations force the labeled assessment-only derivative.
    """
    common = {"id", "object", "model", "usage", "service_tier"}
    nested = {
        "usage",
        "output",
        "choices",
        "error",
        "incomplete_details",
        "tools",
        "text",
        "reasoning",
        "metadata",
    }
    if any(
        isinstance(value, (dict, list)) and name not in nested
        for name, value in body.items()
    ):
        return False
    if endpoint == "responses":
        allowed = common | {
            "created_at",
            "status",
            "error",
            "incomplete_details",
            "output",
            "background",
            "store",
            "temperature",
            "top_p",
            "truncation",
            "max_output_tokens",
            "max_tool_calls",
            "parallel_tool_calls",
            "tools",
            "tool_choice",
            "text",
            "reasoning",
            "metadata",
            "instructions",
            "previous_response_id",
            "conversation",
            "user",
            "prompt",
            "prompt_cache_key",
            "prompt_cache_retention",
            "safety_identifier",
            "context_management",
        }
        if (
            set(body) - allowed
            or body.get("error") is not None
            or body.get("metadata") not in (None, {})
        ):
            return False
        if any(
            body.get(name) is not None
            for name in (
                "instructions",
                "previous_response_id",
                "conversation",
                "user",
                "prompt",
                "prompt_cache_key",
                "safety_identifier",
                "context_management",
            )
        ) or body.get("tools") not in (None, []):
            return False
        text_config = body.get("text")
        if text_config not in (None, {}):
            if not isinstance(text_config, dict) or set(
                cast(dict[str, Any], text_config)
            ) - {"format", "verbosity"}:
                return False
            format_config = cast(dict[str, Any], text_config).get("format")
            committed_text = committed_body.get("text") if committed_body else None
            committed_format = (
                cast(dict[str, Any], committed_text).get("format")
                if isinstance(committed_text, dict)
                else None
            )
            if (
                format_config not in (None, {"type": "text"})
                and format_config != committed_format
            ):
                return False
            if cast(dict[str, Any], text_config).get("verbosity") not in (
                None,
                "low",
                "medium",
                "high",
            ):
                return False
        reasoning = body.get("reasoning")
        if reasoning is not None and (
            not isinstance(reasoning, dict)
            or set(cast(dict[str, Any], reasoning)) - {"effort", "summary"}
            or cast(dict[str, Any], reasoning).get("summary") is not None
            or cast(dict[str, Any], reasoning).get("effort")
            not in (None, "none", "minimal", "low", "medium", "high", "xhigh")
        ):
            return False
        details = body.get("incomplete_details")
        if details is not None and (
            not isinstance(details, dict)
            or set(cast(dict[str, Any], details)) != {"reason"}
            or cast(dict[str, Any], details).get("reason")
            not in {"max_output_tokens", "content_filter"}
        ):
            return False
        outputs = body.get("output")
        if not isinstance(outputs, list):
            return False
        for value in cast(list[object], outputs):
            item = _mapping(value)
            if (
                set(item) - {"id", "type", "role", "status", "content"}
                or item.get("type") != "message"
            ):
                return False
            contents = item.get("content")
            if not isinstance(contents, list):
                return False
            for raw_part in cast(list[object], contents):
                part = _mapping(raw_part)
                if (
                    set(part) - {"type", "text", "annotations", "logprobs"}
                    or part.get("type") != "output_text"
                    or part.get("annotations") not in (None, [])
                    or part.get("logprobs") not in (None, [])
                ):
                    return False
    else:
        allowed = common | {"created", "choices", "system_fingerprint"}
        if set(body) - allowed:
            return False
        choices = body.get("choices")
        if not isinstance(choices, list):
            return False
        for value in cast(list[object], choices):
            choice = _mapping(value)
            if (
                set(choice) - {"index", "finish_reason", "message", "logprobs"}
                or choice.get("logprobs") is not None
            ):
                return False
            message = _mapping(choice.get("message"))
            if (
                set(message)
                - {
                    "role",
                    "content",
                    "refusal",
                    "annotations",
                    "audio",
                    "function_call",
                    "tool_calls",
                }
                or any(
                    message.get(name) is not None
                    for name in ("refusal", "audio", "function_call", "tool_calls")
                )
                or message.get("annotations") not in (None, [])
            ):
                return False
    usage = body.get("usage")
    if usage is not None:
        counters = _mapping(usage)
        allowed_counters = (
            {
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "input_tokens_details",
                "output_tokens_details",
            }
            if endpoint == "responses"
            else {
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_tokens_details",
                "completion_tokens_details",
            }
        )
        if set(counters) - allowed_counters:
            return False
        for name, value in counters.items():
            if name.endswith("_details") and value is not None:
                details = _mapping(value)
                if set(details) - {
                    "cached_tokens",
                    "reasoning_tokens",
                    "audio_tokens",
                    "accepted_prediction_tokens",
                    "rejected_prediction_tokens",
                } or any(
                    item is not None and (type(item) is not int or item < 0)
                    for item in details.values()
                ):
                    return False
            elif value is not None and (type(value) is not int or value < 0):
                return False
    return True


def _extract_assessment_component(
    body: dict[str, Any], endpoint: str
) -> AssessmentComponent:
    """Separate one documented assistant text field without judging its JSON.

    Reasoning, refusal and tool text are never candidates. Multiple assistant
    outputs are ambiguous and remain withheld; recognized text is never joined,
    trimmed, repaired or truncated. Envelope safety remains an independent test.
    """
    if endpoint not in {"responses", "chat.completions"}:
        raise ValueError("unsupported assessment extraction endpoint")
    candidates: list[tuple[int, int | None, str]] = []
    messages = content_count = 0
    reasoning = refusal = tools = unsupported = False
    item_types: set[Literal["message", "reasoning", "tool", "unsupported"]] = set()
    content_types: set[Literal["output_text", "refusal", "tool", "unsupported"]] = set()
    items_value = body.get("output" if endpoint == "responses" else "choices")
    items: list[object] = []
    if isinstance(items_value, list):
        items = cast(list[object], items_value)
    else:
        unsupported = True
    for item_index, item_value in enumerate(items):
        if not isinstance(item_value, dict):
            unsupported = True
            item_types.add("unsupported")
            continue
        item = cast(dict[str, Any], item_value)
        if endpoint == "responses":
            kind = item.get("type")
            if kind == "reasoning":
                reasoning = True
                item_types.add("reasoning")
                continue
            if isinstance(kind, str) and kind in {
                "function_call",
                "custom_tool_call",
                "tool_call",
            }:
                tools = True
                item_types.add("tool")
                continue
            if kind != "message":
                unsupported = True
                item_types.add("unsupported")
                continue
            message = item
        else:
            message_value = item.get("message")
            if not isinstance(message_value, dict):
                unsupported = True
                item_types.add("unsupported")
                continue
            message = cast(dict[str, Any], message_value)
        messages += 1
        item_types.add("message")
        if message.get("role") != "assistant":
            unsupported = True
            continue
        if message.get("refusal") is not None:
            refusal = True
        if any(
            message.get(name) is not None for name in ("tool_calls", "function_call")
        ):
            tools = True
        if any(
            message.get(name) is not None for name in ("reasoning", "reasoning_content")
        ):
            reasoning = True
        if endpoint == "chat.completions":
            content = message.get("content")
            if isinstance(content, str):
                content_count += 1
                content_types.add("output_text")
                if message.get("refusal") is None:
                    candidates.append((item_index, None, content))
            elif content is not None:
                unsupported = True
                content_types.add("unsupported")
            continue
        contents = message.get("content")
        if not isinstance(contents, list):
            unsupported = True
            content_types.add("unsupported")
            continue
        for content_index, part_value in enumerate(cast(list[object], contents)):
            content_count += 1
            if not isinstance(part_value, dict):
                unsupported = True
                content_types.add("unsupported")
                continue
            part = cast(dict[str, Any], part_value)
            if part.get("type") == "refusal":
                refusal = True
                content_types.add("refusal")
            elif part.get("type") == "output_text" and isinstance(
                part.get("text"), str
            ):
                content_types.add("output_text")
                candidates.append((item_index, content_index, part["text"]))
            else:
                unsupported = True
                content_types.add("unsupported")
    metadata = {
        "endpoint": endpoint,
        "item_count": len(items),
        "item_types": tuple(sorted(item_types)),
        "content_types": tuple(sorted(content_types)),
        "message_count": messages,
        "content_component_count": content_count,
        "text_component_count": len(candidates),
        "reasoning_present": reasoning,
        "refusal_present": refusal,
        "tool_present": tools,
        "unsupported_present": unsupported,
    }
    if (
        len(candidates) > 1
        or messages > 1
        or (endpoint == "chat.completions" and len(items) > 1)
    ):
        return AssessmentComponent.model_validate(
            {**metadata, "extraction_status": "multiple_outputs"}
        )
    if not candidates:
        return AssessmentComponent.model_validate(
            {
                **metadata,
                "extraction_status": "unsupported_components"
                if unsupported
                else "no_text",
            }
        )
    item_index, content_index, text = candidates[0]
    location = {
        "source": "responses.output.message.content.output_text"
        if endpoint == "responses"
        else "chat.completions.choices.message.content",
        "item_index": item_index,
        "content_index": content_index,
    }
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        return AssessmentComponent.model_validate(
            {**metadata, **location, "extraction_status": "invalid_utf8"}
        )
    if len(raw) > MAX_ASSESSMENT_COMPONENT_BYTES:
        return AssessmentComponent.model_validate(
            {
                **metadata,
                **location,
                "byte_length": len(raw),
                "extraction_status": "oversized",
            }
        )
    return AssessmentComponent.model_validate(
        {
            **metadata,
            **location,
            "byte_length": len(raw),
            "output_text": raw,
            "extraction_status": "extracted",
        }
    )
