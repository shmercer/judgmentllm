"""Thin CLI registration for explicit Vertex non-generative diagnostics."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from typing import cast

from pydantic import JsonValue

from judgmentllm.cli_controls import (
    ControlEnvelope,
    ControlError,
    _emit,
    diagnostic,
)
from judgmentllm.vertex_diagnostics import (
    AuthVerification,
    ModelDiscovery,
)


def present_vertex(report: AuthVerification | ModelDiscovery) -> None:
    error = ControlError(
        category="unsupported_capability",
        message="Vertex has no reviewed protected non-generative verification operation; inference eligibility remains unknown",
        retryable=False,
    )
    diagnostic(error.category, error.message)
    _emit(
        ControlEnvelope(
            command="providers models"
            if isinstance(report, ModelDiscovery)
            else "auth verify",
            status="error",
            artifacts={},
            data=cast(dict[str, JsonValue], report.model_dump(mode="json")),
            error=error,
        ),
        2,
    )
