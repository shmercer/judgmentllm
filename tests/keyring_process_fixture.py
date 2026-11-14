"""Interrupt a synthetic lookup child; never invoke gdbus or secret-tool."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import os
import signal
import sys
import threading
from pathlib import Path

from judgmentllm import keyring


def lookup(provider: str) -> str:
    # The parent handler must unwind subprocess.run and reap this exact child.
    timer = threading.Timer(0.5, lambda: signal.raise_signal(signal.SIGTERM))
    timer.start()
    try:
        keyring._run(
            [
                sys.executable,
                "-c",
                "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)",
                sys.argv[1],
            ]
        )
    finally:
        timer.cancel()
    raise AssertionError("The synthetic lookup was not terminated")


if __name__ == "__main__":
    for name in keyring._KEY_VARIABLES:
        os.environ.pop(name, None)
    keyring._lookup = lookup
    try:
        with keyring.credentials(("openai",)):
            raise AssertionError("A terminated lookup exposed a credential")
    except SystemExit as error:
        assert "OPENAI_API_KEY" not in os.environ
        Path(sys.argv[1] + ".cleaned").write_text("environment restored")
        raise error
