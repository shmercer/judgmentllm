"""Installed authentication diagnostics stay offline and secret-free across platforms."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from test_auth_cli import KEY_NAMES, PROVIDERS, exclude_secrets

from judgmentllm.cli_controls import ControlEnvelope


def executable() -> str:
    installed = Path(sys.executable).with_name(
        "judgmentllm.exe" if os.name == "nt" else "judgmentllm"
    )
    selected = str(installed) if installed.is_file() else shutil.which("judgmentllm")
    assert selected is not None
    return selected


@pytest.mark.parametrize(
    ("arguments", "code", "required"),
    [
        ([], 0, []),
        (
            ["--require", "openai,anthropic", "--require", "gemini,openai"],
            0,
            ["openai", "anthropic", "gemini"],
        ),
        (["--require", "vertex,openai"], 2, ["openai", "vertex"]),
        (["--require", "ollama"], 0, ["ollama"]),
        (["--require", "openai,,gemini"], 2, None),
        (["--require", "bad-provider"], 2, None),
        (["--invalid", "PRIVATE-ARGUMENT"], 2, None),
    ],
)
def test_installed_json_status_with_controlled_unicode_credentials(
    tmp_path: Path,
    arguments: list[str],
    code: int,
    required: list[str] | None,
) -> None:
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.upper() in KEY_NAMES:
            del environment[name]
    values = tuple(
        f"processLEFT{index:04d}résumé模型-opaque-processRIGHT{index:04d}"
        for index in range(len(KEY_NAMES))
    )
    environment.update(zip(KEY_NAMES, values, strict=True))
    environment.update(
        PYTHONIOENCODING="ascii", JUDGMENTLLM_RUN_HOME=str(tmp_path / "unused-store")
    )
    before = set(tmp_path.rglob("*"))
    result = subprocess.run(
        [executable(), "auth", "status", *arguments, "--json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != code:
        pytest.fail(
            "Installed auth command returned an unexpected exit code.", pytrace=False
        )
    assert result.stdout.isascii() and result.stderr.isascii()
    stdout, stderr = result.stdout.decode("ascii"), result.stderr.decode("ascii")
    exclude_secrets(stdout + stderr, values)
    decoded, end = json.JSONDecoder().raw_decode(stdout)
    assert stdout[end:] in {"\n", "\r\n"}
    envelope = cast(dict[str, Any], decoded)
    ControlEnvelope.model_validate(envelope)
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
    assert envelope["schema_version"] == 1 and envelope["command"] == "auth status"
    assert (
        envelope["run_id"] is None
        and envelope["run_status"] is None
        and envelope["artifacts"] == {}
    )
    if required is not None:
        data = envelope["data"]
        assert set(data) == {"providers", "required_providers", "requirements_met"}
        assert [row["provider"] for row in data["providers"]] == list(PROVIDERS)
        assert data["required_providers"] == required
        assert data["requirements_met"] is (code == 0)
        assert data["providers"][0]["status"] == "configured"
        assert data["providers"][3]["status"] == "unknown"
        assert data["providers"][5]["status"] == "not_required"
    else:
        assert envelope["data"] == {}
    if code:
        assert envelope["status"] == "error"
        assert envelope["error"]["category"] in {"usage", "validation"}
    else:
        assert envelope["status"] == "ok" and envelope["error"] is None and stderr == ""
    for line in stderr.splitlines():
        assert json.loads(line)["event"] == "diagnostic"
    assert set(tmp_path.rglob("*")) == before


def test_installed_missing_requirement_and_help(tmp_path: Path) -> None:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() not in KEY_NAMES
    }
    for arguments, expected in [
        (["--require", "openai", "--json"], 2),
        (["--help", "--json"], 0),
        ([], 0),
    ]:
        result = subprocess.run(
            [executable(), "auth", "status", *arguments],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == expected
        if "--json" in arguments:
            decoded = json.loads(result.stdout)
            assert (
                decoded["schema_version"] == 1 and decoded["command"] == "auth status"
            )
            if expected:
                assert decoded["error"]["category"] == "validation"
                assert decoded["data"]["providers"][0]["status"] == "missing"
            else:
                assert "Usage:" in decoded["data"]["help"]
        else:
            assert [row.split()[0] for row in result.stdout.splitlines()[1:]] == list(
                PROVIDERS
            )
    assert list(tmp_path.iterdir()) == []
