"""Operator pauses use the real study ledger, engine and SDK HTTP guard."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from reasoning_fixture import response_body
from study_fixture import KEY, assessment, authorization, seal
from test_study_native import offline as offline

from judgmentllm import keyring
from judgmentllm.config import RunSpecV3
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.study import cli, execution
from judgmentllm.study.budget import BudgetLedger
from judgmentllm.study.execution import adapter_for
from judgmentllm.study.records import Authorization, Case, Proposal


@pytest.fixture
def prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pilot: tuple[Path, Proposal]
) -> tuple[Path, Path, Proposal, list[str]]:
    directory, proofs = tmp_path / "proposal", tmp_path / "proofs"
    proposal = seal(pilot[1], proofs)
    shutil.copytree(pilot[0], directory)
    (directory / "proposal.json").write_bytes(canonical_bytes(proposal))
    (directory / "proposal.sha256").write_text(proposal.digest + "\n")
    sent: list[str] = []

    def adapter(spec: RunSpecV3, guard: execution._StudyGuard) -> Any:
        assert guard.case is not None
        case = guard.case
        body, headers = response_body(
            spec,
            assessment(
                spec,
                None
                if spec.execution.grouping == "grouped"
                else (case.selection.trait_id or "",),
            ),
        )

        def handler(request: Any) -> Any:
            assert request.content == case.request_body_utf8.encode()
            sent.append(case.case_id)
            return httpx2.Response(200, json=body, headers=headers, request=request)

        return adapter_for(spec, guard, http_transport=httpx2.MockTransport(handler))

    def no_service(provider: str) -> str:
        pytest.fail("Unexpected real key lookup")

    monkeypatch.setattr(execution, "adapter_for", adapter)
    monkeypatch.setattr(keyring, "_lookup", no_service)
    return directory, proofs, proposal, sent


@pytest.mark.serial
@pytest.mark.asyncio
async def test_one_handoff_resume_and_completion_preserve_all_identities(
    prepared: tuple[Path, Path, Proposal, list[str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory, proofs, proposal, sent = prepared
    campaign = tmp_path / "campaign"
    looked_up: list[str] = []

    def lookup(provider: str) -> str:
        events = BudgetLedger(campaign).events()
        assert events[0].kind == "start"
        assert (directory / "campaign-binding.json").is_file()
        assert not set(keyring._KEY_VARIABLES) & os.environ.keys()
        looked_up.append(provider)
        return KEY

    monkeypatch.setattr(keyring, "_lookup", lookup)
    before = (directory / "proposal.json").read_bytes()
    result = await execution.execute(
        directory,
        proposal.digest,
        campaign,
        authorization(proposal),
        proofs,
        max_new_cases=1,
        keyring_providers=("openai", "anthropic", "gemini"),
    )
    assert sent == ["case-01"] and looked_up == ["openai"]
    assert result["invocation"] == {
        "max_new_cases": 1,
        "new_handoffs": 1,
        "case_limit_reached": True,
    }
    assert result["suite_execution_state"] == "awaiting_explicit_execution"
    assert result["incomplete_coverage"] and not result["campaign_review_holds"]
    assert result["calls"][0]["result_count"] == 12
    assert result["calls"][0]["assessment_diagnostic"]["canonical_valid"]
    started = BudgetLedger(campaign).events()[0]
    binding = (directory / "campaign-binding.json").read_bytes()
    # Independent status reads cannot resolve keys or imply the suite is complete.
    assert (
        cli.main(
            [
                "status",
                "--proposal",
                str(directory),
                "--proposal-sha256",
                proposal.digest,
            ]
        )
        == 0
    )
    status = json.loads(capsys.readouterr().out)
    assert status["suite_execution_state"] == result["suite_execution_state"]
    assert "invocation" not in status
    second = await execution.execute(
        directory,
        proposal.digest,
        campaign,
        authorization(proposal),
        proofs,
        max_new_cases=1,
        keyring_providers=("openai",),
    )
    assert sent == ["case-01", "case-02"] and looked_up == ["openai", "openai"]
    assert second["invocation"]["new_handoffs"] == 1
    assert second["calls"][0] == result["calls"][0]
    assert BudgetLedger(campaign).events()[0] == started
    # A larger limit finishes the remainder; terminal cases consume no new limit.
    completed = await execution.execute(
        directory,
        proposal.digest,
        campaign,
        authorization(proposal),
        proofs,
        max_new_cases=98,
    )
    assert len(sent) == len(set(sent)) == 42
    assert completed["invocation"]["new_handoffs"] == 40
    assert not completed["invocation"]["case_limit_reached"]
    assert completed["suite_execution_state"] == "complete"
    assert not completed["incomplete_coverage"]
    for recover in (False, True):
        reopened = await execution.execute(
            directory,
            proposal.digest,
            campaign,
            authorization(proposal),
            proofs,
            recover_only=recover,
            max_new_cases=None if recover else 1,
        )
        assert reopened["invocation"]["new_handoffs"] == 0
        assert reopened["campaign_reserved_calls"] == 42 and len(sent) == 42
    assert (directory / "proposal.json").read_bytes() == before
    assert (directory / "campaign-binding.json").read_bytes() == binding
    assert BudgetLedger(campaign).events()[0] == started
    exports = execution.exports(directory, proposal.digest)
    assert len(exports) == 126
    assert {row["run_id"] for row in exports} == {c.run_id for c in proposal.calls}
    assert KEY not in json.dumps(completed)
    assert all(
        KEY.encode() not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    )


@pytest.mark.parametrize("limit", [0, -1, 99, True, 1.0, "1"])
@pytest.mark.asyncio
async def test_invalid_limits_reject_before_admission(
    tmp_path: Path,
    limit: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "unprepared"
    proofs = tmp_path / "unused-proofs"
    consent = Authorization(
        proposal_sha256="0" * 64,
        source_commit="a" * 40,
        suite="native_pilot",
        maximum_calls=42,
        ceiling_usd=Decimal(5),
        elapsed_seconds=28800,
        accept_billable_calls=True,
        accept_synthetic_capture=True,
        authorized_by="synthetic-reviewer",
        authorized_at=datetime(2026, 11, 12, tzinfo=UTC),
    )

    def deny(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Invalid execution controls reached admission or credentials")

    # These controls must fail before any mutable store or proof construction.
    monkeypatch.setattr(execution, "verify_admission", deny)
    monkeypatch.setattr(keyring, "_lookup", deny)
    with pytest.raises(ValueError, match="integer from 1 through 98"):
        await execution.execute(
            directory,
            consent.proposal_sha256,
            tmp_path / "campaign",
            consent,
            proofs,
            max_new_cases=limit,
        )
    with pytest.raises(ValueError):
        await execution.execute(
            directory,
            consent.proposal_sha256,
            tmp_path / "campaign",
            consent,
            proofs,
            max_new_cases=1,
            recover_only=True,
        )
    with pytest.raises(ValueError, match="exact study provider names"):
        await execution.execute(
            directory,
            consent.proposal_sha256,
            tmp_path / "campaign",
            consent,
            proofs,
            keyring_providers=("vertex",),
        )
    assert (
        not directory.exists()
        and not proofs.exists()
        and not (tmp_path / "campaign").exists()
    )


def test_cli_limit_validation_and_readonly_admission(
    prepared: tuple[Path, Path, Proposal, list[str]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory, proofs, proposal, sent = prepared
    consent = tmp_path / "authorization.json"
    consent.write_bytes(canonical_bytes(authorization(proposal)))
    args = [
        "--proposal",
        str(directory),
        "--proposal-sha256",
        proposal.digest,
        "--authorization",
        str(consent),
        "--proofs",
        str(proofs),
    ]
    assert cli.main(["check-admission", *args]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert (
        checked["admission_documents_verified"]
        and checked["authorized_calls_in_proposal"] == 0
    )
    assert not (directory / "campaign-binding.json").exists()
    assert cli._case_limit("1") == 1
    for value in ("0", "99", "1.5", "true"):
        with pytest.raises(SystemExit) as stopped:
            cli.main(["execute", *args, "--max-new-cases", value])
        assert stopped.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["recover", *args, "--max-new-cases", "1"])
    assert not sent


@pytest.mark.parametrize(
    "gate",
    [
        "consent",
        "proof",
        "source",
        "hold",
        "cancel",
        "deadline",
        "checkpoint",
        "journal",
        "provider",
    ],
)
@pytest.mark.asyncio
async def test_denied_admission_budget_and_state_never_lookup(
    prepared: tuple[Path, Path, Proposal, list[str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
) -> None:
    directory, proofs, proposal, sent = prepared
    campaign = tmp_path / "campaign"
    consent = authorization(proposal)
    if gate == "consent":
        consent = consent.model_copy(update={"proposal_sha256": "b" * 64})
    elif gate == "proof":
        assert proposal.calls[0].price is not None
        (proofs / (proposal.calls[0].price.evidence_sha256 + ".json")).write_text(
            "changed"
        )
    elif gate == "source":
        monkeypatch.setattr("judgmentllm.study.plans._source_commit", lambda: "b" * 40)
    elif gate not in {"provider"}:
        with BudgetLedger(campaign).locked() as ledger:
            ledger.authorize(
                proposal,
                consent,
                now=datetime.now(UTC) - timedelta(hours=9)
                if gate == "deadline"
                else None,
            )
            if gate == "hold":
                ledger.stop(proposal, proposal.calls[0], "independent_review_hold")
            if gate == "cancel":
                ledger.cancel(proposal)
            if gate == "checkpoint":
                next(ledger._checkpoints.glob("*.json")).unlink()
            if gate == "journal":
                (campaign / "campaign.json").unlink()
    if gate in {"hold", "cancel", "deadline"}:
        result = await execution.execute(
            directory,
            proposal.digest,
            campaign,
            consent,
            proofs,
            max_new_cases=1,
            keyring_providers=("openai",),
        )
        assert result["suite_execution_state"] == "held"
        assert not result["invocation"]["case_limit_reached"]
    else:
        with pytest.raises((OSError, ValueError)):
            await execution.execute(
                directory,
                proposal.digest,
                campaign,
                consent,
                proofs,
                max_new_cases=1,
                keyring_providers=("anthropic",) if gate == "provider" else ("openai",),
            )
    assert not sent


@pytest.mark.parametrize(
    "failure",
    ["invalid", "missing_tier", "wrong_model", "uncertain", "before", "after"],
)
@pytest.mark.asyncio
async def test_limit_retains_validation_failures_and_uncertainty(
    prepared: tuple[Path, Path, Proposal, list[str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    directory, proofs, proposal, sent = prepared
    campaign = tmp_path / "campaign"

    def adapter(spec: RunSpecV3, guard: execution._StudyGuard) -> Any:
        # Reuse the real guard and SDK but change only the synthetic HTTP outcome.
        body, headers = response_body(
            spec, "{}" if failure == "invalid" else assessment(spec)
        )
        if failure == "missing_tier":
            body.pop("service_tier", None)
        if failure == "wrong_model":
            body["model"] = "other-exact-model"

        def handler(request: Any) -> Any:
            assert guard.case is not None
            sent.append(guard.case.case_id)
            if failure == "after":
                raise asyncio.CancelledError
            return httpx2.Response(
                503 if failure == "uncertain" else 200,
                json={"error": {"message": "synthetic"}}
                if failure == "uncertain"
                else body,
                headers=headers,
                request=request,
            )

        return adapter_for(spec, guard, http_transport=httpx2.MockTransport(handler))

    monkeypatch.setattr(execution, "adapter_for", adapter)
    original_execute = execution.execute_run
    if failure == "before":

        async def interrupt(*args: Any, **kwargs: Any) -> Any:
            raise asyncio.CancelledError

        monkeypatch.setattr(execution, "execute_run", interrupt)
    if failure in {"before", "after"}:
        with pytest.raises(asyncio.CancelledError):
            await execution.execute(
                directory,
                proposal.digest,
                campaign,
                authorization(proposal),
                proofs,
                max_new_cases=1,
            )
        assert len(sent) == (failure == "after")
        if failure == "before":
            monkeypatch.setattr(execution, "execute_run", original_execute)
            continued = await execution.execute(
                directory,
                proposal.digest,
                campaign,
                authorization(proposal),
                proofs,
                max_new_cases=1,
            )
            assert sent == ["case-01"]
            assert continued["invocation"]["new_handoffs"] == 1
            return
    else:
        result = await execution.execute(
            directory,
            proposal.digest,
            campaign,
            authorization(proposal),
            proofs,
            max_new_cases=1,
        )
        assert len(sent) == 1
        if failure == "uncertain":
            assert result["calls"][0]["canonical_valid"] is None
        elif failure in {"invalid", "missing_tier"}:
            assert result["calls"][0]["canonical_valid"] == (failure != "invalid")
        if failure == "wrong_model":
            assert result["calls"][0]["reported_model"] == "other-exact-model"
            assert result["calls"][0]["reason"] == "reported_model_integrity_mismatch"
        assert result["invocation"]["case_limit_reached"] == (failure == "invalid")
        if failure == "invalid":
            assert result["calls"][0]["continuation_allowed"]
            assert not result["campaign_review_holds"]
            return
        if failure == "missing_tier":
            assert result["calls"][0]["result_count"] == 12
            assert result["calls"][0]["reason"] == "served_tier_review_required"
    recovered = await execution.execute(
        directory,
        proposal.digest,
        campaign,
        authorization(proposal),
        proofs,
        recover_only=True,
    )
    assert len(sent) == 1 and recovered["campaign_reserved_calls"] == 1
    assert recovered["campaign_review_holds"]
    resumed = await execution.execute(
        directory,
        proposal.digest,
        campaign,
        authorization(proposal),
        proofs,
        max_new_cases=1,
    )
    assert len(sent) == 1 and resumed["suite_execution_state"] == "held"
    assert not resumed["invocation"]["case_limit_reached"]


@pytest.mark.parametrize("mode", ["execute", "crash"])
def test_separate_process_resume_and_recovery_never_replace_requests(
    prepared: tuple[Path, Path, Proposal, list[str]], tmp_path: Path, mode: str
) -> None:
    assert prepared[0].parent == tmp_path
    helper = Path(__file__).with_name("study_limit_process.py")
    environment = {
        k: v for k, v in os.environ.items() if k not in keyring._KEY_VARIABLES
    }

    def run(operation: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(helper), str(tmp_path), operation],
            capture_output=True,
            text=True,
            env=environment,
            timeout=120,
        )

    first = run(mode)
    assert first.returncode == (86 if mode == "crash" else 0), (
        first.stdout + first.stderr
    )
    if mode == "crash":
        directory, _, proposal, _ = prepared
        before_recovery = execution.report(directory, proposal.digest)
        assert before_recovery["suite_execution_state"] == "recovery_required"
    recovered = run("recover")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    report = json.loads(recovered.stdout)
    assert report["campaign_reserved_calls"] == 1
    assert report["invocation"]["new_handoffs"] == 0
    continued = run("execute")
    assert continued.returncode == 0, continued.stdout + continued.stderr
    calls = [
        json.loads(line)
        for line in (tmp_path / "http-calls.jsonl").read_text().splitlines()
    ]
    assert calls == (["case-01"] if mode == "crash" else ["case-01", "case-02"])
    assert len(calls) == len(set(calls))
    assert (
        KEY
        not in first.stdout
        + first.stderr
        + recovered.stdout
        + recovered.stderr
        + continued.stdout
        + continued.stderr
    )


@pytest.mark.asyncio
async def test_pause_never_exhausts_budget_and_resuming_rechecks_it(
    prepared: tuple[Path, Path, Proposal, list[str]], tmp_path: Path
) -> None:
    directory, proofs, proposal, sent = prepared
    calls: list[Case] = []
    for case in proposal.calls:
        assert case.price is not None
        price = case.price.model_copy(
            update={
                "outcomes": tuple(
                    outcome.model_copy(update={"input_per_million": Decimal(100)})
                    for outcome in case.price.outcomes
                )
            }
        )
        calls.append(
            case.model_copy(
                update={"price": price, "reserved_usd": price.reservation(32768, 8192)}
            )
        )
    proposal = Proposal.model_validate_json(
        canonical_bytes(proposal.model_copy(update={"calls": tuple(calls)}))
    )
    (directory / "proposal.json").write_bytes(canonical_bytes(proposal))
    (directory / "proposal.sha256").write_text(proposal.digest + "\n")
    result = await execution.execute(
        directory,
        proposal.digest,
        tmp_path / "campaign",
        authorization(proposal),
        proofs,
        max_new_cases=1,
    )
    assert sent == ["case-01"]
    assert result["suite_execution_state"] == "awaiting_explicit_execution"
    assert not result["campaign_review_holds"]
    assert result["invocation"]["case_limit_reached"]
    resumed = await execution.execute(
        directory,
        proposal.digest,
        tmp_path / "campaign",
        authorization(proposal),
        proofs,
        max_new_cases=1,
    )
    assert sent == ["case-01"]
    assert resumed["suite_execution_state"] == "held"
    assert "USD" in resumed["campaign_review_holds"][0]
    assert resumed["campaign_reserved_usd"] == result["campaign_reserved_usd"]
    assert resumed["invocation"]["new_handoffs"] == 0


@pytest.mark.serial
@pytest.mark.r_interop
@pytest.mark.asyncio
async def test_paused_sdk_exports_are_consumed_in_strict_r(
    prepared: tuple[Path, Path, Proposal, list[str]], tmp_path: Path
) -> None:
    directory, proofs, proposal, sent = prepared
    rscript = shutil.which("Rscript")
    assert rscript is not None, "Rscript/jsonlite are required for local acceptance"
    result = await execution.execute(
        directory,
        proposal.digest,
        tmp_path / "campaign",
        authorization(proposal),
        proofs,
        max_new_cases=1,
    )
    exports = execution.exports(directory, proposal.digest)
    selected = [row for row in exports if row["run_id"] == proposal.calls[0].run_id]
    (tmp_path / "paused.json").write_text(
        json.dumps({"report": result, "exports": selected})
    )
    program = """
    stopifnot(requireNamespace("jsonlite", quietly=TRUE))
    x <- jsonlite::fromJSON(commandArgs(TRUE)[1], simplifyVector=FALSE)
    stopifnot(x$report$suite_execution_state == "awaiting_explicit_execution",
              x$report$campaign_reserved_calls == 1L,
              x$report$incomplete_coverage,
              x$report$invocation$new_handoffs == 1L)
    row <- x$report$calls[[1]]
    json <- Filter(function(e) e$format == "jsonl", x$exports)[[1]]
    results <- lapply(readLines(json$artifact_path), jsonlite::fromJSON)
    stopifnot(length(results) == 12L,
              all(vapply(results, function(r) r$attempt_id == row$attempt_id, logical(1))),
              all(vapply(results, function(r) r$request_uid == row$request_uid, logical(1))),
              all(vapply(results, function(r) is.null(r$probabilities), logical(1))),
              all(vapply(results, function(r) identical(r$resolved_levels$label, c("0", "1", "2")), logical(1))))
    r <- Filter(function(e) e$view == "r", x$exports)[[1]]
    csv <- read.csv(r$artifact_path, colClasses="character", na.strings="NA")
    stopifnot(nrow(csv) == 12L, length(unique(csv$attempt_id)) == 1L)
    cat("Paused study export passed\\n")
    """
    checked = subprocess.run(
        [rscript, "--vanilla", "-e", program, str(tmp_path / "paused.json")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "Paused study export passed" in checked.stdout and sent == ["case-01"]


@pytest.mark.resource
def test_single_handoff_resource_and_cleanup_bounds(
    prepared: tuple[Path, Path, Proposal, list[str]], tmp_path: Path
) -> None:
    assert prepared[0].parent == tmp_path
    helper = Path(__file__).with_name("study_limit_process.py")
    checked = subprocess.run(
        [sys.executable, str(helper), str(tmp_path), "resource"],
        capture_output=True,
        text=True,
        timeout=180,
        env={k: v for k, v in os.environ.items() if k not in keyring._KEY_VARIABLES},
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    result = json.loads(checked.stdout)
    measured = result["resources"]
    (tmp_path / "single-request-resources.json").write_text(json.dumps(measured))
    assert 0 < measured["python_peak_bytes"] < 64 * 1024 * 1024
    assert 0 < measured["rss_after_bytes"] < 256 * 1024 * 1024
    assert measured["threads_after"] <= measured["threads_before"]
    assert (
        result["invocation"]["new_handoffs"] == result["campaign_reserved_calls"] == 1
    )
    assert (tmp_path / "http-calls.jsonl").read_text().splitlines() == ['"case-01"']
