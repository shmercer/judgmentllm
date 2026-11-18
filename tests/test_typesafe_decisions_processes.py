"""Real process death and credential-free installed-console Jev recovery."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx2
import pytest
from test_typesafe_decisions import CONTEXT_MESSAGE, jev_spec, native_spec

from judgmentllm.config import RunSpecV4
from judgmentllm.execution import core, execute_run
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.evidence import (
    DecisionsEvidenceReceiptV4,
    read_evidence_receipt,
)
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.protocol import PreparedRequest, ProviderAcknowledgmentV4
from judgmentllm.providers.typesafe_decisions import TypeSafeDecisionsAdapter

HOOK = Path(__file__).parent / "fixtures/typesafe_decisions"


def cli(
    root: Path,
    *args: str,
    fault: str = "",
    response: str = "filtered",
    key: str | None = None,
    json_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "SystemRoot",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
        )
        if key in os.environ
    }
    env.update(
        PYTHONPATH=str(HOOK),
        PYTHONDONTWRITEBYTECODE="1",
        JLLM_TYPESAFE_FIXTURE_ROOT=str(root),
        JLLM_TYPESAFE_FIXTURE_FAULT=fault,
        JLLM_TYPESAFE_FIXTURE_RESPONSE=response,
    )
    if key is not None:
        env["TYPESAFE_API_KEY"] = key
    elif args[0] == "run" or "--retry-task" in args:
        env["TYPESAFE_API_KEY"] = "synthetic-jev-key"
    console = Path(sys.executable).with_name(
        "judgmentllm.exe" if os.name == "nt" else "judgmentllm"
    )
    result = subprocess.run(
        [
            str(console),
            *args,
            "--store",
            str(root / "store"),
            *(["--json"] if json_output else []),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "UNSAFE_JEV_CANARY" not in result.stdout + result.stderr
    assert "Error in sitecustomize" not in result.stderr
    return result


def control(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode in (0, 3), (result.stdout, result.stderr)
    data = json.loads(result.stdout)
    assert data["schema_version"] == 1
    return data


def config_path(root: Path, spec: RunSpecV4) -> str:
    path = root / "run.json"
    path.write_text(spec.model_dump_json(), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    "fault,capture,statuses",
    [
        ("forward", "full", []),
        ("after_acknowledgment_receipt", "full", []),
        ("after_evidence_receipt", "full", ["complete", "invalid", "complete"]),
        ("during_result_transaction", "full", ["complete", "invalid", "complete"]),
        ("after_evidence_receipt", "minimal", []),
        ("after_failure_evidence_receipt", "full", ["errored"] * 3),
    ],
)
def test_fresh_process_recovery(
    tmp_path: Path,
    fault: str,
    capture: str,
    statuses: list[str],
) -> None:
    spec = jev_spec(native_spec(tmp_path / "input", capture=capture))
    crash = cli(
        tmp_path,
        "run",
        "--config",
        config_path(tmp_path, spec),
        fault=fault,
        response="rejection"
        if fault == "after_failure_evidence_receipt"
        else "filtered",
    )
    assert crash.returncode == 79, crash.stderr
    store = tmp_path / "store"
    with Repository(store) as repository:
        run_id = str(
            repository.connection.execute("SELECT run_id FROM runs").fetchone()[0]
        )
    first = control(cli(tmp_path, "resume", "--run", run_id))
    second = control(cli(tmp_path, "resume", "--run", run_id))
    detail = second["data"]["decisions"]
    assert detail == first["data"]["decisions"]
    assert second["data"]["recovery_required"] == (not statuses)
    if fault == "after_failure_evidence_receipt":
        assert detail["diagnostics"][0]["message"] == CONTEXT_MESSAGE
    with Repository(store) as repository:
        assert [row.status for row in repository.iter_results(run_id)] == statuses
        count = repository.connection.execute(
            "SELECT count(*) FROM usage_revisions"
        ).fetchone()[0]
        assert count == int(bool(statuses) or capture == "minimal")
        for path in repository.run_directory(run_id).rglob("*"):
            if path.is_file():
                assert b"UNSAFE_JEV_CANARY" not in path.read_bytes()
    assert (tmp_path / "forwards").read_text().splitlines() == ["systemone"]


def test_installed_cli_context_rejection_preserves_diagnostic(tmp_path: Path) -> None:
    spec = jev_spec(native_spec(tmp_path / "input"))
    first = control(
        cli(
            tmp_path,
            "run",
            "--config",
            config_path(tmp_path, spec),
            response="rejection",
        )
    )
    run_id = first["run_id"]
    diagnostics = first["data"]["decisions"]["diagnostics"]
    assert len(diagnostics) == 1
    assert diagnostics[0]["code"] == "max_tokens_exceeded"
    assert diagnostics[0]["message"] == CONTEXT_MESSAGE
    assert (
        control(cli(tmp_path, "resume", "--run", run_id))["data"]["decisions"][
            "diagnostics"
        ]
        == diagnostics
    )
    human = cli(tmp_path, "resume", "--run", run_id, json_output=False)
    assert human.returncode == 3
    assert CONTEXT_MESSAGE in human.stdout
    assert "max_tokens_exceeded" in human.stdout
    with Repository(tmp_path / "store") as repository:
        assert [row.status for row in repository.iter_results(run_id)] == [
            "errored"
        ] * 3
        (submission,) = repository.iter_submissions(run_id)
        receipt = repository.get_evidence_for_submission(
            run_id, submission.submission_id
        )
        assert isinstance(receipt, DecisionsEvidenceReceiptV4)
        assert receipt.usage.input_tokens is None
        assert repository.usage_high_water_mark(run_id) == 1
        assert receipt.rejection is not None
        raw = b'{"detail":{"error_type":"max_tokens_exceeded","message":"UNSAFE_JEV_CANARY"}}'
        assert receipt.rejection.response_sha256 == sha256(raw).hexdigest()
        assert receipt.rejection.response_byte_length == len(raw)
    assert (tmp_path / "forwards").read_text().splitlines() == ["systemone"]


@pytest.mark.parametrize("prepared_intent", [False, True])
def test_invalid_sdk_key_leaves_prepared_work_untouched(
    tmp_path: Path, prepared_intent: bool
) -> None:
    spec = jev_spec(native_spec(tmp_path / "input"))
    config = config_path(tmp_path, spec)
    store = tmp_path / "store"
    args = ("run", "--config", config)
    if prepared_intent:
        assert cli(tmp_path, *args, fault="after_initial_submission").returncode == 79
        with Repository(store) as repository:
            interrupted_id = str(
                repository.connection.execute("SELECT run_id FROM runs").fetchone()[0]
            )
        args = ("resume", "--run", interrupted_id)
    first = cli(tmp_path, *args, key="UNSAFE_JEV_CANARY-é")
    assert first.returncode == 2, first.stderr
    error = json.loads(first.stdout)
    assert error["error"]["category"] == "validation"
    run_id = error["run_id"]
    with Repository(store) as repository:
        (original,) = repository.iter_attempts(run_id)
        assert original.state == "prepared"
        assert [record.certainty for record in repository.iter_submissions(run_id)] == (
            ["not_started"] if prepared_intent else []
        )
        assert repository.usage_high_water_mark(run_id) == 0
    assert not (tmp_path / "forwards").exists()
    resumed = control(
        cli(
            tmp_path,
            "resume",
            "--run",
            run_id,
            key="synthetic-jev-corrected",
            response="complete",
        )
    )
    assert resumed["status"] == "ok"
    assert control(cli(tmp_path, "resume", "--run", run_id))["status"] == "ok"
    with Repository(store) as repository:
        (attempt,) = repository.iter_attempts(run_id)
        assert attempt.attempt_id == original.attempt_id
        assert attempt.payload_sha256 == original.payload_sha256
        assert repository.usage_high_water_mark(run_id) == 1
    assert (tmp_path / "forwards").read_text().splitlines() == ["systemone"]


def test_invalid_sdk_key_cannot_authorize_subset_retry(tmp_path: Path) -> None:
    spec = jev_spec(native_spec(tmp_path / "input"))
    run_id = control(cli(tmp_path, "run", "--config", config_path(tmp_path, spec)))[
        "run_id"
    ]
    store = tmp_path / "store"
    with Repository(store) as repository:
        (failed,) = (
            row for row in repository.iter_results(run_id) if row.status == "invalid"
        )

    def counts() -> tuple[int, ...]:
        with Repository(store) as repository:
            return tuple(
                repository.connection.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                for table in ("retry_operations", "attempts", "submissions")
            )

    retry = (
        "resume",
        "--run",
        run_id,
        "--retry-task",
        failed.task_id,
        "--retry-id",
        "retry",
    )
    assert counts() == (0, 1, 1)
    refused = cli(tmp_path, *retry, key="UNSAFE_JEV_CANARY-é")
    assert refused.returncode == 2, refused.stderr
    assert json.loads(refused.stdout)["error"]["category"] == "validation"
    assert counts() == (0, 1, 1)
    assert (tmp_path / "forwards").read_text().splitlines() == ["systemone"]
    assert control(cli(tmp_path, *retry, response="complete"))["status"] == "ok"
    assert counts() == (1, 2, 2)
    assert (tmp_path / "forwards").read_text().splitlines() == ["systemone"] * 2


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_model_conflict_reopen_keeps_physical_evidence(
    tmp_path: Path, capture: str
) -> None:
    spec = jev_spec(native_spec(tmp_path / "input", capture=capture))
    crash = capture == "minimal"
    first = cli(
        tmp_path,
        "run",
        "--config",
        config_path(tmp_path, spec),
        response="model_conflict",
        fault="after_evidence_receipt" if crash else "",
    )
    assert first.returncode == (79 if crash else 1), first.stderr
    if not crash:
        assert json.loads(first.stdout)["error"]["category"] == "execution"
    store = tmp_path / "store"
    with Repository(store) as repository:
        run_id = str(
            repository.connection.execute("SELECT run_id FROM runs").fetchone()[0]
        )
        (submission,) = repository.iter_submissions(run_id)
        artifacts = ArtifactStore(repository.run_directory(run_id), create=False)
        original = read_evidence_receipt(artifacts, submission.submission_id)
        assert isinstance(original, DecisionsEvidenceReceiptV4)
        assert original.validation is not None
        assert "model_conflict" in original.validation.findings
        assert original.original is not None
        raw = b'{"model":"UNSAFE_JEV_CANARY","answers":{},"usage":{"input_tokens":120}}'
        assert original.original.sha256 == sha256(raw).hexdigest()
        assert original.original.byte_length == len(raw)
        assert original.retained is not None
        assert (original.retained.relative_path is None) == (capture == "minimal")
        if capture == "full":
            assert b"UNSAFE_JEV_CANARY" not in artifacts.read(original.retained)
        assert repository.usage_high_water_mark(run_id) == int(not crash)
    for _ in range(2):
        resumed = cli(tmp_path, "resume", "--run", run_id)
        assert resumed.returncode == 1, resumed.stderr
        assert json.loads(resumed.stdout)["error"]["category"] == "execution"
        with Repository(store) as repository:
            assert list(repository.iter_results(run_id)) == []
            assert repository.usage_high_water_mark(run_id) == 1
            assert (
                repository.get_evidence_for_submission(run_id, submission.submission_id)
                == original
            )
    assert (tmp_path / "forwards").read_text().splitlines() == ["systemone"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["before_eof", "stream_close", "queued", "write_error", "sibling_error"]
)
async def test_rejection_write_drains_under_ownership_before_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-jev-key")
    sibling_error = phase == "sibling_error"
    secondary_error = phase in {"write_error", "sibling_error"}
    block_write = phase in {"queued", "write_error", "sibling_error"}
    run_id = "001"
    store = tmp_path / "store"
    base = native_spec(tmp_path / "input", sources=1 + int(sibling_error))
    spec = jev_spec(
        base,
        execution={
            **base.execution.model_dump(),
            "concurrency": 1 + int(sibling_error),
        },
    )
    with prepare_run(spec, store=store, run_id=run_id):
        pass

    paused = asyncio.Event()
    queued = asyncio.Event()
    sibling_entered = asyncio.Event()
    release_writer = threading.Event()
    submitted_writes: list[Future[object]] = []
    writes: list[str] = []
    transport_tasks: list[asyncio.Task[Any] | None] = []
    submit_tasks: list[asyncio.Task[Any] | None] = []
    primary = IntegrityError("synthetic sibling failure")
    raw = b'{"detail":"UNSAFE_JEV_CANARY"}'

    def hold_queue_head() -> None:
        assert release_writer.wait(15)

    def owned_executor(**kwargs: Any) -> ThreadPoolExecutor:
        executor = ThreadPoolExecutor(**kwargs)
        submit = executor.submit

        def enqueue(function: Callable[[], object]) -> Future[object]:
            if getattr(function, "keywords", {}).get("rejection") is not None:
                if block_write:
                    submit(hold_queue_head)
                pending = submit(function)
                submitted_writes.append(pending)
                queued.set()
                return pending
            return submit(function)

        monkeypatch.setattr(executor, "submit", enqueue)
        return executor

    class RejectionStream(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield raw
            if phase == "before_eof":
                paused.set()
                await asyncio.Event().wait()

        async def aclose(self) -> None:
            if phase == "stream_close":
                paused.set()
                await asyncio.Event().wait()

    async def respond(request: httpx2.Request) -> httpx2.Response:
        transport_tasks.append(asyncio.current_task())
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        with (tmp_path / "forwards").open("a", encoding="utf-8") as forwarded:
            forwarded.write("systemone\n")
        if sibling_error:
            await sibling_entered.wait()
        return httpx2.Response(413, stream=RejectionStream())

    class Adapter(TypeSafeDecisionsAdapter):
        async def submit(
            self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
        ) -> ProviderAcknowledgmentV4:
            if sibling_error and b"Synthetic source 1." in prepared.payload:
                sibling_entered.set()
                await queued.wait()
                raise primary
            submit_tasks.append(asyncio.current_task())
            return await super().submit(
                prepared, submission_id=submission_id, correlation_id=correlation_id
            )

    def failure_boundary(stage: str, request_uid: str) -> None:
        if stage == "after_failure_evidence_receipt":
            writes.append(request_uid)
            if secondary_error:
                raise OSError("synthetic failure after rejection receipt")

    monkeypatch.setattr(core, "ThreadPoolExecutor", owned_executor)
    adapter = Adapter(
        spec.model,
        concurrency=spec.execution.concurrency,
        http_transport=httpx2.MockTransport(respond),
    )
    task = asyncio.create_task(
        execute_run(run_id, store=store, adapter=adapter, fault=failure_boundary)
    )
    try:
        try:
            await asyncio.wait_for((queued if block_write else paused).wait(), 10)
            if block_write:
                assert len(submitted_writes) == 1
                assert not submitted_writes[0].running()
            if not sibling_error:
                task.cancel()
            if block_write:
                competing = await asyncio.to_thread(
                    cli, tmp_path, "resume", "--run", run_id
                )
                assert competing.returncode == 1, competing.stderr
                assert json.loads(competing.stdout)["error"]["message"] == (
                    "run executor ownership is unavailable"
                )
                assert not task.done() and not submitted_writes[0].done()
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                    assert not task.done()
        finally:
            release_writer.set()
        if sibling_error:
            with pytest.raises(ExceptionGroup) as group:
                await asyncio.wait_for(task, 10)
            assert group.value.exceptions == (primary,)
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await adapter.aclose()

    assert len(transport_tasks) == 1 and transport_tasks == submit_tasks
    complete = phase != "before_eof"
    assert len(submitted_writes) == len(writes) == int(complete)
    assert all(write.done() and not write.cancelled() for write in submitted_writes)
    with Repository(store) as repository:
        assert repository.get_owner(run_id) is None
        if not sibling_error:
            assert repository.get_run(run_id).state == "cancelled"
        assert repository.usage_high_water_mark(run_id) == int(
            complete and not secondary_error
        )
        if complete and not secondary_error:
            assert [row.status for row in repository.iter_results(run_id)] == [
                "errored"
            ] * 3

    resumed = control(cli(tmp_path, "resume", "--run", run_id))
    assert resumed["data"]["recovery_required"] == (not complete or sibling_error)
    if complete:
        (diagnostic,) = resumed["data"]["decisions"]["diagnostics"]
        assert diagnostic["code"] == "payload_too_large"
        assert "HTTP body was too large" in diagnostic["message"]
    with Repository(store) as repository:
        assert repository.usage_high_water_mark(run_id) == int(complete)
        assert [row.status for row in repository.iter_results(run_id)] == [
            "errored"
        ] * (3 if complete else 0)
        if complete:
            (receipt,) = repository.iter_evidence(run_id)
            assert isinstance(receipt, DecisionsEvidenceReceiptV4)
            assert receipt.rejection is not None
            assert receipt.rejection.response_sha256 == sha256(raw).hexdigest()
    assert (tmp_path / "forwards").read_text().splitlines() == ["systemone"]
