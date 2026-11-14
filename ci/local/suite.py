"""Execute complete, disjoint pytest partitions with auditable case receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, cast

# The policy checkout owns selection even when tests execute in a candidate.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preflight_provenance import (
    RUN_ID_ENV,
    receipt_provenance,
    source_record,
    verify_preflight_provenance,
)
from resource_budget import ResourceLease
from resource_specs import read_resources
from selection import digest, manifest, read
from telemetry import ProcessSampler

WARNINGS = [
    "-Werror::ResourceWarning",
    "-Werror::pytest.PytestUnraisableExceptionWarning",
]


def outcomes(receipt: dict[str, Any], expected: set[str]) -> Counter[str]:
    """Require exactly one terminal execution per selected case, including skips."""
    collected = [item["nodeid"] for item in receipt["collected"]]
    if len(collected) != len(set(collected)) or set(collected) != expected:
        raise ValueError("Collected cases differ from the partition manifest")
    grouped: dict[str, list[dict[str, Any]]] = {node: [] for node in expected}
    for report in receipt["reports"]:
        nodeid = report["nodeid"]
        if nodeid not in grouped:
            raise ValueError("Unexpected case execution")
        grouped[nodeid].append(report)
    result: Counter[str] = Counter()
    for reports in grouped.values():
        phases = [report["phase"] for report in reports]
        if len(phases) != len(set(phases)) or not {"setup", "teardown"} <= set(phases):
            raise ValueError("Missing or duplicated case execution")
        if any(report["xfail"] for report in reports):
            raise ValueError("Unexpected xfail in qualification")
        if any(report["outcome"] == "failed" for report in reports):
            result[
                "failed"
                if any(
                    report["phase"] == "call" and report["outcome"] == "failed"
                    for report in reports
                )
                else "errors"
            ] += 1
        elif any(report["outcome"] == "skipped" for report in reports):
            result["skipped"] += 1
        elif phases.count("call") == 1:
            result["passed"] += 1
        else:
            raise ValueError("Case has no terminal outcome")
    return result


def partition(items: list[dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"ordinary": [], "serial": [], "resource": []}
    seen: set[str] = set()
    for item in items:
        node = cast(str, item["nodeid"])
        if node in seen:
            raise ValueError("Duplicate collected node ID")
        seen.add(node)
        markers = item["markers"]
        stage = (
            "resource"
            if "resource" in markers
            else "serial"
            if "serial" in markers
            else "ordinary"
        )
        result[stage].append(node)
    return result


def verify_contract(
    path: Path, actual: set[str], targets: list[str]
) -> dict[str, object]:
    contract = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    if (
        type(contract.get("schema_version")) is not int
        or contract["schema_version"] != 1
    ):
        raise ValueError("Unsupported test acceptance contract")
    required = cast(list[str], contract["required_nodes"])
    replacements = cast(dict[str, list[str]], contract["replacements"])
    covered = [
        node
        for node in required
        if any(
            node == target
            or node.startswith((target.rstrip("/") + "/", target + "::", target + "["))
            for target in targets
        )
    ]
    missing = [
        node
        for node in covered
        if node not in actual
        and not (replacements.get(node) and set(replacements[node]) <= actual)
    ]
    if missing:
        raise ValueError(
            f"Required cases have no documented replacement: {missing[:12]}"
        )
    return {
        "required_cases": len(covered),
        "replacement_cases": sorted(node for node in covered if node not in actual),
        "contract": str(path),
    }


def verify_skips(
    receipt: dict[str, Any], policy: dict[str, Any], *, platform: str, require_r: bool
) -> None:
    allowed = set(cast(list[str], policy.get("platform_skips", {}).get(platform, [])))
    if not require_r:
        allowed.update(cast(list[str], policy.get("optional_r_skips", [])))
    unexpected = sorted(
        {
            report["nodeid"]
            for report in receipt["reports"]
            if report["outcome"] == "skipped"
            and report["nodeid"].split("[", 1)[0] not in allowed
        }
    )
    if unexpected:
        raise ValueError(f"Unexpected qualification skips: {unexpected}")


def run(
    *,
    source: Path,
    output: Path,
    targets: list[str],
    workers: int,
    coverage: bool,
    resource_root: Path | None = None,
    resource_slots: int = 1,
    baseline: Path | None = None,
    stage: str = "full",
    bound_workers: bool = False,
    preflight_reports: list[Path] | None = None,
    contract: Path | None = None,
    distribution: str = "worksteal",
    include_resources: bool = True,
    include_r: bool = True,
    coverage_ref: str | None = None,
    plan: dict[str, Any] | None = None,
    lane: str = "linux-3.11",
    execution_scope: str = "local",
) -> int:
    if isinstance(workers, bool) or not 0 <= workers <= 32:
        raise ValueError("pytest workers must be an integer from 0 through 32")
    if stage not in {"full", "ordinary", "resource"} or (coverage and stage != "full"):
        raise ValueError("Coverage qualification requires the complete suite")
    if coverage and not include_r:
        raise ValueError("Local coverage qualification must include R cases")
    profile = None
    if plan is not None:
        from risk import lane_profile

        if stage != "full" or targets != ["tests"] or preflight_reports:
            raise ValueError(
                "Planned lanes require full collection and independent execution"
            )
        profile = lane_profile(plan, lane, execution_scope)
        if coverage and profile != "full":
            raise ValueError(
                "Selected coverage cannot establish historical qualification"
            )
    source = source.resolve(strict=True)
    output = output.resolve()
    if output.is_relative_to(source):
        raise ValueError("Test evidence must be outside the source tree")
    output.mkdir(parents=True, exist_ok=False)
    environment = dict(os.environ)
    for key in (
        "PYTEST_XDIST_WORKER",
        "PYTEST_XDIST_WORKER_COUNT",
        "PYTEST_XDIST_TESTRUNUID",
    ):
        environment.pop(key, None)
    environment["COVERAGE_FILE"] = str(output / ".coverage")
    # Load only this checkout's reporting plugin, independent of editable installs.
    plugin_root = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = os.pathsep.join(
        [plugin_root, str(source), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    qualification_run_id = environment.setdefault(RUN_ID_ENV, uuid.uuid4().hex)
    resources = read_resources()
    requested_workers = workers
    if bound_workers:
        workers = min(workers, resources["effective_cpus"])
    started = time.monotonic()
    summary: dict[str, Any] = {
        "schema_version": 1,
        "workers": workers,
        "distribution": distribution,
        "requested_workers": requested_workers,
        "resources": resources,
        "coverage_enabled": coverage,
        "status": "running",
        "partitions": [],
        "manifest_complete": False,
        "selection_complete": False,
        "stage": stage,
        "source": source_record(source),
        "qualification_run_id": qualification_run_id,
    }
    summary["provenance"] = receipt_provenance(summary["source"], qualification_run_id)
    totals: Counter[str] = Counter()

    def save() -> None:
        (output / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def execute(label: str, arguments: list[str]) -> tuple[int, dict[str, Any]]:
        temporary = (
            output
            / {"collection": "c", "ordinary": "t", "serial": "s", "resource": "r"}[
                label
            ]
        )
        command = [
            sys.executable,
            "-P",
            "-m",
            "pytest",
            "-p",
            "ci.local.pytest_report",
            *(
                targets
                if label == "collection"
                else sorted({node.split("::", 1)[0] for node in parts[label]})
            ),
            f"--ci-report={output / (label + '.json')}",
            f"--basetemp={temporary}",
            "-o",
            f"cache_dir={output / 'cache'}",
            *WARNINGS,
            *arguments,
        ]
        print("Qualification partition:", label, flush=True)
        with (output / f"{label}.log").open("w", encoding="utf-8") as stream:
            with ProcessSampler() as sampler:
                completed = subprocess.run(
                    command,
                    cwd=source,
                    env=environment,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
        (output / f"{label}-processes.json").write_text(
            json.dumps(sampler.report(), indent=2) + "\n", encoding="utf-8"
        )
        log = (output / f"{label}.log").read_text(encoding="utf-8", errors="replace")
        print(log, flush=True)
        path = output / f"{label}.json"
        receipt = (
            cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
            if path.exists()
            else {}
        )
        if label == "resource":
            # Keep portable evidence even when Windows uses a short guest-only
            # scratch path. Never copy the large stores or generated inputs.
            measurements = output / "resource-measurements"
            measurements.mkdir(exist_ok=True)
            candidates = (
                path
                for path in temporary.rglob("*.json")
                if path.name.startswith("measurement")
                or path.name
                in {"transient-peak-measurement.json", "parent-child-measurement.json"}
                or (
                    path.parent.name == "raw" and path.parent.parent.parent == temporary
                )
            )
            index: list[dict[str, str]] = []
            for number, path in enumerate(sorted(candidates)):
                filename = f"evidence-{number:03}.json"
                shutil.copyfile(path, measurements / filename)
                index.append(
                    {
                        "source": path.relative_to(temporary).as_posix(),
                        "file": filename,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
            (measurements / "manifest.json").write_text(
                json.dumps(index, indent=2) + "\n", encoding="utf-8"
            )
        return completed.returncode, receipt

    try:
        code, collected = execute("collection", ["--collect-only", "--no-cov", "-q"])
        if code != 0 or not collected.get("collected"):
            raise ValueError("Full-suite collection failed or produced no cases")
        parts = partition(collected["collected"])
        selection_manifest = None
        if profile is not None:
            assert plan is not None
            selection_manifest = manifest(
                collected["collected"],
                profile=profile,
                impacted=plan["impact"],
                include_r=include_r,
                include_resources=include_resources,
            )
            if selection_manifest["policy_sha256"] != plan["selection_policy_sha256"]:
                raise ValueError("Selection policy differs from planned trusted policy")
            required = {
                case["nodeid"]
                for case in selection_manifest["cases"]
                if case["disposition"] == "required"
            }
            for label in parts:
                parts[label] = [node for node in parts[label] if node in required]
            (output / "selection-manifest.json").write_text(
                json.dumps(selection_manifest, indent=2) + "\n", encoding="utf-8"
            )
            summary.update(
                test_profile=profile,
                selection_sha256=selection_manifest["selection_sha256"],
                universe_cases=len(collected["collected"]),
                not_required_cases=sum(
                    case["disposition"] == "not-required-for-this-lane"
                    for case in selection_manifest["cases"]
                ),
            )
        deferred_r = (
            [
                item["nodeid"]
                for item in collected["collected"]
                if "r_interop" in item["markers"]
            ]
            if not include_r
            else []
        )
        for label in parts:
            parts[label] = [node for node in parts[label] if node not in deferred_r]
        summary["deferred_r_cases"] = deferred_r
        # SDK byte-identity probes are correctness gates even though their cold
        # imports need resource isolation. Only measurement workloads are deferred.
        deferred = []
        if not include_resources:
            deferred = [
                node
                for node in parts["resource"]
                if not node.startswith("tests/test_native_other_sdk.py::")
            ]
            parts["resource"] = [
                node for node in parts["resource"] if node not in deferred
            ]
        summary["deferred_resource_cases"] = deferred
        if selection_manifest is not None:
            summary["deferred_resource_cases"] = [
                case["nodeid"]
                for case in selection_manifest["cases"]
                if case["disposition"] == "deferred-by-policy"
                and case["partition"] == "resource"
            ]
        summary["expected_cases"] = sum(map(len, parts.values()))
        all_nodes = {item["nodeid"] for item in collected["collected"]}
        if contract is not None:
            summary["acceptance_contract"] = verify_contract(
                contract, all_nodes, targets
            )
        skip_policy = (
            cast(dict[str, Any], json.loads(contract.read_text(encoding="utf-8")))
            if contract is not None
            else None
        )
        already_executed: set[str] = set()
        if preflight_reports and (coverage or stage != "full"):
            raise ValueError("Preflight reuse requires a complete noncoverage lane")
        for path in preflight_reports or []:
            receipt = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
            verify_preflight_provenance(receipt, summary["provenance"])
            nodes = {item["nodeid"] for item in receipt["collected"]}
            if (
                not nodes
                or not nodes <= set().union(*map(set, parts.values()))
                or nodes & already_executed
                or nodes & set(parts["resource"])
            ):
                raise ValueError(
                    "Preflight cases are missing, duplicated or require resource isolation"
                )
            counts = outcomes(receipt, nodes)
            if skip_policy is not None:
                verify_skips(
                    receipt,
                    skip_policy,
                    platform=sys.platform,
                    require_r=environment.get("JUDGMENTLLM_R_STRICT") == "1",
                )
            if receipt["exit_code"] != 0 or counts["failed"] or counts["errors"]:
                raise ValueError("A required preflight did not pass")
            already_executed.update(nodes)
            totals.update(counts)
            summary["partitions"].append(
                {
                    "name": path.stem,
                    "cases": len(nodes),
                    "counts": dict(counts),
                    "exit_code": 0,
                    "executed_before_suite": True,
                }
            )
        for label in parts:
            parts[label] = [
                node for node in parts[label] if node not in already_executed
            ]
        summary["reused_preflight_cases"] = len(already_executed)
        failed = False
        for label, nodes in parts.items():
            if stage != "full" and label != stage:
                continue
            if not nodes:
                continue
            selection = output / f"{label}-selection.json"
            selection.write_text(json.dumps(nodes, indent=2) + "\n", encoding="utf-8")
            arguments = ["-ra", f"--ci-selection={selection}"]
            if label == "ordinary" and workers:
                arguments.extend(
                    [
                        "-n",
                        str(workers),
                        f"--dist={distribution}",
                        "--max-worker-restart=0",
                    ]
                )
            arguments.extend(
                [
                    "--cov-append",
                    f"--cov-report=xml:{output / 'coverage.xml'}",
                    "--cov-fail-under=0",
                ]
                if coverage
                else ["--no-cov"]
            )
            before = time.monotonic()
            if label == "resource":
                with ResourceLease(
                    resource_root or output / "resource-budget", resource_slots
                ) as lease:
                    lease.record(output / "resource-lease.json")
                    code, receipt = execute(label, arguments)
            else:
                code, receipt = execute(label, arguments)
            counts = outcomes(receipt, set(nodes))
            if skip_policy is not None:
                verify_skips(
                    receipt,
                    skip_policy,
                    platform=sys.platform,
                    require_r=environment.get("JUDGMENTLLM_R_STRICT") == "1",
                )
            totals.update(counts)
            failed |= code != 0 or bool(counts["failed"] + counts["errors"])
            summary["partitions"].append(
                {
                    "name": label,
                    "exit_code": code,
                    "cases": len(nodes),
                    "counts": dict(counts),
                    "duration_seconds": time.monotonic() - before,
                }
            )
            save()
        expected_selection = (
            summary["expected_cases"] if stage == "full" else len(parts[stage])
        )
        summary["selection_complete"] = sum(totals.values()) == expected_selection
        summary["manifest_complete"] = (
            stage == "full"
            and summary["selection_complete"]
            and not summary["deferred_resource_cases"]
            and not deferred_r
            and not summary.get("not_required_cases", 0)
        )
        if not summary["selection_complete"]:
            raise ValueError(
                "Qualification partitions did not execute the complete suite"
            )
        if coverage:
            for arguments in [
                ["json", "-o", str(output / "coverage.json")],
                ["xml", "-o", str(output / "coverage.xml")],
                ["report", "--fail-under=90"],
            ]:
                completed = subprocess.run(
                    [sys.executable, "-m", "coverage", *arguments],
                    cwd=source,
                    env=environment,
                    check=False,
                )
                failed |= completed.returncode != 0
            from coverage_equivalence import compare

            comparison = compare(
                output / "coverage.json", baseline, source, reviewed_ref=coverage_ref
            )
            summary["coverage"] = comparison
            (output / "coverage-comparison.json").write_text(
                json.dumps(comparison, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            failed |= not comparison["passed"]
            if "combined_percent" in comparison:
                print(
                    f"Total coverage: {comparison['combined_percent']:.2f}%", flush=True
                )
        summary["status"] = "failed" if failed else "passed"
        if selection_manifest is not None:
            accounting = [
                {
                    **case,
                    "disposition": "executed"
                    if case["disposition"] == "required"
                    else case["disposition"],
                }
                for case in selection_manifest["cases"]
            ]
            (output / "case-accounting.json").write_text(
                json.dumps(accounting, indent=2) + "\n", encoding="utf-8"
            )
            summary["accounting_sha256"] = digest(accounting)
        if source_record(source) != summary["source"]:
            raise ValueError("Source/environment changed during suite execution")
    except (OSError, ValueError, KeyError, TypeError) as error:
        summary.update(
            status="failed", error={"type": type(error).__name__, "message": str(error)}
        )
        print(f"Qualification failed: {error}", file=sys.stderr, flush=True)
    finally:
        summary["counts"] = {
            key: totals[key] for key in ("passed", "failed", "skipped", "errors")
        }
        summary["duration_seconds"] = time.monotonic() - started
        save()
    print(
        ", ".join(
            f"{totals[key]} {key}" for key in ("passed", "failed", "skipped", "errors")
        )
        + f" in {summary['duration_seconds']:.2f}s",
        flush=True,
    )
    return int(summary["status"] != "passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--distribution", choices=("loadfile", "worksteal"), default="worksteal"
    )
    parser.add_argument(
        "--benchmark-workers", help="Ordinary-only benchmark sweep, for example 0,2,4,6"
    )
    parser.add_argument(
        "--bound-workers",
        action="store_true",
        help="Bound the explicit worker cap to the measured CPU allocation",
    )
    parser.add_argument("--no-coverage", action="store_true")
    parser.add_argument("--resource-root", type=Path)
    parser.add_argument("--resource-slots", type=int, default=1)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--coverage-ref", help="Reviewed committed source for change-aware coverage"
    )
    parser.add_argument("--skip-resources", action="store_true")
    parser.add_argument("--skip-r", action="store_true")
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--lane", default="linux-3.11")
    parser.add_argument(
        "--execution-scope", choices=("local", "hosted"), default="local"
    )
    parser.add_argument("--preflight-report", type=Path, action="append", default=[])
    parser.add_argument(
        "--preflight-root",
        type=Path,
        help="Required Windows precheck receipts; ignored on other platforms",
    )
    parser.add_argument(
        "--stage", choices=("full", "ordinary", "resource"), default="full"
    )
    parser.add_argument(
        "targets", nargs="*", default=["tests", "ci/local/test_gates.py"]
    )
    options = parser.parse_args()
    if options.preflight_root is not None and os.name == "nt":
        options.preflight_report.extend(
            options.preflight_root / f"{name}.json" for name in ("d", "l")
        )
    if options.benchmark_workers:
        if (
            options.stage != "ordinary"
            or not options.no_coverage
            or options.preflight_report
        ):
            parser.error("worker sweeps require --stage ordinary --no-coverage")
        counts = [int(value) for value in options.benchmark_workers.split(",")]
        if (
            len(counts) != len(set(counts))
            or not counts
            or any(value < 0 or value > 32 for value in counts)
        ):
            parser.error("worker counts must be distinct integers from 0 through 32")
        options.output.mkdir(parents=True, exist_ok=False)
        started = time.monotonic()
        runs: list[dict[str, Any]] = []
        total: Counter[str] = Counter()
        for workers in counts:
            target = options.output / f"w{workers}"
            run(
                source=options.source,
                output=target,
                targets=options.targets,
                workers=workers,
                coverage=False,
                resource_root=options.resource_root,
                resource_slots=options.resource_slots,
                contract=options.contract,
                stage="ordinary",
                distribution=options.distribution,
            )
            receipt = cast(
                dict[str, Any],
                json.loads((target / "summary.json").read_text(encoding="utf-8")),
            )
            runs.append(receipt)
            total.update(receipt["counts"])
        passed = all(
            receipt["status"] == "passed" and receipt["selection_complete"]
            for receipt in runs
        )
        summary = {
            "schema_version": 1,
            "status": "passed" if passed else "failed",
            "stage": "ordinary",
            "benchmark_workers": counts,
            "source": source_record(options.source),
            "selection_complete": passed,
            "manifest_complete": False,
            "coverage_enabled": False,
            "expected_cases": sum(receipt["expected_cases"] for receipt in runs),
            "counts": dict(total),
            "runs": runs,
            "duration_seconds": time.monotonic() - started,
        }
        (options.output / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(
            ", ".join(
                f"{total[key]} {key}"
                for key in ("passed", "failed", "skipped", "errors")
            )
            + f" in {summary['duration_seconds']:.2f}s",
            flush=True,
        )
        return int(not passed)
    return run(
        source=options.source,
        output=options.output,
        targets=options.targets,
        workers=options.workers,
        coverage=not options.no_coverage,
        resource_root=options.resource_root,
        resource_slots=options.resource_slots,
        baseline=options.baseline,
        stage=options.stage,
        bound_workers=options.bound_workers,
        preflight_reports=options.preflight_report,
        contract=options.contract,
        distribution=options.distribution,
        include_resources=not options.skip_resources,
        include_r=not options.skip_r,
        coverage_ref=options.coverage_ref,
        plan=read(options.plan) if options.plan else None,
        lane=options.lane,
        execution_scope=options.execution_scope,
    )


if __name__ == "__main__":
    raise SystemExit(main())
