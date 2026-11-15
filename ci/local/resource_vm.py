"""Read-only VM feasibility and fail-closed adoption decision; no disk mutation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from resource_specs import read_resources


def doctor() -> dict[str, Any]:
    resources = read_resources()
    disks: list[dict[str, Any]] = []
    for name in ("/mnt/VM_Fast", "/mnt/vm", "/mnt/ellipse_archive"):
        path = Path(name)
        if not path.is_mount():
            continue
        usage = shutil.disk_usage(path)
        disks.append(
            {
                "mount": name,
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "writable": os.access(path, os.W_OK),
                "filesystem": subprocess.check_output(
                    [
                        "findmnt",
                        "-J",
                        "-o",
                        "TARGET,SOURCE,FSTYPE,OPTIONS",
                        "--target",
                        name,
                    ],
                    text=True,
                ),
            }
        )
    allowed = os.sched_getaffinity(0)
    cores: dict[tuple[str, str], list[int]] = {}
    for cpu in sorted(allowed):
        topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        key = (
            (topology / "physical_package_id").read_text().strip(),
            (topology / "core_id").read_text().strip(),
        )
        cores.setdefault(key, []).append(cpu)
    cpuset = Path(
        f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/cgroup.controllers"
    )
    return {
        "schema_version": 1,
        "resources": resources,
        "physical_cores": list(cores.values()),
        "kvm_access": os.access("/dev/kvm", os.R_OK | os.W_OK),
        "qemu": shutil.which("qemu-system-x86_64"),
        "libvirt": shutil.which("virsh"),
        "delegated_controllers": cpuset.read_text().split() if cpuset.is_file() else [],
        "storage": disks,
        "requirements": {
            "guest_vcpus": 6,
            "guest_ram_bytes": 20 * 1024**3,
            "disk_capacity_bytes": 96 * 1024**3,
            "host_reserved_ram_bytes": 32 * 1024**3,
            "independent_coverage": True,
            "original_thresholds": True,
            "physical_core_reservation": True,
        },
        "provisioned": False,
    }


def decide(feasibility: dict[str, Any], coverage: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if (
        coverage.get("vm_coverage_eligible") is not True
        or coverage.get("functional_comparison", {}).get("passed") is not True
    ):
        reasons.append(
            "ordinary/serial coverage is insufficient; a separate sound functional resource pass has not been qualified"
        )
    if not feasibility["kvm_access"] or not feasibility["qemu"]:
        reasons.append("KVM/QEMU is unavailable")
    if (
        len(feasibility["physical_cores"]) < 16
        or (feasibility["resources"]["available_memory_bytes"] or 0) < 64 * 1024**3
    ):
        reasons.append(
            "insufficient reserved physical cores or RAM for a representative isolated comparison"
        )
    if not any(
        disk["writable"] and disk["free_bytes"] > 120 * 1024**3
        for disk in feasibility["storage"]
    ):
        reasons.append("no inspected suitable mounted storage with safe free capacity")
    return {
        "schema_version": 1,
        "adopted": False,
        "decision": "no-go" if reasons else "experiment-required",
        "reasons": reasons
        or [
            "no authoritative VM adoption before independent measurement equivalence and whole-run benefit"
        ],
        "fallback": "exclusive host resources with original coverage instrumentation and thresholds",
        "feasibility": feasibility,
        "coverage": coverage,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage-assessment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("VM decision evidence must be fresh")
    result = decide(doctor(), json.loads(args.coverage_assessment.read_text()))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(result["decision"], result["reasons"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
