"""Conservative coverage comparison; line identity is valid only for equal source."""

from __future__ import annotations

import difflib
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any, cast


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_bytes(source: Path, ref: str, filename: str) -> bytes:
    return subprocess.check_output(
        ["git", "show", f"{ref}:{filename}"], cwd=source, stderr=subprocess.PIPE
    )


def percentages(summary: dict[str, Any]) -> dict[str, float]:
    lines, branches = summary["num_statements"], summary["num_branches"]
    return {
        "line": 100 * summary["covered_lines"] / lines if lines else 100.0,
        "branch": 100 * summary["covered_branches"] / branches if branches else 100.0,
        "combined": (
            100
            * (summary["covered_lines"] + summary["covered_branches"])
            / (lines + branches)
            if lines + branches
            else 100.0
        ),
    }


def validate_measurement(measured: dict[str, Any], source: Path) -> None:
    """Missing modules, contradictory counts, and non-branch evidence are fatal."""
    if measured["meta"]["branch_coverage"] is not True or not measured["files"]:
        raise ValueError("Complete branch-enabled coverage evidence is required")
    count_keys = ("num_statements", "covered_lines", "num_branches", "covered_branches")
    summed = dict.fromkeys(count_keys, 0)
    for filename, item in measured["files"].items():
        path = source / filename
        if not path.resolve().is_relative_to(source.resolve()) or not path.is_file():
            raise ValueError(f"Missing or invalid measured source: {filename}")
        summary = item["summary"]
        for key in count_keys:
            if type(summary[key]) is not int or summary[key] < 0:
                raise ValueError("Invalid coverage counts")
            summed[key] += summary[key]
        for covered, missing, hit_key, total_key in (
            ("executed_lines", "missing_lines", "covered_lines", "num_statements"),
            (
                "executed_branches",
                "missing_branches",
                "covered_branches",
                "num_branches",
            ),
        ):
            hits: set[Any] = {
                tuple(cast(list[int], x)) if isinstance(x, list) else x
                for x in item[covered]
            }
            misses: set[Any] = {
                tuple(cast(list[int], x)) if isinstance(x, list) else x
                for x in item[missing]
            }
            if (
                len(hits) != len(item[covered])
                or len(misses) != len(item[missing])
                or hits & misses
                or len(hits) != summary[hit_key]
                or len(hits | misses) != summary[total_key]
            ):
                raise ValueError(f"Inconsistent coverage evidence: {filename}")
        if set(item["excluded_lines"]) & set(
            item["executed_lines"] + item["missing_lines"]
        ):
            raise ValueError("Excluded and measured lines overlap")
    if any(measured["totals"][key] != value for key, value in summed.items()):
        raise ValueError("Coverage totals disagree with file evidence")
    production = source / "src/judgmentllm"
    if production.is_dir():
        expected = {p.relative_to(source).as_posix() for p in production.rglob("*.py")}
        if expected != set(measured["files"]):
            raise ValueError("Coverage omitted or added production modules")


def compare(
    current: Path,
    baseline: Path | None,
    source: Path,
    *,
    reviewed_ref: str | None = None,
) -> dict[str, Any]:
    """Use a trusted base's floor, never a PR's proposed replacement.

    A modified module needs committed source, a hash-verified historical snapshot,
    all changed executable lines/branch origins covered, mapped previously covered
    equal lines retained, and no file metric loss over one percentage point.
    Overall historical ratios and the 90% combined floor remain blocking.
    Branch identities across edited source are explicitly NOT claimed equivalent.
    Archive/release runs must reconcile modified baselines before staging.
    """
    result: dict[str, Any] = {"passed": False, "losses": [], "modified": []}
    try:
        measured = json.loads(current.read_text(encoding="utf-8"))
        validate_measurement(measured, source)
        totals = measured["totals"]
        rates = percentages(totals)
        result.update(
            totals=totals,
            **{f"{key}_percent": value for key, value in rates.items()},
        )
        if rates["combined"] < 90:
            result["losses"].append({"reason": "combined coverage below 90%"})
        if baseline is None:
            raise ValueError("A trusted coverage baseline is required")
        original = json.loads(baseline.read_text(encoding="utf-8"))
        project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
        if project["tool"]["coverage"] != original["measurement_settings"]:
            raise ValueError("Coverage measurement settings changed")
        # pytest CLI defaults can otherwise silently disable branches or lower the gate.
        expected_addopts = original.get("pytest_addopts")
        if expected_addopts is not None and (
            project["tool"]["pytest"]["ini_options"]["addopts"] != expected_addopts
        ):
            raise ValueError("pytest coverage/selection defaults changed")
        for metric, before in percentages(original["totals"]).items():
            if rates[metric] + 1e-9 < before:
                result["losses"].append(
                    {"metric": metric, "reason": "overall coverage decreased"}
                )
        if not set(original["files"]) <= set(measured["files"]):
            raise ValueError("Baseline module evidence is missing")
        for filename, actual in measured["files"].items():
            expected = original["files"].get(filename)
            content = (source / filename).read_bytes()
            if expected is not None and digest(content) == expected["source_sha256"]:
                if (
                    any(
                        actual["summary"][key] != value
                        for key, value in expected["denominators"].items()
                    )
                    or actual["excluded_lines"] != expected["excluded_lines"]
                ):
                    raise ValueError(
                        f"Coverage denominators/exclusions changed: {filename}"
                    )
                lines = sorted(
                    set(expected["executed_lines"]) - set(actual["executed_lines"])
                )
                arcs = sorted(
                    {
                        (a, b)
                        for a, b in cast(list[list[int]], expected["executed_branches"])
                    }
                    - {
                        (a, b)
                        for a, b in cast(list[list[int]], actual["executed_branches"])
                    }
                )
                if lines or arcs:
                    result["losses"].append(
                        {
                            "file": filename,
                            "missing_lines": lines,
                            "missing_branches": arcs,
                        }
                    )
                continue
            if (
                reviewed_ref is None
                or git_bytes(source, reviewed_ref, filename) != content
            ):
                raise ValueError(f"Unreviewed production source change: {filename}")
            old = b""
            if expected is not None:
                snapshot = expected.get(
                    "source_commit",
                    original.get("source_snapshot_commit", original["source_commit"]),
                )
                old = git_bytes(source, snapshot, filename)
                if digest(old) != expected["source_sha256"]:
                    raise ValueError(f"Historical source hash mismatch: {filename}")
            mapping: dict[int, int] = {}
            for a, b, length in difflib.SequenceMatcher(
                None,
                old.decode("utf-8").splitlines(),
                content.decode("utf-8").splitlines(),
                autojunk=False,
            ).get_matching_blocks():
                mapping.update(
                    {a + offset + 1: b + offset + 1 for offset in range(length)}
                )
            unchanged = set(mapping.values())
            changed = (
                set(actual["executed_lines"] + actual["missing_lines"]) - unchanged
            )
            missing = sorted(changed & set(actual["missing_lines"]))
            missing_arcs = [
                arc for arc in actual["missing_branches"] if arc[0] in changed
            ]
            mapped_hits: set[int] = (
                {
                    mapping[line]
                    for line in expected["executed_lines"]
                    if line in mapping
                }
                if expected
                else set()
            )
            lost_equal_lines = sorted(mapped_hits - set(actual["executed_lines"]))
            exclusions = (
                sorted(
                    mapping[line]
                    for line in expected["excluded_lines"]
                    if line in mapping
                )
                if expected
                else []
            )
            if actual["excluded_lines"] != exclusions:
                raise ValueError(
                    f"Modified source exclusions require explicit reconciliation: {filename}"
                )
            current_rates = percentages(actual["summary"])
            previous_rates = (
                percentages(
                    {
                        **expected["denominators"],
                        "covered_lines": len(expected["executed_lines"]),
                        "covered_branches": len(expected["executed_branches"]),
                    }
                )
                if expected
                else {"line": 91.0, "branch": 81.0, "combined": 91.0}
            )
            regressions = {
                key: {"before": value, "after": current_rates[key]}
                for key, value in previous_rates.items()
                if current_rates[key] + 1.0 + 1e-9 < value
            }
            detail = {
                "file": filename,
                "changed_executable_lines": sorted(changed),
                "missing_changed_lines": missing,
                "missing_changed_branches": missing_arcs,
                "lost_equal_lines": lost_equal_lines,
                "regressions": regressions,
                "branch_correspondence": "not established across source edits; ratios and changed origins checked",
                "deleted_source_lines": len(old.decode("utf-8").splitlines())
                - len(mapping),
            }
            result["modified"].append(detail)
            if missing or missing_arcs or lost_equal_lines or regressions:
                result["losses"].append(detail)
        result["passed"] = not result["losses"]
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ZeroDivisionError,
        subprocess.CalledProcessError,
    ) as error:
        result["losses"].append({"reason": f"Invalid coverage evidence: {error}"})
    return result
