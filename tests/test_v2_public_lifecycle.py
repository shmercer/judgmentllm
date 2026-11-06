"""V2 public activation, content retention and independent recovery acceptance."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from judgmentllm import (
    LifecycleError,
    RunSpec,
    RunSpecV2,
    arun_live,
    estimate_run,
    export_run_results,
    get_run,
    load_run_spec,
    resume_run,
    run_live,
    validate_inputs,
)
from judgmentllm.io import read_jsonl
from judgmentllm.providers import MockAdapter
from judgmentllm.results import RubricResult

SENTINELS = (
    "PRIVATE_RESPONSE_v2_6f189",
    "PRIVATE_RUBRIC_DESCRIPTION_v2_6f189",
    "PRIVATE_EXEMPLAR_v2_6f189",
    "PRIVATE_RATIONALE_v2_6f189",
    "PRIVATE_METADATA_v2_6f189",
)


def configuration(
    directory: Path, *, capture: str = "minimal", order: str = "scores_first"
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    responses = directory / "responses.jsonl"
    responses.write_text(
        json.dumps({"item_id": "001", "text": SENTINELS[0]}) + "\n", encoding="utf-8"
    )
    rubric = {
        "rubric_id": "001",
        "name": "Synthetic",
        "description": SENTINELS[1],
        "traits": [
            {
                "trait_id": ident,
                "name": ident,
                "description": SENTINELS[1],
                "levels": [
                    {"label": "low", "numeric_value": 1},
                    {"label": "high", "numeric_value": 8},
                ],
            }
            for ident in ("01", "02")
        ],
    }
    path = directory / "run.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "task_kind": "rubric",
                "input": str(responses),
                "rubric": rubric,
                "provider": "mock",
                "endpoint": "synthetic",
                "model": "offline-v2",
                "execution": {
                    "grouping": "grouped",
                    "explanations": True,
                    "concurrency": 1,
                },
                "prompt": {"output_order": order},
                "capture": capture,
                "exemplars": [
                    {
                        "exemplar_id": "0001",
                        "text": SENTINELS[2],
                        "rationales": {"01": SENTINELS[3]},
                        "metadata": {"private": SENTINELS[4]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def contents(root: Path) -> bytes:
    return b"\n".join(path.read_bytes() for path in root.rglob("*") if path.is_file())


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("order", ["together", "scores_first", "explanations_first"])
def test_public_execution_preserves_contract_privacy_and_replay(
    tmp_path: Path,
    capture: str,
    order: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_connect = socket.socket.connect

    def no_network(connection: socket.socket, address: tuple[str, int]) -> None:
        # Windows asyncio creates its wakeup socketpair using IPv4 loopback.
        # Permit that local IPC while prohibiting external provider connections.
        if os.name == "nt" and address[0] == "127.0.0.1":
            original_connect(connection, address)
            return
        raise AssertionError("offline execution must not open an external connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    source = configuration(tmp_path / "input", capture=capture, order=order)
    store = tmp_path / "runtime"
    spec = load_run_spec(source)
    assert isinstance(spec, RunSpecV2)
    assert validate_inputs(spec).valid
    assert estimate_run(spec).requests == 1
    result = run_live(source, store=store)
    assert result.outcome == "ok" and result.completed_tasks == 2
    assert result.output_selection is not None
    assert result.output_selection.mechanism == "text"
    assert (
        get_run(result.run_id, store=store).output_selection == result.output_selection
    )
    ledger = MockAdapter(store / "mock-service.sqlite3").ledger
    assert ledger.acceptance_count() == 1
    resumed = resume_run(result.run_id, store=store)
    assert resumed.results == 2 and ledger.acceptance_count() == 1
    exported = export_run_results(result.run_id, store=store)
    rows = list(read_jsonl(exported.files[0].path, RubricResult))
    assert {row.trait_id for row in rows} == {"01", "02"}
    assert all(
        row.expected_value is None and row.primary_score_kind == "category_code"
        for row in rows
    )
    retained = contents(store)
    for sentinel in SENTINELS:
        assert (sentinel.encode() in retained) == (capture == "full")


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_independent_process_recovers_exact_accepted_v2_observation(
    tmp_path: Path, capture: str
) -> None:
    source = configuration(tmp_path / "input", capture=capture)
    store = tmp_path / "runtime"
    script = """
import asyncio, os, sys
from pathlib import Path
from judgmentllm.preparation import prepare_run
from judgmentllm.execution import execute_run
from judgmentllm.providers import MockAdapter
source, store = Path(sys.argv[1]), Path(sys.argv[2])
adapter = MockAdapter(store / 'mock-service.sqlite3')
with prepare_run(source, store=store, run_id='v2-private', capabilities=adapter.capabilities_for('mock', 'synthetic', 'offline-v2')):
    pass
def fault(stage, uid):
    if stage == 'after_receipt':
        os._exit(76)
asyncio.run(execute_run('v2-private', store=store, adapter=adapter, fault=fault))
"""
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(source), str(store)],
        capture_output=True,
        timeout=30,
    )
    assert crashed.returncode == 76, crashed.stderr.decode()
    ledger = MockAdapter(store / "mock-service.sqlite3").ledger
    assert ledger.acceptance_count() == 1
    manifest = next((store / "runs").glob("*/manifest.json"))
    original_manifest = manifest.read_bytes()
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    resumed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from judgmentllm import resume_run; print(resume_run('v2-private', store=sys.argv[1]).model_dump_json())",
            str(store),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=30,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["completed_tasks"] == 2
    assert ledger.acceptance_count() == 1
    assert manifest.read_bytes() == original_manifest
    retained = contents(store)
    for sentinel in SENTINELS:
        assert (sentinel.encode() in retained) == (capture == "full")


def test_native_rejected_with_safe_diagnostic_before_runtime_creation(
    tmp_path: Path,
) -> None:
    source = configuration(tmp_path / "input")
    document = json.loads(source.read_text())
    document["prompt"]["structured_output"] = "native"
    source.write_text(json.dumps(document), encoding="utf-8")
    store = tmp_path / "runtime"
    report = validate_inputs(source)
    assert not report.valid
    assert any(
        issue.field == "prompt.structured_output" and "native" in issue.message
        for issue in report.issues
    )
    with pytest.raises(LifecycleError, match="native") as error:
        run_live(source, store=store)
    assert error.value.category == "unsupported_capability"
    assert not store.exists()


def test_v1_api_rejects_v2_instance_and_mutated_v2_settings(tmp_path: Path) -> None:
    spec = load_run_spec(configuration(tmp_path / "input"))
    assert isinstance(spec, RunSpecV2)
    with pytest.raises(ValidationError):
        RunSpec.model_validate(spec)
    changed = spec.model_copy(
        update={"prompt": spec.prompt.model_copy(update={"arbitrary_schema": {}})}
    )
    with pytest.raises(LifecycleError):
        run_live(changed, store=tmp_path / "runtime")
    assert not (tmp_path / "runtime").exists()


@pytest.mark.asyncio
async def test_async_public_v2_execution(tmp_path: Path) -> None:
    result = await arun_live(
        configuration(tmp_path / "input"), store=tmp_path / "runtime"
    )
    assert result.outcome == "ok" and result.results == 2


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_pair_contract_metadata_privacy_and_independent_reopening(
    tmp_path: Path, capture: str
) -> None:
    source = configuration(tmp_path / "input", capture=capture)
    document = json.loads(source.read_text(encoding="utf-8"))
    response_path = Path(document["input"])
    with response_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"item_id": "002", "text": SENTINELS[0]}) + "\n")
    private_pair_metadata = "PRIVATE_PAIR_METADATA_v2_93bd"
    pairs = source.parent / "pairs.jsonl"
    pairs.write_text(
        json.dumps(
            {
                "pair_id": "0001",
                "pair_uid": "0001-reversed",
                "left_item_id": "001",
                "right_item_id": "002",
                "trait_id": "01",
                "presented_order": ["002", "001"],
                "metadata": {"private": private_pair_metadata},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    document.pop("rubric")
    document.update(
        task_kind="pairwise",
        pairs=str(pairs),
        criteria=[{"trait_id": "01", "name": "Synthetic", "description": SENTINELS[1]}],
        execution={"grouping": "independent", "explanations": True},
        prompt={},
    )
    source.write_text(json.dumps(document), encoding="utf-8")
    store = tmp_path / "runtime"
    first = run_live(source, store=store)
    assert first.outcome == "ok" and first.results == 1
    reopened = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from judgmentllm import resume_run; print(resume_run(sys.argv[1], store=sys.argv[2]).model_dump_json())",
            first.run_id,
            str(store),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=30,
    )
    assert reopened.returncode == 0, reopened.stderr
    assert json.loads(reopened.stdout)["completed_tasks"] == 1
    assert MockAdapter(store / "mock-service.sqlite3").ledger.acceptance_count() == 1
    retained = contents(store)
    for sentinel in (*SENTINELS, private_pair_metadata):
        assert (sentinel.encode() in retained) == (capture == "full")
