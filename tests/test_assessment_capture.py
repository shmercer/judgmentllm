"""Exact diagnostic text through actual pinned SDK and durable production stores."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal, cast

import httpx2
import pytest
from test_openai_lifecycle import envelope, production_spec

from judgmentllm.assessment_diagnostics import MAX_ASSESSMENT_COMPONENT_BYTES
from judgmentllm.assessment_inspection import inspect_assessment_output
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.lifecycle import export_run_results
from judgmentllm.persistence.models import ArtifactIntegrityError
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.preparation import prepare_run
from judgmentllm.production_transport import production_context
from judgmentllm.providers.openai import OpenAIAdapter


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_AUTH_CANARY")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("External networking is forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


_TEXT_CASES = [
    ('  {"scores":[{"trait_id":"01","category_label":"low"}]}\n', "valid", True),
    (
        '{"scores":[{"trait_id":"01","category_label":"WRONG_CATEGORY"}]}',
        "category_error",
        False,
    ),
    ('{"scores":[]}', "trait_error", False),
    (
        '{"scores":[{"trait_id":"WRONG_TRAIT","category_label":"low"}]}',
        "trait_error",
        False,
    ),
    (
        '{"scores":[{"trait_id":"01","category_label":"low"}],"UNEXPECTED_FIELD":true}',
        "extra_fields",
        False,
    ),
    ('{"scores": BROKEN_OUTPUT}', "json_syntax", False),
    (
        '```json\n{"scores":[{"trait_id":"01","category_label":"low"}]}\n```',
        "json_syntax",
        False,
    ),
    ('{"scores":[{"trait_id":"01"', "json_syntax", False),
    ('{"scores":[],"scores":[]}', "duplicate_keys", False),
    ("[]", "root_type", False),
    ('Prose before {"scores":[]} after', "json_syntax", False),
    ('{"winner_id":"002"}', "valid", True),
    ('{"winner_id":"WRONG_WINNER"}', "winner_error", False),
]


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("text, code, valid", _TEXT_CASES)
@pytest.mark.asyncio
async def test_exact_diagnostic_capture_and_invalid_outcomes(
    tmp_path: Path,
    endpoint: str,
    capture: Literal["full", "minimal"],
    text: str,
    code: str,
    valid: bool,
) -> None:
    pairwise = "winner_id" in text
    spec = production_spec(
        tmp_path / "i",
        endpoint,
        capture=capture,
        traits=1,
        pairwise=pairwise,
        explanations=False,
    )
    store = tmp_path / "s"
    with prepare_run(
        spec, store=store, run_id="001", transport_context=production_context(spec)
    ):
        pass
    sent: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.content)
        return httpx2.Response(200, json=envelope(endpoint, text), request=request)

    adapter = OpenAIAdapter(
        endpoint, spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        await execute_run("001", store=store, spec=spec, adapter=adapter)
        with ReadOnlyRepository(store) as repo:
            result = tuple(repo.iter_results("001"))
            receipt = tuple(repo.iter_evidence("001"))[0]
            diagnostic = receipt.assessment_diagnostic
            assert diagnostic is not None
            assert diagnostic.parser_diagnostic == code
            assert diagnostic.canonical_valid is valid
            assert diagnostic.identity == receipt.identity
            assert diagnostic.provider_outcome == "completed"
            assert (
                diagnostic.content_sha256 == hashlib.sha256(text.encode()).hexdigest()
            )
            assert diagnostic.byte_length == len(text.encode())
            assert all(
                row.status == ("complete" if valid else "invalid") for row in result
            )
            assert diagnostic.artifact is not None
            assert (diagnostic.artifact.relative_path is not None) == (
                capture == "full"
            )
            assert diagnostic.retention == (
                "retained" if capture == "full" else "hash_only"
            )
            observation_id = receipt.identity.observation_id
            assert observation_id is not None
        metadata = inspect_assessment_output("001", observation_id, store=store)
        assert metadata.output_text is None
        inspected = inspect_assessment_output(
            "001", observation_id, store=store, include_text=True
        )
        assert inspected.output_text == (text.encode() if capture == "full" else None)
        assert "output_text" not in inspected.model_dump()
        assert text not in repr(inspected)
        exported = export_run_results("001", store=store)
        for output in exported.files:
            assert "assessment_diagnostic" not in Path(output.path).read_text()
        await recover_run("001", store=store, spec=spec, adapter=adapter)
        assert len(sent) == 1
        for p in store.rglob("*"):
            if p.is_file():
                data = p.read_bytes()
                assert b"SYNTHETIC_AUTH_CANARY" not in data
                if capture == "minimal" and len(text.encode()) > 32:
                    assert text.encode() not in data
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize(
    "scenario",
    [
        "refusal",
        "truncated",
        "unsupported",
        "multiple",
        "reasoning",
        "unsafe_metadata",
        "oversized",
    ],
)
@pytest.mark.asyncio
async def test_component_privacy_is_independent_of_envelope_retention(
    tmp_path: Path,
    endpoint: str,
    scenario: str,
) -> None:
    spec = production_spec(
        tmp_path / "i", endpoint, capture="full", traits=1, explanations=False
    )
    store = tmp_path / "s"
    text = '{"scores":[{"trait_id":"01","category_label":"low"}]}'
    if scenario == "oversized":
        text = "DIAGNOSTIC_OVERSIZE_CANARY" + " " * MAX_ASSESSMENT_COMPONENT_BYTES
    body = envelope(endpoint, text)
    if endpoint == "responses":
        message = body["output"][0]
        if scenario == "refusal":
            message["content"] = [
                {"type": "refusal", "refusal": "REFUSAL_PRIVATE_CANARY"}
            ]
        elif scenario == "truncated":
            body.update(
                status="incomplete", incomplete_details={"reason": "max_output_tokens"}
            )
            message["status"] = "incomplete"
        elif scenario == "unsupported":
            body["output"].append(
                {"type": "unknown_private", "value": "UNKNOWN_PRIVATE_CANARY"}
            )
        elif scenario == "multiple":
            message["content"].append(
                {"type": "output_text", "text": "SECOND_PRIVATE_CANARY"}
            )
        elif scenario == "reasoning":
            body["output"].insert(
                0,
                {
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "REASONING_PRIVATE_CANARY"}
                    ],
                },
            )
    else:
        choice = body["choices"][0]
        if scenario == "refusal":
            choice["message"].update(content=None, refusal="REFUSAL_PRIVATE_CANARY")
        elif scenario == "truncated":
            choice["finish_reason"] = "length"
        elif scenario == "unsupported":
            choice["message"]["tool_calls"] = [
                {"type": "function", "function": {"name": "UNKNOWN_PRIVATE_CANARY"}}
            ]
        elif scenario == "multiple":
            body["choices"].append(
                {
                    "index": 1,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "SECOND_PRIVATE_CANARY",
                    },
                }
            )
        elif scenario == "reasoning":
            choice["message"]["reasoning_content"] = "REASONING_PRIVATE_CANARY"
    if scenario == "unsafe_metadata":
        body["metadata"] = {"unknown": "UNKNOWN_METADATA_CANARY"}
    with prepare_run(
        spec, store=store, run_id="001", transport_context=production_context(spec)
    ):
        pass
    adapter = OpenAIAdapter(
        endpoint,
        spec.model,
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json=body, request=request)
        ),
    )
    try:
        await execute_run("001", store=store, spec=spec, adapter=adapter)
        with ReadOnlyRepository(store) as repo:
            receipt = tuple(repo.iter_evidence("001"))[0]
            diagnostic = receipt.assessment_diagnostic
            assert diagnostic is not None
            if scenario in {"refusal", "multiple", "oversized"}:
                assert diagnostic.retention == "withheld"
                assert diagnostic.artifact is None
                assert diagnostic.content_sha256 is None
            if scenario in {"truncated", "reasoning", "unsafe_metadata"}:
                assert diagnostic.retention == "retained"
                assert diagnostic.artifact is not None
                assert receipt.identity.observation_id is not None
                assert (
                    inspect_assessment_output(
                        "001",
                        receipt.identity.observation_id,
                        store=store,
                        include_text=True,
                    ).output_text
                    == text.encode()
                )
            assert not receipt.reasoning
        for p in store.rglob("*"):
            if p.is_file():
                data = p.read_bytes()
                for canary in (
                    b"REFUSAL_PRIVATE_CANARY",
                    b"UNKNOWN_PRIVATE_CANARY",
                    b"SECOND_PRIVATE_CANARY",
                    b"REASONING_PRIVATE_CANARY",
                    b"UNKNOWN_METADATA_CANARY",
                    b"DIAGNOSTIC_OVERSIZE_CANARY",
                ):
                    # Existing proven-safe complete envelopes may retain their
                    # recognized text components under full capture. The new
                    # separate diagnostic remains withheld for ambiguity/size.
                    if (
                        scenario == "multiple" and canary == b"SECOND_PRIVATE_CANARY"
                    ) or (
                        scenario == "oversized"
                        and canary == b"DIAGNOSTIC_OVERSIZE_CANARY"
                    ):
                        continue
                    assert canary not in data
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_fresh_process_inspection_and_corrupt_artifact(tmp_path: Path) -> None:
    from test_openai_lifecycle import setup_case

    text = "UNVALIDATED_PRIVATE_CANARY {"
    spec, store, adapter, _ = setup_case(tmp_path, "responses", output=text)
    try:
        await execute_run("001", store=store, spec=spec, adapter=adapter)
        with ReadOnlyRepository(store) as repo:
            diagnostic = tuple(repo.iter_evidence("001"))[0].assessment_diagnostic
            assert diagnostic is not None and diagnostic.artifact is not None
            observation_id = diagnostic.identity.observation_id
            run_dir = repo.run_directory("001")
        assert observation_id is not None
        before = {str(p): p.read_bytes() for p in store.rglob("*") if p.is_file()}
        script = """
import sys, socket, hashlib
from judgmentllm.assessment_inspection import inspect_assessment_output
socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network forbidden'))
r=inspect_assessment_output('001',sys.argv[2],store=sys.argv[1])
assert r.output_text is None
r=inspect_assessment_output('001',sys.argv[2],store=sys.argv[1],include_text=True)
assert r.output_text is not None
print(hashlib.sha256(r.output_text).hexdigest())
"""
        process = subprocess.run(
            [sys.executable, "-B", "-c", script, str(store), observation_id],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=30,
        )
        assert process.stdout.strip() == hashlib.sha256(text.encode()).hexdigest()
        assert text not in process.stdout + process.stderr
        assert before == {
            str(p): p.read_bytes() for p in store.rglob("*") if p.is_file()
        }
        assert diagnostic.artifact.relative_path is not None
        artifact_path = run_dir / diagnostic.artifact.relative_path
        artifact_path.write_bytes(b"corrupt")
        with pytest.raises(ArtifactIntegrityError):
            inspect_assessment_output(
                "001", observation_id, store=store, include_text=True
            )
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_diagnostic_record_identity_policy_and_version_are_strict(
    tmp_path: Path,
) -> None:
    from test_openai_lifecycle import setup_case

    from judgmentllm.evidence.assessment import AssessmentDiagnosticV1
    from judgmentllm.persistence.evidence import EvidenceReceipt
    from judgmentllm.persistence.hashing import canonical_bytes

    spec, store, adapter, _ = setup_case(
        tmp_path, "responses", output="PRIVATE_FAILED_OUTPUT {"
    )
    try:
        await execute_run("001", store=store, spec=spec, adapter=adapter)
        with ReadOnlyRepository(store) as repo:
            receipt = tuple(repo.iter_evidence("001"))[0]
        diagnostic = receipt.assessment_diagnostic
        assert diagnostic is not None and diagnostic.artifact is not None
        for value in (True, 1.0, "1", 2):
            document = diagnostic.model_dump(mode="json")
            document["diagnostic_version"] = value
            with pytest.raises(ValueError, match="Invalid evidence record"):
                AssessmentDiagnosticV1.model_validate_json(json.dumps(document))
        for change in (
            {"capture": "minimal"},
            {"canonical_valid": True},
            {"parser_diagnostic": "valid"},
            {"content_sha256": "a" * 64},
        ):
            with pytest.raises(ValueError):
                AssessmentDiagnosticV1.model_validate_json(
                    json.dumps(diagnostic.model_dump(mode="json") | change)
                )
        for change in (
            {
                "identity": diagnostic.identity.model_dump(mode="json")
                | {"run_id": "other"}
            },
            {"endpoint": "chat.completions"},
            {"requested_model": "other"},
            {"remote_response_id": "other"},
            {"reported_model": "other"},
        ):
            document = receipt.model_dump(mode="json")
            document["assessment_diagnostic"] = (
                diagnostic.model_dump(mode="json") | change
            )
            with pytest.raises(ValueError):
                EvidenceReceipt.model_validate_json(json.dumps(document))
        # Optional extension absence preserves historical receipt serialization.
        legacy = receipt.model_dump(mode="json")
        del legacy["assessment_diagnostic"]
        old = EvidenceReceipt.model_validate_json(json.dumps(legacy))
        assert old.assessment_diagnostic is None
        assert b'"assessment_diagnostic"' not in canonical_bytes(old)
        with pytest.raises(ValueError, match="include_text must be a boolean"):
            inspect_assessment_output(
                "001",
                diagnostic.identity.observation_id or "",
                store=store,
                include_text=cast(Any, 1),
            )
    finally:
        await adapter.aclose()
