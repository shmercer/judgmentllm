"""Test-only interception around the ordinary installed CLI and actual SDK."""

import json
import os
import socket
from pathlib import Path
from typing import Any

import httpx2

import judgmentllm
import judgmentllm.preparation as preparation
from judgmentllm.execution.session import ExecutionSession

ROOT = Path(os.environ["DECISIONS_FIXTURE_ROOT"])
(ROOT / "import-origin.txt").write_text(str(Path(judgmentllm.__file__).resolve()))
CANARY = "UNSAFE_WIRE_CANARY"


def forbidden(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("Provider sockets blocked in fixture")


socket.getaddrinfo = forbidden
socket.create_connection = forbidden


def respond(request: httpx2.Request) -> httpx2.Response:
    with (ROOT / "forwarding.jsonl").open("a") as stream:
        stream.write(json.dumps({"path": request.url.path}) + "\n")
    body = json.loads(request.content)
    answers: list[dict[str, Any]] = []
    for index, question in enumerate(body["questions"]):
        if question["type"] == "score":
            answer = dict(
                name=question["name"],
                type="score",
                score=1.55,
                confidence=0.8,
                probabilities=[
                    dict(value=i, label=level["label"], probability=p)
                    for i, (level, p) in enumerate(
                        zip(question["levels"], (0.1, 0.25, 0.65), strict=True)
                    )
                ],
            )
        else:
            answer = dict(
                name=question["name"],
                type="choice",
                choice="essay_02",
                confidence=0.7,
                probabilities=[
                    dict(value="essay_01", probability=0.2),
                    dict(value="essay_02", probability=0.8),
                ],
            )
        if os.environ.get("DECISIONS_FIXTURE_FILTER") == "1" and index == 1:
            answer["unsafe"] = CANARY
        answers.append(answer)
    if os.environ.get("DECISIONS_FIXTURE_FILTER") == "1":
        answers.insert(1, {"name": CANARY, "type": "refusal"})
    return httpx2.Response(
        200,
        json=dict(
            model=body["model"],
            answers=answers,
            usage=dict(
                input_tokens=120,
                input_tokens_details=dict(cached_tokens=0, cache_write_tokens=0),
                output_tokens=4,
                output_tokens_details=dict(reasoning_tokens=0),
                total_tokens=124,
            ),
        ),
    )


httpx2.AsyncHTTPTransport = lambda **_kwargs: httpx2.MockTransport(respond)  # type: ignore[assignment]
_original_checkpoint = ExecutionSession.checkpoint


def checkpoint(self: ExecutionSession, stage: str, *args: Any) -> None:
    if stage == os.environ.get("DECISIONS_FIXTURE_CRASH"):
        os._exit(79)
    _original_checkpoint(self, stage, *args)


ExecutionSession.checkpoint = checkpoint  # pyright: ignore[reportAttributeAccessIssue]
_original_preparation_checkpoint = preparation._checkpoint  # pyright: ignore[reportPrivateUsage]


def preparation_checkpoint(stage: str, *args: Any) -> None:
    if stage == os.environ.get("DECISIONS_FIXTURE_CRASH"):
        os._exit(79)
    _original_preparation_checkpoint(stage, *args)


preparation._checkpoint = preparation_checkpoint  # pyright: ignore[reportPrivateUsage]
