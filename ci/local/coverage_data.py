"""Seal normal pytest-cov group data, then combine only authorized inputs."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from coverage import Coverage, CoverageData


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def measured_paths(data: CoverageData, source: Path) -> dict[str, str]:
    paths: dict[str, str] = {}
    for filename in data.measured_files():
        path = (source / filename).resolve()
        if (
            not path.is_relative_to(source.resolve())
            or not path.is_file()
            or path.suffix != ".py"
        ):
            raise ValueError(f"Unexpected measured source: {filename}")
        relative = path.relative_to(source.resolve()).as_posix()
        if (source / "src/judgmentllm").is_dir() and not relative.startswith(
            "src/judgmentllm/"
        ):
            raise ValueError(f"Unexpected production coverage path: {filename}")
        paths[relative] = checksum(path)
    return paths


def seal(
    data_file: Path,
    receipt: dict[str, Any],
    source: Path,
    identity: dict[str, Any],
    group: str,
) -> dict[str, Any]:
    """No worker snapshots: pytest-cov has already finished normal combination."""
    if (
        type(receipt.get("exit_code")) is not int
        or receipt["exit_code"] != 0
        or any(
            report.get("outcome") == "failed" or report.get("xfail") is True
            for report in receipt.get("reports", [])
        )
    ):
        raise ValueError("Failed stages cannot contribute coverage")
    if receipt.get("provenance") != identity:
        raise ValueError("Coverage stage identity differs")
    if receipt.get("execution_group") != group:
        raise ValueError("Coverage execution group identity differs")
    from suite import outcomes

    nodes = {item["nodeid"] for item in receipt.get("collected", [])}
    if not nodes:
        raise ValueError("Coverage stage has no completed case selection")
    counts = outcomes(receipt, nodes)
    if counts["failed"] or counts["errors"]:
        raise ValueError("Incomplete coverage stage execution")
    data = CoverageData(basename=str(data_file))
    data.read()
    if not data.has_arcs() or not data.measured_files():
        raise ValueError("Complete branch-enabled coverage data is required")
    contributors = receipt.get("coverage_contributors", [])
    identifiers = [item.get("worker") for item in contributors]
    if (
        not contributors
        or len(identifiers) != len(set(identifiers))
        or sorted(identifiers)
        != sorted(receipt.get("coverage_expected_contributors", []))
    ):
        raise ValueError("Missing or duplicate coverage contributors")
    contexts = data.measured_contexts()
    for contributor in contributors:
        if (
            contributor.get("complete") is not True
            or contributor.get("branch") is not True
            or not set(contributor.get("contexts", [])) <= contexts
        ):
            raise ValueError("Missing coverage from an active contributor")
    result = {
        "schema_version": 1,
        "group": group,
        "provenance": identity,
        "data_file": str(data_file.resolve()),
        "sha256": checksum(data_file),
        "receipt_sha256": canonical(receipt),
        "sources": measured_paths(data, source),
        "contributors": contributors,
    }
    result["manifest_sha256"] = canonical(result)
    return result


def combine(
    groups: list[dict[str, Any]],
    receipts: dict[str, dict[str, Any]],
    source: Path,
    identity: dict[str, Any],
    output: Path,
) -> dict[str, Any]:
    """Revalidate every exact group, preserve inputs and create a fresh dataset."""
    names = [group.get("group") for group in groups]
    if not groups or len(names) != len(set(names)) or set(names) != set(receipts):
        raise ValueError("Missing, duplicate or unauthorized coverage groups")
    if output.exists():
        raise ValueError("Combined coverage output must be fresh")
    output.parent.mkdir(parents=True, exist_ok=True)
    merged = CoverageData(basename=str(output))
    for group in sorted(groups, key=lambda item: item["group"]):
        actual = seal(
            Path(group["data_file"]),
            receipts[group["group"]],
            source,
            identity,
            group["group"],
        )
        if canonical(actual) != canonical(group):
            raise ValueError("Corrupt, foreign or stale coverage artifact")
        item = CoverageData(basename=group["data_file"])
        item.read()
        merged.update(item)
    merged.write()
    return {
        "schema_version": 1,
        "provenance": identity,
        "groups": groups,
        "combined_sha256": checksum(output),
    }


def report(
    data_file: Path, source: Path, output: Path, targets: list[str] | None = None
) -> None:
    coverage = Coverage(
        data_file=str(data_file), config_file=str(source / "pyproject.toml")
    )
    coverage.load()
    coverage.json_report(outfile=str(output), morfs=targets, ignore_errors=False)
    if output.name == "coverage.json":
        coverage.xml_report(
            outfile=str(output.with_suffix(".xml")), morfs=targets, ignore_errors=False
        )


def validate_suite_coverage(
    root: Path,
    source: Path,
    plan: dict[str, Any],
    identity: dict[str, Any],
    names: set[str],
) -> dict[str, Any]:
    """Independently rebuild from passing, explicitly authorized group artifacts."""
    from coverage_equivalence import compare

    manifest = json.loads((root / "coverage-manifest.json").read_text())
    groups = manifest["groups"]
    if manifest.get("provenance") != identity or {g["group"] for g in groups} != names:
        raise ValueError("Coverage execution groups differ from required partitions")
    for group in groups:
        expected = root / "coverage-groups" / group["group"] / ".coverage"
        if (
            Path(group["data_file"]).resolve() != expected.resolve()
            or expected.is_symlink()
        ):
            raise ValueError("Unauthorized coverage artifact location")
    final = root / ".coverage"
    if manifest.get("combined_sha256") != checksum(final):
        raise ValueError("Final coverage dataset changed")
    receipts = {
        name: json.loads((root / (name + ".json")).read_text()) for name in names
    }
    with tempfile.TemporaryDirectory(
        prefix="judgmentllm-verify-coverage-"
    ) as temporary:
        directory = Path(temporary)
        data = directory / ".coverage"
        combine(groups, receipts, source, identity, data)
        actual = CoverageData(basename=str(final))
        actual.read()
        rebuilt = CoverageData(basename=str(data))
        rebuilt.read()
        if (
            actual.measured_files() != rebuilt.measured_files()
            or actual.measured_contexts() != rebuilt.measured_contexts()
            or any(
                set(actual.arcs(filename) or []) != set(rebuilt.arcs(filename) or [])
                or actual.contexts_by_lineno(filename)
                != rebuilt.contexts_by_lineno(filename)
                for filename in rebuilt.measured_files()
            )
        ):
            raise ValueError(
                "Combined coverage does not equal every authorized contribution"
            )
        output = directory / "coverage.json"
        targets = (
            plan["coverage_targets"] if plan["coverage_mode"] == "targeted" else None
        )
        report(data, source, output, targets)
        baseline = Path(__file__).resolve().parents[1] / "coverage-floor.json"
        if targets is not None:
            from targeted_coverage import compare as targeted

            result = targeted(
                output, source, plan["base_sha"], plan["source_sha"], targets, baseline
            )
        else:
            result = compare(output, baseline, source, reviewed_ref=plan["source_sha"])
        if result.get("passed") is not True:
            raise ValueError(
                f"Independently rebuilt coverage fails admission: {result}"
            )
        return result
