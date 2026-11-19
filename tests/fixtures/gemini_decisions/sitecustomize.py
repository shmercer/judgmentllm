"""Block provider sockets and intercept the real SDK in fresh CLI processes."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from traceback import walk_tb
from typing import Any

import httpx2

from judgmentllm.execution.session import ExecutionSession

root = Path(os.environ["JLLM_GEMINI_DECISIONS_ROOT"])
fault = os.environ["JLLM_GEMINI_DECISIONS_FAULT"]
scenario = os.environ["JLLM_GEMINI_DECISIONS_SCENARIO"]


def forbidden(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("Provider sockets are blocked in the CLI fixture")


socket.getaddrinfo = forbidden
socket.create_connection = forbidden
_connect = socket.socket.connect


def connect(sock: socket.socket, address: Any) -> Any:
    # The Windows asyncio wakeup channel uses a literal loopback socket pair.
    if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in (
        "127.0.0.1",
        "::1",
    ):
        return forbidden()
    return _connect(sock, address)


socket.socket.connect = connect


if os.environ.get("JLLM_GEMINI_DECISIONS_TRACE_ASYNCIO") == "1":
    _run = asyncio.run

    def trace_run(*args: Any, **kwargs: Any) -> Any:
        try:
            return _run(*args, **kwargs)
        except Exception as error:
            # Keep the original failure location without exception text or locals.
            print(f"Gemini asyncio exception: {type(error).__name__}", file=sys.stderr)
            for frame, line in walk_tb(error.__traceback__):
                code = frame.f_code
                print(f"  {code.co_filename}:{line} in {code.co_name}", file=sys.stderr)
            raise

    asyncio.run = trace_run


def respond(request: httpx2.Request) -> httpx2.Response:
    if scenario == "diagnostics":
        assert request.method == "GET"
        page_size = request.url.params["pageSize"]
        assert page_size in ("1", "1000")
        assert (
            str(request.url)
            == f"https://generativelanguage.googleapis.com/v1beta/models?pageSize={page_size}"
        )
        assert request.headers["x-goog-api-key"] == "SYNTHETIC_CLI_GEMINI_KEY"
        with (root / "forwards").open("a", encoding="utf-8") as stream:
            stream.write(f"models-{page_size}\n")
        return httpx2.Response(200, json={"models": [{"name": "models/visible"}]})
    assert (
        str(request.url) == "https://generativelanguage.googleapis.com/v1beta/decisions"
    )
    assert request.headers["x-goog-api-key"] == "SYNTHETIC_CLI_GEMINI_KEY"
    assert len(request.content) < 262_144
    with (root / "forwards").open("a", encoding="utf-8") as stream:
        stream.write("decisions\n")
    if fault == "forward":
        os._exit(79)
    if scenario in {"malformed-key", "malformed-nested-key"}:
        prefix = b"\\" if scenario == "malformed-key" else b"\\\\"
        return httpx2.Response(
            200,
            content=b'\xff{"message":"' + prefix + b'u0053YNTHETIC_CLI_GEMINI_KEY"}',
        )
    if scenario in {
        "context",
        "message-only-context",
        "unknown",
        "truncated",
        "400-refusal",
        "conflicting-403",
    }:
        message = "The input token count (262145) exceeds the maximum number of tokens allowed (262144)."
        error = {
            "400-refusal": dict(code="safety", message="GEMINI_UNSAFE_CANARY"),
            "conflicting-403": dict(
                code=500, status="INTERNAL", message="GEMINI_UNSAFE_CANARY"
            ),
            "message-only-context": dict(message=message),
            "unknown": dict(
                code=400,
                status="INVALID_ARGUMENT",
                message="GEMINI_UNSAFE_CANARY mentioned context window in question text.",
            ),
        }.get(scenario, dict(code=400, status="INVALID_ARGUMENT", message=message))
        encoded = json.dumps({"error": error}).encode()
        return httpx2.Response(
            403 if scenario == "conflicting-403" else 400,
            content=encoded,
            headers={"content-length": str(len(encoded) + 1)}
            if scenario == "truncated"
            else {},
        )
    if scenario == "200-refusal":
        return httpx2.Response(
            200,
            json={
                "answers": {},
                "prompt_feedback": {
                    "block_reason": "safety",
                    "block_reason_message": "GEMINI_UNSAFE_CANARY",
                },
            },
        )
    body = json.loads(request.content)
    answers: dict[str, dict[str, Any]] = {}
    for i, (key, question) in enumerate(body["questions"].items()):
        answers[key] = (
            dict(
                type="score",
                score=1.55 + (0.000001 if scenario == "filtered" else 0),
                confidence=0.65,
                probabilities={"2": 0.65, "0": 0.1, "1": 0.25},
                legend={str(k): v for k, v in enumerate(question["criteria"])},
            )
            if question["type"] == "score"
            else dict(
                type="choice",
                choice="essay_02",
                confidence=0.8,
                probabilities={"essay_02": 0.8, "essay_01": 0.2},
            )
        )
        if i == 1 and scenario == "filtered":
            answers[key]["unknown"] = "GEMINI_UNSAFE_CANARY"
        if i == 1 and scenario in {"overflow", "oversized-integer"}:
            answers[key]["score"] = "overflow_number"
        if i == 0 and scenario == "oversized-integer":
            answers[key].update(
                score=2, confidence=1, probabilities={"0": 0, "1": 0, "2": 1}
            )
    result = dict(
        answers=dict(reversed(tuple(answers.items()))),
        usage={"total_input_tokens": 120, "total_output_tokens": 0},
    )
    if scenario in {"overflow", "oversized-integer"}:
        number = "1e400" if scenario == "overflow" else "9" * 4301
        return httpx2.Response(
            200,
            content=json.dumps(result).replace('"overflow_number"', number).encode(),
        )
    return httpx2.Response(200, json=result)


httpx2.AsyncHTTPTransport = lambda **_kwargs: httpx2.MockTransport(respond)  # type: ignore[assignment]
_checkpoint = ExecutionSession.checkpoint


def checkpoint(self: ExecutionSession, stage: str, *args: Any) -> None:
    if stage == fault:
        os._exit(79)
    _checkpoint(self, stage, *args)


ExecutionSession.checkpoint = checkpoint  # pyright: ignore[reportAttributeAccessIssue]
