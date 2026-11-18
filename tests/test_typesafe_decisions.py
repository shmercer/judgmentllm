"""Native Jev contracts through the real locked SDK and local lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import httpcore2
import httpx2
import pytest
from test_decisions_contracts import pair_spec
from test_decisions_lifecycle import native_spec
from typer.testing import CliRunner

from judgmentllm.cli import app
from judgmentllm.config import RunSpecV4
from judgmentllm.execution.session import ExecutionSession
from judgmentllm.lifecycle import (
    LifecycleError,
    RetrySelection,
    arun_live,
    estimate_run,
    get_run,
    resume_run,
    run_live,
    validate_inputs,
)
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.decisions_invocations import (
    DecisionsInvocationV3,
    read_invocation,
)
from judgmentllm.persistence.evidence import DecisionsEvidenceReceiptV4
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
        (
            400,
            {"error_type": "max_tokens_exceeded", "message": "UNSAFE_JEV_CANARY"},
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
        assert receipt["response_byte_length"] == len(raw)
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
    "raw,reason",
    [
        (
            b'{"detail":{"error_type":"bad_input","message":"max_tokens_exceeded"}}',
            "request_rejected",
        ),
        (b'{"detail":"max_tokens_exceeded"}', "max_tokens_exceeded"),
        (b'{"code":"max_tokens_exceeded"}', "max_tokens_exceeded"),
        (
            b'{"detail":{"error_type":"bad_input","error_type":"max_tokens_exceeded"}}',
            None,
        ),
        (b'{"detail":{"error_type":"bad_input","code":"max_tokens_exceeded"}}', None),
        (b'{"detail":{"error_type":"max_tokens_exceeded"},"code":"bad_input"}', None),
    ],
    ids=["generic", "scalar", "flat", "duplicate", "nested-conflict", "outer-conflict"],
)
def test_400_reason_requires_unambiguous_raw_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: bytes, reason: str | None
) -> None:
    forwards: list[str] = []

    def reject(request: httpx2.Request) -> httpx2.Response:
        forwards.append(str(request.url))
        return httpx2.Response(400, content=raw)

    def transport(**_kwargs: object) -> httpx2.MockTransport:
        return httpx2.MockTransport(reject)

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport)
    store = tmp_path / "store"
    result = run_live(jev_spec(native_spec(tmp_path)), store=store)
    reopened = resume_run(result.run_id, store=store)
    assert result.recovery_required == reopened.recovery_required == (reason is None)
    assert result.decisions == reopened.decisions
    with Repository(store) as repository:
        (submission,) = repository.iter_submissions(result.run_id)
        evidence = repository.get_evidence_for_submission(
            result.run_id, submission.submission_id
        )
        if reason is None:
            assert evidence is None
            artifacts = ArtifactStore(
                repository.run_directory(result.run_id), create=False
            )
            assert isinstance(
                read_invocation(artifacts, submission.submission_id),
                DecisionsInvocationV3,
            )
            assert list(repository.iter_results(result.run_id)) == []
        else:
            assert isinstance(evidence, DecisionsEvidenceReceiptV4)
            assert evidence.rejection is not None
            assert evidence.rejection.reason == reason
            assert [r.status for r in repository.iter_results(result.run_id)] == [
                "errored"
            ] * 3
    assert forwards == ["https://api.typesafe.ai/v1/systemone"]


def test_sdk_and_guard_use_credential_validated_before_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typesafe_sdk import AsyncTypeSafeClient

    sdk_keys: list[str] = []
    headers: list[str] = []
    initialize = AsyncTypeSafeClient.__init__
    checkpoint = ExecutionSession.checkpoint
    rotations: list[str] = []

    def rotate(session: ExecutionSession, stage: str, request_uid: str = "") -> None:
        if stage == "after_initial_submission":
            monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-jev-rotated")
            rotations.append(request_uid)
        checkpoint(session, stage, request_uid)

    def sdk_init(client: AsyncTypeSafeClient, **kwargs: Any) -> None:
        sdk_keys.append(kwargs["api_key"])
        initialize(client, **kwargs)

    def respond(request: httpx2.Request) -> httpx2.Response:
        headers.append(request.headers["authorization"])
        body = json.loads(request.content)
        return httpx2.Response(
            200, json=dict(model=body["model"], answers=answer_map(body))
        )

    def transport(**_kwargs: object) -> httpx2.MockTransport:
        return httpx2.MockTransport(respond)

    monkeypatch.setattr(ExecutionSession, "checkpoint", rotate)
    monkeypatch.setattr(AsyncTypeSafeClient, "__init__", sdk_init)
    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport)
    result = run_live(jev_spec(native_spec(tmp_path)), store=tmp_path / "store")
    assert result.outcome == "ok"
    assert len(rotations) == 1
    assert sdk_keys == ["synthetic-jev-key"]
    assert headers == ["Bearer synthetic-jev-key"]


@pytest.mark.asyncio
async def test_jev_http1_logging_stays_private_during_overlap_and_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_transport = httpx2.AsyncHTTPTransport
    body = b'{"detail":"UNSAFE_JEV_BODY"}'
    headers = (
        b"HTTP/1.1 413 Payload Too Large\r\nX-Private: UNSAFE_JEV_HEADER\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
    )

    class PausedStream(httpcore2.AsyncMockStream):
        def __init__(self) -> None:
            super().__init__([headers, body])
            self.reads = 0
            self.waiting_for_body = asyncio.Event()
            self.release_body = asyncio.Event()
            self.closed = False

        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            self.reads += 1
            if self.reads == 2:
                self.waiting_for_body.set()
                await self.release_body.wait()
            return await super().read(max_bytes, timeout)

        async def aclose(self) -> None:
            self.closed = True
            logging.getLogger("httpcore2.connection").debug("UNSAFE_JEV_TEARDOWN")
            await super().aclose()

    class Backend(httpcore2.AsyncMockBackend):
        def __init__(self, stream: PausedStream) -> None:
            super().__init__([])
            self.stream = stream

        async def connect_tcp(
            self, *_args: Any, **_kwargs: Any
        ) -> httpcore2.AsyncNetworkStream:
            return self.stream

    def transport(backend: httpcore2.AsyncNetworkBackend) -> httpx2.AsyncHTTPTransport:
        client_transport = real_transport(verify=False, trust_env=False)
        monkeypatch.setattr(
            client_transport,
            "_pool",
            httpcore2.AsyncConnectionPool(network_backend=backend),
        )
        return client_transport

    async def unrelated(label: str) -> None:
        raw = f"HTTP/1.1 204 No Content\r\nX-Trace: {label}\r\n\r\n".encode()
        async with httpx2.AsyncClient(
            transport=transport(httpcore2.AsyncMockBackend([raw])), trust_env=False
        ) as client:
            assert (await client.get(f"https://{label}.invalid/")).status_code == 204
        logging.getLogger("typesafe_sdk").debug("unrelated SDK log %s", label)

    first, second = PausedStream(), PausedStream()
    jev_transports = iter((transport(Backend(first)), transport(Backend(second))))

    def jev_transport(**_kwargs: object) -> httpx2.AsyncHTTPTransport:
        return next(jev_transports)

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", jev_transport)
    loggers = ("typesafe_sdk", "httpx2", "httpcore2.connection", "httpcore2.http11")
    for logger in loggers:
        caplog.set_level(logging.DEBUG, logger=logger)
    tasks: list[asyncio.Task[Any]] = []
    try:
        async with asyncio.timeout(10):
            for index, stream in enumerate((first, second)):
                root = tmp_path / str(index)
                tasks.append(
                    asyncio.create_task(
                        arun_live(jev_spec(native_spec(root)), store=root / "store")
                    )
                )
                await stream.waiting_for_body.wait()
            await unrelated("unrelated-during")
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            second.release_body.set()
            completed = await tasks[1]
            assert completed.outcome == "partial" and not completed.recovery_required
            await unrelated("unrelated-after")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert first.closed and second.closed
    assert "UNSAFE_JEV_" not in caplog.text
    for label in ("unrelated-during", "unrelated-after"):
        assert {
            record.name for record in caplog.records if label in record.getMessage()
        } >= set(loggers)


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
    if case == "wrong_model":
        with pytest.raises(LifecycleError) as conflict:
            run_live(spec, store=store)
        assert conflict.value.category == "execution"
        run_id = conflict.value.run_id
        assert run_id is not None
        with pytest.raises(LifecycleError) as reopened:
            resume_run(run_id, store=store)
        assert reopened.value.category == "execution"
        with Repository(store) as repository:
            assert list(repository.iter_results(run_id)) == []
            assert repository.usage_high_water_mark(run_id) == 1
        assert len(calls) == 1
        return
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
