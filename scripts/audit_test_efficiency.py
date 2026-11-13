"""Read three retained suite receipts; report inventory, runtime and coverage changes."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, cast

from ci.local.coverage_equivalence import percentages


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot(directory: Path) -> dict[str, Any]:
    summary = read(directory / "summary.json")
    coverage = read(directory / "coverage.json")
    groups: Counter[str] = Counter()
    setups: Counter[str] = Counter()
    slow: list[dict[str, Any]] = []
    measurements: dict[str, Any] = {}
    for phase in ("collection", "ordinary", "serial", "resource"):
        path = directory / (phase + ".json")
        if not path.exists():
            continue
        for report in read(path)["reports"]:
            name = report["nodeid"].split("::")[0]
            groups[name] += report["seconds"]
            if report["phase"] == "setup":
                setups[name] += report["seconds"]
            slow.append(report)
        process = directory / (phase + "-processes.json")
        if process.exists():
            measurements[phase] = {
                key: value for key, value in read(process).items() if key != "samples"
            }
    return {
        "directory": str(directory.resolve()),
        "source": summary["source"],
        "status": summary["status"],
        "counts": summary["counts"],
        "workers": summary["workers"],
        "distribution": summary["distribution"],
        "resources": summary["resources"],
        "duration_seconds": summary["duration_seconds"],
        "partitions": summary["partitions"],
        "coverage": percentages(coverage["totals"]),
        "coverage_totals": coverage["totals"],
        "historical_comparison_passed": summary["coverage"]["passed"],
        "module_coverage": {
            name: percentages(item["summary"])
            for name, item in coverage["files"].items()
        },
        "costliest_groups_worker_seconds": groups.most_common(12),
        "costliest_setups_worker_seconds": setups.most_common(12),
        "slowest_phases": sorted(slow, key=lambda r: r["seconds"], reverse=True)[:20],
        "process_measurements": measurements,
        "evidence_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.glob("*.json"))
        },
    }


def coverage_losses(
    before: dict[str, Any], after: dict[str, Any], unchanged: set[str]
) -> list[dict[str, Any]]:
    """Compare exact line/arc identities only where complete source bytes match."""
    losses: list[dict[str, Any]] = []
    for name in sorted(unchanged):
        old, new = before["files"][name], after["files"][name]
        lines = sorted(set(old["executed_lines"]) - set(new["executed_lines"]))
        arcs = sorted(
            {(a, b) for a, b in cast(list[list[int]], old["executed_branches"])}
            - {(a, b) for a, b in cast(list[list[int]], new["executed_branches"])}
        )
        if (
            lines
            or arcs
            or old["excluded_lines"] != new["excluded_lines"]
            or any(
                old["summary"][key] != new["summary"][key]
                for key in ("num_statements", "num_branches")
            )
        ):
            losses.append({"module": name, "lost_lines": lines, "lost_arcs": arcs})
    return losses


def compare(source: Path, before: Path, after: Path) -> dict[str, Any]:
    def blobs(directory: Path) -> dict[str, str]:
        ref = read(directory / "summary.json")["source"]["commit"]
        raw = subprocess.check_output(
            ["git", "ls-tree", "-r", ref, "src/judgmentllm"], cwd=source, text=True
        )
        return {row.split("\t")[1]: row.split()[2] for row in raw.splitlines()}

    old, new = read(before / "coverage.json"), read(after / "coverage.json")
    old_blobs, new_blobs = blobs(before), blobs(after)
    common = set(old["files"]) & set(new["files"])
    unchanged = {name for name in common if old_blobs[name] == new_blobs[name]}

    def inventory(directory: Path) -> set[str]:
        return {r["nodeid"] for r in read(directory / "collection.json")["collected"]}

    old_ids, new_ids = inventory(before), inventory(after)
    return {
        "unchanged_modules": sorted(unchanged),
        "changed_modules": sorted(common - unchanged),
        "branch_identity_across_changed_modules": "not established; use retained historical reconciliation",
        "unchanged_source_coverage_losses": coverage_losses(old, new, unchanged),
        "overall_before": percentages(old["totals"]),
        "overall_after": percentages(new["totals"]),
        "test_inventory": {
            "before_count": len(old_ids),
            "after_count": len(new_ids),
            "added": sorted(new_ids - old_ids),
            "removed": sorted(old_ids - new_ids),
            "retained": sorted(old_ids & new_ids),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    for name in ("baseline", "before", "after", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = {
        "schema_version": 1,
        "baseline": snapshot(args.baseline),
        "before": snapshot(args.before),
        "after": snapshot(args.after),
        "baseline_to_features": compare(args.source, args.baseline, args.before),
        "features_to_final": compare(args.source, args.before, args.after),
    }
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write("\n")


if __name__ == "__main__":
    main()
