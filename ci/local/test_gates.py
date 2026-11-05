"""Offline regressions for the local development runner, outside package tests."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import gates
import pytest

from judgmentllm import __version__


@pytest.fixture
def staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "source"
    files = (
        "uv.lock",
        "pyproject.toml",
        "ci/local/gates.py",
        "scripts/smoke_install.py",
    )
    for relative in files:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic tracked source\n", encoding="utf-8")
    manifest = {
        "format_version": 1,
        "commit": "a" * 40,
        "archive_sha256": "b" * 64,
        "lockfile_sha256": gates.digest(source / "uv.lock"),
        "files": {relative: gates.digest(source / relative) for relative in files},
    }
    (source / gates.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(gates, "__file__", str(source / "ci/local/gates.py"))
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    patch = gates.PYTHON_VERSIONS.get(gates.platform.system(), {}).get(minor)
    if patch is not None:
        monkeypatch.setattr(gates.platform, "python_version", lambda: patch)
    return source


def options(
    source: Path,
    output: Path,
    *,
    mode: str = "check",
    python_version: str | None = None,
) -> gates.Options:
    return gates.Options(
        source,
        output,
        "a" * 40,
        "b" * 64,
        python_version or f"{sys.version_info.major}.{sys.version_info.minor}",
        mode,
        sys.executable,
    )


@pytest.mark.parametrize(
    "path",
    ["../escape", "/escape", "C:/escape", "dir\\escape", ".git/config", gates.MANIFEST],
)
def test_manifest_rejects_unsafe_paths(staged: Path, path: str) -> None:
    manifest_path = staged / gates.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][path] = "c" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest entry"):
        gates.verify_source(staged, "a" * 40, "b" * 64)


def test_manifest_checks_commit_lockfile_and_same_length_mutation(staged: Path) -> None:
    assert len(gates.verify_source(staged, "a" * 40, "b" * 64)) == 4
    with pytest.raises(ValueError, match="commit/archive"):
        gates.verify_source(staged, "c" * 40, "b" * 64)
    lockfile = staged / "uv.lock"
    lockfile.write_bytes(lockfile.read_bytes().replace(b"tracked", b"changed"))
    with pytest.raises(ValueError, match="Tracked source bytes"):
        gates.verify_source(staged, "a" * 40, "b" * 64)


def test_existing_or_overlapping_output_is_rejected(
    staged: Path, tmp_path: Path
) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(FileExistsError):
        gates.Runner(options(staged, output))
    with pytest.raises(ValueError, match="separate directory"):
        gates.Runner(options(staged, staged / "output"))


def fake_commands(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_lint: bool = False,
    fail_pytest: bool = False,
    mutate: Path | None = None,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def execute(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        stream = kwargs["stdout"]
        environment = kwargs["env"]
        assert environment["UV_OFFLINE"] == "1"
        assert environment["UV_PYTHON_DOWNLOADS"] == "never"
        assert environment["JUDGMENTLLM_R_STRICT"] == "1"
        assert "PYTHONPATH" not in environment
        if "import importlib.metadata as m,json" in " ".join(command):
            stream.write('{"pyright":"1.1.414"}')
        elif any(Path(argument).name == "r-probe.R" for argument in command):
            stream.write('{"R":"4.6.1","jsonlite":"2.0.0"}')
        elif "build" in command:
            directory = Path(command[command.index("--out-dir") + 1])
            directory.mkdir()
            for name in ("synthetic.whl", "synthetic.tar.gz"):
                (directory / name).write_bytes(b"synthetic artifact")
        elif "pytest" in command:
            stream.write(
                (
                    "1 failed, 1375 passed, 4 skipped in 1.25s\n"
                    if fail_pytest
                    else "1376 passed in 1s\n"
                )
                + "Required test coverage of 90% reached. Total coverage: 96.16%\n"
            )
        if mutate is not None and "twine" in command:
            mutate.write_text("Changed during checks")
        failed = (fail_lint and "ruff" in command and "check" in command) or (
            fail_pytest and "pytest" in command
        )
        if failed:
            stream.write("synthetic lint failure retained\n")
        return subprocess.CompletedProcess(command, 1 if failed else 0)

    monkeypatch.setattr(gates.subprocess, "run", execute)

    def which(value: str, **kwargs: Any) -> str:
        return value

    monkeypatch.setattr(gates.shutil, "which", which)
    monkeypatch.setenv("PYTHONPATH", "ignored-external-source")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "ignored-old-environment")
    return calls


@pytest.mark.parametrize(
    ("system", "minor", "patch"),
    [
        ("Linux", "3.11", "3.11.18"),
        ("Linux", "3.12", "3.12.16"),
        ("Linux", "3.15", "3.15.2"),
        ("Darwin", "3.11", "3.11.18"),
        ("Darwin", "3.12", "3.12.16"),
        ("Windows", "3.11", "3.11.18"),
        ("Windows", "3.12", "3.12.16"),
    ],
)
@pytest.mark.parametrize("fail_pytest", [False, True])
def test_seven_lanes_preserve_full_tests_r_gates_and_single_coverage_lane(
    staged: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    minor: str,
    patch: str,
    fail_pytest: bool,
) -> None:
    monkeypatch.setattr(gates.platform, "system", lambda: system)
    monkeypatch.setattr(gates.platform, "python_version", lambda: patch)
    monkeypatch.setattr(gates.platform, "python_implementation", lambda: "CPython")

    def allocate(environment: dict[str, str]) -> Path:
        return gates.create_scratch(tmp_path / "scratch")

    monkeypatch.setattr(gates, "allocate_windows_scratch", allocate)
    calls = fake_commands(monkeypatch, fail_pytest=fail_pytest)
    runner = gates.Runner(options(staged, tmp_path / "result", python_version=minor))
    runner.windows = system == "Windows"
    assert runner.run() == int(fail_pytest)
    assert runner.summary["interpreter_verified"] is True
    coverage_enabled = system == "Linux" and minor == "3.11"
    name = "pytest-coverage" if coverage_enabled else "pytest-no-coverage"
    step = next(step for step in runner.summary["steps"] if step["name"] == name)
    command = step["command"]
    assert "tests" in command and "ci/local/test_gates.py" in command
    assert "-ra" in command
    assert "-Werror::ResourceWarning" in command
    assert "-Werror::pytest.PytestUnraisableExceptionWarning" in command
    assert not any(
        argument.startswith(("--ignore", "--deselect")) for argument in command
    )
    assert "-k" not in command and "-m" not in command[3:]
    assert ("--no-cov" not in command) == coverage_enabled
    assert (
        any(argument.startswith("--cov-report=xml:") for argument in command)
        == coverage_enabled
    )
    assert step["coverage_enabled"] == coverage_enabled
    assert step["coverage_percent"] == (96.16 if coverage_enabled else None)
    assert step["exit_code"] == int(fail_pytest)
    assert step["pytest_elapsed_seconds"] == (1.25 if fail_pytest else 1.0)
    assert step["tests"] == {
        "passed": 1375 if fail_pytest else 1376,
        "failed": int(fail_pytest),
        "skipped": 4 if fail_pytest else 0,
        "errors": 0,
    }
    assert (runner.output / step["log"]).is_file()
    assert (
        sum(
            step["name"].startswith("r-") and step["name"].endswith("-assertions")
            for step in runner.summary["steps"]
        )
        == 7
    )
    assert runner.summary["all_scheduled_gates_finished"] is True
    if fail_pytest:
        assert step["failure_kind"] == "check_failure"
        assert runner.summary["status"] == "failed"
    if system == "Windows":
        focused = [
            command
            for command in calls
            if "pytest" in command and "tests" not in command
        ]
        assert len(focused) == 2
        assert all("--no-cov" in command for command in focused)


@pytest.mark.parametrize(
    ("system", "minor", "patch", "implementation"),
    [
        ("Linux", "3.15", "3.15.1", "CPython"),
        ("Linux", "3.11", "3.11.19", "CPython"),
        ("Linux", "3.12", "3.12.16", "PyPy"),
        ("Darwin", "3.15", "3.15.2", "CPython"),
        ("Windows", "3.15", "3.15.2", "CPython"),
        ("Other", "3.11", "3.11.18", "CPython"),
    ],
)
def test_unqualified_interpreters_fail_before_running_any_gate(
    staged: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    minor: str,
    patch: str,
    implementation: str,
) -> None:
    monkeypatch.setattr(gates.platform, "system", lambda: system)
    monkeypatch.setattr(gates.platform, "python_version", lambda: patch)
    monkeypatch.setattr(gates.platform, "python_implementation", lambda: implementation)
    calls = fake_commands(monkeypatch)
    runner = gates.Runner(options(staged, tmp_path / "result", python_version=minor))
    runner.windows = False
    assert runner.run() == 1
    assert calls == []
    assert "pinned CPython" in runner.summary["error"]["message"]
    assert runner.summary["all_scheduled_gates_finished"] is False
    assert runner.summary["status"] == "failed"


@pytest.mark.parametrize("fail_lint", [False, True])
def test_complete_offline_plan_preserves_failures(
    staged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_lint: bool
) -> None:
    calls = fake_commands(monkeypatch, fail_lint=fail_lint)
    runner = gates.Runner(options(staged, tmp_path / "result"))
    assert runner.environment["UV_PROJECT_ENVIRONMENT"] == str(staged / ".venv")
    assert runner.run() == int(fail_lint)
    summary = json.loads((runner.output / "summary.json").read_text())
    assert summary["status"] == ("failed" if fail_lint else "passed")
    assert summary["source_verified_before"] and summary["source_verified_after"]
    assert summary["lockfile_sha256"] == gates.digest(staged / "uv.lock")
    assert (
        sum(
            (
                "--vanilla" in command
                and not any(Path(arg).name == "r-probe.R" for arg in command)
            )
            or "scripts/cli_lifecycle_r_gate.py" in command
            for command in calls
        )
        == 7
    )
    assert any("--offline" in command and "build" in command for command in calls)
    smokes = [
        command
        for command in calls
        if any(arg.endswith("smoke_install.py") for arg in command)
    ]
    assert len(smokes) == 2 and all("--offline" in command for command in smokes)
    pytest_step = next(
        step for step in summary["steps"] if step["name"].startswith("pytest-")
    )
    coverage_enabled = (
        gates.platform.system() == "Linux" and runner.options.python_version == "3.11"
    )
    assert pytest_step["coverage_enabled"] == coverage_enabled
    assert pytest_step["coverage_percent"] == (96.16 if coverage_enabled else None)
    assert pytest_step["tests"] == {
        "passed": 1376,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
    }
    assert pytest_step["pytest_elapsed_seconds"] == 1.0
    assert pytest_step["exit_code"] == 0
    if fail_lint:
        failure = next(step for step in summary["steps"] if step["status"] == "failed")
        assert failure["failure_kind"] == "check_failure"
        assert "lint failure retained" in (runner.output / failure["log"]).read_text()


def test_r_version_probe_uses_retained_script_file(
    staged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = fake_commands(monkeypatch)
    runner = gates.Runner(options(staged, tmp_path / "result"))
    assert runner.run() == 0
    script = runner.output / "r-probe.R"
    assert script.read_bytes() == gates.R_PROBE.encode("utf-8")
    commands = [command for command in calls if str(script) in command]
    assert commands == [["Rscript", "--vanilla", str(script)]]
    assert all("\n" not in argument and argument != "-e" for argument in commands[0])


@pytest.mark.parametrize(
    "root",
    [
        r"D:\JudgmentLLM-ci\tmp",
        r"\\host\share\tmp",
        r"C:JudgmentLLM-ci\tmp",
        r"C:\JudgmentLLM-ci\tmp\..\other",
        r"C:\JudgmentLLM-ci\tmp-other",
        r"C:\JudgmentLLM-ci\tmp\01234567890123456789",
        r"C:\JudgmentLLM-ci\tmp\bad:stream",
        r"C:\JudgmentLLM-ci\tmp\bad.",
    ],
)
def test_windows_scratch_root_rejects_unsafe_or_long_overrides(root: str) -> None:
    with pytest.raises(ValueError, match="Windows scratch root"):
        gates.windows_scratch_root(root)


def test_windows_scratch_root_accepts_its_short_project_namespace() -> None:
    assert (
        str(gates.windows_scratch_root("c:/JudgmentLLM-ci/tmp"))
        == r"c:\JudgmentLLM-ci\tmp"
    )
    assert (
        str(gates.windows_scratch_root(r"C:\JudgmentLLM-ci\tmp\ci"))
        == r"C:\JudgmentLLM-ci\tmp\ci"
    )


def test_scratch_allocation_preserves_existing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "scratch"
    existing = root / "raaaaaaaa"
    existing.mkdir(parents=True)
    (existing / "evidence.txt").write_text("preserve existing run")
    names = iter(("aaaaaaaa", "bbbbbbbb"))

    def token(nbytes: int) -> str:
        assert nbytes == 4
        return next(names)

    monkeypatch.setattr(gates.secrets, "token_hex", token)
    assert gates.create_scratch(root) == root / "rbbbbbbbb"
    assert (existing / "evidence.txt").read_text() == "preserve existing run"


@pytest.mark.parametrize("fail_lint", [False, True])
def test_windows_plan_uses_distinct_short_data_paths_and_preserves_them(
    staged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_lint: bool
) -> None:
    calls = fake_commands(monkeypatch, fail_lint=fail_lint)

    def allocate(environment: dict[str, str]) -> Path:
        scratch = gates.create_scratch(tmp_path / "scratch")
        (scratch / "evidence.txt").write_text("retained fixture data")
        return scratch

    monkeypatch.setattr(gates, "allocate_windows_scratch", allocate)
    runner = gates.Runner(options(staged, tmp_path / "long-retained-results"))
    runner.windows = True  # Exercise the Windows plan without touching a real C: drive.
    assert runner.run() == int(fail_lint)
    pytest_calls = [command for command in calls if "pytest" in command]
    bases = {Path(command[command.index("--basetemp") + 1]) for command in pytest_calls}
    assert bases == {runner.data_root / name for name in ("d", "l", "p")}
    assert not any(path.exists() for path in bases)
    assert runner.data_root != runner.output
    for key in ("TEMP", "TMP", "TMPDIR"):
        assert runner.environment[key] == str(runner.data_root / "t")
    fixture_calls = [command for command in calls if "--output" in command]
    assert len(fixture_calls) == 12
    assert all(
        Path(command[command.index("--output") + 1]).is_relative_to(
            runner.data_root / "r"
        )
        for command in fixture_calls
    )
    assert runner.summary["scratch"]["path"] == str(runner.data_root)
    assert (runner.data_root / "evidence.txt").read_text() == "retained fixture data"
    assert (runner.output / "summary.json").is_file()


def test_final_source_mutation_never_reports_success(
    staged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_commands(monkeypatch, mutate=staged / "uv.lock")
    runner = gates.Runner(options(staged, tmp_path / "result"))
    assert runner.run() == 1
    assert runner.summary["source_verified_after"] is False
    assert "Tracked source bytes differ" in runner.summary["source_integrity_error"]


def test_missing_command_retains_environment_failure_log(
    staged: Path, tmp_path: Path
) -> None:
    runner = gates.Runner(options(staged, tmp_path / "result"))
    assert not runner.step(
        "missing", [str(tmp_path / "missing-executable")], prerequisite=True
    )
    step = runner.summary["steps"][-1]
    assert step["failure_kind"] == "environment_setup"
    assert "FileNotFoundError" in (runner.output / step["log"]).read_text()


def test_timeout_is_observed_without_asserting_its_cause(
    staged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timeout(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(gates.subprocess, "run", timeout)
    runner = gates.Runner(options(staged, tmp_path / "result"))
    assert not runner.step("timeout", ["synthetic-probe"])
    step = runner.summary["steps"][-1]
    assert step["failure_kind"] == "timeout_or_resource_limit"
    assert "TimeoutExpired" in (runner.output / step["log"]).read_text()


def test_missing_mandatory_r_fails_without_reporting_gate_success(
    staged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_commands(monkeypatch)

    def which(value: str, **kwargs: Any) -> str | None:
        return None if value == "Rscript" else value

    monkeypatch.setattr(gates.shutil, "which", which)
    runner = gates.Runner(options(staged, tmp_path / "result"))
    assert runner.run() == 1
    assert runner.summary["status"] == "failed"
    assert "Required Rscript is unavailable" in runner.summary["error"]["message"]


def test_unexpected_runner_failure_never_records_success(
    staged: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_commands(monkeypatch)

    def broken(self: gates.Runner, rscript: str) -> None:
        raise TypeError("synthetic runner failure")

    monkeypatch.setattr(gates.Runner, "check", broken)
    runner = gates.Runner(options(staged, tmp_path / "result"))
    assert runner.run() == 1
    assert runner.summary["error"]["type"] == "TypeError"
    assert runner.summary["all_scheduled_gates_finished"] is False
    assert runner.summary["status"] == "failed"


@pytest.mark.parametrize("offline", [False, True])
def test_smoke_installer_preserves_default_and_explicit_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: bool
) -> None:
    path = Path(__file__).resolve().parents[2] / "scripts/smoke_install.py"
    specification = importlib.util.spec_from_file_location("local_ci_smoke_test", path)
    assert specification is not None and specification.loader is not None
    smoke = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(smoke)
    installations: list[bool] = []

    def execute(command: list[str], **kwargs: Any) -> str:
        if "install" in command:
            installations.append(kwargs["offline"])
        if "--json" in command:
            exported = tmp_path / "smoke.csv"
            exported.write_text("synthetic", encoding="utf-8")
            return json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "smoke",
                    "run_status": "completed",
                    "data": {"valid": True, "results": 1},
                    "artifacts": {"export": str(exported)},
                }
            )
        if "--help" in command:
            return "--help --version"
        if "--version" in command:
            return f"judgmentllm {__version__}"
        if "-c" in command:
            return __version__
        return "rubric_scores_only Synthetic installation response."

    monkeypatch.setattr(smoke, "run", execute)
    smoke.smoke_install(
        "uv",
        tmp_path / "package.whl",
        tmp_path / "constraints",
        tmp_path,
        offline=offline,
    )
    assert installations == [offline]
