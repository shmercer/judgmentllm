"""Qualify genuine installed a2 to a3 lifecycle recovery offline, outside CI.

Supply prepared interpreters and exact Git source archives. The command never
fetches, installs dependencies, changes saved versions, or rewrites manifests.
All generated data and the final report stay in the supplied external directory.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any

import judgmentllm
import judgmentllm.preparation as preparation
from judgmentllm import LifecycleError, MockExecutionOptions, get_run, resume_run
from judgmentllm.config import load_run_spec
from judgmentllm.execution import execute_run
from judgmentllm.planning import iter_requests
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.mock import MockAdapter, MockScenario

if __package__:
    from .cli_process_supervisor import interrupt_run
    from .version_compatibility_fixture import BASELINE, digest, evidence, write_json
else:
    from cli_process_supervisor import interrupt_run
    from version_compatibility_fixture import BASELINE, digest, evidence, write_json


PRIMARY_KINDS = ("completed", "partial", "interrupted", "uncertain")
BOUNDARIES = ("sealed", "after_preparation", "after_receipt", "after_observation")


def expected_cases() -> set[str]:
    return {
        f"{kind}-{capture}"
        for capture in ("full", "minimal")
        for kind in (*PRIMARY_KINDS, *BOUNDARIES)
    }


def expected_counts(kind: str, capture: str) -> tuple[int, int, int, int, int]:
    """Logical tasks, original requests, physical attempts, acceptances, result rows."""
    if kind in BOUNDARIES:
        return 3, 1, 1, 1, 3
    if kind == "completed":
        return (2, 2, 2, 2, 2) if capture == "full" else (6, 2, 2, 2, 6)
    if kind == "partial":
        return 6, 2, 4, 4, 8
    if kind == "interrupted":
        return 6, 2, 2, 1, 3
    if kind == "uncertain":
        return 6, 2, 2, 2, 6
    raise ValueError("Unknown compatibility workload")


def console() -> Path:
    executable = Path(sys.executable).with_name(
        "judgmentllm.exe" if os.name == "nt" else "judgmentllm"
    )
    if not executable.is_file():
        raise ValueError("The selected interpreter needs its installed console")
    return executable


def identify(archive: Path, expected: str) -> dict[str, Any]:
    package = Path(judgmentllm.__file__).resolve().parent
    if not package.is_relative_to(Path(sys.prefix).resolve()):
        raise ValueError("Compatibility requires an installed package, not a checkout")
    if (
        judgmentllm.__version__ != expected
        or importlib.metadata.version("judgmentllm") != expected
    ):
        raise ValueError(
            "Installed package version differs from required producer/consumer"
        )
    files: dict[str, str] = {}
    with tarfile.open(archive, "r:") as source:
        commit = source.pax_headers.get("comment", "")
        if len(commit) != 40 or any(
            character not in "0123456789abcdef" for character in commit
        ):
            raise ValueError("Source archive requires an exact Git commit")
        if expected == "0.1.0a2" and commit != BASELINE:
            raise ValueError("Baseline producer is not the approved commit")
        for member in source:
            if not member.isfile() or not member.name.startswith("src/judgmentllm/"):
                continue
            name = PurePosixPath(member.name).relative_to("src/judgmentllm")
            stream = source.extractfile(member)
            if stream is None:
                raise ValueError("Source archive member is unreadable")
            with stream:
                expected_hash = hashlib.sha256(stream.read()).hexdigest()
            if digest(package / str(name)) != expected_hash:
                raise ValueError("Installed package differs from archived source")
            files[member.name] = expected_hash
    if not files:
        raise ValueError("Source archive has no package files")
    return {
        "commit": commit,
        "package_version": expected,
        "package_files": files,
        "source_archive_sha256": digest(archive),
        "python": sys.version,
        "interpreter": sys.executable,
        "sqlite": sqlite3.sqlite_version,
        "pydantic": importlib.metadata.version("pydantic"),
        "platform": platform.platform(),
    }


def call_cli(
    root: Path, *arguments: str, expected: tuple[int, ...] = (0,)
) -> dict[str, Any]:
    process = subprocess.run(
        [str(console()), *arguments, "--json"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=45,
    )
    # Retain exact outputs, including failures, without printing synthetic data.
    logs = root / "process-logs"
    logs.mkdir(exist_ok=True)
    sequence = len(tuple(logs.glob("*.json")))
    write_json(
        logs / f"{sequence:04d}.json",
        {
            "args": list(arguments),
            "exit": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
        },
    )
    if process.returncode not in expected:
        raise AssertionError(
            f"Unexpected installed-console exit {process.returncode}; see {logs}"
        )
    lines = process.stdout.splitlines()
    if len(lines) != 1:
        raise AssertionError("Installed console did not return one control envelope")
    result: dict[str, Any] = json.loads(lines[0])
    if result["schema_version"] != 1:
        raise AssertionError("Unsupported control envelope")
    return result


def create_case(root: Path, kind: str, capture: str) -> None:
    root.mkdir(parents=True, exist_ok=False)
    (root / "responses.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "item_id": key,
                    "text": f"Synthetic organized writing with evidence {key}.",
                }
            )
            + "\n"
            for key in ("001", "002")
        ),
        encoding="utf-8",
    )
    spec: dict[str, Any] = {
        "schema_version": 1,
        "task_kind": "rubric",
        "input": "responses.jsonl",
        "provider": "mock",
        "endpoint": "synthetic",
        "model": "synthetic-v1",
        "capture": capture,
        "output_path": str(root / "store"),
        "execution": {"concurrency": 1, "grouping": "grouped"},
        "rubric": {
            "rubric_id": "001",
            "name": "Synthetic compatibility rubric",
            "traits": [
                {
                    "trait_id": key,
                    "name": "Writing",
                    "description": "Evaluate this writing dimension.",
                    "levels": [
                        {"label": label, "numeric_value": code}
                        for label, code in (("low", 1), ("middle", 3), ("high", 8))
                    ],
                }
                for key in ("001", "002", "003")
            ],
        },
    }
    if kind == "completed" and capture == "full":
        spec.pop("rubric")
        spec.update(
            task_kind="pairwise",
            pairs="pairs.jsonl",
            criteria=[
                {
                    "trait_id": "001",
                    "name": "Writing",
                    "description": "Compare supporting evidence.",
                }
            ],
            execution={"concurrency": 1, "grouping": "independent"},
        )
        (root / "pairs.jsonl").write_text(
            "".join(
                json.dumps(
                    {
                        "pair_id": "001",
                        "pair_uid": key,
                        "left_item_id": "001",
                        "right_item_id": "002",
                        "trait_id": "001",
                        "presented_order": order,
                        "is_reversal": key == "002",
                        "source_pair_uid": "001",
                        "reversal_group": "001",
                    }
                )
                + "\n"
                for key, order in (("001", ["001", "002"]), ("002", ["002", "001"]))
            ),
            encoding="utf-8",
        )
    write_json(root / "config.json", spec)
    scenario: dict[str, Any] = {"probabilities": True, "category_index": 0}
    document: dict[str, Any] = {"schema_version": 1, "scenario": scenario}
    if kind == "partial":
        scenario["members"] = {"003": {"outcome": "invalid_category"}}
        scenario["reverse_members"] = True
    elif kind == "uncertain":
        scenario["transport"] = "timeout_after_acceptance"
    elif kind == "interrupted":
        requests = tuple(iter_requests(load_run_spec(root / "config.json")))
        document["scenarios"] = {
            requests[0].request_uid: scenario,
            requests[1].request_uid: {**scenario, "delay_seconds": 60},
        }
    write_json(root / "fixture.json", document)
    write_json(
        root / "valid.json",
        {"schema_version": 1, "scenario": {"probabilities": True, "category_index": 0}},
    )


def settings(root: Path, fixture: str = "fixture.json") -> tuple[str, ...]:
    return (
        "--mock-ledger",
        str(root / "ledger.sqlite3"),
        "--mock-fixture",
        str(root / fixture),
    )


def produce_boundary(root: Path, boundary: str) -> dict[str, Any]:
    """Exit a genuine baseline child at an existing durable engine fault seam."""
    if judgmentllm.__version__ != "0.1.0a2" or boundary not in BOUNDARIES:
        raise ValueError("Boundary production requires the genuine a2 package")
    spec = load_run_spec(root / "config.json")
    run_id = f"a2-{boundary}-{spec.capture}"
    adapter = MockAdapter(
        root / "ledger.sqlite3",
        scenario=MockScenario(
            probabilities=True,
            category_index=0,
        ),
    )
    with prepare_run(
        root / "config.json",
        store=root / "store",
        run_id=run_id,
        capabilities=adapter.capabilities(spec),
    ):
        pass
    planned = tuple(iter_requests(spec))
    if len(planned) != 1 or len(planned[0].tasks) != 3:
        raise AssertionError("Boundary supplement must contain one three-trait group")
    marker = {
        "run_id": run_id,
        "boundary": boundary,
        "package_version": judgmentllm.__version__,
        "producer_module": judgmentllm.__file__,
        "request_uid": planned[0].request_uid,
        "task_ids": [task.task_id for task in planned[0].tasks],
    }
    if boundary == "sealed":
        write_json(root / "boundary.json", marker)
        return marker

    def fault(stage: str, request_uid: str | None) -> None:
        if stage == boundary:
            if request_uid != marker["request_uid"]:
                raise AssertionError("Fault request differs from the baseline plan")
            write_json(root / "boundary.json", marker)
            # The parent owns this exact child and joins its exit. No PID read
            # from the journal selects a termination target.
            os._exit(73)

    asyncio.run(
        execute_run(
            run_id, store=root / "store", adapter=adapter, spec=spec, fault=fault
        )
    )
    raise AssertionError("Baseline did not reach the requested durable boundary")


def verify_boundary_before(
    root: Path, boundary: str, capture: str, before: dict[str, Any]
) -> None:
    """Prove each supplement represents the claimed loss/preparation window."""
    if boundary == "sealed":
        for table in (
            "tasks",
            "requests",
            "attempts",
            "submissions",
            "results",
            "acceptances",
            "observations",
        ):
            if before[table]:
                raise AssertionError("Sealed fixture already materialized execution")
        return
    if (
        len(before["tasks"]),
        len(before["requests"]),
        len(before["attempts"]),
        len(before["submissions"]),
        len(before["results"]),
    ) != (3, 1, 1, 1, 0):
        raise AssertionError("Boundary fixture has unexpected prepared membership")
    accepted = sum(row["acceptance_count"] for row in before["acceptances"])
    if accepted != (0 if boundary == "after_preparation" else 1):
        raise AssertionError("Boundary fixture has unexpected provider acceptance")
    if boundary == "after_preparation":
        if before["attempts"][0]["state"] != "prepared" or before["observations"]:
            raise AssertionError("Prepared fixture passed its submission boundary")
        return
    if len(before["observations"]) != (1 if boundary == "after_observation" else 0):
        raise AssertionError("Receipt/observation window was not preserved")
    receipt = next((root / "store").glob("runs/*/receipts/*.json"))
    raw = json.loads(receipt.read_text(encoding="utf-8"))["raw"]
    if capture == "full":
        artifact = receipt.parent.parent / raw["relative_path"]
        if digest(artifact) != raw["sha256"]:
            raise AssertionError(
                "Full response-loss fixture lacks exact captured bytes"
            )
    elif raw["relative_path"] is not None:
        raise AssertionError("Minimal response-loss fixture retained raw output")


def produce(root: Path) -> dict[str, Any]:
    if judgmentllm.__version__ != "0.1.0a2":
        raise ValueError("Only the authentic a2 interpreter may produce upgrade state")
    root.mkdir(parents=True, exist_ok=False)
    cases: list[dict[str, Any]] = []
    for capture in ("full", "minimal"):
        for kind in (*PRIMARY_KINDS, *BOUNDARIES):
            directory = root / f"{kind}-{capture}"
            create_case(directory, kind, capture)
            if kind in BOUNDARIES:
                # Sources are finalized before baseline preparation commits any
                # identity. Supplements contain just one grouped request.
                response = (
                    (directory / "responses.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()[0]
                )
                (directory / "responses.jsonl").write_text(
                    response + "\n", encoding="utf-8"
                )
                process = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--phase",
                        "produce-boundary",
                        "--boundary",
                        kind,
                        "--output",
                        str(directory),
                    ],
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                (directory / "boundary.stdout.json").write_text(
                    process.stdout, encoding="utf-8"
                )
                (directory / "boundary.stderr.txt").write_text(
                    process.stderr, encoding="utf-8"
                )
                if process.returncode != (0 if kind == "sealed" else 73):
                    raise AssertionError("Genuine baseline boundary child failed")
                marker = json.loads(
                    (directory / "boundary.json").read_text(encoding="utf-8")
                )
                if marker["package_version"] != "0.1.0a2" or marker["boundary"] != kind:
                    raise AssertionError(
                        "Boundary marker conflicts with baseline producer"
                    )
                run_id = marker["run_id"]
            elif kind == "interrupted":
                interrupted = interrupt_run(directory, console(), evidence)
                if not interrupted["exit_confirmed"]:
                    raise AssertionError("Original executor exit was not confirmed")
                run_id = interrupted["run_id"]
                write_json(directory / "interruption.json", interrupted)
            else:
                extra = (
                    ("--mock-no-reconciliation", "--mock-no-retrieval")
                    if kind == "uncertain"
                    else ()
                )
                started = call_cli(
                    directory,
                    "run",
                    "--config",
                    str(directory / "config.json"),
                    "--store",
                    str(directory / "store"),
                    *settings(directory),
                    *extra,
                    expected=(0,) if kind == "completed" else (3,),
                )
                run_id = started["run_id"]
            exports: list[dict[str, Any]] = []
            for format_, view in (
                ("jsonl", "canonical"),
                ("csv", "canonical"),
                ("csv", "r"),
            ):
                report = call_cli(
                    directory,
                    "results",
                    "export",
                    "--run",
                    run_id,
                    "--store",
                    str(directory / "store"),
                    "--format",
                    format_,
                    "--view",
                    view,
                )
                exports.append(report)
            before = evidence(directory)
            if kind in BOUNDARIES:
                verify_boundary_before(directory, kind, capture, before)
            write_json(directory / "before.json", before)
            manifest = next((directory / "store").glob("runs/*/manifest.json"))
            if (
                json.loads(manifest.read_text(encoding="utf-8"))["package_version"]
                != "0.1.0a2"
            ):
                raise AssertionError("Fixture manifest was not produced by a2")
            cases.append(
                {
                    "name": directory.name,
                    "kind": kind,
                    "capture": capture,
                    "run_id": run_id,
                    "manifest_sha256": digest(manifest),
                    "boundary_sha256": digest(directory / "boundary.json")
                    if kind in BOUNDARIES
                    else None,
                    "exports": exports,
                }
            )
    result = {
        "schema_version": 1,
        "producer_version": judgmentllm.__version__,
        "cases": cases,
    }
    write_json(root / "cases.json", result)
    return result


def compare_identities(before: dict[str, Any], after: dict[str, Any]) -> None:
    fields = {
        "tasks": (
            "task_id",
            "run_id",
            "trait_id",
            "item_id",
            "pair_uid",
            "scheduling_ordinal",
        ),
        "requests": (
            "request_uid",
            "run_id",
            "dependency_sha256",
            "scheduling_ordinal",
        ),
        "attempts": (
            "attempt_id",
            "run_id",
            "request_uid",
            "attempt_number",
            "prompt_sha256",
            "payload_sha256",
            "membership_sha256",
            "authorization_id",
        ),
        "submissions": (
            "submission_id",
            "run_id",
            "attempt_id",
            "correlation_id",
            "payload_sha256",
        ),
    }
    for table, names in fields.items():
        key = names[0]
        current = {row[key]: row for row in after[table]}
        for row in before[table]:
            if {name: row[name] for name in names} != {
                name: current[row[key]][name] for name in names
            }:
                raise AssertionError(f"Original {table} identity changed")
    for table in (
        "attempt_tasks",
        "observations",
        "submission_failures",
        "results",
        "acceptances",
    ):
        for row in before[table]:
            if row not in after[table]:
                raise AssertionError(f"Historical {table} evidence changed")
    if not all(
        row["acceptance_count"] == 1 for row in after["acceptances"] if row["accepted"]
    ):
        raise AssertionError("An accepted submission executed more than once")


def negative_checks(root: Path, fixture: dict[str, Any]) -> list[str]:
    """Exercise unchanged integrity guards using actual old stores and consumer."""
    passed: list[str] = []

    def rejected(name: str, operation: Callable[[], object]) -> None:
        try:
            operation()
        except LifecycleError:
            passed.append(name)
        else:
            raise AssertionError(f"Compatibility bypassed {name}")

    for producer, consumer in (
        ("0.1.0a3", "0.1.0a2"),
        ("0.1.0a4", "0.1.0a3"),
        ("0.1.0a2", "0.1.0a4"),
        ("0.1.0a4", "0.1.0a4"),
    ):
        if preparation._compatible_package_version(producer, consumer):  # pyright: ignore[reportPrivateUsage]
            raise AssertionError("Unqualified package transition was accepted")
    passed.append("unsupported-package-transitions")
    for capture in ("full", "minimal"):
        case = next(
            value
            for value in fixture["cases"]
            if value["name"] == f"completed-{capture}"
        )
        directory = root / case["name"]
        run_id = case["run_id"]
        store = directory / "store"
        manifest = next(store.glob("runs/*/manifest.json"))
        original = manifest.read_bytes()
        before = evidence(directory)
        options = MockExecutionOptions(
            ledger_path=directory / "ledger.sqlite3",
            fixture_path=directory / "fixture.json",
        )

        resume = partial(
            resume_run,
            run_id,
            store=store,
            mock=options,
            use_frozen_snapshot=capture == "full",
        )

        incompatible = load_run_spec(directory / "config.json").model_copy(
            update={"model": "changed-model"}
        )
        rejected(
            f"incompatible-context-{capture}",
            partial(resume_run, run_id, store=store, mock=options, spec=incompatible),
        )
        ledger = directory / "ledger.sqlite3"
        held = directory / "held-ledger.sqlite3"
        ledger.rename(held)
        try:
            rejected(f"missing-ledger-{capture}", resume)
            if ledger.exists():
                raise AssertionError("Missing ledger was silently recreated")
        finally:
            held.rename(ledger)
        manifest.write_bytes(original + b" ")
        try:
            rejected(
                f"altered-manifest-{capture}", partial(get_run, run_id, store=store)
            )
        finally:
            manifest.write_bytes(original)
        if capture == "full":
            document = json.loads(original)
            source = next(
                row for row in document["sources"] if row["role"] == "response"
            )
            snapshot = manifest.parent / source["snapshot"]["relative_path"]
            raw = snapshot.read_bytes()
            snapshot.write_bytes(raw + b"changed")
            try:
                rejected("corrupt-frozen-snapshot-full", resume)
            finally:
                snapshot.write_bytes(raw)
        else:
            # Deliberately last: restoring bytes cannot restore original ctime
            # and inode commitments. Retain this failed-source specimen.
            source_path = directory / "responses.jsonl"
            source_path.write_bytes(source_path.read_bytes() + b"\n")
            rejected("changed-original-minimal", resume)
        if evidence(directory)["acceptances"] != before["acceptances"]:
            raise AssertionError("Rejected context/evidence added an acceptance")
        if manifest.read_bytes() != original:
            raise AssertionError(
                "Negative checks did not restore original manifest bytes"
            )
    return passed


def consume(root: Path) -> dict[str, Any]:
    if judgmentllm.__version__ != "0.1.0a3":
        raise ValueError(
            "Cross-version consumption requires the actual installed a3 package"
        )
    fixture: dict[str, Any] = json.loads(
        (root / "cases.json").read_text(encoding="utf-8")
    )
    if (
        fixture["producer_version"] != "0.1.0a2"
        or {case["name"] for case in fixture["cases"]} != expected_cases()
        or len(fixture["cases"]) != len(expected_cases())
    ):
        raise AssertionError(
            "Compatibility matrix is missing required genuine baseline cases"
        )
    passed: list[str] = []
    for case in fixture["cases"]:
        directory = root / case["name"]
        run_id = case["run_id"]
        arguments = ("--run", run_id, "--store", str(directory / "store"))
        before: dict[str, Any] = json.loads(
            (directory / "before.json").read_text(encoding="utf-8")
        )
        manifest = next((directory / "store").glob("runs/*/manifest.json"))
        original = manifest.read_bytes()
        if digest(manifest) != case["manifest_sha256"]:
            raise AssertionError("Input manifest changed before the consumer")
        call_cli(directory, "runs", "status", *arguments)
        call_cli(
            directory,
            "runs",
            "status",
            "--run",
            str(manifest.parent),
            "--store",
            str(directory / "store"),
        )
        call_cli(directory, "runs", "list", "--store", str(directory / "store"))
        if case["kind"] == "uncertain":
            blocked = call_cli(
                directory,
                "resume",
                *arguments,
                *settings(directory),
                "--mock-no-reconciliation",
                "--mock-no-retrieval",
                expected=(3,),
            )
            if (
                blocked["data"]["results"] != 0
                or evidence(directory)["acceptances"] != before["acceptances"]
            ):
                raise AssertionError(
                    "Missing authoritative evidence fabricated an outcome"
                )
        if case["kind"] in {"after_receipt", "after_observation"}:
            recovered = call_cli(
                directory,
                "resume",
                *arguments,
                *settings(directory),
                "--mock-no-retrieval",
                expected=(0,) if case["capture"] == "full" else (3,),
            )
            expected_without_retrieval = 3 if case["capture"] == "full" else 0
            if recovered["data"]["results"] != expected_without_retrieval:
                raise AssertionError(
                    "Response-loss recovery violated capture/evidence policy"
                )
            if evidence(directory)["acceptances"] != before["acceptances"]:
                raise AssertionError("Response-loss recovery repeated an acceptance")
        call_cli(
            directory,
            "resume",
            *arguments,
            *settings(directory, "valid.json"),
            expected=(3,) if case["kind"] in {"partial", "interrupted"} else (0,),
        )
        if case["kind"] == "partial":
            failures = [row for row in before["results"] if row["status"] == "invalid"]
            if len(failures) != 2:
                raise AssertionError(
                    "Partial fixture did not retain both failed members"
                )
            for index, row in enumerate(failures):
                selection = (
                    "--retry-task",
                    row["task_id"],
                    "--retry-id",
                    f"upgrade-retry-{index}",
                )
                call_cli(
                    directory,
                    "resume",
                    *arguments,
                    *settings(directory, "valid.json"),
                    *selection,
                    expected=(0,) if index == len(failures) - 1 else (3,),
                )
                if index == len(failures) - 1:
                    call_cli(
                        directory,
                        "resume",
                        *arguments,
                        *settings(directory, "valid.json"),
                        *selection,
                    )
        after = evidence(directory)
        compare_identities(before, after)
        if case["kind"] in BOUNDARIES:
            marker_path = directory / "boundary.json"
            if digest(marker_path) != case["boundary_sha256"]:
                raise AssertionError("Baseline boundary/plan evidence changed")
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            membership = sorted(after["attempt_tasks"], key=lambda row: row["position"])
            if [row["task_id"] for row in membership] != marker["task_ids"] or [
                row["request_uid"] for row in after["requests"]
            ] != [marker["request_uid"]]:
                raise AssertionError(
                    "Upgraded scheduling differs from the actual a2 plan"
                )
        logical_count, requests, attempts, expected_accepted, expected_rows = (
            expected_counts(case["kind"], case["capture"])
        )
        if len(after["tasks"]) != logical_count or len(after["requests"]) != requests:
            raise AssertionError("Logical and physical request counts changed")
        if len(after["attempts"]) != attempts:
            raise AssertionError("Unexpected attempt count")
        if (
            sum(row["acceptance_count"] for row in after["acceptances"])
            != expected_accepted
        ):
            raise AssertionError("Unexpected accepted submission count after upgrade")
        if len(after["results"]) != expected_rows:
            raise AssertionError("Unexpected all-attempt result count")
        for record in after["results"]:
            if (
                json.loads(record["record_json"])["manifest_sha256"]
                != case["manifest_sha256"]
            ):
                raise AssertionError(
                    "Upgraded results lost original manifest provenance"
                )
        for prior in case["exports"]:
            data = prior["data"]
            historical = call_cli(
                directory,
                "results",
                "export",
                *arguments,
                "--format",
                data["format"],
                "--view",
                data["view"],
                "--high-water-mark",
                str(data["result_high_water_mark"]),
            )
            if historical["data"]["export_id"] != data["export_id"]:
                raise AssertionError("Historical export identity changed")
            for reference in data["files"]:
                if digest(Path(reference["path"])) != reference["sha256"]:
                    raise AssertionError("Historical export bytes changed")
            current = call_cli(
                directory,
                "results",
                "export",
                *arguments,
                "--format",
                data["format"],
                "--view",
                data["view"],
            )
            if current["data"]["row_count"] != expected_rows:
                raise AssertionError("Current export omitted attempts")
        if manifest.read_bytes() != original:
            raise AssertionError("Consumer rewrote original manifest")
        write_json(directory / "after.json", after)
        passed.append(case["name"])
    result = {
        "schema_version": 1,
        "passed": passed,
        "negative_checks": negative_checks(root, fixture),
    }
    write_json(root / "consumer-report.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-python", type=Path)
    parser.add_argument("--candidate-python", type=Path)
    parser.add_argument("--baseline-source-archive", type=Path)
    parser.add_argument("--candidate-source-archive", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--phase",
        choices=(
            "identify-a2",
            "identify-a3",
            "produce",
            "produce-boundary",
            "consume",
        ),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--boundary", choices=BOUNDARIES, help=argparse.SUPPRESS)
    options = parser.parse_args()
    output = options.output.resolve()
    if options.phase:
        if options.phase == "identify-a2":
            result = identify(options.baseline_source_archive, "0.1.0a2")
        elif options.phase == "identify-a3":
            result = identify(options.candidate_source_archive, "0.1.0a3")
        elif options.phase == "produce":
            result = produce(output)
        elif options.phase == "produce-boundary":
            if options.boundary is None:
                parser.error("Boundary child requires an explicit durable boundary")
            result = produce_boundary(output, options.boundary)
        else:
            result = consume(output)
        print(json.dumps(result, sort_keys=True))
        return
    required = (
        options.baseline_python,
        options.candidate_python,
        options.baseline_source_archive,
        options.candidate_source_archive,
    )
    if any(value is None or not value.is_file() for value in required):
        parser.error(
            "Supply both installed interpreters and their existing exact-commit source archives"
        )
    if output.exists():
        parser.error("Qualification output must be a fresh external directory")
    if output.is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error("Keep compatibility stores outside the checkout")
    output.mkdir(parents=True)
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    report: dict[str, Any] = {
        "schema_version": 1,
        "qualified": False,
        "tool_sha256": digest(Path(__file__)),
        "fixture_tool_sha256": digest(
            Path(__file__).with_name("version_compatibility_fixture.py")
        ),
        "supervisor_sha256": digest(
            Path(__file__).with_name("cli_process_supervisor.py")
        ),
        "baseline_source_archive_sha256": digest(options.baseline_source_archive),
        "candidate_source_archive_sha256": digest(options.candidate_source_archive),
        "started_at": time.time(),
        "phases": [],
    }
    try:
        for phase, interpreter in (
            ("identify-a2", options.baseline_python),
            ("identify-a3", options.candidate_python),
            ("produce", options.baseline_python),
            ("consume", options.candidate_python),
        ):
            started = time.perf_counter()
            process = subprocess.run(
                [
                    # Resolving a venv's python symlink selects the base
                    # interpreter and would lose the installed package.
                    str(interpreter.absolute()),
                    str(Path(__file__).resolve()),
                    "--phase",
                    phase,
                    "--baseline-source-archive",
                    str(options.baseline_source_archive.resolve()),
                    "--candidate-source-archive",
                    str(options.candidate_source_archive.resolve()),
                    "--output",
                    str(output / "fixtures"),
                ],
                cwd=output,
                env=environment,
                capture_output=True,
                text=True,
                timeout=300,
            )
            (output / f"{phase}.stdout.json").write_text(
                process.stdout, encoding="utf-8"
            )
            (output / f"{phase}.stderr.txt").write_text(
                process.stderr, encoding="utf-8"
            )
            report["phases"].append(
                {
                    "phase": phase,
                    "exit": process.returncode,
                    "wall_seconds": time.perf_counter() - started,
                }
            )
            if process.returncode:
                raise RuntimeError(
                    f"Compatibility phase {phase} failed; see retained outputs"
                )
        report["qualified"] = True
    finally:
        report["finished_at"] = time.time()
        write_json(output / "report.json", report)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
