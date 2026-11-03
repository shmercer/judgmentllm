"""Small content/environment identity for comparable resumable measurements."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import sqlite3
from collections.abc import Iterable
from pathlib import Path


def _fingerprint(root: Path, files: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def source_fingerprint(root: Path) -> str:
    files = [
        path
        for path in (root / "src" / "judgmentllm").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]
    files.extend(root / name for name in ("pyproject.toml", "uv.lock"))
    return _fingerprint(root, files)


def harness_fingerprint(directory: Path | None = None) -> str:
    """Hash performance tools/RSS helper, excluding the separate parity program."""
    directory = directory or Path(__file__).resolve().parent
    files = [
        directory / name
        for name in (
            "execution_benchmark.py",
            "execution_worker.py",
            "instrumentation.py",
            "provenance.py",
            "workloads.py",
        )
    ]
    files.append(directory.parent / "scripts" / "preparation_memory_fixture.py")
    return _fingerprint(directory.parent, files)


def fixture_fingerprint() -> str:
    directory = Path(__file__).resolve().parent
    return _fingerprint(directory, [directory / "workloads.py"])


def input_fingerprint(directory: Path) -> str:
    return _fingerprint(
        directory, (path for path in directory.iterdir() if path.is_file())
    )


def runtime_identity() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        **{
            name: importlib.metadata.version(name)
            for name in ("pydantic", "pydantic_core", "Jinja2", "PyYAML", "coverage")
        },
    }
