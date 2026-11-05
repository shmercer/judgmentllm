"""Synthetic files and read-only independent evidence for installed-CLI acceptance.

R owns every lifecycle invocation. Only offline structural planning is imported
to select a deterministic delayed request; execution services are never called.
The interruption helper terminates only the executor bound to this fresh fixture.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


def document(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def create(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for kind in ("independent", "grouped", "pairwise", "recovery", "interrupted"):
        captures = (
            ("full", "minimal")
            if kind in {"independent", "grouped", "pairwise"}
            else ("minimal",)
        )
        for capture in captures:
            directory = root / f"{kind}-{capture}"
            directory.mkdir(exist_ok=False)
            (directory / "responses.jsonl").write_text(
                "".join(
                    json.dumps(
                        {"item_id": key, "text": f"SOURCE_ONLY_SENTINEL résumé {key}"}
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
                "output_path": str((directory / "store").resolve()),
                "execution": {
                    "concurrency": 1,
                    "grouping": "grouped" if kind == "grouped" else "independent",
                    "explanations": True,
                },
                "rubric": {
                    "rubric_id": "001",
                    "name": "Synthetic rubric",
                    "traits": [
                        {
                            "trait_id": key,
                            "name": "Quality",
                            "description": "Assess quality.",
                            "levels": [
                                {"label": "low", "numeric_value": 1},
                                {"label": "middle", "numeric_value": 3},
                                {"label": "high", "numeric_value": 8},
                            ],
                        }
                        for key in (
                            ("001", "002", "003") if kind == "grouped" else ("001",)
                        )
                    ],
                },
            }
            if kind == "pairwise":
                spec.pop("rubric")
                spec.update(
                    task_kind="pairwise",
                    criteria=[
                        {
                            "trait_id": "001",
                            "name": "Quality",
                            "description": "Compare.",
                        }
                    ],
                    pairs="pairs.jsonl",
                )
                (directory / "pairs.jsonl").write_text(
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
                        for key, order in (
                            ("001", ["001", "002"]),
                            ("002", ["002", "001"]),
                        )
                    ),
                    encoding="utf-8",
                )
            document(directory / "config.json", spec)
            scenario: dict[str, Any] = {
                "probabilities": kind != "independent",
                "category_index": 0,
                "explanation": "Authorized normalized explanation.",
                "raw_marker": "RAW_ONLY_SENTINEL",
            }
            if kind == "grouped":
                scenario.update(
                    members={"003": {"outcome": "invalid_category"}},
                    reverse_members=True,
                )
            elif kind == "recovery":
                scenario["transport"] = "timeout_after_acceptance"
            elif kind == "interrupted":
                scenario["delay_seconds"] = 60
            fixture_document: dict[str, Any] = {
                "schema_version": 1,
                "scenario": scenario,
            }
            if kind == "interrupted":
                from judgmentllm.config import load_run_spec
                from judgmentllm.planning import iter_requests

                requests = tuple(
                    iter_requests(load_run_spec(directory / "config.json"))
                )
                fixture_document["scenarios"] = {
                    requests[0].request_uid: {**scenario, "delay_seconds": 0},
                    requests[1].request_uid: scenario,
                }
            document(directory / "fixture.json", fixture_document)
            document(
                directory / "valid.json",
                {
                    "schema_version": 1,
                    "scenario": {**scenario, "members": {}, "delay_seconds": 0},
                },
            )


def read_store(directory: Path) -> dict[str, Any]:
    database = directory / "store" / "registry.sqlite3"
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        tables: dict[str, Any] = {}
        for table in (
            "runs",
            "tasks",
            "requests",
            "attempts",
            "attempt_tasks",
            "retry_operations",
            "submissions",
            "observations",
            "submission_failures",
            "results",
        ):
            tables[table] = [
                dict(row) for row in connection.execute(f"SELECT * FROM {table}")
            ]
    ledger = directory / "ledger.sqlite3"
    with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        tables["acceptances"] = [
            dict(row) for row in connection.execute("SELECT * FROM acceptances")
        ]
    return tables


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", choices=("create", "inspect", "interrupt"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--executable", type=Path)
    options = parser.parse_args()
    root = options.output.resolve()
    if options.phase == "create":
        create(root)
    elif options.phase == "interrupt":
        from cli_process_supervisor import interrupt_run

        if options.executable is None or not options.executable.is_absolute():
            parser.error("interrupt requires an absolute installed --executable path")
        print(json.dumps(interrupt_run(root, options.executable, read_store)))
    else:
        print(json.dumps(read_store(root)))


if __name__ == "__main__":
    main()
