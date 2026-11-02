"""Command-line entry point for the JudgmentLLM package foundation."""

from typing import Annotated

import typer

from judgmentllm import __version__

app = typer.Typer(
    name="judgmentllm",
    help="JudgmentLLM package foundation. Assessment commands are not implemented yet.",
    add_completion=False,
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", help="Show the package version.", is_eager=True)
    ] = False,
) -> None:
    """Show package information and available options."""
    if version:
        typer.echo(f"judgmentllm {__version__}")
    else:
        typer.echo(ctx.get_help())
