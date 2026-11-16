"""Fresh OS-process crash windows; receiver entries precede acceptance dedup."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_decisions_contracts import pair_spec
from test_decisions_lifecycle import native_spec
from test_preparation_rendering import specification

from judgmentllm.config import RunSpec, RunSpecV2
from judgmentllm.decisions.provenance import export_decisions_provenance
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run

CHILD = r"""
import asyncio, json, os, sys
from pathlib import Path
from judgmentllm.config import load_run_spec
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.persistence.repository import Repository
from judgmentllm.decisions.provenance import export_decisions_provenance
from judgmentllm.providers.decisions_mock import MockDecisionsAdapter, DecisionsScenarioV1
from judgmentllm.preparation import open_prepared_run
store, config, target, mode = sys.argv[1:]
spec = load_run_spec(config)
def receiver(submission):
    with open(Path(store) / "receiver.jsonl", "a", encoding="utf-8") as stream:
        stream.write(json.dumps(submission) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    if target == "receiver_before_acceptance":
        os._exit(73)
def fault(stage, *_):
    if stage == target:
        os._exit(73)
scenario = DecisionsScenarioV1() if mode in {"retry", "retry_resume"} else DecisionsScenarioV1.model_validate({"members": {"01": {"outcome": "refusal"}}, "transport": "timeout_after" if mode == "uncertain" else "success"})
adapter = MockDecisionsAdapter(Path(store) / "mock-service.sqlite3", scenario=scenario, receiver_observer=receiver, retrieval_available=mode != "unavailable", reconciliation_available=mode != "unavailable")
if mode == "retry":
    with Repository(store) as repository:
        failed = next(r for r in repository.iter_results("001") if r.status == "refused")
    attempt = authorize_subset_retry("001", store=store, adapter=adapter, task_ids=(failed.task_id,), operation_id="retry-001", fault=fault if target else None)
    print(attempt.model_dump_json())
elif mode == "export":
    report = export_decisions_provenance("001", store=store, fault=fault)
    print(report.model_dump_json())
elif mode == "open":
    with open_prepared_run("001", store=store, spec=spec) as prepared:
        print(json.dumps([{"request": d.request.request_uid, "tasks": [t.task_id for t in d.request.tasks], "presentation": d.presentation_id, "orders": [q.pair.presented_order for q in d.questions]} for d in prepared.iter_request_dependencies()]))
else:
    report = asyncio.run((execute_run if target else recover_run)("001", store=store, adapter=adapter, spec=spec, fault=fault if target else None))
    print(report.model_dump_json())
"""


def run_child(
    store: Path, config: Path, stage: str = "", mode: str = "normal"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", CHILD, str(store), str(config), stage, mode],
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("kind", ["rubric", "pairwise", "legacy-v1", "legacy-v2"])
def test_first_public_resume_after_preseal_process_death_uses_original_adapter(
    tmp_path: Path, capture: str, kind: str
) -> None:
    if kind.startswith("legacy"):
        original = specification(tmp_path / "inputs", responses=1, traits=2)
        model = RunSpec if kind == "legacy-v1" else RunSpecV2
        spec = model.model_validate(
            {
                **original.model_dump(),
                "schema_version": 1 if kind == "legacy-v1" else 2,
                "provider": "mock",
                "endpoint": "synthetic",
                "capture": capture,
                "execution": {"grouping": "grouped", "concurrency": 1},
            }
        )
        expected_tasks = 2
    else:
        original = native_spec(tmp_path) if kind == "rubric" else pair_spec(tmp_path)
        spec = type(original).model_validate(
            {**original.model_dump(), "capture": capture}
        )
        expected_tasks = 3 if kind == "rubric" else 12
    config = tmp_path / "config.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    store = tmp_path / "store"
    crashed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys; import judgmentllm.preparation as preparation\n"
            "from judgmentllm import run_live\n"
            "def fault(stage, *_):\n"
            "    if stage == 'manifest_published': os._exit(73)\n"
            "preparation._checkpoint = fault\n"
            "run_live(sys.argv[1], store=sys.argv[2])\n",
            str(config),
            str(store),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert crashed.returncode == 73, crashed.stderr
    with Repository(store) as repository:
        (row,) = repository.connection.execute("SELECT run_id FROM runs").fetchall()
        run = repository.get_run(row["run_id"])
        assert run.state == "created" and run.manifest_sha256 is None
        assert repository.decisions_version(run.run_id) == (
            None if kind.startswith("legacy") else 4
        )
        before = repository.summary(run.run_id)
        assert before.results == 0 and not list(repository.iter_submissions(run.run_id))
        memberships = {
            request.request_uid: repository.request_task_ids(
                run.run_id, request.request_uid
            )
            for request in repository.iter_requests(run.run_id)
        }
        manifest_file = repository.run_directory(run.run_id) / "manifest.json"
        published = manifest_file.read_bytes()
    reports: list[dict[str, object]] = []
    for _ in range(2):
        resumed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from judgmentllm import resume_run; "
                "print(resume_run(sys.argv[1], store=sys.argv[2]).model_dump_json())",
                run.run_id,
                str(store),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert resumed.returncode == 0, resumed.stderr
        report = json.loads(resumed.stdout)
        assert report["outcome"] == "ok" and report["run_status"] == "completed"
        assert report["results"] == report["completed_tasks"] == expected_tasks
        assert not report["recovery_required"] and not report["executor_active"]
        assert ("decisions" in report) == (not kind.startswith("legacy"))
        reports.append(report)
    assert reports[0]["attempts"] == reports[1]["attempts"]
    with Repository(store) as repository:
        after = repository.summary(run.run_id)
        assert after.results == expected_tasks
        expected_requests = 4 if kind == "pairwise" else 1
        assert len(list(repository.iter_submissions(run.run_id))) == expected_requests
        if memberships:
            assert {
                request.request_uid: repository.request_task_ids(
                    run.run_id, request.request_uid
                )
                for request in repository.iter_requests(run.run_id)
            } == memberships
    assert reports[0]["requests"] == reports[1]["requests"] == expected_requests
    assert manifest_file.read_bytes() == published


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "stage",
    [
        "after_submitting",
        "receiver_before_acceptance",
        "after_submit",
        "after_acknowledgment_receipt",
        "after_acknowledgment",
        "after_response",
        "after_evidence_artifacts",
        "after_evidence_receipt",
        "after_receipt",
        "during_evidence_transaction",
        "after_observation",
        "during_member_validation",
        "after_decisions_outcome_artifact",
        "during_result_transaction",
        "after_result",
    ],
)
def test_real_death_reopens_and_commits_a_full_vector_once(
    tmp_path: Path, stage: str, capture: str
) -> None:
    spec = native_spec(tmp_path, capture=capture)
    config = tmp_path / "config.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    store = tmp_path / "store"
    with prepare_run(config, store=store, run_id="001"):
        pass
    crashed = run_child(store, config, stage)
    assert crashed.returncode == 73, crashed.stderr
    with Repository(store) as repository:
        before = repository.summary("001")
        assert before.results in {0, 3}
        if stage == "during_result_transaction":
            assert before.results == 0
            assert (
                repository.connection.execute(
                    "SELECT count(*) FROM tasks WHERE state IN ('complete','refused')"
                ).fetchone()[0]
                == 0
            )
    resumed = run_child(store, config)
    assert resumed.returncode == 0, resumed.stderr
    again = run_child(store, config)
    assert again.returncode == 0, again.stderr
    entries = (
        (store / "receiver.jsonl").read_text().splitlines()
        if (store / "receiver.jsonl").exists()
        else []
    )
    with Repository(store) as repository:
        results = list(repository.iter_results("001"))
        if stage in {"after_submitting", "receiver_before_acceptance"}:
            assert results == [] and len(entries) == int(
                stage == "receiver_before_acceptance"
            )
            assert next(repository.iter_submissions("001")).certainty == "unknown"
            assert json.loads(resumed.stdout)["recovery_required"]
            assert json.loads(again.stdout)["recovery_required"]
        else:
            assert len(results) == 3 and len(entries) == 1
            assert {r.trait_id: r.status for r in results} == {
                "00": "complete",
                "01": "refused",
                "02": "complete",
            }
    if stage not in {"after_submitting", "receiver_before_acceptance"}:
        assert (
            export_decisions_provenance(
                "001", store=store
            ).manifest.totals.known_invocations
            == 1
        )
    else:
        totals = export_decisions_provenance("001", store=store).manifest.totals
        assert totals.known_invocations == 0 and totals.invocation_unknown == 1
        assert totals.input_tokens is totals.output_tokens is totals.cost is None
        from judgmentllm.execution.retry import authorize_subset_retry
        from judgmentllm.persistence.models import RetryAuthorizationError
        from judgmentllm.providers.decisions_mock import MockDecisionsAdapter

        with Repository(store) as repository:
            request = next(repository.iter_requests("001"))
            tasks = repository.request_task_ids("001", request.request_uid)
        with pytest.raises(RetryAuthorizationError):
            authorize_subset_retry(
                "001",
                store=store,
                adapter=MockDecisionsAdapter(store / "mock-service.sqlite3"),
                task_ids=tasks,
                operation_id="unsafe-preacceptance",
            )


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_lost_accepted_response_without_reconciliation_stays_held(
    tmp_path: Path, capture: str
) -> None:
    spec = native_spec(tmp_path, capture=capture)
    config = tmp_path / "config.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    store = tmp_path / "store"
    with prepare_run(config, store=store, run_id="001"):
        pass
    assert run_child(store, config, "after_submit").returncode == 73
    for _ in range(2):
        held = run_child(store, config, mode="unavailable")
        assert held.returncode == 0, held.stderr
        assert json.loads(held.stdout)["recovery_required"]
    assert len((store / "receiver.jsonl").read_text().splitlines()) == 1
    with Repository(store) as repository:
        assert repository.summary("001").results == 0
        assert next(repository.iter_submissions("001")).certainty == "unknown"


@pytest.mark.parametrize(
    "stage",
    [
        "after_provenance_artifact",
        "during_provenance_registration",
        "before_provenance_reference",
        "after_provenance_reference",
    ],
)
def test_provenance_process_death_and_repeated_publication(
    tmp_path: Path, stage: str
) -> None:
    spec = native_spec(tmp_path)
    config = tmp_path / "config.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    store = tmp_path / "store"
    with prepare_run(config, store=store, run_id="001"):
        pass
    assert run_child(store, config).returncode == 0
    death = run_child(store, config, stage, "export")
    assert death.returncode == 73, death.stderr
    references = list((store / "runs").glob("*/decisions-provenance/*.json"))
    assert len(references) == int(stage == "after_provenance_reference")
    first = run_child(store, config, mode="export")
    second = run_child(store, config, mode="export")
    assert first.returncode == second.returncode == 0, (first.stderr, second.stderr)
    assert first.stdout == second.stdout
    assert len((store / "receiver.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "stage",
    [
        "after_provenance_artifact",
        "during_provenance_registration",
        "before_provenance_reference",
        "after_provenance_reference",
    ],
)
def test_empty_companion_publication_survives_process_death_without_source_upgrade(
    tmp_path: Path, capture: str, stage: str
) -> None:
    from judgmentllm.persistence.hashing import bytes_digest

    spec = native_spec(tmp_path, sources=0, capture=capture)
    config = tmp_path / "config.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    store = tmp_path / "store"
    with prepare_run(config, store=store, run_id="001") as prepared:
        original = prepared.repository.get_artifact("001", bytes_digest(b""))
    completed = run_child(store, config)
    assert completed.returncode == 0, completed.stderr
    death = run_child(store, config, stage, "export")
    assert death.returncode == 73, death.stderr
    references = list((store / "runs").glob("*/decisions-provenance/*.json"))
    assert len(references) == int(stage == "after_provenance_reference")
    first, second = (
        run_child(store, config, mode="export"),
        run_child(store, config, mode="export"),
    )
    assert first.returncode == second.returncode == 0, (first.stderr, second.stderr)
    assert first.stdout == second.stdout
    with Repository(store) as repository:
        assert repository.get_artifact("001", original.sha256) == original
        assert repository.summary("001").results == 0
        assert (
            repository.connection.execute("SELECT count(*) FROM exports").fetchone()[0]
            == 0
        )
    assert not (store / "receiver.jsonl").exists()


@pytest.mark.parametrize("seed", [41, 42])
def test_sched1_preparation_reopens_in_a_fresh_process(
    tmp_path: Path, seed: int
) -> None:
    spec = pair_spec(tmp_path, seed=seed)
    config = tmp_path / "config.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    store = tmp_path / "store"
    with prepare_run(config, store=store, run_id="001"):
        pass
    opened = run_child(store, config, mode="open")
    assert opened.returncode == 0, opened.stderr
    rows = json.loads(opened.stdout)
    assert len(rows) == 4 and sum(len(r["tasks"]) for r in rows) == 12
    assert all(r["orders"].count(r["orders"][0]) == 3 for r in rows)
    assert rows[0]["orders"][0] == (["002", "001"] if seed == 41 else ["001", "002"])


def test_failed_subset_retry_authorization_survives_fresh_process_death(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path)
    config = tmp_path / "config.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    store = tmp_path / "store"
    with prepare_run(config, store=store, run_id="001"):
        pass
    assert run_child(store, config).returncode == 0
    crash = run_child(store, config, "after_retry_preparation", "retry")
    assert crash.returncode == 73, crash.stderr
    resumed = run_child(store, config, mode="retry_resume")
    assert resumed.returncode == 0, resumed.stderr
    repeated = run_child(store, config, mode="retry")
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["attempt_number"] == 2
    with Repository(store) as repository:
        assert repository.summary("001").results == 4
        assert len(tuple(repository.iter_attempts("001"))) == 2
    entries = (store / "receiver.jsonl").read_text().splitlines()
    assert len(entries) == len(set(entries)) == 2


def test_forwarding_fact_receipt_crash_can_reconcile_without_rewriting_it(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path)
    config = tmp_path / "config.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    store = tmp_path / "store"
    with prepare_run(config, store=store, run_id="001"):
        pass
    crash = run_child(store, config, "after_decisions_invocation_receipt", "uncertain")
    assert crash.returncode == 73, crash.stderr
    facts = list((store / "runs").glob("*/decisions-invocations/*.json"))
    assert len(facts) == 1
    before = facts[0].read_bytes()
    resumed = run_child(store, config)
    assert resumed.returncode == 0, resumed.stderr
    assert facts[0].read_bytes() == before
    with Repository(store) as repository:
        assert repository.summary("001").results == 3
    assert len((store / "receiver.jsonl").read_text().splitlines()) == 1
