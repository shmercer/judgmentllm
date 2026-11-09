"""Non-UTF-8 assessment scalars through the real offline SDK HTTP boundary."""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from typing import Any, Literal

import httpx2
import pytest
import pytest_asyncio
from test_anthropic_lifecycle import envelope, production_spec

from judgmentllm.anthropic_transport import production_context
from judgmentllm.assessment_inspection import inspect_assessment_output
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.anthropic import AnthropicAdapter


@pytest_asyncio.fixture(autouse=True)
async def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "UTF8_SYNTHETIC_AUTH_CANARY")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("External network is forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.mark.parametrize("endpoint", ["messages"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("text", ["\ud800", "\udfff", "", " \t\n"])
@pytest.mark.asyncio
async def test_escaped_surrogate_and_empty_text_do_not_lose_observation(
    tmp_path: Path,
    endpoint: str,
    capture: Literal["full", "minimal"],
    text: str,
) -> None:
    spec = production_spec(
        tmp_path / "i", endpoint, capture=capture, traits=1, explanations=False
    )
    store = tmp_path / "s"
    with prepare_run(
        spec, store=store, run_id="001", transport_context=production_context(spec)
    ):
        pass
    body = envelope(endpoint, text)
    # The HTTP body is valid ASCII JSON containing an escaped surrogate. Passing
    # it as json= would make the test HTTP transport encode invalid Unicode.
    raw = json.dumps(body, ensure_ascii=True).encode("ascii")
    calls = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(
            200,
            content=raw,
            headers={"content-type": "application/json"},
            request=request,
        )

    adapter = AnthropicAdapter(
        endpoint, spec.model, http_transport=httpx2.MockTransport(respond)
    )
    invalid_utf8 = bool(text and 0xD800 <= ord(text[0]) <= 0xDFFF)
    try:
        report = await execute_run("001", store=store, spec=spec, adapter=adapter)
        assert not report.recovery_required
        with ReadOnlyRepository(store) as repository:
            evidence = tuple(repository.iter_evidence("001"))[0]
            diagnostic = evidence.assessment_diagnostic
            assert diagnostic is not None
            assert diagnostic.parser_diagnostic == (
                "invalid_utf8" if invalid_utf8 else "no_text"
            )
            assert not diagnostic.canonical_valid
            assert diagnostic.component.extraction_status == (
                "invalid_utf8" if invalid_utf8 else "extracted"
            )
            assert diagnostic.provider_outcome == (
                "completed" if invalid_utf8 else "missing_output"
            )
            assert all(
                row.status != "complete" for row in repository.iter_results("001")
            )
            assert evidence.usage.input_tokens is not None
            assert evidence.usage.output_tokens is not None
            submission = repository.get_submission(
                "001", evidence.identity.submission_id
            )
            assert submission.certainty == "accepted"
            observation_id = evidence.identity.observation_id
            assert observation_id is not None
            if invalid_utf8:
                assert diagnostic.retention == "withheld"
                assert diagnostic.byte_length is None
                assert diagnostic.content_sha256 is diagnostic.artifact is None
                assert evidence.original is None
            else:
                assert diagnostic.byte_length == len(text.encode())
                assert (
                    diagnostic.content_sha256
                    == hashlib.sha256(text.encode()).hexdigest()
                )
                assert diagnostic.retention == (
                    "retained" if capture == "full" else "hash_only"
                )
        inspected = inspect_assessment_output(
            "001", observation_id, store=store, include_text=True
        )
        assert inspected.output_text == (
            text.encode() if not invalid_utf8 and capture == "full" else None
        )
        await recover_run("001", store=store, spec=spec, adapter=adapter)
        assert calls == 1
        for path in store.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                assert b"UTF8_SYNTHETIC_AUTH_CANARY" not in data
                if invalid_utf8:
                    assert b"\\ud800" not in data and b"\\udfff" not in data
    finally:
        await adapter.aclose()
