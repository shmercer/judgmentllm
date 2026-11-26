"""Seal normal pytest-cov group data, then combine only authorized inputs."""

from __future__ import annotations

import hashlib
import json
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from coverage import Coverage, CoverageData


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def context_name(identity: dict[str, Any], group: str) -> str:
    if not isinstance(identity.get("attempt_id"), str) or not identity["attempt_id"]:
        raise ValueError("Coverage requires a unique execution attempt")
    return "ci:" + canonical({"provenance": identity, "group": group})


def prepare_config(
    source: Path, directory: Path, identity: dict[str, Any], group: str
) -> Path:
    text = (source / "pyproject.toml").read_text()
    settings = tomllib.loads(text)
    if (
        "context" in settings["tool"]["coverage"]["run"]
        or text.count("[tool.coverage.run]") != 1
    ):
        raise ValueError("Static coverage context requires a reviewed configuration")
    context = context_name(identity, group)
    path = directory / "pyproject.toml"
    path.write_text(
        text.replace(
            "[tool.coverage.run]",
            "[tool.coverage.run]\ncontext = " + json.dumps(context),
            1,
        )
    )
    return path


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
    context = context_name(identity, group)
    config = data_file.parent / "pyproject.toml"
    original = tomllib.loads((source / "pyproject.toml").read_text())
    configured = tomllib.loads(config.read_text())
    if (
        configured["tool"]["coverage"]["run"].pop("context", None) != context
        or configured != original
    ):
        raise ValueError("Coverage group configuration changed protected settings")
    no_hits = not data.measured_files()
    if not data.has_arcs() and not no_hits:
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
    if any(
        value != context and not value.startswith(context + "|") for value in contexts
    ):
        raise ValueError("Foreign source, run, group or attempt coverage context")
    for contributor in contributors:
        if (
            contributor.get("complete") is not True
            or contributor.get("branch") is not True
            or contributor.get("static_context") != context
            or not set(contributor.get("contexts", [])) <= contexts
        ):
            raise ValueError("Missing coverage from an active contributor")
    if no_hits and any(contributor.get("contexts") for contributor in contributors):
        raise ValueError("Empty dataset lost active production contributions")
    result = {
        "schema_version": 1,
        "group": group,
        "provenance": identity,
        "data_file": str(data_file.resolve()),
        "sha256": checksum(data_file),
        "receipt_sha256": canonical(receipt),
        "sources": measured_paths(data, source),
        "contributors": contributors,
        "no_production_hits": no_hits,
        "configuration_sha256": checksum(config),
        "context": context,
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


def proof_interpreter(
    receipts: dict[str, dict[str, Any]], passing_nodes: set[str] | None
) -> dict[str, Any] | None:
    if passing_nodes is None:
        return None
    # Sealing checked that every group has the same originating provenance.
    return next(iter(receipts.values()))["provenance"]["interpreter"]


def validate_suite_coverage(
    root: Path,
    source: Path,
    plan: dict[str, Any],
    identity: dict[str, Any],
    receipts: dict[str, dict[str, Any]],
    *,
    passing_nodes: set[str] | None = None,
) -> dict[str, Any]:
    """Independently rebuild from passing, explicitly authorized group artifacts."""
    from coverage_equivalence import compare
    from selection import read

    manifest = read(root / "coverage-manifest.json")
    declared = read(root / "coverage.json")
    declared_comparison = read(root / "coverage-comparison.json")
    if manifest.get("reports") != {
        name: checksum(root / name)
        for name in ("coverage.json", "coverage.xml", "coverage-comparison.json")
    }:
        raise ValueError("Missing, corrupt or stale coverage reports")
    groups = manifest["groups"]
    if manifest.get("provenance") != identity or {g["group"] for g in groups} != set(
        receipts
    ):
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
                or {
                    line: set(values)
                    for line, values in actual.contexts_by_lineno(filename).items()
                }
                != {
                    line: set(values)
                    for line, values in rebuilt.contexts_by_lineno(filename).items()
                }
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
        rebuilt_json = read(output)
        if (
            any(
                declared.get(key) != rebuilt_json.get(key)
                for key in ("files", "totals")
            )
            or declared.get("meta", {}).get("branch_coverage") is not True
        ):
            raise ValueError("Missing, incomplete or malformed final coverage report")
        baseline = Path(__file__).resolve().parents[1] / "coverage-floor.json"
        if targets is not None:
            from targeted_coverage import compare as targeted

            result = targeted(
                output, source, plan["base_sha"], plan["source_sha"], targets, baseline
            )
        else:
            result = compare(
                output,
                baseline,
                source,
                reviewed_ref=plan["source_sha"],
                passing_nodes=passing_nodes,
                proof_interpreter=proof_interpreter(receipts, passing_nodes),
                **(
                    {
                        "coverage_policy": plan["coverage_policy"],
                        "protected_base": plan["base_sha"],
                    }
                    if plan.get("coverage_policy") is not None
                    else {}
                ),
            )
            if plan.get("coverage_policy") is not None and (
                result.get("coverage_policy_sha256")
                != plan.get("coverage_policy_sha256")
                or result.get("source_tree_sha") != plan.get("source_tree_sha")
            ):
                raise ValueError(
                    "Rebuilt coverage authority/tree differs from the trusted plan"
                )
        if result.get("passed") is not True:
            raise ValueError(
                f"Independently rebuilt coverage fails admission: {result}"
            )
        if declared_comparison != result:
            raise ValueError(
                "Stored coverage comparison differs from independent admission"
            )
        return result
