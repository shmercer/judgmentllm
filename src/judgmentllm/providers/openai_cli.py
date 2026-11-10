"""Thin CLI registration for explicit OpenAI and Anthropic diagnostics."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from typing import Annotated, cast

import typer
from pydantic import JsonValue

from judgmentllm.cli_controls import (
    ControlEnvelope,
    ControlError,
    _emit,
    diagnostic,
    perform,
)
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.openai_diagnostics import (
    AuthVerification,
    ModelDiscovery,
    discover_models,
    verify_authentication,
)


def _present(report: AuthVerification | ModelDiscovery) -> None:
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
            message="OpenAI non-generative verification was inconclusive"
            if partial
            else "OpenAI non-generative verification failed; inspect the safe diagnostic code",
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


def register_commands(app: typer.Typer, auth_app: typer.Typer) -> typer.Typer:
    """Register auth verify and the providers group, returning it for smoke."""
    providers = typer.Typer(
        help="Explicit online provider diagnostics and qualification."
    )
    app.add_typer(providers, name="providers")

    @auth_app.command("verify")
    def auth_verify(
        provider: Annotated[
            str, typer.Option(help="Exact provider; openai, anthropic or gemini.")
        ],
        json_output: Annotated[
            bool, typer.Option("--json", help="Emit the version-1 control envelope.")
        ] = False,
    ) -> None:
        """Make one protected non-generative read; does not qualify a model."""

        if provider == "vertex":
            from judgmentllm.providers.vertex_cli import present_vertex
            from judgmentllm.vertex_diagnostics import (
                verify_authentication as verify_vertex,
            )

            return perform(lambda: verify_vertex(), present=present_vertex)

        if provider == "gemini":
            from judgmentllm.gemini_diagnostics import (
                verify_authentication as verify_gemini,
            )
            from judgmentllm.providers.gemini_cli import present_gemini

            return perform(lambda: verify_gemini(), present=present_gemini)

        if provider == "anthropic":
            from judgmentllm.anthropic_diagnostics import (
                verify_authentication as verify_anthropic,
            )
            from judgmentllm.providers.anthropic_cli import present_anthropic

            perform(lambda: verify_anthropic(), present=present_anthropic)
            return

        def operation() -> AuthVerification:
            if provider != "openai":
                raise LifecycleError(
                    "unsupported_capability",
                    "Authentication verification supports providers openai, anthropic and gemini",
                )
            return verify_authentication(provider=provider)

        perform(operation, present=_present)

    @providers.command("models")
    def providers_models(
        provider: Annotated[
            str, typer.Option(help="Exact provider; openai, anthropic or gemini.")
        ],
        model: Annotated[
            str | None,
            typer.Option(help="Optional exact model ID whose visibility is inspected."),
        ] = None,
        json_output: Annotated[
            bool, typer.Option("--json", help="Emit the version-1 control envelope.")
        ] = False,
    ) -> None:
        """List visible models with one read; no generation or pagination."""

        if provider == "vertex":
            from judgmentllm.providers.vertex_cli import present_vertex
            from judgmentllm.vertex_diagnostics import (
                discover_models as discover_vertex,
            )

            return perform(lambda: discover_vertex(model=model), present=present_vertex)

        if provider == "gemini":
            from judgmentllm.gemini_diagnostics import (
                discover_models as discover_gemini,
            )
            from judgmentllm.providers.gemini_cli import present_gemini

            return perform(lambda: discover_gemini(model=model), present=present_gemini)

        if provider == "anthropic":
            from judgmentllm.anthropic_diagnostics import (
                discover_models as discover_anthropic,
            )
            from judgmentllm.providers.anthropic_cli import present_anthropic

            perform(lambda: discover_anthropic(model=model), present=present_anthropic)
            return

        def operation() -> ModelDiscovery:
            if provider != "openai":
                raise LifecycleError(
                    "unsupported_capability",
                    "Model discovery supports providers openai, anthropic and gemini",
                )
            return discover_models(provider=provider, model=model)

        perform(operation, present=_present)

    return providers
