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
    assert "Assessment commands are not implemented yet" in help_text


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
