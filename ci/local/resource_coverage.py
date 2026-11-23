"""Keep functional coverage obligations separate from resource measurements."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from coverage import CoverageData
from coverage_equivalence import compare


def assess(
    functional: Path,
    baseline: Path,
    source: Path,
    head: str,
    *,
    complete: Path | None = None,
    resource_data: Path | None = None,
    coverage_policy: dict[str, Any] | None = None,
    protected_base: str | None = None,
) -> dict[str, Any]:
    authority: dict[str, Any] = (
        {"coverage_policy": coverage_policy, "protected_base": protected_base}
        if coverage_policy is not None
        else {}
    )
    comparison = compare(
        functional,
        baseline,
        source,
        reviewed_ref=head,
        **authority,
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "source_sha": head,
        "functional_comparison": comparison,
        "path": "A" if comparison["passed"] else "B",
        "vm_coverage_eligible": comparison["passed"],
        "resource_contributions": [],
        "reason": "ordinary and serial independently preserve every historical coverage obligation"
        if comparison["passed"]
        else "functional coverage is insufficient; authoritative host resources retain the verified coverage configuration",
    }
    if complete is not None:
        before, after = (
            json.loads(functional.read_text()),
            json.loads(complete.read_text()),
        )
        data = (
            CoverageData(basename=str(resource_data))
            if resource_data is not None
            else None
        )
        if data is not None:
            data.read()
        for name, measured in after["files"].items():
            ordinary = before["files"].get(name, {})
            lines = sorted(
                set(measured["executed_lines"])
                - set(ordinary.get("executed_lines", []))
            )
            arcs: list[tuple[int, int]] = sorted(
                {(a, b) for a, b in measured["executed_branches"]}
                - {(a, b) for a, b in ordinary.get("executed_branches", [])}
            )
            if not lines and not arcs:
                continue
            cases: dict[str, Any] = {}
            if data is not None:
                for context in sorted(data.measured_contexts()):
                    if "::" not in context:
                        continue
                    data.set_query_context(context)
                    hits = sorted(set(lines) & set(data.lines(name) or []))
                    branch_hits = sorted(set(arcs) & set(data.arcs(name) or []))
                    if hits or branch_hits:
                        cases[context] = {"lines": hits, "arcs": branch_hits}
            result["resource_contributions"].append(
                {"file": name, "lines": lines, "arcs": arcs, "cases": cases}
            )
    return result


def admit_vm(
    assessment: dict[str, Any],
    *,
    source_sha: str,
    isolated: bool,
    thresholds_passed: bool,
    performance_passed: bool,
) -> None:
    if (
        assessment.get("schema_version") != 1
        or assessment.get("source_sha") != source_sha
        or assessment.get("path") != "A"
        or assessment.get("vm_coverage_eligible") is not True
        or assessment.get("functional_comparison", {}).get("passed") is not True
        or not isolated
        or not thresholds_passed
        or not performance_passed
    ):
        raise ValueError(
            "Resource VM lacks independent historical coverage, original-threshold, isolation or total-runtime qualification"
        )
