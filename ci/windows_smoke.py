"""Bounded Windows diagnostics; never a substitute for planned suite receipts."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent / "local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from environment import clean_environment
from local.preflight_provenance import (
    ATTEMPT_ENV,
    RUN_ID_ENV,
    receipt_provenance,
    source_record,
    verify_preflight_provenance,
)
from local.suite import WARNINGS, outcomes
from selection import inventory_items, read

from scripts.cli_process_windows import WindowsProcess

POLICY = Path(__file__).resolve().parents[1]

SMOKE = {
    "tests/test_batch_compatibility.py::test_historical_wire_preserves_native_writer_and_exact_identity": (
        "",
    ),
    "tests/test_batch_compatibility.py::test_foreign_snapshot_preserves_proof_without_touching_original_aliases": (
        "[manifest-nonpath]",
        "[manifest-path]",
        "[spec-nonpath]",
        "[spec-path]",
    ),
    "tests/test_batch_privacy_bounds.py::test_privacy_audit_defers_only_the_live_windows_lock": (
        "",
    ),
    "tests/test_batch_io.py::test_each_protocol_boundary_obeys_shared_deadline": (
        "[cancel]",
        "[create]",
        "[reconcile]",
        "[source]",
        "[status]",
        "[stream]",
        "[upload]",
    ),
    "tests/test_batch_io.py::test_mock_connections_close_without_gc_and_keep_rollback": (
        "",
    ),
}
FALLBACK = (
    "Windows ordinary diagnostics unavailable or invalid. Full suite: "
    "unexecuted/incomplete; completion not verified.\n"
)
WORK_SECONDS = 300
OUTCOMES = {"success", "failure", "cancelled", "skipped"}


def expected_nodes() -> set[str]:
    expected = {
        selector + parameter
        for selector, parameters in SMOKE.items()
        for parameter in parameters
    }
    actual = {
        item["nodeid"]: item["markers"]
        for item in inventory_items(read(POLICY / "ci/test-inventory.json"))
        if item["nodeid"].split("[", 1)[0] in SMOKE
    }
    if len(expected) != 14 or set(actual) != expected or any(actual.values()):
        raise ValueError("Protected smoke inventory changed")
    return expected


def document(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Unavailable diagnostic document")
    value = cast(object, read(path))
    if not isinstance(value, dict):
        raise ValueError("Invalid diagnostic document")
    return cast(dict[str, Any], value)


def validate_report(
    report: dict[str, Any], expected: dict[str, Any], nodes: set[str]
) -> None:
    if (
        type(report.get("schema_version")) is not int
        or report["schema_version"] != 1
        or type(report.get("exit_code")) is not int
        or report["exit_code"] != 0
        or report.get("execution_group") != "windows-smoke"
    ):
        raise ValueError("Incomplete smoke execution")
    verify_preflight_provenance(report, expected)
    for item in report["collected"]:
        markers = cast(object, item["markers"])
        if (
            not isinstance(markers, list)
            or not all(
                isinstance(marker, str) for marker in cast(list[object], markers)
            )
            or {"serial", "resource", "r_interop", "smoke"}.intersection(
                cast(list[object], markers)
            )
        ):
            raise ValueError("Smoke isolation changed")
    if dict(outcomes(report, nodes)) != {"passed": 14}:
        raise ValueError("All fourteen smoke cases must pass")


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("Smoke wall budget expired")
    return seconds


def execute(
    command: list[str],
    source: Path,
    stream: BinaryIO,
    deadline: float,
    status: dict[str, str],
) -> int:
    import psutil

    # Constructor failure already owns its 10s handle wait plus 10s close.
    owned = WindowsProcess(command, source, stream, stream)
    try:
        process = psutil.Process(owned.pid)
        if not owned.owns(process.pid):
            raise ValueError("Smoke launcher ownership unavailable")
        code = process.wait(timeout=remaining(deadline))
        if type(code) is not int:
            raise ValueError("Smoke exit status unavailable")
        return code
    finally:
        # Exactly one close: its internal termination wait is at most 10s.
        owned.close()
        status["cleanup"] = "confirmed"


def run_smoke(
    source: Path, output: Path, plan_path: Path, head: str, lane: str, run_id: str
) -> int:
    import psutil

    deadline = time.monotonic() + WORK_SECONDS
    status = {"outcome": "failed", "cleanup": "unconfirmed"}
    source, output = source.resolve(strict=True), output.resolve()
    if output.is_relative_to(source):
        raise ValueError("Diagnostics must be outside the candidate")
    output.mkdir(parents=True, exist_ok=False)
    try:
        if (
            sys.platform != "win32"
            or lane != f"windows-{sys.version_info.major}.{sys.version_info.minor}"
        ):
            raise ValueError("Smoke interpreter differs from its Windows lane")
        plan = read(plan_path)
        planned_lane = next(item for item in plan["lanes"] if item["id"] == lane)
        if plan["source_sha"] != head or not any(
            item["id"] == lane and item["partition"] == "ordinary"
            for item in plan["hosted_tasks"]
        ):
            raise ValueError("Smoke differs from the planned source/task")
        if planned_lane["os"] != "windows-latest" or planned_lane["python"] != (
            f"{sys.version_info.major}.{sys.version_info.minor}"
        ):
            raise ValueError("Smoke interpreter differs from the planned interpreter")
        environment = clean_environment()
        environment.update(
            {
                RUN_ID_ENV: f"{run_id}:{lane}:ordinary:windows-smoke",
                ATTEMPT_ENV: uuid.uuid4().hex,
                "JUDGMENTLLM_CI_GROUP": "windows-smoke",
                "PYTHONPATH": str(POLICY),
            }
        )
        # WindowsProcess inherits the parent environment; scrub before construction.
        os.environ.clear()
        os.environ.update(environment)
        if (source / ".judgmentllm-source.json").exists():
            raise ValueError("Hosted smoke requires a Git checkout")
        before = source_record(source)
        if (
            before["commit"] != head
            or before["dirty"] is not False
            or before["lockfile_sha256"]
            not in {
                plan["lockfile_sha256"],
                plan["windows_crlf_lockfile_sha256"],
            }
            or any(
                not isinstance(version, str) or not version
                for version in before["dependencies"].values()
            )
        ):
            raise ValueError("Smoke source/dependencies differ from the plan")
        expected = receipt_provenance(
            before, environment[RUN_ID_ENV], environment[ATTEMPT_ENV]
        )
        nodes = expected_nodes()
        command = [
            sys.executable,
            "-P",
            "-m",
            "pytest",
            "-p",
            "ci.local.pytest_report",
            "--no-cov",
            "-q",
            *WARNINGS,
            *SMOKE,
            f"--ci-report={output / 'cases.json'}",
            f"--basetemp={output / 't'}",
            "-o",
            f"cache_dir={output / 'cache'}",
        ]
        remaining(deadline)
        with (output / "smoke.log").open("wb") as stream:
            code = execute(command, source, stream, deadline, status)
        if code != 0:
            raise ValueError("Smoke process failed")
        validate_report(document(output / "cases.json"), expected, nodes)
        if source_record(source) != before:
            raise ValueError("Smoke source/environment changed")
        remaining(deadline)
        status["outcome"] = "passed"
    except (TimeoutError, psutil.TimeoutExpired):
        status["outcome"] = "timed-out"
    except Exception:
        # Exception text can contain candidate inputs; only fixed outcomes escape.
        pass
    finally:
        (output / "status.json").write_text(json.dumps(status) + "\n", encoding="utf-8")
    return int(status != {"outcome": "passed", "cleanup": "confirmed"})


def summary_text(
    output: Path,
    evidence: Path,
    head: str,
    lane: str,
    run_id: str,
    checks: str,
    upload: str,
) -> str:
    if checks not in OUTCOMES or upload not in OUTCOMES:
        raise ValueError("Invalid workflow outcome")
    status = document(output / "status.json")
    if (
        set(status) != {"outcome", "cleanup"}
        or status["outcome"] not in {"passed", "failed", "timed-out"}
        or status["cleanup"] not in {"confirmed", "unconfirmed"}
        or status["outcome"] == "passed"
        and status["cleanup"] != "confirmed"
    ):
        raise ValueError("Invalid smoke diagnostic")
    full = "unexecuted/incomplete"
    if checks == "success":
        if status != {"outcome": "passed", "cleanup": "confirmed"}:
            raise ValueError("Contradictory smoke/full completion")
        receipt = document(evidence / "receipt.json")
        suite = document(evidence / "suite/summary.json")
        if (
            any(
                receipt.get(key) != value
                for key, value in {
                    "source_sha": head,
                    "lane": lane,
                    "run_id": run_id,
                    "partition": "ordinary",
                    "status": "passed",
                    "phase": "complete",
                    "execution_scope": "hosted",
                }.items()
            )
            or receipt.get("source_verified") is not True
            or suite.get("status") != "passed"
            or suite.get("selection_complete") is not True
            or suite["source"]["commit"] != head
            or suite["source"]["dirty"] is not False
        ):
            raise ValueError("Full completion unverified")
        full = "completion reported; existing acceptance remains authoritative"
    artifact = "available" if upload == "success" else "unavailable"
    return (
        f"Windows ordinary smoke: {status['outcome']}; cleanup: {status['cleanup']}.\n\n"
        f"Full suite: {full}. Diagnostics artifact: {artifact}.\n"
    )


def append_summary(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "summary"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--head", required=True)
    parser.add_argument(
        "--lane", choices=("windows-3.11", "windows-3.12"), required=True
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--checks", default="skipped")
    parser.add_argument("--upload", default="skipped")
    args = parser.parse_args()
    code = 1
    try:
        if (
            re.fullmatch(r"[0-9a-f]{40}", args.head) is None
            or re.fullmatch(r"[0-9]+:[0-9]+", args.run_id) is None
        ):
            raise ValueError("Invalid workflow identity")
        if args.operation == "run":
            if args.plan is None:
                raise ValueError("Missing plan")
            code = run_smoke(
                args.source, args.output, args.plan, args.head, args.lane, args.run_id
            )
        else:
            code = 0
        try:
            text = summary_text(
                args.output,
                args.output.parent / "v",
                args.head,
                args.lane,
                args.run_id,
                args.checks,
                args.upload,
            )
        except Exception:
            text = FALLBACK
        append_summary(args.summary, text)
    except Exception:
        try:
            append_summary(args.summary, FALLBACK)
        except OSError:
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
