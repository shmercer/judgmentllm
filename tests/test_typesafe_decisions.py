"""Native Jev contracts through the real locked SDK and local lifecycle."""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from test_decisions_contracts import pair_spec
from test_decisions_lifecycle import native_spec
from typer.testing import CliRunner

from judgmentllm.cli import app
from judgmentllm.config import RunSpecV4
from judgmentllm.lifecycle import (
    RetrySelection,
    estimate_run,
    get_run,
    resume_run,
    run_live,
    validate_inputs,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run

CONTEXT_MESSAGE = (
    "TypeSafe Jev rejected the request because it exceeded a context limit. "
    "Start a new run with smaller shared input or, for grouped requests, a lower "
    "`execution.request_grouping.max_questions_per_request`. "
    "The original attempt is terminal."
)


def jev_spec(base: RunSpecV4, **changes: Any) -> RunSpecV4:
    return RunSpecV4.model_validate(
        {
            **base.model_dump(),
            "provider": "typesafe",
            "endpoint": "systemone",
            "model": "jev-1.14.0",
            **changes,
        }
    )


def answer_map(body: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (
            dict(
                type="score",
                score=1.55,
                confidence=0.8,
                legend={str(i): value for i, value in enumerate(question["criteria"])},
                probabilities={"2": 0.65, "0": 0.10, "1": 0.25},
            )
            if question["type"] == "score"
            else dict(
                type="choice",
                choice="essay_02",
                confidence=0.7,
                probabilities={"essay_02": 0.8, "essay_01": 0.2},
            )
        )
        for key, question in reversed(tuple(body["questions"].items()))
    }


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-jev-key")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("unexpected network access")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_offline_plan_exposes_unverified_context_and_exact_scope(
    tmp_path: Path,
) -> None:
    spec = jev_spec(native_spec(tmp_path))
    report = validate_inputs(spec)
    assert report.valid
    assert any(i.code == "context_fit_unverified" for i in report.issues)
    plan = estimate_run(spec).model_dump()
    assert plan["requests"] == 1
    assert plan["limits"]["request_context_fit"] == "unverified"
    assert plan["limits"]["question_context_fit"] == "unverified"
    assert plan["estimated_input_tokens"] is None
    assert plan["cache_report"] == "unknown"


@pytest.mark.parametrize(
    "change", [{"strict_capabilities": True}, {"model": "jev-latest"}]
)
def test_unapproved_scope_is_refused_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    from judgmentllm.lifecycle import LifecycleError
    from judgmentllm.providers import typesafe_decisions

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("unapproved scope reached credential resolution")

    monkeypatch.setattr(typesafe_decisions, "_resolve_credentials", forbidden)
    spec = jev_spec(native_spec(tmp_path), **change)
    with pytest.raises(LifecycleError, match="context|model"):
        run_live(spec, store=tmp_path / "store")


@pytest.mark.parametrize(
    "status,detail,reason",
    [
        (
            400,
            {"code": "max_tokens_exceeded", "message": "UNSAFE_JEV_CANARY"},
            "max_tokens_exceeded",
        ),
        (413, "UNSAFE_JEV_CANARY", "payload_too_large"),
    ],
)
def test_sdk_rejection_is_terminal_and_survives_cli_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    detail: Any,
    reason: str,
) -> None:
    from judgmentllm.decisions.provenance import export_decisions_provenance
    from judgmentllm.lifecycle import LifecycleError

    spec = jev_spec(native_spec(tmp_path))
    store = tmp_path / "store"
    raw = json.dumps({"detail": detail}).encode()
    calls: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        assert len(json.loads(request.content)["questions"]) == 3
        return httpx2.Response(status, content=raw)

    def transport(**_kwargs: object) -> httpx2.MockTransport:
        return httpx2.MockTransport(respond)

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport)
    result = run_live(spec, store=store)
    assert result.outcome == "partial" and not result.recovery_required
    assert result.decisions is not None
    diagnostic = result.decisions.model_dump()["diagnostics"][0]
    assert diagnostic["code"] == reason
    if status == 400:
        assert diagnostic["message"] == CONTEXT_MESSAGE
    else:
        assert "body" in diagnostic["message"]
    with Repository(store) as repository:
        rows = list(repository.iter_results(result.run_id))
        assert [row.status for row in rows] == ["errored"] * 3
        provenance = export_decisions_provenance(result.run_id, store=store)
        physical = next(
            file for file in provenance.manifest.files if file.name == "physical.jsonl"
        )
        path = repository.run_directory(result.run_id) / (
            physical.artifact.relative_path or ""
        )
        receipt = json.loads(path.read_bytes())["rejection"]
        assert receipt["response_sha256"] == sha256(raw).hexdigest()
        assert receipt["http_status"] == status and receipt["reason"] == reason
        assert receipt["attempt_id"] == rows[0].attempt_id
        assert "UNSAFE_JEV_CANARY" not in path.read_text()
    assert get_run(result.run_id, store=store).decisions is not None
    assert (
        resume_run(result.run_id, store=store).decisions
        == get_run(result.run_id, store=store).decisions
    )
    resumed = CliRunner().invoke(
        app, ["resume", "--run", result.run_id, "--store", str(store), "--json"]
    )
    assert resumed.exit_code == 3
    control = json.loads(resumed.stdout)
    assert control["data"]["decisions"]["diagnostics"] == [diagnostic]
    with pytest.raises(LifecycleError, match="retry selection"):
        resume_run(
            result.run_id,
            store=store,
            retry=RetrySelection(task_ids=(rows[0].task_id,), operation_id="retry"),
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "case",
    [
        "score",
        "sdk_decode",
        "duplicate",
        "wrong_model",
        "choice",
        "drop",
        "incomplete_rejection",
        "redirect",
        "overload",
    ],
)
def test_sdk_named_answers_and_physical_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    base = (
        pair_spec(tmp_path, criteria=13) if case == "choice" else native_spec(tmp_path)
    )
    spec = jev_spec(base)
    store = tmp_path / "store"
    calls: list[httpx2.Request] = []
    uncertain = case in {"drop", "incomplete_rejection", "redirect", "overload"}
    sdk_errors: list[Exception] = []

    if case == "sdk_decode":
        from typesafe_sdk import AsyncTypeSafeClient, TypeSafeAPIResponseValidationError

        actual = cast(Any, AsyncTypeSafeClient.system_one)  # pyright: ignore[reportUnknownMemberType]

        async def probe(client: AsyncTypeSafeClient, *args: Any, **kwargs: Any) -> Any:
            try:
                return await actual(client, *args, **kwargs)
            except TypeSafeAPIResponseValidationError as error:
                sdk_errors.append(error)
                raise

        monkeypatch.setattr(AsyncTypeSafeClient, "system_one", probe)

    class Incomplete(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"detail":{"code":"max_tokens_exceeded"}}'
            raise httpx2.ReadError("EOF was not established")

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        assert request.method == "POST"
        assert request.headers["authorization"] == "Bearer synthetic-jev-key"
        assert "content-encoding" not in request.headers
        body = json.loads(request.content)
        assert set(body) == {"model", "state", "questions"}
        if case == "drop":
            raise httpx2.ReadError("UNSAFE_JEV_CANARY")
        if case == "incomplete_rejection":
            return httpx2.Response(400, stream=Incomplete())
        if case == "redirect":
            return httpx2.Response(302, headers={"location": "https://unused.invalid/"})
        if case == "overload":
            return httpx2.Response(529)
        answers = answer_map(body)
        model = "jev-1.13.0" if case == "wrong_model" else body["model"]
        doc = dict(
            model=model, answers=answers, usage=dict(input_tokens=120, output_tokens=0)
        )
        if case == "sdk_decode":
            answers[next(iter(body["questions"]))]["confidence"] = "UNSAFE_JEV_CANARY"
        if case == "duplicate":
            key = next(iter(answers))
            body_bytes = (
                json.dumps(doc)
                .replace(f'"{key}":', f'"{key}": null, "{key}":', 1)
                .encode()
            )
            return httpx2.Response(200, content=body_bytes)
        return httpx2.Response(
            200, json=doc, headers={"x-typesafe-request-id": "jev-synthetic-001"}
        )

    def transport(**_kwargs: object) -> httpx2.MockTransport:
        return httpx2.MockTransport(respond)

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport)
    caplog.set_level(logging.DEBUG, logger="typesafe_sdk")
    result = run_live(spec, store=store)
    assert "UNSAFE_JEV_CANARY" not in caplog.text
    with Repository(store) as repository:
        rows = list(repository.iter_results(result.run_id))
        if uncertain:
            assert result.recovery_required and rows == []
        elif case == "choice":
            assert len(rows) == 52 and all(row.status == "complete" for row in rows)
            for row in rows:
                detail = row.model_dump()
                assert detail["winner_id"] == detail["presented_order"][1]
                assert detail["p_left"] == (
                    0.8 if detail["winner_id"] == detail["left_item_id"] else 0.2
                )
        else:
            assert [row.status for row in rows] == {
                "score": ["complete"] * 3,
                "sdk_decode": ["invalid", "complete", "complete"],
                "duplicate": ["invalid"] * 3,
                "wrong_model": ["invalid"] * 3,
            }[case]
            for row in rows:
                if row.status == "complete":
                    assert row.model_dump()["primary_score"] == pytest.approx(5.15)
        assert repository.connection.execute(
            "SELECT count(*) FROM usage_revisions"
        ).fetchone()[0] == (0 if uncertain else len(calls))
    resume_run(result.run_id, store=store)
    assert len(calls) == (4 if case == "choice" else 1)
    assert len(sdk_errors) == int(case == "sdk_decode")


@pytest.mark.parametrize(
    "count,maximum,groups",
    [(1, None, [1]), (2, 32, [2]), (4, 3, [3, 1]), (12, 32, [12])],
)
def test_jev_grouping_uses_the_existing_user_maximum(
    tmp_path: Path, count: int, maximum: int | None, groups: list[int]
) -> None:
    spec = jev_spec(native_spec(tmp_path, traits=count, maximum=maximum))
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        assert [
            len(item.request.tasks) for item in prepared.iter_request_dependencies()
        ] == groups


@pytest.mark.parametrize("levels", [2, 10, 11])
def test_jev_score_level_limit_is_enforced_in_offline_preflight(
    tmp_path: Path, levels: int
) -> None:
    source = native_spec(tmp_path, traits=1)
    values = source.model_dump()
    values["rubric"]["traits"][0]["levels"] = [
        {"label": str(i), "description": f"Level {i}"} for i in range(levels)
    ]
    report = validate_inputs(jev_spec(RunSpecV4.model_validate(values)))
    assert report.valid is (levels <= 10)


def test_jev_body_splits_reuse_exact_shared_state(tmp_path: Path) -> None:
    from judgmentllm.decisions.records import (
        TypeSafeDecisionsContextV1,
        TypeSafeDecisionsLimitsV3,
    )
    from judgmentllm.decisions.transport import plan_source, serialize_request
    from judgmentllm.models import ResponseItem

    def unexpected_item(_item_id: str) -> ResponseItem:
        pytest.fail("one rubric source needs no pair lookup")

    spec = jev_spec(native_spec(tmp_path))
    item = ResponseItem(item_id="001", text="Complete partagé essay.")
    normal = TypeSafeDecisionsContextV1()
    (first,) = plan_source(spec, item, unexpected_item, normal, None)
    exact_two = len(serialize_request(item.text, first.questions[:2], spec, normal))
    split = TypeSafeDecisionsContextV1(
        limits=TypeSafeDecisionsLimitsV3(local_body_bytes=exact_two)
    )
    dependencies = list(plan_source(spec, item, unexpected_item, split, None))
    assert [len(group.questions) for group in dependencies] == [2, 1]
    assert all(group.shared_input == item.text for group in dependencies)
    assert [q.key for group in dependencies for q in group.questions] == [
        q.key for q in first.questions
    ]
    too_small = TypeSafeDecisionsContextV1(
        limits=TypeSafeDecisionsLimitsV3(local_body_bytes=1)
    )
    with pytest.raises(ValueError, match="oversized Decisions member"):
        next(plan_source(spec, item, unexpected_item, too_small, None))
