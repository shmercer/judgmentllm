"""Independently validate raw lane case receipts using only protected code/data."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from risk import lane_profile, required_steps
from selection import digest, manifest, read

sys.path.insert(0, str(Path(__file__).resolve().parent / "local"))
from local.suite import outcomes, verify_contract, verify_skips


def validate_suite(
    plan: dict[str, Any], lane: str, output: Path, run_id: str, *, scope: str = "hosted"
) -> dict[str, Any]:
    """Recompute the exact scope and require one terminal report per planned case."""
    root = output / "suite"
    summary = read(root / "summary.json")
    collection = read(root / "collection.json")
    needed = required_steps(plan, lane, execution_scope=scope)
    expected = next(item for item in plan["lanes"] if item["id"] == lane)
    profile = lane_profile(plan, lane, scope)
    selection = manifest(
        collection["collected"],
        profile=profile,
        impacted=plan["impact"],
        include_r=scope == "local",
        include_resources="resources" in needed,
    )
    if (
        type(collection.get("exit_code")) is not int
        or collection["exit_code"] != 0
        or collection.get("reports") != []
        or selection != read(root / "selection-manifest.json")
        or selection["policy_sha256"] != plan["selection_policy_sha256"]
        or summary.get("test_profile") != profile
        or summary.get("selection_sha256") != selection["selection_sha256"]
        or summary.get("stage") != "full"
        or summary.get("status") != "passed"
        or summary.get("selection_complete") is not True
        or summary.get("coverage_enabled") is not ("coverage" in needed)
        or summary["source"]["commit"] != plan["source_sha"]
        or summary["source"]["dirty"] is not False
    ):
        raise ValueError("Incomplete, stale or incorrect suite selection/identity")
    if scope == "hosted" and (
        summary.get("requested_workers") != expected["workers"]
        or summary.get("distribution") != expected["distribution"]
        or type(summary.get("workers")) is not int
        or not 1 <= summary["workers"] <= expected["workers"]
    ):
        raise ValueError("Suite scheduling differs from the hosted plan")
    provenance = summary["provenance"]
    interpreter = provenance["interpreter"]
    platform = {
        "ubuntu-24.04": "linux",
        "windows-latest": "win32",
        "macos-latest": "darwin",
    }[expected["os"]]
    if (
        provenance["source"]["commit"] != plan["source_sha"]
        or provenance["source"]["dirty"] is not False
        or provenance["qualification_run_id"] != f"{run_id}:{lane}"
        or interpreter["platform"] != platform
        or not interpreter["version"].startswith(expected["python"] + ".")
        or provenance["lockfile_sha256"] != plan["lockfile_sha256"]
    ):
        raise ValueError("Case receipt source/run/platform/interpreter/lock mismatch")
    if collection.get("provenance") != provenance:
        raise ValueError("Collection provenance mismatch")
    verify_contract(
        Path(__file__).with_name("test-contract.json"),
        {item["nodeid"] for item in collection["collected"]},
        ["tests"],
    )
    total: Counter[str] = Counter()
    required: dict[str, set[str]] = {
        name: set() for name in ("ordinary", "serial", "resource")
    }
    for case in selection["cases"]:
        if case["disposition"] == "required":
            required[case["partition"]].add(case["nodeid"])
    parts = summary["partitions"]
    names = [part["name"] for part in parts]
    if len(names) != len(set(names)) or set(names) != {
        name for name, nodes in required.items() if nodes
    }:
        raise ValueError("Missing, duplicated or unexpected case partition")
    skip_policy = read(Path(__file__).with_name("test-contract.json"))
    for part in parts:
        receipt = read(root / (part["name"] + ".json"))
        counts = outcomes(receipt, required[part["name"]])
        if (
            receipt.get("provenance") != provenance
            or type(receipt.get("exit_code")) is not int
            or receipt["exit_code"] != 0
            or type(part.get("exit_code")) is not int
            or part["exit_code"] != 0
            or counts["failed"]
            or counts["errors"]
            or part["cases"] != len(required[part["name"]])
            or part["counts"] != dict(counts)
        ):
            raise ValueError("Failed, incomplete or foreign partition receipt")
        verify_skips(
            receipt, skip_policy, platform=platform, require_r=scope == "local"
        )
        total.update(counts)
    if any(
        total[name] != summary["counts"].get(name, 0)
        for name in ("passed", "failed", "skipped", "errors")
    ) or summary["expected_cases"] != sum(total.values()):
        raise ValueError("Truncated or incorrect case totals")
    accounting = [
        {
            **case,
            "disposition": "executed"
            if case["disposition"] == "required"
            else case["disposition"],
        }
        for case in selection["cases"]
    ]
    if accounting != json.loads(
        (root / "case-accounting.json").read_text(encoding="utf-8")
    ) or summary["accounting_sha256"] != digest(accounting):
        raise ValueError("Incomplete executed/deferred/not-required case accounting")
    return summary
