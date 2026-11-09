"""Thin CLI registration for explicit Anthropic non-generative diagnostics."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from typing import cast

from pydantic import JsonValue

from judgmentllm.anthropic_diagnostics import (
    AuthVerification,
    ModelDiscovery,
)
from judgmentllm.cli_controls import (
    ControlEnvelope,
    ControlError,
    _emit,
    diagnostic,
)


def present_anthropic(report: AuthVerification | ModelDiscovery) -> None:
    verification = report.verification if isinstance(report, ModelDiscovery) else report
    status = verification.verification_status
    success = status in {"authentication_accepted", "not_required"}
    partial = status == "inconclusive"
    unsupported = status == "unsupported"
    error = (
        None
        if success
        else ControlError(
            category="partial_completion"
            if partial
            else "unsupported_capability"
            if unsupported
            else "execution",
            message="Anthropic non-generative verification was inconclusive"
            if partial
            else "Anthropic non-generative verification failed; inspect the safe diagnostic code",
            retryable=False,
        )
    )
    if error is not None:
        diagnostic(error.category, error.message)
    _emit(
        ControlEnvelope(
            command="providers models"
            if isinstance(report, ModelDiscovery)
            else "auth verify",
            status="ok" if success else "partial" if partial else "error",
            artifacts={},
            data=cast(dict[str, JsonValue], report.model_dump(mode="json")),
            error=error,
        ),
        0 if success else 3 if partial else 2 if unsupported else 1,
    )
