"""Synthetic credentials stay in memory and status cannot perform authentication."""

from __future__ import annotations

import builtins
import importlib.util
import inspect
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import webbrowser
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Literal
from uuid import uuid4

import pytest
from test_public_lifecycle import config
from typer.testing import CliRunner

import judgmentllm._auth as private_auth
import judgmentllm.auth as auth
from judgmentllm import (
    estimate_run,
    export_run_results,
    get_run,
    preview_prompt,
    resume_run,
    run_live,
    validate_inputs,
)
from judgmentllm.cli import app
from judgmentllm.config import RunSpecV2

KEY_VARIABLES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "TOGETHER_API_KEY",
    "TYPESAFE_API_KEY",
)


@pytest.fixture
def synthetic_credentials(monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """Replace every supported key; never inspect inherited credential values."""
    values = tuple(f"synthetic-{uuid4().hex}-éλ" for _ in KEY_VARIABLES)
    for variable, value in zip(KEY_VARIABLES, values, strict=True):
        monkeypatch.setenv(variable, value)
    return values


def assert_secret_free(content: str | bytes, values: tuple[str, ...]) -> None:
    """A failing privacy assertion must not echo either stream or the sentinel."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    for value in values:
        # Independently distinctive fragments detect partial-value leakage too.
        fragments = (value, value[10:26], value[26:42])
        for fragment in fragments:
            if fragment.encode("utf-8") in data:
                pytest.fail(
                    "Synthetic credential material escaped its private boundary"
                )


def assert_files_secret_free(root: Path, values: tuple[str, ...]) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            assert_secret_free(path.read_bytes(), values)
            assert_secret_free(str(path.relative_to(root)), values)


def test_public_private_and_cli_representations_exclude_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_credentials: tuple[str, ...],
) -> None:
    monkeypatch.chdir(tmp_path)
    statuses = auth.credential_status()
    representations = [repr(statuses)]
    for status in statuses:
        representations.extend(
            (repr(status), repr(status.model_dump()), status.model_dump_json())
        )
    for provider in (
        "openai",
        "anthropic",
        "gemini",
        "vertex",
        "together",
        "ollama",
        "typesafe",
    ):
        representations.append(repr(private_auth._resolve_credentials(provider)))  # pyright: ignore[reportPrivateUsage]
    for arguments in (
        ["auth", "status"],
        ["auth", "status", "--json"],
        ["auth", "status", "--require", "vertex", "--json"],
        ["auth", "status", "--require", "ollama,openai", "--json"],
    ):
        result = CliRunner().invoke(app, arguments)
        representations.extend((result.stdout, result.stderr, repr(result.exception)))
    # A safe, test-owned qualification record contains public metadata only.
    report = tmp_path / "qualification.json"
    report.write_text(
        json.dumps({"providers": [s.model_dump() for s in statuses]}), encoding="utf-8"
    )
    for representation in representations:
        assert_secret_free(representation, synthetic_credentials)
    assert_files_secret_free(tmp_path, synthetic_credentials)
    if set(tmp_path.iterdir()) != {report}:
        pytest.fail("Authentication status created an unexpected artifact")


def test_controlled_failures_never_embed_invalid_keys_or_provider_input(
    monkeypatch: pytest.MonkeyPatch, synthetic_credentials: tuple[str, ...]
) -> None:
    errors: list[str] = []
    with pytest.raises(ValueError) as selected:
        auth.credential_status(providers=(synthetic_credentials[0],))
    errors.extend((str(selected.value), repr(selected.value)))
    for suffix in (" ", "\t", "\r\n", "\x01"):
        monkeypatch.setenv("OPENAI_API_KEY", synthetic_credentials[0] + suffix)
        with pytest.raises(ValueError) as resolved:
            private_auth._resolve_credentials("openai")  # pyright: ignore[reportPrivateUsage]
        errors.extend((str(resolved.value), repr(resolved.value)))
        statuses = auth.credential_status(providers=("openai",))
        errors.extend((repr(statuses), statuses[0].model_dump_json()))
        for arguments in (
            ["auth", "status", "--require", "openai"],
            ["auth", "status", "--require", "openai", "--json"],
        ):
            result = CliRunner().invoke(app, arguments)
            if result.exit_code != 2:
                pytest.fail("Malformed required credential did not fail safely")
            errors.extend((result.stdout, result.stderr, repr(result.exception)))
    for error in errors:
        assert_secret_free(error, synthetic_credentials)


def test_status_isolated_from_io_discovery_sdk_and_runtime_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_credentials: tuple[str, ...],
) -> None:
    monkeypatch.chdir(tmp_path)
    adc = tmp_path / "inaccessible-adc.json"
    adc.write_text("Synthetic unreadable credential fixture", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "Do not load this synthetic fixture", encoding="utf-8"
    )
    gcloud = tmp_path / "gcloud"
    gcloud.mkdir()
    (gcloud / "application_default_credentials.json").write_text("{}", encoding="utf-8")
    before_files = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")}
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc))
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "gcloud"))
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "synthetic-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "synthetic-location")
    monkeypatch.setenv("JUDGMENTLLM_RUN_HOME", str(tmp_path / "forbidden-store"))
    # Prepare help rendering only; the first status execution stays guarded.
    runner = CliRunner()
    runner.invoke(app, ["auth", "status", "--help"])
    previous_environment = dict(os.environ)
    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> None:
        calls.append("forbidden")
        raise AssertionError("Offline status crossed an authentication or I/O boundary")

    original_import = builtins.__import__

    def import_guard(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] | None = (),
        level: int = 0,
    ) -> ModuleType:
        if name.split(".")[0] in {
            "openai",
            "anthropic",
            "google",
            "together",
            "ollama",
            "typesafe",
            "dotenv",
        }:
            forbidden()
        return original_import(name, globals, locals, fromlist, level)

    with monkeypatch.context() as guard:
        # The guard is scoped to synchronous status calls: no event-loop/Windows
        # local IPC setup is patched while the test runner is operating.
        for owner, names in (
            (
                socket,
                (
                    "create_connection",
                    "getaddrinfo",
                    "gethostbyname",
                    "gethostbyname_ex",
                ),
            ),
            (socket.socket, ("connect", "connect_ex", "sendto")),
            (builtins, ("open",)),
            (io, ("open",)),
            (os, ("open", "mkdir", "makedirs", "scandir", "listdir")),
            (
                Path,
                (
                    "open",
                    "read_text",
                    "read_bytes",
                    "mkdir",
                    "iterdir",
                    "glob",
                    "rglob",
                ),
            ),
            (sqlite3, ("connect",)),
            (subprocess, ("Popen",)),
            (webbrowser, ("open", "open_new", "open_new_tab")),
        ):
            for name in names:
                guard.setattr(owner, name, forbidden)
        guard.setattr(builtins, "__import__", import_guard)
        statuses = auth.credential_status()
        vertex = auth.credential_status(providers=("vertex",))[0]
        result = runner.invoke(app, ["auth", "status", "--json"])
    if calls or result.exit_code != 0:
        pytest.fail("Offline status attempted a prohibited operation")
    assert len(statuses) == 7 and vertex.status == "unknown"
    if previous_environment != dict(os.environ):
        pytest.fail("Offline status changed the process environment")
    if {
        str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")
    } != before_files:
        pytest.fail("Offline status created a file, runtime store, or cache")
    assert_secret_free(result.stdout + result.stderr, synthetic_credentials)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_mock_lifecycle_and_all_exports_never_resolve_or_persist_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_credentials: tuple[str, ...],
    version: int,
    capture: Literal["full", "minimal"],
) -> None:
    spec, path = config(tmp_path, capture=capture, grouped=True)
    if version == 2:
        spec2 = RunSpecV2.model_validate({**spec.model_dump(), "schema_version": 2})
        path.write_text(spec2.model_dump_json(), encoding="utf-8")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "An ordinary offline lifecycle operation resolved credentials"
        )

    monkeypatch.setattr(auth, "credential_status", forbidden)
    monkeypatch.setattr(private_auth, "_resolve_credentials", forbidden)
    monkeypatch.setattr(private_auth, "_inspect_credentials", forbidden)
    monkeypatch.setattr(auth, "_inspect_credentials", forbidden)
    assert validate_inputs(path).valid
    assert estimate_run(path).logical_tasks == 6
    preview_prompt(path, item_id="000", trait_id="000")
    run = run_live(path)
    assert run.outcome == "ok"
    store = spec.output_path
    assert get_run(run.run_id, store=store).run_status == "completed"
    assert resume_run(run.run_id, store=store).run_status == "completed"
    formats: tuple[tuple[Literal["jsonl", "csv"], Literal["canonical", "r"]], ...] = (
        ("jsonl", "canonical"),
        ("csv", "canonical"),
        ("csv", "r"),
    )
    for format, view in formats:
        exported = export_run_results(run.run_id, store=store, format=format, view=view)
        assert exported.row_count == 6
    assert_files_secret_free(tmp_path, synthetic_credentials)


def test_r_gate_excludes_inherited_authentication_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    synthetic_credentials: tuple[str, ...],
) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts/cli_lifecycle_r_gate.py"
    specification = importlib.util.spec_from_file_location(
        "auth_privacy_r_gate", script
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    monkeypatch.setenv("openai_api_key", synthetic_credentials[0])
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", synthetic_credentials[1])
    monkeypatch.setenv("CLOUDSDK_CONFIG", synthetic_credentials[2])
    seen: list[bool] = []

    def controlled_run(
        *_args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        environment = kwargs["env"]
        if not isinstance(environment, dict):
            pytest.fail("R gate omitted its controlled environment")
        serialized = json.dumps(environment)
        assert_secret_free(serialized, synthetic_credentials)
        if any(name in environment for name in KEY_VARIABLES):
            pytest.fail("R gate inherited a registered authentication variable")
        seen.append(True)
        return subprocess.CompletedProcess([], 0)

    def synthetic_rscript(_name: str) -> str:
        return "synthetic-Rscript"

    monkeypatch.setattr(module.shutil, "which", synthetic_rscript)
    monkeypatch.setattr(module.subprocess, "run", controlled_run)
    monkeypatch.setattr(sys, "argv", [str(script), "--output", str(tmp_path / "gate")])
    module.main()
    assert seen == [True]


def _fresh_status_probe() -> None:
    """Run in a fresh interpreter with SDK boundaries guarded before import."""
    import builtins
    import io
    import json
    import os
    import socket
    import sqlite3
    import subprocess
    import sys
    import webbrowser
    from pathlib import Path
    from types import ModuleType
    from typing import Any, NoReturn, cast

    calls: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> NoReturn:
        calls.append("prohibited")
        raise AssertionError("Offline authentication crossed a prohibited boundary")

    # SDK objects are registered before JudgmentLLM imports: cached constructor,
    # ADC/default and refresh references therefore remain instrumented too.
    modules: dict[str, ModuleType] = {}

    def fake_module(name: str) -> ModuleType:
        if name in modules:
            return modules[name]
        value = ModuleType(name)
        value.__path__ = []
        modules[name] = value
        sys.modules[name] = value
        parent, _, child = name.rpartition(".")
        if parent:
            setattr(fake_module(parent), child, value)
        return value

    for name, attributes in {
        "openai": ("OpenAI", "AsyncOpenAI", "Client", "AsyncClient"),
        "anthropic": ("Anthropic", "AsyncAnthropic", "AnthropicVertex", "Client"),
        "together": ("Together", "AsyncTogether"),
        "google.genai": ("Client",),
        "ollama": ("Client", "AsyncClient", "list", "show", "ps", "chat", "generate"),
        "typesafe": ("Client", "AsyncClient", "TypeSafe", "Typesafe"),
        "typesafe_sdk": ("Client", "AsyncClient", "TypeSafe", "Typesafe"),
        "dotenv": ("load_dotenv", "find_dotenv", "dotenv_values"),
        "google.auth": (
            "default",
            "load_credentials_from_file",
            "load_credentials_from_dict",
        ),
        "google.auth._default": ("default", "load_credentials_from_file"),
        "google.auth.transport.requests": ("Request",),
    }.items():
        module = fake_module(name)
        for attribute in attributes:
            setattr(module, attribute, forbidden)
    blocked_credentials = type(
        "GuardedCredentials",
        (),
        {
            "__init__": forbidden,
            "refresh": forbidden,
            "before_request": forbidden,
            "from_service_account_file": forbidden,
            "from_authorized_user_file": forbidden,
        },
    )
    for name in (
        "google.auth.credentials",
        "google.oauth2.credentials",
        "google.oauth2.service_account",
        "google.auth.compute_engine",
        "google.auth.impersonated_credentials",
        "google.auth.external_account",
    ):
        fake_module(name).__dict__["Credentials"] = blocked_credentials

    # During legitimate Python imports only credential-file reads are denied.
    # Status itself later receives a full file-I/O guard, without interfering
    # with runner imports, Windows event-loop setup, or local IPC.
    original_open, original_io_open = builtins.open, io.open

    def file_guard(original: Any, path: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(path, (str, bytes, os.PathLike)):
            text = (
                os.fsdecode(cast("str | bytes | os.PathLike[str]", path))
                .replace("\\", "/")
                .casefold()
            )
            if (
                Path(text).name
                in {
                    ".env",
                    "inaccessible-adc.json",
                    "application_default_credentials.json",
                }
                or "/gcloud/" in text
            ):
                forbidden()
        return original(path, *args, **kwargs)

    def guarded_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        return file_guard(original_open, path, *args, **kwargs)

    def guarded_io_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        return file_guard(original_io_open, path, *args, **kwargs)

    builtins.open = guarded_open
    io.open = guarded_io_open
    for owner, attributes in (
        (
            socket,
            ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex"),
        ),
        (socket.socket, ("connect", "connect_ex", "sendto")),
        (sqlite3, ("connect",)),
        (subprocess, ("Popen",)),
        (webbrowser, ("open", "open_new", "open_new_tab")),
    ):
        for attribute in attributes:
            setattr(owner, attribute, forbidden)

    try:
        from typer.testing import CliRunner

        from judgmentllm.auth import credential_status
        from judgmentllm.cli import app

        runner = CliRunner()
        help_result = runner.invoke(app, ["auth", "status", "--help"])
        if help_result.exit_code:
            raise AssertionError("Guarded help failed")
        for owner, attributes in (
            (builtins, ("open",)),
            (io, ("open",)),
            (os, ("open", "mkdir", "makedirs", "scandir", "listdir")),
            (
                Path,
                (
                    "open",
                    "read_text",
                    "read_bytes",
                    "mkdir",
                    "iterdir",
                    "glob",
                    "rglob",
                ),
            ),
        ):
            for attribute in attributes:
                setattr(owner, attribute, forbidden)
        statuses = credential_status()
        result = runner.invoke(app, ["auth", "status", "--json"])
        if result.exit_code or calls or len(statuses) != 7:
            raise AssertionError(
                "First offline status attempted a prohibited operation"
            )
        value = json.loads(result.stdout)
        if value["command"] != "auth status" or len(value["data"]["providers"]) != 7:
            raise AssertionError("Guarded first status returned an invalid envelope")
    except BaseException:
        # Never echo streams, SDK exception objects, or process-environment values.
        raise SystemExit(1) from None
    print("guarded fresh status passed")


def test_first_status_import_and_invocation_are_guarded_in_fresh_process(
    tmp_path: Path,
    synthetic_credentials: tuple[str, ...],
) -> None:
    source = inspect.getsource(_fresh_status_probe)
    result = subprocess.run(
        [sys.executable, "-B", "-I", "-c", source + "\n_fresh_status_probe()\n"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert_secret_free(result.stdout + result.stderr, synthetic_credentials)
    if result.returncode or result.stdout.strip() != "guarded fresh status passed":
        pytest.fail("Fresh offline authentication crossed a guarded boundary")
    if list(tmp_path.iterdir()):
        pytest.fail("Fresh offline authentication created a runtime artifact")
