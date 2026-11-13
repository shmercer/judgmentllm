"""Fail closed on changed study bytes, independent proofs and budget evidence."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import copy
import json
import runpy
import shutil
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from study_fixture import PACKAGE, SOURCE, authorization, original_files, seal, spec_for
from test_study_native import offline as offline
from test_study_native import pilot as pilot

from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.providers.protocol import ProviderIntegrityError
from judgmentllm.study import cli, execution, plans, sources
from judgmentllm.study.budget import BudgetLedger, BudgetStop, Event, immutable
from judgmentllm.study.definitions import configuration
from judgmentllm.study.records import Authorization, Case, Proposal

NOW = datetime(2026, 11, 13, tzinfo=UTC)


def test_original_verification_detects_migrated_and_rendered_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "originals"
    original_files(source)
    package = tmp_path / "package"
    shutil.copytree(PACKAGE, package)
    path = package / "sources/rubric/prompts/all_12_score_only.txt"
    original = path.read_bytes()
    path.write_text("Changed rubric")
    with pytest.raises(ValueError, match="Migrated"):
        sources.verify_originals(source, package)
    path.write_bytes(original)

    def drifted(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(text="Changed rendered output", output_schema={})

    original_preview = sources.preview_prompt
    monkeypatch.setattr(sources, "preview_prompt", drifted)
    with pytest.raises(ValueError, match="preview drift"):
        sources.verify_originals(source, package)
    monkeypatch.setattr(sources, "preview_prompt", original_preview)
    rows = sources.archive(PACKAGE / "original-previews.jsonl.gz.base64")

    def extra_preview(*args: Any) -> list[dict[str, Any]]:
        return rows + [rows[0] | {"name": "extra"}]

    monkeypatch.setattr(sources, "archive", extra_preview)
    with pytest.raises(ValueError, match="Incomplete"):
        sources.verify_originals(source, package)


def _write_proposal(directory: Path, proposal: Proposal) -> str:
    directory.mkdir(exist_ok=True)
    (directory / "proposal.json").write_bytes(canonical_bytes(proposal))
    (directory / "proposal.sha256").write_text(proposal.digest + "\n")
    return proposal.digest


def test_configuration_cli_entrypoints_and_scope_rejections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    copy = tmp_path / "package"
    shutil.copytree(PACKAGE, copy)
    manifest = copy / "original-manifests.json"
    raw_manifest = manifest.read_bytes()
    manifest.write_text("{}")
    with pytest.raises(ValueError, match="manifest"):
        configuration(copy, "openai", "gpt-6-luna", "cj", "flex")
    manifest.write_bytes(raw_manifest)
    assert cli.main(["--package", str(copy), "configure"]) == 0
    capsys.readouterr()
    assert cli.main(["eligibility"]) == 0
    assert len(json.loads(capsys.readouterr().out)["rows"]) == 18
    with pytest.raises(ValueError, match="Unknown"):
        configuration(PACKAGE, "openai", "other-model", "cj", "flex")
    with pytest.raises(ValueError, match="Unknown"):
        plans.suite_selections(cast(Any, "other-suite"))
    index = copy / "configurations.json"
    original = index.read_bytes()
    data = json.loads(original)
    data["configurations"] = data["configurations"][:-1]
    index.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="counts"):
        cli.verify_library(copy)
    data = json.loads(original)
    data["configurations"][0]["scenario"] = "cj"
    index.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="identity"):
        cli.verify_library(copy)
    index.write_bytes(original)
    config = copy / data["configurations"][0]["file"]
    config_data = json.loads(config.read_text())
    config_data["model"] = "unqualified-different-model"
    config.write_text(json.dumps(config_data))
    with pytest.raises(ValueError, match="content"):
        cli.verify_library(copy)
    from judgmentllm.providers.openai import OpenAIAdapter

    def admitted(*args: Any) -> None:
        return None

    monkeypatch.setattr(OpenAIAdapter, "validate_new_run", admitted)
    with pytest.raises(ValueError, match="Unexpected production"):
        cli.eligibility_matrix(PACKAGE)
    monkeypatch.setattr(sys, "argv", ["study", "verify"])
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("judgmentllm.study", run_name="__main__")
    assert exited.value.code == 0
    with pytest.warns(RuntimeWarning, match="found in sys.modules"):
        with pytest.raises(SystemExit) as exited:
            runpy.run_module("judgmentllm.study.cli", run_name="__main__")
    assert exited.value.code == 0


def test_cli_plan_and_cancel_route_requires_existing_independent_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pilot: tuple[Path, Proposal],
) -> None:
    def planned(*args: Any) -> dict[str, bool]:
        return {"synthetic_plan": True}

    monkeypatch.setattr(cli, "full_plan", planned)
    assert cli.main(["plan", "--output", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["synthetic_plan"]
    proposal = seal(pilot[1], tmp_path / "proofs")

    def loaded(*args: Any) -> Proposal:
        return proposal

    monkeypatch.setattr(cli, "load_proposal", loaded)
    args = ["cancel", "--proposal", str(pilot[0]), "--proposal-sha256", proposal.digest]
    assert cli.main(args) == 2
    campaign = tmp_path / "campaign"
    with BudgetLedger(campaign).locked() as ledger:
        ledger.authorize(proposal, authorization(proposal))
    assert cli.main(args + ["--campaign", str(campaign)]) == 0
    capsys.readouterr()
    assert BudgetLedger(campaign).events()[-1].kind == "cancel"


def test_consent_exact_types_dates_and_case_boundaries(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    for change in (
        {"concurrency": True},
        {"synthetic_only": 1},
        {"max_calls": 42.0},
        {"study_version": "other"},
    ):
        with pytest.raises(ValueError):
            Proposal.model_validate(proposal.model_dump() | change)
    consent = authorization(proposal)
    for change in (
        {"accept_billable_calls": 1},
        {"accept_synthetic_capture": False},
        {"authorized_at": NOW.replace(tzinfo=None)},
        {"authorized_at": datetime.now(UTC) + timedelta(days=1)},
        {"proposal_sha256": "b" * 64},
    ):
        with pytest.raises(ValueError):
            Authorization.model_validate(consent.model_dump() | change).verify(proposal)
    case = proposal.calls[0]
    assert case.price is not None and case.eligibility is not None
    forged_case = copy.deepcopy(case)
    object.__setattr__(forged_case, "reserved_usd", Decimal(0))
    forged = copy.deepcopy(proposal)
    object.__setattr__(forged, "calls", (forged_case,) + proposal.calls[1:])
    with BudgetLedger(tmp_path / "forged-campaign").locked() as ledger:
        with pytest.raises(ValueError):
            ledger.authorize(forged, authorization(forged))
    for change in (
        {"price": None},
        {"reserved_usd": None},
        {"price": case.price.model_copy(update={"account_scope": "other"})},
        {
            "price": case.price.model_copy(
                update={"outcomes": (case.price.outcomes[0],)}
            ),
        },
        {"eligibility": case.eligibility.model_copy(update={"features": ()})},
    ):
        with pytest.raises(ValueError):
            Case.model_validate(case.model_dump() | change)


def test_proofs_are_exact_private_account_and_credential_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pilot: tuple[Path, Proposal]
) -> None:
    proofs = tmp_path / "proofs"
    proposal = seal(pilot[1], proofs)
    case = proposal.calls[0]
    assert case.eligibility is not None
    with pytest.raises(ValueError, match="unknown"):
        execution._verify_proofs(pilot[1].calls[0], proofs, credentials=False)
    path = proofs / (case.eligibility.evidence_sha256 + ".json")
    original = path.read_bytes()
    path.write_text("changed")
    with pytest.raises(ValueError, match="commitment"):
        execution._verify_proofs(case, proofs, credentials=False)
    path.write_bytes(original)
    binding = execution.CredentialBinding.model_validate_json(
        (proofs / (case.eligibility.binding_sha256 + ".json")).read_bytes()
    )
    for change in ({"account_scope": "other"}, {"credential_source": "GOOGLE_API_KEY"}):
        updated_binding = binding.model_copy(update=change)
        raw = canonical_bytes(updated_binding)
        digest = bytes_digest(raw)
        (proofs / (digest + ".json")).write_bytes(raw)
        eligibility = case.eligibility.model_copy(update={"binding_sha256": digest})
        if "credential_source" in change:
            eligibility = eligibility.model_copy(update=change)
        updated = case.model_copy(update={"eligibility": eligibility})
        with pytest.raises(ValueError, match="binding|exact provider"):
            execution._verify_proofs(updated, proofs, credentials=False)
    from judgmentllm import _auth

    def resolved(*args: Any) -> SimpleNamespace:
        return SimpleNamespace(
            source="OPENAI_API_KEY", _api_key=lambda: "other-synthetic-key"
        )

    monkeypatch.setattr(_auth, "_resolve_credentials", resolved)
    with pytest.raises(ProviderIntegrityError, match="bound account"):
        execution._verify_proofs(case, proofs, credentials=True)


def test_proposal_reopening_refuses_scope_bytes_paths_and_sdk_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pilot: tuple[Path, Proposal]
) -> None:
    proposal = pilot[1]
    directory = tmp_path / "proposal"
    digest = _write_proposal(directory, proposal)
    path = directory / "proposal.json"
    path.rename(directory / "actual.json")
    path.symlink_to(directory / "actual.json")
    with pytest.raises(ValueError, match="symlink"):
        plans.load_proposal(directory, digest)
    path.unlink()
    path.write_bytes((directory / "actual.json").read_bytes())
    (directory / "proposal.sha256").write_text("b" * 64)
    with pytest.raises(ValueError, match="commitment"):
        plans.load_proposal(directory, digest)
    swapped = proposal.model_copy(update={"calls": tuple(reversed(proposal.calls))})
    with pytest.raises(ValueError, match="scope/order"):
        plans.load_proposal(directory, _write_proposal(directory, swapped))
    case = proposal.calls[0]
    for field in ("case_id", "run_id", "configuration_id", "sdk_version"):
        changed = case.model_copy(update={field: "other"})
        updated = proposal.model_copy(update={"calls": (changed,) + proposal.calls[1:]})
        with pytest.raises(ValueError, match="identity|configuration|scope"):
            plans.load_proposal(directory, _write_proposal(directory, updated))
    changed_spec = json.loads(case.specification_json)
    changed_spec["execution"]["concurrency"] = 2
    raw_spec = canonical_bytes(changed_spec)
    changed = case.model_copy(
        update={
            "specification_json": raw_spec.decode(),
            "specification_sha256": bytes_digest(raw_spec),
        }
    )
    updated = proposal.model_copy(update={"calls": (changed,) + proposal.calls[1:]})
    with pytest.raises(ValueError, match="source/configuration"):
        plans.load_proposal(directory, _write_proposal(directory, updated))
    _write_proposal(directory, proposal)
    original_version = plans.importlib.metadata.version

    def other_version(*args: Any) -> str:
        return "0.0.0"

    monkeypatch.setattr(plans.importlib.metadata, "version", other_version)
    with pytest.raises(ValueError, match="SDK"):
        plans.load_proposal(directory, digest)
    spec = spec_for(proposal)
    with pytest.raises(ValueError, match="SDK"):
        plans.prepare(spec, SOURCE, tmp_path / "wrong-sdk", "test")
    monkeypatch.setattr(plans.importlib.metadata, "version", original_version)
    with pytest.raises(ValueError, match="outside"):
        plans.build_proposal(PACKAGE, PACKAGE / "forbidden", "native_pilot")
    with plans.prepare(
        spec, SOURCE, pilot[0] / "preparation" / case.case_id, case.run_id
    ) as prepared:
        wire = next(plans.request_for(prepared, case.selection))
        for field in ("account_scope", "schema_sha256", "prompt_sha256"):
            with pytest.raises(ValueError, match="scope|commitment|sealed"):
                plans.verify_wire(case.model_copy(update={field: "b" * 64}), wire)

    def no_requests(*args: Any) -> Any:
        return iter(())

    monkeypatch.setattr(plans, "request_for", no_requests)
    with pytest.raises(ValueError, match="selection"):
        plans.build_proposal(PACKAGE, tmp_path / "missing-selection", "native_pilot")
    with pytest.raises(ValueError, match="selection"):
        plans.load_proposal(directory, digest)


def test_private_guard_rejects_proposal_and_selection_substitution(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    directory = tmp_path / "proposal"
    _write_proposal(directory, proposal)
    spec = spec_for(proposal)
    with BudgetLedger(tmp_path / "campaign").locked() as ledger:
        ledger.authorize(proposal, authorization(proposal))
        guard = execution._StudyGuard(
            spec,
            SOURCE,
            token=execution._STUDY_TOKEN,
            proposal=proposal,
            case=proposal.calls[0],
            directory=directory,
            ledger=ledger,
            proofs=tmp_path / "proofs",
        )
        (directory / "proposal.json").write_text("changed")
        with pytest.raises(ProviderIntegrityError, match="proposal bytes"):
            guard.verify_execution(guard.context)
    with pytest.raises(ProviderIntegrityError, match="selection"):
        execution.selected_attempts(
            SimpleNamespace(_submission_guard=object()), iter(())
        )


def test_one_unaffordable_call_prevents_safe_sealing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pilot: tuple[Path, Proposal]
) -> None:
    proofs = tmp_path / "proofs"
    case = seal(pilot[1], proofs).calls[0]
    assert case.price is not None and case.eligibility is not None
    price = case.price.model_copy(
        update={
            "outcomes": tuple(
                p.model_copy(update={"input_per_million": Decimal(1000)})
                for p in case.price.outcomes
            )
        }
    )

    def one(*args: Any) -> Any:
        return (case.selection,)

    monkeypatch.setattr(plans, "suite_selections", one)
    directory = tmp_path / "unaffordable"
    with pytest.raises(ValueError, match="per-call reservation"):
        plans.build_proposal(
            PACKAGE,
            directory,
            "native_pilot",
            eligibilities=(case.eligibility,),
            prices=(price,),
            proofs=proofs,
        )
    assert not (directory / "proposal.json").exists()


def test_budget_inventory_evidence_and_symlinks_fail_closed(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    case = proposal.calls[0]
    root = tmp_path / "campaign"
    with pytest.raises(ValueError, match="outside"):
        BudgetLedger(PACKAGE / "forbidden")
    with BudgetLedger(root).locked() as ledger:
        ledger.authorize(proposal, authorization(proposal), now=NOW)
        path = root / "events/000000.json"
        original = path.read_bytes()
        event = Event.model_validate_json(original)
        wrong_name = path.with_name("000001.json")
        path.rename(wrong_name)
        with pytest.raises(ValueError, match="inventory"):
            ledger.events()
        wrong_name.rename(path)
        for change, reason in (
            ({"suite": "other"}, "Unknown"),
            ({"kind": "hold"}, "start/deadline"),
            ({"case_id": "unknown"}, "immutable proposal"),
            ({"reserved_usd": Decimal(0)}, "immutable proposal"),
        ):
            path.write_bytes(canonical_bytes(event.model_copy(update=change)))
            with pytest.raises(ValueError, match=reason):
                ledger.events()
        path.write_bytes(original)
        with pytest.raises(BudgetStop, match="outside"):
            ledger.reserve(
                proposal, case.model_copy(update={"case_id": "other"}), now=NOW
            )
        with pytest.raises(BudgetStop, match="twice"):
            ledger.finish(
                proposal,
                case,
                terminal=True,
                reason="no-reservation",
                evidence="a" * 64,
                now=NOW,
            )
        ledger.reserve(proposal, case, now=NOW)
        path = root / "events/000001.json"
        original = path.read_bytes()
        event = Event.model_validate_json(original)
        path.write_bytes(
            canonical_bytes(
                event.model_copy(update={"case_id": proposal.calls[1].case_id})
            )
        )
        with pytest.raises(ValueError, match="reservation"):
            ledger.events()
        path.write_bytes(original)
        ledger.finish(
            proposal,
            case,
            terminal=False,
            reason="uncertain",
            evidence="b" * 64,
            now=NOW,
        )
        ledger.finish(
            proposal,
            case,
            terminal=False,
            reason="still-uncertain",
            evidence="b" * 64,
            now=NOW,
        )
        assert len(ledger.events()) == 3
        ledger.finish(
            proposal,
            case,
            terminal=True,
            reason="reviewed-original",
            evidence="c" * 64,
            now=NOW,
        )
        assert ledger.events()[-1].kind == "terminal"
        assert any(e.kind == "hold" for e in ledger.events())
    link = tmp_path / "symlink.json"
    link.symlink_to(root / "campaign.json")
    with pytest.raises(ValueError, match="symlinks"):
        immutable(link, b"replacement")


def test_journal_cannot_claim_more_calls_than_the_sealed_suite(
    tmp_path: Path, pilot: tuple[Path, Proposal]
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")
    with BudgetLedger(tmp_path / "campaign").locked() as ledger:
        ledger.authorize(proposal, authorization(proposal), now=NOW)
        first = ledger.events()[0]
        previous = bytes_digest(canonical_bytes(first))
        for index in range(43):
            case = proposal.calls[min(index, 41)]
            event = Event(
                sequence=index + 1,
                previous_sha256=previous,
                at=NOW,
                kind="reserve",
                suite=proposal.suite,
                proposal_sha256=proposal.digest,
                case_id=case.case_id,
                reserved_usd=case.reserved_usd or Decimal(0),
                input_tokens=case.reserved_input_tokens,
                output_tokens=case.reserved_output_tokens,
                seconds=case.operation_deadline_seconds,
                reason="synthetic tampered inventory",
            )
            raw = canonical_bytes(event)
            (ledger.root / "events" / f"{index + 1:06d}.json").write_bytes(raw)
            previous = bytes_digest(raw)
        with pytest.raises(ValueError, match="call count"):
            ledger.events()


def test_reports_retain_unknown_intents_and_unobserved_reservations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pilot: tuple[Path, Proposal]
) -> None:
    proposal = seal(pilot[1], tmp_path / "proofs")

    def loaded(*args: Any) -> Proposal:
        return proposal

    monkeypatch.setattr(execution, "load_proposal", loaded)
    campaign = tmp_path / "campaign"
    with BudgetLedger(campaign).locked() as ledger:
        ledger.authorize(proposal, authorization(proposal))
        ledger.reserve(proposal, proposal.calls[0])
    report = execution.report(pilot[0], proposal.digest, campaign)
    assert report["calls"][0]["reason"] == "uncertain_handoff_reservation_retained"
    assert report["calls"][1]["reason"] == "not_yet_authorized_or_executed"
    intent = next((campaign / "forward-intents").glob("*.json"))
    intent.write_text("incomplete")
    report = execution.report(pilot[0], proposal.digest, campaign)
    assert report["campaign_journal_uncertain"]
    assert report["campaign_reserved_usd"] is None
    assert all(r["allocation_consumed"] is None for r in report["calls"])
    assert all("intent" in r["reason"] for r in report["calls"])


def test_report_rejects_extra_or_orphaned_physical_evidence(
    monkeypatch: pytest.MonkeyPatch, pilot: tuple[Path, Proposal]
) -> None:
    repo = execution.ReadOnlyRepository
    case = pilot[1].calls[0]

    def extra(*args: Any) -> Any:
        return iter((object(), object()))

    with monkeypatch.context() as corrupted:
        corrupted.setattr(repo, "iter_evidence", extra)
        with pytest.raises(ValueError, match="extra physical"):
            execution.case_report(pilot[0], case)

    def orphan(*args: Any) -> Any:
        return iter((SimpleNamespace(attempt_id=case.attempt_id),))

    with monkeypatch.context() as corrupted:
        corrupted.setattr(repo, "iter_results", orphan)
        with pytest.raises(ValueError, match="no physical"):
            execution.case_report(pilot[0], case)

    def substituted(*args: Any) -> Any:
        return iter(
            (
                SimpleNamespace(
                    identity=SimpleNamespace(
                        run_id="other",
                        request_uid=case.request_uid,
                        attempt_id=case.attempt_id,
                        submission_id=case.submission_id,
                    ),
                    payload_sha256=case.payload_sha256,
                ),
            )
        )

    with monkeypatch.context() as corrupted:
        corrupted.setattr(repo, "iter_evidence", substituted)
        with pytest.raises(ValueError, match="identity"):
            execution.case_report(pilot[0], case)
    for state, reason in (
        ("not_submitted", "proven_not_submitted"),
        ("cancelled", "cancelled_before_handoff"),
    ):

        def state_of(*args: Any, state: str = state) -> Any:
            return SimpleNamespace(state=state)

        with monkeypatch.context() as interrupted:
            interrupted.setattr(repo, "get_attempt", state_of)
            assert execution.case_report(pilot[0], case)["reason"] == reason
