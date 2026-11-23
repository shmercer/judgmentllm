"""Batch CLI controls and fresh source processes; installed acceptance stays separate."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_cli_lifecycle import control
from test_native_batch import batch_spec


def configuration(directory: Path) -> Path:
    spec = batch_spec(directory, capture="minimal")
    path = directory / "run.json"
    path.write_text(spec.model_dump_json(), encoding="utf-8")
    return path


def test_batch_machine_controls_use_public_services(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    store = tmp_path / "store"
    selection = ["--run", "001", "--store", str(store)]
    prepared, _ = control(["batch", "prepare", "--config", str(config), *selection])
    assert prepared["command"] == "batch prepare" and prepared["run_id"] == "001"
    submitted, _ = control(["batch", "submit", *selection])
    assert submitted["data"]["batch"]["accepted_jobs"] == 1
    control(["batch", "status", *selection, "--refresh"])
    partial, _ = control(["batch", "collect", *selection, "--max-result-rows", "1"], 3)
    assert partial["data"]["batch"]["missing_members"] == 1
    finished, _ = control(["batch", "resume", *selection])
    assert finished["run_status"] == "completed"
    exported, _ = control(["results", "provenance", *selection])
    assert exported["data"]["manifest"]["kind"] == "native_batch_audit"
    control(["batch", "status", "--help"])


def test_separate_cli_processes_keep_original_identity(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment = {**os.environ, "PYTHONPATH": source, "PYTHONDONTWRITEBYTECODE": "1"}
    selection = ["--run", "001", "--store", str(tmp_path / "store")]
    for arguments, expected in (
        (["batch", "prepare", "--config", str(config), *selection], 0),
        (["batch", "submit", *selection], 0),
        (["batch", "collect", *selection, "--max-result-rows", "1"], 3),
        (["batch", "resume", *selection], 0),
        (["results", "export", *selection], 0),
    ):
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "from judgmentllm.cli import app; app()",
                *arguments,
                "--json",
            ],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == expected, (completed.stdout, completed.stderr)
        assert len(completed.stdout.splitlines()) == 1
        envelope = json.loads(completed.stdout)
        assert envelope["schema_version"] == 1 and envelope["run_id"] == "001"
        for line in completed.stderr.splitlines():
            assert json.loads(line)["event"] in ("diagnostic", "progress")


@pytest.mark.parametrize("mode", ["live", "bulk_live"])
def test_batch_namespace_cannot_change_execution_mode(
    tmp_path: Path, mode: str
) -> None:
    config = configuration(tmp_path)
    data = json.loads(config.read_text())
    data["execution"]["mode"] = mode
    config.write_text(json.dumps(data))
    result, _ = control(
        [
            "batch",
            "prepare",
            "--config",
            str(config),
            "--store",
            str(tmp_path / "absent"),
        ],
        2,
    )
    assert result["error"]["category"] == "unsupported_capability"
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        ("submit", ["--retry-task", "task", "--retry-id", "retry"]),
        ("resume", ["--retry-task", "task"]),
        ("status", ["--retry-id", "retry"]),
        ("status", ["--allow-completed"]),
    ],
)
def test_retry_controls_fail_before_opening_a_store(
    tmp_path: Path, command: str, arguments: list[str]
) -> None:
    root = tmp_path / "absent"
    envelope, _ = control(
        ["batch", command, "--run", "001", "--store", str(root), *arguments], 2
    )
    assert envelope["error"]["category"] == "validation"
    assert not root.exists()


def test_local_status_and_explicit_cli_retry_keep_original_task(tmp_path: Path) -> None:
    from judgmentllm.persistence.repository import ReadOnlyRepository

    config = configuration(tmp_path)
    root = tmp_path / "store"
    selection = ["--run", "001", "--store", str(root)]
    control(["batch", "prepare", "--config", str(config), *selection])
    status, _ = control(["batch", "status", *selection])
    assert status["data"]["batch"]["accepted_jobs"] == 0
    control(["batch", "resume", *selection])
    with ReadOnlyRepository(root) as repository:
        original = next(repository.iter_results("001"))
    control(
        [
            "batch",
            "resume",
            *selection,
            "--retry-task",
            original.task_id,
            "--retry-id",
            "retry-001",
            "--allow-completed",
        ]
    )
    with ReadOnlyRepository(root) as repository:
        results = tuple(repository.iter_results("001"))
    selected = [r for r in results if r.task_id == original.task_id]
    assert len(selected) == 2
    assert selected[0].attempt_id != selected[1].attempt_id


def test_local_status_does_not_fall_through_when_output_handler_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import Callable

    from typer.testing import CliRunner

    from judgmentllm import batch_cli, cli

    config = configuration(tmp_path)
    root = tmp_path / "store"
    control(
        [
            "batch",
            "prepare",
            "--config",
            str(config),
            "--store",
            str(root),
            "--run",
            "001",
        ]
    )
    observed: list[object] = []

    def perform(function: Callable[[], object]) -> None:
        observed.append(function())

    monkeypatch.setattr(batch_cli, "perform", perform)
    result = CliRunner().invoke(
        cli.app, ["batch", "status", "--run", "001", "--store", str(root)]
    )
    assert result.exit_code == 0 and len(observed) == 1
