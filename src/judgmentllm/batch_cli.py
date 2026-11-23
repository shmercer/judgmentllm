"""Thin native-batch commands; all orchestration lives in public Python services."""

from pathlib import Path
from typing import Annotated

import typer

from judgmentllm.batch_models import (
    BatchControlOptionsV1,
    BatchLimitsV1,
    BatchMockOptions,
)
from judgmentllm.cli_controls import LifecycleGroup, perform
from judgmentllm.config import load_run_spec
from judgmentllm.lifecycle_models import LifecycleError, RetrySelection
from judgmentllm.native_batch import (
    cancel_batch,
    collect_batch,
    get_batch_status,
    poll_batch,
    prepare_batch,
    resume_batch,
    submit_batch,
)


def register_commands(app: typer.Typer) -> None:
    group = typer.Typer(
        cls=LifecycleGroup, help="Offline native batch preparation and durable control."
    )
    app.add_typer(group, name="batch")

    @group.command("prepare")
    def prepare(
        config: Annotated[Path, typer.Option("--config")],
        store: Annotated[Path | None, typer.Option()] = None,
        run: Annotated[str | None, typer.Option("--run")] = None,
        mock_ledger: Annotated[Path | None, typer.Option()] = None,
        mock_fixture: Annotated[Path | None, typer.Option()] = None,
        mock_max_members: Annotated[int, typer.Option(min=1)] = 1000,
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        perform(
            lambda: prepare_batch(
                config,
                store=store,
                run_id=run,
                mock=BatchMockOptions(
                    ledger_path=mock_ledger,
                    fixture_path=mock_fixture,
                    limits=BatchLimitsV1(max_members=mock_max_members),
                ),
            )
        )

    def control(
        ctx: typer.Context,
        run: Annotated[str, typer.Option("--run")],
        store: Annotated[Path | None, typer.Option()] = None,
        config: Annotated[Path | None, typer.Option("--config")] = None,
        use_frozen_snapshot: Annotated[bool, typer.Option()] = False,
        refresh: Annotated[bool, typer.Option()] = False,
        mock_ledger: Annotated[Path | None, typer.Option()] = None,
        mock_fixture: Annotated[Path | None, typer.Option()] = None,
        mock_no_reconciliation: Annotated[bool, typer.Option()] = False,
        mock_no_retrieval: Annotated[bool, typer.Option()] = False,
        mock_max_members: Annotated[int, typer.Option(min=1)] = 1000,
        deadline_seconds: Annotated[float, typer.Option(min=0)] = 30,
        max_polls: Annotated[int, typer.Option(min=1)] = 1,
        max_io_steps: Annotated[int, typer.Option(min=1)] = 1024,
        max_result_rows: Annotated[int, typer.Option(min=1)] = 128,
        retry_task: Annotated[list[str] | None, typer.Option("--retry-task")] = None,
        retry_id: Annotated[str | None, typer.Option("--retry-id")] = None,
        allow_completed: Annotated[bool, typer.Option()] = False,
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        name = ctx.command.name

        def retry_selection() -> RetrySelection | None:
            if retry_task:
                if name != "resume" or retry_id is None:
                    raise LifecycleError(
                        "validation",
                        "batch retry requires resume, selected tasks and --retry-id",
                    )
                return RetrySelection(
                    task_ids=tuple(retry_task),
                    operation_id=retry_id,
                    allow_completed=allow_completed,
                )
            if retry_id is not None or allow_completed:
                raise LifecycleError(
                    "validation", "batch retry controls require selected tasks"
                )
            return None

        if name == "status" and not refresh:

            def local_status():
                retry_selection()
                return get_batch_status(run, store=store)

            perform(local_status)
            return
        function = {
            "submit": submit_batch,
            "status": poll_batch,
            "collect": collect_batch,
            "resume": resume_batch,
            "cancel": cancel_batch,
        }[str(name)]
        perform(
            lambda: function(
                run,
                store=store,
                spec=load_run_spec(config) if config else None,
                use_frozen_snapshot=use_frozen_snapshot,
                mock=BatchMockOptions(
                    ledger_path=mock_ledger,
                    fixture_path=mock_fixture,
                    reconciliation_available=not mock_no_reconciliation,
                    retrieval_available=not mock_no_retrieval,
                    limits=BatchLimitsV1(max_members=mock_max_members),
                ),
                options=BatchControlOptionsV1(
                    deadline_seconds=deadline_seconds,
                    max_polls=max_polls,
                    max_io_steps=max_io_steps,
                    max_result_rows=max_result_rows,
                ),
                retry=retry_selection(),
            ),
            processing=True,
        )

    for name in ("submit", "status", "collect", "resume", "cancel"):
        group.command(name)(control)
