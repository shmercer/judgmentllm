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
    return source


def options(source: Path, output: Path, *, mode: str = "check") -> gates.Options:
    return gates.Options(
        source,
        output,
        "a" * 40,
        "b" * 64,
        f"{sys.version_info.major}.{sys.version_info.minor}",
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
                "1376 passed in 1s\nRequired test coverage of 90% reached. Total coverage: 96.16%\n"
            )
        if mutate is not None and "twine" in command:
            mutate.write_text("Changed during checks")
        failed = fail_lint and "ruff" in command and "check" in command
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
        step for step in summary["steps"] if step["name"] == "pytest-coverage"
    )
    assert pytest_step["coverage_percent"] == 96.16
    assert pytest_step["tests"] == {"passed": 1376}
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
            return "judgmentllm 0.1.0a2"
        if "-c" in command:
            return "0.1.0a2"
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
