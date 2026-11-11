"""Actual sealed requests, occurrence aliases, retries and historical recovery."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from judgmentllm.config import RunSpecV2
from judgmentllm.execution import execute_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.io import write_jsonl
from judgmentllm.models import PairTask, PairwiseCriterion, ResponseItem
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.providers import MockAdapter, MockScenario
from judgmentllm.results import PairwiseResult


@pytest.mark.asyncio
@pytest.mark.parametrize("run_id", ["v1-pending", "v2-pending", "v2-recovery"])
async def test_genuine_pre_change_prepared_and_received_requests(
    tmp_path: Path, run_id: str
) -> None:
    fixture = Path(__file__).parent / "fixtures/presentation-history"
    provenance = json.loads((fixture / "provenance.json").read_text())
    assert provenance["source_commit"] == "38bda981a46330b73fec6e80a9b1c0c7a3c1c61a"
    raw_archive = base64.b64decode(
        (fixture / "store.tar.gz.base64").read_bytes(), validate=True
    )
    assert hashlib.sha256(raw_archive).hexdigest() == provenance["archive_sha256"]
    assert (
        hashlib.sha256((fixture / "producer.py.txt").read_bytes()).hexdigest()
        == provenance["producer_script_sha256"]
    )
    with tarfile.open(fileobj=io.BytesIO(raw_archive), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    store = tmp_path / "store"
    adapter = MockAdapter(
        tmp_path / "ledger.sqlite3", scenario=MockScenario(winner="second")
    )
    with open_prepared_run(run_id, store=store, use_frozen_snapshot=True) as prepared:
        assert prepared.manifest.template.presentation_version == 1
        dependency = next(prepared.iter_request_dependencies())
        rendered = prepared.render(dependency)
        assert "First response essay_02" in rendered.text
        assert "Second response essay_01" in rendered.text
        manifest_path = prepared.store.root / "manifest.json"
        before = manifest_path.read_bytes()
        spec = prepared.spec
    before_calls = adapter.ledger.acceptance_count()
    report = await execute_run(
        run_id, store=store, adapter=adapter, use_frozen_snapshot=True, spec=spec
    )
    assert report.summary.successful_results == 1
    assert adapter.submit_calls == (0 if run_id == "v2-recovery" else 1)
    assert adapter.ledger.acceptance_count() == before_calls + adapter.submit_calls
    await execute_run(
        run_id, store=store, adapter=adapter, use_frozen_snapshot=True, spec=spec
    )
    with Repository(store) as repository:
        row = next(repository.iter_results(run_id))
        assert isinstance(row, PairwiseResult)
        # The old wire essay_01 meant the original right item. Revision-2 alias
        # interpretation would instead select the left item essay_02 here.
        assert row.winner_id == row.right_item_id == "essay_01"
        assert row.presented_order == ["essay_02", "essay_01"]
        assert len(tuple(repository.iter_attempts(run_id))) == 1
    assert manifest_path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", ["full", "minimal"])
async def test_repeated_reversed_resumed_and_explicit_retry_aliases(
    tmp_path: Path, capture: str
) -> None:
    responses = tmp_path / "responses.jsonl"
    write_jsonl(
        responses,
        [
            ResponseItem(item_id="001", text="Essay A.\n\nComplete second paragraph."),
            ResponseItem(item_id="002", text="Essay B."),
        ],
    )
    pairs = tmp_path / "pairs.jsonl"
    write_jsonl(
        pairs,
        [
            PairTask(
                pair_id="0001",
                pair_uid=f"0001-{index}",
                left_item_id="001",
                right_item_id="002",
                trait_id="overall",
                presented_order=["002", "001"] if index == 1 else ["001", "002"],
                repetition_index=index,
                is_reversal=index == 1,
                reversal_group="0001",
            )
            for index in range(3)
        ],
    )
    spec = RunSpecV2.model_validate(
        dict(
            task_kind="pairwise",
            input=responses,
            pairs=pairs,
            criteria=[
                PairwiseCriterion(
                    trait_id="overall",
                    name="Overall",
                    description="Compare only writing quality.",
                )
            ],
            provider="mock",
            endpoint="synthetic",
            model="offline",
            capture=capture,
            execution={"concurrency": 1},
        )
    )
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        assert prepared.manifest.template.presentation_version == 2
        dependencies = tuple(prepared.iter_request_dependencies())
        scenarios = {
            dependency.request.request_uid: MockScenario(
                winner="second"
                if dependency.request.pair and dependency.request.pair.is_reversal
                else "first"
            )
            for dependency in dependencies
        }
        for dependency in dependencies:
            rendered = prepared.render(dependency)
            assert "001" not in rendered.text and "002" not in rendered.text
    adapter = MockAdapter(tmp_path / "ledger.sqlite3", scenarios=scenarios)

    def interrupt_after_receipt(stage: str, _request_uid: str) -> None:
        if stage == "after_evidence_commit":
            raise RuntimeError("synthetic interruption after alias response receipt")

    with pytest.raises(ExceptionGroup):
        await execute_run(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            fault=interrupt_after_receipt,
        )
    await execute_run("001", store=store, adapter=adapter, spec=spec)
    await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert adapter.submit_calls == 3
    with Repository(store) as repository:
        rows = tuple(repository.iter_results("001"))
        assert all(
            isinstance(row, PairwiseResult) and row.winner_id == "001" for row in rows
        )
        selected = rows[1]
    retry = authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        spec=spec,
        task_ids=[selected.task_id],
        operation_id="explicit-position-retry",
        allow_completed=True,
    )
    await execute_run("001", store=store, adapter=adapter, spec=spec)
    again = authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        spec=spec,
        task_ids=[selected.task_id],
        operation_id="explicit-position-retry",
        allow_completed=True,
    )
    assert retry.attempt_id == again.attempt_id
    assert retry.payload_sha256 == again.payload_sha256
    await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 4
    with Repository(store) as repository:
        results = tuple(repository.iter_results("001"))
        assert len(results) == 4
        assert all(
            isinstance(row, PairwiseResult) and row.winner_id == "001"
            for row in results
        )


@pytest.mark.asyncio
async def test_new_mock_envelope_keeps_complete_probability_types(
    tmp_path: Path,
) -> None:
    from test_mock_provider import request

    from judgmentllm.execution.processing import normalize
    from judgmentllm.providers import PreparedRequestV2
    from judgmentllm.results import RubricResult

    adapter = MockAdapter(
        tmp_path / "ledger.sqlite3",
        scenario=MockScenario(category_index=0, probabilities=True),
    )
    spec, dependency, wire = request(adapter, explanations=True, presentation_version=2)
    assert isinstance(wire, PreparedRequestV2)
    await adapter.submit(wire, submission_id="submission", correlation_id="correlation")
    response = await adapter.retrieve(
        wire, submission_id="submission", correlation_id="correlation"
    )
    parsed = adapter.parse(response)
    assert parsed == adapter.parse(response, strict_output=True)
    assert (
        isinstance(parsed.rubric_probabilities, tuple)
        and len(parsed.rubric_probabilities) == 3
    )
    result = normalize(
        parsed,
        spec,
        dependency,
        "run",
        "attempt",
        contract=wire.output_commitment.contract,
    )
    assert isinstance(result, RubricResult)
    assert result.category_label == "low" and result.numeric_category_value == 1
    assert result.primary_score_kind == "expected"
    assert result.expected_value == pytest.approx(3.6)
