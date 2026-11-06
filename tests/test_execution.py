"""Actual submission boundaries, recovery evidence, and capture privacy."""

from __future__ import annotations

import asyncio
import json
import mmap
import sys
from pathlib import Path
from typing import Literal

import pytest
from test_preparation_rendering import specification

from judgmentllm.config import RunSpec
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers import (
    MockAdapter,
    MockScenario,
    PreparedRequest,
    ProviderAcknowledgment,
)
from judgmentllm.results import PairwiseResult, RubricResult


def setup(
    tmp_path: Path,
    *,
    capture: Literal["full", "minimal"] = "minimal",
    pairwise: bool = False,
    responses: int = 1,
    traits: int = 1,
    explanations: bool = False,
    allow_ties: bool = False,
) -> tuple[RunSpec, Path, Path]:
    spec = specification(
        tmp_path / "inputs",
        responses=max(responses, 2) if pairwise else responses,
        traits=traits,
        pairwise=pairwise,
    )
    spec.provider, spec.endpoint, spec.capture = "mock", "synthetic", capture
    spec.execution.concurrency = 1
    spec.execution.explanations = explanations
    spec.prompt.preset = None
    spec.allow_ties = allow_ties
    if allow_ties:
        template = tmp_path / "inputs" / "ties.txt"
        template.write_text(
            "{{ criterion }}\n{{ exemplars }}\n{{ first_response }}\n{{ second_response }}\n{{ output_instructions }}"
        )
        spec.prompt.file = template
    if pairwise:
        assert spec.pairs is not None
        rows = [json.loads(line) for line in spec.pairs.path.read_text().splitlines()][
            :responses
        ]
        for row in rows:
            row["presented_order"] = ["001", "000"]
        spec.pairs.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    store, ledger = tmp_path / "store", tmp_path / "service" / "ledger.sqlite3"
    spec = RunSpec.model_validate(spec.model_dump())
    with prepare_run(spec, store=store, run_id="001"):
        pass
    return spec, store, ledger


def interrupt(target: str):
    def fault(stage: str, _: str) -> None:
        if stage == target:
            raise RuntimeError("synthetic interruption")

    return fault


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("pairwise", [False, True])
async def test_end_to_end_exact_evidence_and_idempotent_completed_resume(
    tmp_path: Path,
    capture: Literal["full", "minimal"],
    pairwise: bool,
) -> None:
    spec, store, ledger = setup(
        tmp_path, capture=capture, pairwise=pairwise, explanations=True
    )
    adapter = MockAdapter(
        ledger, scenario=MockScenario(probabilities=True, category_index=0)
    )
    report = await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert report.summary.state == "completed" and report.summary.results == 1
    with Repository(store) as repository:
        result = list(repository.iter_results("001"))[0]
        attempt = next(repository.iter_attempts("001"))
        submission = next(repository.iter_submissions("001"))
        observation = repository.get_observation_for_submission(
            "001", submission.submission_id
        )
        assert observation is not None and observation.attempt_id == result.attempt_id
        assert submission.attempt_id == result.attempt_id == attempt.attempt_id
        assert submission.payload_sha256 == attempt.payload_sha256
        assert repository.request_task_ids("001", result.request_uid) == (
            result.task_id,
        )
        assert repository.attempt_task_ids("001", attempt.attempt_id) == (
            result.task_id,
        )
        assert result.explanation == "Synthetic mock assessment."
        assert (
            bool(result.raw_response_path)
            == bool(result.prompt_path)
            == (capture == "full")
        )
        if isinstance(result, PairwiseResult):
            assert result.original_order == ["000", "001"]
            assert result.presented_order == ["001", "000"]
            assert result.winner_id == "001" and result.p_right == 0.75
        else:
            assert isinstance(result, RubricResult)
            assert (
                result.category_label == "low" and result.modal_category_label == "high"
            )
            assert result.primary_score_kind == "expected"
            assert result.probabilities is not None and len(result.probabilities) == 2
    calls = adapter.submit_calls
    await recover_run("001", store=store, adapter=adapter, spec=spec)
    assert adapter.submit_calls == calls == 1 and adapter.ledger.acceptance_count() == 1


STAGES = [
    "before_request_creation",
    "after_preparation_artifacts",
    "after_preparation",
    "after_claim",
    "after_submitting",
    "after_submit",
    "after_acknowledgment_receipt",
    "after_acknowledgment",
    "after_response",
    "after_receipt",
    "after_observation",
    "before_parsing",
    "before_validation",
    "after_normalization",
    "during_result_transaction",
    "after_result",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("stage", STAGES)
async def test_all_durable_boundaries_recover_without_second_acceptance(
    tmp_path: Path,
    capture: Literal["full", "minimal"],
    stage: str,
) -> None:
    spec, store, ledger = setup(tmp_path, capture=capture)
    adapter = MockAdapter(ledger)
    with pytest.raises(ExceptionGroup):
        await execute_run(
            "001", store=store, adapter=adapter, spec=spec, fault=interrupt(stage)
        )
    report = await recover_run(
        "001", store=store, adapter=MockAdapter(ledger), spec=spec
    )
    expected = 0 if stage == "after_submitting" else 1
    assert report.summary.results == expected
    assert adapter.ledger.acceptance_count() == expected
    if expected:
        assert report.summary.state == "completed"
    else:
        with Repository(store) as repository:
            submission = next(repository.iter_submissions("001"))
            assert submission.state == "confirmed_not_submitted"
            assert list(repository.iter_results("001")) == []
            assert (
                repository.get_observation_for_submission(
                    "001", submission.submission_id
                )
                is None
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "after_submit",
        "after_acknowledgment_receipt",
        "after_acknowledgment",
        "after_response",
        "after_receipt",
        "after_observation",
        "before_parsing",
        "before_validation",
        "after_normalization",
        "during_result_transaction",
    ],
)
async def test_minimal_lost_response_unavailable_retrieval_stays_unresolved(
    tmp_path: Path,
    stage: str,
) -> None:
    spec, store, ledger = setup(tmp_path)
    adapter = MockAdapter(ledger)
    with pytest.raises(ExceptionGroup):
        await execute_run(
            "001", store=store, adapter=adapter, spec=spec, fault=interrupt(stage)
        )
    unavailable = MockAdapter(ledger, retrieval_available=False)
    report = await recover_run("001", store=store, adapter=unavailable, spec=spec)
    assert report.recovery_required and report.summary.results == 0
    assert report.summary.state != "completed"
    assert unavailable.submit_calls == 0 and unavailable.ledger.acceptance_count() == 1
    recovered = await recover_run(
        "001", store=store, adapter=MockAdapter(ledger), spec=spec
    )
    assert recovered.summary.results == 1 and adapter.ledger.acceptance_count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,status,error",
    [
        ("invalid", "invalid", "invalid_output"),
        ("refusal", "refused", "provider_refusal"),
        ("provider_error", "errored", "provider_error"),
    ],
)
async def test_received_outcomes_are_distinct(
    tmp_path: Path,
    outcome: str,
    status: str,
    error: str,
) -> None:
    spec, store, ledger = setup(tmp_path)
    adapter = MockAdapter(
        ledger, scenario=MockScenario.model_validate({"outcome": outcome})
    )
    report = await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert report.summary.results == 1 and report.summary.state == "partial"
    with Repository(store) as repository:
        result = next(repository.iter_results("001"))
        assert result.status == status and result.error_kind == error
        assert isinstance(result, RubricResult) and result.primary_score is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport",
    ["not_submitted", "timeout_before_acceptance", "timeout_after_acceptance"],
)
async def test_transport_evidence_never_fabricates_provider_observations(
    tmp_path: Path,
    transport: str,
) -> None:
    spec, store, ledger = setup(tmp_path)
    adapter = MockAdapter(
        ledger, scenario=MockScenario.model_validate({"transport": transport})
    )
    report = await execute_run("001", store=store, adapter=adapter, spec=spec)
    if transport == "not_submitted":
        assert report.summary.results == 1 and not report.recovery_required
        with Repository(store) as repository:
            result = next(repository.iter_results("001"))
            assert result.error_kind == "transport_not_submitted"
            assert (
                repository.connection.execute(
                    "SELECT COUNT(*) FROM observations"
                ).fetchone()[0]
                == 0
            )
    else:
        assert report.recovery_required and report.summary.results == 0
        report = await recover_run(
            "001", store=store, adapter=MockAdapter(ledger), spec=spec
        )
        assert report.summary.results == (
            1 if transport == "timeout_after_acceptance" else 0
        )


@pytest.mark.asyncio
async def test_tie_and_explanation(tmp_path: Path) -> None:
    spec, store, ledger = setup(
        tmp_path, pairwise=True, explanations=True, allow_ties=True
    )
    adapter = MockAdapter(
        ledger, scenario=MockScenario(winner="tie", probabilities=True)
    )
    await execute_run("001", store=store, adapter=adapter, spec=spec)
    with Repository(store) as repository:
        result = next(repository.iter_results("001"))
        assert isinstance(result, PairwiseResult) and result.tie and result.p_tie == 0.5


def read_live_runtime_file(
    path: Path,
    *,
    root: Path,
    executor_lock: Path,
    deferred: set[Path],
    windows_locking: bool,
    sqlite_shm: Path | None = None,
    mapped: set[Path] | None = None,
) -> bytes | None:
    """Scan the exact SQLite WAL index through Windows locks; defer only ownership."""
    try:
        return path.read_bytes()
    except PermissionError:
        relative = path.relative_to(root).as_posix()
        if windows_locking and path == sqlite_shm:
            assert mapped is not None
            assert not path.is_symlink() and path.is_file() and path.stat().st_size > 0
            # SQLite locks bytes in its WAL index during writes. Windows mapped
            # views remain readable through byte-range locks; inspect all bytes.
            try:
                with path.open("rb") as stream:
                    with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as view:
                        content = view[:]
            except (OSError, ValueError):
                raise AssertionError(
                    f"unreadable live SQLite WAL index: {relative}"
                ) from None
            mapped.add(path)
            return content
        assert windows_locking and path == executor_lock, (
            f"unexpected unreadable live runtime file: {relative}"
        )
        assert not path.is_symlink() and path.stat().st_size == 1, (
            f"unexpected executor lock identity/length: {relative}"
        )
        deferred.add(path)
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [None, "after_receipt", "during_result_transaction"])
async def test_minimal_privacy_success_and_loss_windows(
    tmp_path: Path,
    stage: str | None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = specification(tmp_path / "inputs", responses=1, traits=1)
    spec.provider, spec.endpoint = "mock", "synthetic"
    spec.execution.explanations = True
    spec.prompt.preset = None
    source, prompt, payload, raw, explanation, credential = (
        "SOURCE_PRIVATE_151",
        "PROMPT_PRIVATE_252",
        "PAYLOAD_PRIVATE_353",
        "RAW_PRIVATE_454",
        "EXPLANATION_AUTHORIZED_555",
        "CREDENTIAL_PRIVATE_656",
    )
    spec.input.path.write_text(json.dumps({"item_id": "000", "text": source}) + "\n")
    template = tmp_path / "inputs" / "prompt.txt"
    template.write_text(
        prompt
        + "\n{{ rubric }}\n{{ exemplars }}\n{{ response }}\n{{ output_instructions }}"
    )
    spec.prompt.file = template
    store, ledger = tmp_path / "store", tmp_path / "ledger.sqlite3"
    spec = RunSpec.model_validate(spec.model_dump())
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockAdapter(
        ledger,
        scenario=MockScenario(
            payload_marker=payload, raw_marker=raw, explanation=explanation
        ),
    )
    with Repository(store) as repository:
        executor_lock = repository.run_directory("001") / "executor.lock"
    deferred: set[Path] = set()
    mapped: set[Path] = set()
    sqlite_shm = store / "registry.sqlite3-shm"
    normalized_result_files = {
        store / "registry.sqlite3",
        store / "registry.sqlite3-wal",
    }
    injected = False

    def inspect_live(boundary: str, _: str) -> None:
        nonlocal injected
        # Inspect WAL/staging while the executor connection is still open.
        for path in tmp_path.rglob("*"):
            if not path.is_file() or (tmp_path / "inputs") in path.parents:
                continue
            content = read_live_runtime_file(
                path,
                root=tmp_path,
                executor_lock=executor_lock,
                deferred=deferred,
                windows_locking=sys.platform == "win32",
                sqlite_shm=sqlite_shm,
                mapped=mapped,
            )
            if content is None:
                continue
            assert all(
                marker.encode() not in content
                for marker in (source, prompt, payload, raw, credential)
            ), path.relative_to(tmp_path)
            if explanation.encode() in content:
                assert path in normalized_result_files, path.relative_to(tmp_path)
        if boundary == stage:
            injected = True
            raise RuntimeError("synthetic interruption")

    if stage:
        with pytest.raises(ExceptionGroup):
            await execute_run(
                "001", store=store, adapter=adapter, spec=spec, fault=inspect_live
            )
    else:
        await execute_run(
            "001", store=store, adapter=adapter, spec=spec, fault=inspect_live
        )
    assert injected == (stage is not None)
    assert deferred == ({executor_lock} if sys.platform == "win32" else set())
    assert mapped == (
        {sqlite_shm} if sys.platform == "win32" and stage != "after_receipt" else set()
    )
    # Ownership has ended: no file is exempt, including the mandatory lock byte.
    assert executor_lock.read_bytes() == b"\0"
    captured = capsys.readouterr()
    assert not captured.out and not captured.err
    for path in tmp_path.rglob("*"):
        if not path.is_file() or (tmp_path / "inputs") in path.parents:
            continue
        content = path.read_bytes()
        assert all(
            marker.encode() not in content
            for marker in (source, prompt, payload, raw, credential)
        ), path
        if explanation.encode() in content:
            assert stage is None and path in normalized_result_files, path.relative_to(
                tmp_path
            )
    if stage:
        await recover_run("001", store=store, adapter=adapter, spec=spec)
        assert adapter.ledger.acceptance_count() == 1


@pytest.mark.asyncio
async def test_logical_order_preserved_when_completions_reorder(tmp_path: Path) -> None:
    spec, store, ledger = setup(tmp_path, responses=5)
    # Concurrency is frozen; build this fixture again with the selected value.
    spec.execution.concurrency = 3
    store = tmp_path / "concurrent"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        requests = [
            dependency.request for dependency in prepared.iter_request_dependencies()
        ]
    second_committed = asyncio.Event()
    loop = asyncio.get_running_loop()

    class ReorderedAdapter(MockAdapter):
        async def submit(
            self,
            prepared: PreparedRequest,
            *,
            submission_id: str,
            correlation_id: str,
        ) -> ProviderAcknowledgment:
            if prepared.request_uid == requests[0].request_uid:
                await second_committed.wait()
            return await super().submit(
                prepared, submission_id=submission_id, correlation_id=correlation_id
            )

    def observe(stage: str, request_uid: str) -> None:
        if stage == "after_result" and request_uid == requests[1].request_uid:
            # Durable result callbacks run on the executor's I/O thread.
            loop.call_soon_threadsafe(second_committed.set)

    adapter = ReorderedAdapter(ledger)
    report = await asyncio.wait_for(
        execute_run("001", store=store, adapter=adapter, spec=spec, fault=observe), 20
    )
    assert second_committed.is_set()
    assert report.worker_count == 3 and report.request_queue_high_water <= 6
    assert report.result_queue_high_water <= 3
    with Repository(store) as repository:
        assert [r.request_uid for r in repository.iter_requests("001")] == [
            r.request_uid for r in requests
        ]
        results = list(repository.iter_results("001"))
        assert results[0].request_uid != requests[0].request_uid
        assert {r.request_uid for r in results} == {r.request_uid for r in requests}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting,value",
    [
        ("provider", "openai"),
        ("endpoint", "chat"),
        ("mode", "batch_native"),
        ("cache_policy", "explicit"),
        ("tier", "priority"),
    ],
)
async def test_unsupported_execution_fails_before_submission(
    tmp_path: Path,
    setting: str,
    value: str,
) -> None:
    spec = specification(tmp_path / "inputs", responses=1, traits=2)
    spec.provider, spec.endpoint = "mock", "synthetic"
    if setting in {"provider", "endpoint"}:
        setattr(spec, setting, value)
    else:
        setattr(spec.execution, setting, value)
    spec = RunSpec.model_validate(spec.model_dump())
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockAdapter(tmp_path / "ledger")
    with pytest.raises(RuntimeError):
        await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 0
    with Repository(store) as repository:
        assert repository.summary("001").results == 0
        assert (
            repository.connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_late_render_failure_does_not_fabricate_observation(
    tmp_path: Path,
) -> None:
    spec = specification(tmp_path / "inputs", responses=2, traits=1)
    spec.provider, spec.endpoint = "mock", "synthetic"
    spec.execution.concurrency = 1
    spec.input.path.write_text(
        '{"item_id":"000","text":"ok"}\n'
        + json.dumps({"item_id": "001", "text": "large " * 5000})
        + "\n"
    )
    spec.prompt.limits.max_output_bytes = 5000
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockAdapter(tmp_path / "ledger")
    with pytest.raises(ExceptionGroup):
        await execute_run("001", store=store, adapter=adapter, spec=spec)
    with Repository(store) as repository:
        # The producer discovers the invalid second request before any submit.
        assert repository.summary("001").results == 0
        assert (
            repository.connection.execute(
                "SELECT COUNT(*) FROM observations"
            ).fetchone()[0]
            == 0
        )
    assert adapter.ledger.acceptance_count() == 0


@pytest.mark.asyncio
async def test_cancelled_initialization_releases_ownership(tmp_path: Path) -> None:
    import threading

    from judgmentllm.execution import core
    from judgmentllm.persistence.ownership import ExecutorOwnership

    spec, store, ledger = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = core._Session.__init__  # pyright: ignore[reportPrivateUsage]
    # Delay initialization after the lock has been acquired and resources exist.
    from unittest.mock import patch

    def initialize(self: core._Session, *args: object, **kwargs: object) -> None:  # pyright: ignore[reportPrivateUsage]
        original(self, *args, **kwargs)  # pyright: ignore[reportArgumentType]
        entered.set()
        release.wait(5)

    with patch.object(core._Session, "__init__", initialize):  # pyright: ignore[reportPrivateUsage]
        task = asyncio.create_task(
            execute_run("001", store=store, adapter=MockAdapter(ledger), spec=spec)
        )
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    with Repository(store) as repository, ExecutorOwnership(repository, "001"):
        assert repository.summary("001").results == 0


@pytest.mark.asyncio
async def test_acknowledgment_receipt_recovers_acceptance_without_lookup(
    tmp_path: Path,
) -> None:
    spec, store, ledger = setup(tmp_path)
    adapter = MockAdapter(ledger)
    with pytest.raises(ExceptionGroup):
        await execute_run(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            fault=interrupt("after_acknowledgment_receipt"),
        )
    unavailable = MockAdapter(
        ledger, reconciliation_available=False, retrieval_available=False
    )
    report = await recover_run("001", store=store, adapter=unavailable, spec=spec)
    assert report.recovery_required and report.summary.results == 0
    with Repository(store) as repository:
        assert next(repository.iter_submissions("001")).certainty == "accepted"
    available = MockAdapter(ledger, reconciliation_available=False)
    report = await recover_run("001", store=store, adapter=available, spec=spec)
    assert report.summary.results == 1 and available.submit_calls == 0
    assert adapter.ledger.acceptance_count() == 1
