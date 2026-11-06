"""Execution must reject changed commitments and local validation interruptions."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from test_execution import setup

from judgmentllm.config import GenerationSpec, RunSpecification
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.receipts import read_acknowledgment
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import CapabilityProfile
from judgmentllm.preparation import RequestDependency, prepare_run
from judgmentllm.prompts import RenderedAssessmentPrompt
from judgmentllm.providers import (
    MockAdapter,
    PreparedRequest,
    ProviderAcknowledgment,
    ProviderResponse,
    ReconciliationEvidence,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("request_uid", "other-request"),
        ("dependency_sha256", "0" * 64),
        ("provider", "other-provider"),
        ("endpoint", "other-endpoint"),
        ("model", "other-model"),
        ("generation", GenerationSpec(temperature=0)),
    ],
)
async def test_adapter_envelope_must_match_frozen_selected_work(
    tmp_path: Path, field: str, value: object
) -> None:
    spec, store, ledger = setup(tmp_path)

    class ChangedEnvelope(MockAdapter):
        def prepare(
            self,
            spec: RunSpecification,
            dependency: RequestDependency,
            rendered: RenderedAssessmentPrompt,
        ) -> PreparedRequest:
            prepared = super().prepare(spec, dependency, rendered)
            return prepared.model_copy(update={field: value})

    adapter = ChangedEnvelope(ledger)
    with pytest.raises(ExceptionGroup):
        await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 0
    with Repository(store) as repository:
        assert list(repository.iter_attempts("001")) == []
        assert list(repository.iter_submissions("001")) == []
        assert list(repository.iter_results("001")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before_parsing", "before_validation"])
async def test_local_value_error_does_not_fabricate_invalid_output(
    tmp_path: Path, stage: str
) -> None:
    spec, store, ledger = setup(tmp_path)
    adapter = MockAdapter(ledger)

    def fault(current: str, _: str) -> None:
        if current == stage:
            raise ValueError("synthetic local interruption")

    with pytest.raises(ExceptionGroup):
        await execute_run("001", store=store, adapter=adapter, spec=spec, fault=fault)
    assert adapter.ledger.acceptance_count() == 1
    with Repository(store) as repository:
        assert list(repository.iter_results("001")) == []
        submission = next(repository.iter_submissions("001"))
        observation = repository.get_observation_for_submission(
            "001", submission.submission_id
        )
        assert observation is not None and observation.kind == "response"
    report = await recover_run(
        "001", store=store, adapter=MockAdapter(ledger), spec=spec
    )
    assert report.summary.successful_results == 1
    assert adapter.ledger.acceptance_count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before_request_creation", "after_claim"])
@pytest.mark.parametrize("replace", [False, True])
async def test_disk_manifest_change_blocks_submission(
    tmp_path: Path, stage: str, replace: bool
) -> None:
    spec, store, ledger = setup(tmp_path)
    adapter = MockAdapter(ledger)
    with Repository(store) as repository:
        manifest = repository.run_directory("001") / "manifest.json"
    changed = False

    def fault(current: str, _: str) -> None:
        nonlocal changed
        if current == stage and not changed:
            if replace:
                manifest.unlink()
            manifest.write_bytes(b"{}")
            changed = True

    with pytest.raises(ExceptionGroup):
        await execute_run("001", store=store, adapter=adapter, spec=spec, fault=fault)
    assert changed
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 0
    with Repository(store) as repository:
        assert list(repository.iter_results("001")) == []
        assert all(
            submission.state == "prepared"
            for submission in repository.iter_submissions("001")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", ["full", "minimal"])
async def test_original_source_change_after_claim_blocks_submission(
    tmp_path: Path, capture: Literal["full", "minimal"]
) -> None:
    spec, store, ledger = setup(tmp_path, capture=capture)
    adapter = MockAdapter(ledger)

    def fault(current: str, _: str) -> None:
        if current == "after_claim":
            original = spec.input.path.read_text(encoding="utf-8")
            spec.input.path.write_text(
                original.replace("Synthetic", "Different"), encoding="utf-8"
            )

    with pytest.raises(ExceptionGroup):
        await execute_run("001", store=store, adapter=adapter, spec=spec, fault=fault)
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 0
    with Repository(store) as repository:
        assert list(repository.iter_results("001")) == []
        assert next(repository.iter_submissions("001")).state == "prepared"


@pytest.mark.asyncio
async def test_new_generation_restriction_blocks_before_submission_without_resealing(
    tmp_path: Path,
) -> None:
    spec, _, ledger = setup(tmp_path)
    spec.generation = GenerationSpec(temperature=0)

    class RestrictedLater(MockAdapter):
        restricted = False

        def capabilities(self, spec: RunSpecification) -> CapabilityProfile:
            profile = super().capabilities(spec)
            if self.restricted:
                assert profile.generation is not None
                generation = profile.generation.model_copy(
                    update={
                        "temperature": profile.generation.temperature.model_copy(
                            update={"supported": False}
                        )
                    }
                )
                profile = profile.model_copy(update={"generation": generation})
            return profile

    adapter = RestrictedLater(ledger)
    store = tmp_path / "generation-store"
    with prepare_run(
        spec, store=store, run_id="001", capabilities=adapter.capabilities(spec)
    ) as prepared:
        frozen = prepared.manifest_sha256
        manifest_bytes = (prepared.store.root / "manifest.json").read_bytes()

    def fault(current: str, _: str) -> None:
        if current == "after_claim":
            adapter.restricted = True

    with pytest.raises(ExceptionGroup):
        await execute_run("001", store=store, adapter=adapter, spec=spec, fault=fault)
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 0
    with Repository(store) as repository:
        assert repository.get_run("001").manifest_sha256 == frozen
        assert (
            repository.run_directory("001") / "manifest.json"
        ).read_bytes() == manifest_bytes
        assert list(repository.iter_results("001")) == []
        assert next(repository.iter_submissions("001")).state == "prepared"
    adapter.restricted = False
    report = await recover_run("001", store=store, adapter=adapter, spec=spec)
    assert report.summary.successful_results == adapter.ledger.acceptance_count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", ["full", "minimal"])
async def test_acknowledgment_key_must_match_first_retrieved_observation(
    tmp_path: Path, capture: Literal["full", "minimal"]
) -> None:
    spec, store, ledger = setup(tmp_path, capture=capture)

    class WrongAcknowledgment(MockAdapter):
        async def submit(
            self,
            prepared: PreparedRequest,
            *,
            submission_id: str,
            correlation_id: str,
        ) -> ProviderAcknowledgment:
            acknowledgment = await super().submit(
                prepared, submission_id=submission_id, correlation_id=correlation_id
            )
            return acknowledgment.model_copy(
                update={"observation_key": "conflicting-acknowledgment-key"}
            )

    adapter = WrongAcknowledgment(ledger)
    with pytest.raises(ExceptionGroup):
        await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1
    with Repository(store) as repository:
        submission = next(repository.iter_submissions("001"))
        receipt = read_acknowledgment(
            ArtifactStore(repository.run_directory("001")), submission.submission_id
        )
        assert receipt is not None
        assert receipt.observation_key == "conflicting-acknowledgment-key"
        accepted = adapter.ledger.get(submission.submission_id)
        assert accepted is not None
        assert accepted.observation_key != receipt.observation_key
        assert list(repository.iter_results("001")) == []
        assert (
            repository.get_observation_for_submission("001", submission.submission_id)
            is None
        )
    recovery = MockAdapter(ledger)
    with pytest.raises(ExceptionGroup):
        await recover_run("001", store=store, adapter=recovery, spec=spec)
    assert recovery.submit_calls == 0
    assert recovery.ledger.acceptance_count() == 1
    with Repository(store) as repository:
        assert list(repository.iter_results("001")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "stage", ["after_acknowledgment_receipt", "after_acknowledgment"]
)
async def test_recovery_preserves_durable_acknowledged_observation_identity(
    tmp_path: Path, capture: Literal["full", "minimal"], stage: str
) -> None:
    spec, store, ledger = setup(tmp_path, capture=capture)
    original = MockAdapter(ledger)

    def fault(current: str, _: str) -> None:
        if current == stage:
            raise RuntimeError("synthetic interruption after durable acknowledgment")

    with pytest.raises(ExceptionGroup):
        await execute_run("001", store=store, adapter=original, spec=spec, fault=fault)
    assert original.ledger.acceptance_count() == 1
    with Repository(store) as repository:
        submission = next(repository.iter_submissions("001"))
        receipt = read_acknowledgment(
            ArtifactStore(repository.run_directory("001")), submission.submission_id
        )
        assert receipt is not None
        original_key = receipt.observation_key

    class ConflictingRetrieval(MockAdapter):
        async def retrieve(
            self,
            prepared: PreparedRequest,
            *,
            submission_id: str,
            correlation_id: str,
        ) -> ProviderResponse:
            response = await super().retrieve(
                prepared, submission_id=submission_id, correlation_id=correlation_id
            )
            return response.model_copy(
                update={"observation_key": "conflicting-retrieved-key"}
            )

        async def reconcile(
            self,
            prepared: PreparedRequest,
            *,
            submission_id: str,
            correlation_id: str,
        ) -> ReconciliationEvidence:
            raise AssertionError("durable acknowledgment must be reused before lookup")

    conflict = ConflictingRetrieval(ledger)
    with pytest.raises(ExceptionGroup):
        await recover_run("001", store=store, adapter=conflict, spec=spec)
    assert conflict.submit_calls == 0 and conflict.ledger.acceptance_count() == 1
    with Repository(store) as repository:
        submission = next(repository.iter_submissions("001"))
        receipt = read_acknowledgment(
            ArtifactStore(repository.run_directory("001")), submission.submission_id
        )
        assert receipt is not None and receipt.observation_key == original_key
        assert list(repository.iter_results("001")) == []
        assert (
            repository.get_observation_for_submission("001", submission.submission_id)
            is None
        )
    report = await recover_run(
        "001", store=store, adapter=MockAdapter(ledger), spec=spec
    )
    assert report.summary.successful_results == 1
    assert conflict.ledger.acceptance_count() == 1
