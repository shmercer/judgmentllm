"""Versioned lifecycle control output and a safe command parsing boundary."""

from __future__ import annotations

import json
import sys
import warnings
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, TextIO, cast

import typer
from pydantic import BaseModel, ConfigDict, JsonValue
from typer import _click  # pyright: ignore[reportPrivateUsage]
from typer.core import TyperCommand, TyperGroup

from judgmentllm.lifecycle import LifecycleError, ProgressEvent, get_run
from judgmentllm.lifecycle_models import ErrorCategory
from judgmentllm.models import StrictModel, VersionedModel
from judgmentllm.persistence.models import RunState

CommandOutcome = Literal["ok", "partial", "error"]


class ControlError(StrictModel):
    """Safe command failure information, independent of durable run status."""

    model_config = ConfigDict(frozen=True)

    category: ErrorCategory
    message: str
    retryable: bool


class ControlEnvelope(VersionedModel):
    """One version-1 object on lifecycle JSON-mode stdout."""

    model_config = ConfigDict(frozen=True)

    command: str
    status: CommandOutcome
    run_id: str | None = None
    run_status: RunState | None = None
    artifacts: dict[str, str | None]
    data: dict[str, JsonValue]
    error: ControlError | None = None


class Diagnostic(VersionedModel):
    """Content-free JSONL stderr record; no arbitrary exception serialization."""

    model_config = ConfigDict(frozen=True)

    event: Literal["diagnostic", "progress"]
    category: str
    message: str
    run_id: str | None = None


@dataclass
class Invocation:
    command: str
    machine: bool
    store: str | None = None
    run_id: str | None = None


_invocation: ContextVar[Invocation | None] = ContextVar("lifecycle_cli", default=None)
_LIFECYCLE = {"validate", "plan", "run", "resume", "runs", "results"}
_USAGE = {"usage", "validation", "unsupported_capability", "not_found"}


def _command_name(args: Sequence[str]) -> str:
    if not args or args[0] not in _LIFECYCLE:
        return "unknown"
    if args[0] in {"runs", "results"}:
        if len(args) > 1 and (args[0], args[1]) in {
            ("runs", "list"),
            ("runs", "status"),
            ("results", "export"),
        }:
            return " ".join(args[:2])
        return args[0]
    return args[0]


def _current() -> Invocation:
    invocation = _invocation.get()
    if invocation is None:
        raise RuntimeError("lifecycle command requires an invocation context")
    return invocation


def diagnostic(category: str, message: str, *, progress: bool = False) -> None:
    """Write safe diagnostics separately from the stdout control response."""
    _write_diagnostic(_current(), category, message, progress=progress)


def _write_diagnostic(
    invocation: Invocation, category: str, message: str, *, progress: bool = False
) -> None:
    if invocation.machine:
        typer.echo(
            json.dumps(
                Diagnostic(
                    event="progress" if progress else "diagnostic",
                    category=category,
                    message=message,
                    run_id=invocation.run_id,
                ).model_dump(mode="json"),
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            err=True,
        )
    else:
        typer.echo(message, err=True)


def progress(event: ProgressEvent) -> None:
    invocation = _current()
    invocation.run_id = event.run_id
    invocation.store = event.store
    messages = {
        "started": "Run preparation started.",
        "prepared": "Run preparation is durable.",
        "finished": "Run processing finished.",
    }
    diagnostic(event.stage, messages[event.stage], progress=True)


def _emit(envelope: ControlEnvelope, code: int) -> None:
    invocation = _current()
    if invocation.machine:
        typer.echo(
            json.dumps(
                envelope.model_dump(mode="json", exclude_none=False),
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    else:
        typer.echo(f"{envelope.command}: {envelope.status}")
        if envelope.run_id is not None:
            typer.echo(f"Run {envelope.run_id}: {envelope.run_status or 'unavailable'}")
        if envelope.data:
            typer.echo(json.dumps(envelope.data, ensure_ascii=False, indent=2))
        for name, path in envelope.artifacts.items():
            if path is not None:
                typer.echo(f"{name}: {path}")
    raise typer.Exit(code)


def _parts(
    model: BaseModel,
) -> tuple[str | None, RunState | None, dict[str, str | None], dict[str, JsonValue]]:
    data = cast(dict[str, JsonValue], model.model_dump(mode="json"))
    data.pop("schema_version", None)
    run_id = cast(str | None, data.pop("run_id", None))
    state = cast(RunState | None, data.pop("run_status", None))
    artifacts = cast(dict[str, str | None], data.pop("artifacts", {}))
    return run_id, state, artifacts, data


def fail(
    category: ErrorCategory,
    message: str,
    *,
    retryable: bool = False,
    code: int | None = None,
) -> None:
    invocation = _current()
    run_id, state, artifacts, data = invocation.run_id, None, {}, {}
    if run_id is not None:
        try:
            run_id, state, artifacts, data = _parts(
                get_run(run_id, store=invocation.store)
            )
        except Exception:
            # Failure to inspect an interrupted/unsealed store cannot conceal the
            # original command error or cause a second control object.
            pass
    diagnostic(category, message)
    _emit(
        ControlEnvelope(
            command=invocation.command,
            status="error",
            run_id=run_id,
            run_status=state,
            artifacts=artifacts,
            data=data,
            error=ControlError(category=category, message=message, retryable=retryable),
        ),
        code if code is not None else (2 if category in _USAGE else 1),
    )


def perform(
    operation: Callable[[], BaseModel],
    *,
    processing: bool = False,
    validation: bool = False,
) -> None:
    """Call one public service and present its typed outcome without interpretation of judgments."""
    invocation = _current()
    seen_warnings: set[str] = set()

    def safe_warning(
        message: Warning | str,
        category: type[Warning],
        filename: str,
        lineno: int,
        file: TextIO | None = None,
        line: str | None = None,
    ) -> None:
        # Library warnings can contain assessment identifiers or arbitrary raw
        # exception material. Preserve warning-as-error filters, but translate
        # emitted warnings to bounded safe stderr records, including I/O threads.
        kind = (
            "validation_warning"
            if issubclass(category, UserWarning)
            else "runtime_warning"
        )
        if kind not in seen_warnings:
            seen_warnings.add(kind)
            _write_diagnostic(
                invocation,
                kind,
                "A library warning occurred; review validation issues and documented configuration constraints.",
            )

    try:
        with warnings.catch_warnings():
            warnings.showwarning = safe_warning
            result = operation()
    except LifecycleError as error:
        invocation = _current()
        invocation.run_id = error.run_id or invocation.run_id
        invocation.store = error.store or invocation.store
        fail(error.category, error.message, retryable=error.retryable)
        return
    except KeyboardInterrupt:
        fail(
            "interrupted",
            "Operation interrupted; inspect the durable run before resuming.",
            code=130,
        )
        return
    except ValueError:
        fail("validation", "Invalid configuration, selection, or local resource.")
        return
    except OSError:
        fail("execution", "The local execution storage operation failed.")
        return
    except Exception:
        fail(
            "internal",
            "The operation failed unexpectedly; durable run evidence was retained.",
        )
        return
    run_id, state, artifacts, data = _parts(result)
    invocation = _current()
    invocation.run_id = run_id
    if validation and data.get("valid") is False:
        diagnostic("validation", "Input validation failed; review the reported issues.")
        _emit(
            ControlEnvelope(
                command=invocation.command,
                status="error",
                artifacts=artifacts,
                data=data,
                error=ControlError(
                    category="validation",
                    message="Input validation failed.",
                    retryable=False,
                ),
            ),
            2,
        )
    outcome: CommandOutcome = "ok"
    code = 0
    error = None
    if processing:
        outcome = cast(CommandOutcome, data.pop("outcome"))
        code = {"ok": 0, "partial": 3, "error": 1}[outcome]
        if outcome != "ok":
            category = "partial_completion" if outcome == "partial" else "execution"
            message = (
                "Processing retained unsuccessful or unresolved work."
                if outcome == "partial"
                else "Execution did not complete."
            )
            error = ControlError(category=category, message=message, retryable=False)
            diagnostic(category, message)
    _emit(
        ControlEnvelope(
            command=invocation.command,
            status=outcome,
            run_id=run_id,
            run_status=state,
            artifacts=artifacts,
            data=data,
            error=error,
        ),
        code,
    )


class LifecycleGroup(TyperGroup):
    """Intercept handled lifecycle parsing failures before Click writes text."""

    def _json_help(self, command: str, prog_name: str) -> None:
        with ExitStack() as contexts:
            context = contexts.enter_context(
                self.make_context(prog_name, [], resilient_parsing=True)
            )
            target: _click.Command = self
            for part in command.split():
                if not isinstance(target, TyperGroup):
                    fail(
                        "usage",
                        "Unknown lifecycle command. Use --help for available commands.",
                    )
                    return
                child = target.get_command(context, part)
                if child is None:
                    fail(
                        "usage",
                        "Unknown lifecycle command. Use --help for available commands.",
                    )
                    return
                target = child
                context = contexts.enter_context(
                    target.make_context(
                        part, [], parent=context, resilient_parsing=True
                    )
                )
            # Typer rich formatting prints directly instead of returning text.
            # Use its plain formatter for JSON help, then restore this command.
            if isinstance(target, (TyperCommand, TyperGroup)):
                markup = target.rich_markup_mode
                target.rich_markup_mode = None
                try:
                    help_text = context.get_help()
                finally:
                    target.rich_markup_mode = markup
            else:
                help_text = context.get_help()
            _emit(
                ControlEnvelope(
                    command=command, status="ok", artifacts={}, data={"help": help_text}
                ),
                0,
            )

    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        arguments = list(sys.argv[1:] if args is None else args)
        machine = "--json" in arguments
        lifecycle = bool(arguments and arguments[0] in _LIFECYCLE)
        if not lifecycle and not (
            machine and (not arguments or arguments[0] != "templates")
        ):
            return super().main(
                args=args,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=standalone_mode,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        invocation = Invocation(_command_name(arguments), machine)
        token = _invocation.set(invocation)
        code = 1
        try:
            try:
                if (
                    machine
                    and "--help" in arguments
                    and invocation.command != "unknown"
                ):
                    self._json_help(invocation.command, prog_name or "judgmentllm")
                # Invoke under our own boundary: Typer's standard main writes
                # an unstructured newline/abort message for KeyboardInterrupt.
                # Explicit arguments also avoid Windows glob/env expansion.
                with self.make_context(
                    prog_name or "judgmentllm", arguments, **extra
                ) as context:
                    value = self.invoke(context)
                code = value if isinstance(value, int) else 0
            except _click.ClickException:
                try:
                    fail(
                        "usage",
                        "Invalid command arguments. Use the command's --help for usage.",
                    )
                except typer.Exit as error:
                    code = error.exit_code
            except (KeyboardInterrupt, typer.Abort):
                try:
                    fail(
                        "interrupted",
                        "Operation interrupted; inspect the durable run before resuming.",
                        code=130,
                    )
                except typer.Exit as error:
                    code = error.exit_code
            except typer.Exit as error:
                code = error.exit_code
            except Exception:
                try:
                    fail(
                        "internal",
                        "The operation failed unexpectedly; durable run evidence was retained.",
                    )
                except typer.Exit as error:
                    code = error.exit_code
            if standalone_mode:
                raise SystemExit(code)
            return code
        finally:
            _invocation.reset(token)
