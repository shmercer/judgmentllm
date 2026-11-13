"""Corrupt independent and local budget state without forwarding any request."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from study_fixture import PACKAGE, authorization, seal
from test_study_native import offline as offline
from test_study_native import pilot as pilot

from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.study.budget import BudgetLedger
from judgmentllm.study.definitions import source_inventory
from judgmentllm.study.records import Proposal


def test_independent_checkpoint_and_root_identity_corruption(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    with BudgetLedger(tmp_path / "campaign").locked() as ledger:
        ledger.authorize(
            proposal, authorization(proposal), now=datetime(2026, 11, 13, tzinfo=UTC)
        )
        original_events = ledger.events()
        directory = ledger._checkpoints
        saved_directory = directory.with_name(directory.name + "-saved")
        directory.rename(saved_directory)
        directory.symlink_to(saved_directory, target_is_directory=True)
        with pytest.raises(ValueError, match="directory is a symlink"):
            ledger.events()
        directory.unlink()
        saved_directory.rename(directory)
        first = directory / "000000.json"
        saved_first = first.with_suffix(".saved")
        first.rename(saved_first)
        first.symlink_to(saved_first)
        with pytest.raises(ValueError, match="not a regular file"):
            ledger.events()
        first.unlink()
        saved_first.rename(first)
        raw = first.read_bytes()
        data = json.loads(raw)
        first.write_bytes(canonical_bytes(data | {"relative_path": "../escape"}))
        with pytest.raises(ValueError, match="commitment"):
            ledger.events()
        first.write_bytes(raw)
        marker = ledger._root_marker
        saved_marker = marker.with_suffix(".saved")
        marker.rename(saved_marker)
        marker.symlink_to(saved_marker)
        with pytest.raises(ValueError, match="checkpoint is missing"):
            ledger.events()
        marker.unlink()
        marker.mkdir()
        with pytest.raises(ValueError, match="marker is not a regular"):
            ledger.events()
        marker.rmdir()
        with pytest.raises(ValueError, match="root identity"):
            ledger.events()
        with pytest.raises(ValueError, match="Previously authorized"):
            ledger.authorize(proposal, authorization(proposal))
        saved_marker.rename(marker)
        raw_marker = marker.read_bytes()
        marker.write_bytes(
            canonical_bytes(
                json.loads(raw_marker) | {"campaign_root": str(tmp_path / "other")}
            )
        )
        with pytest.raises(ValueError, match="root identity"):
            ledger.events()
        marker.write_bytes(raw_marker)
        directory.rename(saved_directory)
        with pytest.raises(ValueError, match="checkpoint is missing"):
            ledger.events()
        with pytest.raises(ValueError, match="Previously authorized"):
            ledger.authorize(proposal, authorization(proposal))
        saved_directory.rename(directory)
        link = ledger.root / "uncommitted-link"
        link.symlink_to(ledger.root / "campaign.json")
        with pytest.raises(ValueError, match="journal inventory contains a symlink"):
            ledger.events()
        link.unlink()
        with pytest.raises(ValueError, match="Immutable"):
            ledger._publish("campaign.json", b"different bytes")
        (source_marker,) = (p for p in marker.parent.glob("*.json") if p != marker)
        saved_source = source_marker.with_suffix(".saved")
        source_marker.rename(saved_source)
        source_marker.symlink_to(saved_source)
        with pytest.raises(ValueError, match="source binding is not a regular"):
            ledger.authorize(proposal, authorization(proposal))
        source_marker.unlink()
        saved_source.rename(source_marker)
        intent = ledger.root / "suite-intents/native_pilot.json"
        raw_intent = intent.read_bytes()
        intent.write_bytes(
            canonical_bytes(
                json.loads(raw_intent) | {"started_at": "2026-11-12T00:00:00+00:00"}
            )
        )
        with pytest.raises(ValueError, match="start/deadline"):
            ledger.events()
        intent.write_bytes(raw_intent)
        incomplete = intent.with_name("tier_feasibility.json")
        incomplete.write_bytes(raw_intent)
        with pytest.raises(ValueError, match="start is incomplete"):
            ledger.events()
        incomplete.unlink()
        assert ledger.events() == original_events


@pytest.mark.parametrize("component", ["sources", "cj"])
def test_imported_source_directory_links_are_not_followed(
    tmp_path: Path, component: str
) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "original-manifests.json").write_bytes(
        (PACKAGE / "original-manifests.json").read_bytes()
    )
    if component == "sources":
        (package / "sources").symlink_to(PACKAGE / "sources", target_is_directory=True)
    else:
        (package / "sources").mkdir()
        (package / "sources/cj").symlink_to(
            PACKAGE / "sources/cj", target_is_directory=True
        )
    with pytest.raises(ValueError, match="symlinks"):
        source_inventory(package, "cj")
