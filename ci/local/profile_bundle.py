"""Measure one fixed bounded workload; certify whole-run benefit or keep fallback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from allocation import QualificationLease, state_root
from coverage_data import checksum, combine
from policy import fingerprint
from policy import read as read_policy
from preflight_provenance import source_record
from r_evidence import validate as validate_r
from resource_specs import machine_fingerprint
from suite import outcomes, run

ROOT = Path(__file__).resolve().parents[2]


def default_certificate() -> Path:
    return state_root().parent / "schedules/certificate.json"


def operational_fingerprint(source: Path, ref: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", ref) is None:
        raise ValueError("Performance evidence requires a complete Git commit")
    try:
        entries = subprocess.check_output(
            ["git", "ls-tree", "-rz", "--full-tree", ref],
            cwd=source,
            stderr=subprocess.PIPE,
        ).split(b"\0")
    except subprocess.CalledProcessError as error:
        raise ValueError("Performance comparison history is unavailable") from error
    selected = [
        entry
        for entry in entries
        if entry
        and not (
            entry.split(b"\t", 1)[1].startswith(b"docs/")
            and entry.endswith(b".md")
            or entry.split(b"\t", 1)[1] == b"README.md"
        )
    ]
    return hashlib.sha256(b"\0".join(selected)).hexdigest()


def signature(path: Path) -> dict[str, Any]:
    measured = json.loads(path.read_text())
    return {
        name: {
            key: item[key]
            for key in (
                "executed_lines",
                "missing_lines",
                "executed_branches",
                "missing_branches",
                "excluded_lines",
            )
        }
        for name, item in measured["files"].items()
    }


def inspect_trial(
    directory: Path,
    targets: list[str],
    source: Path,
    expected_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = json.loads((directory / "summary.json").read_text())
    if (
        summary.get("qualification") is not False
        or summary.get("status") != "passed"
        or summary.get("selection_complete") is not True
        or summary["source"] != (expected_source or source_record(source))
    ):
        raise ValueError(
            "Profiling trial is incomplete or from another source/environment"
        )
    identity = summary["provenance"]
    collection = json.loads((directory / "collection.json").read_text())
    nodes = {item["nodeid"] for item in collection["collected"]}
    from selection import inventory_items, matches

    expected_nodes = {
        item["nodeid"]
        for item in inventory_items(
            json.loads((ROOT / "ci/test-inventory.json").read_text())
        )
        if any(matches(item["nodeid"], target) for target in targets)
    }
    if nodes != expected_nodes or any(
        not any(
            node == target or node.startswith((target + "::", target + "["))
            for target in targets
        )
        for node in nodes
    ):
        raise ValueError("Profiling selection differs from fixed representative cases")
    manifests = json.loads((directory / "coverage-manifest.json").read_text())["groups"]
    receipts: dict[str, dict[str, Any]] = {}
    executed: set[str] = set()
    cpu, peak_ram, swap = 0.0, 0, 0
    for part in summary["partitions"]:
        name = part["name"]
        receipt = json.loads((directory / (name + ".json")).read_text())
        selected = {item["nodeid"] for item in receipt["collected"]}
        counts = outcomes(receipt, selected)
        if (
            executed & selected
            or counts["failed"]
            or counts["errors"]
            or counts["skipped"]
            or receipt["exit_code"] != 0
            or receipt["provenance"] != identity
        ):
            raise ValueError("Failed, duplicate or foreign profiling cases")
        executed.update(selected)
        receipts[name] = receipt
        processes = json.loads((directory / (name + "-processes.json")).read_text())
        if processes.get("cleanup_complete") is not True:
            raise ValueError("Profiling process ownership did not complete")
        telemetry = processes.get("telemetry", {})
        cpu += telemetry.get(
            "waited_children_cpu_seconds", telemetry.get("sampled_cpu_seconds", 0)
        )
        peak_ram = max(peak_ram, telemetry.get("peak_tree_rss_bytes", 0))
        swap = max(swap, telemetry.get("peak_visible_swap_used_bytes", 0))
    if executed != nodes:
        raise ValueError("Missing profiling cases")
    with tempfile.TemporaryDirectory(
        prefix="judgmentllm-profile-validate-"
    ) as temporary:
        combine(manifests, receipts, source, identity, Path(temporary) / ".coverage")
    expected_r = json.loads((ROOT / "ci/profile-bundle.json").read_text())["r_gates"]
    if set(summary["r_gates"]) != set(expected_r):
        raise ValueError("Missing named R profiling gates")
    for name in expected_r:
        gate = directory / "r-gates" / name
        record = json.loads((gate / "receipt.json").read_text())
        validate_r(name, gate, record, identity)
        for step in record["steps"]:
            telemetry = step["processes"].get("telemetry", {})
            cpu += telemetry.get(
                "waited_children_cpu_seconds", telemetry.get("sampled_cpu_seconds", 0)
            )
            peak_ram = max(peak_ram, telemetry.get("peak_tree_rss_bytes", 0))
            swap = max(swap, telemetry.get("peak_visible_swap_used_bytes", 0))
    return {
        "profile": summary["local_schedule"],
        "seconds": summary["duration_seconds"],
        "workers": summary["workers"],
        "passed": True,
        "aggregate_process_cpu_seconds": cpu,
        "largest_stage_tree_rss_bytes": peak_ram,
        "peak_visible_swap_used_bytes": swap,
        "summary": str(directory / "summary.json"),
        "summary_sha256": checksum(directory / "summary.json"),
        "coverage_signature": signature(directory / "coverage.json"),
    }


def verify_certificate(certificate: dict[str, Any], source: Path) -> None:
    specification = json.loads((ROOT / "ci/profile-bundle.json").read_text())
    current = source_record(source)
    qualified = certificate.get("source", {})
    if (
        current.get("dirty") is not False
        or qualified.get("dirty") is not False
        or {key: value for key, value in current.items() if key != "commit"}
        != {key: value for key, value in qualified.items() if key != "commit"}
        or certificate.get("operational_sha256")
        != operational_fingerprint(source, current["commit"])
        or certificate["operational_sha256"]
        != operational_fingerprint(source, qualified["commit"])
    ):
        raise ValueError(
            "Scheduling certificate differs in operational code, dependencies or environment"
        )
    if (
        certificate.get("bundle_sha256") != checksum(ROOT / "ci/profile-bundle.json")
        or not 4 <= len(certificate["trials"]) <= specification["maximum_trials"]
    ):
        raise ValueError("Stale or incomplete scheduling certificate")
    locations: set[Path] = set()
    for trial in certificate["trials"]:
        path = Path(trial["summary"])
        if (
            path in locations
            or path.resolve().is_relative_to(source.resolve())
            or path.is_symlink()
        ):
            raise ValueError("Duplicate or untrusted profiling artifact location")
        locations.add(path)
        actual = inspect_trial(path.parent, specification["targets"], source, qualified)
        if actual != trial:
            raise ValueError("Scheduling evidence changed")
    if any(
        trial["coverage_signature"] != certificate["trials"][0]["coverage_signature"]
        for trial in certificate["trials"]
    ):
        raise ValueError("Whole-run strategies have different coverage")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if output.is_relative_to(source) or source_record(source)["dirty"] is not False:
        parser.error("Profiling requires clean committed source and external evidence")
    output.mkdir(parents=True, exist_ok=False)
    specification = json.loads((ROOT / "ci/profile-bundle.json").read_text())
    certificate: dict[str, Any] = {
        "schema_version": 1,
        "source": source_record(source),
        "operational_sha256": operational_fingerprint(
            source, source_record(source)["commit"]
        ),
        "bundle_sha256": checksum(ROOT / "ci/profile-bundle.json"),
        "policy_sha256": fingerprint(read_policy(ROOT / "ci")),
        "machine": machine_fingerprint(),
        "equivalent": False,
        "cleanup_passed": False,
        "trials": [],
    }
    with QualificationLease() as allocation:
        os.environ.update(allocation.environment())
        os.environ["JUDGMENTLLM_R_STRICT"] = "1"
        configurations = [
            ("conservative", 12),
            *(
                ("concurrent", workers)
                for workers in specification["worker_candidates"]
            ),
        ]
        for index in range(specification["maximum_trials"]):
            if index == 4:
                best = min(
                    (
                        item
                        for item in certificate["trials"]
                        if item["profile"] == "concurrent"
                    ),
                    key=lambda item: item["seconds"],
                )
                certificate["ordinary_workers"] = best["workers"]
                configurations.extend(
                    (("concurrent", best["workers"]), ("conservative", 12))
                )
            profile, workers = configurations[index]
            trial = output / f"trial-{index + 1}"
            code = run(
                source=source,
                output=trial,
                targets=specification["targets"],
                workers=workers,
                coverage=True,
                bound_workers=True,
                contract=ROOT / "ci/test-contract.json",
                distribution="worksteal",
                local_schedule=profile,
                run_r_gates=True,
                benchmark_only=True,
            )
            if code:
                certificate["failure"] = {"trial": str(trial), "exit_code": code}
                break
            try:
                certificate["trials"].append(
                    inspect_trial(trial, specification["targets"], source)
                )
            except Exception as error:
                certificate["failure"] = {
                    "trial": str(trial),
                    "phase": "independent inspection",
                    "reason": str(error),
                }
                break
        try:
            verify_certificate(certificate, source)
            certificate.update(equivalent=True, cleanup_passed=True)
        except (OSError, ValueError, KeyError, TypeError) as error:
            certificate.setdefault("failure", {"reason": str(error)})
    from orchestrator import choose

    decision, reason = choose(
        "auto",
        certificate=certificate,
        machine=certificate["machine"],
        policy_hash=certificate["policy_sha256"],
    )
    certificate.update(decision=decision, reason=reason)
    (output / "certificate.json").write_text(json.dumps(certificate, indent=2) + "\n")
    if decision == "concurrent":
        from main_worker import atomic

        destination = default_certificate()
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic(destination, certificate)
    print(f"Measured scheduler decision: {decision}: {reason}")
    return int(not certificate["equivalent"])


if __name__ == "__main__":
    raise SystemExit(main())
