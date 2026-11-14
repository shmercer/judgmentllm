"""Compare both providers with snapshots captured from accepted main before extraction."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import hashlib
import json
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from gemini_fixture import wire_for
from gemini_offline import envelope
from vertex_offline import isolate

from judgmentllm.assessment_diagnostics import diagnose_assessment_output
from judgmentllm.providers import gemini_response, vertex_response
from judgmentllm.providers.gemini import GeminiAdapter
from judgmentllm.providers.protocol import ProviderResponse
from judgmentllm.providers.vertex import VertexAdapter

ENDPOINT = "models.generate_content"
BASE = "6e2a8d204f4ae33e3f74d25e7421674ef3d28c96"
BASELINE = Path(__file__).parent / "fixtures/google_response/baseline.json"
RAW_CASES = (b"\xff", b"{bad", b'{"candidates":[],"candidates":[]}', b"[]", b"null")


def matrix() -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {"completed": envelope("{}")}

    def add(name: str, **updates: Any) -> None:
        cases[name] = envelope("{}", **updates)

    def candidate(name: str, **updates: Any) -> None:
        value = envelope("{}")["candidates"][0]
        value.update(updates)
        add(name, candidates=[value])

    cases["absent_candidates"] = {}
    candidate_shapes: tuple[object, ...] = (
        None,
        {},
        "invalid",
        [],
        [None],
        [1],
        [dict[str, object](), dict[str, object]()],
    )
    for index, value in enumerate(candidate_shapes):
        add(f"candidates_{index}", candidates=value)
    indices: tuple[object, ...] = (None, True, False, -1, 1, 0.0, "0", [], {})
    for index, value in enumerate(indices):
        candidate(f"index_{index}", index=value)
    candidate("omitted_index")
    cases["omitted_index"]["candidates"][0].pop("index")
    candidate("omitted_content")
    cases["omitted_content"]["candidates"][0].pop("content")
    for index, content in enumerate(
        (
            None,
            [],
            {},
            {"role": "user", "parts": [{"text": "{}"}]},
            {"role": "model", "parts": [{"text": "{}"}], "private": "CANARY"},
        )
    ):
        candidate(f"content_{index}", content=content)
    parts_shapes: tuple[object, ...] = (None, {}, "invalid", [])
    for index, parts in enumerate(parts_shapes):
        candidate(f"parts_shape_{index}", content={"role": "model", "parts": parts})
    for reason in sorted(gemini_response.FINISH_REASONS) + [None, "PRIVATE_REASON"]:
        candidate(f"finish_{reason}", finishReason=reason)
    malformed_reasons: tuple[object, ...] = (False, 1, [], {})
    for index, reason in enumerate(malformed_reasons):
        candidate(f"malformed_finish_{index}", finishReason=reason)
    candidate("absent_finish")
    cases["absent_finish"]["candidates"][0].pop("finishReason")
    for index, token in enumerate(("next", "", False, 0, None)):
        candidate(f"continuation_{index}", continuationToken=token)
    for reason in sorted(gemini_response.BLOCK_REASONS) + [None, "PRIVATE_BLOCK"]:
        add(f"block_{reason}", promptFeedback={"blockReason": reason})
    feedback_shapes: tuple[object, ...] = (None, {}, [], "CANARY", False)
    for index, feedback in enumerate(feedback_shapes):
        add(f"feedback_{index}", promptFeedback=feedback)
    ratings_shapes: tuple[object, ...] = (
        None,
        [],
        [dict[str, object]()],
        [{"blocked": False}],
        [{"blocked": True}],
        [{"blocked": None}],
        [{"blocked": True}, {"blocked": False}],
        "CANARY",
        {},
        [None],
        [True],
        [{"blocked": 1}],
        [{"blocked": "CANARY"}],
        [{"blocked": True}, {"blocked": 1}],
    )
    for where in ("prompt", "candidate"):
        for index, ratings in enumerate(ratings_shapes):
            if where == "prompt":
                add(
                    f"{where}_ratings_{index}",
                    promptFeedback={"safetyRatings": ratings},
                )
            else:
                candidate(f"{where}_ratings_{index}", safetyRatings=ratings)
    for reason in ("MAX_TOKENS", "CONTINUATION", "UNEXPECTED_TOOL_CALL", "STOP"):
        candidate(
            f"rating_precedence_{reason}",
            finishReason=reason,
            safetyRatings=[{"blocked": True}],
            continuationToken="next",
        )
    add(
        "prompt_precedes_missing",
        candidates=[],
        promptFeedback={"blockReason": "SAFETY"},
    )
    add(
        "prompt_precedes_multiple",
        candidates=[{}, {}],
        promptFeedback={"blockReason": "SAFETY"},
    )
    candidate(
        "safety_precedes_bad_rating", finishReason="SAFETY", safetyRatings="CANARY"
    )
    add(
        "block_precedes_bad_rating",
        promptFeedback={"blockReason": "SAFETY", "safetyRatings": "CANARY"},
    )
    candidate(
        "limit_precedes_continuation",
        finishReason="MAX_TOKENS",
        continuationToken="next",
    )
    parts_cases: list[list[object]] = [
        [{"text": ""}],
        [{"text": " \n\t"}],
        [{"text": "{"}, {"text": "}"}],
        [{"text": "CANARY_THOUGHT", "thought": True}],
        [{"text": "CANARY_THOUGHT", "thought": True}, {"text": "{}"}],
        [{"text": "{}"}, {"text": "CANARY_THOUGHT", "thought": True}],
        [{"text": "{}", "thoughtSignature": "CANARY_SIGNATURE"}],
        [{"text": "{}", "thought": False}],
        [{"text": "{}", "thought": None}],
        [{"text": "{}", "thoughtSignature": None}],
        [{"text": "{}", "thought": "false"}],
        [{"text": "{}", "thoughtSignature": 1}],
        [{"inlineData": {}}],
        [{"fileData": {}}],
        [{"text": 1}],
        [{"text": "{}", "private": "CANARY"}],
        [None],
        ["CANARY"],
        [False],
    ]
    for field in (
        "functionCall",
        "functionResponse",
        "executableCode",
        "codeExecutionResult",
    ):
        parts_cases.extend(
            (
                [{field: {}}],
                [{"text": "{}"}, {field: {}}],
                [{"text": "{}", "thought": True, field: {}}],
            )
        )
    for index, parts in enumerate(parts_cases):
        candidate(f"components_{index}", content={"role": "model", "parts": parts})
    for name, text in {
        "unicode": '{"winner_id":"001","explanation":"雪\\nline"}',
        "invalid_json": "{bad",
        "duplicate_keys": '{"scores":[],"scores":[]}',
        "wrong_root": "[]",
        "high_surrogate": "\ud800",
        "low_surrogate": "\udfff",
        "oversized": "x" * 1_048_577,
    }.items():
        cases[name] = envelope(text)
    for index, usage in enumerate(
        (
            None,
            {},
            {"candidatesTokenCount": 0},
            {"promptTokenCount": True},
            {"thoughtsTokenCount": "CANARY"},
        )
    ):
        add(f"usage_{index}", usageMetadata=usage)
    add("unsafe_metadata", privateMetadata="CANARY_METADATA")
    return cases


def captured(action: Callable[[], object]) -> object:
    try:
        return action()
    except (ValueError, TypeError) as error:
        message = str(error)
        if type(error) is TypeError:
            # Python 3.15 adds context to this builtin error; retain the old snapshot.
            for name in ("list", "dict"):
                original = f"unhashable type: '{name}'"
                if message == f"cannot use '{name}' as a set element ({original})":
                    message = original
                    break
        return {"error": type(error).__name__, "message": message}


def test_snapshot_preserves_types_and_only_normalizes_native_set_errors() -> None:
    values: tuple[object, ...] = ([], {})
    for value in values:
        assert captured(lambda value=value: value in frozenset[object]({"STOP"})) == {
            "error": "TypeError",
            "message": f"unhashable type: '{type(value).__name__}'",
        }
    for kind in (TypeError, ValueError):
        for message in ("provider-safe-error", "unhashable type: 'set'"):

            def fail(kind: type[Exception] = kind, message: str = message) -> object:
                raise kind(message)

            assert captured(fail) == {"error": kind.__name__, "message": message}


def snapshot(provider: str, body: dict[str, Any], tmp_path: Path) -> dict[str, object]:
    module = gemini_response if provider == "gemini" else vertex_response
    adapter = (GeminiAdapter if provider == "gemini" else VertexAdapter)(
        ENDPOINT, "synthetic-model"
    )
    wire, _, _, _ = wire_for(tmp_path)
    raw = json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("ascii")

    def component() -> object:
        value = module._extract_assessment_component(body, ENDPOINT)
        return {
            "metadata": value.model_dump(mode="json"),
            "text_hex": value.output_text.hex()
            if value.output_text is not None
            else None,
            "diagnostic": diagnose_assessment_output(
                value, wire.output_commitment.contract
            ).__dict__,
        }

    def response() -> object:
        value = adapter._response(raw, body, "synthetic-response-001")
        evidence = value.evidence
        assert value.raw == raw and evidence is not None
        return {
            "response": value.model_dump(mode="json"),
            "evidence": evidence.model_dump(mode="json"),
            "component": evidence.assessment_component.model_dump(mode="json")
            if evidence.assessment_component is not None
            else None,
            "reasoning_text": [
                block.text.hex() if block.text is not None else None
                for block in evidence.reasoning
            ],
            "raw_hex": value.raw.hex(),
            "safe_hex": evidence.safe_response.hex()
            if evidence.safe_response is not None
            else None,
            "assessment_hex": evidence.output_text.hex()
            if evidence.output_text is not None
            else None,
            "component_hex": evidence.assessment_component.output_text.hex()
            if evidence.assessment_component is not None
            and evidence.assessment_component.output_text is not None
            else None,
            "parsed": captured(lambda: adapter.parse(value).model_dump(mode="json")),
        }

    def usage() -> object:
        value = module._usage(ENDPOINT, body.get("usageMetadata"))
        return value.model_dump(mode="json") if value is not None else None

    result = {
        "parts": captured(lambda: module._parts(body)),
        "output": captured(lambda: module._generate_content_output(body)),
        "component": captured(component),
        "safe_original": captured(
            lambda: module._safe_original_envelope(body, ENDPOINT)
        ),
        "usage": captured(usage),
        "response": captured(response),
    }
    assert adapter.actual_call_count == 0
    return result


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
    ).hexdigest()


def raw_snapshot(provider: str, raw: bytes) -> object:
    adapter = (GeminiAdapter if provider == "gemini" else VertexAdapter)(
        ENDPOINT, "synthetic-model"
    )
    response = ProviderResponse(observation_key="001", kind="response", raw=raw)
    result = captured(lambda: adapter.parse(response).model_dump(mode="json"))
    assert response.raw == raw and adapter.actual_call_count == 0
    return result


@pytest.mark.parametrize("provider", ["gemini", "vertex"])
@pytest.mark.parametrize("name,body", matrix().items(), ids=matrix().keys())
def test_accepted_main_equivalence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    name: str,
    body: dict[str, Any],
) -> None:
    isolate(monkeypatch)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert baseline["source_commit"] == BASE
    assert set(baseline["cases"]) == set(matrix())
    original = deepcopy(body)
    assert digest(body) == baseline["cases"][name]["input_sha256"]
    assert (
        digest(snapshot(provider, body, tmp_path)) == baseline["cases"][name][provider]
    )
    assert body == original


@pytest.mark.parametrize("provider", ["gemini", "vertex"])
@pytest.mark.parametrize(
    "raw",
    RAW_CASES,
    ids=("invalid_utf8", "invalid_json", "duplicate_keys", "array", "null"),
)
def test_raw_transport_equivalence(
    monkeypatch: pytest.MonkeyPatch, provider: str, raw: bytes
) -> None:
    isolate(monkeypatch)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert raw_snapshot(provider, raw) == baseline["raw_cases"][raw.hex()][provider]


@pytest.mark.parametrize(
    "module,provider", [(gemini_response, "Gemini"), (vertex_response, "Vertex")]
)
def test_provider_safe_errors_remain_exact(module: Any, provider: str) -> None:
    for action, message in (
        (lambda: module._mapping(None), f"Invalid {provider} response structure"),
        (lambda: module._part_kind(None), f"Invalid {provider} response structure"),
        (
            lambda: module._blocked_ratings("CANARY"),
            f"Invalid {provider} safety structure",
        ),
        (
            lambda: module._blocked_ratings([{"blocked": 1}]),
            f"Invalid {provider} safety structure",
        ),
    ):
        assert captured(action) == {"error": "ValueError", "message": message}
