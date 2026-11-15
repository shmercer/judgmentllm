"""Minimal offline environment shared by trusted launchers."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def clean_environment() -> dict[str, str]:
    keys = (
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "R_LIBS",
        "R_LIBS_USER",
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "JUDGMENTLLM_ALLOCATION_ROOT",
        "JUDGMENTLLM_ALLOCATION_TOKEN",
        "JUDGMENTLLM_BACKGROUND_RUN",
        "JUDGMENTLLM_CI_ATTEMPT_ID",
    )
    result = {key: os.environ[key] for key in keys if key in os.environ}
    result.update(
        PATH=str(Path(sys.executable).parent) + os.pathsep + result.get("PATH", ""),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
        UV_OFFLINE="1",
        UV_PYTHON_DOWNLOADS="never",
        NO_COLOR="1",
        PYRIGHT_PYTHON_USE_BUNDLED_PYRIGHT="1",
        PYRIGHT_PYTHON_NODEJS_WHEEL="1",
        PYRIGHT_PYTHON_GLOBAL_NODE="0",
        PYRIGHT_PYTHON_IGNORE_WARNINGS="1",
        JUDGMENTLLM_REQUIRE_NATIVE_SDK_PROBES="1",
    )
    return result
