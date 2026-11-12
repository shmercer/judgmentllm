"""Non-generative provider commands preserve their byte-level control contract."""

from __future__ import annotations

import importlib
import json
import os
import socket
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from judgmentllm.cli import app

PROVIDERS = ("openai", "anthropic", "gemini")
COMMANDS = ("auth verify", "providers models")
OUTCOMES = (
    ("authentication_accepted", "ok", 0, None),
    ("not_required", "ok", 0, None),
    ("inconclusive", "partial", 3, "partial_completion"),
    ("unsupported", "error", 2, "unsupported_capability"),
    ("credentials_rejected", "error", 1, "execution"),
    ("permission_denied", "error", 1, "execution"),
    ("network_unavailable", "error", 1, "execution"),
    ("rate_limited", "error", 1, "execution"),
)


def report_for(provider: str, status: str, command: str) -> BaseModel:
    module = importlib.import_module(f"judgmentllm.{provider}_diagnostics")
    verification = module.AuthVerification(
        checked_at=datetime(2026, 11, 12, tzinfo=UTC),
        verification_status=status,
        diagnostic_code={
            "inconclusive": "inconclusive_response",
            "unsupported": f"missing_{provider}_extra",
        }.get(
            status,
            status
            if status not in {"authentication_accepted", "not_required"}
            else None,
        ),
    )
    if command == "auth verify":
        return verification
    return module.ModelDiscovery(
        checked_at=verification.checked_at,
        verification=verification,
        models=("001", "modèle-模型"),
        requested_model="modèle-模型",
        model_visibility="visible",
        complete=False,
        sdk_calls=1,
    )


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("status,outcome,code,category", OUTCOMES)
@pytest.mark.parametrize("machine", (False, True), ids=("human", "json"))
def test_provider_diagnostic_output(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    command: str,
    status: str,
    outcome: str,
    code: int,
    category: str | None,
    machine: bool,
) -> None:
    report = report_for(provider, status, command)

    def saved_report(**kwargs: Any) -> BaseModel:
        assert kwargs == ({"provider": "openai"} if provider == "openai" else {}) | (
            {"model": "modèle-模型"} if command == "providers models" else {}
        )
        return report

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Unexpected provider or network access")

    module = importlib.import_module(
        "judgmentllm.providers.openai_cli"
        if provider == "openai"
        else f"judgmentllm.{provider}_diagnostics"
    )
    operation = (
        "verify_authentication" if command == "auth verify" else "discover_models"
    )
    monkeypatch.setattr(module, operation, saved_report)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    args = [*command.split(), "--provider", provider]
    if command == "providers models":
        args += ["--model", "modèle-模型"]
    if machine:
        args.append("--json")
    result = CliRunner().invoke(app, args)

    display = {"openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Gemini"}[
        provider
    ]
    message = (
        f"{display} non-generative verification was inconclusive"
        if status == "inconclusive"
        else f"{display} non-generative verification failed; inspect the safe diagnostic code"
    )
    data = report.model_dump(mode="json")
    error = (
        {"category": category, "message": message, "retryable": False}
        if category is not None
        else None
    )
    envelope = {
        "schema_version": 1,
        "command": command,
        "status": outcome,
        "run_id": None,
        "run_status": None,
        "artifacts": {},
        "data": data,
        "error": error,
    }
    diagnostic = {
        "schema_version": 1,
        "event": "diagnostic",
        "category": category,
        "message": message,
        "run_id": None,
    }
    stdout = (
        json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
        if machine
        else f"{command}: {outcome}\n" + json.dumps(data, ensure_ascii=False, indent=2)
    ) + "\n"
    stderr = (
        (
            json.dumps(diagnostic, ensure_ascii=True, separators=(",", ":"))
            if machine
            else message
        )
        + "\n"
        if error is not None
        else ""
    )
    assert result.exit_code == code, result.output
    assert result.stdout_bytes == stdout.replace("\n", os.linesep).encode("utf-8")
    assert result.stderr_bytes == stderr.replace("\n", os.linesep).encode("utf-8")
