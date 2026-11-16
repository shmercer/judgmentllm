"""Ordinary installed-module CLI V4 preparation, execution and typed envelopes."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_decisions_lifecycle import native_spec

from judgmentllm.persistence.models import RunRecord
from judgmentllm.persistence.repository import Repository


def command(*arguments: str) -> dict[str, Any]:
    executable = shutil.which("judgmentllm", path=str(Path(sys.executable).parent))
    assert executable is not None, "Required installed CLI is unavailable"
    result = subprocess.run(
        [executable, *arguments, "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert len(result.stdout.splitlines()) == 1
    decoded = json.loads(result.stdout)
    assert (
        decoded["schema_version"] == 1
        and decoded["status"] == "ok"
        and decoded["error"] is None
    )
    return decoded


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_ordinary_v4_cli_and_nested_historical_listing(
    tmp_path: Path, capture: str
) -> None:
    spec = native_spec(tmp_path, capture=capture)
    config = tmp_path / "config.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    store = tmp_path / "store"
    validated = command("validate", "--config", str(config))
    assert (
        validated["data"]["valid"]
        and validated["data"]["decisions"]["logical_tasks"] == 3
    )
    planned = command("plan", "--config", str(config))
    assert planned["data"]["requests"] == 1 and "trait_groups" not in planned["data"]
    preview = command(
        "templates",
        "preview",
        "--config",
        str(config),
        "--item-id",
        "000",
        "--trait",
        "01",
    )
    assert (
        preview["data"]["decisions_preview_version"] == 1
        and len(preview["data"]["questions"]) == 3
    )
    initial = command("run", "--config", str(config), "--store", str(store))
    run_id = initial["run_id"]
    assert initial["data"]["decisions"]["known_invocations"] == 1
    assert initial["data"]["decisions"]["input_tokens"] is None
    resumed = command(
        "resume", "--run", run_id, "--store", str(store), "--config", str(config)
    )
    assert resumed["data"]["decisions"]["known_invocations"] == 1
    exported = command("results", "export", "--run", run_id, "--store", str(store))
    assert exported["data"]["row_count"] == 3
    bundle = command("results", "provenance", "--run", run_id, "--store", str(store))
    assert bundle["command"] == "results provenance"
    reference = bundle["data"]["manifest_path"]
    assert Path(reference).is_file()
    status = command("runs", "status", "--run", run_id, "--store", str(store))
    assert status["data"]["decisions"]["provenance_manifest"] == reference
    with Repository(store) as repository:
        repository.create_run(
            RunRecord(
                run_id="historical",
                task_kind="rubric",
                provider="mock",
                endpoint="synthetic",
                model="old",
            )
        )
        assert (
            repository.connection.execute("SELECT count(*) FROM exports").fetchone()[0]
            == 1
        )
    listing = command("runs", "list", "--store", str(store))
    old = next(r for r in listing["data"]["runs"] if r["run_id"] == "historical")
    new = next(r for r in listing["data"]["runs"] if r["run_id"] == run_id)
    assert "decisions" not in old and old["usage"] == dict(
        input_tokens=None, output_tokens=None, cost=None
    )
    assert new["decisions"]["known_invocations"] == 1
