"""Thin CLI registration for explicit Anthropic non-generative diagnostics."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from typing import cast

from pydantic import JsonValue

from judgmentllm.anthropic_diagnostics import (
    AuthVerification,
    ModelDiscovery,
)
from judgmentllm.cli_controls import _present_provider_diagnostic


def present_anthropic(report: AuthVerification | ModelDiscovery) -> None:
    verification = report.verification if isinstance(report, ModelDiscovery) else report
    _present_provider_diagnostic(
        "Anthropic",
        "providers models" if isinstance(report, ModelDiscovery) else "auth verify",
        verification.verification_status,
        cast(dict[str, JsonValue], report.model_dump(mode="json")),
    )
