"""Inspect a partial run and explicitly retry failed members using installed CLI.

All inputs are synthetic. The example preserves earlier failed rows and invokes
each lifecycle operation in a separate process, as an R caller would.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="Fresh local directory"
    )
    options = parser.parse_args()
    root = options.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    store = root / "store"
    partial, good = root / "partial.json", root / "good.json"
    partial.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenario": {
                    "probabilities": True,
                    "members": {"evidence": {"outcome": "invalid_category"}},
                },
            }
        ),
        encoding="utf-8",
    )
    good.write_text(
        json.dumps({"schema_version": 1, "scenario": {"probabilities": True}}),
        encoding="utf-8",
    )
    console = Path(sys.executable).parent / (
        "judgmentllm.exe" if os.name == "nt" else "judgmentllm"
    )
    configuration = Path(__file__).with_name("mock-run.yaml")

    def command(*arguments: str, expected: int = 0) -> dict[str, Any]:
        completed = subprocess.run(
            [str(console), *arguments, "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != expected:
            raise RuntimeError(
                f"CLI returned {completed.returncode}; expected {expected}: {completed.stderr}"
            )
        value: dict[str, Any] = json.loads(completed.stdout)
        for line in completed.stderr.splitlines():
            json.loads(line)  # Diagnostics have a separate JSONL channel.
        print(completed.stdout.strip())
        return value

    command("validate", "--config", str(configuration))
    command("plan", "--config", str(configuration))
    first = command(
        "run",
        "--config",
        str(configuration),
        "--store",
        str(store),
        "--mock-fixture",
        str(partial),
        expected=3,
    )
    run_id = first["run_id"]
    reference = ("--run", run_id, "--store", str(store))
    command("runs", "status", *reference)
    # Ordinary recovery preserves failures; it does not authorize new judgments.
    command("resume", *reference, "--mock-fixture", str(partial), expected=3)
    before = command("results", "export", *reference)
    path = Path(before["artifacts"]["export"])
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    failed = [row for row in rows if row["status"] != "complete"]
    assert failed
    # Each selected member belongs to its own original grouped request.
    for index, row in enumerate(failed):
        command(
            "resume",
            *reference,
            "--mock-fixture",
            str(good),
            "--retry-task",
            row["task_id"],
            "--retry-id",
            f"failed-member-{index:03d}",
            expected=3 if index < len(failed) - 1 else 0,
        )
    canonical = command("results", "export", *reference)
    final = [
        json.loads(line)
        for line in Path(canonical["artifacts"]["export"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(final) == len(rows) + len(failed)
    assert sum(row["status"] != "complete" for row in final) == len(failed)
    prior = command(
        "results",
        "export",
        *reference,
        "--high-water-mark",
        str(before["data"]["result_high_water_mark"]),
    )
    assert prior["data"]["export_id"] == before["data"]["export_id"]
    assert prior["data"]["files"] == before["data"]["files"]
    command("results", "export", *reference, "--format", "csv", "--view", "r")


if __name__ == "__main__":
    main()
