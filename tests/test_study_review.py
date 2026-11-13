"""Independent review regressions: lost budgets, complete inventories and billing."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from study_fixture import PACKAGE, authorization, original_files, seal
from test_study_native import offline as offline
from test_study_native import pilot as pilot
from test_study_native import tier as tier

from judgmentllm.study import budget, cli, plans
from judgmentllm.study.budget import BudgetLedger
from judgmentllm.study.records import Case, Eligibility, Proposal
from judgmentllm.study.sources import verify_originals

NOW = datetime(2026, 11, 13, tzinfo=UTC)


@pytest.mark.parametrize("damage", ["header", "start", "root", "rollback"])
@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("other_suite", [False, True])
def test_lost_or_rolled_back_state_never_renews_either_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pilot: tuple[Path, Proposal],
    tier: tuple[Path, Proposal],
    damage: str,
    uncertain: bool,
    other_suite: bool,
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    target = seal(tier[1], tmp_path / "tier-proofs") if other_suite else proposal
    root = tmp_path / "campaign"
    with BudgetLedger(root).locked() as ledger:
        ledger.authorize(proposal, authorization(proposal), now=NOW)
    snapshot = tmp_path / "older-snapshot"
    shutil.copytree(root, snapshot)
    with BudgetLedger(root).locked() as ledger:
        if uncertain:
            original = budget.immutable

            def crash(path: Path, raw: bytes) -> None:
                if path == root / "events/000001.json":
                    raise OSError(
                        "synthetic death after independent forwarding commitment"
                    )
                original(path, raw)

            with monkeypatch.context() as killed:
                killed.setattr(budget, "immutable", crash)
                with pytest.raises(OSError):
                    ledger.reserve(proposal, proposal.calls[0], now=NOW)
        else:
            ledger.reserve(proposal, proposal.calls[0], now=NOW)
            ledger.finish(
                proposal,
                proposal.calls[0],
                terminal=True,
                reason="synthetic terminal",
                evidence="a" * 64,
                now=NOW + timedelta(seconds=1),
            )
        checkpoints = {
            p.name: p.read_bytes() for p in ledger._checkpoints.glob("*.json")
        }
    if damage == "header":
        (root / "campaign.json").unlink()
    elif damage == "start":
        (root / "events/000000.json").unlink()
    else:
        shutil.rmtree(root)
        if damage == "rollback":
            shutil.copytree(snapshot, root)
    with BudgetLedger(root).locked() as restarted:
        with pytest.raises((OSError, ValueError)):
            restarted.authorize(
                target, authorization(target), now=NOW + timedelta(seconds=2)
            )
            restarted.reserve(target, target.calls[0], now=NOW + timedelta(seconds=2))
        assert {
            p.name: p.read_bytes() for p in restarted._checkpoints.glob("*.json")
        } == checkpoints


def test_untouched_restart_preserves_consumption_and_original_deadline(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    root = tmp_path / "campaign"
    with BudgetLedger(root).locked() as ledger:
        ledger.authorize(proposal, authorization(proposal), now=NOW)
        ledger.reserve(proposal, proposal.calls[0], now=NOW)
        ledger.finish(
            proposal,
            proposal.calls[0],
            terminal=True,
            reason="synthetic terminal",
            evidence="a" * 64,
            now=NOW,
        )
        original = ledger.events()
    with BudgetLedger(root).locked() as ledger:
        ledger.authorize(
            proposal, authorization(proposal), now=NOW + timedelta(hours=1)
        )
        assert ledger.events() == original
        ledger.reserve(proposal, proposal.calls[1], now=NOW + timedelta(hours=1))
        assert len([e for e in ledger.events() if e.kind == "reserve"]) == 2


@pytest.mark.parametrize(
    "damage", ["missing", "extra", "symlink", "directory", "bytes"]
)
def test_complete_import_inventory_is_required_by_every_entrypoint(
    tmp_path: Path, damage: str
) -> None:
    package = tmp_path / "package"
    shutil.copytree(PACKAGE, package)
    originals = tmp_path / "originals"
    original_files(originals)
    path = package / "sources/cj/original_rubric_source.txt"
    if damage == "missing":
        path.unlink()
    elif damage == "extra":
        path.with_name("unexpected.txt").write_text("synthetic extra")
    elif damage == "symlink":
        path.unlink()
        path.symlink_to(PACKAGE / "sources/cj/original_rubric_source.txt")
    elif damage == "directory":
        path.with_name("unexpected-directory").mkdir()
    else:
        path.write_text("altered unused source")
    with pytest.raises(ValueError):
        verify_originals(originals, package)
    for command in ("verify", "configure", "verify-originals"):
        args = ["--package", str(package), command]
        if command == "verify-originals":
            args += ["--source", str(originals)]
        assert cli.main(args) == 2
    proposal = tmp_path / "proposal"
    with pytest.raises(ValueError):
        plans.build_proposal(package, proposal, "native_pilot")
    assert not proposal.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX FIFO type; symlink and directory guards run everywhere",
)
def test_nonregular_imported_entry_is_rejected_without_reading_it(
    tmp_path: Path,
) -> None:
    from judgmentllm.study.definitions import source_inventory

    package = tmp_path / "package"
    shutil.copytree(PACKAGE, package)
    os.mkfifo(package / "sources/cj/unexpected-pipe")
    with pytest.raises(ValueError, match="entry"):
        source_inventory(package, "cj")


@pytest.mark.parametrize("index", [0, 14])
def test_flex_prices_cover_standard_and_all_independently_reviewed_outcomes(
    tmp_path: Path, pilot: tuple[Path, Proposal], index: int
) -> None:
    case = seal(pilot[1], tmp_path / "proofs").calls[index]
    assert case.price is not None and case.eligibility is not None
    for tiers in (("flex",), ("standard", "flex")):
        price = case.price.model_copy(
            update={
                "outcomes": tuple(
                    p for p in case.price.outcomes if p.served_tier in tiers
                )
            }
        )
        with pytest.raises(ValueError):
            case.model_copy(
                update={"price": price, "reserved_usd": price.reservation(32768, 8192)}
            )
    for tiers in (
        ("flex",),
        ("standard", "flex", "unknown"),
        ("standard", "flex", "flex"),
    ):
        with pytest.raises(ValueError):
            Case.model_validate(
                case.model_dump()
                | {
                    "eligibility": case.eligibility.model_copy(
                        update={"billable_served_tiers": tiers}
                    )
                }
            )
    data: dict[str, Any] = case.eligibility.model_dump()
    del data["served_outcomes_verified"]
    with pytest.raises(ValueError):
        Eligibility.model_validate(data)
