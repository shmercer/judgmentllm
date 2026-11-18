"""Test-only network guard for fresh installed-console processes."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

import httpx2

from judgmentllm.execution.session import ExecutionSession

root = Path(os.environ["JLLM_TYPESAFE_FIXTURE_ROOT"])
fault = os.environ["JLLM_TYPESAFE_FIXTURE_FAULT"]
scenario = os.environ["JLLM_TYPESAFE_FIXTURE_RESPONSE"]


def forbidden(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("Provider sockets are blocked in the fixture")


socket.getaddrinfo = forbidden
socket.create_connection = forbidden


def respond(request: httpx2.Request) -> httpx2.Response:
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    with (root / "forwards").open("a", encoding="utf-8") as stream:
        stream.write("systemone\n")
    if fault == "forward":
        os._exit(79)
    if scenario == "rejection":
        return httpx2.Response(400, json={"detail": {"code": "max_tokens_exceeded"}})
    body = json.loads(request.content)
    answers: dict[str, dict[str, Any]] = {}
    for i, (key, question) in enumerate(body["questions"].items()):
        answers[key] = (
            dict(
                type="score",
                score=1.55,
                confidence=0.8,
                legend={str(k): v for k, v in enumerate(question["criteria"])},
                probabilities={"0": 0.1, "1": 0.25, "2": 0.65},
            )
            if question["type"] == "score"
            else dict(
                type="choice",
                choice="essay_02",
                confidence=0.7,
                probabilities={"essay_01": 0.2, "essay_02": 0.8},
            )
        )
        if scenario == "filtered" and i == 1:
            answers[key]["extra"] = "UNSAFE_JEV_CANARY"
    return httpx2.Response(
        200,
        json=dict(model=body["model"], answers=answers, usage={"input_tokens": 120}),
    )


httpx2.AsyncHTTPTransport = lambda **_kwargs: httpx2.MockTransport(respond)  # type: ignore[assignment]
_checkpoint = ExecutionSession.checkpoint


def checkpoint(self: ExecutionSession, stage: str, *args: Any) -> None:
    if stage == fault:
        os._exit(79)
    _checkpoint(self, stage, *args)


ExecutionSession.checkpoint = checkpoint  # pyright: ignore[reportAttributeAccessIssue]
