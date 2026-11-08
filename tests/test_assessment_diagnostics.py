"""Exact component extraction and canonical, content-free failure diagnostics."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

import pytest
from test_openai_lifecycle import envelope
from test_output_contracts import pair, traits

from judgmentllm.assessment_diagnostics import (
    MAX_ASSESSMENT_COMPONENT_BYTES,
    AssessmentComponentMetadata,
    diagnose_assessment_output,
)
from judgmentllm.assessment_errors import AssessmentOutputError
from judgmentllm.output_contracts import (
    PairwiseOutputContract,
    RubricOutputContract,
    decode_assessment_output,
)
from judgmentllm.providers.openai_response import _extract_assessment_component


def rubric() -> RubricOutputContract:
    return RubricOutputContract(traits=traits()[:1])


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize(
    "text",
    [
        "not JSON",
        '{"scores":',
        '```json\n{"scores":[]}\n```',
        '{"scores":[],"scores":[]}',
        '  {"scores":[{"trait_id":"01","category_label":"high"}]}\n',
    ],
)
def test_exact_component_excludes_envelope_and_reasoning(
    endpoint: str, text: str
) -> None:
    body = envelope(endpoint, text)
    if endpoint == "responses":
        body["output"].insert(
            0, {"type": "reasoning", "summary": [{"text": "private-reasoning-canary"}]}
        )
    else:
        body["choices"][0]["message"]["reasoning_content"] = "private-reasoning-canary"
    body["unknown-private-envelope"] = "private-envelope-canary"
    component = _extract_assessment_component(body, endpoint)
    assert component.output_text == text.encode("utf-8")
    assert component.reasoning_present
    assert component.byte_length == len(text.encode("utf-8"))
    assert component.item_types == (
        ("message", "reasoning") if endpoint == "responses" else ("message",)
    )
    metadata = AssessmentComponentMetadata.model_validate(component.model_dump())
    assert metadata.item_index == (1 if endpoint == "responses" else 0)
    assert text not in repr(component)
    dumped = metadata.model_dump_json()
    assert "private-reasoning-canary" not in dumped
    assert "private-envelope-canary" not in dumped
    assert "output_text" not in component.model_dump()


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize(
    "scenario, expected",
    [
        ("multiple", "multiple_outputs"),
        ("none", "no_text"),
        ("unsupported", "unsupported_components"),
        ("refusal", "no_text"),
        ("oversized", "oversized"),
        ("surrogate", "invalid_utf8"),
    ],
)
def test_unavailable_components_are_withheld(
    endpoint: str, scenario: str, expected: str
) -> None:
    body = envelope(endpoint, "safe text")
    if scenario == "multiple":
        key = "output" if endpoint == "responses" else "choices"
        body[key].append(deepcopy(body[key][0]))
    elif endpoint == "responses":
        content: Any = []
        if scenario == "unsupported":
            content = [{"type": "unknown", "text": "private-unsupported-canary"}]
        elif scenario == "refusal":
            content = [{"type": "refusal", "refusal": "private-refusal-canary"}]
        elif scenario in {"oversized", "surrogate"}:
            content = [
                {
                    "type": "output_text",
                    "text": "x" * (MAX_ASSESSMENT_COMPONENT_BYTES + 1)
                    if scenario == "oversized"
                    else "\ud800",
                }
            ]
        body["output"][0]["content"] = content
    else:
        message = body["choices"][0]["message"]
        message["content"] = None
        if scenario == "unsupported":
            message["content"] = [
                {"type": "unknown", "text": "private-unsupported-canary"}
            ]
        elif scenario == "refusal":
            message["refusal"] = "private-refusal-canary"
        elif scenario == "oversized":
            message["content"] = "x" * (MAX_ASSESSMENT_COMPONENT_BYTES + 1)
        elif scenario == "surrogate":
            message["content"] = "\ud800"
    component = _extract_assessment_component(body, endpoint)
    assert component.output_text is None
    assert component.extraction_status == expected
    assert not any("canary" in str(value) for value in component.model_dump().values())
    if scenario == "oversized":
        assert component.byte_length == MAX_ASSESSMENT_COMPONENT_BYTES + 1
    if scenario == "refusal":
        assert component.refusal_present


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", "no_text"),
        ("not JSON", "json_syntax"),
        ('{"scores":', "json_syntax"),
        ('{"scores":[],"scores":[]}', "duplicate_keys"),
        ('{"scores":[{"trait_id":"01","trait_id":"01"}]}', "duplicate_keys"),
        ('{"scor\\u0065s":[],"scores":[]}', "duplicate_keys"),
        ("[]", "root_type"),
        ('{"scores":[]}', "trait_error"),
        ("null", "root_type"),
        ("{}", "missing_fields"),
        ('{"scores":[{"trait_id":"01"}]}', "missing_fields"),
        (
            '{"scores":[{"trait_id":"01","category_label":"high"}],"private-extra-canary":true}',
            "extra_fields",
        ),
        (
            '{"scores":[{"trait_id":"01","category_label":"private-category-canary"}]}',
            "category_error",
        ),
        (
            '{"scores":[{"trait_id":"private-trait-canary","category_label":"high"}]}',
            "trait_error",
        ),
        ('{"scores":[{"trait_id":"01","category_label":"high"}]}', "valid"),
    ],
)
def test_canonical_failure_classes_are_content_free(text: str, expected: str) -> None:
    component = _extract_assessment_component(envelope("responses", text), "responses")
    result = diagnose_assessment_output(component, rubric())
    assert result.code == expected
    assert result.canonical_valid is (expected == "valid")
    assert "canary" not in repr(result)


@pytest.mark.parametrize(
    "text, expected",
    [
        ('{"winner_id":"001"}', "valid"),
        ('{"winner_id":null}', "winner_error"),
        ('{"winner_id":"001","tie":true}', "winner_error"),
        ('{"winner_id":"private-winner-canary"}', "winner_error"),
        ('{"winner_id":"001","winner_id":"002"}', "duplicate_keys"),
    ],
)
def test_pairwise_canonical_diagnostics(text: str, expected: str) -> None:
    component = _extract_assessment_component(
        envelope("chat.completions", text), "chat.completions"
    )
    result = diagnose_assessment_output(component, PairwiseOutputContract(pair=pair()))
    assert result.code == expected
    assert result.canonical_valid is (expected == "valid")


def test_invalid_utf8_has_fixed_error_and_provider_outcome_does_not_redefine_validity() -> (
    None
):
    with pytest.raises(
        AssessmentOutputError, match="invalid assessment output JSON"
    ) as caught:
        decode_assessment_output(b"\xff")
    assert caught.value.diagnostic_code == "invalid_utf8"
    text = '{"scores":[{"trait_id":"01","category_label":"high"}]}'
    body = envelope("responses", text)
    body["status"] = "incomplete"
    body["output"][0]["status"] = "incomplete"
    component = _extract_assessment_component(body, "responses")
    assert component.output_text == text.encode()
    parsed = diagnose_assessment_output(component, rubric())
    assert parsed.code == "valid" and parsed.canonical_valid
    assert body["status"] == "incomplete"


@pytest.mark.parametrize(
    "changed",
    [
        {"source": "chat.completions.choices.message.content"},
        {"item_index": 50},
        {"content_index": 50},
        {"message_count": 2},
        {"text_component_count": 2},
        {"item_types": ("message", "message")},
        {"extraction_status": "oversized"},
        {"extraction_status": "no_text"},
        {"byte_length": MAX_ASSESSMENT_COMPONENT_BYTES + 1},
        {"private-field-canary": "private-value-canary"},
    ],
)
def test_extraction_metadata_rejects_inconsistent_or_untrusted_fields(
    changed: dict[str, Any],
) -> None:
    component = _extract_assessment_component(
        envelope("responses", "exact text"), "responses"
    )
    with pytest.raises(ValueError) as caught:
        AssessmentComponentMetadata.model_validate(
            {**component.model_dump(), **changed}
        )
    assert "canary" not in str(caught.value)


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize(
    "parts",
    cast(
        list[Any],
        [
            None,
            {},
            "unknown",
            [None],
            [{}],
            [{"type": []}],
            [{"type": "message", "role": "assistant", "content": None}],
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": []}],
                }
            ],
        ],
    ),
)
def test_malformed_envelope_components_have_safe_metadata(
    endpoint: str, parts: Any
) -> None:
    body = {"output" if endpoint == "responses" else "choices": parts}
    component = _extract_assessment_component(body, endpoint)
    assert component.output_text is None
    assert component.extraction_status in {"no_text", "unsupported_components"}
    assert component.source == "unavailable"
