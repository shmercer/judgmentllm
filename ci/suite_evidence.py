"""Independently validate raw lane case receipts using only protected code/data."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from risk import lane_profile, required_steps
from selection import digest, manifest, read, validate_candidate_metadata

sys.path.insert(0, str(Path(__file__).resolve().parent / "local"))
from local.suite import outcomes, verify_contract, verify_skips


def validate_suite(
    plan: dict[str, Any],
    lane: str,
    output: Path,
    run_id: str,
    *,
    scope: str = "hosted",
    partition: str = "all",
    source: Path | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Recompute the exact scope and require one terminal report per planned case."""
    root = output / "suite"
    summary = read(root / "summary.json")
    collection = read(root / "collection.json")
    needed = required_steps(plan, lane, execution_scope=scope, partition=partition)
    expected = next(item for item in plan["lanes"] if item["id"] == lane)
    if scope == "hosted" and not any(
        item["id"] == lane and item["partition"] == partition
        for item in plan["hosted_tasks"]
    ):
        raise ValueError("Unexpected hosted grid partition")
    platform = {
        "ubuntu-24.04": "linux",
        "windows-latest": "win32",
        "macos-latest": "darwin",
    }[expected["os"]]
    profile = lane_profile(plan, lane, scope)
    selection = manifest(
        collection["collected"],
        profile=profile,
        impacted=plan["impact"],
        include_r=scope == "local",
        include_resources="resources" in needed,
        platform=platform,
        partition=partition,
        policy=plan.get("selection_policy", {}).get("taxonomy"),
        known=plan.get("selection_policy", {}).get("inventory"),
        resource_selectors=plan.get("resource_selectors") if scope == "local" else None,
    )
    if source is not None:
        validate_candidate_metadata(source, plan, collection["collected"])
    coverage_enabled = "coverage" in needed and not (
        plan.get("coverage_mode") == "targeted" and not plan.get("coverage_targets")
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
        or summary.get("grid_partition", "all") != partition
        or summary.get("status") != "passed"
        or summary.get("qualification", True) is not True
        or summary.get("selection_complete") is not True
        or summary.get("coverage_enabled") is not coverage_enabled
        or summary["source"]["commit"] != plan["source_sha"]
        or summary["source"]["dirty"] is not False
    ):
        raise ValueError("Incomplete, stale or incorrect suite selection/identity")
    if scope == "hosted" and (
        summary.get("requested_workers")
        != (1 if partition == "serial" else expected["workers"])
        or summary.get("distribution") != expected["distribution"]
        or type(summary.get("workers")) is not int
        or not 1
        <= summary["workers"]
        <= (1 if partition == "serial" else expected["workers"])
    ):
        raise ValueError("Suite scheduling differs from the hosted plan")
    provenance = summary["provenance"]
    if plan.get("schema_version", 1) >= 2 and (
        not isinstance(provenance.get("attempt_id"), str)
        or not provenance["attempt_id"]
        or attempt_id is not None
        and provenance["attempt_id"] != attempt_id
    ):
        raise ValueError("Missing or foreign qualification attempt")
    interpreter = provenance["interpreter"]
    execution_id = f"{run_id}:{lane}" + (f":{partition}" if partition != "all" else "")
    expected_locks = {plan["lockfile_sha256"]}
    if platform == "win32":
        expected_locks.add(plan["windows_crlf_lockfile_sha256"])
    if (
        provenance["source"]["commit"] != plan["source_sha"]
        or provenance["source"]["dirty"] is not False
        or provenance["qualification_run_id"] != execution_id
        or interpreter["platform"] != platform
        or not interpreter["version"].startswith(expected["python"] + ".")
        or provenance["lockfile_sha256"] not in expected_locks
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
    if scope == "local" and summary.get("local_schedule") == "concurrent":
        if source is None:
            raise ValueError(
                "Local serial placement requires exact source verification"
            )
        from local.policy import serial_groups

        grouped = serial_groups(
            sorted(required.pop("serial")), plan["local_policy"], source
        )
        for placement, nodes in grouped.items():
            label = {
                "concurrency-safe-serial": "serial-safe",
                "host-exclusive-serial": "serial-host",
                "globally-exclusive-serial": "serial-global",
            }[placement]
            required[label] = set(nodes)
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
    if (
        scope == "local"
        and plan.get("schema_version", 1) >= 2
        and set(summary.get("r_gates", {})) != set(plan["r_gates"])
    ):
        raise ValueError("Required named R gates were not independently executed")
    if scope == "local" and coverage_enabled and plan.get("schema_version", 1) >= 2:
        if source is None:
            raise ValueError("Coverage admission requires exact source")
        from local.coverage_data import validate_suite_coverage

        rebuilt = validate_suite_coverage(root, source, plan, provenance, set(names))
        if rebuilt != summary.get("coverage"):
            raise ValueError("Declared coverage differs from independent admission")
    if scope == "local" and plan.get("schema_version", 1) >= 2:
        from local.orchestrator import validate_execution

        logical = {part["name"]: part.get("partition", part["name"]) for part in parts}
        execution = read(root / "execution.json")
        if execution.get("provenance") != provenance:
            raise ValueError("Foreign scheduler source/run/attempt manifest")
        validate_execution(
            execution,
            logical,
            plan["r_gates"],
            summary["local_schedule"],
        )
        for name in ["collection", *names]:
            processes = read(root / (name + "-processes.json"))
            if (
                processes.get("cleanup_complete") is not True
                or processes.get("identity")
                != {"provenance": provenance, "group": name}
                or processes.get("cancelled") is not False
                or processes.get("leaked_descendants") is not False
                or processes.get("command_exit_code") != 0
            ):
                raise ValueError(
                    "Incomplete, interrupted or leaked local process ownership"
                )
    if scope == "local" and summary.get("r_gates"):
        from local.r_evidence import validate

        if set(summary["r_gates"]) != set(plan["r_gates"]):
            raise ValueError("Missing or unexpected named R gate")
        for name, item in summary["r_gates"].items():
            gate_root = root / "r-gates" / name
            if (
                item.get("exit_code") != 0
                or Path(item["receipt"]).resolve()
                != (gate_root / "receipt.json").resolve()
            ):
                raise ValueError("Foreign or failed named R gate")
            validate(name, gate_root, read(gate_root / "receipt.json"), provenance)
    return summary
