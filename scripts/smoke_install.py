"""Install built distributions into clean uv environments and verify the core CLI.

Run after syncing development dependencies and building distributions. Installation
may download packages at the core dependency versions exported from uv.lock.
Constraint export and installed-package checks remain offline.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from click import unstyle

VERIFY_INSTALLED = """
import importlib.metadata as metadata
import importlib.resources as resources
from pathlib import Path
import sys

import judgmentllm

distribution = metadata.distribution("judgmentllm")
assert judgmentllm.__version__ == distribution.version
assert Path(judgmentllm.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert resources.files("judgmentllm").joinpath("py.typed").is_file()
assert distribution.metadata["License-Expression"] == "MIT"
assert any(path.name == "LICENSE" for path in distribution.files or [])
entries = list(metadata.entry_points(group="console_scripts", name="judgmentllm"))
assert len(entries) == 1 and callable(entries[0].load())
for name in (
    "openai", "anthropic", "google-genai", "together", "ollama",
    "typesafe-sdk", "pyarrow", "fastapi", "uvicorn",
):
    try:
        metadata.distribution(name)
    except metadata.PackageNotFoundError:
        continue
    raise AssertionError(f"Optional dependency installed in the core environment: {name}")
print(distribution.version)
"""


def run(command: list[str], *, cwd: Path, offline: bool = True) -> str:
    """Run offline by default, retaining diagnostics if verification fails."""
    result = subprocess.run(
        command,
        cwd=cwd,
        env={
            **os.environ,
            "UV_OFFLINE": "1" if offline else "0",
            "NO_COLOR": "1",
            "TERM": "dumb",
        },
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"{result.stdout}{result.stderr}"
        )
    return result.stdout


def smoke_install(uv: str, artifact: Path, constraints: Path, directory: Path) -> None:
    """Verify one non-editable artifact installation outside the checkout."""
    environment = directory / "venv"
    run([uv, "venv", "--python", sys.executable, str(environment)], cwd=directory)
    binaries = environment / ("Scripts" if os.name == "nt" else "bin")
    python = binaries / ("python.exe" if os.name == "nt" else "python")
    console = binaries / ("judgmentllm.exe" if os.name == "nt" else "judgmentllm")
    run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--constraint",
            str(constraints),
            str(artifact),
        ],
        cwd=directory,
        offline=False,
    )
    version = run([str(python), "-I", "-c", VERIFY_INSTALLED], cwd=directory).strip()
    help_text = unstyle(run([str(console), "--help"], cwd=directory))
    if "--help" not in help_text or "--version" not in help_text:
        raise RuntimeError("Installed CLI help does not expose the supported options")
    version_text = run([str(console), "--version"], cwd=directory).strip()
    if version_text != f"judgmentllm {version}":
        raise RuntimeError(f"Installed CLI version mismatch: {version_text!r}")
    print(f"Smoke installation passed: {artifact.name} (version {version})")


def main() -> None:
    """Resolve artifact globs on every platform and smoke-test each artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", help="Wheel or sdist paths/globs")
    patterns: list[str] = parser.parse_args().artifacts
    artifacts: set[Path] = set()
    for pattern in patterns:
        matches = glob.glob(pattern)
        if not matches:
            parser.error(f"No distribution matches {pattern!r}")
        for match in matches:
            path = Path(match).resolve()
            if not path.is_file() or not path.name.endswith((".whl", ".tar.gz")):
                parser.error(f"Expected a wheel or source distribution: {path}")
            artifacts.add(path)
    uv = shutil.which("uv")
    if uv is None:
        parser.error("uv must be installed and available on PATH")

    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="judgmentllm-install-") as temporary:
        temporary_path = Path(temporary)
        constraints = temporary_path / "core-constraints.txt"
        run(
            [
                uv,
                "export",
                "--locked",
                "--no-default-groups",
                "--no-emit-project",
                "--no-hashes",
                "--output-file",
                str(constraints),
            ],
            cwd=repository,
        )
        for index, artifact in enumerate(sorted(artifacts)):
            directory = temporary_path / str(index)
            directory.mkdir()
            smoke_install(uv, artifact, constraints, directory)


if __name__ == "__main__":
    main()
