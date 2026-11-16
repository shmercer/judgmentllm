"""The reviewed virtual EOF correspondence never licenses a physical exclusion."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from coverage import Coverage
from coverage.parser import PythonParser
from coverage_equivalence import compare
from test_coverage_policy import CODE, SETTINGS, commit, evidence


def eof_case(
    root: Path,
    *,
    old: str = CODE,
    new: str = "# shifted\n" + CODE,
    old_exclusions: list[int] | None = None,
    new_exclusions: list[int] | None = None,
    old_measurement: dict[str, Any] | None = None,
    new_measurement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = root / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "sample.py").write_text(old)
    (source / "pyproject.toml").write_text(SETTINGS)
    previous = commit(source)
    original = old_measurement or evidence([1, 2, 3, 4], [], [[2, 3], [2, 4]], [])
    current = new_measurement or evidence([2, 3, 4, 5], [], [[3, 4], [3, 5]], [])
    before = original["files"]["sample.py"]
    after = current["files"]["sample.py"]
    before["excluded_lines"] = (
        old_exclusions if old_exclusions is not None else [len(old.splitlines()) + 1]
    )
    after["excluded_lines"] = (
        new_exclusions if new_exclusions is not None else [len(new.splitlines()) + 1]
    )
    baseline = {
        "source_commit": previous,
        "measurement_settings": {"run": {"branch": True}, "report": {"fail_under": 90}},
        "totals": original["totals"],
        "files": {
            "sample.py": {
                "source_sha256": hashlib.sha256(old.encode()).hexdigest(),
                "executed_lines": before["executed_lines"],
                "executed_branches": before["executed_branches"],
                "excluded_lines": before["excluded_lines"],
                "denominators": {
                    k: before["summary"][k] for k in ("num_statements", "num_branches")
                },
            }
        },
    }
    baseline_path, current_path = root / "baseline.json", root / "current.json"
    baseline_path.write_text(json.dumps(baseline))
    current_path.write_text(json.dumps(current))
    (source / "sample.py").write_text(new)
    head = commit(source)
    return compare(current_path, baseline_path, source, reviewed_ref=head)


def test_shifted_virtual_eof_matches_without_entering_executable_mapping(
    tmp_path: Path,
) -> None:
    result = eof_case(tmp_path)
    assert result["passed"], result
    assert result["modified"][0]["changed_executable_lines"] == []


def test_real_coverage_parser_reports_the_protocol_eof_boundary() -> None:
    program = "from typing import Protocol\n\nclass Adapter(Protocol):\n    def submit(self) -> None: ...\n"
    expression = (
        "|".join(Coverage(config_file=False).get_exclude_list())
        + r"|class .*\bProtocol\b"
    )
    for prefix in ("", "# shifted\n"):
        text = prefix + program
        parser = PythonParser(text=text, exclude=expression)
        parser.parse_source()
        assert len(text.splitlines()) + 1 in parser.excluded


@pytest.mark.parametrize(
    "side,accepted", [("historical_only", True), ("current_only", False)]
)
def test_one_sided_virtual_eof_does_not_create_a_correspondence(
    tmp_path: Path, side: str, accepted: bool
) -> None:
    result = eof_case(
        tmp_path,
        old_exclusions=[] if side == "current_only" else None,
        new_exclusions=[] if side == "historical_only" else None,
    )
    # Removing an unmapped nonphysical point retains the previous behavior.
    assert result["passed"] is accepted


def test_virtual_eof_never_allows_a_new_physical_exclusion(tmp_path: Path) -> None:
    result = eof_case(tmp_path, new_exclusions=[1, 6])
    assert not result["passed"]
    assert "exclusions" in result["losses"][-1]["reason"]


def test_virtual_eof_cannot_hide_an_uncovered_added_statement(tmp_path: Path) -> None:
    result = eof_case(
        tmp_path,
        new="# shifted\nADDED = 1\n" + CODE,
        new_measurement=evidence([3, 4, 5, 6], [2], [[4, 5], [4, 6]], []),
    )
    assert not result["passed"]
    assert result["modified"][0]["missing_changed_lines"] == [2]


def test_virtual_eof_cannot_hide_changed_branch_regression(tmp_path: Path) -> None:
    result = eof_case(
        tmp_path,
        new="# shifted\n" + CODE.replace("if flag:", "if flag is True:"),
        new_measurement=evidence([2, 3, 4], [5], [[3, 4]], [[3, 5]]),
    )
    assert not result["passed"]
    assert result["modified"][0]["missing_changed_branches"] == [[3, 5]]


@pytest.mark.parametrize(
    "old,new",
    [("", "# added\n"), (CODE.rstrip("\n"), "# shifted\n" + CODE.rstrip("\n"))],
)
def test_virtual_eof_handles_empty_and_no_final_newline(
    tmp_path: Path, old: str, new: str
) -> None:
    if old:
        result = eof_case(tmp_path, old=old, new=new)
    else:
        result = eof_case(
            tmp_path,
            old=old,
            new=new,
            old_measurement=evidence([], [], [], []),
            new_measurement=evidence([], [], [], []),
        )
    assert result["passed"]


@pytest.mark.parametrize("before,after", [(6, 7), (5, 7), (6, 6), (99, 100)])
def test_arbitrary_beyond_eof_values_are_not_mapped(
    tmp_path: Path, before: int, after: int
) -> None:
    result = eof_case(tmp_path, old_exclusions=[before], new_exclusions=[after])
    assert not result["passed"]
    assert "exclusions" in result["losses"][-1]["reason"]
