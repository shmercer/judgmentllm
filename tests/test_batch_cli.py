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
