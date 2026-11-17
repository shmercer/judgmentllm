"""Independent Python processes and strict base R/jsonlite lifecycle assertions."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock, call

import pytest
from fixture_artifacts import immutable_exports
from r_dependencies import require_rscript

from judgmentllm.io import read_results, read_results_csv

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts" / "grouped_lifecycle_r_fixture.py"
OPENAI_FIXTURE = ROOT / "scripts" / "openai_decisions_r_fixture.py"
R_SCRIPT = ROOT / "examples" / "grouped_lifecycle_interoperability.R"


def generate(output: Path) -> None:
    # Keep the eight OpenAI CLI calls outside the historical phase's deadline.
    for phase in ("persist", "export"):
        for script, location, flags in (
            (FIXTURE, output, ["--historical-only"]),
            (OPENAI_FIXTURE, output / "openai-decisions", []),
        ):
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--phase",
                    phase,
                    "--output",
                    str(location),
                    *flags,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert completed.returncode == 0, completed.stderr
    runs = json.loads(
        (output / "openai-decisions" / "exports.json").read_text(encoding="utf-8")
    )
    assert sorted((run["kind"], run["capture"]) for run in runs) == [
        ("pairwise", "full"),
        ("pairwise", "minimal"),
        ("rubric", "full"),
        ("rubric", "minimal"),
    ], "Expected all four OpenAI rubric/pairwise full/minimal exports"


@pytest.fixture(scope="module")
def generated_exports(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    # Both readers consume one real persist/export pipeline. The helper verifies
    # every artifact remains byte-identical after all readers finish.
    with immutable_exports(tmp_path_factory.mktemp("rf"), generate) as output:
        yield output


def test_grouped_lifecycle_survives_independent_processes(
    generated_exports: Path,
) -> None:
    evidence = [
        json.loads(line)
        for line in (generated_exports / "grouped-lifecycle.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["run_id"] for row in evidence] == ["001", "002"]
    for row in evidence:
        assert len(row["attempts"]) == len(row["acceptances"]) == 2
        assert [len(attempt["task_ids"]) for attempt in row["attempts"]] == [3, 1]
        assert all(accepted["acceptance_count"] == 1 for accepted in row["acceptances"])
        records = list(read_results(generated_exports / f"{row['run_id']}.jsonl"))
        assert records == list(
            read_results_csv(generated_exports / f"{row['run_id']}.csv")
        )
        assert [record.status for record in records] == [
            "complete",
            "complete",
            "invalid",
            "complete",
        ]
    runs = json.loads(
        (generated_exports / "openai-decisions" / "exports.json").read_text(
            encoding="utf-8"
        )
    )
    for run in runs:
        records = list(read_results(run["canonical"]["files"][0]["path"]))
        assert records == list(read_results_csv(run["csv"]["files"][0]["path"]))
        assert len(records) == (7 if run["kind"] == "rubric" else 13)
        assert Path(run["r"]["files"][0]["path"]).is_file()
        assert Path(run["provenance"]["manifest_path"]).is_file()


@pytest.mark.r_interop
def test_grouped_lifecycle_passes_required_r_assertions(
    generated_exports: Path,
    tmp_path: Path,
) -> None:
    executable = require_rscript()
    completed = subprocess.run(
        [executable, "--vanilla", str(R_SCRIPT), str(generated_exports)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Grouped lifecycle interoperability passed" in completed.stdout
    exports = json.loads(
        (generated_exports / "openai-decisions" / "exports.json").read_text(
            encoding="utf-8"
        )
    )
    for name, runs in (
        ("missing", None),
        ("empty", []),
        ("partial", exports[:-1]),
        ("duplicate", [*exports[:-1], exports[0]]),
    ):
        invalid = tmp_path / name
        invalid.mkdir()
        if runs is not None:
            (invalid / "exports.json").write_text(json.dumps(runs), encoding="utf-8")
        rejected = subprocess.run(
            [
                executable,
                "--vanilla",
                str(ROOT / "examples" / "openai_decisions_interoperability.R"),
                str(invalid),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert rejected.returncode != 0, name
        assert "OpenAI Decisions interoperability passed" not in rejected.stdout


VARIANTS = [
    {"kind": kind, "capture": capture}
    for kind in ("rubric", "pairwise")
    for capture in ("full", "minimal")
]


def test_pytest_generator_has_four_independently_bounded_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    openai = tmp_path / "openai-decisions"
    openai.mkdir()
    (openai / "exports.json").write_text(json.dumps(VARIANTS), encoding="utf-8")
    runner = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", runner)
    with immutable_exports(tmp_path, generate):
        assert runner.call_args_list == [
            call(
                [
                    sys.executable,
                    str(script),
                    "--phase",
                    phase,
                    "--output",
                    str(output),
                    *flags,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            for phase in ("persist", "export")
            for script, output, flags in (
                (FIXTURE, tmp_path, ["--historical-only"]),
                (ROOT / "scripts" / "openai_decisions_r_fixture.py", openai, []),
            )
        ]


@pytest.mark.parametrize("phase", [0, 1, 2, 3])
@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_pytest_generator_stops_before_sharing_failed_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: int, failure: str
) -> None:
    runner = Mock(
        side_effect=[subprocess.CompletedProcess[str]([], 0)] * phase
        + [
            subprocess.CompletedProcess[str]([], 1, stderr="fixture phase failed")
            if failure == "exit"
            else subprocess.TimeoutExpired("fixture phase", 60)
        ]
    )
    monkeypatch.setattr(subprocess, "run", runner)
    error = AssertionError if failure == "exit" else subprocess.TimeoutExpired
    with pytest.raises(error):
        with immutable_exports(tmp_path, generate):
            pytest.fail("Incomplete fixtures reached a reader")
    assert runner.call_count == phase + 1


@pytest.mark.parametrize("invalid", ["missing", "empty", "partial", "duplicate"])
def test_pytest_generator_rejects_incomplete_openai_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    openai = tmp_path / "openai-decisions"
    openai.mkdir()
    if invalid != "missing":
        runs = {
            "empty": [],
            "partial": VARIANTS[:-1],
            "duplicate": [*VARIANTS[:-1], VARIANTS[0]],
        }[invalid]
        (openai / "exports.json").write_text(json.dumps(runs), encoding="utf-8")
    runner = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(subprocess, "run", runner)
    with pytest.raises(FileNotFoundError if invalid == "missing" else AssertionError):
        with immutable_exports(tmp_path, generate):
            pytest.fail("Incomplete OpenAI exports reached a reader")
    assert runner.call_count == 4


def test_shared_fixture_detects_openai_artifact_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    openai = tmp_path / "openai-decisions"
    openai.mkdir()
    (openai / "exports.json").write_text(json.dumps(VARIANTS), encoding="utf-8")
    artifact = openai / "result.jsonl"
    artifact.write_text("original", encoding="utf-8")
    monkeypatch.setattr(
        subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0))
    )
    with pytest.raises(AssertionError, match="mutated shared R fixture artifacts"):
        with immutable_exports(tmp_path, generate):
            artifact.write_text("changed", encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("historical_only", [False, True])
async def test_grouped_fixture_default_complete_and_explicit_historical_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, historical_only: bool
) -> None:
    from scripts import grouped_lifecycle_r_fixture, openai_decisions_r_fixture

    monkeypatch.setattr(sys, "path", [str(ROOT / "scripts"), *sys.path])
    monkeypatch.setitem(
        sys.modules, "openai_decisions_r_fixture", openai_decisions_r_fixture
    )
    persist = Mock()
    export = Mock()
    monkeypatch.setattr(openai_decisions_r_fixture, "persist", persist)
    monkeypatch.setattr(openai_decisions_r_fixture, "export", export)
    options = {"historical_only": True} if historical_only else {}
    await grouped_lifecycle_r_fixture.persist(tmp_path, **options)
    await grouped_lifecycle_r_fixture.export(tmp_path, **options)
    evidence = [
        json.loads(line)
        for line in (tmp_path / "grouped-lifecycle.jsonl").read_text().splitlines()
    ]
    assert [row["run_id"] for row in evidence] == ["001", "002"]
    assert (tmp_path / "decisions" / "decisions.jsonl").is_file()
    for run in ("001", "002"):
        assert list(read_results(tmp_path / f"{run}.jsonl")) == list(
            read_results_csv(tmp_path / f"{run}.csv")
        )
    expected = [] if historical_only else [call(tmp_path / "openai-decisions")]
    assert persist.call_args_list == export.call_args_list == expected
