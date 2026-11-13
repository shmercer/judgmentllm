"""Two independently consented control suites over the original guarded engine."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
    sdk_bytes,
    seal,
    spec_for,
)
from test_study_native import offline as offline

from judgmentllm.config import GenerationSpec
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.retry import RetryPolicy
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.preparation import PreparationError
from judgmentllm.providers.protocol import ProviderError
from judgmentllm.study import plans
from judgmentllm.study.admission import candidate_context
from judgmentllm.study.budget import BudgetLedger, BudgetStop
from judgmentllm.study.cli import main
from judgmentllm.study.definitions import CONTRASTS, MODELS, SUITES
from judgmentllm.study.execution import (
    _STUDY_TOKEN,
    _StudyGuard,
    adapter_for,
    case_report,
)
from judgmentllm.study.plans import (
    prepare,
    request_for,
    selected_configuration,
    suite_selections,
)
from judgmentllm.study.records import Authorization, Proposal, Selection


@pytest.mark.asyncio
async def test_complete_control_matrix_and_actual_pinned_sdk_bytes(
    control_proposal: tuple[Path, Proposal],
) -> None:
    directory, proposal = control_proposal
    count, dollars, seconds = SUITES[proposal.suite]
    assert len(proposal.calls) == count
    assert proposal.ceiling_usd == dollars and proposal.elapsed_seconds == seconds
    assert proposal.status == "blocked" and proposal.authorized_calls == 0
    assert proposal.concurrency == 1 and proposal.automatic_retries == 0
    assert proposal.reasoning_capture == "none"
    assert all(
        c.blockers and c.price is None and c.eligibility is None for c in proposal.calls
    )
    assert tuple(c.selection for c in proposal.calls) == suite_selections(
        proposal.suite
    )
    assert plans.load_proposal(directory, proposal.digest) == proposal
    assert len({c.configuration_id for c in proposal.calls}) == count
    for first, second in zip(proposal.calls[::2], proposal.calls[1::2], strict=True):
        assert first.selection.model == second.selection.model
        assert first.selection.tier == second.selection.tier
        assert first.selection.scenario == second.selection.scenario
        assert first.account_scope == second.account_scope
        assert first.prompt_sha256 == second.prompt_sha256
        assert first.schema_sha256 == second.schema_sha256
        assert first.projection_sha256 == second.projection_sha256
        assert first.mechanism == second.mechanism
        assert (
            first.max_output_tokens
            == second.max_output_tokens
            == (8192 if first.selection.scenario == "grouped_scores" else 4096)
        )
        assert first.request_uid != second.request_uid
        assert first.payload_sha256 != second.payload_sha256
        bodies = [json.loads(c.request_body_utf8) for c in (first, second)]
        for case in (first, second):
            spec = spec_for(proposal, proposal.calls.index(case))
            assert spec.generation is not None and spec.generation.top_p is None
            assert spec.execution.tier == (
                "standard_only" if spec.provider == "anthropic" else "flex"
            )
            assert not spec.execution.explanations and not spec.allow_ties
            assert (
                case.reserved_input_tokens == 32768
                and case.reserved_output_tokens == 8192
            )
            assert case.payload_sha256 == bytes_digest(case.request_body_utf8.encode())
            with prepare(
                spec, SOURCE, directory / "preparation" / case.case_id, case.run_id
            ) as prepared:
                wires = list(request_for(prepared, case.selection))
                assert len(wires) == 1
                output = wires[0].output_commitment
                assert output.selection.kind == "qualification_probe"
                if case.selection.control_condition == "A":
                    assert output.reasoning_policy is None
                    assert spec.generation.reasoning is None
                else:
                    assert output.reasoning_policy is not None
                    assert spec.generation.reasoning is not None
                    assert spec.generation.reasoning.mode is None
                    assert spec.generation.reasoning.effort == CONTRASTS[spec.model]
                await sdk_bytes(spec, wires)
            if proposal.suite == "reasoning_comparison":
                assert spec.generation.temperature is None
            else:
                assert spec.provider == "openai"
                assert spec.generation.reasoning is not None
                assert spec.generation.reasoning.effort == "none"
        if proposal.suite == "temperature_acceptance":
            assert (
                "temperature" not in bodies[0] and bodies[1].pop("temperature") == 0.2
            )
        elif first.selection.provider == "openai":
            assert "reasoning" not in bodies[0]
            assert bodies[1].pop("reasoning") == {"effort": "none"}
        elif first.selection.provider == "anthropic":
            assert "effort" not in bodies[0]["output_config"]
            assert bodies[1]["output_config"].pop("effort") == "low"
            assert "thinking" not in bodies[0] and "thinking" not in bodies[1]
        else:
            assert "thinkingConfig" not in bodies[0]["generationConfig"]
            assert bodies[1]["generationConfig"].pop("thinkingConfig") == {
                "thinking_level": CONTRASTS[first.selection.model].upper()
            }
        assert bodies[0] == bodies[1]
    with pytest.raises(ValueError, match="sealed"):
        authorization(proposal).verify(proposal)


def test_control_cli_has_all_candidates_and_defaults_are_only_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["controls"]) == 0
    matrix = json.loads(capsys.readouterr().out)
    assert len(matrix["rows"]) == 32
    for row in matrix["rows"]:
        assert row["strict_production_preflight"] == "denied"
        assert not row["live_observations"] and not row["live_qualified"]
        if row["selection"]["control_condition"] == "A":
            assert set(row["generation"]) == {"max_output_tokens"}
        assert row["documentation_urls"] and row["documented_default_effort"]


@pytest.mark.parametrize("provider,model", MODELS)
@pytest.mark.parametrize(
    "change",
    [
        {"reasoning": {"effort": "medium"}, "temperature": 0.2},
        {"reasoning": {"effort": "high"}, "temperature": 0.2},
        {"reasoning": {"effort": "minimal"}},
        {"reasoning": {"effort": "low", "mode": "disabled"}},
        {"reasoning": {"effort": "none", "mode": "pro"}},
        {"reasoning": {"effort": "none"}, "top_p": 0.8},
        {"temperature": 0.2},
    ],
)
def test_unsupported_study_controls_fail_before_credentials(
    monkeypatch: pytest.MonkeyPatch, provider: str, model: str, change: dict[str, Any]
) -> None:
    def deny(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Preflight resolved credentials")

    monkeypatch.setattr("judgmentllm._auth._resolve_credentials", deny)
    selection = suite_selections("reasoning_comparison")[
        next(
            i
            for i, s in enumerate(suite_selections("reasoning_comparison"))
            if s.model == model
        )
    ]
    _, spec = selected_configuration(PACKAGE, selection)
    assert spec.provider == provider
    spec.generation = GenerationSpec.model_validate(
        {"max_output_tokens": 8192} | change
    )
    with pytest.raises((ValueError, ProviderError)):
        candidate_context(spec, SOURCE)


@pytest.mark.parametrize(
    "change",
    [
        {"model": "unknown-model"},
        {"provider": "gemini"},
        {"tier": "default"},
        {"scenario": "grouped_explanations"},
        {"trait_id": "other"},
        {"reversed": True},
    ],
)
def test_control_selection_cannot_inherit_another_scope(change: dict[str, Any]) -> None:
    selection = suite_selections("temperature_acceptance")[0]
    with pytest.raises(ValueError, match="Invalid evidence record"):
        Selection.model_validate_json(
            canonical_bytes(selection.model_copy(update=change))
        )


def test_independent_extended_consent_and_durable_cross_suite_hold(
    tmp_path: Path,
    pilot: tuple[Path, Proposal],
    control_proposal: tuple[Path, Proposal],
) -> None:
    original = seal(pilot[1], tmp_path / "proofs")
    extension = seal(control_proposal[1], tmp_path / "proofs")
    consent = authorization(extension)
    for change in (
        {"campaign_calls": 66, "campaign_usd": Decimal(10), "campaign_seconds": 39600},
        {"campaign_usd": Decimal(15)},
        {"suite": "native_pilot"},
    ):
        with pytest.raises(ValueError):
            Authorization.model_validate_json(
                canonical_bytes(consent.model_copy(update=change))
            ).verify(extension)
    now = datetime(2026, 11, 13, tzinfo=UTC)
    root = tmp_path / "campaign"
    with BudgetLedger(root).locked() as ledger:
        ledger.authorize(original, authorization(original), now=now)
        header = (root / "campaign.json").read_bytes()
        assert not (root / "campaign-extension.json").exists()
        ledger.authorize(extension, consent, now=now)
        assert (root / "campaign.json").read_bytes() == header
        assert json.loads(header)["usd"] == "10"
        assert (
            json.loads((root / "campaign-extension.json").read_bytes())["usd"] == "16"
        )
        ledger.reserve(extension, extension.calls[0], now=now)
    with BudgetLedger(root).locked() as ledger:
        with pytest.raises(BudgetStop, match="uncertain"):
            ledger.reserve(original, original.calls[0], now=now)
        ledger.finish(
            extension,
            extension.calls[0],
            terminal=False,
            reason="missing_tier",
            evidence="a" * 64,
            now=now + timedelta(seconds=1),
        )
    with BudgetLedger(root).locked() as ledger:
        ledger.authorize(extension, consent, now=now + timedelta(seconds=2))
        with pytest.raises(BudgetStop, match="hold"):
            ledger.reserve(original, original.calls[0], now=now + timedelta(seconds=2))
        assert len([e for e in ledger.events() if e.kind == "reserve"]) == 1
    with BudgetLedger(tmp_path / "fresh-campaign").locked() as ledger:
        with pytest.raises(ValueError, match="Immutable"):
            ledger.authorize(extension, consent)


@pytest.mark.parametrize(
    "scope,field,value,reason",
    [
        ("aggregate", "count", 98, "Campaign calls"),
        ("aggregate", "reserved_usd", Decimal(16), "Campaign USD"),
        ("aggregate", "seconds", 68400, "Campaign active time"),
        ("original", "count", 66, "Original campaign calls"),
        ("original", "reserved_usd", Decimal(10), "Original campaign USD"),
        ("original", "seconds", 39600, "Original campaign active time"),
    ],
)
def test_aggregate_and_original_subcaps_remain_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pilot: tuple[Path, Proposal],
    control_proposal: tuple[Path, Proposal],
    scope: str,
    field: str,
    value: int | Decimal,
    reason: str,
) -> None:
    original = seal(pilot[1], tmp_path / "proofs")
    extension = seal(control_proposal[1], tmp_path / "proofs")
    with BudgetLedger(tmp_path / "campaign").locked() as ledger:
        ledger.authorize(original, authorization(original))
        ledger.authorize(extension, authorization(extension))
        starts = ledger.events()
        proposal = original if scope == "original" else extension
        # Isolate each ceiling from the journal's independently tested admission.
        prior = starts[0].model_copy(
            update={
                "kind": "reserve",
                "suite": "tier_feasibility",
                "reserved_usd": value if field == "reserved_usd" else Decimal(0),
                "seconds": value if field == "seconds" else 0,
            }
        )
        events = starts + [prior] * (int(value) if field == "count" else 1)
        events.append(prior.model_copy(update={"kind": "terminal"}))
        monkeypatch.setattr(ledger, "events", lambda: events)
        with pytest.raises(BudgetStop, match=reason):
            ledger.reserve(proposal, proposal.calls[0])


@pytest.mark.parametrize("index", [1, 3])
@pytest.mark.asyncio
async def test_control_execution_privacy_recovery_and_changed_control_rejection(
    tmp_path: Path,
    control_proposal: tuple[Path, Proposal],
    index: int,
) -> None:
    directory, proofs, proposal, case, spec = case_files(
        tmp_path, control_proposal, index
    )
    body, headers = response_body(spec, assessment(spec))
    canary = "PRIVATE_CONTROL_REASONING_CANARY"
    body["output"].insert(
        0,
        {
            "type": "reasoning",
            "id": "private-reasoning",
            "summary": [{"type": "summary_text", "text": canary}],
        },
    )
    sent: list[bytes] = []

    def handler(request: Any) -> Any:
        assert request.content == case.request_body_utf8.encode()
        sent.append(bytes(request.content))
        return httpx2.Response(200, json=body, headers=headers, request=request)

    root = tmp_path / "campaign"
    with BudgetLedger(root).locked() as ledger:
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
            for _ in range(2):
                await execute_run(
                    case.run_id,
                    store=directory / "preparation" / case.case_id,
                    spec=spec,
                    adapter=adapter,
                    retry_policy=RetryPolicy(max_attempts=1),
                )
            recovered = await recover_run(
                case.run_id,
                store=directory / "preparation" / case.case_id,
                spec=spec,
                adapter=adapter,
            )
            assert not recovered.recovery_required
            assert len(sent) == 1
            altered = spec.model_copy(deep=True)
            altered.generation = GenerationSpec(
                max_output_tokens=case.max_output_tokens
            )
            with pytest.raises((ValueError, ProviderError)):
                adapter.validate_new_run(altered)
            with pytest.raises((ValueError, ProviderError, PreparationError)):
                with prepare(
                    altered,
                    SOURCE,
                    directory / "preparation" / case.case_id,
                    case.run_id,
                ):
                    pass
        finally:
            await adapter.aclose()
    report = case_report(directory, case)
    assert report["canonical_valid"] and report["result_count"] == (
        12 if index == 1 else 1
    )
    assert spec.generation is not None
    assert report["requested_generation"] == spec.generation.model_dump(mode="json")
    with ReadOnlyRepository(directory / "preparation" / case.case_id) as repo:
        results = list(repo.iter_results(case.run_id))
        assert all(r.status == "complete" for r in results)
        if index == 3:
            assert results[0].task_kind == "pairwise"
            assert results[0].winner_id == results[0].presented_order[0]
    for path in directory.rglob("*"):
        if path.is_file():
            assert canary.encode() not in path.read_bytes()
