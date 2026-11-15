"""Small bounded stage scheduler; failure never establishes partial acceptance."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Stage:
    name: str
    invoke: Callable[[], dict[str, Any]]
    dependencies: tuple[str, ...] = ()
    cpus: int = 1
    memory_bytes: int = 0
    kind: str = "ordinary"


def run(
    stages: list[Stage],
    *,
    cpus: int,
    memory_bytes: int,
    output: Path,
    cancel: threading.Event,
    r_workers: int = 2,
) -> dict[str, Any]:
    names = {stage.name for stage in stages}
    if (
        len(names) != len(stages)
        or type(cpus) is not int
        or cpus < 1
        or not 1 <= r_workers <= 2
        or any(
            stage.cpus < 1
            or stage.cpus > cpus
            or stage.memory_bytes < 0
            or stage.memory_bytes > memory_bytes
            or not set(stage.dependencies) <= names
            or stage.name in stage.dependencies
            for stage in stages
        )
    ):
        raise ValueError("Invalid stage graph or capacity")
    pending = list(stages)
    active: dict[Future[dict[str, Any]], tuple[Stage, float]] = {}
    results: dict[str, Any] = {}
    started = time.monotonic()
    status = "passed"
    output.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        output.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": status,
                    "cpus": cpus,
                    "memory_bytes": memory_bytes,
                    "r_workers": r_workers,
                    "critical_path_seconds": time.monotonic() - started,
                    "stages": results,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    with ThreadPoolExecutor(
        max_workers=max(1, min(len(stages), 4)), thread_name_prefix="ci-stage"
    ) as pool:
        try:
            while pending or active:
                if cancel.is_set() and status == "passed":
                    status = "interrupted"
                if status == "passed":
                    for stage in list(pending):
                        if not all(
                            name in results and results[name]["status"] == "passed"
                            for name in stage.dependencies
                        ):
                            continue
                        used_cpus = sum(item.cpus for item, _ in active.values())
                        used_memory = sum(
                            item.memory_bytes for item, _ in active.values()
                        )
                        used_r = sum(item.kind == "r" for item, _ in active.values())
                        if (
                            len(active) >= 4
                            or used_cpus + stage.cpus > cpus
                            or used_memory + stage.memory_bytes > memory_bytes
                            or stage.kind == "r"
                            and used_r >= r_workers
                        ):
                            continue
                        pending.remove(stage)
                        before = time.monotonic()
                        active[pool.submit(stage.invoke)] = (stage, before)
                        results[stage.name] = {
                            "status": "running",
                            "start_seconds": before - started,
                            "waiting_seconds": before - started,
                            "cpus": stage.cpus,
                            "memory_bytes": stage.memory_bytes,
                            "kind": stage.kind,
                            "dependencies": list(stage.dependencies),
                        }
                        save()
                if not active:
                    if pending and status == "passed":
                        raise ValueError("Cyclic or unsatisfied execution graph")
                    break
                completed, _ = wait(active, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in completed:
                    stage, before = active.pop(future)
                    try:
                        result = future.result()
                    except Exception as error:
                        result = {
                            "exit_code": 1,
                            "error": {
                                "type": type(error).__name__,
                                "message": str(error),
                            },
                        }
                    passed = (
                        type(result.get("exit_code")) is int
                        and result["exit_code"] == 0
                    )
                    results[stage.name].update(
                        status="passed" if passed else "failed",
                        stop_seconds=time.monotonic() - started,
                        seconds=time.monotonic() - before,
                        result=result,
                    )
                    if not passed:
                        status = "failed"
                        cancel.set()
                    save()
        except BaseException:
            cancel.set()
            status = "interrupted"
            save()
            raise
    for stage in pending:
        results[stage.name] = {"status": "not-executed", "reason": status}
    save()
    return {
        "status": status,
        "stages": results,
        "critical_path_seconds": time.monotonic() - started,
    }


def choose(
    requested: str,
    *,
    certificate: dict[str, Any] | None,
    machine: dict[str, Any],
    policy_hash: str,
) -> tuple[str, str]:
    if requested not in {"auto", "conservative", "concurrent", "resource-isolated"}:
        raise ValueError("Unknown local schedule")
    if requested != "auto":
        return (
            requested,
            "explicit execution profile; all verification obligations retained",
        )
    if (
        certificate is not None
        and certificate.get("schema_version") == 1
        and certificate.get("policy_sha256") == policy_hash
        and certificate.get("machine") == machine
        and certificate.get("equivalent") is True
        and certificate.get("cleanup_passed") is True
    ):
        trials = certificate.get("trials", [])
        baseline = [
            item["seconds"] for item in trials if item["profile"] == "conservative"
        ]
        concurrent = [
            item["seconds"]
            for item in trials
            if item["profile"] == "concurrent"
            and item.get("workers") == certificate.get("ordinary_workers")
        ]
        if (
            len(baseline) >= 2
            and len(concurrent) >= 2
            and all(item.get("passed") is True for item in trials)
        ):
            variability = max(
                max(baseline) - min(baseline), max(concurrent) - min(concurrent)
            )
            gain = sum(baseline) / len(baseline) - sum(concurrent) / len(concurrent)
            if gain > max(variability, 0.05 * sum(baseline) / len(baseline)):
                return (
                    "concurrent",
                    "same-machine whole-bundle benefit exceeds observed variability",
                )
    return (
        "conservative",
        "no current equivalent, reliable whole-bundle speedup certificate",
    )


def validate_execution(
    record: dict[str, Any], groups: dict[str, str], r_names: list[str], profile: str
) -> None:
    """Recheck isolation independently of the scheduler's claimed success."""
    import math

    stages = record["stages"]
    expected = set(groups) | {"r-" + name for name in r_names}
    if record.get("status") != "passed" or set(stages) != expected:
        raise ValueError("Missing, failed or unexpected scheduler stages")
    events: list[tuple[float, int, str]] = []
    for name, item in stages.items():
        start, stop = item["start_seconds"], item["stop_seconds"]
        if (
            item["status"] != "passed"
            or item["result"].get("exit_code") != 0
            or not all(
                isinstance(t, (int, float)) and math.isfinite(t) for t in (start, stop)
            )
            or not 0 <= start <= stop <= record["critical_path_seconds"]
        ):
            raise ValueError("Incomplete scheduler timing or outcome")
        events.extend(((start, 1, name), (stop, -1, name)))
    active: set[str] = set()
    for _, event, name in sorted(events):
        if event == -1:
            active.remove(name)
            continue
        incompatible = profile == "conservative" and bool(active)
        exclusive = {
            key
            for key, kind in groups.items()
            if kind == "resource" or key in {"serial-global", "serial-host"}
        }
        if incompatible or name in exclusive and active or active & exclusive:
            raise ValueError("Exclusive serial/resource isolation was violated")
        active.add(name)
        if (
            sum(stages[key]["cpus"] for key in active) > record["cpus"]
            or sum(stages[key]["memory_bytes"] for key in active)
            > record["memory_bytes"]
            or sum(key.startswith("r-") for key in active) > record["r_workers"]
        ):
            raise ValueError("Concurrent execution exceeded the sealed allocation")
