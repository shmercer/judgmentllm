"""Immutable consent, independent ceilings, durable holds and journal tampering."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from study_fixture import authorization, seal
from test_study_native import offline as offline

from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.study.budget import BudgetLedger, BudgetStop, immutable
from judgmentllm.study.records import Case, Proposal

NOW = datetime(2026, 11, 13, tzinfo=UTC)


def test_durable_order_reservations_restart_and_review_hold(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    cases = proposal.calls
    root = tmp_path / "campaign"
    with BudgetLedger(root).locked() as ledger:
        ledger.authorize(proposal, authorization(proposal), now=NOW)
        with pytest.raises(BudgetStop, match="order"):
            ledger.reserve(proposal, cases[1], now=NOW)
        assert ledger.reserve(proposal, cases[0], now=NOW, commit=False) is None
        assert not [e for e in ledger.events() if e.kind == "reserve"]
        ledger.reserve(proposal, cases[0], now=NOW)
        with pytest.raises(BudgetStop, match="uncertain"):
            ledger.reserve(proposal, cases[1], now=NOW)
    with BudgetLedger(root).locked() as ledger:
        assert len([e for e in ledger.events() if e.kind == "reserve"]) == 1
        with pytest.raises(BudgetStop, match="uncertain"):
            ledger.reserve(proposal, cases[0], now=NOW)
        ledger.finish(
            proposal,
            cases[0],
            terminal=True,
            reason="accepted_invalid_no_rescore",
            evidence="a" * 64,
            now=NOW + timedelta(seconds=1),
        )
        with pytest.raises(BudgetStop, match="twice"):
            ledger.finish(
                proposal,
                cases[0],
                terminal=True,
                reason="replay",
                evidence="a" * 64,
                now=NOW,
            )
        with pytest.raises(BudgetStop, match="backwards"):
            ledger.reserve(proposal, cases[1], now=NOW)
        with pytest.raises(BudgetStop, match="deadline"):
            ledger.reserve(proposal, cases[1], now=NOW + timedelta(seconds=28800))
        ledger.reserve(proposal, cases[1], now=NOW + timedelta(seconds=2))
        ledger.finish(
            proposal,
            cases[1],
            terminal=False,
            reason="ambiguous_429_or_503",
            evidence="b" * 64,
            now=NOW + timedelta(seconds=3),
        )
    with BudgetLedger(root).locked() as ledger:
        with pytest.raises(BudgetStop, match="hold"):
            ledger.reserve(proposal, cases[2], now=NOW + timedelta(seconds=4))
        reserved = [e for e in ledger.events() if e.kind == "reserve"]
        assert len(reserved) == 2 and sum(e.input_tokens for e in reserved) == 65536
        assert sum(e.output_tokens for e in reserved) == 16384
        assert sum(e.reserved_usd for e in reserved) == sum(
            c.reserved_usd or Decimal(0) for c in cases[:2]
        )


def test_budget_limited_incomplete_and_nontransferable_money(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    calls: list[Case] = []
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
        calls.append(
            case.model_copy(
                update={"price": price, "reserved_usd": price.reservation(32768, 8192)}
            )
        )
    proposal = Proposal.model_validate_json(
        canonical_bytes(proposal.model_copy(update={"calls": tuple(calls)}))
    )
    with BudgetLedger(tmp_path / "campaign").locked() as ledger:
        ledger.authorize(proposal, authorization(proposal), now=NOW)
        ledger.reserve(proposal, proposal.calls[0], now=NOW)
        ledger.finish(
            proposal,
            proposal.calls[0],
            terminal=True,
            reason="terminal",
            evidence="a" * 64,
            now=NOW + timedelta(seconds=1),
        )
        with pytest.raises(BudgetStop, match="USD"):
            ledger.reserve(proposal, proposal.calls[1], now=NOW + timedelta(seconds=2))
        assert len([e for e in ledger.events() if e.kind == "reserve"]) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_calls", 43),
        ("ceiling_usd", "6"),
        ("elapsed_seconds", 28801),
        ("concurrency", 2),
        ("automatic_retries", 1),
        ("authorized_calls", 1),
    ],
)
def test_proposal_budget_tampering_rejected(
    tmp_path: Path, pilot: tuple[Path, Proposal], field: str, value: Any
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    with pytest.raises(ValueError):
        Proposal.model_validate_json(
            json.dumps(proposal.model_dump(mode="json") | {field: value})
        )


@pytest.mark.parametrize(
    "change",
    [
        {"reserved_input_tokens": 32769},
        {"reserved_output_tokens": 8193},
        {"max_output_tokens": 8192},
        {"operation_deadline_seconds": 90},
        {"payload_sha256": "b" * 64},
        {"specification_sha256": "b" * 64},
        {"reserved_usd": "0"},
        {"blockers": ["fabricated"]},
    ],
)
def test_case_budget_and_commitment_tampering(
    tmp_path: Path, pilot: tuple[Path, Proposal], change: dict[str, Any]
) -> None:
    case = seal(pilot[1], tmp_path / "proofs").calls[0]
    with pytest.raises(ValueError):
        Case.model_validate_json(json.dumps(case.model_dump(mode="json") | change))


def test_cancellation_and_deadline_survive_restart(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    ledger = BudgetLedger(tmp_path / "campaign")
    with pytest.raises(ValueError, match="ownership"):
        ledger.authorize(proposal, authorization(proposal), now=NOW)
    with ledger.locked():
        ledger.authorize(proposal, authorization(proposal), now=NOW)
        ledger.reserve(proposal, proposal.calls[0], now=NOW)
        ledger.finish(
            proposal,
            proposal.calls[0],
            terminal=True,
            reason="too_late",
            evidence="a" * 64,
            now=NOW + timedelta(seconds=961),
        )
        assert ledger.events()[-1].reason == "operation_deadline_exceeded"
        ledger.cancel(proposal, now=NOW + timedelta(seconds=962))
    with BudgetLedger(ledger.root).locked() as reopened:
        with pytest.raises(BudgetStop):
            reopened.reserve(
                proposal, proposal.calls[1], now=NOW + timedelta(seconds=963)
            )


def test_intent_and_journal_tampering_stop_replay(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    ledger = BudgetLedger(tmp_path / "campaign")
    with ledger.locked():
        ledger.authorize(proposal, authorization(proposal), now=NOW)
        ledger.reserve(proposal, proposal.calls[0], now=NOW)
        path = ledger.root / "events/000000.json"
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'"sequence":0', b'"sequence":1'))
        with pytest.raises(ValueError, match="journal"):
            ledger.events()
        path.write_bytes(raw)
        intent = next((ledger.root / "forward-intents").glob("*.json"))
        intent.write_text("replaced")
        with pytest.raises(BudgetStop, match="intent"):
            ledger.events()
    with pytest.raises(ValueError, match="differs"):
        immutable(ledger.root / "campaign.json", b"replacement")
