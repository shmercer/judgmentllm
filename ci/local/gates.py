"""Run the same development gates on an explicitly identified local source archive.

The prepare mode may populate dependency/build caches online. Check mode uses
those caches offline, including clean wheel and source installations. This is a
development tool, not an installed JudgmentLLM command. Host adapters supply an
exact interpreter and a manifest generated when staging the source archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, cast

from preflight_provenance import RUN_ID_ENV
from telemetry import ProcessSampler

MANIFEST = ".judgmentllm-source.json"
PYTHON_VERSIONS = {
    "Linux": {"3.11": "3.11.18", "3.12": "3.12.16", "3.15": "3.15.2"},
    "Windows": {"3.11": "3.11.18", "3.12": "3.12.16"},
    "Darwin": {"3.11": "3.11.18", "3.12": "3.12.16"},
}
R_PROBE = """
stopifnot(requireNamespace('jsonlite', quietly=TRUE))
stopifnot(as.character(utils::packageVersion('jsonlite')) == '2.0.0')
cat(jsonlite::toJSON(list(R=as.character(getRversion()),
    jsonlite=as.character(utils::packageVersion('jsonlite'))), auto_unbox=TRUE))
"""
R_GATES = (
    ("exports", "examples/export_r.py", "examples/r_interoperability.R"),
    (
        "persistence",
        "scripts/persistence_r_fixture.py",
        "examples/persistence_interoperability.R",
    ),
    (
        "generation",
        "scripts/generation_r_fixture.py",
        "examples/generation_interoperability.R",
    ),
    (
        "source-preparation",
        "scripts/source_preparation_r_fixture.py",
        "examples/source_preparation_interoperability.R",
    ),
    (
        "execution",
        "scripts/execution_r_fixture.py",
        "examples/execution_interoperability.R",
    ),
    (
        "grouped-lifecycle",
        "scripts/grouped_lifecycle_r_fixture.py",
        "examples/grouped_lifecycle_interoperability.R",
    ),
)


@dataclass(frozen=True)
class Options:
    source: Path
    output: Path
    commit: str
    archive_sha256: str
    python_version: str
    mode: str
    uv: str
    workers: int = 0
    resource_slots: int = 1
    resource_root: Path | None = None
    suite_stage: str = "full"
    benchmark_workers: str = ""
    distribution: str = "worksteal"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_interpreter(python_version: str) -> None:
    """Reject a substitute implementation, patch version, or unsupported OS lane."""
    expected = PYTHON_VERSIONS.get(platform.system(), {}).get(python_version)
    if (
        expected is None
        or platform.python_implementation() != "CPython"
        or platform.python_version() != expected
    ):
        raise ValueError(
            "Runner interpreter does not match the pinned CPython version "
            f"for {platform.system()}/Python {python_version}: expected {expected}, "
            f"found {platform.python_implementation()} {platform.python_version()}"
        )


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def windows_scratch_root(value: str) -> PureWindowsPath:
    """Keep Windows generated data in a short, local, project-owned namespace."""
    path = PureWindowsPath(value)
    prefix = PureWindowsPath(r"C:\JudgmentLLM-ci\tmp")
    if (
        not path.is_absolute()
        or tuple(part.casefold() for part in path.parts[: len(prefix.parts)])
        != tuple(part.casefold() for part in prefix.parts)
        or ".." in path.parts
        or any(part.endswith((" ", ".")) or ":" in part for part in path.parts[1:])
        or len(str(path)) > 32
    ):
        raise ValueError(
            "Windows scratch root must be C:\\JudgmentLLM-ci\\tmp or a short subdirectory (maximum 32 characters)"
        )
    return path


def create_scratch(root: Path) -> Path:
    """Allocate exclusively; collisions and existing evidence are never removed."""
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(16):
        path = root / ("r" + secrets.token_hex(4))
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise FileExistsError("Could not allocate a fresh project scratch directory")


def allocate_windows_scratch(environment: dict[str, str]) -> Path:
    root = Path(
        str(
            windows_scratch_root(
                environment.get("JUDGMENTLLM_CI_SCRATCH_ROOT", r"C:\JudgmentLLM-ci\tmp")
            )
        )
    )
    # Resolve junctions/symlinks before admitting an override or creating data.
    windows_scratch_root(str(root.resolve()))
    return create_scratch(root)


def verify_source(source: Path, commit: str, archive_sha256: str) -> dict[str, str]:
    """Verify metadata and every declared tracked file without trusting path escapes."""
    raw: object = json.loads((source / MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Source manifest must be a JSON object")
    manifest = cast(dict[str, Any], raw)
    if manifest.get("format_version") != 1 or (
        manifest.get("commit"),
        manifest.get("archive_sha256"),
    ) != (commit, archive_sha256):
        raise ValueError("Source manifest does not match requested commit/archive")
    raw_files: object = manifest.get("files")
    if not isinstance(raw_files, dict):
        raise ValueError("Source manifest files must be a JSON object")
    files = cast(dict[str, object], raw_files)
    if not {
        "pyproject.toml",
        "uv.lock",
        "ci/local/gates.py",
        "scripts/smoke_install.py",
    }.issubset(files):
        raise ValueError("Source manifest lacks required tracked source files")
    verified: dict[str, str] = {}
    for relative, expected in files.items():
        if not isinstance(expected, str):
            raise ValueError("Source manifest file entries must be strings")
        parts = PurePosixPath(relative).parts
        if (
            not parts
            or parts[0] in {"/", ".git"}
            or ".." in parts
            or "\\" in relative
            or ":" in relative
            or relative == MANIFEST
            or str(PurePosixPath(relative)) != relative
            or re.fullmatch(r"[0-9a-f]{64}", expected) is None
        ):
            raise ValueError(f"Invalid source manifest entry: {relative!r}")
        path = source.joinpath(*parts)
        if not path.resolve().is_relative_to(source) or any(
            source.joinpath(*parts[:index]).is_symlink()
            for index in range(1, len(parts) + 1)
        ):
            raise ValueError(
                f"Source manifest path escapes or uses a symlink: {relative}"
            )
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"Tracked source bytes differ: {relative}")
        verified[relative] = expected
    if manifest.get("lockfile_sha256") != digest(source / "uv.lock"):
        raise ValueError("Source manifest lockfile commitment differs")
    return verified


class Runner:
    def __init__(self, options: Options) -> None:
        self.source = options.source.resolve(strict=True)
        self.output = options.output.resolve()
        if self.output.is_relative_to(self.source) or self.source.is_relative_to(
            self.output
        ):
            raise ValueError("Output and source must be separate directory trees")
        self.output.mkdir(parents=True, exist_ok=False)
        (self.output / "logs").mkdir()
        self.options = options
        if options.benchmark_workers and options.suite_stage != "ordinary":
            raise ValueError(
                "Worker benchmarks require an explicitly partial ordinary stage"
            )
        if not 0 <= options.workers <= 32 or not 1 <= options.resource_slots <= 7:
            raise ValueError("Invalid pytest worker/resource allocation")
        self.windows = os.name == "nt"
        self.data_root = self.output
        self.uv = shutil.which(options.uv) or options.uv
        self.environment = {
            **os.environ,
            RUN_ID_ENV: secrets.token_hex(16),
            "UV_OFFLINE": "1" if options.mode == "check" else "0",
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_PROJECT_ENVIRONMENT": str(self.source / ".venv"),
            "UV_PYTHON": sys.executable,
            "JUDGMENTLLM_R_STRICT": "1",
            "JUDGMENTLLM_REQUIRE_NATIVE_SDK_PROBES": "1",
            "JUDGMENTLLM_OPENAI_RESOURCE_EVIDENCE": str(
                self.output / "openai-resources"
            ),
            "COVERAGE_RCFILE": str(self.source / "pyproject.toml"),
            "COVERAGE_FILE": str(self.output / ".coverage"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYRIGHT_PYTHON_IGNORE_WARNINGS": "1",
            "PYRIGHT_PYTHON_USE_BUNDLED_PYRIGHT": "1",
            "PYRIGHT_PYTHON_NODEJS_WHEEL": "1",
            "PYRIGHT_PYTHON_GLOBAL_NODE": "0",
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
        # External PYTHONPATH/PYTHONHOME must not select a different source tree.
        for key in (
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "PYTEST_ADDOPTS",
            "PYTEST_PLUGINS",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            "COVERAGE_PROCESS_START",
            "PYRIGHT_PYTHON_FORCE_VERSION",
            "PYRIGHT_PYTHON_PYLANCE_VERSION",
            "PYRIGHT_PYTHON_NODE_VERSION",
        ):
            self.environment.pop(key, None)
        self.binaries = (
            self.source / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        )
        self.python = self.binaries / ("python.exe" if os.name == "nt" else "python")
        self.environment["PATH"] = os.pathsep.join(
            (str(self.binaries), str(Path(self.uv).parent), os.environ.get("PATH", ""))
        )
        self.summary: dict[str, Any] = {
            "schema_version": 1,
            "mode": options.mode,
            "suite_stage": options.suite_stage,
            "workers": options.workers,
            "resource_slots": options.resource_slots,
            "status": "running",
            "commit": options.commit,
            "qualification_run_id": self.environment[RUN_ID_ENV],
            "archive_sha256": options.archive_sha256,
            "source": str(self.source),
            "output": str(self.output),
            "python": {
                "version": platform.python_version(),
                "executable": sys.executable,
                "implementation": platform.python_implementation(),
            },
            "os": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
            "started_at": datetime.now(UTC).isoformat(),
            "steps": [],
            "source_verified_before": False,
            "source_verified_after": False,
        }
        self.save()

    def save(self) -> None:
        atomic_json(self.output / "summary.json", self.summary)

    def step(
        self,
        name: str,
        command: list[str],
        *,
        prerequisite: bool = False,
        timeout: int | None = None,
    ) -> bool:
        """Stream both output channels to a durable per-step log, including failures."""
        log = self.output / "logs" / f"{len(self.summary['steps']) + 1:02d}-{name}.log"
        record: dict[str, Any] = {
            "name": name,
            "command": command,
            "cwd": str(self.source),
            "log": str(log.relative_to(self.output)),
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
        }
        is_suite = len(command) > 1 and Path(command[1]).name == "suite.py"
        is_pytest = command[1:3] == ["-m", "pytest"] or is_suite
        if is_pytest:
            record.update(
                coverage_enabled="--no-cov" not in command
                and "--no-coverage" not in command,
                coverage_percent=None,
                pytest_elapsed_seconds=None,
                tests={
                    outcome: None
                    for outcome in ("passed", "failed", "skipped", "errors")
                },
            )
        self.summary["steps"].append(record)
        self.save()
        print(f"{name}: running", flush=True)
        started = time.monotonic()
        try:
            if Path(__file__).resolve() != self.source / "ci/local/gates.py":
                raise ValueError(
                    "Invoke the gate runner belonging to the staged source"
                )
            with log.open("w", encoding="utf-8", newline="\n") as stream:
                try:
                    with ProcessSampler() as sampler:
                        result = subprocess.run(
                            command,
                            cwd=self.source,
                            env=self.environment,
                            stdout=stream,
                            stderr=subprocess.STDOUT,
                            timeout=timeout,
                            check=False,
                        )
                    record["process_metrics"] = sampler.report()
                    record["exit_code"] = result.returncode
                    record["status"] = "passed" if result.returncode == 0 else "failed"
                except (OSError, subprocess.TimeoutExpired) as error:
                    stream.write(f"\n{type(error).__name__}: {error}\n")
                    record.update(
                        status="failed",
                        exit_code=None,
                        failure_kind="timeout_or_resource_limit"
                        if isinstance(error, subprocess.TimeoutExpired)
                        else "environment_setup",
                    )
        except KeyboardInterrupt:
            record.update(
                status="interrupted", exit_code=None, failure_kind="interrupted"
            )
            raise
        finally:
            record["duration_seconds"] = time.monotonic() - started
            self.save()
        text = log.read_text(encoding="utf-8", errors="replace")
        if is_pytest:
            totals = [
                line
                for line in text.splitlines()
                if re.search(
                    r"\d+ (?:passed|failed|skipped|errors?).* in [\d.]+s", line
                )
            ]
            if totals:
                counts = {
                    outcome: 0 for outcome in ("passed", "failed", "skipped", "errors")
                }
                for count, outcome in re.findall(
                    r"(\d+) (passed|failed|skipped|errors?)", totals[-1]
                ):
                    counts["errors" if outcome == "error" else outcome] = int(count)
                record["tests"] = counts
                elapsed = re.search(r" in ([\d.]+)s", totals[-1])
                if elapsed:
                    record["pytest_elapsed_seconds"] = float(elapsed.group(1))
            coverage = re.search(r"Total coverage:\s*([\d.]+)%", text)
            if record["coverage_enabled"] and coverage:
                record["coverage_percent"] = float(coverage.group(1))
        if is_suite:
            suite_output = Path(command[command.index("--output") + 1])
            suite_report = suite_output / "summary.json"
            try:
                receipt = json.loads(suite_report.read_text(encoding="utf-8"))
                complete = receipt.get("selection_complete") is True
                complete = (
                    complete
                    and receipt.get("source", {}).get("commit") == self.options.commit
                )
                if self.options.suite_stage == "full":
                    complete = complete and receipt.get("manifest_complete") is True
                if not complete or receipt.get("status") != "passed":
                    record["status"] = "failed"
                record["suite"] = {
                    key: receipt.get(key)
                    for key in (
                        "status",
                        "stage",
                        "workers",
                        "manifest_complete",
                        "selection_complete",
                        "expected_cases",
                        "reused_preflight_cases",
                        "counts",
                        "partitions",
                        "coverage",
                    )
                }
                record["suite_report_sha256"] = digest(suite_report)
                # Windows uses a short private scratch directory. Preserve the
                # complete receipts before adapter cleanup; omit synthetic stores.
                evidence = self.output / "suite"
                evidence.mkdir(exist_ok=True)
                for pattern in ("*.json", "*.log"):
                    for path in suite_output.glob(pattern):
                        shutil.copyfile(path, evidence / path.name)
                if (suite_output / "resource-measurements").is_dir():
                    shutil.copytree(
                        suite_output / "resource-measurements",
                        evidence / "resource-measurements",
                    )
                for trial in suite_output.glob("w[0-9]*"):
                    if trial.is_dir():
                        destination = evidence / trial.name
                        destination.mkdir(exist_ok=True)
                        for pattern in ("*.json", "*.log"):
                            for path in trial.glob(pattern):
                                shutil.copyfile(path, destination / path.name)
                if record["coverage_enabled"]:
                    for name in (
                        "coverage.xml",
                        ".coverage",
                        "coverage-comparison.json",
                    ):
                        if (suite_output / name).exists():
                            shutil.copyfile(suite_output / name, self.output / name)
            except (OSError, ValueError, TypeError) as error:
                record.update(
                    status="failed",
                    failure_kind="missing_suite_receipt",
                    receipt_error=str(error),
                )
        if record["status"] != "passed":
            record.setdefault(
                "failure_kind",
                "timeout_or_resource_limit"
                if "TimeoutExpired" in text or "timed out after" in text
                else "environment_setup"
                if prerequisite
                else "check_failure",
            )
        self.save()
        print(
            f"{name}: {record['status']} ({record['duration_seconds']:.2f}s)",
            flush=True,
        )
        return record["status"] == "passed"

    def required(
        self, name: str, command: list[str], *, timeout: int | None = None
    ) -> None:
        if not self.step(name, command, prerequisite=True, timeout=timeout):
            raise RuntimeError(f"Prerequisite failed: {name}; see its retained log")

    def module(self, name: str, *arguments: str) -> list[str]:
        return [str(self.python), "-m", name, *arguments]

    def configure_data_paths(self) -> None:
        if not self.windows:
            return
        self.data_root = allocate_windows_scratch(self.environment)
        temporary = self.data_root / "t"
        temporary.mkdir()
        self.environment.update(
            TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary)
        )
        self.summary["scratch"] = {
            "policy": "windows-short-project-scratch-v1",
            "path": str(self.data_root),
            "temporary_directory": str(temporary),
            "r_fixture_root": str(self.data_root / "r"),
            "pytest_basetemps": {
                "durability": str(self.data_root / "d"),
                "privacy": str(self.data_root / "l"),
                "full": str(self.data_root / "p"),
            },
            "retention": "preserved; runner never removes scratch evidence",
        }
        self.save()

    def pytest_command(self, *arguments: str, scratch_name: str) -> list[str]:
        command = self.module("pytest", *arguments)
        if scratch_name in {"d", "l"}:
            command.extend(
                (
                    "-p",
                    "ci.local.pytest_report",
                    "--ci-report",
                    str(self.output / f"preflight-{scratch_name}.json"),
                )
            )
        if self.windows:
            command.extend(("--basetemp", str(self.data_root / scratch_name)))
        return command

    def build_and_smoke(self) -> None:
        artifacts = self.output / "artifacts"
        command = [
            self.uv,
            "build",
            "--python",
            sys.executable,
            "--out-dir",
            str(artifacts),
        ]
        if self.options.mode == "check":
            command.append("--offline")
        if not self.step("build", command):
            return
        wheels, sdists = list(artifacts.glob("*.whl")), list(artifacts.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ValueError("Build must produce exactly one wheel and source archive")
        self.summary["artifacts"] = [
            {"file": path.name, "sha256": digest(path)} for path in (*wheels, *sdists)
        ]
        self.step(
            "distribution-metadata",
            self.module("twine", "check", "--strict", str(wheels[0]), str(sdists[0])),
        )
        for label, path in (("wheel", wheels[0]), ("source", sdists[0])):
            command = [
                str(self.python),
                str(self.source / "scripts/smoke_install.py"),
                str(path),
            ]
            if self.options.mode == "check":
                command.append("--offline")
            self.step(f"clean-{label}-installation", command)

    def run(self) -> int:
        started = time.monotonic()
        completed = False
        phase = "source-verification"
        try:
            files = verify_source(
                self.source, self.options.commit, self.options.archive_sha256
            )
            self.summary.update(
                source_verified_before=True,
                tracked_file_count=len(files),
                source_manifest_sha256=digest(self.source / MANIFEST),
                lockfile_sha256=digest(self.source / "uv.lock"),
            )
            phase = "environment-setup"
            self.configure_data_paths()
            verify_interpreter(self.options.python_version)
            self.summary["interpreter_verified"] = True
            self.required("uv-version", [self.uv, "--version"], timeout=30)
            self.summary["uv"] = (
                (self.output / self.summary["steps"][-1]["log"])
                .read_text(encoding="utf-8")
                .strip()
            )
            sync = [
                self.uv,
                "sync",
                "--locked",
                "--all-extras",
                "--group",
                "dev",
                "--python",
                sys.executable,
            ]
            if self.options.mode == "check":
                sync.append("--offline")
            self.required("locked-sync", sync)
            self.required(
                "dependency-versions",
                [
                    str(self.python),
                    "-c",
                    "import importlib.metadata as m,json; print(json.dumps({d.metadata['Name']:d.version for d in m.distributions()},sort_keys=True))",
                ],
            )
            self.summary["dependencies"] = json.loads(
                (self.output / self.summary["steps"][-1]["log"]).read_text(
                    encoding="utf-8"
                )
            )
            self.required(
                "bundled-type-checker",
                [
                    str(self.python),
                    "-c",
                    "import pyright,nodejs_wheel; from pathlib import Path; "
                    "assert Path(pyright.__file__).parent.joinpath('dist/index.js').is_file()",
                ],
            )
            rscript = shutil.which("Rscript", path=self.environment["PATH"])
            if rscript is None:
                raise ValueError(
                    "Required Rscript is unavailable; provision R/jsonlite before running gates"
                )
            # Windows Rscript can crash on multiline code supplied through -e.
            # Keep the exact probe as a retained UTF-8 script on every platform.
            r_probe = self.output / "r-probe.R"
            r_probe.write_text(R_PROBE, encoding="utf-8", newline="\n")
            self.required(
                "r-jsonlite-versions", [rscript, "--vanilla", str(r_probe)], timeout=30
            )
            self.summary["r"] = json.loads(
                (self.output / self.summary["steps"][-1]["log"]).read_text(
                    encoding="utf-8"
                )
            )
            phase = self.options.mode
            if self.options.mode == "prepare":
                self.required("warm-pyright-node", self.module("pyright", "--version"))
                self.build_and_smoke()
            else:
                self.check(rscript)
            completed = True
        except (Exception, KeyboardInterrupt) as error:
            self.summary["error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "phase": phase,
            }
        finally:
            try:
                verify_source(
                    self.source, self.options.commit, self.options.archive_sha256
                )
                if self.summary.get("source_manifest_sha256") != digest(
                    self.source / MANIFEST
                ):
                    raise ValueError("Source manifest changed during gates")
                self.summary["source_verified_after"] = True
            except (OSError, ValueError) as error:
                self.summary["source_integrity_error"] = str(error)
            failed = any(step["status"] != "passed" for step in self.summary["steps"])
            passed = (
                completed
                and not failed
                and "error" not in self.summary
                and self.summary["source_verified_before"]
                and self.summary["source_verified_after"]
            )
            self.summary.update(
                status="passed" if passed else "failed",
                finished_at=datetime.now(UTC).isoformat(),
                duration_seconds=time.monotonic() - started,
                all_scheduled_gates_finished=completed,
            )
            self.save()
        return 0 if self.summary["status"] == "passed" else 1

    def check_suite(self) -> None:
        coverage_enabled = (
            platform.system() == "Linux"
            and self.options.python_version == "3.11"
            and self.options.suite_stage == "full"
        )
        command = [
            str(self.python),
            str(self.source / "ci/local/suite.py"),
            "--source",
            str(self.source),
            "--output",
            str(self.data_root / "p"),
            "--workers",
            str(self.options.workers),
            "--resource-slots",
            str(self.options.resource_slots),
            "--stage",
            self.options.suite_stage,
            "--contract",
            str(self.source / "ci/test-contract.json"),
        ]
        if self.windows and self.options.suite_stage == "full":
            for name in ("d", "l"):
                command.extend(
                    ("--preflight-report", str(self.output / f"preflight-{name}.json"))
                )
        command.extend(("--distribution", self.options.distribution))
        if self.options.benchmark_workers:
            command.extend(("--benchmark-workers", self.options.benchmark_workers))
        if self.options.resource_root is not None:
            command.extend(("--resource-root", str(self.options.resource_root)))
        if coverage_enabled:
            command.extend(("--baseline", str(self.source / "ci/coverage-floor.json")))
        else:
            command.append("--no-coverage")
        command.extend(
            ("tests", "ci/local/test_gates.py", "ci/local/test_optimization.py")
        )
        self.step(
            "pytest-coverage" if coverage_enabled else "pytest-no-coverage", command
        )

    def check(self, rscript: str) -> None:
        if self.options.suite_stage != "full":
            self.check_suite()
            return
        self.step("ruff-lint", self.module("ruff", "check", "."))
        self.step("ruff-format", self.module("ruff", "format", "--check", "."))
        self.step("strict-pyright", self.module("pyright"))
        warnings = [
            "-Werror::ResourceWarning",
            "-Werror::pytest.PytestUnraisableExceptionWarning",
        ]
        if self.windows:
            self.step(
                "windows-durability-cancellation",
                self.pytest_command(
                    "--no-cov",
                    "-q",
                    "-rP",
                    "tests/test_export_durability.py",
                    "tests/test_execution_exports.py",
                    "tests/test_grouped_execution.py::test_grouped_minimal_privacy_live_and_recovery",
                    "tests/test_execution_cancellation_processes.py::test_competing_process_cannot_cancel_exclusive_executor",
                    *warnings,
                    scratch_name="d",
                ),
            )
            self.step(
                "windows-signatures",
                [str(self.python), "scripts/source_signature_probe.py"],
            )
            self.step(
                "windows-privacy-locks",
                self.pytest_command(
                    "--no-cov",
                    "-q",
                    "tests/test_execution_privacy.py",
                    "tests/test_execution.py::test_minimal_privacy_success_and_loss_windows",
                    *warnings,
                    scratch_name="l",
                ),
            )
        self.check_suite()
        for name, fixture, assertions in R_GATES:
            target = self.data_root / "r" / name
            phases = (None,) if name == "exports" else ("persist", "export")
            for phase in phases:
                command = [str(self.python), fixture, "--output", str(target)]
                if phase:
                    command.extend(("--phase", phase))
                self.step(f"r-{name}-{phase or 'generate'}", command)
            self.step(
                f"r-{name}-assertions", [rscript, "--vanilla", assertions, str(target)]
            )
        self.step(
            "r-cli-lifecycle-assertions",
            [
                str(self.python),
                "scripts/cli_lifecycle_r_gate.py",
                "--output",
                str(self.data_root / "r" / "cli-lifecycle"),
            ],
        )
        self.build_and_smoke()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument(
        "--python-version", choices=("3.11", "3.12", "3.15"), required=True
    )
    parser.add_argument("--mode", choices=("prepare", "check"), required=True)
    parser.add_argument("--uv", required=True)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--benchmark-workers", default="")
    parser.add_argument(
        "--distribution", choices=("loadfile", "worksteal"), default="worksteal"
    )
    parser.add_argument("--resource-slots", type=int, default=1)
    parser.add_argument("--resource-root", type=Path)
    parser.add_argument(
        "--suite-stage", choices=("full", "ordinary", "resource"), default="full"
    )
    options = parser.parse_args()
    if (
        re.fullmatch(r"[0-9a-f]{40}", options.commit) is None
        or re.fullmatch(r"[0-9a-f]{64}", options.archive_sha256) is None
    ):
        parser.error("commit/archive must be complete lowercase hexadecimal hashes")
    try:
        return Runner(Options(**vars(options))).run()
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
