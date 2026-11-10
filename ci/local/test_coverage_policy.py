"""Coverage policy regressions use deterministic, internally complete evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from coverage_equivalence import compare

CODE = "def choose(flag):\n    if flag:\n        return 1\n    return 0\n"
SETTINGS = (
    "[tool.coverage.run]\nbranch = true\n[tool.coverage.report]\nfail_under = 90\n"
)


def commit(source: Path) -> str:
    subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CI fixture",
            "-c",
            "user.email=ci@example.invalid",
            "commit",
            "-qm",
            "Synthetic coverage source",
        ],
        cwd=source,
        check=True,
        capture_output=True,
    )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()


def evidence(
    lines: list[int],
    missing: list[int],
    arcs: list[list[int]],
    missing_arcs: list[list[int]],
) -> dict[str, Any]:
    summary = {
        "covered_lines": len(lines),
        "num_statements": len(lines) + len(missing),
        "covered_branches": len(arcs),
        "num_branches": len(arcs) + len(missing_arcs),
    }
    return {
        "meta": {"branch_coverage": True},
        "totals": summary,
        "files": {
            "sample.py": {
                "summary": summary,
                "executed_lines": lines,
                "missing_lines": missing,
                "executed_branches": arcs,
                "missing_branches": missing_arcs,
                "excluded_lines": [],
            }
        },
    }


@pytest.fixture
def sample(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Any], dict[str, Any]]:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "sample.py").write_text(CODE)
    (source / "pyproject.toml").write_text(SETTINGS)
    ref = commit(source)
    measured = evidence([1, 2, 3, 4], [], [[2, 3], [2, 4]], [])
    original = {
        "source_commit": ref,
        "measurement_settings": {"run": {"branch": True}, "report": {"fail_under": 90}},
        "totals": measured["totals"],
        "files": {
            "sample.py": {
                "source_sha256": hashlib.sha256(CODE.encode()).hexdigest(),
                "executed_lines": [1, 2, 3, 4],
                "executed_branches": [[2, 3], [2, 4]],
                "excluded_lines": [],
                "denominators": {"num_statements": 4, "num_branches": 2},
            }
        },
    }
    baseline, current = tmp_path / "baseline.json", tmp_path / "current.json"
    baseline.write_text(json.dumps(original))
    current.write_text(json.dumps(measured))
    return source, baseline, current, original, measured


def test_unchanged_module_cannot_trade_covered_lines_or_arcs(sample: Any) -> None:
    source, baseline, current, _, measured = sample
    assert compare(current, baseline, source)["passed"]
    measured["files"]["sample.py"]["executed_branches"][1] = [2, -1]
    current.write_text(json.dumps(measured))
    result = compare(current, baseline, source)
    assert not result["passed"]
    assert result["losses"][0]["missing_branches"] == [(2, 4)]


def test_pr27_style_prompt_edit_is_not_rejected_for_its_hash(sample: Any) -> None:
    source, baseline, current, _, _ = sample
    (source / "sample.py").write_text(
        CODE.replace("return 1", 'return "one raw JSON object"')
    )
    head = commit(source)
    result = compare(current, baseline, source, reviewed_ref=head)
    assert result["passed"], result
    assert result["modified"][0]["changed_executable_lines"] == [3]
    assert "not established" in result["modified"][0]["branch_correspondence"]
    assert not compare(current, baseline, source)["passed"]


def test_inserted_lines_do_not_use_old_line_number_sets(sample: Any) -> None:
    source, baseline, current, _, _ = sample
    (source / "sample.py").write_text("# inserted documentation\n" + CODE)
    head = commit(source)
    current.write_text(json.dumps(evidence([2, 3, 4, 5], [], [[3, 4], [3, 5]], [])))
    assert compare(current, baseline, source, reviewed_ref=head)["passed"]


def test_modified_behavior_requires_changed_lines_and_branches(sample: Any) -> None:
    source, baseline, current, _, _ = sample
    (source / "sample.py").write_text(CODE.replace("return 1", "return 2"))
    head = commit(source)
    current.write_text(json.dumps(evidence([1, 2, 4], [3], [[2, 4]], [[2, 3]])))
    result = compare(current, baseline, source, reviewed_ref=head)
    assert not result["passed"]
    assert result["modified"][0]["missing_changed_lines"] == [3]
    assert result["modified"][0]["regressions"]


def test_modified_branch_origin_requires_both_outcomes(sample: Any) -> None:
    source, baseline, current, _, _ = sample
    (source / "sample.py").write_text(CODE.replace("if flag:", "if flag is True:"))
    head = commit(source)
    current.write_text(json.dumps(evidence([1, 2, 3, 4], [], [[2, 3]], [[2, 4]])))
    result = compare(current, baseline, source, reviewed_ref=head)
    assert result["modified"][0]["missing_changed_branches"] == [[2, 4]]
    assert not result["passed"]


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "corrupt",
        "branch-disabled",
        "file-omitted",
        "counts",
        "threshold",
        "config-exclusion",
        "inline-exclusion",
        "source-uncommitted",
        "historical-hash",
    ],
)
def test_missing_corrupt_or_weakened_evidence_fails(sample: Any, fault: str) -> None:
    source, baseline, current, original, measured = sample
    head = original["source_commit"]
    if fault == "missing":
        current.unlink()
    elif fault == "corrupt":
        current.write_text("{")
    elif fault == "threshold":
        (source / "pyproject.toml").write_text(SETTINGS.replace("90", "80"))
    elif fault == "config-exclusion":
        (source / "pyproject.toml").write_text(SETTINGS + 'exclude_lines = [".*"]\n')
    elif fault in {"source-uncommitted", "historical-hash"}:
        (source / "sample.py").write_text(CODE.replace("return 1", "return 2"))
        if fault == "historical-hash":
            head = commit(source)
            original["files"]["sample.py"]["source_sha256"] = "0" * 64
            baseline.write_text(json.dumps(original))
    else:
        if fault == "branch-disabled":
            measured["meta"]["branch_coverage"] = False
        elif fault == "file-omitted":
            measured["files"] = {}
        elif fault == "counts":
            measured["totals"] = {**measured["totals"], "covered_lines": 5}
        elif fault == "inline-exclusion":
            measured["files"]["sample.py"]["excluded_lines"] = [8]
        current.write_text(json.dumps(measured))
    assert not compare(current, baseline, source, reviewed_ref=head)["passed"]


def test_candidate_baseline_replacement_cannot_bypass_trusted_baseline(
    sample: Any,
) -> None:
    source, baseline, current, original, _ = sample
    # A PR may propose a floor file, but acceptance reads the protected base copy.
    candidate = source / "ci/coverage-floor.json"
    candidate.parent.mkdir()
    candidate.write_text(json.dumps({**original, "files": {}, "totals": {}}))
    current.write_text(json.dumps(evidence([1, 2, 4], [3], [[2, 4]], [[2, 3]])))
    result = compare(current, baseline, source)
    assert not result["passed"]
    assert result["losses"]
