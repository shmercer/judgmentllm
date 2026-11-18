"""Native Jev contracts through the real locked SDK and local lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import AsyncIterator, Callable
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
from judgmentllm.decisions.transport import PreparedDecisionsRequestV3
from judgmentllm.execution import execute_run
from judgmentllm.execution.session import ExecutionSession
from judgmentllm.lifecycle import (
    LifecycleError,
    MockExecutionOptions,
    RetrySelection,
    aresume_run,
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
from judgmentllm.persistence.receipts import read_acknowledgment
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import PreparationError, prepare_run
from judgmentllm.providers import typesafe_decisions
from judgmentllm.providers.protocol import (
    PreparedRequest,
    ProviderError,
    ProviderIntegrityError,
    RetrievalUnavailable,
)
from judgmentllm.providers.typesafe_decisions import TypeSafeDecisionsAdapter
from judgmentllm.providers.typesafe_decisions_response import canonical_answer

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


def ten_level_jev_spec(base: RunSpecV4) -> RunSpecV4:
    rubric = base.model_dump()["rubric"]
    levels = [
        dict(label=f"{i:02d}", description=f"Level {i}", numeric_value=i**2)
        for i in range(10)
    ]
    for trait in rubric["traits"]:
        trait["levels"] = levels
    return jev_spec(base, rubric=rubric)


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


def jev_envelope(request: httpx2.Request) -> dict[str, Any]:
    body = json.loads(request.content)
    return dict(
        model=body["model"],
        answers=answer_map(body),
        usage=dict(input_tokens=120, output_tokens=0),
    )


def intercept_jev(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[httpx2.Request], httpx2.Response],
) -> None:
    def transport(**_kwargs: object) -> httpx2.MockTransport:
        return httpx2.MockTransport(respond)

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport)


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
    from judgmentllm.providers import typesafe_decisions

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("unapproved scope reached credentials or transport")

    monkeypatch.setattr(typesafe_decisions, "_resolve_credentials", forbidden)
    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", forbidden)
    spec = jev_spec(native_spec(tmp_path), **change)
    diagnostic = (
        "TypeSafe Jev requires exact model jev-1.14.0."
        if "model" in change
        else "TypeSafe Jev context fit is unverified under strict capabilities."
    )
    with pytest.raises(PreparationError) as rejected_preparation:
        prepare_run(spec, store=tmp_path / "prepare", run_id="001")
    assert str(rejected_preparation.value) == diagnostic
    with pytest.raises(LifecycleError) as rejected_run:
        run_live(spec, store=tmp_path / "store")
    assert rejected_run.value.category == "unsupported_capability"
    assert rejected_run.value.message == diagnostic


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


@pytest.mark.parametrize("mode", ["single", "grouped"])
def test_jev_score_precision_preserves_native_values_and_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    # Only the numeric tuples come from the synthetic Jev grid.
    values = (
        [(1.95, (0.01, 0.02, 0.97), 6.84)]
        if mode == "single"
        else [
            (1.26, (0.36, 0.03, 0.61), 4.69),
            (0.04, (0.98, 0.01, 0.01), 1.07),
            (1.57, (0.10, 0.25, 0.65), None),
        ]
    )
    spec = jev_spec(
        native_spec(
            tmp_path, traits=len(values), maximum=None if mode == "single" else 32
        )
    )
    store = tmp_path / "store"
    responses: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        doc = jev_envelope(request)
        for key, (index, probabilities, _) in zip(
            body["questions"], values, strict=True
        ):
            doc["answers"][key].update(
                score=index,
                probabilities={str(i): p for i, p in enumerate(probabilities)},
            )
        raw = json.dumps(doc).encode()
        responses.append(raw)
        return httpx2.Response(200, content=raw)

    intercept_jev(monkeypatch, respond)
    result = run_live(spec, store=store)
    assert not result.recovery_required
    (raw,) = responses
    with Repository(store) as repository:
        rows = list(repository.iter_results(result.run_id))
        for row, (index, probabilities, mean) in zip(rows, values, strict=True):
            assert row.task_kind == "rubric"
            if mean is None:
                assert row.status == "invalid" and row.error_kind == "invalid_output"
            else:
                assert row.status == "complete"
                assert row.provider_expected_index == index
                assert row.primary_score == pytest.approx(mean)
                assert [p.probability for p in row.probabilities or []] == list(
                    probabilities
                )
                assert row.category_label is None and row.confidence == 0.8
        (receipt,) = repository.iter_evidence(result.run_id)
        assert isinstance(receipt, DecisionsEvidenceReceiptV4)
        assert (
            receipt.original is not None
            and receipt.original.sha256 == sha256(raw).hexdigest()
        )
        assert receipt.retained is not None and receipt.validation is not None
        assert receipt.usage.input_tokens == 120
        assert repository.usage_high_water_mark(result.run_id) == 1
        artifacts = ArtifactStore(repository.run_directory(result.run_id), create=False)
        retained = artifacts.read(receipt.retained)
        if mode == "single":
            assert receipt.retained_kind == "original" and retained == raw
        else:
            assert receipt.retained_kind == "filtered_replay"
            replay = json.loads(retained)
            assert [answer is None for answer in replay["answers"]] == [
                False,
                False,
                True,
            ]
            assert receipt.validation.members[-1].failure == "invalid_output"


@pytest.mark.parametrize(
    "score,probabilities,accepted",
    [
        pytest.param(1.94, (0.01, 0.02, 0.97), False, id="two-cents"),
        pytest.param(1.951, (0.01, 0.02, 0.97), False, id="off-grid-score"),
        pytest.param(1.95, (0.0101, 0.0199, 0.97), False, id="off-grid-probabilities"),
        pytest.param(1.95, (0.0, 0.02, 0.97), False, id="unbalanced-mass"),
        pytest.param(2.01, (0.0, 0.0, 1.0), False, id="out-of-range"),
        pytest.param(0.96, (0.02, 0.98), False, id="two-level"),
        pytest.param(0.97, (0.02, 0.98), True, id="two-level-contact"),
        pytest.param(0.98, (0.02, 0.98), True, id="strict-two-level"),
        pytest.param(1.9599, (0.0101, 0.0199, 0.97), True, id="strict-precise"),
        pytest.param(1.94, (0.01, 0.03, 0.96), True, id="captured-group-high"),
        pytest.param(1.98, (0.0, 0.01, 0.99), True, id="captured-group-peak"),
        pytest.param(0.01, (0.99, 0.0, 0.01), True, id="captured-group-low"),
        pytest.param(0.04, (0.97, 0.01, 0.02), True, id="captured-group-repeat"),
        pytest.param(1.48, (0.25,) * 4, True, id="four-lower"),
        pytest.param(1.47, (0.25,) * 4, False, id="four-below"),
        pytest.param(1.52, (0.25,) * 4, True, id="four-upper"),
        pytest.param(1.53, (0.25,) * 4, False, id="four-above"),
        pytest.param(1.97, (0.20,) * 5, True, id="five-lower"),
        pytest.param(1.96, (0.20,) * 5, False, id="five-below"),
        pytest.param(2.03, (0.20,) * 5, True, id="five-upper"),
        pytest.param(2.04, (0.20,) * 5, False, id="five-above"),
        pytest.param(4.37, (0.10,) * 10, True, id="ten-lower-contact"),
        pytest.param(4.36, (0.10,) * 10, False, id="ten-below"),
        pytest.param(4.63, (0.10,) * 10, True, id="ten-upper-contact"),
        pytest.param(4.64, (0.10,) * 10, False, id="ten-above"),
        pytest.param(0.05, (1.0,) + (0.0,) * 9, True, id="low-peak-contact"),
        pytest.param(0.06, (1.0,) + (0.0,) * 9, False, id="low-peak-outside"),
        pytest.param(8.95, (0.0,) * 9 + (1.0,), True, id="high-peak-contact"),
        pytest.param(8.94, (0.0,) * 9 + (1.0,), False, id="high-peak-outside"),
        pytest.param(4.51, (0.11,) + (0.10,) * 9, False, id="excess-mass"),
        pytest.param(0.0003, (0.9999, 0.0, 0.0, 0.0001), True, id="strict-tiny-mass"),
        pytest.param(0.01, (0.9999, 0.0, 0.0, 0.0001), False, id="off-grid-tiny-mass"),
        pytest.param(
            1.51, (0.25, 0.25, 0.25, 0.249999999), False, id="almost-unit-mass"
        ),
        pytest.param(4.51, (0.10,) * 10 + (0.0,), False, id="eleven-level"),
    ],
)
def test_jev_score_precision_requires_exact_grid_and_discrepancy_bound(
    score: float, probabilities: tuple[float, ...], accepted: bool
) -> None:
    criteria = [
        dict(label=str(i), description="Synthetic") for i in range(len(probabilities))
    ]
    question = dict(type="score", criteria=criteria)
    answer = dict(
        type="score",
        score=score,
        confidence=0.8,
        probabilities={str(i): p for i, p in enumerate(probabilities)},
        legend={str(i): criterion for i, criterion in enumerate(criteria)},
    )
    if accepted:
        assert (
            canonical_answer("synthetic", answer, question, 1e-8)["expected_index"]
            == score
        )
    else:
        with pytest.raises(ValueError, match="score conflicts with distribution"):
            canonical_answer("synthetic", answer, question, 1e-8)


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


@pytest.mark.parametrize(
    "field,diagnostic",
    [
        ("model", "TypeSafe Jev requires exact model jev-1.14.0."),
        ("execution.tier", "TypeSafe Jev requires the default service tier."),
        ("rubric", "TypeSafe Jev supports at most ten score levels per trait."),
    ],
)
def test_jev_validation_and_admission_refuse_unsupported_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, diagnostic: str
) -> None:
    base = jev_spec(native_spec(tmp_path))
    values = base.model_dump()
    if field == "model":
        values["model"] = "jev-latest"
    elif field == "execution.tier":
        values["execution"]["tier"] = "priority"
    else:
        values["rubric"]["traits"][0]["levels"] = [
            {"label": str(i), "description": f"Level {i}"} for i in range(11)
        ]
    spec = RunSpecV4.model_validate(values)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("unapproved Jev scope reached credentials or transport")

    monkeypatch.setattr(typesafe_decisions, "_resolve_credentials", forbidden)
    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", forbidden)
    report = validate_inputs(spec)
    assert not report.valid
    assert any(
        issue.field == field and issue.code == "unsupported" for issue in report.issues
    )
    with pytest.raises(PreparationError) as preparation:
        prepare_run(spec, store=tmp_path / "prepare", run_id="001")
    assert str(preparation.value) == diagnostic
    with pytest.raises(LifecycleError) as execution:
        run_live(spec, store=tmp_path / "store")
    assert execution.value.category == "unsupported_capability"
    assert execution.value.message == diagnostic


def test_jev_cannot_use_mock_controls_or_legacy_adapter_entrypoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("unapproved Jev operation reached credentials or transport")

    monkeypatch.setattr(typesafe_decisions, "_resolve_credentials", forbidden)
    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", forbidden)
    base = native_spec(tmp_path)
    spec = jev_spec(base)
    adapter = TypeSafeDecisionsAdapter(spec.model)
    with pytest.raises(
        LifecycleError, match="TypeSafe Jev does not accept mock controls"
    ) as rejected:
        run_live(spec, store=tmp_path / "store", mock=MockExecutionOptions())
    assert rejected.value.category == "validation"
    with pytest.raises(
        typesafe_decisions.TypeSafeConfigurationError, match="endpoint systemone"
    ):
        typesafe_decisions.decisions_context(base)
    with pytest.raises(ProviderIntegrityError, match="runtime scope differs"):
        adapter.validate(jev_spec(base, account_scope="foreign"))
    with pytest.raises(ProviderError, match="sealed preparation"):
        adapter.prepare()
    with pytest.raises(ProviderError, match="named-member interpretation"):
        adapter.parse()


@pytest.mark.parametrize(
    "case,category,diagnostic",
    [
        (
            "sdk",
            "unsupported_capability",
            "TypeSafe Jev requires typesafe-sdk==0.7.5 from the typesafe extra.",
        ),
        (
            "missing",
            "validation",
            "TypeSafe Jev requires the selected TYPESAFE_API_KEY credential.",
        ),
        (
            "invalid",
            "validation",
            "TypeSafe Jev requires a TYPESAFE_API_KEY with printable ASCII and no whitespace.",
        ),
    ],
)
def test_jev_readiness_refusal_leaves_initial_work_without_submission_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    category: str,
    diagnostic: str,
) -> None:
    from importlib.metadata import PackageNotFoundError

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("refused readiness reached the credential resolver or transport")

    def package_missing(name: str) -> str:
        assert name == "typesafe-sdk"
        raise PackageNotFoundError(name)

    def key_missing(provider: str) -> Any:
        assert provider == "typesafe"
        raise ValueError("UNSAFE_JEV_CANARY")

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", forbidden)
    if case == "sdk":
        monkeypatch.setattr(typesafe_decisions, "version", package_missing)
        monkeypatch.setattr(typesafe_decisions, "_resolve_credentials", forbidden)
    elif case == "missing":
        monkeypatch.setattr(typesafe_decisions, "_resolve_credentials", key_missing)
    else:
        monkeypatch.setenv("TYPESAFE_API_KEY", "UNSAFE_JEV_CANARY-é")

    store = tmp_path / "store"
    with pytest.raises(LifecycleError) as refused:
        run_live(jev_spec(native_spec(tmp_path)), store=store)
    assert refused.value.category == category
    assert refused.value.message == diagnostic
    run_id = refused.value.run_id
    assert run_id is not None
    with Repository(store) as repository:
        (attempt,) = repository.iter_attempts(run_id)
        assert attempt.state == "prepared"
        assert list(repository.iter_submissions(run_id)) == []
        assert repository.usage_high_water_mark(run_id) == 0


def sealed_jev(root: Path) -> PreparedDecisionsRequestV3:
    from judgmentllm.preparation.decisions import DecisionsPreparedRun
    from judgmentllm.transport import physical_identity

    with prepare_run(
        jev_spec(native_spec(root)), store=root / "store", run_id="001"
    ) as prepared:
        assert isinstance(prepared, DecisionsPreparedRun)
        (dependency,) = prepared.iter_request_dependencies()
        (attempt,) = prepared.repository.iter_attempts("001")
        wire = prepared.prepare_transport_attempt(
            dependency,
            physical_identity("001", attempt.request_uid, attempt.attempt_id),
        )
    assert isinstance(wire, PreparedDecisionsRequestV3)
    return wire


@pytest.mark.asyncio
async def test_jev_submit_requires_native_wire_identity_and_bound_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("unadmitted Jev submission reached credentials or transport")

    monkeypatch.setattr(typesafe_decisions, "_resolve_credentials", forbidden)
    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", forbidden)
    wire = sealed_jev(tmp_path)
    identity = wire.transport_commitment.identity
    generic = PreparedRequest.model_validate(
        wire.model_dump(include=set(PreparedRequest.model_fields))
    )
    adapter = TypeSafeDecisionsAdapter(wire.model)
    try:
        with pytest.raises(
            ProviderIntegrityError, match="requires production preparation 10"
        ):
            await adapter.submit(
                generic,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
        with pytest.raises(ProviderIntegrityError, match="invocation identity differs"):
            await adapter.submit(
                wire,
                submission_id=identity.submission_id,
                correlation_id="foreign-correlation",
            )
        with pytest.raises(
            ProviderIntegrityError, match="SDK credential was not validated"
        ):
            await adapter.submit(
                wire,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("tamper", ["request_bytes", "second_invocation"])
@pytest.mark.asyncio
async def test_jev_guard_refuses_extra_or_altered_sdk_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    from typesafe_sdk import AsyncTypeSafeClient

    wire = sealed_jev(tmp_path)
    identity = wire.transport_commitment.identity
    forwards: list[bytes] = []
    actual = cast(Any, AsyncTypeSafeClient.system_one)  # pyright: ignore[reportUnknownMemberType]

    async def sdk(client: AsyncTypeSafeClient, **kwargs: Any) -> Any:
        if tamper == "request_bytes":
            return await actual(client, **{**kwargs, "state": "altered state"})
        await actual(client, **kwargs)
        return await actual(client, **kwargs)

    def respond(request: httpx2.Request) -> httpx2.Response:
        forwards.append(request.content)
        return httpx2.Response(200, json=jev_envelope(request))

    monkeypatch.setattr(AsyncTypeSafeClient, "system_one", sdk)
    adapter = TypeSafeDecisionsAdapter(
        wire.model, http_transport=httpx2.MockTransport(respond)
    )
    adapter.validate_credentials()
    ids = dict(
        submission_id=identity.submission_id, correlation_id=identity.correlation_id
    )
    try:
        if tamper == "request_bytes":
            with pytest.raises(
                ProviderIntegrityError, match="forwarding differs from sealed request"
            ):
                await adapter.submit(wire, **ids)
            assert forwards == []
        else:
            ack = await adapter.submit(wire, **ids)
            response = await adapter.retrieve(wire, **ids)
            assert (
                ack.response_evidence.response_sha256
                == sha256(response.raw).hexdigest()
            )
            assert len(adapter.parse_decisions(response, wire).answers) == 3
            assert forwards == [wire.payload]
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_jev_retrieval_requires_the_original_pending_handoff(
    tmp_path: Path,
) -> None:
    wire = sealed_jev(tmp_path)
    identity = wire.transport_commitment.identity
    forwards: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        forwards.append(request.content)
        return httpx2.Response(200, json=jev_envelope(request))

    adapter = TypeSafeDecisionsAdapter(
        wire.model, http_transport=httpx2.MockTransport(respond)
    )
    adapter.validate_credentials()
    ids = dict(
        submission_id=identity.submission_id, correlation_id=identity.correlation_id
    )
    try:
        await adapter.submit(wire, **ids)
        with pytest.raises(ProviderIntegrityError, match="retrieval identity differs"):
            await adapter.retrieve(wire, **{**ids, "correlation_id": "foreign"})
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(wire, **ids)
        assert forwards == [wire.payload]
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("boundary", ["encoding", "raw_size", "filtered_size"])
def test_jev_response_guard_leaves_nonretained_response_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    store = tmp_path / "store"
    raw = b'{"model":"jev-1.14.0","answers":{},"private":"UNSAFE_JEV_CANARY"}'
    if boundary != "encoding":
        # The raw response fits only in the filtered-size case. Its bound
        # derivative includes the immutable replay metadata and is larger.
        monkeypatch.setattr(
            typesafe_decisions,
            "_RESPONSE_LIMIT",
            len(raw) - int(boundary == "raw_size"),
        )
    forwards: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        forwards.append(request.content)
        headers = {"content-encoding": "gzip"} if boundary == "encoding" else None
        return httpx2.Response(200, stream=httpx2.ByteStream(raw), headers=headers)

    intercept_jev(monkeypatch, respond)
    initial = run_live(jev_spec(native_spec(tmp_path)), store=store)
    assert initial.recovery_required
    resumed = resume_run(initial.run_id, store=store)
    assert resumed.recovery_required
    assert len(forwards) == 1
    with Repository(store) as repository:
        (submission,) = repository.iter_submissions(initial.run_id)
        artifacts = ArtifactStore(
            repository.run_directory(initial.run_id), create=False
        )
        fact = read_invocation(artifacts, submission.submission_id)
        assert isinstance(fact, DecisionsInvocationV3)
        assert fact.payload_sha256 == sha256(forwards[0]).hexdigest()
        assert submission.certainty == "unknown"
        assert read_acknowledgment(artifacts, submission.submission_id) is None
        assert (
            repository.get_evidence_for_submission(
                initial.run_id, submission.submission_id
            )
            is None
        )
        assert list(repository.iter_results(initial.run_id)) == []
        assert repository.usage_high_water_mark(initial.run_id) == 0


@pytest.mark.parametrize("status", [200, 529])
@pytest.mark.asyncio
async def test_jev_cancellation_after_transport_eof_preserves_only_established_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    from judgmentllm.decisions.outcomes import read_outcomes
    from judgmentllm.persistence.ownership import executor_is_active
    from judgmentllm.persistence.receipts import AcknowledgmentReceiptV4
    from judgmentllm.persistence.repository import ReadOnlyRepository

    spec = jev_spec(native_spec(tmp_path))
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass

    closing = asyncio.Event()
    forwards: list[bytes] = []

    class ClosingStream(httpx2.AsyncByteStream):
        def __init__(self, raw: bytes) -> None:
            self.raw = raw

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield self.raw

        async def aclose(self) -> None:
            closing.set()
            await asyncio.Event().wait()

    def respond(request: httpx2.Request) -> httpx2.Response:
        forwards.append(request.content)
        document = jev_envelope(request)
        first = next(iter(json.loads(request.content)["questions"]))
        document["answers"][first]["confidence"] = None
        raw = (
            json.dumps(document).encode() if status == 200 else b'{"detail":"overload"}'
        )
        return httpx2.Response(status, stream=ClosingStream(raw))

    def ledger_snapshot() -> tuple[object, ...]:
        with ReadOnlyRepository(store) as repository:
            assert repository.get_owner("001") is None
            assert executor_is_active(repository, "001") is False
            (submission,) = repository.iter_submissions("001")
            submission_id = submission.submission_id
            artifacts = ArtifactStore(repository.run_directory("001"), create=False)
            ack = read_acknowledgment(artifacts, submission_id)
            fact = read_invocation(artifacts, submission_id)
            observation = repository.get_observation_for_submission(
                "001", submission_id
            )
            evidence = repository.get_evidence_for_submission("001", submission_id)
            outcomes = read_outcomes(artifacts, submission_id)
            rows = list(repository.iter_results("001"))
            usage = list(repository.usage_revision_history("001", submission_id))
            if status == 200:
                assert submission.certainty == "accepted"
                assert isinstance(ack, AcknowledgmentReceiptV4)
                assert isinstance(evidence, DecisionsEvidenceReceiptV4)
                assert observation is not None
                assert outcomes is not None
                assert outcomes.observation_id == observation.observation_id
                assert [answer.answer_type for answer in outcomes.answers] == [
                    "invalid",
                    "score",
                    "score",
                ]
                assert [row.status for row in rows] == [
                    "invalid",
                    "complete",
                    "complete",
                ]
                (record,) = usage
                assert record.usage == evidence.usage
                assert record.usage.input_tokens == 120
            else:
                assert (
                    submission.state == "uncertain"
                    and submission.certainty == "unknown"
                )
                assert isinstance(fact, DecisionsInvocationV3)
                assert fact.payload_sha256 == sha256(forwards[0]).hexdigest()
                assert ack is None and observation is None and evidence is None
                assert outcomes is None and rows == [] and usage == []
            return submission, ack, fact, observation, evidence, outcomes, rows, usage

    adapter = TypeSafeDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    task = asyncio.create_task(execute_run("001", store=store, adapter=adapter))
    try:
        await asyncio.wait_for(closing.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert len(forwards) == 1
        settled = ledger_snapshot()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await adapter.aclose()

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("recovery of cancelled work reached credentials or transport")

    monkeypatch.setattr(typesafe_decisions, "_resolve_credentials", forbidden)
    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", forbidden)
    resumed = await aresume_run("001", store=store)
    assert resumed.recovery_required == (status == 529)
    assert len(forwards) == 1
    assert ledger_snapshot() == settled


@pytest.mark.parametrize(
    "case",
    [
        "answer",
        "kind",
        "probabilities",
        "legend",
        "expected_index",
        "probability_sum",
        "choice_keys",
        "envelope",
        "usage",
    ],
)
def test_jev_response_validation_binds_partial_outcomes_and_filtered_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    base = (
        pair_spec(tmp_path, criteria=2)
        if case == "choice_keys"
        else native_spec(tmp_path)
    )
    store = tmp_path / "store"
    sent: dict[str, bytes] = {}

    def respond(request: httpx2.Request) -> httpx2.Response:
        wire = json.loads(request.content)
        key = next(iter(wire["questions"]))
        doc = jev_envelope(request)
        answer = doc["answers"][key]
        if case == "answer":
            doc["answers"][key] = "UNSAFE_JEV_CANARY"
        elif case == "kind":
            answer["type"] = "choice"
        elif case == "probabilities":
            answer["probabilities"] = [0.10, 0.25, 0.65]
        elif case == "legend":
            answer["legend"]["0"]["label"] = "UNSAFE_JEV_CANARY"
        elif case == "expected_index":
            answer["score"] = 1.6
        elif case == "probability_sum":
            answer["probabilities"]["0"] = 0.15
        elif case == "choice_keys":
            del answer["probabilities"]["essay_02"]
        elif case == "envelope":
            doc["answers"] = ["UNSAFE_JEV_CANARY"]
        elif case == "usage":
            doc["usage"] = "UNSAFE_JEV_CANARY"
        raw = json.dumps(doc).encode()
        sent[sha256(raw).hexdigest()] = raw
        return httpx2.Response(200, content=raw)

    intercept_jev(monkeypatch, respond)
    result = run_live(jev_spec(base), store=store)
    assert not result.recovery_required
    with Repository(store) as repository:
        receipts = list(repository.iter_evidence(result.run_id))
        assert len(receipts) == len(sent) == (4 if case == "choice_keys" else 1)
        assert repository.usage_high_water_mark(result.run_id) == len(receipts)
        artifacts = ArtifactStore(repository.run_directory(result.run_id), create=False)
        for receipt in receipts:
            assert isinstance(receipt, DecisionsEvidenceReceiptV4)
            assert receipt.validation is not None and receipt.retained is not None
            assert receipt.original is not None
            raw = sent[receipt.original.sha256]
            assert receipt.original.byte_length == len(raw)
            assert receipt.retained_kind == "filtered_replay"
            assert b"UNSAFE_JEV_CANARY" not in artifacts.read(receipt.retained)
            rows = repository.get_results_for_attempt(
                result.run_id, receipt.identity.attempt_id
            )
            if case == "envelope":
                assert receipt.validation.findings == ("unrecognized_envelope",)
                assert not receipt.validation.recognizable
                assert [row.status for row in rows] == ["invalid"] * 3
            elif case == "usage":
                assert receipt.validation.findings == ("unknown_field",)
                assert (
                    receipt.usage.input_tokens is None
                    and receipt.usage.output_tokens is None
                )
                assert [row.status for row in rows] == ["complete"] * 3
            else:
                assert receipt.validation.recognizable
                assert receipt.validation.members[0].failure == "invalid_output"
                assert [row.status for row in rows] == ["invalid"] + ["complete"] * (
                    1 if case == "choice_keys" else 2
                )
                assert receipt.usage.input_tokens == 120


@pytest.mark.parametrize("fault", ["protocol", "correlation"])
def test_jev_replay_requires_the_sealed_protocol_and_invocation(
    tmp_path: Path, fault: str
) -> None:
    from judgmentllm.decisions.replay import OpenAIDecisionsReplayV1
    from judgmentllm.providers.protocol import ProviderResponse
    from judgmentllm.providers.typesafe_decisions_response import (
        analyze,
        observation_key,
    )

    wire = sealed_jev(tmp_path)
    body = json.loads(wire.payload)
    raw = json.dumps({"model": wire.model, "answers": answer_map(body)}).encode()
    replay, safe, _ = analyze(raw, wire)
    assert safe
    if fault == "protocol":
        other = OpenAIDecisionsReplayV1(
            metadata=replay.metadata, answers=replay.answers
        )
        diagnostic = "replay protocol differs"
    else:
        metadata = replay.metadata.model_copy(update={"correlation_id": "foreign"})
        other = replay.model_copy(update={"metadata": metadata})
        diagnostic = "replay differs from committed invocation"
    response = ProviderResponse(
        observation_key=observation_key(wire),
        kind="response",
        raw=raw,
        decisions_replay=other,
    )
    assert type(response.decisions_replay) is type(other)
    with pytest.raises(ProviderIntegrityError, match=diagnostic):
        TypeSafeDecisionsAdapter(wire.model).parse_decisions(response, wire)
