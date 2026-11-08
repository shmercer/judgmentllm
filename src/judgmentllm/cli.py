"""Offline command-line tools backed by the public library functions."""

from pathlib import Path
from typing import Annotated, Literal

import typer

from judgmentllm import __version__
from judgmentllm._auth import _select_providers  # pyright: ignore[reportPrivateUsage]
from judgmentllm.auth import credential_status
from judgmentllm.cli_controls import (
    LifecycleGroup,
    _AuthStatusReport,  # pyright: ignore[reportPrivateUsage]
    diagnostic,
    perform,
    present_auth_status,
    progress,
)
from judgmentllm.config import RunSpecV2, RunSpecV3, load_run_spec
from judgmentllm.lifecycle import (
    LifecycleError,
    MockExecutionOptions,
    RetrySelection,
    RunSummary,
    estimate_run,
    export_run_results,
    get_run,
    list_runs,
    resume_run,
    run_live,
    validate_inputs,
)
from judgmentllm.planning import validate_run
from judgmentllm.preview import preview_prompt
from judgmentllm.production_prompts import RenderedProductionPromptV1
from judgmentllm.prompts import RenderedAssessmentPrompt, init_template, list_templates
from judgmentllm.providers.openai_cli import (
    register_commands as _register_openai_commands,
)

app = typer.Typer(
    name="judgmentllm",
    cls=LifecycleGroup,
    help="Assessment preparation and durable local execution lifecycle.",
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
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit one version-1 control object with exact prompt metadata.",
        ),
    ] = False,
) -> None:
    """Print one complete prompt locally; no credentials or provider calls."""
    if json_output:

        def operation() -> RenderedAssessmentPrompt | RenderedProductionPromptV1:
            spec = load_run_spec(config)
            rendered = preview_prompt(
                spec, item_id=item_id, trait_id=trait_id, pair_uid=pair_uid
            )
            for issue in validate_run(spec).issues:
                if issue.severity == "warning":
                    diagnostic(
                        issue.code,
                        "Review the prompt preflight diagnostics; no provider qualification was inferred.",
                    )
            return rendered

        perform(operation)
        return
    spec = None
    try:
        spec = load_run_spec(config)
        rendered = preview_prompt(
            spec, item_id=item_id, trait_id=trait_id, pair_uid=pair_uid
        )
        for issue in validate_run(spec).issues:
            if issue.severity == "warning":
                typer.echo(f"Warning [{issue.code}]: {issue.message}", err=True)
        if isinstance(spec, (RunSpecV2, RunSpecV3)):
            typer.echo(rendered.text.encode("utf-8"), nl=False)
        else:
            typer.echo(rendered.text)
    except (ValueError, OSError) as error:
        message = (
            "Invalid configuration or prompt selection."
            if spec is None or isinstance(spec, (RunSpecV2, RunSpecV3))
            else str(error)
        )
        typer.echo(f"Error: {message}", err=True)
        raise typer.Exit(2) from error


runs_app = typer.Typer(help="Inspect durable local runs without contacting providers.")
results_app = typer.Typer(help="Publish immutable all-attempt result exports.")
app.add_typer(runs_app, name="runs")
app.add_typer(results_app, name="results")
auth_app = typer.Typer(help="Inspect credential configuration strictly offline.")
app.add_typer(auth_app, name="auth")

ConfigOption = Annotated[
    Path, typer.Option("--config", help="YAML/JSON run configuration.")
]
StoreOption = Annotated[
    Path | None, typer.Option("--store", help="Local runtime store.")
]
RunOption = Annotated[
    str, typer.Option("--run", help="Original run ID or absolute run directory.")
]
JsonOption = Annotated[
    bool, typer.Option("--json", help="Emit one version-1 JSON control object.")
]
MockLedgerOption = Annotated[
    Path | None, typer.Option("--mock-ledger", help="Original mock acceptance ledger.")
]
MockFixtureOption = Annotated[
    Path | None,
    typer.Option("--mock-fixture", help="Strict synthetic scenario document."),
]
MockNoRetrievalOption = Annotated[
    bool,
    typer.Option(
        "--mock-no-retrieval", help="Disable authoritative mock observation retrieval."
    ),
]
MockNoReconciliationOption = Annotated[
    bool,
    typer.Option(
        "--mock-no-reconciliation",
        help="Disable authoritative mock submission reconciliation.",
    ),
]


@auth_app.command("status")
def auth_status(
    require: Annotated[
        list[str] | None,
        typer.Option(
            "--require",
            help="Required provider IDs; repeat or use comma-separated lowercase IDs.",
        ),
    ] = None,
    json_output: JsonOption = False,
) -> None:
    """Inspect documented credential sources without verifying credentials or model access."""

    def operation() -> _AuthStatusReport:
        required = _select_providers(
            tuple(member for value in (require or ()) for member in value.split(","))
        )
        statuses = credential_status()
        return _AuthStatusReport(
            providers=statuses,
            required_providers=required,
            requirements_met=all(
                row.status in {"configured", "not_required"}
                for row in statuses
                if row.provider in required
            ),
        )

    perform(operation, present=present_auth_status)


def _mock(
    ledger: Path | None,
    fixture: Path | None,
    no_retrieval: bool,
    no_reconciliation: bool,
) -> MockExecutionOptions | None:
    if (
        ledger is None
        and fixture is None
        and not no_retrieval
        and not no_reconciliation
    ):
        return None
    return MockExecutionOptions(
        ledger_path=ledger,
        fixture_path=fixture,
        retrieval_available=not no_retrieval,
        reconciliation_available=not no_reconciliation,
    )


@app.command("validate")
def validate_command(
    config: ConfigOption,
    exhaustive_rendering: Annotated[
        bool, typer.Option(help="Render every actual request during validation.")
    ] = False,
    json_output: JsonOption = False,
) -> None:
    """Validate all local sources without provider submission."""
    perform(
        lambda: validate_inputs(config, exhaustive_rendering=exhaustive_rendering),
        validation=True,
    )


@app.command("plan")
def plan_command(config: ConfigOption, json_output: JsonOption = False) -> None:
    """Count exact tasks/requests and retain unknown token or price estimates."""
    perform(lambda: estimate_run(config))


@app.command("run")
def run_command(
    config: ConfigOption,
    store: StoreOption = None,
    mock_ledger: MockLedgerOption = None,
    mock_fixture: MockFixtureOption = None,
    mock_no_retrieval: MockNoRetrievalOption = False,
    mock_no_reconciliation: MockNoReconciliationOption = False,
    json_output: JsonOption = False,
) -> None:
    """Execute within this process and retain durable state for later inspection."""
    perform(
        lambda: run_live(
            config,
            store=store,
            progress=progress,
            mock=_mock(
                mock_ledger, mock_fixture, mock_no_retrieval, mock_no_reconciliation
            ),
        ),
        processing=True,
    )


@runs_app.command("resume")
@app.command("resume")
def resume_command(
    run: RunOption,
    store: StoreOption = None,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config", help="Original compatible specification, when required."
        ),
    ] = None,
    use_frozen_snapshot: Annotated[
        bool,
        typer.Option(
            help="Explicitly authorize original verified full-capture snapshots."
        ),
    ] = False,
    retry_task: Annotated[
        list[str] | None,
        typer.Option(
            "--retry-task",
            help="Explicit member task ID; repeat within one original request.",
        ),
    ] = None,
    retry_id: Annotated[
        str | None,
        typer.Option(
            "--retry-id",
            help="Stable retry-operation ID; required with task selection.",
        ),
    ] = None,
    allow_completed: Annotated[
        bool, typer.Option(help="Authorize selected previously completed tasks.")
    ] = False,
    mock_ledger: MockLedgerOption = None,
    mock_fixture: MockFixtureOption = None,
    mock_no_retrieval: MockNoRetrievalOption = False,
    mock_no_reconciliation: MockNoReconciliationOption = False,
    json_output: JsonOption = False,
) -> None:
    """Recover original work or execute a durably authorized exact subset retry."""

    def operation() -> RunSummary:
        if retry_task and retry_id is None:
            raise LifecycleError("usage", "--retry-task requires --retry-id.")
        if not retry_task and (retry_id is not None or allow_completed):
            raise LifecycleError(
                "usage", "--retry-id and --allow-completed require --retry-task."
            )
        retry = (
            RetrySelection(
                task_ids=tuple(retry_task),
                operation_id=retry_id,
                allow_completed=allow_completed,
            )
            if retry_task and retry_id is not None
            else None
        )
        return resume_run(
            run,
            store=store,
            spec=load_run_spec(config) if config is not None else None,
            use_frozen_snapshot=use_frozen_snapshot,
            retry=retry,
            progress=progress,
            mock=_mock(
                mock_ledger, mock_fixture, mock_no_retrieval, mock_no_reconciliation
            ),
        )

    perform(operation, processing=True)


@runs_app.command("list")
def runs_list(
    store: StoreOption = None,
    limit: Annotated[
        int,
        typer.Option(help="Maximum number of runs (1 through 1000).", min=1, max=1000),
    ] = 100,
    cursor: Annotated[
        str | None, typer.Option(help="Opaque cursor from the preceding page.")
    ] = None,
    json_output: JsonOption = False,
) -> None:
    """List a bounded page of local runs."""
    perform(lambda: list_runs(store=store, limit=limit, cursor=cursor))


@runs_app.command("status")
def runs_status(
    run: RunOption, store: StoreOption = None, json_output: JsonOption = False
) -> None:
    """Inspect durable state; an incomplete or failed run is a successful query."""
    perform(lambda: get_run(run, store=store))


@results_app.command("export")
def results_export(
    run: RunOption,
    store: StoreOption = None,
    format: Annotated[
        Literal["jsonl", "csv"], typer.Option(help="jsonl or csv.")
    ] = "jsonl",
    view: Annotated[
        Literal["canonical", "r"],
        typer.Option(help="canonical or r; R view requires CSV."),
    ] = "canonical",
    na_string: Annotated[
        str, typer.Option(help="Missing marker for R-compatible CSV only.")
    ] = "NA",
    high_water_mark: Annotated[
        int | None, typer.Option(help="Explicit immutable result snapshot.", min=0)
    ] = None,
    json_output: JsonOption = False,
) -> None:
    """Publish recorded attempts, including unsuccessful outcomes, without provider calls."""
    perform(
        lambda: export_run_results(
            run,
            store=store,
            format=format,
            view=view,
            na_string=na_string,
            high_water_mark=high_water_mark,
        )
    )


# Register explicit online diagnostics; ordinary auth status remains offline.

_register_openai_commands(app, auth_app)
