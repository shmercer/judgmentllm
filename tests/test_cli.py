"""Deterministic CLI checks with no credentials or provider calls."""

import shutil
import subprocess
from pathlib import Path

import pytest

# Click exposes this helper here without declaring a typed re-export.
from click.utils import strip_ansi  # pyright: ignore[reportPrivateImportUsage]
from typer.testing import CliRunner

from judgmentllm import __version__
from judgmentllm.cli import app

runner = CliRunner()


def test_help_shows_supported_options() -> None:
    result = runner.invoke(app, ["--help"], color=False)
    assert result.exit_code == 0
    help_text = strip_ansi(result.output)
    assert "Usage:" in help_text
    assert "--help" in help_text
    assert "--version" in help_text
    assert "templates" in help_text


def test_version_matches_package() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output == f"judgmentllm {__version__}\n"


def test_default_invocation_prints_help() -> None:
    result = runner.invoke(app, [], color=False)
    assert result.exit_code == 0
    assert "Usage:" in result.output


@pytest.mark.parametrize("argument", ["run", "serve", "--unknown-option"])
def test_unsupported_arguments_fail(argument: str) -> None:
    result = runner.invoke(app, [argument], color=False)
    assert result.exit_code != 0


@pytest.mark.parametrize("option", ["--help", "--version"])
def test_installed_entry_point_runs_outside_checkout(
    option: str, tmp_path: Path
) -> None:
    executable = shutil.which("judgmentllm")
    assert executable is not None
    result = subprocess.run(
        [executable, option],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    expected = "Usage:" if option == "--help" else f"judgmentllm {__version__}"
    assert expected in result.stdout


def test_templates_list_and_initialize(tmp_path: Path) -> None:
    result = runner.invoke(app, ["templates", "list"])
    assert result.exit_code == 0
    assert "rubric_scores_only" in result.output
    output = tmp_path / "template.txt"
    command = [
        "templates",
        "init",
        "--preset",
        "rubric_scores_only",
        "--output",
        str(output),
    ]
    assert runner.invoke(app, command).exit_code == 0
    assert "{{ response }}" in output.read_text()
    assert runner.invoke(app, command).exit_code == 2
    assert (
        runner.invoke(
            app,
            [
                "templates",
                "init",
                "--preset",
                "unknown",
                "--output",
                str(tmp_path / "missing.txt"),
            ],
        ).exit_code
        == 2
    )


def test_cli_previews_and_errors() -> None:
    examples = Path(__file__).resolve().parents[1] / "examples"
    commands = [
        [
            "--config",
            str(examples / "run.yaml"),
            "--item-id",
            "essay_001",
            "--trait",
            "organization",
        ],
        [
            "--config",
            str(examples / "pairwise.yaml"),
            "--pair-uid",
            "comparison_001_reverse",
        ],
    ]
    for arguments in commands:
        result = runner.invoke(app, ["templates", "preview", *arguments])
        assert result.exit_code == 0, result.output
        assert "synthetic" in result.output.lower()
        assert "Warning [unknown_capability]" in result.output
    result = runner.invoke(
        app, ["templates", "preview", "--config", str(examples / "run.yaml")]
    )
    assert result.exit_code == 2
    assert "requires item_id" in result.output
    assert (
        runner.invoke(
            app, ["templates", "preview", "--config", "missing.yaml"]
        ).exit_code
        == 2
    )


def test_invalid_advanced_template_has_actionable_cli_error(tmp_path: Path) -> None:
    from judgmentllm.config import PromptSpec, RunSpec, load_run_spec

    spec = load_run_spec(Path(__file__).resolve().parents[1] / "examples" / "run.yaml")
    assert isinstance(spec, RunSpec)
    source = tmp_path / "invalid.j2"
    source.write_text(
        "{{ rubric }} {{ exemplars }} {{ response|upper(1) }} {{ output_instructions }}"
    )
    spec.prompt = PromptSpec(file=source, advanced=True)
    config = tmp_path / "run.json"
    config.write_text(spec.model_dump_json())
    result = runner.invoke(
        app,
        [
            "templates",
            "preview",
            "--config",
            str(config),
            "--item-id",
            "essay_001",
            "--trait",
            "organization",
        ],
    )
    assert result.exit_code == 2
    assert "Cannot render prompt template" in result.output
    assert "Traceback" not in result.output
