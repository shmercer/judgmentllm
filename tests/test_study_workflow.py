"""CLI routing, source-bound sealing, conservative pricing and bounded orchestration."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from reasoning_fixture import response_body
from study_fixture import PACKAGE, assessment, authorization, seal, spec_for
from test_study_native import offline as offline
from test_study_native import pilot as pilot

from judgmentllm.config import RunSpecV3
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.study import cli, execution, plans
from judgmentllm.study.budget import BudgetLedger, BudgetStop
from judgmentllm.study.budget import _global_lock_path as default_lock_path
from judgmentllm.study.definitions import library
from judgmentllm.study.records import Case, OutcomePrice, Price, Proposal


def test_worst_documented_served_price_and_independent_scope(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    case = proposal.calls[0]
    assert case.price is not None
    price = case.price.model_copy(
        update={
            "outcomes": (
                OutcomePrice(
                    served_tier="standard",
                    input_per_million=Decimal(2),
                    billable_output_per_million=Decimal(10),
                ),
                OutcomePrice(
                    served_tier="flex",
                    input_per_million=Decimal(1),
                    billable_output_per_million=Decimal(20),
                ),
            )
        }
    )
    assert price.reservation(32768, 8192) == Decimal("0.196608")
    with pytest.raises(ValueError):
        Price.model_validate_json(
            canonical_bytes(
                price.model_copy(update={"outcomes": (price.outcomes[0],) * 2})
            )
        )
    with pytest.raises(ValueError):
        Case.model_validate_json(
            canonical_bytes(
                case.model_copy(update={"price": price, "reserved_usd": Decimal(0)})
            )
        )
    assert default_lock_path().name == "study-campaign-v1.lock"


def test_global_ownership_and_plan_cannot_move_to_fresh_budget(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    with BudgetLedger(tmp_path / "one").locked() as ledger:
        ledger.authorize(proposal, authorization(proposal))
        with pytest.raises((OSError, ValueError)):
            with BudgetLedger(tmp_path / "two").locked():
                pytest.fail("A second campaign acquired global forwarding ownership")
        with pytest.raises(ValueError, match="already"):
            with ledger.locked():
                pytest.fail("Ownership was reentered")
    with BudgetLedger(tmp_path / "two").locked() as other:
        with pytest.raises(ValueError, match="Immutable"):
            other.authorize(proposal, authorization(proposal))


def test_plan_artifacts_and_holistic_cli_previews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entries = library(PACKAGE)

    def first_only(package: Path) -> list[tuple[str, str, RunSpecV3, bool]]:
        return entries[:1]

    monkeypatch.setattr(plans, "library", first_only)
    result = plans.full_plan(PACKAGE, tmp_path / "plan", "a" * 40)
    assert result["configurations"] == result["requests"] == 1
    row = json.loads((tmp_path / "plan/requests.jsonl").read_text())
    assert row["configuration_id"] == entries[0][0]
    assert json.loads(row["files"]["00.payload.json"])["max_output_tokens"] == 4096
    for command in ("preview", "schema", "preflight"):
        assert (
            cli.main(
                [
                    "--package",
                    str(PACKAGE),
                    command,
                    "--model",
                    "gpt-6-luna",
                    "--scenario",
                    "cj",
                    "--tier",
                    "flex",
                    "--trait",
                    "overall_writing_quality",
                    "--reverse",
                ]
            )
            == 0
        )
        returned = json.loads(capsys.readouterr().out)
        assert returned["candidate_preflight"] == "passed_offline_only"
        assert returned["account_eligibility"] == "unknown"
        if command == "preview":
            assert "Overall Writing Quality" in returned["prompt"]
            assert "student_opaque_" not in returned["prompt"]
    assert (
        cli.main(
            [
                "preview",
                "--model",
                "gpt-6-luna",
                "--scenario",
                "grouped_scores",
                "--tier",
                "flex",
                "--reverse",
            ]
        )
        == 2
    )
    assert (
        cli.main(
            [
                "preview",
                "--model",
                "gpt-6-luna",
                "--scenario",
                "grouped_scores",
                "--tier",
                "flex",
            ]
        )
        == 0
    )
    assert (
        json.loads(capsys.readouterr().out)["candidate_preflight"]
        == "passed_offline_only"
    )


def test_source_bound_positive_sealing_and_unknown_proof_denial(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    proofs = tmp_path / "proofs"
    sealed = seal(pilot[1], proofs)
    eligibility = tuple(
        c.eligibility for c in sealed.calls if c.eligibility is not None
    )
    prices = tuple(c.price for c in sealed.calls if c.price is not None)
    with pytest.raises(ValueError, match="proofs"):
        plans.build_proposal(
            PACKAGE,
            tmp_path / "missing",
            "native_pilot",
            eligibilities=eligibility,
            prices=prices,
        )
    proposal = plans.build_proposal(
        PACKAGE,
        tmp_path / "sealed",
        "native_pilot",
        eligibilities=eligibility,
        prices=prices,
        proofs=proofs,
    )
    assert proposal.status == "sealed" and proposal.authorized_calls == 0
    assert all(
        c.max_output_tokens == old.max_output_tokens
        for c, old in zip(proposal.calls, pilot[1].calls, strict=True)
    )
    assert plans.load_proposal(tmp_path / "sealed", proposal.digest) == proposal
    with pytest.raises(ValueError):
        plans.load_proposal(tmp_path / "sealed", "b" * 64)


@pytest.mark.asyncio
async def test_shared_executor_reports_budget_incomplete_and_recovery_has_zero_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pilot: tuple[Path, Proposal]
) -> None:
    proofs = tmp_path / "proofs"
    proposal = seal(pilot[1], proofs)
    cases: list[Case] = []
    for case in proposal.calls:
        assert case.price is not None
        price = case.price.model_copy(
            update={
                "outcomes": tuple(
                    p.model_copy(update={"input_per_million": Decimal(100)})
                    for p in case.price.outcomes
                )
            }
        )
        cases.append(
            case.model_copy(
                update={"price": price, "reserved_usd": price.reservation(32768, 8192)}
            )
        )
    proposal = Proposal.model_validate_json(
        canonical_bytes(proposal.model_copy(update={"calls": tuple(cases)}))
    )
    directory = tmp_path / "proposal"
    shutil.copytree(pilot[0], directory)
    (directory / "proposal.json").write_bytes(canonical_bytes(proposal))
    (directory / "proposal.sha256").write_text(proposal.digest + "\n")

    def loaded(directory: Path, digest: str) -> Proposal:
        return proposal

    monkeypatch.setattr(execution, "load_proposal", loaded)
    sent: list[bytes] = []
    spec = spec_for(proposal)
    body, headers = response_body(spec, assessment(spec))

    def handler(request: Any) -> Any:
        sent.append(bytes(request.content))
        return httpx2.Response(200, json=body, headers=headers, request=request)

    original = execution.adapter_for

    def mocked_adapter(spec: RunSpecV3, guard: execution._StudyGuard) -> Any:
        return original(spec, guard, http_transport=httpx2.MockTransport(handler))

    monkeypatch.setattr(execution, "adapter_for", mocked_adapter)
    report = await execution.execute(
        directory,
        proposal.digest,
        tmp_path / "campaign",
        authorization(proposal),
        proofs,
    )
    assert len(sent) == 1 and report["incomplete_coverage"]
    assert report["campaign_reserved_calls"] == 1
    assert all("USD" in row["reason"] for row in report["calls"][1:])
    recovered = await execution.execute(
        directory,
        proposal.digest,
        tmp_path / "campaign",
        authorization(proposal),
        proofs,
        recover_only=True,
    )
    assert (
        len(sent) == 1
        and recovered["campaign_reserved_usd"] == report["campaign_reserved_usd"]
    )
    files = execution.exports(directory, proposal.digest)
    assert (
        len(files) == 42 * 3 and sum(cast(int, r["row_count"]) for r in files) == 12 * 3
    )


def test_cli_routes_proposals_reports_and_explicit_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pilot: tuple[Path, Proposal],
) -> None:
    proposal = pilot[1]
    empty = tmp_path / "empty.json"
    empty.write_text("[]")

    def built(*args: Any, **kwargs: Any) -> Proposal:
        return proposal

    monkeypatch.setattr(cli, "build_proposal", built)
    assert (
        cli.main(
            [
                "proposal",
                "--suite",
                "native_pilot",
                "--output",
                str(tmp_path / "p"),
                "--eligibilities",
                str(empty),
                "--prices",
                str(empty),
                "--proofs",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["authorized_calls"] == 0

    def reported(*args: Any) -> dict[str, bool]:
        return {"synthetic_report": True}

    def exported(*args: Any) -> list[Any]:
        return []

    monkeypatch.setattr(cli, "report", reported)
    monkeypatch.setattr(cli, "exports", exported)
    for operation in ("status", "report", "export"):
        assert (
            cli.main(
                [
                    operation,
                    "--proposal",
                    str(pilot[0]),
                    "--proposal-sha256",
                    proposal.digest,
                ]
            )
            == 0
        )
        capsys.readouterr()
    assert (
        cli.main(
            [
                "execute",
                "--proposal",
                str(pilot[0]),
                "--proposal-sha256",
                proposal.digest,
                "--authorization",
                str(empty),
                "--proofs",
                str(tmp_path),
            ]
        )
        == 2
    )
    capsys.readouterr()
    consent = tmp_path / "authorization.json"
    consent.write_bytes(canonical_bytes(authorization(proposal)))

    async def unexecuted(*args: Any, **kwargs: Any) -> dict[str, object]:
        return {"synthetic_execution_only": True}

    monkeypatch.setattr(cli, "execute", unexecuted)
    for operation in ("execute", "recover"):
        assert (
            cli.main(
                [
                    operation,
                    "--proposal",
                    str(pilot[0]),
                    "--proposal-sha256",
                    proposal.digest,
                    "--authorization",
                    str(consent),
                    "--proofs",
                    str(tmp_path),
                    "--campaign",
                    str(tmp_path / "campaign"),
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["synthetic_execution_only"]


@pytest.mark.parametrize(
    "failure", ["handoff_budget", "after_response", "ambiguous_503"]
)
@pytest.mark.asyncio
async def test_orchestration_halts_and_keeps_reservations_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pilot: tuple[Path, Proposal],
    failure: str,
) -> None:
    proofs = tmp_path / "proofs"
    proposal = seal(pilot[1], proofs)
    directory = tmp_path / "proposal"
    shutil.copytree(pilot[0], directory)
    (directory / "proposal.json").write_bytes(canonical_bytes(proposal))
    spec = spec_for(proposal)
    body, headers = response_body(spec, assessment(spec))
    sent: list[bytes] = []

    def loaded(*args: Any) -> Proposal:
        return proposal

    def handler(request: Any) -> Any:
        sent.append(bytes(request.content))
        return httpx2.Response(
            503 if failure == "ambiguous_503" else 200,
            json={"error": {"message": "synthetic uncertainty"}}
            if failure == "ambiguous_503"
            else body,
            headers=headers,
            request=request,
        )

    original_adapter = execution.adapter_for

    def adapter(spec: RunSpecV3, guard: execution._StudyGuard) -> Any:
        return original_adapter(
            spec, guard, http_transport=httpx2.MockTransport(handler)
        )

    original_execute = execution.execute_run

    async def failed(*args: Any, **kwargs: Any) -> Any:
        if failure == "handoff_budget":
            raise BudgetStop("suite deadline changed before HTTP handoff")
        result = await original_execute(*args, **kwargs)
        if failure == "after_response":
            raise ExceptionGroup(
                "synthetic shutdown failure", [ValueError("review required")]
            )
        return result

    monkeypatch.setattr(execution, "load_proposal", loaded)
    monkeypatch.setattr(execution, "adapter_for", adapter)
    monkeypatch.setattr(execution, "execute_run", failed)
    report = await execution.execute(
        directory,
        proposal.digest,
        tmp_path / "campaign",
        authorization(proposal),
        proofs,
    )
    expected = 0 if failure == "handoff_budget" else 1
    assert len(sent) == report["campaign_reserved_calls"] == expected
    assert report["campaign_review_holds"] and report["incomplete_coverage"]
    monkeypatch.setattr(execution, "execute_run", original_execute)
    recovered = await execution.execute(
        directory,
        proposal.digest,
        tmp_path / "campaign",
        authorization(proposal),
        proofs,
        recover_only=True,
    )
    assert len(sent) == expected == recovered["campaign_reserved_calls"]
    assert recovered["campaign_reserved_usd"] == report["campaign_reserved_usd"]


@pytest.mark.serial
@pytest.mark.asyncio
async def test_complete_synthetic_pilot_recovery_never_forwards_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pilot: tuple[Path, Proposal]
) -> None:
    proofs = tmp_path / "proofs"
    proposal = seal(pilot[1], proofs)
    directory = tmp_path / "proposal"
    shutil.copytree(pilot[0], directory)
    (directory / "proposal.json").write_bytes(canonical_bytes(proposal))
    sent: list[bytes] = []

    def loaded(*args: Any) -> Proposal:
        return proposal

    original = execution.adapter_for

    def adapter(spec: RunSpecV3, guard: execution._StudyGuard) -> Any:
        assert guard.case is not None
        case = guard.case
        output = assessment(
            spec,
            None
            if spec.execution.grouping == "grouped"
            else (case.selection.trait_id or "",),
        )
        body, headers = response_body(spec, output)

        def handler(request: Any) -> Any:
            assert request.content == case.request_body_utf8.encode()
            sent.append(bytes(request.content))
            return httpx2.Response(200, json=body, headers=headers, request=request)

        return original(spec, guard, http_transport=httpx2.MockTransport(handler))

    monkeypatch.setattr(execution, "load_proposal", loaded)
    monkeypatch.setattr(execution, "adapter_for", adapter)
    report = await execution.execute(
        directory,
        proposal.digest,
        tmp_path / "campaign",
        authorization(proposal),
        proofs,
    )
    assert len(sent) == report["campaign_reserved_calls"] == 42
    assert not report["incomplete_coverage"] and not report["campaign_review_holds"]
    assert all(
        r["canonical_valid"] and not r["live_qualified"] for r in report["calls"]
    )
    recovered = await execution.execute(
        directory,
        proposal.digest,
        tmp_path / "campaign",
        authorization(proposal),
        proofs,
        recover_only=True,
    )
    assert (
        len(sent) == 42
        and recovered["campaign_reserved_usd"] == report["campaign_reserved_usd"]
    )
    with BudgetLedger(tmp_path / "campaign").locked() as ledger:
        with pytest.raises(BudgetStop, match="order"):
            ledger.reserve(proposal, proposal.calls[0])
