"""Selected, process-local GNOME Keyring credentials for the existing CLI."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import argparse
import ast
import os
import re
import signal
import subprocess
import sys
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from types import FrameType
from typing import Any, cast

from judgmentllm._auth import VertexAuthMode, _resolve_credentials

PROVIDERS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "vertex": "JUDGMENTLLM_VERTEX_API_KEY",
    "together": "TOGETHER_API_KEY",
    "typesafe": "TYPESAFE_API_KEY",
}
_KEY_VARIABLES = (*PROVIDERS.values(), "GEMINI_API_KEY")


class KeyringError(ValueError):
    """Fixed diagnostics only; subprocess output and key values are never attached."""


def _run(command: list[str]) -> bytes:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=10,
            env={k: v for k, v in os.environ.items() if k not in _KEY_VARIABLES},
        )
        if result.returncode == 0:
            return result.stdout
    except (OSError, subprocess.SubprocessError):
        pass
    raise KeyringError(
        "Keyring unavailable: check secret-tool, gdbus and the unlocked desktop "
        "Secret Service session. SSH/tmux must use that same session bus."
    ) from None


def _lookup(provider: str) -> str:
    if provider not in PROVIDERS:
        raise KeyringError("Select an approved exact provider name.")
    # lookup returns only the first match; SearchItems checks ambiguity without secrets.
    metadata = _run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.freedesktop.secrets",
            "--object-path",
            "/org/freedesktop/secrets",
            "--method",
            "org.freedesktop.Secret.Service.SearchItems",
            "{'provider': '" + provider + "'}",
        ]
    )
    try:
        parsed: object = ast.literal_eval(
            metadata.decode("utf-8").replace("objectpath ", "").replace("@ao ", "")
        )
        if type(parsed) is not tuple:
            raise ValueError
        items = cast(tuple[Any, ...], parsed)
        if (
            len(items) != 2
            or any(type(group) is not list for group in items)
            or any(
                type(path) is not str or re.fullmatch(r"/[A-Za-z0-9_/]+", path) is None
                for group in items
                for path in group
            )
        ):
            raise ValueError
    except (ValueError, SyntaxError, UnicodeError):
        raise KeyringError("Secret Service returned invalid item metadata.") from None
    unlocked, locked = cast(tuple[list[str], list[str]], items)
    if not unlocked and not locked:
        raise KeyringError("Required provider credential is missing.")
    if len(unlocked) + len(locked) != 1:
        raise KeyringError(
            "Required provider credential is ambiguous; retain one matching item."
        )
    if locked:
        raise KeyringError(
            "Required provider credential is locked; unlock it on the desktop."
        )
    raw = _run(["secret-tool", "lookup", "provider", provider])
    try:
        # secret-tool adds no newline when stdout is a pipe; never trim a stored key.
        return raw.decode("utf-8")
    except UnicodeError:
        raise KeyringError("Required provider credential is not valid text.") from None


@contextmanager
def credentials(
    providers: Sequence[str], *, vertex_auth_mode: VertexAuthMode = "api_key"
) -> Generator[None]:
    """Expose only selected keys, restore the environment even on lookup/CLI failure."""
    if (
        not providers
        or any(p not in PROVIDERS for p in providers)
        or vertex_auth_mode not in {"api_key", "adc"}
    ):
        raise KeyringError(
            "Select approved providers and an explicit Vertex authentication mode."
        )
    names = (*_KEY_VARIABLES, "JUDGMENTLLM_VERTEX_AUTH_MODE")
    previous = {name: os.environ.get(name) for name in names}
    prior_handler = signal.getsignal(signal.SIGTERM)

    def terminate(signum: int, frame: FrameType | None) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    try:
        for name in names:
            os.environ.pop(name, None)
        for provider in dict.fromkeys(providers):
            if provider == "vertex":
                os.environ["JUDGMENTLLM_VERTEX_AUTH_MODE"] = vertex_auth_mode
                if vertex_auth_mode == "adc":
                    continue
            value = _lookup(provider)
            try:
                os.environ[PROVIDERS[provider]] = value
                _resolve_credentials(provider)
            except ValueError:
                raise KeyringError(
                    "Required provider credential is empty or malformed."
                ) from None
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        signal.signal(signal.SIGTERM, prior_handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", action="append", choices=PROVIDERS, required=True)
    parser.add_argument(
        "--vertex-auth-mode", choices=("api_key", "adc"), default="api_key"
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if command[:1] == ["judgmentllm"]:
        command = command[1:]
    if not command or any(a.startswith("--keyring-provider") for a in command):
        parser.error("Supply JudgmentLLM arguments or study arguments after --.")
    try:
        if sys.platform != "linux":
            raise KeyringError(
                "This launcher requires Linux with GNOME Secret Service."
            )
        if command[0] == "study":
            # No key is read here: the study executor owns authorization and lookup order.
            if command[1:2] in (["execute"], ["recover"]):
                for provider in dict.fromkeys(args.provider):
                    command.extend(("--keyring-provider", provider))
            environment = {
                k: v for k, v in os.environ.items() if k not in _KEY_VARIABLES
            }
            os.execve(
                sys.executable,
                [sys.executable, "-m", "judgmentllm.study", *command[1:]],
                environment,
            )
        else:
            with credentials(args.provider, vertex_auth_mode=args.vertex_auth_mode):
                # Replace this process: the CLI owns its exit status, signals and cleanup.
                os.execve(
                    sys.executable,
                    [
                        sys.executable,
                        "-c",
                        "from judgmentllm.cli import app; app()",
                        *command,
                    ],
                    dict(os.environ),
                )
    except (KeyringError, OSError) as error:
        sys.stderr.write(
            str(error) + "\n"
            if isinstance(error, KeyringError)
            else "Unable to start JudgmentLLM.\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
