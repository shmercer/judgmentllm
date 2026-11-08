"""Development, complete local PR, and offline release qualification entry point."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fast(changed_since: str, output: Path) -> int:
    output = output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("Development evidence must be outside the source tree")
    python = (
        ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if not python.is_file():
        raise ValueError(
            "Prepare the locked development environment with uv sync first"
        )
    output.mkdir(parents=True, exist_ok=False)
    changed = subprocess.check_output(
        ["git", "diff", "--name-only", changed_since, "--"], cwd=ROOT, text=True
    ).splitlines()
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=ROOT, text=True
    ).splitlines()
    selected = {
        "tests/test_models.py",
        "tests/test_config.py",
        "tests/test_results.py",
        "tests/test_package.py",
        "tests/test_public_lifecycle.py",
        "tests/test_auth_privacy.py",
    }
    expanded = False
    for name in [*changed, *untracked]:
        path = Path(name)
        if name.startswith("tests/test_") and path.suffix == ".py":
            selected.add(name)
        elif name.startswith("ci/") or name.startswith(".github/"):
            selected.update(
                str(p.relative_to(ROOT)) for p in (ROOT / "ci/local").glob("test_*.py")
            )
        elif (
            name.startswith("src/")
            or name.startswith("scripts/")
            or name in {"pyproject.toml", "uv.lock"}
        ):
            # Unknown dependencies must expand selection, rather than silently
            # claiming that a guessed component mapping establishes protection.
            expanded = True
    targets = ["tests", "ci/local"] if expanded else sorted(selected)
    commands = [
        [str(python), "-m", "ruff", "check", "."],
        [str(python), "-m", "ruff", "format", "--check", "."],
        [str(python), "-m", "pyright"],
        [
            str(python),
            "-m",
            "pytest",
            "--no-cov",
            "-ra",
            *targets,
            "-m",
            "not resource and not serial",
            f"--basetemp={output / 'tmp'}",
        ],
    ]
    environment = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "SystemRoot",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
        )
        if key in os.environ
    }
    environment.update(
        PATH=str(python.parent) + os.pathsep + environment.get("PATH", ""),
        PYRIGHT_PYTHON_USE_BUNDLED_PYRIGHT="1",
        PYRIGHT_PYTHON_NODEJS_WHEEL="1",
        PYRIGHT_PYTHON_GLOBAL_NODE="0",
        PYRIGHT_PYTHON_IGNORE_WARNINGS="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    records: list[dict[str, object]] = []
    for index, command in enumerate(commands):
        with (output / f"{index + 1}.log").open("w", encoding="utf-8") as stream:
            code = subprocess.run(
                command,
                cwd=ROOT,
                env=environment,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            ).returncode
        records.append({"command": command, "exit_code": code})
        if code:
            break
    report = {
        "schema_version": 1,
        "profile": "fast",
        "qualification": False,
        "expanded_selection": expanded,
        "targets": targets,
        "steps": records,
        "passed": all(record["exit_code"] == 0 for record in records),
    }
    (output / "summary.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Fast development evidence: {output}; complete qualification: false")
    return int(not report["passed"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("fast", "pr", "release"), default="fast")
    parser.add_argument("--changed-since", default="origin/main")
    parser.add_argument("--ref")
    parser.add_argument("--mode", choices=("prepare", "check"), default="check")
    parser.add_argument(
        "--schedule",
        choices=("baseline", "parallel", "staged", "resource-aware", "isolated"),
        default="parallel",
    )
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    if options.profile == "fast":
        output = options.output or Path(tempfile.gettempdir()) / (
            "judgmentllm-fast-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        )
        return fast(options.changed_since, output)
    if options.ref is None:
        parser.error("complete profiles require --ref FULL_COMMIT_SHA")
    command = [
        sys.executable,
        str(ROOT / "ci/local/grid.py"),
        "--ref",
        options.ref,
        "--mode",
        options.mode,
        "--schedule",
        options.schedule,
    ]
    if options.output:
        command.extend(["--result-root", str(options.output)])
    code = subprocess.run(command, cwd=ROOT, check=False).returncode
    if options.profile == "release" and code == 0 and options.mode == "check":
        # Offline CI cannot establish outstanding provider families, independent
        # usability review or authorization of exact release artifacts.
        print(
            "Offline matrix finished. Release acceptance remains pending the existing alpha-release-readiness checklist and exact-source compatibility/scale evidence. No provider call or publication was performed."
        )
        return 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())
