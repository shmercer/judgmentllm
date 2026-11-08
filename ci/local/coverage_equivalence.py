"""Compare measured line/branch sets without changing coverage configuration."""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any, cast


def compare(current: Path, baseline: Path | None, source: Path) -> dict[str, Any]:
    measured = cast(dict[str, Any], json.loads(current.read_text(encoding="utf-8")))
    totals = measured["totals"]
    line_total, line_hits = totals["num_statements"], totals["covered_lines"]
    branch_total, branch_hits = totals["num_branches"], totals["covered_branches"]
    combined = 100 * (line_hits + branch_hits) / (line_total + branch_total)
    result: dict[str, Any] = {
        "passed": combined >= 90,
        "combined_percent": combined,
        "line_percent": 100 * line_hits / line_total,
        "branch_percent": 100 * branch_hits / branch_total if branch_total else 100.0,
        "totals": totals,
        "losses": [],
    }
    if baseline is None:
        return result
    original = cast(dict[str, Any], json.loads(baseline.read_text(encoding="utf-8")))
    if "measurement_settings" in original:
        project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
        if project["tool"]["coverage"] != original["measurement_settings"]:
            result["losses"].append({"reason": "coverage measurement settings changed"})
    before = original["totals"]
    for hits, denominator in [
        ("covered_lines", "num_statements"),
        ("covered_branches", "num_branches"),
    ]:
        if totals[hits] * before[denominator] < before[hits] * totals[denominator]:
            result["losses"].append(
                {"metric": hits, "reason": "overall coverage decreased"}
            )
    if (line_hits + branch_hits) * (
        before["num_statements"] + before["num_branches"]
    ) < (before["covered_lines"] + before["covered_branches"]) * (
        line_total + branch_total
    ):
        result["losses"].append(
            {"metric": "combined", "reason": "overall coverage decreased"}
        )
    for filename, expected in original["files"].items():
        path = source / filename
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest()
            != expected["source_sha256"]
        ):
            result["losses"].append(
                {"file": filename, "reason": "production source changed"}
            )
            continue
        actual = measured["files"].get(filename, {})
        if "denominators" in expected and any(
            actual.get("summary", {}).get(key) != value
            for key, value in expected["denominators"].items()
        ):
            result["losses"].append(
                {"file": filename, "reason": "coverage source denominator changed"}
            )
        if (
            "excluded_lines" in expected
            and actual.get("excluded_lines") != expected["excluded_lines"]
        ):
            result["losses"].append(
                {"file": filename, "reason": "coverage exclusions changed"}
            )
        lines = sorted(
            set(expected["executed_lines"]) - set(actual.get("executed_lines", []))
        )
        expected_branches = {
            (branch[0], branch[1])
            for branch in cast(list[list[int]], expected["executed_branches"])
        }
        actual_branches = {
            (branch[0], branch[1])
            for branch in cast(list[list[int]], actual.get("executed_branches", []))
        }
        branches = sorted(expected_branches - actual_branches)
        if lines or branches:
            result["losses"].append(
                {"file": filename, "missing_lines": lines, "missing_branches": branches}
            )
    result["passed"] = result["passed"] and not result["losses"]
    return result
