"""Terminal failures, uncertainty, cancellation and exact original-source checks."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx2
import pytest
from reasoning_fixture import response_body
from study_fixture import (
    PACKAGE,
    SOURCE,
    assessment,
    authorization,
    case_files,
    original_files,
)
from test_study_native import offline as offline
from test_study_native import pilot as pilot
from test_study_native import tier as tier

from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.retry import RetryPolicy
from judgmentllm.study.budget import BudgetLedger
from judgmentllm.study.cli import main
from judgmentllm.study.execution import (
    _STUDY_TOKEN,
    _StudyGuard,
    adapter_for,
    case_report,
)
from judgmentllm.study.records import Proposal
from judgmentllm.study.sources import verify_originals


def test_full_original_manifests_and_drift_diagnostics(tmp_path: Path) -> None:
    source = tmp_path / "originals"
    original_files(source)
    assert verify_originals(source, PACKAGE)["prompt_schema_pairs_matched"] == 312
    assert main(["verify-originals", "--source", str(source)]) == 0
    path = source / "judgmentllm_rubric_study_renderer_v3/manifest_sha256.json"
    path.write_text("{}")
    with pytest.raises(ValueError, match="manifest"):
        verify_originals(source, PACKAGE)


@pytest.mark.parametrize("index", [0, 14, 28])
@pytest.mark.parametrize(
    "outcome",
    [
        "invalid",
        "missing_tier",
        "missing_usage",
        "token_limit",
        "ambiguous_503",
        "wrong_model",
        "truncated",
        "refusal",
        "wrong_tier",
        "nonacceptance",
    ],
)
@pytest.mark.asyncio
async def test_failed_and_incomplete_observations_never_rescore(
    tmp_path: Path, pilot: tuple[Path, Proposal], index: int, outcome: str
) -> None:
    directory, proofs, proposal, case, spec = case_files(tmp_path, pilot, index)
    body, headers = response_body(
        spec, "{}" if outcome == "invalid" else assessment(spec)
    )
    if outcome == "missing_tier":
        if spec.provider == "openai":
            body.pop("service_tier", None)
        elif spec.provider == "anthropic":
            body["usage"].pop("service_tier", None)
        else:
            headers.clear()
    if outcome == "missing_usage":
        body.pop("usageMetadata" if spec.provider == "gemini" else "usage", None)
    if outcome == "wrong_model":
        body["modelVersion" if spec.provider == "gemini" else "model"] = (
            "unrecognized-model"
        )
    if outcome == "wrong_tier":
        if spec.provider == "openai":
            body["service_tier"] = "default"
        elif spec.provider == "anthropic":
            body["usage"]["service_tier"] = "priority"
        else:
            headers["x-gemini-service-tier"] = "priority"
    if outcome == "refusal":
        if spec.provider == "openai":
            body["output"][0]["content"] = [
                {"type": "refusal", "refusal": "PRIVATE_REFUSAL_CANARY"}
            ]
        elif spec.provider == "anthropic":
            body["stop_reason"] = "refusal"
        else:
            body["candidates"][0]["finishReason"] = "SAFETY"
    if outcome == "truncated":
        if spec.provider == "openai":
            body["status"] = "incomplete"
            body["incomplete_details"] = {"reason": "max_output_tokens"}
        elif spec.provider == "anthropic":
            body["stop_reason"] = "max_tokens"
        else:
            body["candidates"][0]["finishReason"] = "MAX_TOKENS"
    if outcome == "token_limit":
        usage = body["usageMetadata" if spec.provider == "gemini" else "usage"]
        usage["promptTokenCount" if spec.provider == "gemini" else "input_tokens"] = (
            40000
        )
        if spec.provider == "gemini":
            usage["totalTokenCount"] = 50000
    sent: list[bytes] = []

    def handler(request: Any) -> Any:
        sent.append(bytes(request.content))
        return httpx2.Response(
            503
            if outcome == "ambiguous_503"
            else 400
            if outcome == "nonacceptance"
            else 200,
            json={"error": {"message": "synthetic uncertain failure"}}
            if outcome in {"ambiguous_503", "nonacceptance"}
            else body,
            headers=headers,
            request=request,
        )

    with BudgetLedger(tmp_path / "campaign").locked() as ledger:
        ledger.authorize(proposal, authorization(proposal))
        guard = _StudyGuard(
            spec,
            SOURCE,
            token=_STUDY_TOKEN,
            proposal=proposal,
            case=case,
            directory=directory,
            ledger=ledger,
            proofs=proofs,
        )
        adapter = adapter_for(spec, guard, http_transport=httpx2.MockTransport(handler))
        try:
            try:
                await execute_run(
                    case.run_id,
                    store=directory / "preparation" / case.case_id,
                    spec=spec,
                    adapter=adapter,
                    retry_policy=RetryPolicy(max_attempts=1),
                )
            except ExceptionGroup:
                assert outcome in {
                    "wrong_model",
                    "missing_usage",
                    "missing_tier",
                    "truncated",
                    "wrong_tier",
                    "refusal",
                    "nonacceptance",
                }
            assert len(sent) == 1
            row = case_report(directory, case)
            if outcome == "invalid":
                assert row["canonical_valid"] is False and row["continuation_allowed"]
            elif outcome == "missing_tier" and spec.provider == "anthropic":
                assert row["canonical_valid"] is True and row["continuation_allowed"]
            elif outcome == "missing_usage":
                assert row["usage"] is None or row["usage"]["input_tokens"] is None
            else:
                assert not row["continuation_allowed"]
            if outcome == "missing_tier" and spec.provider != "anthropic":
                assert row["result_count"] == 12 and row["canonical_valid"] is True
            ledger.finish(
                proposal,
                case,
                terminal=bool(row["continuation_allowed"]),
                reason=row["reason"],
                evidence=row.get("evidence_sha256", "a" * 64),
            )
            assert len([e for e in ledger.events() if e.kind == "reserve"]) == 1
        finally:
            await adapter.aclose()


@pytest.mark.parametrize("index", [0, 14, 28])
@pytest.mark.asyncio
async def test_cancelled_handoff_keeps_reservation(
    tmp_path: Path, pilot: tuple[Path, Proposal], index: int
) -> None:
    directory, proofs, proposal, case, spec = case_files(tmp_path, pilot, index)
    handed = asyncio.Event()
    sent: list[bytes] = []

    async def handler(request: Any) -> Any:
        sent.append(bytes(request.content))
        handed.set()
        await asyncio.Event().wait()

    with BudgetLedger(tmp_path / "campaign").locked() as ledger:
        ledger.authorize(proposal, authorization(proposal))
        guard = _StudyGuard(
            spec,
            SOURCE,
            token=_STUDY_TOKEN,
            proposal=proposal,
            case=case,
            directory=directory,
            ledger=ledger,
            proofs=proofs,
        )
        adapter = adapter_for(spec, guard, http_transport=httpx2.MockTransport(handler))
        try:
            task = asyncio.create_task(
                execute_run(
                    case.run_id,
                    store=directory / "preparation" / case.case_id,
                    spec=spec,
                    adapter=adapter,
                    retry_policy=RetryPolicy(max_attempts=1),
                )
            )
            await asyncio.wait_for(handed.wait(), 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(sent) == 1
            assert not case_report(directory, case)["continuation_allowed"]
            ledger.finish(
                proposal,
                case,
                terminal=False,
                reason="cancelled_after_handoff",
                evidence="a" * 64,
            )
            ledger.cancel(proposal)
            with pytest.raises(ValueError):
                ledger.reserve(proposal, proposal.calls[1])
        finally:
            await adapter.aclose()


@pytest.mark.parametrize("which,index", [("pilot", 0), ("tier", 4), ("tier", 16)])
@pytest.mark.asyncio
async def test_premium_downgrade_and_minimal_privacy(
    tmp_path: Path,
    pilot: tuple[Path, Proposal],
    tier: tuple[Path, Proposal],
    which: str,
    index: int,
) -> None:
    from judgmentllm.io import write_results_jsonl
    from judgmentllm.persistence.repository import ReadOnlyRepository

    directory, proofs, proposal, case, spec = case_files(
        tmp_path, pilot if which == "pilot" else tier, index
    )
    body, headers = response_body(spec, assessment(spec))
    secret = "SYNTHETIC_PRIVATE_THINKING_DO_NOT_RETAIN"
    if spec.provider == "openai":
        body["service_tier"] = "default" if which == "tier" else "flex"
        body["output"].insert(
            0,
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": secret}],
            },
        )
    else:
        headers["x-gemini-service-tier"] = "standard"
        body["candidates"][0]["content"]["parts"].insert(
            0, {"text": secret, "thought": True}
        )
    sent: list[bytes] = []

    def handler(request: Any) -> Any:
        sent.append(bytes(request.content))
        return httpx2.Response(200, json=body, headers=headers, request=request)

    with BudgetLedger(tmp_path / "campaign").locked() as ledger:
        ledger.authorize(proposal, authorization(proposal))
        guard = _StudyGuard(
            spec,
            SOURCE,
            token=_STUDY_TOKEN,
            proposal=proposal,
            case=case,
            directory=directory,
            ledger=ledger,
            proofs=proofs,
        )
        adapter = adapter_for(spec, guard, http_transport=httpx2.MockTransport(handler))
        try:
            await execute_run(
                case.run_id,
                store=directory / "preparation" / case.case_id,
                spec=spec,
                adapter=adapter,
                retry_policy=RetryPolicy(max_attempts=1),
            )
            row = case_report(directory, case)
            assert row["canonical_valid"] and row["continuation_allowed"]
            assert (
                row["reason"]
                == (
                    "expected_premium_downgrade"
                    if which == "tier"
                    else "terminal_assessment"
                )
                and not row["live_qualified"]
            )
            assert len(sent) == 1
            with ReadOnlyRepository(
                directory / "preparation" / case.case_id
            ) as repository:
                write_results_jsonl(
                    tmp_path / "results.jsonl", repository.iter_results(case.run_id)
                )
                receipt = next(repository.iter_evidence(case.run_id))
                assert (
                    receipt.capture == spec.capture
                    and receipt.assessment_diagnostic is not None
                )
                assert receipt.assessment_diagnostic.retention == (
                    "hash_only" if spec.capture == "minimal" else "retained"
                )
            assert all(
                secret.encode() not in p.read_bytes()
                for p in directory.rglob("*")
                if p.is_file()
            )
            assert secret.encode() not in (tmp_path / "results.jsonl").read_bytes()
        finally:
            await adapter.aclose()


def test_nonstandard_review_hold_crosses_suites(
    tmp_path: Path, pilot: tuple[Path, Proposal], tier: tuple[Path, Proposal]
) -> None:
    from study_fixture import seal

    first = seal(pilot[1], tmp_path / "proofs1")
    second = seal(tier[1], tmp_path / "proofs2")
    with BudgetLedger(tmp_path / "campaign").locked() as ledger:
        ledger.authorize(first, authorization(first))
        ledger.authorize(second, authorization(second))
        ledger.reserve(first, first.calls[0])
        ledger.finish(
            first,
            first.calls[0],
            terminal=False,
            reason="served_tier_review_required",
            evidence="a" * 64,
        )
        with pytest.raises(ValueError, match="hold"):
            ledger.reserve(second, second.calls[0])


@pytest.mark.serial
@pytest.mark.asyncio
async def test_process_death_cannot_replenish_calls_tokens_or_money(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    directory, proofs, proposal, case, spec = case_files(tmp_path, pilot, 0)
    child = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("study_process_fixture.py")),
            str(tmp_path),
        ],
        capture_output=True,
        timeout=60,
    )
    assert child.returncode == 86, child.stderr.decode()
    forwarded: list[bytes] = []

    def deny(request: Any) -> Any:
        forwarded.append(bytes(request.content))
        raise AssertionError("Recovery attempted a replacement HTTP call")

    with BudgetLedger(tmp_path / "campaign").locked() as ledger:
        ledger.authorize(proposal, authorization(proposal))
        before = [e for e in ledger.events() if e.kind == "reserve"]
        assert (
            len(before) == 1
            and before[0].input_tokens == 32768
            and before[0].output_tokens == 8192
        )
        guard = _StudyGuard(
            spec,
            SOURCE,
            token=_STUDY_TOKEN,
            proposal=proposal,
            case=case,
            directory=directory,
            ledger=ledger,
            proofs=proofs,
        )
        adapter = adapter_for(spec, guard, http_transport=httpx2.MockTransport(deny))
        try:
            recovered = await recover_run(
                case.run_id,
                store=directory / "preparation" / case.case_id,
                spec=spec,
                adapter=adapter,
                retry_policy=RetryPolicy(max_attempts=1),
            )
            assert recovered.recovery_required and not forwarded
            assert [e for e in ledger.events() if e.kind == "reserve"] == before
            with pytest.raises(ValueError, match="uncertain"):
                ledger.reserve(proposal, case)
        finally:
            await adapter.aclose()
