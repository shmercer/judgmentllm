"""Synthetic inputs, read-only inspection and owned-process death for the R gate.

R starts every CLI operation. This helper never calls lifecycle/scoring services.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any


def document(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def create(root: Path) -> None:
    for name, capture, missing, pairwise in (
        ("batch-terminal-full", "full", False, False),
        ("batch-terminal-minimal", "minimal", False, False),
        ("batch-missing", "minimal", True, False),
        ("batch-pairwise", "full", False, True),
    ):
        directory = root / name
        directory.mkdir()
        responses = directory / "responses.jsonl"
        responses.write_text(
            """{"schema_version":1,"item_id":"001","text":"Synthetic first response."}
{"schema_version":1,"item_id":"002","text":"Synthetic second response."}
""",
            encoding="utf-8",
        )
        config: dict[str, Any] = {
            "schema_version": 2,
            "task_kind": "pairwise" if pairwise else "rubric",
            "provider": "mock",
            "endpoint": "synthetic",
            "model": "batch-synthetic",
            "input": str(responses),
            "capture": capture,
            "execution": {"mode": "batch_native", "cache_policy": "off"},
            "output_path": str(directory / "store"),
        }
        if pairwise:
            config["criteria"] = [
                {
                    "trait_id": "01",
                    "name": "Quality",
                    "description": "Compare synthetic responses.",
                }
            ]
            pairs = directory / "pairs.jsonl"
            pairs.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "pair_id": "01",
                            "pair_uid": uid,
                            "left_item_id": "001",
                            "right_item_id": "002",
                            "trait_id": "01",
                            "presented_order": order,
                        }
                    )
                    for uid, order in (("01", ["002", "001"]), ("02", ["001", "002"]))
                )
                + "\n",
                encoding="utf-8",
            )
            config["pairs"] = str(pairs)
            fixture = {"schema_version": 1, "rows": [{"probabilities": True}]}
        else:
            config["rubric"] = {
                "schema_version": 1,
                "rubric_id": "01",
                "name": "Synthetic rubric",
                "traits": [
                    {
                        "trait_id": "01",
                        "name": "Quality",
                        "description": "Use the complete synthetic scale.",
                        "levels": [
                            {"label": "001", "numeric_value": 1},
                            {"label": "middle", "numeric_value": 2},
                            {"label": "top", "numeric_value": 7},
                        ],
                    }
                ],
            }
            fixture = {
                "schema_version": 1,
                "rows": [
                    {"probabilities": True},
                    {"outcome": "confirmed_cancellation"},
                ],
                "input_tokens": 7,
                "output_tokens": 3,
                "status_failures": 1,
                "ack_delay_seconds": 30,
                "missing_positions": [1] if missing else [],
            }
        document(directory / "config.json", config)
        document(directory / "batch-fixture.json", fixture)


def inspect(directory: Path) -> dict[str, Any]:
    root = directory / "store"
    result: dict[str, Any] = {}
    with closing(
        sqlite3.connect((root / "registry.sqlite3").as_uri() + "?mode=ro", uri=True)
    ) as connection:
        result["attempts"] = [
            r[0]
            for r in connection.execute(
                "SELECT attempt_id FROM attempts ORDER BY attempt_number,attempt_id"
            )
        ]
        result["submissions"] = [
            list(r)
            for r in connection.execute(
                "SELECT submission_id,remote_id FROM submissions ORDER BY submission_id"
            )
        ]
        result["receipts"] = [
            json.loads(r[0])
            for r in connection.execute(
                "SELECT record_json FROM batch_member_receipts ORDER BY member_key"
            )
        ]
    with closing(
        sqlite3.connect(
            (root / "mock-batch-service.sqlite3").as_uri() + "?mode=ro", uri=True
        )
    ) as connection:
        result["forwards"] = connection.execute(
            "SELECT coalesce(sum(forwards),0) FROM operations"
        ).fetchone()[0]
        result["jobs"] = [
            r[0]
            for r in connection.execute("SELECT remote_id FROM jobs ORDER BY remote_id")
        ]
        result["cancelled_jobs"] = connection.execute(
            "SELECT coalesce(sum(cancelled),0) FROM jobs"
        ).fetchone()[0]
    return result


def kill_after_create(directory: Path) -> dict[str, Any]:
    import psutil

    root = directory / "store"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            evidence = inspect(directory)
            with closing(
                sqlite3.connect(
                    (root / "registry.sqlite3").as_uri() + "?mode=ro", uri=True
                )
            ) as connection:
                row = connection.execute(
                    "SELECT owner_pid FROM runs WHERE run_id='001'"
                ).fetchone()
            if len(evidence["jobs"]) == 1 and row is not None and row[0]:
                process = psutil.Process(row[0])
                command = process.cmdline()
                if (
                    "submit" not in command
                    or "--store" not in command
                    or "001" not in command
                    or Path(command[command.index("--store") + 1]).resolve() != root
                ):
                    raise RuntimeError("synthetic executor identity differs")
                if process.pid == os.getpid():
                    raise RuntimeError("fixture cannot terminate itself")
                process.terminate()
                ended = time.monotonic() + 10
                while (
                    process.is_running()
                    and process.status() != psutil.STATUS_ZOMBIE
                    and time.monotonic() < ended
                ):
                    time.sleep(0.01)
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    raise RuntimeError("synthetic executor did not terminate")
                return {"terminated_owned_executor": True, "jobs": evidence["jobs"]}
        except (sqlite3.Error, FileNotFoundError, psutil.NoSuchProcess):
            pass
        time.sleep(0.01)
    raise RuntimeError("synthetic create did not reach the bounded death window")
