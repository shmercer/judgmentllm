"""Forwarding guard, conservative uncertainty, retries and immutable companions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_decisions_contracts import pair_spec
from test_decisions_lifecycle import native_spec

from judgmentllm.decisions.provenance import export_decisions_provenance
from judgmentllm.decisions.transport import DecisionsCommitmentV1
from judgmentllm.evidence import ReportedUsageInput
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.exports import export_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.lifecycle import estimate_run, get_run, run_live, validate_inputs
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.evidence import (
    UsageRevision,
    append_usage_revision,
    authoritative_usage,
    publish_usage_revision,
)
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.models import RetryAuthorizationError
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.providers.decisions_mock import (
    DecisionsScenarioV1,
    MockDecisionsAdapter,
)
from judgmentllm.results import PairwiseResult


@pytest.mark.parametrize("extra", ["retry", "redirect", "continuation"])
@pytest.mark.asyncio
async def test_extra_forward_is_blocked_and_first_evidence_survives(
    tmp_path: Path, extra: str
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    observed: list[str] = []
    adapter = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=DecisionsScenarioV1.model_validate(dict(extra_forward=extra)),
        receiver_observer=observed.append,
    )
    with pytest.raises(ExceptionGroup):
        await execute_run("001", store=store, adapter=adapter)
    assert (
        adapter.forward_entries
        == len(observed)
        == adapter.ledger.acceptance_count()
        == 1
    )
    with Repository(store) as repository:
        assert repository.summary("001").successful_results == 3
        assert next(repository.iter_submissions("001")).certainty == "accepted"
    await recover_run("001", store=store, adapter=adapter)
    assert len(observed) == 1


@pytest.mark.asyncio
async def test_receiver_negative_control_counts_before_acceptance_deduplication(
    tmp_path: Path,
) -> None:
    from judgmentllm.preparation.decisions import DecisionsPreparedRun
    from judgmentllm.providers.decisions_mock import InvocationGuard
    from judgmentllm.transport import physical_identity

    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    observed: list[str] = []
    adapter = MockDecisionsAdapter(
        store / "mock-service.sqlite3", receiver_observer=observed.append
    )
    with prepare_run(spec, store=store, run_id="001") as prepared:
        assert isinstance(prepared, DecisionsPreparedRun)
        dependency = next(prepared.iter_request_dependencies())
        attempt = next(prepared.repository.iter_attempts("001"))
        wire = prepared.prepare_transport_attempt(
            dependency,
            physical_identity("001", attempt.request_uid, attempt.attempt_id),
        )
        # Deliberately bypass the shared guard to establish the observer's sensitivity.
        await adapter._receive(wire, InvocationGuard(wire))  # pyright: ignore[reportPrivateUsage]
        await adapter._receive(wire, InvocationGuard(wire))  # pyright: ignore[reportPrivateUsage]
    assert observed[0] == observed[1] and len(observed) == adapter.forward_entries == 2
    assert adapter.ledger.acceptance_count() == 1


@pytest.mark.parametrize(
    "transport", ["timeout_before", "timeout_after", "partial_reception"]
)
@pytest.mark.asyncio
async def test_uncertainty_is_held_and_authoritative_reconciliation_never_forwards(
    tmp_path: Path, transport: str
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    scenario = DecisionsScenarioV1.model_validate(dict(transport=transport))
    adapter = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=scenario,
        reconciliation_available=False,
    )
    await execute_run("001", store=store, adapter=adapter)
    with Repository(store) as repository:
        assert repository.summary("001").results == 0
        assert next(repository.iter_submissions("001")).certainty == "unknown"
        selected = repository.request_task_ids(
            "001", next(repository.iter_requests("001")).request_uid
        )
    with pytest.raises(RetryAuthorizationError):
        authorize_subset_retry(
            "001",
            store=store,
            adapter=adapter,
            task_ids=selected,
            operation_id="unsafe",
        )
    held = await recover_run("001", store=store, adapter=adapter)
    assert (
        held.recovery_required
        and held.summary.results == 0
        and adapter.submit_calls == 1
    )
    recovered = MockDecisionsAdapter(store / "mock-service.sqlite3", scenario=scenario)
    report = await recover_run("001", store=store, adapter=recovered)
    assert report.summary.successful_results == (
        0 if transport == "timeout_before" else 3
    )
    assert recovered.forward_entries == recovered.submit_calls == 0


@pytest.mark.asyncio
async def test_failed_subset_retry_is_ancestral_and_operation_idempotent(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    first = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=DecisionsScenarioV1.model_validate(
            dict(members={"01": dict(outcome="missing")})
        ),
    )
    await execute_run("001", store=store, adapter=first)
    with Repository(store) as repository:
        failed = next(
            r for r in repository.iter_results("001") if r.status == "invalid"
        )
    second = MockDecisionsAdapter(store / "mock-service.sqlite3")
    attempt = authorize_subset_retry(
        "001",
        store=store,
        adapter=second,
        task_ids=(failed.task_id,),
        operation_id="retry-failed",
    )
    same = authorize_subset_retry(
        "001",
        store=store,
        adapter=second,
        task_ids=(failed.task_id,),
        operation_id="retry-failed",
    )
    assert attempt == same
    with open_prepared_run("001", store=store) as prepared:
        receipt = prepared.transport_for_attempt(attempt.attempt_id)
        assert isinstance(receipt, DecisionsCommitmentV1)
        assert receipt.parent_commitment_sha256 is not None
        assert [q.task_id for q in receipt.questions] == [failed.task_id]
    await recover_run("001", store=store, adapter=second)
    assert first.forward_entries == second.forward_entries == 1
    with Repository(store) as repository:
        assert repository.summary("001").results == 4
    export = export_run("001", store=store)
    assert export.row_count == 4
    bundle = export_decisions_provenance("001", store=store)
    assert bundle.manifest.totals.known_invocations == 2
    assert [f.rows for f in bundle.manifest.files] == [4, 4, 2, 2]


@pytest.mark.parametrize(
    "stage",
    [
        "after_provenance_serialization",
        "after_provenance_artifact",
        "during_provenance_registration",
        "before_provenance_reference",
        "after_provenance_reference",
    ],
)
@pytest.mark.asyncio
async def test_provenance_reference_published_last_and_repeated_export_converges(
    tmp_path: Path, stage: str
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    await execute_run(
        "001", store=store, adapter=MockDecisionsAdapter(store / "mock-service.sqlite3")
    )

    def fault(current: str) -> None:
        if current == stage:
            raise RuntimeError("synthetic export interruption")

    with pytest.raises(RuntimeError):
        export_decisions_provenance("001", store=store, fault=fault)
    references = list((store / "runs").glob("*/decisions-provenance/*.json"))
    assert len(references) == int(stage == "after_provenance_reference")
    bundle = export_decisions_provenance("001", store=store)
    again = export_decisions_provenance("001", store=store)
    assert bundle == again and Path(
        bundle.manifest_path
    ).read_bytes() == canonical_bytes(bundle.manifest)
    with Repository(store) as repository:
        assert (
            repository.connection.execute("SELECT count(*) FROM exports").fetchone()[0]
            == 0
        )
    assert [f.rows for f in bundle.manifest.files] == [3, 3, 1, 1]
    physical_file = next(f for f in bundle.manifest.files if f.name == "physical.jsonl")
    assert physical_file.artifact.relative_path is not None
    physical = json.loads(
        (
            Path(bundle.manifest_path).parents[1] / physical_file.artifact.relative_path
        ).read_text()
    )
    assert physical["provider_remote_id"] is None and physical[
        "local_observation_key"
    ].startswith("local-decisions-")
    assert physical["selected_usage"]["input_tokens"] is None


@pytest.mark.asyncio
async def test_cj_reversed_presentation_and_explicit_winner(tmp_path: Path) -> None:
    spec = pair_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=DecisionsScenarioV1.model_validate(
            dict(members={"02": dict(outcome="no_selection")})
        ),
    )
    await execute_run("001", store=store, adapter=adapter)
    with Repository(store) as repository:
        results = list(repository.iter_results("001"))
        assert all(isinstance(r, PairwiseResult) for r in results)
        results = [r for r in results if isinstance(r, PairwiseResult)]
        assert len(results) == 12
        first = next(
            r
            for r in results
            if r.trait_id == "01" and not r.is_reversal and r.repetition_index == 0
        )
        assert first.original_order == ["001", "002"] and first.presented_order == [
            "002",
            "001",
        ]
        assert first.winner_id == "001" and [first.p_left, first.p_right] == [0.8, 0.2]
        assert {r.status for r in results if r.trait_id == "02"} == {"invalid"}
        assert all(r.winner_id is None for r in results if r.trait_id == "02")


def test_ordinary_python_validation_plan_execution_status_and_companion(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path, capture="minimal")
    assert validate_inputs(spec).valid
    plan = estimate_run(spec)
    assert (
        plan.requests == 1
        and plan.logical_tasks == 3
        and "trait_groups" not in plan.model_dump()
    )
    summary = run_live(spec, store=tmp_path / "store")
    assert summary.outcome == "ok" and summary.decisions is not None
    assert summary.decisions.known_invocations == 1
    assert (
        summary.decisions.input_tokens
        is summary.decisions.output_tokens
        is summary.decisions.cost
        is None
    )
    assert (
        get_run(summary.run_id, store=tmp_path / "store").decisions == summary.decisions
    )


@pytest.mark.asyncio
async def test_reported_zero_and_selected_usage_revision_count_once(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=DecisionsScenarioV1(
            usage=ReportedUsageInput(
                usage_source="synthetic.reported_usage", input_tokens=0, output_tokens=0
            )
        ),
    )
    await execute_run("001", store=store, adapter=adapter)
    status = get_run("001", store=store)
    assert status.decisions is not None
    assert (
        status.decisions.input_tokens == status.decisions.output_tokens == 0
        and status.decisions.cost is None
    )
    with Repository(store) as repository:
        submission = next(repository.iter_submissions("001"))
        usage = authoritative_usage(repository, "001", submission.submission_id)
        assert usage is not None
        revised = type(usage).model_validate(
            {**usage.model_dump(), "input_tokens": 7, "output_tokens": 11}
        )
        revision = UsageRevision(
            usage=revised,
            revision_id="correction",
            revision_number=1,
            source="synthetic.authoritative",
        )
        publish_usage_revision(ArtifactStore(repository.run_directory("001")), revision)
        with ExecutorOwnership(repository, "001") as owner:
            assert owner.owner is not None
            append_usage_revision(repository, revision, owner_token=owner.owner.token)
    changed = export_decisions_provenance("001", store=store).manifest.totals
    assert (
        changed.known_invocations == 1
        and changed.input_tokens == 7
        and changed.output_tokens == 11
    )
