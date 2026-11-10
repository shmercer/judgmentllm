"""Adversarial source-bound Gemini qualification over synthetic SDK HTTP only."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from gemini_offline import envelope, isolate

from judgmentllm import gemini_qualification as q
from judgmentllm import gemini_qualification_execution as e
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.providers import gemini


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)
    monkeypatch.setattr(q, "_source_commit", lambda: "a" * 40)
    monkeypatch.setattr(e, "_source_commit", lambda: "a" * 40)


@pytest.fixture
def planned(tmp_path: Path) -> tuple[Path, q.GeminiQualificationPlanV1, dict[str, Any]]:
    directory = tmp_path / "plan"
    plan = q.build_plan(directory=directory, source_commit="a" * 40)
    arguments = dict(
        authorized_plan_sha256=bytes_digest((directory / "plan.json").read_bytes()),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
        accept_full_synthetic_capture=True,
    )
    return directory, plan, arguments


def install(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    plan: q.GeminiQualificationPlanV1,
    customize: Callable[[dict[str, Any], int], Any] | None = None,
) -> list[bytes]:
    sent: list[bytes] = []

    def respond(request: Any) -> Any:
        index = next(
            i
            for i, c in enumerate(plan.calls)
            if c.request_body_utf8.encode() == request.content
        )
        call = plan.calls[index]
        assert request.method == "POST"
        assert (
            str(request.url)
            == "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"
        )
        assert request.headers["x-goog-api-key"] == "SYNTHETIC_SELECTED_GEMINI_KEY"
        assert e._consumed(directory, plan, bytes_digest(canonical_bytes(plan))) >= {
            call.case_id
        }
        sent.append(request.content)
        with (directory / "independent-forwards").open("ab", buffering=0) as stream:
            stream.write((call.submission_id + "\n").encode())
            os.fsync(stream.fileno())
        assessment = (
            {"scores": [{"trait_id": "01", "category_label": "high"}]}
            if call.task_kind == "rubric"
            else {"winner_id": "002"}
        )
        body = envelope(
            json.dumps(assessment),
            responseId="synthetic-" + call.case_id,
            modelVersion=plan.model,
            usageMetadata={
                "promptTokenCount": 20,
                "candidatesTokenCount": 11,
                "thoughtsTokenCount": 3,
                "totalTokenCount": 34,
                "cachedContentTokenCount": 0,
            },
        )
        if customize is not None:
            response = customize(body, index)
            if response is not None:
                return response
        return httpx2.Response(200, json=body)

    def transport(**kwargs: Any) -> Any:
        return httpx2.MockTransport(respond)

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport)
    return sent


def files(directory: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(directory)): p.read_bytes()
        for p in directory.rglob("*")
        if p.is_file()
    }


@pytest.mark.asyncio
async def test_all_four_cases_one_shot_deterministic_readonly_and_no_promotion(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, plan, arguments = planned
    sent = install(monkeypatch, directory, plan)
    raw = (directory / "plan.json").read_bytes()
    result = cast(dict[str, Any], await e.execute_plan(directory, **arguments))
    assert len(sent) == result["physical_send_attempts"] == 4
    assert len((directory / "independent-forwards").read_text().splitlines()) == 4
    assert (
        result["live_qualified"]
        and result["overall_qualification_result"] == "qualified"
    )
    assert len(result["successful_cases"]) == 4 and not result["unsuccessful_cases"]
    assert all(
        row["assessment_valid"] and row["response_model_identity_verified"]
        for row in result["calls"]
    )
    assert result["reported_usage_totals"] == dict(
        input_tokens=80,
        output_tokens=44,
        reasoning_tokens=12,
        total_tokens=136,
        cache_read_tokens=0,
    )
    assert result["estimated_actual_usd"] == "0.000104"
    assert (directory / "plan.json").read_bytes() == raw
    consent = json.loads((directory / "execution-authorization.json").read_bytes())
    assert consent["plan_sha256"] == arguments["authorized_plan_sha256"]
    assert (
        consent["full_synthetic_assessment_capture_authorized"]
        and not consent["monetary_cap_provider_enforced"]
    )
    before = files(directory)
    assert q.report_plan(directory) == q.report_plan(directory) == result
    assert files(directory) == before

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Credentials/client forbidden during repeated inspection")

    monkeypatch.setattr(gemini, "_resolve_credentials", forbidden)
    assert await e.execute_plan(directory, **arguments) == result
    assert len(sent) == 4
    from judgmentllm import gemini_registry

    assert gemini_registry._QUALIFIED_PROFILES == ()
    encoded = json.dumps(result)
    assert "SYNTHETIC_SELECTED" not in encoded and "SYNTHETIC_SECONDARY" not in encoded
    assert all(b"SYNTHETIC_SELECTED" not in raw for raw in files(directory).values())


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "missing_digest",
        "source",
        "current_source",
        "billable",
        "capture",
        "truthy",
        "sdk",
        "ceiling",
    ],
)
@pytest.mark.asyncio
async def test_authorization_before_credentials_or_clients(
    planned: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    directory, _plan, arguments = planned

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Credentials/client forbidden before authorization")

    monkeypatch.setattr(gemini, "_resolve_credentials", forbidden)
    monkeypatch.setattr(gemini, "_sdk", forbidden)
    if change == "digest":
        arguments["authorized_plan_sha256"] = "0" * 64
    elif change == "missing_digest":
        arguments["authorized_plan_sha256"] = ""
    elif change == "source":
        arguments["authorized_source_commit"] = "b" * 40
    elif change == "current_source":
        monkeypatch.setattr(e, "_source_commit", lambda: "b" * 40)
    elif change == "billable":
        arguments["accept_billable_calls"] = False
    elif change == "capture":
        arguments["accept_full_synthetic_capture"] = False
    elif change == "truthy":
        arguments["accept_full_synthetic_capture"] = 1
    elif change == "sdk":

        def other_version(package: str) -> str:
            return "other"

        monkeypatch.setattr(e.importlib.metadata, "version", other_version)
    else:
        plan_data = json.loads((directory / "plan.json").read_bytes())
        plan_data["proposed_budget_usd"] = "0.03"
        raw = canonical_bytes(plan_data)
        (directory / "plan.json").write_bytes(raw)
        (directory / "plan.sha256").write_text(bytes_digest(raw) + "\n")
        arguments["authorized_plan_sha256"] = bytes_digest(raw)
    with pytest.raises(q.QualificationError):
        await e.execute_plan(directory, **arguments)
    assert not (directory / "execution-authorization.json").exists()


@pytest.mark.parametrize(
    "kind",
    [
        "malformed",
        "fence",
        "duplicate",
        "category",
        "missing",
        "refusal",
        "truncated",
        "unsupported",
        "multiple",
        "missing_model",
        "alias",
        "prefix",
        "model_substitution",
        "missing_response",
        "unsafe",
        "usage_missing",
        "usage_partial",
        "cache",
        "thinking",
    ],
)
@pytest.mark.asyncio
async def test_assessment_identity_retention_and_usage(
    planned: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    directory, plan, arguments = planned

    def customize(body: dict[str, Any], index: int) -> None:
        if index != 0:
            return
        part = body["candidates"][0]["content"]["parts"][0]
        candidate = body["candidates"][0]
        if kind == "malformed":
            part["text"] = "{broken"
        elif kind == "fence":
            part["text"] = "```json\n" + part["text"] + "\n```"
        elif kind == "duplicate":
            part["text"] = '{"scores":[],"scores":[]}'
        elif kind == "category":
            part["text"] = '{"scores":[{"trait_id":"01","category_label":"invented"}]}'
        elif kind == "missing":
            part["text"] = '{"scores":[]}'
        elif kind == "refusal":
            candidate["finishReason"] = "SAFETY"
        elif kind == "truncated":
            candidate["finishReason"] = "MAX_TOKENS"
        elif kind == "unsupported":
            candidate["finishReason"] = "OTHER"
        elif kind == "multiple":
            candidate["content"]["parts"].append({"text": part["text"]})
        elif kind == "missing_model":
            body.pop("modelVersion")
        elif kind == "alias":
            body["modelVersion"] = plan.model + "-001"
        elif kind == "prefix":
            body["modelVersion"] = "models/" + plan.model
        elif kind == "model_substitution":
            body["modelVersion"] = "unexpected-model"
        elif kind == "missing_response":
            body.pop("responseId")
        elif kind == "unsafe":
            part["text"] = "SYNTHETIC_SELECTED_GEMINI_KEY"
        elif kind == "usage_missing":
            body.pop("usageMetadata")
        elif kind == "usage_partial":
            body["usageMetadata"] = {"promptTokenCount": 20}
        elif kind == "cache":
            body["usageMetadata"]["cachedContentTokenCount"] = 3
        elif kind == "thinking":
            candidate["content"]["parts"].insert(
                0,
                {
                    "thought": True,
                    "text": "PRIVATE_THOUGHT_CANARY",
                    "thoughtSignature": "PRIVATE_SIGNATURE_CANARY",
                },
            )

    sent = install(monkeypatch, directory, plan, customize)
    result = cast(dict[str, Any], await e.execute_plan(directory, **arguments))
    row = result["calls"][0]
    terminal_invalid = kind in {
        "malformed",
        "fence",
        "duplicate",
        "category",
        "missing",
    }
    fully_valid = kind in {"usage_missing", "usage_partial", "cache", "thinking"}
    assert len(sent) == (4 if terminal_invalid or fully_valid else 1)
    assert row["assessment_valid"] is fully_valid
    assert result["live_qualified"] is fully_valid
    if terminal_invalid:
        assert row["assessment_continuation_allowed"] and not row["canonical_valid"]
        assert row["diagnostic"]["retention"] == "retained"
    if kind in {"missing_model", "alias", "prefix", "model_substitution"}:
        assert not row["response_model_identity_verified"] and row["canonical_valid"]
        assert row["response_model_diagnostic"] == (
            "reported_model_missing"
            if kind == "missing_model"
            else "reported_model_mismatch"
        )
    if kind in {"usage_missing", "usage_partial", "cache"}:
        assert result["estimated_actual_usd"] is None
    if kind == "usage_partial":
        assert (
            row["usage"]["input_tokens"] == 20
            and row["usage"]["reasoning_tokens"] is None
        )
    before = files(directory)
    assert q.report_plan(directory) == result and files(directory) == before
    assert all(
        b"PRIVATE_THOUGHT" not in raw
        and b"PRIVATE_SIGNATURE" not in raw
        and b"SYNTHETIC_SELECTED" not in raw
        for raw in before.values()
    )
    assert await e.execute_plan(directory, **arguments) == result
    assert len(sent) <= 4


@pytest.mark.parametrize(
    "failure", [400, 401, 403, 404, 422, 429, 500, "timeout", "drop"]
)
@pytest.mark.asyncio
async def test_rejections_and_uncertain_calls_never_repeat(
    planned: Any, monkeypatch: pytest.MonkeyPatch, failure: Any
) -> None:
    directory, plan, arguments = planned

    def customize(body: dict[str, Any], index: int) -> Any:
        if failure == "timeout":
            raise httpx2.ReadTimeout("PRIVATE_EXCEPTION_CANARY")
        if failure == "drop":
            raise httpx2.ConnectError("PRIVATE_EXCEPTION_CANARY")
        return httpx2.Response(
            failure,
            json={
                "error": {
                    "code": failure,
                    "message": "PRIVATE_EXCEPTION_CANARY",
                    "status": "UNAVAILABLE",
                }
            },
        )

    sent = install(monkeypatch, directory, plan, customize)
    result = cast(dict[str, Any], await e.execute_plan(directory, **arguments))
    assert len(sent) == result["physical_send_attempts"] == 1
    row = result["calls"][0]
    assert (
        row["invocation_failure_code"] is not None
        if isinstance(failure, int) and failure < 500
        else row["status"] == "unresolved"
    )
    assert "PRIVATE_EXCEPTION_CANARY" not in json.dumps(result)
    assert not result["live_qualified"]
    assert await e.execute_plan(directory, **arguments) == result and len(sent) == 1


def wire_for_plan(
    directory: Path, plan: q.GeminiQualificationPlanV1, case_index: int = 0
) -> Any:
    from judgmentllm.gemini_transport import _qualification_context
    from judgmentllm.preparation import open_prepared_run
    from judgmentllm.transport import physical_identity

    case = plan.calls[case_index]
    spec = q._specification(
        directory, case.task_kind, case.mode, plan.max_output_tokens
    )
    with open_prepared_run(
        case.run_id,
        store=directory / "offline-preparation" / case.case_id,
        spec=spec,
        transport_context=_qualification_context(
            spec, source_commit=plan.source_commit
        ),
    ) as prepared:
        return prepared.prepare_transport_attempt(
            next(prepared.iter_request_dependencies()),
            physical_identity(case.run_id, case.request_uid, case.attempt_id),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "payload",
        "payload_sha256",
        "prompt_sha256",
        "request_uid",
        "task_ids",
        "dependency_sha256",
        "receipt",
        "source",
        "plan",
        "context",
    ],
)
def test_boundary_rechecks_exact_committed_authority(
    planned: Any, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    directory, plan, arguments = planned
    e._authorize(directory, plan, arguments["authorized_plan_sha256"])
    guard = e._QualificationSubmissionGuard(
        directory, plan, plan.calls[0], token=e._EXECUTOR_TOKEN
    )
    wire = wire_for_plan(directory, plan)
    if mutation in {
        "payload",
        "payload_sha256",
        "prompt_sha256",
        "request_uid",
        "task_ids",
        "dependency_sha256",
    }:
        object.__setattr__(
            wire,
            mutation,
            {"payload": b"{}", "task_ids": ("other",)}.get(mutation, "0" * 64),
        )
    elif mutation == "receipt":
        object.__setattr__(wire.transport_commitment, "mechanism", "text-other")
    elif mutation == "source":
        monkeypatch.setattr(e, "_source_commit", lambda: "b" * 40)
    elif mutation == "plan":
        (directory / "plan.sha256").write_text("0" * 64)
    else:
        from judgmentllm.gemini_transport import production_context

        spec = q._specification(directory, "rubric", "text", 512)
        with pytest.raises(gemini.ProviderIntegrityError):
            guard.verify_context(production_context(spec))
        return
    with pytest.raises(gemini.ProviderIntegrityError):
        guard(wire)
    assert not list((directory / "forward-intents").iterdir())


def test_guard_token_duplicate_and_ledger_reset_cannot_replenish(planned: Any) -> None:
    directory, plan, arguments = planned
    with pytest.raises(q.QualificationError):
        e._QualificationSubmissionGuard(directory, plan, plan.calls[0], token=object())
    e._authorize(directory, plan, arguments["authorized_plan_sha256"])
    guard = e._QualificationSubmissionGuard(
        directory, plan, plan.calls[0], token=e._EXECUTOR_TOKEN
    )
    wire = wire_for_plan(directory, plan)
    guard(wire)
    with pytest.raises(gemini.ProviderIntegrityError):
        guard(wire)
    with closing(sqlite3.connect(directory / "qualification.sqlite3")) as connection:
        connection.execute("UPDATE calls SET consumed_at=NULL")
        connection.commit()
    assert e._consumed(directory, plan, arguments["authorized_plan_sha256"]) == {
        "rubric-text"
    }
    with pytest.raises(gemini.ProviderIntegrityError):
        guard(wire)
    (directory / "qualification.sqlite3").unlink()
    with pytest.raises(q.QualificationError):
        e._authorize(directory, plan, arguments["authorized_plan_sha256"])


@pytest.mark.parametrize(
    "mutation",
    [
        "authorization",
        "marker",
        "row",
        "plan_row",
        "missing_intents",
        "extra_intent",
        "corrupt_intent",
        "row_without_intent",
        "symlink",
        "orphan",
    ],
)
def test_damaged_authorization_is_not_reconstructed(
    planned: Any, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    directory, plan, arguments = planned
    if mutation == "orphan":
        (directory / "ledger-created").write_bytes(b"orphan")
    else:
        e._authorize(directory, plan, arguments["authorized_plan_sha256"])
        if mutation == "authorization":
            (directory / "execution-authorization.json").write_bytes(b"{}")
        elif mutation == "marker":
            (directory / "ledger-created").write_bytes(b"wrong")
        elif mutation in {"row", "row_without_intent", "plan_row"}:
            with closing(
                sqlite3.connect(directory / "qualification.sqlite3")
            ) as connection:
                connection.execute(
                    {
                        "row": "DELETE FROM calls WHERE case_id='rubric-text'",
                        "row_without_intent": "UPDATE calls SET consumed_at='synthetic'",
                        "plan_row": "UPDATE authorization SET source_commit='wrong'",
                    }[mutation]
                )
                connection.commit()
        elif mutation == "missing_intents":
            (directory / "forward-intents").rmdir()
        elif mutation == "extra_intent":
            (directory / "forward-intents" / "extra.json").write_bytes(b"{}")
        elif mutation == "corrupt_intent":
            (directory / "forward-intents" / "rubric-text.json").write_bytes(b"{}")
        elif mutation == "symlink":
            target = directory / "plan.sha256"
            target.rename(directory / "original-digest")
            try:
                target.symlink_to(directory / "original-digest")
            except OSError:
                # Windows may deny symlink creation. Exercise the same admission
                # predicate without introducing an unexpected qualification skip.
                target.write_bytes((directory / "original-digest").read_bytes())
                original_symlink = Path.is_symlink

                def symbolic(path: Path) -> bool:
                    return path == target or original_symlink(path)

                monkeypatch.setattr(Path, "is_symlink", symbolic)
    with pytest.raises((q.QualificationError, OSError)):
        q.report_plan(directory)
    if mutation in {"orphan", "authorization"}:
        with pytest.raises(q.QualificationError):
            e._authorize(directory, plan, arguments["authorized_plan_sha256"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["diagnostic", "response", "transport", "results", "observation"]
)
async def test_retained_evidence_integrity_blocks_qualification(
    planned: Any, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.repository import ReadOnlyRepository
    from judgmentllm.persistence.transport_receipts import transport_reference

    directory, plan, arguments = planned
    sent = install(monkeypatch, directory, plan)
    await e.execute_plan(directory, **arguments)
    case = plan.calls[0]
    store = directory / "execution" / case.case_id
    with ReadOnlyRepository(store, preserve_files=True) as repository:
        receipt = next(repository.iter_evidence(case.run_id))
        artifacts = ArtifactStore(repository.run_directory(case.run_id), create=False)
        if damage in {"diagnostic", "response", "transport"}:
            if damage == "diagnostic":
                assert (
                    receipt.assessment_diagnostic is not None
                    and receipt.assessment_diagnostic.artifact is not None
                )
                path = artifacts.verify(receipt.assessment_diagnostic.artifact)
                assert path is not None
            elif damage == "response":
                assert receipt.retained is not None
                path = artifacts.verify(receipt.retained)
                assert path is not None
            else:
                path = artifacts.reference_path(transport_reference(case.attempt_id))
            path.write_bytes(b"PRIVATE_CORRUPTION_CANARY")
    if damage in {"results", "observation"}:
        with closing(sqlite3.connect(store / "registry.sqlite3")) as connection:
            table = "results" if damage == "results" else "observations"
            for (trigger,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                (table,),
            ).fetchall():
                connection.execute('DROP TRIGGER "' + trigger.replace('"', '""') + '"')
            connection.execute(
                "DELETE FROM " + ("results" if damage == "results" else "observations")
            )
            connection.commit()
    result = cast(dict[str, Any], q.report_plan(directory))
    assert (
        not result["live_qualified"]
        and not result["calls"][0]["assessment_continuation_allowed"]
    )
    assert "PRIVATE_CORRUPTION_CANARY" not in json.dumps(result)
    before = files(directory)
    assert q.report_plan(directory) == result and files(directory) == before
    try:
        await e.execute_plan(directory, **arguments)
    except (ValueError, RuntimeError):
        pass
    assert len(sent) == 4


_CHILD = r"""
import asyncio, json, os, socket, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path.cwd() / 'tests'))
from gemini_offline import isolate
from test_gemini_qualification_execution import install
from judgmentllm import gemini_qualification as q, gemini_qualification_execution as e
from judgmentllm import execution
m=pytest.MonkeyPatch()
isolate(m)
q._source_commit=e._source_commit=lambda:'a'*40
directory=Path(sys.argv[1])
boundary, mode=sys.argv[2:]
plan,digest=q._load_plan(directory)
def customize(body,index):
    if boundary == 'after_forward' and mode == 'execute': os._exit(73)
install(m,directory,plan,customize)
original=e._durable
def durable(path,raw):
    original(path,raw)
    if mode == 'execute' and boundary == 'before_forward' and path.parent.name == 'forward-intents': os._exit(73)
e._durable=durable
original_execute=execution.execute_run
def fault(stage,request):
    if mode == 'execute' and boundary == stage: os._exit(73)
async def execute(*args,**kwargs):
    return await original_execute(*args,**kwargs,fault=fault)
execution.execute_run=execute
async def main():
    result=await e.execute_plan(directory, authorized_plan_sha256=digest, authorized_source_commit=plan.source_commit, accept_billable_calls=True, accept_full_synthetic_capture=True)
    print(json.dumps(result,sort_keys=True))
asyncio.run(main())
"""


@pytest.mark.parametrize(
    "boundary,expected_initial,expected_final",
    [
        ("before_forward", 0, 0),
        ("after_forward", 1, 1),
        ("after_evidence_receipt", 1, 4),
    ],
)
def test_hard_crash_and_fresh_process_recovery_do_not_reset_allocation(
    planned: Any, boundary: str, expected_initial: int, expected_final: int
) -> None:
    directory, _plan, _arguments = planned
    environment = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "SystemRoot", "WINDIR", "TEMP", "TMP", "LANG")
        if name in os.environ
    }
    command = [sys.executable, "-B", "-c", _CHILD, str(directory), boundary]
    initial = subprocess.run(
        [*command, "execute"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=40,
    )
    assert initial.returncode == 73, initial.stderr
    log = directory / "independent-forwards"
    assert (
        len(log.read_text().splitlines()) if log.exists() else 0
    ) == expected_initial
    for _ in range(2):
        resumed = subprocess.run(
            [*command, "recover"],
            env=environment,
            text=True,
            capture_output=True,
            timeout=40,
            check=True,
        )
        result = json.loads(resumed.stdout)
        forwards: list[str] = log.read_text().splitlines() if log.exists() else []
        assert len(forwards) == len(set(forwards)) == expected_final
        assert result["physical_send_attempts"] == (4 if expected_final == 4 else 1)
        assert result["live_qualified"] is (expected_final == 4)


@pytest.mark.parametrize(
    "field",
    [
        "authorization_version",
        "maximum_physical_requests",
        "maximum_output_tokens_per_call",
    ],
)
def test_authorization_versions_and_limits_require_exact_integers(field: str) -> None:
    with pytest.raises(ValueError):
        e._Authorization.model_validate(
            dict(e._Authorization(plan_sha256="a" * 64, source_commit="b" * 40))
            | {field: True}
        )


@pytest.mark.asyncio
async def test_cli_execute_requires_all_flags_and_reports_sanitized_failures(
    planned: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The CLI uses asyncio.run, so invoke it from a separate sync helper below.
    directory, plan, _arguments = planned
    sent = install(monkeypatch, directory, plan)
    assert not sent
    monkeypatch.setattr(
        sys, "argv", ["qualify_gemini.py", "execute", "--evidence-dir", str(directory)]
    )
    with pytest.raises(SystemExit):
        q.main()
    assert "required" in capsys.readouterr().err and not sent


def test_cli_executes_only_explicit_plan_and_capture_consent(
    planned: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory, plan, arguments = planned
    sent = install(monkeypatch, directory, plan)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualify_gemini.py",
            "execute",
            "--evidence-dir",
            str(directory),
            "--authorize-plan-sha256",
            arguments["authorized_plan_sha256"],
            "--authorize-source-commit",
            plan.source_commit,
            "--accept-billable-calls",
            "--accept-full-synthetic-capture",
        ],
    )
    q.main()
    assert json.loads(capsys.readouterr().out)["live_qualified"] and len(sent) == 4


@pytest.mark.asyncio
async def test_observed_estimate_above_ceiling_stops_remaining_allocations(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, plan, arguments = planned

    def expensive(body: dict[str, Any], index: int) -> None:
        body["usageMetadata"]["promptTokenCount"] = 100000
        body["usageMetadata"]["totalTokenCount"] = 100014

    sent = install(monkeypatch, directory, plan, expensive)
    result = cast(dict[str, Any], await e.execute_plan(directory, **arguments))
    assert len(sent) == 1 and float(result["estimated_exposure_usd"]) > 0.02
    case = plan.calls[1]
    guard = e._QualificationSubmissionGuard(
        directory, plan, case, token=e._EXECUTOR_TOKEN
    )
    from judgmentllm.gemini_transport import _qualification_context
    from judgmentllm.preparation import open_prepared_run
    from judgmentllm.transport import physical_identity

    spec = q._specification(
        directory, case.task_kind, case.mode, plan.max_output_tokens
    )
    with open_prepared_run(
        case.run_id,
        store=directory / "offline-preparation" / case.case_id,
        spec=spec,
        transport_context=_qualification_context(
            spec, source_commit=plan.source_commit
        ),
    ) as prepared:
        wire = prepared.prepare_transport_attempt(
            next(prepared.iter_request_dependencies()),
            physical_identity(case.run_id, case.request_uid, case.attempt_id),
        )
    with pytest.raises(gemini.ProviderIntegrityError):
        guard(wire)
    assert len(sent) == 1


@pytest.mark.parametrize("mutation", ["input", "dependency", "body"])
@pytest.mark.asyncio
async def test_modified_rehashed_plan_or_inputs_fail_before_credential_access(
    planned: Any, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    directory, _plan, arguments = planned

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Credential access before plan validation")

    monkeypatch.setattr(gemini, "_resolve_credentials", forbidden)
    if mutation == "input":
        (directory / "responses.jsonl").write_bytes(b"PRIVATE_MODIFIED_INPUT")
    else:
        data = json.loads((directory / "plan.json").read_bytes())
        if mutation == "dependency":
            data["calls"][0]["dependency_sha256"] = "0" * 64
        else:
            call = data["calls"][0]
            call["request_body_utf8"] = call["request_body_utf8"].replace(
                "The seed grew", "A different seed grew"
            )
            call["payload_sha256"] = bytes_digest(call["request_body_utf8"].encode())
            call["reserved_input_tokens"], call["input_allowance_evidence"] = (
                q._input_allowance(call["request_body_utf8"])
            )
        raw = canonical_bytes(data)
        (directory / "plan.json").write_bytes(raw)
        (directory / "plan.sha256").write_text(bytes_digest(raw))
        arguments["authorized_plan_sha256"] = bytes_digest(raw)
    with pytest.raises(ValueError):
        await e.execute_plan(directory, **arguments)
    assert not (directory / "execution-authorization.json").exists()


def test_out_of_order_guard_has_no_remaining_case_authority(planned: Any) -> None:
    directory, plan, arguments = planned
    e._authorize(directory, plan, arguments["authorized_plan_sha256"])
    guard = e._QualificationSubmissionGuard(
        directory, plan, plan.calls[1], token=e._EXECUTOR_TOKEN
    )
    with pytest.raises(gemini.ProviderIntegrityError):
        guard(wire_for_plan(directory, plan, 1))
    assert not e._consumed(directory, plan, arguments["authorized_plan_sha256"])


@pytest.mark.asyncio
async def test_preflight_verifies_every_prepared_allocation_before_credentials(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Generator
    from contextlib import contextmanager

    from judgmentllm import preparation

    directory, _plan, arguments = planned
    original = preparation.prepare_run

    @contextmanager
    def tampered(*args: Any, **kwargs: Any) -> Generator[Any]:
        with original(*args, **kwargs) as prepared:
            deps = tuple(prepared.iter_request_dependencies())

            def extra(**kwargs: Any) -> Any:
                return iter(deps * 2)

            prepared.iter_request_dependencies = extra
            yield prepared

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Credential access before prepared-request validation")

    monkeypatch.setattr(preparation, "prepare_run", tampered)
    monkeypatch.setattr(gemini, "_resolve_credentials", forbidden)
    with pytest.raises(q.QualificationError):
        await e.execute_plan(directory, **arguments)
    assert not list((directory / "forward-intents").iterdir())


def test_concurrent_guards_cannot_consume_the_same_allocation_twice(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    directory, plan, arguments = planned
    e._authorize(directory, plan, arguments["authorized_plan_sha256"])
    guard = e._QualificationSubmissionGuard(
        directory, plan, plan.calls[0], token=e._EXECUTOR_TOKEN
    )
    wire = wire_for_plan(directory, plan)
    barrier = Barrier(2)
    original = e._consumed

    def synchronized(*args: Any, **kwargs: Any) -> set[str]:
        result = original(*args, **kwargs)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(e, "_consumed", synchronized)

    def attempt() -> bool:
        try:
            guard(wire)
        except gemini.ProviderIntegrityError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result(timeout=15)
            for future in (pool.submit(attempt), pool.submit(attempt))
        ]
    assert sorted(outcomes) == [False, True]
    assert original(directory, plan, arguments["authorized_plan_sha256"]) == {
        "rubric-text"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inconsistency",
    [
        "missing_transport",
        "extra_attempt",
        "missing_evidence",
        "unconsumed",
        "missing_response",
        "observation_identity",
        "missing_diagnostic",
    ],
)
async def test_report_rejects_inconsistent_independent_durable_views(
    planned: Any, monkeypatch: pytest.MonkeyPatch, inconsistency: str
) -> None:
    from types import SimpleNamespace

    from judgmentllm.persistence.repository import ReadOnlyRepository

    directory, plan, arguments = planned
    install(monkeypatch, directory, plan)
    await e.execute_plan(directory, **arguments)

    def unavailable(*args: Any, **kwargs: Any) -> None:
        return None

    if inconsistency == "missing_transport":
        monkeypatch.setattr(e, "read_transport_commitment", unavailable)
    elif inconsistency == "extra_attempt":
        original_summary = ReadOnlyRepository.summary

        def summary(self: Any, *args: Any, **kwargs: Any) -> Any:
            return original_summary(self, *args, **kwargs).model_copy(
                update={"attempts": 2}
            )

        monkeypatch.setattr(ReadOnlyRepository, "summary", summary)
    elif inconsistency in {"missing_evidence", "missing_diagnostic"}:
        original_evidence = ReadOnlyRepository.iter_evidence

        def evidence(self: Any, *args: Any, **kwargs: Any) -> Any:
            if inconsistency == "missing_evidence":
                return iter(())
            return iter(
                SimpleNamespace(**(vars(receipt) | {"assessment_diagnostic": None}))
                for receipt in original_evidence(self, *args, **kwargs)
            )

        monkeypatch.setattr(ReadOnlyRepository, "iter_evidence", evidence)
    elif inconsistency == "missing_response":
        monkeypatch.setattr(e, "read_receipt", unavailable)
    elif inconsistency == "observation_identity":
        original_response = e.read_receipt

        def response(*args: Any, **kwargs: Any) -> Any:
            receipt = original_response(*args, **kwargs)
            assert receipt is not None
            return receipt.model_copy(update={"observation_key": "unexpected"})

        monkeypatch.setattr(e, "read_receipt", response)
    row = e._case_report(directory, plan, plan.calls[0], inconsistency != "unconsumed")
    assert row["evidence_integrity_failure"] and not row["assessment_valid"]
    assert not row["assessment_continuation_allowed"] and not row["live_qualified"]
    before = files(directory)
    assert (
        e._case_report(directory, plan, plan.calls[0], inconsistency != "unconsumed")
        == row
    )
    assert files(directory) == before
