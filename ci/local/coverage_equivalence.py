"""Conservative coverage comparison; line identity is valid only for equal source."""

from __future__ import annotations

import difflib
import hashlib
import math
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, cast

if __package__:
    from .coverage_authority import (
        AuthorityError,
        checked_exceptions,
        commit_tree,
        disk_bytes,
        document,
        fingerprint,
        integer_list,
        load,
    )
    from .coverage_authority import git_bytes as source_blob
else:
    from coverage_authority import (
        AuthorityError,
        checked_exceptions,
        commit_tree,
        disk_bytes,
        document,
        fingerprint,
        integer_list,
        load,
    )
    from coverage_authority import git_bytes as source_blob


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_bytes(source: Path, ref: str, filename: str) -> bytes:
    raw = source_blob(source, ref, filename)
    if raw is None:
        raise ValueError(f"Missing historical source: {filename}")
    return raw


def base_analysis(raw: bytes, config: Path) -> dict[str, Any]:
    """Analyze committed Python without importing it; only actual runs need Coverage."""
    from coverage import Coverage
    from coverage.exceptions import CoverageException

    with tempfile.TemporaryDirectory(
        prefix="judgmentllm-coverage-analysis-"
    ) as temporary:
        root = Path(temporary)
        path = root / "previous.py"
        path.write_bytes(raw)
        coverage = Coverage(
            config_file=str(config), data_file=str(root / ".coverage"), branch=True
        )
        coverage.get_data().add_arcs({str(path): []})
        output = root / "analysis.json"
        try:
            coverage.json_report(morfs=[str(path)], outfile=str(output))
        except CoverageException as error:
            raise ValueError(f"Coverage source analysis failed: {error}") from error
        return next(iter(document(output.read_bytes())["files"].values()))


def correspondence(old: bytes, current: bytes) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for a, b, length in difflib.SequenceMatcher(
        None,
        old.decode("utf-8").splitlines(),
        current.decode("utf-8").splitlines(),
        autojunk=False,
    ).get_matching_blocks():
        mapping.update({a + i + 1: b + i + 1 for i in range(length)})
    return mapping


def projected_exclusions(
    old: bytes,
    current: bytes,
    expected: dict[str, Any] | None,
    actual: dict[str, Any],
    mapping: dict[int, int],
) -> list[int]:
    if expected is None:
        return []
    exclusions = {mapping[n] for n in expected["excluded_lines"] if n in mapping}
    old_eof, new_eof = (
        len(old.decode().splitlines()) + 1,
        len(current.decode().splitlines()) + 1,
    )
    if old_eof in expected["excluded_lines"] and new_eof in actual["excluded_lines"]:
        # Virtual EOF correspondence never enters the executable/arc mapping.
        exclusions.add(new_eof)
    return sorted(exclusions)


def validate_lists(item: dict[str, Any]) -> None:
    for key in ("executed_lines", "excluded_lines"):
        integer_list(item[key])
    integer_list(item["executed_branches"], arcs=True)
    for key in ("missing_lines", "missing_branches"):
        if key in item:
            integer_list(item[key], arcs=key.endswith("branches"))


def bounds(item: dict[str, Any], content: bytes) -> None:
    size = len(content.decode("utf-8").splitlines())
    if any(n > size + 1 for n in item["excluded_lines"]):
        raise ValueError("Invalid exclusions beyond virtual EOF")
    if any(
        n > size
        for key in ("executed_lines", "missing_lines")
        for n in item.get(key, [])
    ):
        raise ValueError("Measured line beyond source")
    if any(
        abs(n) > size
        for key in ("executed_branches", "missing_branches")
        for arc in item.get(key, [])
        for n in arc
    ):
        raise ValueError("Measured branch endpoint beyond source")


def arcs(item: dict[str, Any]) -> set[tuple[int, int]]:
    return {
        (a, b) for a, b in item["executed_branches"] + item.get("missing_branches", [])
    }


def validate_parser(parser: dict[str, str], version: Any) -> None:
    from coverage import __version__

    if (
        sys.implementation.name != parser["implementation"]
        or f"{sys.version_info.major}.{sys.version_info.minor}" != parser["python"]
        or __version__ != parser["coverage"]
        or version != __version__
    ):
        raise ValueError(
            "Coverage authority requires the locked interpreter/Coverage context"
        )


def validate_proof_interpreter(
    interpreter: dict[str, Any] | None, parser: dict[str, str]
) -> None:
    if (
        interpreter is None
        or interpreter.get("platform") != "linux"
        or interpreter.get("implementation") != parser["implementation"]
        or not isinstance(version := interpreter.get("version"), str)
        or re.match(rf"{re.escape(parser['python'])}\.\d+", version) is None
        or not isinstance(dependencies := interpreter.get("dependencies"), dict)
        or cast(dict[str, Any], dependencies).get("coverage") != parser["coverage"]
    ):
        raise ValueError(
            "Changed coverage proof origin differs from the locked Linux interpreter/Coverage context"
        )


def validate_locked_source(
    source: Path, head: str, configuration: bytes, parser: dict[str, str]
) -> None:
    lock = git_bytes(source, head, "uv.lock")
    if (
        disk_bytes(source, "uv.lock") != lock
        or git_bytes(source, head, "pyproject.toml") != configuration
        or [
            p["version"]
            for p in tomllib.loads(lock.decode("utf-8"))["package"]
            if p["name"] == "coverage"
        ]
        != [parser["coverage"]]
    ):
        raise ValueError(
            "Candidate coverage configuration/lock differs from the locked context"
        )


def reconcile(
    entry: dict[str, Any],
    expected: dict[str, Any] | None,
    actual: dict[str, Any],
    old: bytes,
    current: bytes,
    mapping: dict[int, int],
    exclusions: list[int],
    config: Path,
    parser: dict[str, str],
    version: Any,
) -> dict[str, Any]:
    validate_parser(parser, version)
    before: dict[str, Any] = (
        base_analysis(old, config)
        if old
        else {
            "missing_lines": [],
            "executed_lines": [],
            "excluded_lines": [],
            "missing_branches": [],
            "executed_branches": [],
            "summary": {"num_statements": 0, "num_branches": 0},
        }
    )
    after = base_analysis(current, config)
    for measured, parsed, historical in (
        (expected, before, True),
        (actual, after, False),
    ):
        if measured is None:
            continue
        if (
            measured["excluded_lines"] != parsed["excluded_lines"]
            or any(
                measured["denominators" if historical else "summary"][key]
                != parsed["summary"][key]
                for key in ("num_statements", "num_branches")
            )
            or not set(measured["executed_lines"]) <= set(parsed["missing_lines"])
            or not arcs(measured) <= arcs(parsed)
            or not historical
            and (
                set(measured["executed_lines"] + measured["missing_lines"])
                != set(parsed["missing_lines"])
                or arcs(measured) != arcs(parsed)
            )
        ):
            raise ValueError(
                "Reconciliation evidence differs from authenticated parser universes"
            )
    added = sorted(set(actual["excluded_lines"]) - set(exclusions))
    removed = sorted(set(exclusions) - set(actual["excluded_lines"]))
    if (
        entry["historical_source_sha256"]
        != (expected["source_sha256"] if expected else None)
        or entry["added_excluded_lines"] != added
        or entry["removed_excluded_lines"] != removed
    ):
        raise AuthorityError(
            "Active reconciliation does not match the complete exclusion delta"
        )
    historical_statements = {
        mapping[n] for n in before["missing_lines"] if n in mapping
    }
    historical_branch_ends = {
        mapping[n] for arc in arcs(before) for n in arc if n > 0 and n in mapping
    }
    if set(added) & (historical_statements | historical_branch_ends):
        raise AuthorityError(
            "Declaration reconciliation cannot hide historical executable source"
        )

    def mapped(n: int) -> int | None:
        value = mapping.get(abs(n))
        return value * (-1 if n < 0 else 1) if value is not None else None

    exposed_lines = set(after["missing_lines"]) - {
        mapping[n] for n in before["missing_lines"] if n in mapping
    }
    previous_arcs = {(mapped(a), mapped(b)) for a, b in arcs(before)}
    exposed_arcs = arcs(after) - previous_arcs
    return {
        "newly_executable_lines": sorted(exposed_lines),
        "missing_exposed_lines": sorted(exposed_lines & set(actual["missing_lines"])),
        "newly_exposed_branches": [list(arc) for arc in sorted(exposed_arcs)],
        "missing_exposed_branches": [
            list(arc)
            for arc in sorted(
                exposed_arcs & {tuple(arc) for arc in actual["missing_branches"]}
            )
        ],
    }


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


def validate_summary(summary: dict[str, Any]) -> None:
    """Check one measurement internally, never against another provenance's sum."""
    for covered, total, missing in (
        ("covered_lines", "num_statements", "missing_lines"),
        ("covered_branches", "num_branches", "missing_branches"),
    ):
        for key in (covered, total, missing):
            if key != missing or key in summary:
                if type(summary[key]) is not int or summary[key] < 0:
                    raise ValueError(f"Invalid coverage count: {key}")
        if summary[covered] > summary[total] or (
            missing in summary and summary[covered] + summary[missing] != summary[total]
        ):
            raise ValueError("Inconsistent covered/missing coverage counts")
    for key in ("excluded_lines", "num_partial_branches"):
        if key in summary and (type(summary[key]) is not int or summary[key] < 0):
            raise ValueError(f"Invalid coverage count: {key}")
    if (
        summary.get("num_partial_branches", 0)
        > summary["num_branches"] - summary["covered_branches"]
    ):
        raise ValueError("Partial branches exceed missing branches")
    rates = percentages(summary)
    for key, metric in (
        ("percent_covered", "combined"),
        ("percent_statements_covered", "line"),
        ("percent_branches_covered", "branch"),
    ):
        rate = rates[metric]
        if key in summary and (
            type(summary[key]) not in {int, float}
            or not 0 <= summary[key] <= 100
            or not math.isclose(summary[key], rate, rel_tol=0, abs_tol=1e-9)
        ):
            raise ValueError(f"Inconsistent coverage percentage: {key}")
        display = key + "_display"
        if display not in summary:
            continue
        text = summary[display]
        if (
            not isinstance(text, str)
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text) is None
        ):
            raise ValueError(f"Invalid coverage display: {display}")
        # Retain the stored precision. Coverage never rounds a partial ratio to
        # displayed 0 or 100; numeric percentages remain unrounded.
        precision = len(text.partition(".")[2])
        near_zero = 10.0**-precision
        shown = rate
        if 0 < rate < near_zero:
            shown = near_zero
        elif 100 - near_zero < rate < 100:
            shown = 100 - near_zero
        if text != f"{shown:.{precision}f}":
            raise ValueError(f"Inconsistent coverage display: {display}")


def validate_measurement(
    measured: dict[str, Any], source: Path, *, expected_files: set[str] | None = None
) -> None:
    """Missing modules, contradictory counts, and non-branch evidence are fatal."""
    if measured["meta"]["branch_coverage"] is not True or not measured["files"]:
        raise ValueError("Complete branch-enabled coverage evidence is required")
    validate_summary(measured["totals"])
    count_keys = ("num_statements", "covered_lines", "num_branches", "covered_branches")
    summed = dict.fromkeys(count_keys, 0)
    for filename, item in measured["files"].items():
        content = disk_bytes(source, filename)
        validate_lists(item)
        bounds(item, content)
        summary = item["summary"]
        validate_summary(summary)
        for key in count_keys:
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
    if any(
        type(measured["totals"][key]) is not int or measured["totals"][key] != value
        for key, value in summed.items()
    ):
        raise ValueError("Coverage totals disagree with file evidence")
    production = source / "src/judgmentllm"
    if production.is_dir() or expected_files is not None:
        expected = (
            expected_files
            if expected_files is not None
            else {p.relative_to(source).as_posix() for p in production.rglob("*.py")}
        )
        if expected != set(measured["files"]):
            raise ValueError("Coverage omitted or added production modules")


def compare(
    current: Path,
    baseline: Path | None,
    source: Path,
    *,
    reviewed_ref: str | None = None,
    coverage_policy: dict[str, Any] | None = None,
    protected_base: str | None = None,
    passing_nodes: set[str] | None = None,
    proof_interpreter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use a trusted base's floor, never a PR's proposed replacement.

    A modified module needs committed source, a hash-verified historical snapshot,
    mapped previously covered equal lines retained, and no file metric loss over
    one percentage point. Changed misses block unless independently approved
    with passing proof from the final planned local suite. Exposed misses always block.
    Overall historical ratios and the 90% combined floor remain blocking.
    Branch identities across edited source are explicitly NOT claimed equivalent.
    Archive/release runs must reconcile modified baselines before staging.
    """
    result: dict[str, Any] = {"passed": False, "losses": [], "modified": []}
    try:
        entries: list[dict[str, Any]] = []
        if coverage_policy is not None:
            original, entries = load(source, protected_base, coverage_policy)
            if reviewed_ref is None:
                raise AuthorityError(
                    "Coverage authority requires the independently bound head"
                )
            result["source_tree_sha"] = commit_tree(source, reviewed_ref)
            result["coverage_policy_sha256"] = fingerprint(coverage_policy)
            result["authority_base_sha"] = protected_base
        elif baseline is not None:
            original = document(baseline.read_bytes())
        else:
            raise ValueError("A trusted coverage baseline is required")
        measured = document(current.read_bytes())
        validate_measurement(measured, source)
        for expected in original["files"].values():
            validate_lists(expected)
            if set(expected["excluded_lines"]) & set(expected["executed_lines"]):
                raise ValueError("Historical excluded and measured lines overlap")
            if any(
                type(n) is not int or n < 0 for n in expected["denominators"].values()
            ):
                raise ValueError("Invalid historical denominators")
            if any(
                len(expected[key]) > expected["denominators"][denominator]
                for key, denominator in (
                    ("executed_lines", "num_statements"),
                    ("executed_branches", "num_branches"),
                )
            ):
                raise ValueError("Historical hits exceed denominators")
        # Schema-1 totals retain the original aggregate measurement; reconciled
        # per-file entries can have a later source/evidence provenance.
        validate_summary(original["totals"])
        prepared: dict[str, Any] = {}
        applied: list[dict[str, Any]] = []
        for filename, actual in measured["files"].items():
            expected = original["files"].get(filename)
            content = disk_bytes(source, filename)
            same = expected is not None and digest(content) == expected["source_sha256"]
            if (
                same
                and expected is not None
                and (
                    actual["excluded_lines"] != expected["excluded_lines"]
                    or any(
                        actual["summary"][key] != value
                        for key, value in expected["denominators"].items()
                    )
                )
            ):
                raise ValueError(
                    f"Unchanged source denominators/exclusions changed: {filename}"
                )
            if not same or coverage_policy is not None:
                if (
                    reviewed_ref is None
                    or git_bytes(source, reviewed_ref, filename) != content
                ):
                    raise ValueError(f"Unreviewed production source change: {filename}")
            old = content if same else b""
            if expected is not None and not same:
                snapshot = expected.get(
                    "source_commit",
                    original.get("source_snapshot_commit", original["source_commit"]),
                )
                old = git_bytes(source, snapshot, filename)
                if digest(old) != expected["source_sha256"]:
                    raise ValueError(f"Historical source hash mismatch: {filename}")
            if expected is not None:
                bounds(expected, old)
            mapping = correspondence(old, content)
            exclusions = projected_exclusions(old, content, expected, actual, mapping)
            prepared[filename] = (content, old, mapping, exclusions)
        totals = measured["totals"]
        rates = percentages(totals)
        result.update(
            totals=totals,
            **{f"{key}_percent": value for key, value in rates.items()},
        )
        if rates["combined"] < 90:
            result["losses"].append({"reason": "combined coverage below 90%"})
        configuration = disk_bytes(source, "pyproject.toml")
        project = tomllib.loads(configuration.decode("utf-8"))
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
        exposures: dict[str, dict[str, Any]] = {}
        for entry in entries:
            filename = entry["path"]
            if (
                filename not in prepared
                or digest(prepared[filename][0]) != entry["candidate_source_sha256"]
            ):
                continue
            content, old, mapping, exclusions = prepared[filename]
            parser = cast(dict[str, Any], coverage_policy)["parser"]
            assert reviewed_ref is not None
            validate_locked_source(source, reviewed_ref, configuration, parser)
            exposures[filename] = reconcile(
                entry,
                original["files"].get(filename),
                measured["files"][filename],
                old,
                content,
                mapping,
                exclusions,
                source / "pyproject.toml",
                parser,
                measured["meta"].get("version"),
            )
            applied.append(entry)
        if coverage_policy is not None:
            result["applied_reconciliations"] = applied
        matched: dict[str, list[dict[str, Any]]] = {}
        approved: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        if (
            passing_nodes is not None
            and coverage_policy is not None
            and "exceptions_sha256" in coverage_policy
        ):
            assert protected_base is not None and reviewed_ref is not None
            exceptions, scope = checked_exceptions(
                source, protected_base, result["source_tree_sha"], coverage_policy
            )
            if exceptions:
                for entry in exceptions:
                    if entry["scope_sha256"] == scope:
                        matched.setdefault(entry["path"], []).append(entry)
                    else:
                        rejected.append(
                            {"entry": entry, "reason": "stale source scope"}
                        )
                result["changed_coverage_exceptions"] = {
                    "applied": approved,
                    "rejected": rejected,
                }
            if matched:
                if sys.platform != "linux":
                    raise ValueError(
                        "Changed coverage exceptions require the local Linux lane"
                    )
                parser = coverage_policy["parser"]
                validate_proof_interpreter(proof_interpreter, parser)
                validate_parser(parser, measured["meta"].get("version"))
                validate_locked_source(source, reviewed_ref, configuration, parser)
        for filename, actual in measured["files"].items():
            expected = original["files"].get(filename)
            content, old, mapping, exclusions = prepared[filename]
            if expected is not None and digest(content) == expected["source_sha256"]:
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
            unchanged = set(mapping.values())
            changed = (
                set(actual["executed_lines"] + actual["missing_lines"]) - unchanged
            )
            missing = sorted(changed & set(actual["missing_lines"]))
            missing_arcs = [
                arc
                for arc in cast(list[list[int]], actual["missing_branches"])
                if arc[0] in changed
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
            if actual["excluded_lines"] != exclusions and filename not in exposures:
                result["losses"].append(
                    {
                        "file": filename,
                        "reason": "Modified source exclusions require explicit reconciliation",
                        "added_excluded_lines": sorted(
                            set(actual["excluded_lines"]) - set(exclusions)
                        ),
                        "removed_excluded_lines": sorted(
                            set(exclusions) - set(actual["excluded_lines"])
                        ),
                    }
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
            exposed = exposures.get(filename, {})
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
                **exposed,
            }
            result["modified"].append(detail)
            uncovered_lines = set(missing)
            uncovered_arcs = set(map(tuple, missing_arcs))
            for entry in matched.pop(filename, []):
                assert passing_nodes is not None
                lines = set(entry["lines"])
                branches = set(map(tuple, cast(list[list[int]], entry["arcs"])))
                if lines.intersection(exposed.get("newly_executable_lines", [])) or (
                    branches.intersection(
                        map(tuple, exposed.get("newly_exposed_branches", []))
                    )
                ):
                    reason = "newly exposed outcome"
                elif not lines <= uncovered_lines or not branches <= uncovered_arcs:
                    reason = "outcomes are no longer uncovered changed code"
                elif not passing_nodes.issuperset(entry["test_nodeids"]):
                    reason = "missing passing proof tests"
                else:
                    approved.append(entry)
                    uncovered_lines.difference_update(lines)
                    uncovered_arcs.difference_update(branches)
                    continue
                rejected.append({"entry": entry, "reason": reason})
            if (
                uncovered_lines
                or uncovered_arcs
                or lost_equal_lines
                or regressions
                or any(
                    exposed.get(key)
                    for key in ("missing_exposed_lines", "missing_exposed_branches")
                )
            ):
                result["losses"].append(detail)
        for remaining in matched.values():
            rejected.extend(
                {
                    "entry": entry,
                    "reason": "outcomes are no longer uncovered changed code",
                }
                for entry in remaining
            )
        result["passed"] = not result["losses"]
    except (
        OSError,
        ImportError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
        SyntaxError,
        ZeroDivisionError,
        subprocess.CalledProcessError,
    ) as error:
        result["fatal_error"] = (
            "invalid-authority"
            if isinstance(error, AuthorityError)
            else "invalid-evidence"
        )
        result["losses"].append({"reason": f"Invalid coverage evidence: {error}"})
    return result
