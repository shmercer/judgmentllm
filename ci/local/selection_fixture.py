"""Synthetic raw receipts for adversarial policy tests; no executed-test claims."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from risk import lane_profile
from selection import HERE, digest, inventory_items, manifest, read


def write_suite(plan: dict[str, Any], lane: str, root: Path) -> str:
    items = inventory_items(read(HERE / "test-inventory.json"))
    selected = manifest(
        items,
        profile=lane_profile(plan, lane, "hosted"),
        impacted=plan["impact"],
        include_r=False,
        include_resources=False,
    )
    platform = {"linux": "linux", "windows": "win32", "macos": "darwin"}[
        lane.split("-")[0]
    ]
    version = next(item["python"] for item in plan["lanes"] if item["id"] == lane)
    provenance = {
        "source": {"commit": plan["source_sha"], "dirty": False},
        "qualification_run_id": f"run:1:{lane}",
        "lockfile_sha256": plan["lockfile_sha256"],
        "interpreter": {"platform": platform, "version": version + ".0 (synthetic)"},
    }
    root.mkdir(parents=True)

    def save(name: str, value: Any) -> None:
        (root / name).write_text(json.dumps(value), encoding="utf-8")

    save(
        "collection.json",
        {"collected": items, "reports": [], "exit_code": 0, "provenance": provenance},
    )
    save("selection-manifest.json", selected)
    parts: list[dict[str, Any]] = []
    total: Counter[str] = Counter()
    for label in ("ordinary", "serial", "resource"):
        nodes = [
            case["nodeid"]
            for case in selected["cases"]
            if case["partition"] == label and case["disposition"] == "required"
        ]
        if not nodes:
            continue
        reports = [
            {"nodeid": node, "phase": phase, "outcome": "passed", "xfail": False}
            for node in nodes
            for phase in ("setup", "call", "teardown")
        ]
        counts = {"passed": len(nodes)}
        total.update(counts)
        save(
            label + ".json",
            {
                "collected": [{"nodeid": node, "markers": []} for node in nodes],
                "reports": reports,
                "exit_code": 0,
                "provenance": provenance,
            },
        )
        parts.append(
            {"name": label, "cases": len(nodes), "exit_code": 0, "counts": counts}
        )
    accounting = [
        {
            **case,
            "disposition": "executed"
            if case["disposition"] == "required"
            else case["disposition"],
        }
        for case in selected["cases"]
    ]
    save("case-accounting.json", accounting)
    save(
        "summary.json",
        {
            "source": {"commit": plan["source_sha"], "dirty": False},
            "provenance": provenance,
            "status": "passed",
            "selection_complete": True,
            "stage": "full",
            "test_profile": selected["profile"],
            "selection_sha256": selected["selection_sha256"],
            "coverage_enabled": False,
            "workers": next(
                item["workers"] for item in plan["lanes"] if item["id"] == lane
            ),
            "requested_workers": next(
                item["workers"] for item in plan["lanes"] if item["id"] == lane
            ),
            "distribution": next(
                item["distribution"] for item in plan["lanes"] if item["id"] == lane
            ),
            "partitions": parts,
            "counts": dict(total),
            "expected_cases": sum(total.values()),
            "accounting_sha256": digest(accounting),
        },
    )
    return selected["selection_sha256"]
