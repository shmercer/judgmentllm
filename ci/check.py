"""Fast feedback, risk-based PR verification, and comprehensive qualification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fast(changed_since: str, output: Path, focused: list[str], workers: int) -> int:
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
    # An explicit implementation-cycle target is a caller choice. Essential
    # short regressions always remain; neither selection establishes PR acceptance.
    if focused:
        targets = sorted(
            selected.intersection(
                {
                    "tests/test_models.py",
                    "tests/test_config.py",
                    "tests/test_results.py",
                    "tests/test_package.py",
                    "tests/test_public_lifecycle.py",
                    "tests/test_auth_privacy.py",
                }
            )
            | set(focused)
        )
    else:
        targets = ["tests", "ci/local"] if expanded else sorted(selected)
    commands = [
        [str(python), "-m", "ruff", "check", "."],
        [str(python), "-m", "ruff", "format", "--check", "."],
        [str(python), "-m", "pyright"],
        [
            str(python),
            "-m",
            "pytest",
            "-p",
            "ci.local.pytest_report",
            f"--ci-report={output / 'cases.json'}",
            "--no-cov",
            "-ra",
            *targets,
            "-m",
            "not resource and not serial",
            "-n",
            str(workers),
            "--dist=loadfile",
            "--max-worker-restart=0",
            f"--basetemp={output / 'tmp'}",
            "-o",
            f"cache_dir={output / 'cache'}",
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
        RUFF_CACHE_DIR=str(output / "ruff-cache"),
    )
    records: list[dict[str, object]] = []
    started = time.monotonic()
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
        "explicit_targets": focused,
        "workers": workers,
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "working_tree_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
        "lockfile_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "wall_seconds": time.monotonic() - started,
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
    parser.add_argument(
        "--profile", choices=("fast", "pr", "comprehensive", "release"), default="fast"
    )
    parser.add_argument(
        "--risk",
        type=int,
        choices=range(5),
        default=0,
        help="Minimum risk level; can only strengthen automatic classification",
    )
    parser.add_argument(
        "--plan-only", action="store_true", help="Record a PR plan without running it"
    )
    parser.add_argument("--changed-since", default="origin/main")
    parser.add_argument(
        "--test",
        action="append",
        default=[],
        help="Explicit fast-profile pytest target, repeatable; short core regressions remain",
    )
    parser.add_argument(
        "--workers", type=int, default=4, help="Explicit fast-profile worker count"
    )
    parser.add_argument("--ref")
    parser.add_argument("--mode", choices=("prepare", "check"), default="check")
    parser.add_argument(
        "--schedule",
        choices=("baseline", "parallel", "staged", "resource-aware", "isolated"),
        default="parallel",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--baseline-python",
        type=Path,
        help="Release compatibility: genuine installed a2 interpreter",
    )
    parser.add_argument(
        "--candidate-python",
        type=Path,
        help="Release compatibility: exact-head installed candidate interpreter",
    )
    parser.add_argument("--baseline-source-archive", type=Path)
    parser.add_argument("--candidate-source-archive", type=Path)
    parser.add_argument(
        "--release-evidence",
        type=Path,
        help="External output for offline release compatibility and source/scale checklist",
    )
    options = parser.parse_args()
    if not 1 <= options.workers <= 32:
        parser.error("--workers must be 1 through 32")
    if options.profile == "fast":
        output = options.output or Path(tempfile.gettempdir()) / (
            "judgmentllm-fast-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        )
        return fast(options.changed_since, output, options.test, options.workers)
    if options.profile == "pr":
        from acceptance import save
        from risk import git, make_plan

        head = options.ref or git(ROOT, "rev-parse", "HEAD")
        base = git(ROOT, "rev-parse", options.changed_since)
        plan = make_plan(ROOT, base, head, minimum=options.risk)
        output = options.output or Path(tempfile.gettempdir()) / (
            "judgmentllm-pr-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        )
        if output.resolve().is_relative_to(ROOT):
            parser.error("Evidence must be outside the checkout")
        output.mkdir(parents=True, exist_ok=False)
        save(output / "plan.json", plan)
        print(f"Risk {plan['risk']}; plan: {output / 'plan.json'}", flush=True)
        if options.plan_only:
            return 0
        python = (
            ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        code = subprocess.run(
            [
                str(python) if python.is_file() else sys.executable,
                str(ROOT / "ci/acceptance.py"),
                "lane",
                "--source",
                str(ROOT),
                "--plan",
                str(output / "plan.json"),
                "--output",
                str(output / "primary"),
                "--workers",
                str(options.workers),
                "--run-id",
                "local",
            ],
            cwd=ROOT,
            check=False,
        ).returncode
        pending = [item["id"] for item in plan["lanes"] if item["id"] != "linux-3.11"]
        print(
            f"Primary result: {code}; pending hosted lanes: {pending}. "
            "Deferred verification is not passed verification.",
            flush=True,
        )
        return code
    if options.ref is None:
        parser.error("complete profiles require --ref FULL_COMMIT_SHA")
    if options.profile == "release" and options.mode == "check":
        for name in (
            "baseline_python",
            "candidate_python",
            "baseline_source_archive",
            "candidate_source_archive",
            "release_evidence",
        ):
            if getattr(options, name) is None:
                parser.error(
                    f"release checks require --{name.replace('_', '-')}; see docs/ci-optimization.md"
                )
        with tarfile.open(options.candidate_source_archive) as archive:
            if archive.pax_headers.get("comment") != options.ref:
                parser.error("Candidate source archive must embed the requested commit")
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
        release = options.release_evidence.resolve()
        if release.is_relative_to(ROOT):
            raise ValueError("Release evidence must be outside the worktree")
        release.mkdir(parents=True, exist_ok=False)
        compatibility = [
            sys.executable,
            str(ROOT / "scripts/qualify_version_compatibility.py"),
        ]
        for name in (
            "baseline_python",
            "candidate_python",
            "baseline_source_archive",
            "candidate_source_archive",
        ):
            compatibility.extend(
                ("--" + name.replace("_", "-"), str(getattr(options, name)))
            )
        compatibility.extend(("--output", str(release / "version-compatibility")))
        with (release / "compatibility.log").open("x", encoding="utf-8") as stream:
            compatible = subprocess.run(
                compatibility,
                cwd=ROOT,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            ).returncode
        # The established scale protocol permits reviewed carry-forward, rather
        # than repeating hours of scale work in routine CI. Retain explicit
        # unresolved checklist entries; this command never grants publication.
        report = {
            "profile": "release",
            "source_commit": options.ref,
            "offline_matrix_passed": True,
            "compatibility_exit_code": compatible,
            "candidate_archive_sha256": hashlib.sha256(
                options.candidate_source_archive.read_bytes()
            ).hexdigest(),
            "release_ready": False,
            "pending": [
                "Review exact-release scale/allocation/export evidence or its existing permitted source-equivalence mapping (docs/milestone-2-subphase-6-verification.md)",
                "Complete exact-head hosted matrix and alpha-release-readiness checklist, artifact/tag review, independent usability and already authorized live qualification",
            ],
        }
        (release / "summary.json").write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"Offline release evidence: {release}. Publication remains separately gated by the existing release checklist."
        )
        return compatible if compatible else 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())
