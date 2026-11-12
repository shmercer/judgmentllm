"""Execute and aggregate the existing checks according to a recorded risk plan."""

from __future__ import annotations

import argparse
import glob
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from risk import R_GATES, admit, git, make_plan, required_steps

HERE = Path(__file__).resolve().parent


def save(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


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


def run_lane(
    plan: dict[str, Any],
    source: Path,
    output: Path,
    lane: str,
    run_id: str,
    *,
    workers: int = 4,
    quality_only: bool = False,
    quality_receipt: Path | None = None,
    execution_scope: str = "hosted",
) -> int:
    if execution_scope not in {"local", "hosted"}:
        raise ValueError("Execution scope must be local or hosted")
    if execution_scope == "local" and os.environ.get("GITHUB_ACTIONS") == "true":
        raise ValueError("Local resource measurements cannot run in GitHub Actions")
    source, output = source.resolve(), output.resolve()
    if output.is_relative_to(source):
        raise ValueError("Evidence must be outside the checkout")
    expected = next(item for item in plan["lanes"] if item["id"] == lane)
    systems = {
        "ubuntu-latest": "Linux",
        "ubuntu-24.04": "Linux",
        "windows-latest": "Windows",
        "macos-latest": "Darwin",
    }
    if platform.system() != systems[expected["os"]] or (
        f"{sys.version_info.major}.{sys.version_info.minor}" != expected["python"]
    ):
        raise ValueError("Lane interpreter/OS does not match its plan")
    output.mkdir(parents=True, exist_ok=False)
    needed = required_steps(plan, lane, execution_scope=execution_scope)
    report: dict[str, Any] = {
        "source_sha": plan["source_sha"],
        "plan_sha256": plan["plan_sha256"],
        "lane": lane,
        "run_id": run_id,
        "execution_scope": execution_scope,
        "status": "failed",
        "source_verified": False,
        "steps": [],
        "deferred": {
            **plan["deferred"],
            "resource_measurements": execution_scope == "hosted" and plan["resources"],
        },
        "risk": plan["risk"],
        "python": sys.version,
        "os": platform.platform(),
        "phase": "quality" if quality_only else "complete",
    }
    environment = clean_environment()
    environment["JUDGMENTLLM_CI_RUN_ID"] = f"{run_id}:{lane}"
    if any(step.startswith("r-") for step in needed):
        environment["JUDGMENTLLM_R_STRICT"] = "1"
    environment["JUDGMENTLLM_OPENAI_RESOURCE_EVIDENCE"] = str(
        output / "openai-resources"
    )
    environment["RUFF_CACHE_DIR"] = str(output / "ruff-cache")
    started = time.monotonic()
    python = sys.executable
    receipt_path = output / ("quality.json" if quality_only else "receipt.json")
    cheap = needed & {"docs", "ruff", "format", "pyright", "infrastructure"}

    def step(name: str, command: list[str]) -> None:
        before = time.monotonic()
        print(f"{name}: running", flush=True)
        with (output / f"{name}.log").open("w", encoding="utf-8") as stream:
            code = subprocess.run(
                command,
                cwd=source,
                env=environment,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            ).returncode
        report["steps"].append(
            {
                "name": name,
                "command": command,
                "exit_code": code,
                "seconds": time.monotonic() - before,
            }
        )
        save(receipt_path, report)
        if code:
            raise ValueError(f"{name} failed; see {output / (name + '.log')}")

    def verify_source() -> None:
        if git(source, "rev-parse", "HEAD") != plan["source_sha"] or git(
            source, "status", "--porcelain", "--untracked-files=normal"
        ):
            raise ValueError(
                "Source must be clean and match the planned immutable head"
            )

    try:
        verify_source()
        if quality_receipt is not None and lane == "linux-3.11":
            prior = json.loads(quality_receipt.read_text(encoding="utf-8"))
            if (
                any(
                    prior.get(key) != report[key]
                    for key in (
                        "source_sha",
                        "plan_sha256",
                        "lane",
                        "run_id",
                        "execution_scope",
                    )
                )
                or prior.get("phase") != "quality"
                or prior.get("status") != "passed"
                or prior.get("source_verified") is not True
                or {item["name"] for item in prior["steps"]} != cheap
                or any(item["exit_code"] != 0 for item in prior["steps"])
            ):
                raise ValueError("Quality receipt is missing, failed or stale")
            report["steps"] = prior["steps"]
            needed -= cheap
        if quality_only:
            needed = cheap
        if "docs" in needed:
            step(
                "docs",
                [
                    python,
                    str(HERE / "docs_check.py"),
                    str(source),
                    *(item["path"] for item in plan["changes"]),
                ],
            )
        changed_python = [
            item["path"]
            for item in plan["changes"]
            if item["path"].endswith(".py") and (source / item["path"]).is_file()
        ]
        if "ruff" in needed:
            targets = changed_python if plan["risk"] == 1 and changed_python else ["."]
            step("ruff", [python, "-m", "ruff", "check", *targets])
            step("format", [python, "-m", "ruff", "format", "--check", *targets])
            step(
                "pyright",
                [
                    python,
                    "-m",
                    "pyright",
                    *(changed_python if plan["risk"] == 1 else []),
                ],
            )
        if "infrastructure" in needed:
            step(
                "infrastructure", [python, "-m", "pytest", "--no-cov", "-q", "ci/local"]
            )
        if "build" in needed:
            step("build", ["uv", "build", "--offline"])
            step(
                "metadata",
                [
                    python,
                    "-m",
                    "twine",
                    "check",
                    "--strict",
                    *sorted(glob.glob(str(source / "dist/*"))),
                ],
            )
            for kind, pattern in (("wheel", "*.whl"), ("sdist", "*.tar.gz")):
                artifacts = list((source / "dist").glob(pattern))
                if len(artifacts) != 1:
                    raise ValueError(f"Expected exactly one {kind}")
                step(
                    kind,
                    [
                        python,
                        "scripts/smoke_install.py",
                        str(artifacts[0]),
                        "--offline",
                    ],
                )
        if "tests" in needed:
            command = [
                python,
                str(HERE / "local/suite.py"),
                "--source",
                str(source),
                "--output",
                str(output / "suite"),
                "--workers",
                str(workers),
                "--bound-workers",
                "--contract",
                str(HERE / "test-contract.json"),
            ]
            if "coverage" in needed:
                command += [
                    "--baseline",
                    str(HERE / "coverage-floor.json"),
                    "--coverage-ref",
                    plan["source_sha"],
                ]
            else:
                command.append("--no-coverage")
            if "resources" not in needed:
                command.append("--skip-resources")
            command += plan["targets"]
            step("tests", command)
            suite = json.loads(
                (output / "suite/summary.json").read_text(encoding="utf-8")
            )
            if (
                suite["status"] != "passed"
                or not suite["selection_complete"]
                or suite["source"]["commit"] != plan["source_sha"]
                or suite["source"]["dirty"] is not False
            ):
                raise ValueError("Incomplete or mismatched test evidence")
            for alias in ("coverage", "resources"):
                if alias in needed:
                    if (
                        alias == "coverage"
                        and suite.get("coverage", {}).get("passed") is not True
                    ):
                        raise ValueError("Missing passing coverage evidence")
                    if alias == "resources" and suite.get("deferred_resource_cases"):
                        raise ValueError("Required resource cases were deferred")
                    report["steps"].append(
                        {
                            "name": alias,
                            "exit_code": 0,
                            "evidence": "suite/summary.json",
                        }
                    )
        # Each named gate has a single receipt. All persist/export/assertion
        # subprocesses must finish successfully to produce it.
        for name in R_GATES:
            if f"r-{name}" not in needed:
                continue
            step(
                f"r-{name}",
                [
                    python,
                    str(HERE / "acceptance.py"),
                    "r-gate",
                    "--name",
                    name,
                    "--source",
                    str(source),
                    "--output",
                    str(output / ("r-" + name)),
                ],
            )
        verify_source()
        if not needed <= {item["name"] for item in report["steps"]}:
            raise ValueError("Required steps were omitted")
        report.update(status="passed", source_verified=True)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        report["error"] = str(error)
        print(error, file=sys.stderr)
    finally:
        report["seconds"] = time.monotonic() - started
        save(receipt_path, report)
    return int(report["status"] != "passed")


def r_gate(name: str, source: Path, output: Path) -> int:
    fixtures = {
        "exports": ("examples/export_r.py", "examples/r_interoperability.R"),
        "persistence": (
            "scripts/persistence_r_fixture.py",
            "examples/persistence_interoperability.R",
        ),
        "generation": (
            "scripts/generation_r_fixture.py",
            "examples/generation_interoperability.R",
        ),
        "source-preparation": (
            "scripts/source_preparation_r_fixture.py",
            "examples/source_preparation_interoperability.R",
        ),
        "execution": (
            "scripts/execution_r_fixture.py",
            "examples/execution_interoperability.R",
        ),
        "grouped-lifecycle": (
            "scripts/grouped_lifecycle_r_fixture.py",
            "examples/grouped_lifecycle_interoperability.R",
        ),
    }
    environment = clean_environment()
    environment["JUDGMENTLLM_R_STRICT"] = "1"
    if name == "cli-lifecycle":
        commands = [
            [sys.executable, "scripts/cli_lifecycle_r_gate.py", "--output", str(output)]
        ]
    else:
        fixture, assertions = fixtures[name]
        commands = [
            [sys.executable, fixture, "--output", str(output), *phase]
            for phase in (
                [[]]
                if name == "exports"
                else [["--phase", "persist"], ["--phase", "export"]]
            )
        ]
        commands.append(["Rscript", "--vanilla", assertions, str(output)])
    for command in commands:
        subprocess.run(command, cwd=source, env=environment, check=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "lane", "aggregate", "r-gate"))
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--risk", type=int, choices=range(5), default=0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--lane", default="linux-3.11")
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--receipts", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--name", choices=R_GATES)
    parser.add_argument("--quality-only", action="store_true")
    parser.add_argument("--quality-receipt", type=Path)
    parser.add_argument(
        "--execution-scope", choices=("local", "hosted"), default="hosted"
    )
    args = parser.parse_args()
    if args.operation in {"plan", "aggregate"} and args.base:
        # Dispatch has no event.before SHA. Resolve main to a literal commit
        # before classification; never compare a feature branch to itself.
        args.base = git(args.source, "rev-parse", args.base)
    if args.operation == "plan":
        plan = make_plan(args.source, args.base, args.head, minimum=args.risk)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        save(args.output, plan)
        print(json.dumps(plan, indent=2))
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write("matrix=" + json.dumps({"include": plan["lanes"]}) + "\n")
                stream.write(f"risk={plan['risk']}\n")
                stream.write(f"r_required={str(bool(plan['r_gates'])).lower()}\n")
        return 0
    if args.operation == "r-gate":
        return r_gate(args.name, args.source, args.output)
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if args.operation == "lane":
        return run_lane(
            plan,
            args.source,
            args.output,
            args.lane,
            args.run_id,
            workers=args.workers,
            quality_only=args.quality_only,
            quality_receipt=args.quality_receipt,
            execution_scope=args.execution_scope,
        )
    if not args.base or not args.head:
        parser.error("aggregation requires independently supplied --base and --head")
    expected_plan = make_plan(args.source, args.base, args.head, minimum=args.risk)
    if plan != expected_plan:
        raise ValueError("Plan artifact differs from protected-base classification")
    receipts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in args.receipts.rglob("receipt.json")
    ]
    errors = admit(plan, receipts, args.run_id)
    save(
        args.output,
        {
            "source_sha": plan["source_sha"],
            "risk": plan["risk"],
            "passed": not errors,
            "errors": errors,
        },
    )
    print(json.dumps({"passed": not errors, "errors": errors}))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
