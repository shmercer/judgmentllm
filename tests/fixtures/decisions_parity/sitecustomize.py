"""Add named outcomes and request receipts to the existing offline SDK hooks."""

import json
import os
import runpy
from pathlib import Path
from typing import Any

import httpx2

provider = os.environ["DECISIONS_PARITY_PROVIDER"]
assert provider in {"openai", "typesafe", "gemini"}
hook = Path(__file__).parent.parent / f"{provider}_decisions/sitecustomize.py"
respond = runpy.run_path(str(hook))["respond"]


def set_probabilities(answer: dict[str, Any], probabilities: dict[str, float]) -> None:
    if provider == "openai":
        for entry in answer["probabilities"]:
            entry["probability"] = probabilities[str(entry["value"])]
    else:
        answer["probabilities"] = probabilities


def respond_with_parity(request: httpx2.Request) -> httpx2.Response:
    body = json.loads(request.content)
    response = respond(request).json()
    questions: dict[str, dict[str, Any]] = body["questions"]
    answers: dict[str, dict[str, Any]] = response["answers"]
    if provider == "openai":
        questions = {q["name"]: q for q in body["questions"]}
        answers = {a["name"]: a for a in response["answers"]}
    assert len(questions) == len(body["questions"])
    distinct: list[str] = []
    for key, question in questions.items():
        instructions = question["instructions"]
        if "Assess criterion 5." in instructions:
            answers[key].update(score=0.45, confidence=0.65)
            set_probabilities(answers[key], {"0": 0.65, "1": 0.25, "2": 0.1})
        elif "Compare criterion 2 independently." in instructions:
            answers[key].update(choice="essay_01")
            set_probabilities(answers[key], {"essay_01": 0.8, "essay_02": 0.2})
        else:
            continue
        distinct.append(key)

    shared = body["state" if provider == "typesafe" else "input"]
    assert isinstance(shared, str)
    with Path("wire.jsonl").open("a", encoding="utf-8") as log:
        log.write(
            json.dumps(
                dict(
                    keys=list(questions),
                    distinct=distinct,
                    shared_input_bytes=len(shared.encode("utf-8")),
                )
            )
            + "\n"
        )
    return httpx2.Response(200, json=response)


httpx2.AsyncHTTPTransport = lambda **_kwargs: httpx2.MockTransport(respond_with_parity)  # type: ignore[assignment]
