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

if scenario == "legacy_score_precision":
    from judgmentllm.providers import typesafe_decisions_response

    # Run the old strict check with the same receipt and transport implementation.
    def no_precision_fallback(*_args: object, **_kwargs: object) -> bool:
        return False

    typesafe_decisions_response.matches_rounded_score = no_precision_fallback


def respond(request: httpx2.Request) -> httpx2.Response:
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    with (root / "forwards").open("a", encoding="utf-8") as stream:
        stream.write("systemone\n")
    if fault == "forward":
        os._exit(79)
    if scenario == "rejection":
        return httpx2.Response(
            400,
            content=b'{"detail":{"error_type":"max_tokens_exceeded","message":"UNSAFE_JEV_CANARY"}}',
        )
    if scenario == "model_conflict":
        return httpx2.Response(
            200,
            content=b'{"model":"UNSAFE_JEV_CANARY","answers":{},"usage":{"input_tokens":120}}',
        )
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
        if i == 0 and scenario in {
            "score_precision",
            "legacy_score_precision",
            "filtered_score_precision",
        }:
            answers[key].update(
                score=1.95, probabilities={"0": 0.01, "1": 0.02, "2": 0.97}
            )
        if scenario == "ten_level_score_precision":
            assert len(question["criteria"]) == 10
            answers[key].update(
                score=4.63 if i == 0 else 4.64,
                probabilities={str(j): 0.10 for j in range(10)},
            )
        if i == 1 and scenario in {"filtered", "filtered_score_precision"}:
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
