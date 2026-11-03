"""Independent rubric member matching and atomic grouped evidence recovery."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest
from pydantic import JsonValue
from test_execution import read_live_runtime_file
from test_preparation_rendering import specification

from judgmentllm.config import RunSpec
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.processing import interpret
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers import (
    MockAdapter,
    MockMemberScenario,
    MockScenario,
    ParsedResponse,
)
from judgmentllm.results import RubricResult, validate_categorical_output


def fixture(
    directory: Path,
    capture: Literal["full", "minimal"] = "minimal",
    *,
    explanations: bool = True,
) -> tuple[RunSpec, Path, Path]:
    spec = specification(directory / "inputs", responses=1, traits=3)
    spec.provider, spec.endpoint, spec.capture = "mock", "synthetic", capture
    spec.execution.grouping = "grouped"
    spec.execution.explanations = explanations
    spec.execution.concurrency = 1
    spec.output_path = (directory / "output").resolve()
    spec.prompt.preset = None
    spec = RunSpec.model_validate(spec.model_dump())
    store, ledger = directory / "store", directory / "service" / "ledger.sqlite3"
    adapter = MockAdapter(ledger)
    with prepare_run(
        spec, store=store, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass
    return spec, store, ledger


def mixed() -> MockScenario:
    return MockScenario(
        members={
            "001": MockMemberScenario(outcome="invalid_category"),
            "002": MockMemberScenario(outcome="missing"),
        },
        probabilities=True,
        reverse_members=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", ["full", "minimal"])
async def test_three_members_preserve_valid_invalid_and_missing(
    tmp_path: Path, capture: Literal["full", "minimal"]
) -> None:
    spec, store, ledger = fixture(tmp_path, capture)
    adapter = MockAdapter(ledger, scenario=mixed())
    report = await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert report.summary.results == 3
    assert (
        report.summary.successful_results == report.summary.logical_completed_tasks == 1
    )
    assert report.summary.unsuccessful_results == 2
    assert report.summary.state == "partial"
    with Repository(store) as repository:
        attempt = next(repository.iter_attempts("001"))
        results = repository.get_results_for_attempt("001", attempt.attempt_id)
        assert [(r.trait_id, r.status, r.error_kind) for r in results] == [
            ("000", "complete", None),
            ("001", "invalid", "invalid_output"),
            ("002", "invalid", "missing_output"),
        ]
        assert (
            len({r.request_uid for r in results})
            == len({r.attempt_id for r in results})
            == 1
        )
        assert tuple(r.task_id for r in results) == repository.request_task_ids(
            "001", attempt.request_uid
        )
        assert repository.get_request("001", attempt.request_uid).state == "partial"
        valid = results[0]
        assert isinstance(valid, RubricResult) and valid.probabilities is not None
        assert (
            valid.primary_score_kind == "expected"
            and valid.explanation == "Synthetic mock assessment."
        )
        for result in results[1:]:
            assert isinstance(result, RubricResult)
            assert (
                result.category_label
                is result.primary_score
                is result.explanation
                is result.probabilities
                is None
            )
        with pytest.raises(IntegrityError, match="multiple results"):
            repository.get_result_for_attempt("001", attempt.attempt_id)
        assert (
            repository.get_result_for_attempt(
                "001", attempt.attempt_id, task_id=valid.task_id
            )
            == valid
        )
        committed = tuple(repository.iter_results("001"))
    again = await recover_run("001", store=store, adapter=adapter, spec=spec)
    assert (
        again.summary.results == 3
        and adapter.submit_calls == adapter.ledger.acceptance_count() == 1
    )
    with Repository(store) as repository:
        assert tuple(repository.iter_results("001")) == committed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,error",
    [
        ("missing", "missing_output"),
        ("duplicate", "duplicate_output"),
        ("invalid_category", "invalid_output"),
        ("malformed", "invalid_output"),
        ("missing_explanation", "invalid_output"),
        ("invalid_explanation", "invalid_output"),
    ],
)
async def test_independently_attributable_member_validation(
    tmp_path: Path, outcome: str, error: str
) -> None:
    spec, store, ledger = fixture(tmp_path)
    recipe = MockMemberScenario.model_validate({"outcome": outcome})
    scenario = MockScenario(
        members={"001": recipe},
        unexpected_members=2,
        unattributable_members=2,
        reverse_members=True,
    )
    adapter = MockAdapter(ledger, scenario=scenario)
    report = await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert report.summary.successful_results == 2 and report.summary.results == 3
    with Repository(store) as repository:
        results = tuple(repository.iter_results("001"))
        assert results[1].error_kind == error
        assert results[0].status == results[2].status == "complete"
        diagnostics = [
            e.new_state
            for e in repository.events("001")
            if e.entity_kind == "observation"
        ]
        assert "unexpected_members=2;unattributable_members=2" in diagnostics
    assert adapter.ledger.acceptance_count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,status,error",
    [
        ("valid", "complete", None),
        ("invalid", "invalid", "invalid_output"),
        ("refusal", "refused", "provider_refusal"),
        ("provider_error", "errored", "provider_error"),
    ],
)
async def test_outer_and_authoritative_group_outcomes(
    tmp_path: Path, outcome: str, status: str, error: str | None
) -> None:
    spec, store, ledger = fixture(tmp_path)
    adapter = MockAdapter(
        ledger,
        scenario=MockScenario.model_validate(
            {"outcome": outcome, "reverse_members": True}
        ),
    )
    await execute_run("001", store=store, adapter=adapter, spec=spec)
    with Repository(store) as repository:
        results = tuple(repository.iter_results("001"))
        assert len(results) == 3 and all(
            r.status == status and r.error_kind == error for r in results
        )


def test_unrecognizable_structure_and_strict_public_group_validator(
    tmp_path: Path,
) -> None:
    spec, store, _ = fixture(tmp_path)
    with prepare_run(spec, store=tmp_path / "second-store", run_id="002") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        structures: tuple[dict[str, JsonValue], ...] = (
            {"scores": None},
            {"other": []},
            {"scores": [], "unknown": True},
        )
        for output in structures:
            interpreted = interpret(
                ParsedResponse.model_validate({"kind": "response", "output": output}),
                spec,
                dependency,
                "002",
                "attempt",
            )
            assert not interpreted.recognizable
            assert all(
                r.status == "invalid" and r.error_kind == "invalid_output"
                for r in interpreted.results
            )
        assert spec.rubric is not None
        with pytest.raises(ValueError):
            validate_categorical_output(
                {"scores": [{"trait_id": "000", "category_label": "low"}]},
                spec.rubric.traits,
            )
    with Repository(store) as repository:
        assert not tuple(repository.iter_results("001"))


CHILD = """
import asyncio, os, sys
from judgmentllm.config import load_run_spec
from judgmentllm.execution import execute_run
from judgmentllm.providers import MockAdapter, MockScenario, MockMemberScenario
configuration, store, ledger, stage, available = sys.argv[1:]
scenario = MockScenario(members={'001': MockMemberScenario(outcome='invalid_category'), '002': MockMemberScenario(outcome='missing')}, probabilities=True, reverse_members=True)
adapter = MockAdapter(ledger, scenario=scenario, retrieval_available=available == 'yes')
def fault(current, uid):
    if current == stage:
        os._exit(73)
report = asyncio.run(execute_run('001', store=store, adapter=adapter, spec=load_run_spec(configuration), fault=fault))
print(report.model_dump_json())
"""


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "stage",
    [
        "after_response",
        "after_receipt",
        "during_member_validation",
        "during_result_transaction",
        "after_result",
    ],
)
def test_process_death_has_no_partial_group_and_recovers_same_observation(
    tmp_path: Path, capture: Literal["full", "minimal"], stage: str
) -> None:
    spec, store, ledger = fixture(tmp_path, capture)
    configuration = tmp_path / "configuration.json"
    configuration.write_text(spec.model_dump_json(), encoding="utf-8")

    def run(boundary: str, available: str = "yes") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                CHILD,
                str(configuration),
                str(store),
                str(ledger),
                boundary,
                available,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    crashed = run(stage)
    assert crashed.returncode == 73, crashed.stderr
    assert not crashed.stdout and not crashed.stderr
    with Repository(store) as repository:
        before = tuple(repository.iter_results("001"))
        assert len(before) == (3 if stage == "after_result" else 0)
    unavailable = run("", "no")
    assert unavailable.returncode == 0, unavailable.stderr
    assert json.loads(unavailable.stdout)["summary"]["results"] == (
        0
        if stage == "after_response" or capture == "minimal" and stage != "after_result"
        else 3
    )
    recovered = run("")
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["summary"]["results"] == 3
    assert MockAdapter(ledger).ledger.acceptance_count() == 1
    with Repository(store) as repository:
        results = tuple(repository.iter_results("001"))
        if before:
            assert results == before
        assert [r.error_kind for r in results] == [
            None,
            "invalid_output",
            "missing_output",
        ]


@pytest.mark.asyncio
async def test_group_redelivery_conflicts_fail_closed(tmp_path: Path) -> None:
    spec, store, ledger = fixture(tmp_path)
    await execute_run("001", store=store, adapter=MockAdapter(ledger), spec=spec)
    with Repository(store) as repository:
        attempt = next(repository.iter_attempts("001"))
        submission = next(repository.iter_submissions("001"))
        observation = repository.get_observation_for_submission(
            "001", submission.submission_id
        )
        assert observation is not None
        results = repository.get_results_for_attempt("001", attempt.attempt_id)
        assert not repository.commit_results(
            "001",
            attempt.attempt_id,
            results,
            observation_id=observation.observation_id,
        )
        for changed in (
            results[:2],
            tuple(reversed(results)),
            (results[0].model_copy(update={"explanation": "different"}), *results[1:]),
        ):
            with pytest.raises(IntegrityError):
                repository.commit_results(
                    "001",
                    attempt.attempt_id,
                    changed,
                    observation_id=observation.observation_id,
                )
        with pytest.raises(IntegrityError, match="evidence"):
            repository.commit_results(
                "001", attempt.attempt_id, results, observation_id="different"
            )
        assert repository.summary("001").results == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage", ["during_member_validation", "during_result_transaction", None]
)
async def test_grouped_minimal_privacy_live_and_recovery(
    tmp_path: Path, stage: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = specification(tmp_path / "inputs", responses=1, traits=3)
    source, prompt, payload, raw, explanation, credential = (
        "GROUP_SOURCE_010",
        "GROUP_PROMPT_020",
        "GROUP_PAYLOAD_030",
        "GROUP_RAW_040",
        "GROUP_AUTHORIZED_050",
        "GROUP_CREDENTIAL_060",
    )
    spec.input.path.write_text(
        json.dumps({"item_id": "000", "text": source}) + "\n", encoding="utf-8"
    )
    template = tmp_path / "inputs" / "prompt.txt"
    template.write_text(
        prompt
        + "\n{{ rubric }}\n{{ exemplars }}\n{{ response }}\n{{ output_instructions }}",
        encoding="utf-8",
    )
    spec.prompt.file, spec.prompt.preset = template, None
    spec.provider, spec.endpoint = "mock", "synthetic"
    spec.execution.grouping, spec.execution.explanations, spec.execution.concurrency = (
        "grouped",
        True,
        1,
    )
    store, ledger = tmp_path / "store", tmp_path / "service" / "ledger.sqlite3"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockAdapter(
        ledger,
        scenario=MockScenario(
            members={"001": MockMemberScenario(outcome="malformed")},
            explanation=explanation,
            payload_marker=payload,
            raw_marker=raw,
        ),
    )
    with Repository(store) as repository:
        lock = repository.run_directory("001") / "executor.lock"
    deferred: set[Path] = set()

    def inspect(boundary: str, uid: str) -> None:
        for path in tmp_path.rglob("*"):
            if not path.is_file() or tmp_path / "inputs" in path.parents:
                continue
            content = read_live_runtime_file(
                path,
                root=tmp_path,
                executor_lock=lock,
                deferred=deferred,
                windows_locking=sys.platform == "win32",
                sqlite_shm=store / "registry.sqlite3-shm",
            )
            if content is not None:
                assert all(
                    s.encode() not in content
                    for s in (source, prompt, payload, raw, credential)
                )
                if explanation.encode() in content:
                    assert path in {
                        store / "registry.sqlite3",
                        store / "registry.sqlite3-wal",
                    }
        if boundary == stage:
            raise RuntimeError("synthetic interruption")

    if stage:
        with pytest.raises(ExceptionGroup):
            await execute_run(
                "001", store=store, adapter=adapter, spec=spec, fault=inspect
            )
        await recover_run("001", store=store, adapter=adapter, spec=spec)
    else:
        await execute_run("001", store=store, adapter=adapter, spec=spec, fault=inspect)
    assert adapter.ledger.acceptance_count() == 1
    assert lock.read_bytes() == b"\0"
    for path in tmp_path.rglob("*"):
        if path.is_file() and tmp_path / "inputs" not in path.parents:
            assert all(
                s.encode() not in path.read_bytes()
                for s in (source, prompt, payload, raw, credential)
            )
    captured = capsys.readouterr()
    assert not captured.out and not captured.err
