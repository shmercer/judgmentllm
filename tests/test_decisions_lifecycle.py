"""Provider-neutral Decisions preparation, named outcomes and durable recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from judgmentllm.config import RunSpecV4, validate_run_spec
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.decisions_mock import (
    DecisionsScenarioV1,
    MockDecisionsAdapter,
)


def native_spec(
    tmp_path: Path,
    *,
    traits: int = 3,
    sources: int = 1,
    capture: str = "full",
    maximum: int | None = 32,
) -> RunSpecV4:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "responses.jsonl"
    path.write_text(
        "".join(
            json.dumps(dict(item_id=f"{i:03d}", text=f"Synthetic source {i}.")) + "\n"
            for i in range(sources)
        ),
        encoding="utf-8",
    )
    return RunSpecV4.model_validate(
        dict(
            schema_version=4,
            task_kind="rubric",
            input=path,
            provider="mock",
            endpoint="synthetic",
            model="synthetic-decisions",
            capture=capture,
            rubric=dict(
                rubric_id="r",
                name="Synthetic",
                traits=[
                    dict(
                        trait_id=f"{i:02d}",
                        name="Quality",
                        description=f"Assess criterion {i}.",
                        levels=[
                            dict(
                                label=f"{j:02d}",
                                description=f"Level {j}",
                                numeric_value=code,
                            )
                            for j, code in enumerate((1, 2, 7))
                        ],
                    )
                    for i in range(traits)
                ],
            ),
            execution=dict(
                concurrency=1,
                **(
                    {
                        "request_grouping": dict(
                            mode="auto", max_questions_per_request=maximum
                        )
                    }
                    if maximum is not None
                    else {}
                ),
            ),
        )
    )


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.asyncio
async def test_grouped_execution_and_local_receipt_replay(
    tmp_path: Path, capture: str
) -> None:
    spec = native_spec(tmp_path, capture=capture)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        dependencies = list(prepared.iter_request_dependencies())
        assert len(dependencies) == 1 and len(dependencies[0].request.tasks) == 3
    adapter = MockDecisionsAdapter(store / "mock-service.sqlite3")
    report = await execute_run("001", store=store, spec=spec, adapter=adapter)
    assert report.summary.results == 3
    with Repository(store) as repository:
        results = list(repository.iter_results("001"))
        assert {r.status for r in results} == {"complete"}
        for result in results:
            assert result.task_kind == "rubric"
            assert result.primary_score == pytest.approx(5.15)
            assert result.provider_expected_index == pytest.approx(1.55)
            assert result.category_label is None and result.modal_category_label == "02"
            assert result.confidence == 0.8 and result.input_tokens is None
        submission = next(repository.iter_submissions("001"))
        assert submission.remote_id is None and submission.certainty == "accepted"
    await recover_run("001", store=store, spec=spec, adapter=adapter)
    assert (
        adapter.submit_calls
        == adapter.forward_entries
        == adapter.ledger.acceptance_count()
        == 1
    )


@pytest.mark.parametrize(
    "outcome,status",
    [
        ("missing", "invalid"),
        ("duplicate", "invalid"),
        ("wrong_type", "invalid"),
        ("invalid_distribution", "invalid"),
        ("bad_legend", "invalid"),
        ("refusal", "refused"),
    ],
)
@pytest.mark.asyncio
async def test_named_member_salvage(tmp_path: Path, outcome: str, status: str) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    scenario = DecisionsScenarioV1.model_validate(
        dict(members={"01": dict(outcome=outcome)})
    )
    adapter = MockDecisionsAdapter(store / "mock-service.sqlite3", scenario=scenario)
    await execute_run("001", store=store, adapter=adapter)
    with Repository(store) as repository:
        results = {r.trait_id: r for r in repository.iter_results("001")}
        assert [results[k].status for k in ("00", "01", "02")] == [
            "complete",
            status,
            "complete",
        ]
        assert repository.summary("001").results == 3


@pytest.mark.parametrize(
    "envelope", ["malformed_json", "duplicate_json_key", "truncated_json"]
)
@pytest.mark.asyncio
async def test_malformed_received_response_never_salvages(
    tmp_path: Path, envelope: str
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=DecisionsScenarioV1.model_validate(
            dict(envelope=envelope, members={"01": {"outcome": "refusal"}})
        ),
    )
    await execute_run("001", store=store, adapter=adapter)
    with Repository(store) as repository:
        assert {r.status for r in repository.iter_results("001")} == {"invalid"}
        assert next(repository.iter_submissions("001")).certainty == "accepted"


def test_native_grouping_revalidation_and_stable_ids(tmp_path: Path) -> None:
    spec = native_spec(tmp_path)
    tasks: list[list[str]] = []
    requests: list[int] = []
    for maximum in (32, 2, 1, None):
        current = RunSpecV4.model_validate(
            {
                **spec.model_dump(),
                "execution": {
                    "request_grouping": {
                        "mode": "auto",
                        "max_questions_per_request": maximum,
                    }
                }
                if maximum
                else {},
            }
        )
        with prepare_run(
            current, store=tmp_path / str(maximum), run_id="001"
        ) as prepared:
            dependencies = list(prepared.iter_request_dependencies())
            tasks.append([t.task_id for d in dependencies for t in d.request.tasks])
            requests.append(len(dependencies))
    assert tasks.count(tasks[0]) == 4 and requests == [1, 2, 3, 3]
    assert spec.execution.request_grouping.mode == "auto"
    object.__setattr__(
        spec.execution.request_grouping, "max_questions_per_request", True
    )
    with pytest.raises(ValueError):
        validate_run_spec(spec)


@pytest.mark.parametrize(
    "envelope,order,complete,whole",
    [
        ("reverse", "map", 3, True),
        ("reverse", "ordered_array", 0, False),
        ("unexpected", "map", 3, False),
        ("refusal", "map", 0, False),
        ("error", "map", 0, False),
    ],
)
@pytest.mark.asyncio
async def test_whole_envelope_and_named_order_rules(
    tmp_path: Path, envelope: str, order: str, complete: int, whole: bool
) -> None:
    from judgmentllm.decisions.provenance import export_decisions_provenance

    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=DecisionsScenarioV1.model_validate(
            dict(envelope=envelope, answer_order=order)
        ),
    )
    await execute_run("001", store=store, adapter=adapter)
    with Repository(store) as repository:
        results = list(repository.iter_results("001"))
        assert sum(r.status == "complete" for r in results) == complete
        assert len(results) == 3 and adapter.forward_entries == 1
        if envelope == "refusal":
            assert {r.status for r in results} == {"refused"}
        elif envelope == "error":
            assert {r.status for r in results} == {"errored"}
    bundle = export_decisions_provenance("001", store=store)
    question_file = next(
        f for f in bundle.manifest.files if f.name == "questions.jsonl"
    )
    assert question_file.artifact.relative_path is not None
    rows = [
        json.loads(line)
        for line in (
            Path(bundle.manifest_path).parents[1] / question_file.artifact.relative_path
        )
        .read_text()
        .splitlines()
    ]
    assert all(row["whole_response_valid"] is whole for row in rows)


@pytest.mark.parametrize("envelope", ["wrong_model", "unbound"])
@pytest.mark.asyncio
async def test_conflicting_identity_stops_without_success_or_resubmission(
    tmp_path: Path, envelope: str
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=DecisionsScenarioV1.model_validate(dict(envelope=envelope)),
    )
    for operation in (execute_run, recover_run):
        with pytest.raises(ExceptionGroup):
            await operation("001", store=store, adapter=adapter)
    with Repository(store) as repository:
        assert repository.summary("001").results == 0
        assert next(repository.iter_submissions("001")).certainty == "accepted"
    assert adapter.forward_entries == adapter.submit_calls == 1
