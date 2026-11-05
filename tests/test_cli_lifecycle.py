"""CLI controls preserve one safe stdout object and separate stderr diagnostics."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Any, cast

import pytest
from test_preparation_rendering import specification
from typer.testing import CliRunner

from judgmentllm import cli
from judgmentllm.cli_controls import ControlEnvelope, LifecycleGroup
from judgmentllm.config import RunSpec
from judgmentllm.lifecycle import LifecycleError, ProgressEvent
from judgmentllm.persistence.repository import Repository

runner = CliRunner()


def configuration(directory: Path, *, grouped: bool = False) -> Path:
    spec = specification(directory / "inputs", responses=1, traits=3 if grouped else 1)
    spec.provider, spec.endpoint = "mock", "synthetic"
    spec.output_path = directory / "store"
    if grouped:
        spec.execution.grouping = "grouped"
    path = directory / "run.json"
    path.write_text(
        RunSpec.model_validate(spec.model_dump()).model_dump_json(), encoding="utf-8"
    )
    return path


def control(arguments: list[str], expected: int = 0) -> tuple[dict[str, Any], str]:
    result = runner.invoke(cli.app, [*arguments, "--json"], color=False)
    assert result.exit_code == expected, (
        result.stdout,
        result.stderr,
        result.exception,
    )
    decoded, end = json.JSONDecoder().raw_decode(result.stdout)
    assert result.stdout[end:] == "\n"
    envelope = cast(dict[str, Any], decoded)
    assert set(envelope) == {
        "schema_version",
        "command",
        "status",
        "run_id",
        "run_status",
        "artifacts",
        "data",
        "error",
    }
    ControlEnvelope.model_validate(envelope)
    for line in result.stderr.splitlines():
        record = json.loads(line)
        assert record["schema_version"] == 1
        assert record["event"] in {"diagnostic", "progress"}
        assert isinstance(record["category"], str)
        assert isinstance(record["message"], str)
        assert "run_id" in record
    assert "Traceback" not in result.stdout + result.stderr
    return envelope, result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["validate"],
        ["plan"],
        ["run"],
        ["resume"],
        ["runs", "status"],
        ["results", "export"],
        ["run", "--config", "unused", "--unknown-option", "SOURCE-SECRET"],
        ["runs", "list", "--limit", "invalid-SECRET"],
        ["runs", "list", "--limit", "0"],
        ["runs", "list", "--limit", "1001"],
        ["results", "export", "--run", "unused", "--format", "invalid-SECRET"],
        ["results", "export", "--run", "unused", "--view", "invalid-SECRET"],
        ["results", "export", "--run", "unused", "--high-water-mark", "-1"],
        ["runs", "unknown-SECRET"],
        ["unknown-SECRET"],
    ],
)
def test_machine_parsing_errors_are_one_safe_control_object(
    arguments: list[str],
) -> None:
    envelope, stderr = control(arguments, 2)
    assert envelope["status"] == "error"
    assert envelope["error"]["category"] == "usage"
    assert "SECRET" not in json.dumps(envelope) + stderr


@pytest.mark.parametrize(
    "command",
    [
        ["validate"],
        ["plan"],
        ["run"],
        ["resume"],
        ["runs"],
        ["runs", "status"],
        ["runs", "list"],
        ["results", "export"],
    ],
)
def test_machine_help_is_control_output_without_executing(command: list[str]) -> None:
    envelope, stderr = control([*command, "--help"])
    assert envelope["status"] == "ok"
    assert "Usage:" in envelope["data"]["help"]
    assert stderr == ""


def test_validation_planning_and_execution_use_shared_services(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    validated, stderr = control(["validate", "--config", str(config)])
    assert validated["data"]["valid"]
    assert validated["run_id"] is None
    assert stderr == ""
    planned, _ = control(["plan", "--config", str(config)])
    assert planned["data"]["logical_tasks"] == 1
    executed, stderr = control(["run", "--config", str(config)])
    assert executed["run_status"] == "completed"
    assert executed["data"]["completed_tasks"] == 1
    assert executed["artifacts"]["results_jsonl"] is None
    assert {json.loads(line)["category"] for line in stderr.splitlines()} >= {
        "started",
        "prepared",
        "finished",
    }
    assert all(
        Path(value).is_absolute()
        for value in executed["artifacts"].values()
        if value is not None
    )
    run_id = executed["run_id"]
    status, _ = control(
        ["runs", "status", "--run", run_id, "--store", str(tmp_path / "store")]
    )
    assert status["status"] == "ok"
    assert status["run_id"] == run_id
    resumed, _ = control(
        ["resume", "--run", run_id, "--store", str(tmp_path / "store")]
    )
    assert resumed["data"]["attempts"] == 1
    listed, _ = control(
        ["runs", "list", "--store", str(tmp_path / "store"), "--limit", "1"]
    )
    assert listed["data"]["runs"][0]["run_id"] == run_id
    exported, _ = control(
        ["results", "export", "--run", run_id, "--store", str(tmp_path / "store")]
    )
    assert exported["data"]["row_count"] == 1
    assert Path(exported["artifacts"]["results_jsonl"]).is_file()


@pytest.mark.parametrize(
    "task_args",
    [
        ["--retry-task", "task"],
        ["--retry-id", "operation"],
        ["--allow-completed"],
        ["--retry-id", "operation", "--allow-completed"],
    ],
)
def test_retry_controls_require_selection_and_operation_id(
    task_args: list[str],
) -> None:
    envelope, _ = control(["resume", "--run", "unused", *task_args], 2)
    assert envelope["error"]["category"] == "usage"
    assert "--retry" in envelope["error"]["message"]


def test_partial_processing_and_explicit_retry_preserve_history(tmp_path: Path) -> None:
    config = configuration(tmp_path, grouped=True)
    fixture = tmp_path / "mock.json"
    fixture.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenario": {"members": {"002": {"outcome": "invalid_category"}}},
            }
        ),
        encoding="utf-8",
    )
    result, _ = control(
        ["run", "--config", str(config), "--mock-fixture", str(fixture)], 3
    )
    run_id = result["run_id"]
    store = tmp_path / "store"
    assert result["run_status"] == "partial"
    assert result["error"]["category"] == "partial_completion"
    assert result["data"]["completed_tasks"] == 2
    assert result["data"]["unsuccessful_results"] == 1
    status, _ = control(["runs", "status", "--run", run_id, "--store", str(store)])
    assert status["run_status"] == "partial"
    with Repository(store) as repository:
        failed = next(
            row.task_id
            for row in repository.iter_results(run_id)
            if row.status == "invalid"
        )
    retry_command = [
        "resume",
        "--run",
        run_id,
        "--store",
        str(store),
        "--retry-task",
        failed,
        "--retry-id",
        "retry-001",
    ]
    resumed, _ = control(retry_command)
    assert resumed["run_status"] == "completed"
    assert resumed["data"]["successful_results"] == 3
    assert resumed["data"]["unsuccessful_results"] == 1
    repeated, _ = control(retry_command)
    assert repeated["data"]["attempts"] == 2
    export, _ = control(
        [
            "results",
            "export",
            "--run",
            run_id,
            "--store",
            str(store),
            "--format",
            "csv",
            "--view",
            "r",
        ]
    )
    assert export["data"]["row_count"] == 4
    assert Path(export["artifacts"]["results_r_csv"]).is_file()


def test_invalid_input_and_missing_resource_errors_are_safe(tmp_path: Path) -> None:
    config = tmp_path / "invalid.json"
    config.write_text('{"text":"SOURCE-SECRET"}', encoding="utf-8")
    invalid, stderr = control(["validate", "--config", str(config)], 2)
    assert invalid["data"]["valid"] is False
    assert "SOURCE-SECRET" not in json.dumps(invalid) + stderr
    missing, _ = control(
        ["runs", "status", "--run", "missing", "--store", str(tmp_path / "absent")], 2
    )
    assert missing["error"]["category"] == "not_found"
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize(
    "error",
    [RuntimeError("RAW-SECRET"), OSError("RAW-SECRET"), ValueError("RAW-SECRET")],
)
def test_unexpected_and_local_errors_never_serialize_exception_input(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_service(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(cli, "estimate_run", fail_service)
    envelope, stderr = control(
        ["plan", "--config", "unused"], 2 if isinstance(error, ValueError) else 1
    )
    assert "RAW-SECRET" not in json.dumps(envelope) + stderr


def test_typed_service_error_preserves_safe_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_service(*_args: object, **_kwargs: object) -> None:
        raise LifecycleError("execution", "Execution is unavailable.", retryable=True)

    monkeypatch.setattr(cli, "run_live", fail_service)
    envelope, _ = control(["run", "--config", "unused"], 1)
    assert envelope["error"] == {
        "category": "execution",
        "message": "Execution is unavailable.",
        "retryable": True,
    }


def test_interrupted_operation_emits_130_and_observed_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*_args: object, **_kwargs: object) -> None:
        cli.progress(
            ProgressEvent(
                stage="started", run_id="run-interrupted", store=str(tmp_path)
            )
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_live", interrupted)
    envelope, _ = control(["run", "--config", "unused"], 130)
    assert envelope["run_id"] == "run-interrupted"
    assert envelope["error"]["category"] == "interrupted"


def test_nonmock_configuration_never_executes_with_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configuration(tmp_path)
    document = json.loads(config.read_text())
    document.update(
        provider="unimplemented-provider", endpoint="unimplemented-endpoint"
    )
    config.write_text(json.dumps(document), encoding="utf-8")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("A non-mock configuration selected MockAdapter")

    monkeypatch.setattr("judgmentllm.providers.mock.MockAdapter.__init__", forbidden)
    envelope, _ = control(["run", "--config", str(config)], 2)
    assert envelope["error"]["category"] == "unsupported_capability"
    assert not (tmp_path / "store" / "mock-service.sqlite3").exists()


def test_human_mode_and_argument_errors_remain_readable(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    result = runner.invoke(cli.app, ["validate", "--config", str(config)], color=False)
    assert result.exit_code == 0
    assert "validate: ok" in result.stdout
    invalid = runner.invoke(cli.app, ["run"], color=False)
    assert invalid.exit_code == 2
    assert "--help" in invalid.stderr


def test_interruption_during_argument_parsing_keeps_stderr_jsonl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(LifecycleGroup, "make_context", interrupted)
    envelope, stderr = control(["run", "--config", "unused"], 130)
    assert envelope["error"]["category"] == "interrupted"
    assert len(stderr.splitlines()) == 1


def test_mixed_scale_library_warnings_keep_stderr_structured(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    document = json.loads(config.read_text())
    document["rubric"]["traits"][0]["trait_id"] = "PRIVATE-TRAIT-ID"
    document["rubric"]["traits"][0]["levels"] = [{"label": "5"}, {"label": "Advanced"}]
    config.write_text(json.dumps(document), encoding="utf-8")
    envelope, stderr = control(["validate", "--config", str(config)])
    assert envelope["data"]["valid"]
    assert "PRIVATE-TRAIT-ID" not in stderr
    records = [json.loads(line) for line in stderr.splitlines()]
    assert [row["category"] for row in records] == ["validation_warning"]


def test_warning_translation_preserves_warning_as_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def operation(*_args: object, **_kwargs: object) -> None:
        warnings.warn("RAW-SECRET", ResourceWarning, stacklevel=1)

    monkeypatch.setattr(cli, "estimate_run", operation)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ResourceWarning)
        envelope, stderr = control(["plan", "--config", "unused"], 1)
    assert envelope["error"]["category"] == "internal"
    assert "RAW-SECRET" not in json.dumps(envelope) + stderr


def test_installed_json_controls_survive_ascii_redirected_streams(
    tmp_path: Path,
) -> None:
    executable = shutil.which("judgmentllm")
    assert executable is not None
    directory = tmp_path / "résumé-模型"
    config = configuration(directory)
    model = "synthetic-résumé-模型"
    document = json.loads(config.read_text(encoding="utf-8"))
    document["model"] = model
    config.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    environment = {**os.environ, "PYTHONIOENCODING": "ascii"}

    def invoke(arguments: list[str]) -> dict[str, Any]:
        result = subprocess.run(
            [executable, *arguments, "--json"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert result.stdout.isascii()
        assert result.stderr.isascii()
        stdout = result.stdout.decode("ascii")
        data, end = json.JSONDecoder().raw_decode(stdout)
        assert stdout[end:] == "\n" or stdout[end:] == "\r\n"
        envelope = cast(dict[str, Any], data)
        ControlEnvelope.model_validate(envelope)
        for line in result.stderr.decode("ascii").splitlines():
            diagnostic = json.loads(line)
            assert diagnostic["schema_version"] == 1
            assert diagnostic["event"] in {"progress", "diagnostic"}
        return envelope

    started = invoke(["run", "--config", str(config)])
    assert started["data"]["model"] == model
    assert started["artifacts"]["store"] == str(directory / "store")
    inspected = invoke(
        [
            "runs",
            "status",
            "--run",
            started["run_id"],
            "--store",
            str(directory / "store"),
        ]
    )
    assert inspected["data"]["model"] == model
    assert inspected["run_id"] == started["run_id"]
    assert inspected["artifacts"]["store"] == str(directory / "store")
