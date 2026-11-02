"""Offline command-line tools backed by the public library functions."""

from pathlib import Path
from typing import Annotated

import typer

from judgmentllm import __version__
from judgmentllm.config import load_run_spec
from judgmentllm.planning import validate_run
from judgmentllm.preview import preview_prompt
from judgmentllm.prompts import init_template, list_templates

app = typer.Typer(
    name="judgmentllm",
    help="Assessment contracts and offline prompt tools. Execution arrives in Milestone 2.",
    add_completion=False,
    no_args_is_help=False,
)
templates_app = typer.Typer(
    help="List, initialize, and preview text templates offline."
)
app.add_typer(templates_app, name="templates")


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
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


@templates_app.command("list")
def templates_list() -> None:
    """List the bundled prompt presets."""
    for template in list_templates():
        typer.echo(f"{template.preset}\t{template.description}")


@templates_app.command("init")
def templates_init(
    preset: Annotated[str, typer.Option(help="Built-in preset name.")],
    output: Annotated[
        Path, typer.Option(help="New template file; existing files are preserved.")
    ],
) -> None:
    """Copy a preset to an editable text file."""
    try:
        init_template(preset, output)
    except (ValueError, OSError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(2) from error
    typer.echo(str(output))


@templates_app.command("preview")
def templates_preview(
    config: Annotated[Path, typer.Option(help="YAML/JSON run configuration.")],
    item_id: Annotated[str | None, typer.Option(help="Rubric response ID.")] = None,
    trait_id: Annotated[
        str | None, typer.Option("--trait", help="Trait identifying the request group.")
    ] = None,
    pair_uid: Annotated[
        str | None, typer.Option(help="Scheduled pair occurrence ID.")
    ] = None,
) -> None:
    """Print one complete prompt locally; no credentials or provider calls."""
    try:
        spec = load_run_spec(config)
        rendered = preview_prompt(
            spec, item_id=item_id, trait_id=trait_id, pair_uid=pair_uid
        )
        for issue in validate_run(spec).issues:
            if issue.severity == "warning":
                typer.echo(f"Warning [{issue.code}]: {issue.message}", err=True)
        typer.echo(rendered.text)
    except (ValueError, OSError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(2) from error
