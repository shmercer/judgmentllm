"""Public capability errors remain actionable and offline, with safe field names."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from test_openai_lifecycle import production_spec
from typer.testing import CliRunner

from judgmentllm.cli import app
from judgmentllm.config import RunSpecV3, load_run_spec
from judgmentllm.lifecycle import (
    LifecycleError,
    estimate_run,
    run_live,
    validate_inputs,
)
from judgmentllm.preview import preview_prompt
from judgmentllm.providers.openai import OpenAIConfigurationError


def strict_case(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[RunSpecV3, Path]:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("validation must not contact a provider")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    production_spec(directory, explanations=False, grouping="independent", traits=1)
    path = directory / "run.json"
    document = json.loads(path.read_text())
    document.update(
        strict_capabilities=True,
        generation={"temperature": 0.2, "max_output_tokens": 64},
    )
    path.write_text(json.dumps(document))
    spec = load_run_spec(path)
    assert isinstance(spec, RunSpecV3)
    return spec, path


def test_public_validation_preserves_specific_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, path = strict_case(tmp_path / "inputs", monkeypatch)
    report = validate_inputs(spec)
    assert not report.valid
    assert {issue.field for issue in report.issues if issue.severity == "error"} == {
        "generation.temperature",
        "generation.max_output_tokens",
    }
    assert all(issue.code == "unknown_capability" for issue in report.issues)
    result = CliRunner().invoke(app, ["validate", "--config", str(path), "--json"])
    assert result.exit_code == 2
    body = json.loads(result.stdout)
    assert body["error"]["category"] == "validation"
    assert {issue["field"] for issue in body["data"]["issues"]} == {
        "generation.temperature",
        "generation.max_output_tokens",
    }


@pytest.mark.parametrize("command", ["plan", "run", "templates preview"])
def test_public_workflows_report_safe_setting_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    spec, path = strict_case(tmp_path / "inputs", monkeypatch)
    arguments = [*command.split(), "--config", str(path), "--json"]
    if command == "run":
        arguments.extend(["--store", str(tmp_path / "forbidden-store")])
    elif command == "templates preview":
        arguments.extend(["--item-id", "001"])
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)
    assert body["error"]["category"] == "unsupported_capability"
    assert "generation.temperature" in body["error"]["message"]
    assert "generation.max_output_tokens" in body["error"]["message"]
    assert "Synthetic assessment input." not in result.output
    assert not (tmp_path / "forbidden-store").exists()
    with pytest.raises(LifecycleError, match="generation.temperature"):
        if command == "run":
            run_live(spec, store=tmp_path / "forbidden-store")
        elif command == "plan":
            estimate_run(spec)
        else:
            preview_prompt(spec, item_id="001")


def test_diagnostic_never_echoes_arbitrary_field_text() -> None:
    error = OpenAIConfigurationError(
        ("synthetic-private-canary", "generation.temperature")
    )
    assert "synthetic-private-canary" not in str(error)
    assert "configuration" in str(error)
    assert "generation.temperature" in str(error)
