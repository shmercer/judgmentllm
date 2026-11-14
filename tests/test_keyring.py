"""Synthetic keyring outputs only: never contact the desktop Secret Service."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import os
import runpy
import signal
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from judgmentllm import keyring
from judgmentllm._auth import VertexAuthMode, _resolve_credentials

KEY = "SYNTHETIC_KEYRING_SENTINEL"
ITEM = b"([objectpath '/org/freedesktop/secrets/collection/login/1'], @ao [])\n"


def test_launcher_has_no_public_console_entry_point() -> None:
    assert not metadata.entry_points(
        group="console_scripts", name="judgmentllm-keyring"
    )


def test_module_help_never_resolves_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["judgmentllm.keyring", "--help"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(keyring.__file__), run_name="__main__")
    assert stopped.value.code == 0


@pytest.fixture(autouse=True)
def synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    for name in (*keyring._KEY_VARIABLES, "JUDGMENTLLM_VERTEX_AUTH_MODE"):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("A test attempted real Secret Service access")

    monkeypatch.setattr(keyring.subprocess, "run", forbidden)


def service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    metadata: bytes = ITEM,
    value: bytes = KEY.encode(),
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["capture_output"] and kwargs["timeout"] == 10
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert not set(keyring._KEY_VARIABLES) & kwargs["env"].keys()
        assert all(KEY not in arg for arg in command)
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, metadata if command[0] == "gdbus" else value, KEY.encode()
        )

    monkeypatch.setattr(keyring.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("provider,variable", list(keyring.PROVIDERS.items()))
def test_exact_mapping_and_selected_environment(
    provider: str,
    variable: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "SYNTHETIC_UNRELATED")
    calls = service(monkeypatch)
    with keyring.credentials((provider,)):
        assert os.environ[variable] == KEY
        assert {name for name in keyring._KEY_VARIABLES if name in os.environ} == {
            variable
        }
        assert _resolve_credentials(provider).source == variable
        assert _resolve_credentials(provider)._api_key() == KEY
    assert os.environ["GEMINI_API_KEY"] == "SYNTHETIC_UNRELATED"
    assert variable not in os.environ
    assert calls[-1] == ["secret-tool", "lookup", "provider", provider]
    assert calls[0][-1] == "{'provider': '" + provider + "'}"
    assert not list(tmp_path.iterdir())
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


@pytest.mark.resource
def test_lookup_termination_reaps_child_and_restores_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()
    path = tmp_path / "synthetic-child.pid"
    helper = Path(__file__).with_name("keyring_process_fixture.py")
    result = subprocess.run(
        [sys.executable, str(helper), str(path)],
        capture_output=True,
        text=True,
        timeout=15,
        env={k: v for k, v in os.environ.items() if k not in keyring._KEY_VARIABLES},
    )
    assert result.returncode == 128 + signal.SIGTERM, result.stdout + result.stderr
    assert path.with_suffix(".pid.cleaned").read_text() == "environment restored"
    assert not psutil.pid_exists(int(path.read_text()))
    assert KEY not in result.stdout + result.stderr


def test_multiple_providers_and_vertex_adc_never_use_gemini_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = service(monkeypatch)
    monkeypatch.setenv("JUDGMENTLLM_VERTEX_AUTH_MODE", "adc")
    with keyring.credentials(("openai", "gemini", "openai", "vertex")):
        assert _resolve_credentials("vertex").auth_type == "api_key"
        assert os.environ["JUDGMENTLLM_VERTEX_AUTH_MODE"] == "api_key"
    assert [c[-1] for c in calls if c[0] == "secret-tool"] == [
        "openai",
        "gemini",
        "vertex",
    ]
    calls.clear()
    with keyring.credentials(("vertex",), vertex_auth_mode="adc"):
        assert _resolve_credentials("vertex").source == "google_adc"
        assert "GOOGLE_API_KEY" not in os.environ
    assert calls == [] and os.environ["JUDGMENTLLM_VERTEX_AUTH_MODE"] == "adc"


@pytest.mark.parametrize(
    "metadata",
    [
        b"(@ao [], @ao [])",
        b"(@ao [], [objectpath '/locked'])",
        b"([objectpath '/one', '/two'], @ao [])",
        b"(['/one'], ['/two'])",
        b"invalid",
        b"[]",
        b"([],)",
        b"('oops', [])",
        b"([3], [])",
        b"(['not/a/path'], [])",
        b"\xff",
    ],
)
def test_missing_locked_ambiguous_and_bad_metadata_fail_before_lookup(
    monkeypatch: pytest.MonkeyPatch, metadata: bytes
) -> None:
    calls = service(monkeypatch, metadata=metadata)
    with pytest.raises(keyring.KeyringError):
        with keyring.credentials(("openai",)):
            pytest.fail("Invalid keyring state was accepted")
    assert len(calls) == 1 and "OPENAI_API_KEY" not in os.environ


@pytest.mark.parametrize(
    "value", [b"", b" ", b"embedded space", b"newline\n", b"\x00", b"\xff"]
)
def test_empty_and_malformed_keys_never_fall_back(
    monkeypatch: pytest.MonkeyPatch, value: bytes
) -> None:
    service(monkeypatch, value=value)
    monkeypatch.setenv("GOOGLE_API_KEY", "SYNTHETIC_GEMINI")
    monkeypatch.setenv("JUDGMENTLLM_VERTEX_AUTH_MODE", "adc")
    with pytest.raises(keyring.KeyringError):
        with keyring.credentials(("vertex",)):
            pytest.fail("A rejected key fell back to ADC")
    assert "JUDGMENTLLM_VERTEX_API_KEY" not in os.environ
    assert os.environ["JUDGMENTLLM_VERTEX_AUTH_MODE"] == "adc"


@pytest.mark.parametrize("failure", ["exit", "missing", "timeout"])
def test_subprocess_failure_output_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if failure == "missing":
            raise FileNotFoundError(KEY)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 10, output=KEY, stderr=KEY)
        return subprocess.CompletedProcess(command, 1, KEY.encode(), KEY.encode())

    monkeypatch.setattr(keyring.subprocess, "run", run)
    assert keyring.main(["--provider", "openai", "--", "auth", "status"]) == 2
    captured = capsys.readouterr()
    assert "Secret Service session" in captured.err
    assert KEY not in captured.out + captured.err


def test_invalid_selection_and_cli_arguments_never_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for providers, mode in [
        ((), "api_key"),
        (("OPENAI",), "api_key"),
        (("vertex",), "invalid"),
    ]:
        with pytest.raises(keyring.KeyringError):
            with keyring.credentials(
                providers, vertex_auth_mode=cast(VertexAuthMode, mode)
            ):
                pytest.fail("Invalid selection accepted")
    with pytest.raises(keyring.KeyringError):
        keyring._lookup("not-approved")
    for argv in (
        ["--provider", "openai"],
        ["--provider", "openai", "--", "study", "execute", "--keyring-provider=openai"],
    ):
        with pytest.raises(SystemExit) as stopped:
            keyring.main(argv)
        assert stopped.value.code == 2
    monkeypatch.setattr(sys, "platform", "win32")
    assert keyring.main(["--provider", "openai", "--", "auth", "status"]) == 2


@pytest.mark.parametrize(
    "operation", ["execute", "recover", "status", "check-admission"]
)
def test_study_launch_defers_all_lookup_and_strips_inherited_keys(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    seen: list[list[str]] = []

    def replace(executable: str, argv: list[str], environment: dict[str, str]) -> None:
        assert executable == sys.executable
        assert not set(keyring._KEY_VARIABLES) & environment.keys()
        seen.append(argv)

    monkeypatch.setattr(keyring.os, "execve", replace)
    assert (
        keyring.main(
            ["--provider", "openai", "--provider", "gemini", "--", "study", operation]
        )
        == 0
    )
    assert seen[0][:4] == [sys.executable, "-m", "judgmentllm.study", operation]
    assert ("--keyring-provider" in seen[0]) == (operation in {"execute", "recover"})
    assert os.environ["OPENAI_API_KEY"] == KEY


def test_exec_failure_restores_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_PRIOR")

    def replace(executable: str, argv: list[str], environment: dict[str, str]) -> None:
        assert environment["OPENAI_API_KEY"] == KEY
        assert all(KEY not in arg for arg in argv)
        raise OSError(KEY)

    monkeypatch.setattr(keyring.os, "execve", replace)
    assert (
        keyring.main(["--provider", "openai", "--", "judgmentllm", "auth", "status"])
        == 2
    )
    assert KEY not in capsys.readouterr().err
    assert os.environ["OPENAI_API_KEY"] == "SYNTHETIC_PRIOR"


def test_exec_replacement_and_signal_cleanup_receive_only_selected_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service(monkeypatch)
    seen: list[list[str]] = []

    def replace(executable: str, argv: list[str], environment: dict[str, str]) -> None:
        assert environment["OPENAI_API_KEY"] == KEY
        seen.append(argv)

    monkeypatch.setattr(keyring.os, "execve", replace)
    assert keyring.main(["--provider", "openai", "auth", "status"]) == 0
    assert seen[0][-2:] == ["auth", "status"]
    assert "OPENAI_API_KEY" not in os.environ
    previous = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as stopped:
        with keyring.credentials(("openai",)):
            handler: Any = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)
    assert stopped.value.code == 128 + signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) == previous
    assert "OPENAI_API_KEY" not in os.environ


@pytest.mark.parametrize("required,expected", [("openai", 0), ("anthropic", 2)])
def test_actual_cli_exit_propagation_without_real_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, required: str, expected: int
) -> None:
    # The isolated CLI process replaces lookup before launch.
    # Windows needs a real CLI child with portable quoting; POSIX retains execve.
    monkeypatch.undo()
    code = "\n".join(
        [
            "import sys",
            "from judgmentllm import keyring",
            "sys.platform = 'linux'",
            "import os, subprocess",
            "real_run = subprocess.run",
            "if os.name == 'nt':",
            "    def portable_exec(executable, argv, environment):",
            "        raise SystemExit(real_run(argv, env=environment).returncode)",
            "    keyring.os.execve = portable_exec",
            "def service(command, **kwargs):",
            "    assert command[0] in {'gdbus', 'secret-tool'}",
            "    output = b\"([objectpath '/synthetic/item'], @ao [])\" if command[0] == 'gdbus' else b'SYNTHETIC_KEYRING_SENTINEL'",
            "    return subprocess.CompletedProcess(command, 0, output, b'')",
            "keyring.subprocess.run = service",
            "raise SystemExit(keyring.main(sys.argv[1:]))",
        ]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            "--provider",
            "openai",
            "--",
            "auth",
            "status",
            "--require",
            required,
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        env={k: v for k, v in os.environ.items() if k not in keyring._KEY_VARIABLES},
    )
    assert result.returncode == expected, result.stdout + result.stderr
    assert "SyntaxError" not in result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["data"]["requirements_met"] == (expected == 0)
    assert KEY not in result.stdout + result.stderr
    assert not list(tmp_path.iterdir())
