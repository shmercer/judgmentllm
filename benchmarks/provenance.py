"""Small content/environment identity for comparable resumable measurements."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import sqlite3
import subprocess
from collections.abc import Iterable
from pathlib import Path


def _fingerprint(root: Path, files: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                digest.update(chunk)
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
            "scale_execution.py",
            "scale_verify.py",
            "storage.py",
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


def host_identity(root: Path) -> dict[str, object]:
    """Full reproducibility metadata, outside measured execution intervals."""
    import os

    def git(*arguments: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *arguments],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    memory: int | None = None
    if os.name != "nt" and hasattr(os, "sysconf"):
        try:
            memory = int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
        except (OSError, ValueError):
            pass
    archive = root / ".judgmentllm-source.json"
    archived_source = (
        json.loads(archive.read_text(encoding="utf-8")) if archive.exists() else None
    )
    cpu = platform.processor()
    cpuinfo = Path("/proc/cpuinfo")
    if not cpu and cpuinfo.exists():
        cpu = next(
            (
                line.partition(":")[2].strip()
                for line in cpuinfo.read_text().splitlines()
                if line.startswith("model name")
            ),
            "",
        )
    return {
        "git_commit": git("rev-parse", "HEAD")
        or (archived_source.get("commit") if archived_source else None),
        "git_tracked_status": git("status", "--porcelain", "--untracked-files=no"),
        "archived_source": archived_source,
        "cpu": cpu,
        "logical_cpu_count": os.cpu_count(),
        "reported_ram_bytes": memory,
        "dependencies": sorted(
            f"{distribution.metadata['Name']}=={distribution.version}"
            for distribution in importlib.metadata.distributions()
        ),
    }
