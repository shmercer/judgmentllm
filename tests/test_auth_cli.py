"""Offline authentication uses the established safe CLI control boundary."""

from __future__ import annotations

import json
import warnings
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

import pytest
import typer
from typer.testing import CliRunner

from judgmentllm import cli
from judgmentllm.auth import CredentialStatus
from judgmentllm.cli_controls import ControlEnvelope, LifecycleGroup

PROVIDERS = (
    "openai",
    "anthropic",
    "gemini",
    "vertex",
    "together",
    "ollama",
    "typesafe",
)
KEY_NAMES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "TOGETHER_API_KEY",
    "TYPESAFE_API_KEY",
)
runner = CliRunner()
_Parameters = ParamSpec("_Parameters")
_Return = TypeVar("_Return")


def private_failure(
    operation: Callable[_Parameters, _Return],
) -> Callable[_Parameters, _Return]:
    """Keep nested parser/process errors and credential locals out of reports."""

    @wraps(operation)
    def guarded(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> _Return:
        __tracebackhide__ = True
        try:
            # Raw warnings can otherwise bypass fixed failures through pytest's
            # warning summary. Auth commands translate expected warnings safely.
            with warnings.catch_warnings(record=True) as observed:
                result = operation(*args, **kwargs)
            if not observed:
                return result
        except (Exception, pytest.fail.Exception):
            pass
        pytest.fail(
            "Offline authentication CLI check failed; private details suppressed.",
            pytrace=False,
        )

    return guarded


def exclude_secrets(text: str, values: tuple[str, ...]) -> None:
    """Fail with fixed text, never pytest's secret-bearing expression details."""
    __tracebackhide__ = True
    fragments = tuple(
        part for value in values for part in (value, value[:12], value[-12:])
    )
    if any(fragment in text for fragment in fragments):
        pytest.fail(
            "Synthetic credential material escaped its private boundary.", pytrace=False
        )


@pytest.fixture(autouse=True)
def isolated_auth_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in KEY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JUDGMENTLLM_RUN_HOME", str(tmp_path / "unused-store"))
    monkeypatch.chdir(tmp_path)


def control(arguments: list[str], code: int = 0) -> tuple[dict[str, Any], str]:
    __tracebackhide__ = True
    result = runner.invoke(
        cli.app,
        ["auth", *arguments, *([] if "--json" in arguments else ["--json"])],
        color=False,
    )
    if result.exit_code != code:
        pytest.fail(
            "Authentication command returned an unexpected exit code.", pytrace=False
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
    assert envelope["schema_version"] == 1
    assert envelope["run_id"] is None and envelope["run_status"] is None
    assert envelope["artifacts"] == {}
    assert "Traceback" not in result.stdout + result.stderr
    for line in result.stderr.splitlines():
        diagnostic = json.loads(line)
        assert diagnostic["event"] == "diagnostic"
        assert diagnostic["schema_version"] == 1
        assert diagnostic["run_id"] is None
    return envelope, result.stderr


def report(envelope: dict[str, Any]) -> dict[str, Any]:
    __tracebackhide__ = True
    assert envelope["command"] == "auth status"
    data = cast(dict[str, Any], envelope["data"])
    assert set(data) == {"providers", "required_providers", "requirements_met"}
    assert [row["provider"] for row in data["providers"]] == list(PROVIDERS)
    for row in data["providers"]:
        assert set(row) == {
            "provider",
            "auth_type",
            "source",
            "status",
            "diagnostic_code",
        }
    return data


@private_failure
def test_plain_status_table_has_every_provider_without_creating_files(
    tmp_path: Path,
) -> None:
    before = set(tmp_path.rglob("*"))
    result = runner.invoke(cli.app, ["auth", "status"], color=False)
    assert result.exit_code == 0
    assert result.stderr == ""
    rows = result.stdout.splitlines()
    assert rows[0].split() == ["Provider", "Credential", "source", "Status"]
    assert [row.split()[0] for row in rows[1:]] == list(PROVIDERS)
    assert "unknown" in rows[4] and "google_adc" in rows[4]
    assert "not_required" in rows[6] and "local_service" in rows[6]
    assert set(tmp_path.rglob("*")) == before


@private_failure
def test_json_status_all_optional_missing_is_success(tmp_path: Path) -> None:
    before = set(tmp_path.rglob("*"))
    envelope, stderr = control(["status"])
    data = report(envelope)
    assert data["required_providers"] == [] and data["requirements_met"] is True
    assert envelope["status"] == "ok" and envelope["error"] is None
    assert [row["status"] for row in data["providers"]] == [
        "missing",
        "missing",
        "missing",
        "unknown",
        "missing",
        "not_required",
        "missing",
    ]
    assert stderr == "" and set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    ("requirements", "configured", "expected", "ordered"),
    [
        (["openai"], ("OPENAI_API_KEY",), 0, ["openai"]),
        (["openai"], (), 2, ["openai"]),
        (
            ["anthropic,openai"],
            ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
            0,
            ["openai", "anthropic"],
        ),
        (
            ["anthropic,openai", "gemini"],
            ("OPENAI_API_KEY",),
            2,
            ["openai", "anthropic", "gemini"],
        ),
        (
            ["gemini", "openai,gemini", "openai"],
            ("GOOGLE_API_KEY", "OPENAI_API_KEY"),
            0,
            ["openai", "gemini"],
        ),
        (["vertex"], (), 2, ["vertex"]),
        (["ollama"], (), 0, ["ollama"]),
        (["ollama,typesafe"], ("TYPESAFE_API_KEY",), 0, ["ollama", "typesafe"]),
        (
            ["typesafe,together,gemini,anthropic,openai,ollama"],
            KEY_NAMES,
            0,
            ["openai", "anthropic", "gemini", "together", "ollama", "typesafe"],
        ),
    ],
)
@private_failure
def test_requirements_preserve_complete_rows_and_registry_order(
    monkeypatch: pytest.MonkeyPatch,
    requirements: list[str],
    configured: tuple[str, ...],
    expected: int,
    ordered: list[str],
) -> None:
    for index, name in enumerate(configured):
        monkeypatch.setenv(name, f"synthetic-credential-{index}-opaque")
    envelope, stderr = control(
        [
            "status",
            *(token for value in requirements for token in ("--require", value)),
        ],
        expected,
    )
    data = report(envelope)
    assert data["required_providers"] == ordered
    assert data["requirements_met"] is (expected == 0)
    assert envelope["status"] == ("ok" if expected == 0 else "error")
    if expected:
        assert envelope["error"]["category"] == "validation"
        assert envelope["error"]["retryable"] is False
        assert stderr
    else:
        assert envelope["error"] is None and stderr == ""


@private_failure
def test_vertex_requirement_stays_unknown_despite_google_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.setenv(name, "unused-synthetic-setting")
    envelope, _ = control(["status", "--require", "vertex"], 2)
    vertex = report(envelope)["providers"][3]
    assert vertex == {
        "provider": "vertex",
        "auth_type": "application_default_credentials",
        "source": "google_adc",
        "status": "unknown",
        "diagnostic_code": "offline_identity_unknown",
    }


@pytest.mark.parametrize(
    "selection",
    [
        "unknown",
        "",
        " ",
        "openai,",
        ",openai",
        "openai,,gemini",
        "OpenAI",
        "openai anthropic",
        " openai",
        "openai ",
        "../openai",
    ],
)
@private_failure
def test_invalid_requirements_are_safe_validation_errors(selection: str) -> None:
    envelope, _ = control(["status", "--require", selection], 2)
    assert envelope["error"]["category"] == "validation"
    assert envelope["status"] == "error" and envelope["data"] == {}


@pytest.mark.parametrize(
    "arguments",
    [
        ["status", "--unknown", "PRIVATE-ARGUMENT"],
        ["status", "--json", "--require"],
        ["unknown-private"],
    ],
)
@private_failure
def test_parser_errors_remain_one_safe_object(arguments: list[str]) -> None:
    envelope, stderr = control(arguments, 2)
    assert envelope["error"]["category"] == "usage"
    assert "PRIVATE-ARGUMENT" not in json.dumps(envelope) + stderr
    assert "unknown-private" not in json.dumps(envelope) + stderr


@pytest.mark.parametrize(
    "arguments", [["--help"], ["status", "--help"], ["status", "--json", "--help"]]
)
@private_failure
def test_json_help_does_not_inspect_credentials(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    def forbidden() -> None:
        pytest.fail("Help must not inspect credentials.")

    monkeypatch.setattr(cli, "credential_status", forbidden)
    envelope, stderr = control(arguments)
    assert envelope["status"] == "ok"
    assert "Usage:" in envelope["data"]["help"] and stderr == ""


@private_failure
def test_plain_unmet_requirements_keep_table() -> None:
    result = runner.invoke(cli.app, ["auth", "status", "--require", "openai"])
    assert result.exit_code == 2
    assert len(result.stdout.splitlines()) == 8
    assert "required providers" in result.stderr


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, typer.Abort])
@pytest.mark.parametrize("parsing", [False, True])
@pytest.mark.parametrize("machine", [False, True])
@private_failure
def test_handled_interruption_is_safe_and_returns_130(
    monkeypatch: pytest.MonkeyPatch,
    parsing: bool,
    machine: bool,
    interruption: type[BaseException],
) -> None:
    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise interruption

    if parsing:
        monkeypatch.setattr(LifecycleGroup, "make_context", interrupted)
    else:
        monkeypatch.setattr(cli, "credential_status", interrupted)
    if machine:
        envelope, stderr = control(["status"], 130)
        assert envelope["error"]["category"] == "interrupted"
        assert len(stderr.splitlines()) == 1
    else:
        result = runner.invoke(cli.app, ["auth", "status"])
        assert result.exit_code == 130
        assert "interrupted" in result.stderr
        assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize("error_type", [ValueError, OSError, RuntimeError])
@pytest.mark.parametrize("machine", [False, True])
@private_failure
def test_exception_values_cannot_enter_outputs(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    machine: bool,
) -> None:
    secret = "sentinelCLIexception左側-0123456789-private-rightEDGE"

    def operation() -> None:
        raise error_type(secret)

    monkeypatch.setattr(cli, "credential_status", operation)
    result = runner.invoke(
        cli.app, ["auth", "status", *(["--json"] if machine else [])]
    )
    assert result.exit_code == (2 if error_type is ValueError else 1)
    exclude_secrets(result.stdout + result.stderr, (secret,))
    if machine:
        assert json.loads(result.stdout)["status"] == "error"


@private_failure
def test_credential_values_never_enter_plain_json_or_warning_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = tuple(
        f"leftBOUND{index:04d}秘密-synthetic-rightBOUND{index:04d}"
        for index in range(len(KEY_NAMES))
    )
    for name, value in zip(KEY_NAMES, values, strict=True):
        monkeypatch.setenv(name, value)
    for arguments in (
        ["auth", "status"],
        ["auth", "status", "--json"],
        ["auth", "status", "--require", "vertex", "--json"],
    ):
        result = runner.invoke(cli.app, arguments)
        exclude_secrets(result.stdout + result.stderr, values)
    original = cli.credential_status

    def warned() -> tuple[CredentialStatus, ...]:
        warnings.warn(values[0], UserWarning, stacklevel=1)
        return original()

    monkeypatch.setattr(cli, "credential_status", warned)
    result = runner.invoke(cli.app, ["auth", "status", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stderr)["category"] == "validation_warning"
    exclude_secrets(result.stdout + result.stderr, values)


@private_failure
def test_abort_in_existing_lifecycle_service_retains_error_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def aborted(*_args: object, **_kwargs: object) -> None:
        raise typer.Abort

    monkeypatch.setattr(cli, "estimate_run", aborted)
    result = runner.invoke(cli.app, ["plan", "--config", "unused", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["category"] == "internal"
