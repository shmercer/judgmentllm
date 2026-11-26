"""Execute complete, disjoint pytest partitions with auditable case receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, cast

# The policy checkout owns selection even when tests execute in a candidate.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preflight_provenance import (
    ATTEMPT_ENV,
    RUN_ID_ENV,
    receipt_provenance,
    source_record,
    verify_preflight_provenance,
)
from resource_budget import ResourceLease
from resource_specs import read_resources
from selection import digest, manifest, read, validate_candidate_metadata

WARNINGS = [
    "-Werror::ResourceWarning",
    "-Werror::pytest.PytestUnraisableExceptionWarning",
]


def outcomes(
    receipt: dict[str, Any],
    expected: set[str],
    *,
    passing_nodes: set[str] | None = None,
) -> Counter[str]:
    """Require exactly one terminal execution per selected case, including skips."""
    collected = [item["nodeid"] for item in receipt["collected"]]
    if len(collected) != len(set(collected)) or set(collected) != expected:
        raise ValueError("Collected cases differ from the partition manifest")
    grouped: dict[str, list[dict[str, Any]]] = {node: [] for node in expected}
    for report in receipt["reports"]:
        if (
            report.get("phase") not in {"setup", "call", "teardown"}
            or report.get("outcome") not in {"passed", "failed", "skipped"}
            or type(report.get("xfail")) is not bool
        ):
            raise ValueError("Invalid case phase, outcome or xfail evidence")
        nodeid = report["nodeid"]
        if nodeid not in grouped:
            raise ValueError("Unexpected case execution")
        grouped[nodeid].append(report)
    result: Counter[str] = Counter()
    for nodeid, reports in grouped.items():
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
            if passing_nodes is not None:
                passing_nodes.add(nodeid)
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


def prepare_exception_proof(
    source: Path,
    plan: dict[str, Any] | None,
    *,
    coverage: bool,
    profile: str | None,
    scope: str,
    lane: str,
    partition: str,
) -> set[str] | None:
    if not coverage or profile != "full" or plan is None:
        return None
    from coverage_authority import (
        checked_exceptions,
        commit_tree,
        exception_proof_enabled,
    )

    if not exception_proof_enabled(plan, scope, lane, partition):
        return None
    tree = commit_tree(source, plan["source_sha"])
    if tree != plan["source_tree_sha"]:
        raise ValueError("Coverage review tree differs from the plan")
    checked_exceptions(source, plan["base_sha"], tree, plan["coverage_policy"])
    return set()


def run(
    *,
    source: Path,
    output: Path,
    targets: list[str],
    workers: int | None,
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
    grid_partition: str = "all",
    local_schedule: str = "auto",
    schedule_certificate: Path | None = None,
    run_r_gates: bool = False,
    benchmark_only: bool = False,
) -> int:
    if benchmark_only and (plan is not None or targets == ["tests"]):
        raise ValueError(
            "Profiling is bounded and cannot establish planned qualification"
        )
    if workers is not None and (type(workers) is not int or not 0 <= workers <= 32):
        raise ValueError("pytest workers must be an integer from 0 through 32")
    if stage not in {"full", "ordinary", "resource"} or (coverage and stage != "full"):
        raise ValueError("Coverage qualification requires the complete suite")
    if coverage and not include_r:
        raise ValueError("Local coverage qualification must include R cases")
    profile = None
    if grid_partition not in {"all", "ordinary", "serial"} or (
        grid_partition != "all"
        and (plan is None or execution_scope != "hosted" or coverage)
    ):
        raise ValueError("Only planned hosted lanes can split grid partitions")
    if plan is not None:
        from risk import lane_profile

        if stage != "full" or targets != ["tests"] or preflight_reports:
            raise ValueError(
                "Planned lanes require full collection and independent execution"
            )
        profile = lane_profile(plan, lane, execution_scope)
        if execution_scope == "hosted" and not any(
            item["id"] == lane and item["partition"] == grid_partition
            for item in plan["hosted_tasks"]
        ):
            raise ValueError("Grid partition differs from the trusted plan")
        if coverage and profile != "full" and plan.get("coverage_mode") != "targeted":
            raise ValueError(
                "Selected coverage cannot establish historical qualification"
            )
    source = source.resolve(strict=True)
    output = output.resolve()
    if output.is_relative_to(source):
        raise ValueError("Test evidence must be outside the source tree")
    output.mkdir(parents=True, exist_ok=False)
    from environment import clean_environment

    environment = clean_environment()
    for key in (
        RUN_ID_ENV,
        "JUDGMENTLLM_R_STRICT",
        "GITHUB_ACTIONS",
        "JUDGMENTLLM_NATIVE_FIXTURES",
    ):
        if key in os.environ:
            environment[key] = os.environ[key]
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
    attempt_id = environment.setdefault(ATTEMPT_ENV, uuid.uuid4().hex)
    resources = read_resources()
    requested_workers = workers
    if bound_workers and workers is not None:
        workers = min(workers, resources["effective_cpus"])
    started = time.monotonic()
    summary: dict[str, Any] = {
        "schema_version": 1,
        "qualification": not benchmark_only,
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
        "grid_partition": grid_partition,
        "source": source_record(source),
        "qualification_run_id": qualification_run_id,
    }
    summary["provenance"] = receipt_provenance(
        summary["source"], qualification_run_id, attempt_id
    )
    coverage_mode = (
        plan.get("coverage_mode", "historical") if plan is not None else "historical"
    )
    coverage_arguments: dict[str, Any] = (
        {"coverage_policy": plan["coverage_policy"], "protected_base": plan["base_sha"]}
        if plan is not None and plan.get("coverage_policy") is not None
        else {}
    )
    if (
        coverage_mode == "targeted"
        and plan is not None
        and not plan["coverage_targets"]
    ):
        coverage = False
        summary["coverage_enabled"] = False
        summary["coverage"] = {
            "passed": True,
            "mode": "targeted",
            "historical_coverage": "not-assessed",
            "disposition": "no changed production code",
        }
    cancel = threading.Event()
    group_coverages: list[dict[str, Any]] = []
    group_receipts: dict[str, dict[str, Any]] = {}
    totals: Counter[str] = Counter()

    def save() -> None:
        (output / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def execute(label: str, arguments: list[str]) -> tuple[int, dict[str, Any]]:
        temporary = output / {
            "collection": "c",
            "ordinary": "t",
            "serial": "s",
            "resource": "r",
        }.get(label, label)
        group_environment = {
            **environment,
            "JUDGMENTLLM_CI_GROUP": label,
        }
        if coverage and label != "collection":
            from coverage_data import context_name, prepare_config

            directory = output / "coverage-groups" / label
            directory.mkdir(parents=True)
            group_environment["COVERAGE_FILE"] = str(directory / ".coverage")
            config = prepare_config(source, directory, summary["provenance"], label)
            group_environment["COVERAGE_RCFILE"] = str(config)
            group_environment["JUDGMENTLLM_COVERAGE_CONTEXT"] = context_name(
                summary["provenance"], label
            )
            arguments = [*arguments, f"--cov-config={config}"]
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
            f"cache_dir={output / ('cache-' + label)}",
            *WARNINGS,
            *arguments,
        ]
        print("Qualification partition:", label, flush=True)
        with (output / f"{label}.log").open("w", encoding="utf-8") as stream:
            from process import run as run_owned

            code, processes = run_owned(
                command,
                cwd=source,
                env=group_environment,
                stream=stream,
                evidence=output / (label + "-owner"),
                identity={"provenance": summary["provenance"], "group": label},
                cancel=cancel,
            )
        (output / f"{label}-processes.json").write_text(
            json.dumps(processes, indent=2) + "\n", encoding="utf-8"
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
                in {
                    "transient-peak-measurement.json",
                    "parent-child-measurement.json",
                    "single-request-resources.json",
                }
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
        return code, receipt

    try:
        passing_nodes = prepare_exception_proof(
            source,
            plan,
            coverage=coverage,
            profile=profile,
            scope=execution_scope,
            lane=lane,
            partition=grid_partition,
        )
        code, collected = execute("collection", ["--collect-only", "--no-cov", "-q"])
        if code != 0 or not collected.get("collected"):
            raise ValueError("Full-suite collection failed or produced no cases")
        parts = partition(collected["collected"])
        if plan is not None:
            validate_candidate_metadata(source, plan, collected["collected"])
        selection_manifest = None
        if profile is not None:
            assert plan is not None
            selection_manifest = manifest(
                collected["collected"],
                profile=profile,
                impacted=plan["impact"],
                include_r=include_r,
                include_resources=include_resources,
                platform=sys.platform,
                partition=grid_partition,
                policy=plan.get("selection_policy", {}).get("taxonomy"),
                known=plan.get("selection_policy", {}).get("inventory"),
                resource_selectors=plan.get("resource_selectors")
                if execution_scope == "local"
                else None,
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
        from allocation import foreground_pending
        from orchestrator import Stage, choose
        from orchestrator import run as run_stages
        from policy import read as read_local_policy
        from policy import serial_groups
        from process import systemd_available

        local_policy = plan.get("local_policy") if plan is not None else None
        local_policy = local_policy or read_local_policy(
            Path(__file__).resolve().parents[1]
        )
        certificate = None
        if schedule_certificate is not None:
            from profile_bundle import verify_certificate

            try:
                certificate = read(schedule_certificate)
                verify_certificate(certificate, source)
            except Exception as error:
                summary["schedule_certificate_error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
                certificate = None
        release = {}
        if sys.platform == "linux":
            try:
                release = platform.freedesktop_os_release()
            except OSError:
                pass  # Unknown systems retain conservative scheduling.
        chosen, schedule_reason = choose(
            local_schedule,
            policy=local_policy,
            resources=dict(resources),
            ubuntu="ubuntu" in [release.get("ID"), *release.get("ID_LIKE", "").split()],
            process_ownership=systemd_available(),
        )
        if execution_scope != "local" or stage != "full":
            chosen, schedule_reason = (
                "conservative",
                "hosted and partial runners retain their existing scheduling",
            )
        if chosen == "resource-isolated":
            chosen, schedule_reason = (
                "conservative",
                "Resource VM has no authoritative qualification; exclusive host resources retain original coverage and limits",
            )
        if workers is None:
            workers = (
                cast(int, local_policy["profiles"][chosen]["ordinary_workers"])
                if execution_scope == "local" and stage == "full"
                else 2
            )
        if bound_workers or execution_scope == "local":
            workers = min(workers, resources["effective_cpus"])
        logical_parts = {label: list(nodes) for label, nodes in parts.items()}
        group_kinds = {label: label for label in parts}
        if chosen == "concurrent":
            classified = serial_groups(parts.pop("serial"), local_policy, source)
            for placement, nodes in classified.items():
                label = {
                    "concurrency-safe-serial": "serial-safe",
                    "host-exclusive-serial": "serial-host",
                    "globally-exclusive-serial": "serial-global",
                }[placement]
                parts[label] = nodes
                group_kinds[label] = "serial"
            workers = min(
                workers, local_policy["profiles"]["concurrent"]["ordinary_workers"]
            )
        summary.update(
            local_schedule=chosen,
            schedule_evidence={
                "measured_source_sha": certificate["source"]["commit"],
                "operational_sha256": certificate["operational_sha256"],
                "qualification_reused": False,
            }
            if certificate is not None
            else None,
            schedule_reason=schedule_reason,
            workers=workers,
            execution_groups={
                label: {"partition": group_kinds[label], "nodes": list(nodes)}
                for label, nodes in parts.items()
                if nodes
            },
            serial_placement={
                node: {
                    "placement": label,
                    "reason": next(
                        (
                            entry["reason"]
                            for entry in local_policy["serial"]
                            if node == entry["selector"] and label == "serial-safe"
                        ),
                        "default global isolation or changed dependency; conservative barrier",
                    ),
                }
                for label, nodes in parts.items()
                if group_kinds[label] == "serial"
                for node in nodes
            },
        )
        print(
            f"Local schedule: {chosen}; {workers} ordinary workers; {schedule_reason}",
            flush=True,
        )
        mutex = threading.Lock()

        def functional_comparison() -> None:
            if not coverage or not group_coverages:
                return
            from coverage_data import combine, report
            from coverage_equivalence import compare

            manifests = list(group_coverages)
            receipts = dict(group_receipts)
            combine(
                manifests,
                receipts,
                source,
                summary["provenance"],
                output / ".coverage-functional",
            )
            report(
                output / ".coverage-functional",
                source,
                output / "functional-coverage.json",
                plan["coverage_targets"]
                if plan is not None and coverage_mode == "targeted"
                else None,
            )
            if benchmark_only:
                result = {
                    "passed": True,
                    "mode": "profiling-only",
                    "historical_coverage": "not-assessed",
                }
            elif coverage_mode == "targeted" and plan is not None:
                from targeted_coverage import compare as compare_targeted

                assert baseline is not None
                result = compare_targeted(
                    output / "functional-coverage.json",
                    source,
                    plan["base_sha"],
                    plan["source_sha"],
                    plan["coverage_targets"],
                    baseline,
                )
            else:
                result = compare(
                    output / "functional-coverage.json",
                    baseline,
                    source,
                    reviewed_ref=coverage_ref,
                    **coverage_arguments,
                )
            summary["functional_coverage"] = result
            (output / "functional-coverage-comparison.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )

        def partition_job(label: str) -> dict[str, Any]:
            nodes = parts[label]
            logical = group_kinds[label]
            selection = output / f"{label}-selection.json"
            selection.write_text(json.dumps(nodes, indent=2) + "\n", encoding="utf-8")
            arguments = ["-ra", f"--ci-selection={selection}"]
            if logical == "ordinary" and workers:
                arguments += [
                    "-n",
                    str(workers),
                    f"--dist={distribution}",
                    "--max-worker-restart=0",
                ]
            if coverage:
                arguments += [
                    "--ci-coverage",
                    "--cov-append",
                    f"--cov-report=xml:{output / ('coverage-' + label + '.xml')}",
                    "--cov-fail-under=0",
                ]
                if coverage_mode == "targeted" and plan is not None:
                    arguments += [
                        "--cov-reset",
                        *(
                            "--cov="
                            + path.removeprefix("src/")
                            .removesuffix(".py")
                            .replace("/", ".")
                            for path in plan["coverage_targets"]
                        ),
                    ]
            else:
                arguments += ["--no-cov"]
            before = time.monotonic()
            if logical == "resource":
                functional_comparison()
                with ResourceLease(
                    resource_root or output / "resource-budget", resource_slots
                ) as lease:
                    lease.record(output / "resource-lease.json")
                    code, receipt = execute(label, arguments)
            else:
                code, receipt = execute(label, arguments)
            case_passes: set[str] = set()
            counts = outcomes(
                receipt,
                set(nodes),
                passing_nodes=case_passes
                if passing_nodes is not None and logical in {"ordinary", "serial"}
                else None,
            )
            if skip_policy is not None:
                verify_skips(
                    receipt,
                    skip_policy,
                    platform=sys.platform,
                    require_r=environment.get("JUDGMENTLLM_R_STRICT") == "1",
                )
            passed = code == 0 and not (counts["failed"] + counts["errors"])
            with mutex:
                totals.update(counts)
                summary["partitions"].append(
                    {
                        "name": label,
                        "partition": logical,
                        "exit_code": code,
                        "cases": len(nodes),
                        "counts": dict(counts),
                        "duration_seconds": time.monotonic() - before,
                    }
                )
                if coverage and passed:
                    from coverage_data import seal

                    group_coverages.append(
                        seal(
                            output / "coverage-groups" / label / ".coverage",
                            receipt,
                            source,
                            summary["provenance"],
                            label,
                        )
                    )
                    group_receipts[label] = receipt
                    if passing_nodes is not None:
                        passing_nodes.update(case_passes)
                save()
            return {"exit_code": 0 if passed else code or 1}

        from risk import R_GATES

        r_names = (
            list(plan["r_gates"] if plan is not None else R_GATES)
            if run_r_gates and execution_scope == "local"
            else []
        )
        summary["r_gates"] = {}

        def r_job(name: str) -> dict[str, Any]:
            from acceptance import r_gate

            directory = output / "r-gates" / name
            code = r_gate(
                name,
                source,
                directory,
                cancel=cancel,
                run_id=qualification_run_id,
                attempt_id=attempt_id,
            )
            with mutex:
                summary["r_gates"][name] = {
                    "exit_code": code,
                    "receipt": str(directory / "receipt.json"),
                }
                save()
            return {"exit_code": code}

        scheduled: list[Stage] = []
        previous: tuple[str, ...] = ()
        ordinary_names = [
            label
            for label in parts
            if parts[label] and group_kinds[label] == "ordinary"
        ]
        safe_names = [
            label for label in parts if parts[label] and label == "serial-safe"
        ]
        concurrent_names = [
            *ordinary_names,
            *safe_names,
            *("r-" + name for name in r_names),
        ]
        available = (
            resources["available_memory_bytes"]
            or resources["memory_bytes"]
            or 16 * 1024**3
        )
        memory_budget = max(
            512 * 1024**2, available - min(16 * 1024**3, available // 4)
        )
        for label, nodes in parts.items():
            if not nodes or stage != "full" and label != stage:
                continue
            dependencies = (
                previous
                if chosen == "conservative"
                else tuple(name for name in concurrent_names if name != label)
                if group_kinds[label] == "serial" and label != "serial-safe"
                else ()
            )
            if (
                chosen == "concurrent"
                and label == "serial-global"
                and parts.get("serial-host")
            ):
                dependencies += ("serial-host",)
            if chosen == "concurrent" and group_kinds[label] == "resource":
                dependencies = tuple(
                    [
                        name
                        for name, other in parts.items()
                        if other and group_kinds[name] != "resource"
                    ]
                    + ["r-" + name for name in r_names]
                )
            scheduled.append(
                Stage(
                    label,
                    lambda label=label: partition_job(label),
                    dependencies,
                    cpus=workers if group_kinds[label] == "ordinary" and workers else 1,
                    memory_bytes=min(memory_budget, 16 * 1024**3)
                    if chosen == "conservative"
                    else min(memory_budget // 4, 8 * 1024**3),
                    kind=group_kinds[label],
                )
            )
            previous = (label,)
        for name in r_names:
            dependencies = previous if chosen == "conservative" else ()
            scheduled.append(
                Stage(
                    "r-" + name,
                    lambda name=name: r_job(name),
                    dependencies,
                    memory_bytes=min(memory_budget // 8, 2 * 1024**3),
                    kind="r",
                )
            )
            if chosen == "conservative":
                previous = ("r-" + name,)
        scheduled_names = {item.name for item in scheduled}
        if any(not set(item.dependencies) <= scheduled_names for item in scheduled):
            raise ValueError("Incomplete stage graph")
        execution = run_stages(
            scheduled,
            cpus=resources["effective_cpus"],
            memory_bytes=memory_budget,
            output=output / "execution.json",
            cancel=cancel,
            r_workers=local_policy["profiles"][chosen]["r_workers"],
            identity=summary["provenance"],
            yield_at_boundary=(
                lambda: foreground_pending(
                    Path(environment["JUDGMENTLLM_ALLOCATION_ROOT"])
                )
            )
            if environment.get("JUDGMENTLLM_BACKGROUND_RUN") == "1"
            else None,
        )
        failed = execution["status"] != "passed"
        if coverage and "functional_coverage" not in summary and not failed:
            functional_comparison()
        expected_selection = (
            summary["expected_cases"] if stage == "full" else len(logical_parts[stage])
        )
        summary["selection_complete"] = sum(totals.values()) == expected_selection
        summary["manifest_complete"] = (
            stage == "full"
            and grid_partition == "all"
            and summary["selection_complete"]
            and not summary["deferred_resource_cases"]
            and not deferred_r
            and not summary.get("not_required_cases", 0)
        )
        if not summary["selection_complete"]:
            raise ValueError(
                "Qualification partitions did not execute the complete suite"
            )
        if coverage and not failed:
            from coverage_data import combine, proof_interpreter, report
            from coverage_equivalence import compare

            combined = combine(
                group_coverages,
                group_receipts,
                source,
                summary["provenance"],
                output / ".coverage",
            )
            (output / "coverage-manifest.json").write_text(
                json.dumps(combined, indent=2) + "\n"
            )
            report(
                output / ".coverage",
                source,
                output / "coverage.json",
                plan["coverage_targets"]
                if plan is not None and coverage_mode == "targeted"
                else None,
            )
            if benchmark_only:
                comparison = {
                    "passed": True,
                    "mode": "profiling-only",
                    "historical_coverage": "not-assessed",
                }
            elif coverage_mode == "targeted" and plan is not None:
                from targeted_coverage import compare as compare_targeted

                assert baseline is not None
                comparison = compare_targeted(
                    output / "coverage.json",
                    source,
                    plan["base_sha"],
                    plan["source_sha"],
                    plan["coverage_targets"],
                    baseline,
                )
            else:
                comparison = compare(
                    output / "coverage.json",
                    baseline,
                    source,
                    reviewed_ref=coverage_ref,
                    passing_nodes=passing_nodes,
                    proof_interpreter=proof_interpreter(group_receipts, passing_nodes),
                    **coverage_arguments,
                )
            summary["coverage"] = comparison
            if (
                not benchmark_only
                and coverage_mode == "historical"
                and baseline is not None
            ):
                from resource_coverage import assess

                resource_group = output / "coverage-groups/resource/.coverage"
                assessment = assess(
                    output / "functional-coverage.json",
                    baseline,
                    source,
                    summary["source"]["commit"],
                    complete=output / "coverage.json",
                    resource_data=resource_group if resource_group.is_file() else None,
                    **coverage_arguments,
                )
                (output / "resource-coverage-assessment.json").write_text(
                    json.dumps(assessment, indent=2) + "\n"
                )
                summary["resource_coverage_path"] = assessment["path"]
            (output / "coverage-comparison.json").write_text(
                json.dumps(comparison, indent=2, sort_keys=True) + "\n"
            )
            from coverage_data import checksum

            combined["reports"] = {
                name: checksum(output / name)
                for name in (
                    "coverage.json",
                    "coverage.xml",
                    "coverage-comparison.json",
                )
            }
            (output / "coverage-manifest.json").write_text(
                json.dumps(combined, indent=2) + "\n"
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
    except KeyboardInterrupt:
        cancel.set()
        summary.update(
            status="interrupted",
            error={"type": "KeyboardInterrupt", "message": "qualification interrupted"},
        )
    except Exception as error:
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
    parser.add_argument(
        "--workers",
        type=int,
        help="Default: selected local policy allocation (10 concurrent, 12 conservative); partial runs use 2",
    )
    parser.add_argument(
        "--local-schedule",
        choices=("auto", "conservative", "concurrent", "resource-isolated"),
        default="auto",
    )
    parser.add_argument(
        "--schedule-certificate",
        type=Path,
        help="Optional performance diagnostics; never controls scheduling or workers",
    )
    parser.add_argument("--run-r-gates", action="store_true")
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
        "--grid-partition", choices=("all", "ordinary", "serial"), default="all"
    )
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
        grid_partition=options.grid_partition,
        local_schedule=options.local_schedule,
        schedule_certificate=options.schedule_certificate,
        run_r_gates=options.run_r_gates,
    )


if __name__ == "__main__":
    if sys.platform == "linux" and os.environ.get("GITHUB_ACTIONS") != "true":
        from allocation import QualificationLease

        with QualificationLease() as allocation:
            os.environ.update(allocation.environment())
            raise SystemExit(main())
    raise SystemExit(main())
