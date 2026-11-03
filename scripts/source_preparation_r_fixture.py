"""Freeze synthetic sources, then reopen/export in an independent process.

These records describe actual executable preparation manifests and indexed
inputs. They contain no provider submissions, attempts, or model judgments.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Literal

from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, semantic_digest
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.prompts import get_template

RUNS: tuple[tuple[str, Literal["full", "minimal"], str], ...] = (
    ("001", "full", "rubric"),
    ("002", "minimal", "rubric"),
    ("003", "full", "pairwise"),
    ("004", "minimal", "pairwise"),
)


def write_document(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def sources(directory: Path, capture: str, kind: str) -> Path:
    """Include document references, Unicode, multiline CSV, and string IDs."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "responses.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(["item_id", "text", "metadata"])
        writer.writerow(["001", 'Synthetic first résumé,\nwith "quotation".', "{}"])
        writer.writerow(["002", "Synthetic second response.", "{}"])
    write_document(
        directory / "exemplars.json",
        {
            "schema_version": 1,
            "exemplars": [{"exemplar_id": "001", "text": "Synthetic anchor."}],
        },
    )
    configuration: dict[str, object] = {
        "schema_version": 1,
        "task_kind": kind,
        "input": "responses.csv",
        "provider": "fixture",
        "endpoint": "generative",
        "model": "synthetic-v1",
        "generation": {"temperature": 0, "max_output_tokens": 127},
        "capture": capture,
        "seed": 41,
        "exemplars": "exemplars.json",
        "prompt": {"file": "template.txt"},
    }
    if kind == "rubric":
        configuration["rubric"] = "rubric.json"
        write_document(
            directory / "rubric.json",
            {
                "schema_version": 1,
                "rubric_id": "001",
                "name": "Synthetic rubric",
                "traits": [
                    {
                        "trait_id": "001",
                        "name": "Quality",
                        "description": "Synthetic criterion.",
                        "levels": [
                            {"label": "low", "numeric_value": 1.0},
                            {"label": "high", "numeric_value": 5.0},
                        ],
                    }
                ],
            },
        )
        preset = "rubric_scores_only"
    else:
        configuration.update(criteria="criteria.json", pairs="pairs.jsonl")
        write_document(
            directory / "criteria.json",
            {
                "schema_version": 1,
                "criteria": [
                    {
                        "trait_id": "001",
                        "name": "Quality",
                        "description": "Choose the stronger synthetic response.",
                    }
                ],
            },
        )
        pair: dict[str, object] = {
            "schema_version": 1,
            "pair_id": "001",
            "pair_uid": "001",
            "left_item_id": "001",
            "right_item_id": "002",
            "trait_id": "001",
        }
        reverse = {
            **pair,
            "pair_uid": "002",
            "presented_order": ["002", "001"],
            "source_pair_uid": "001",
            "is_reversal": True,
            "reversal_group": "001",
        }
        (directory / "pairs.jsonl").write_text(
            "\n".join(json.dumps(record) for record in (pair, reverse)) + "\n",
            encoding="utf-8",
        )
        preset = "pairwise_forced_choice"
    (directory / "template.txt").write_text(get_template(preset), encoding="utf-8")
    path = directory / "run.json"
    write_document(path, configuration)
    return path


def persist(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    commitments: dict[str, str] = {}
    for run_id, capture, kind in RUNS:
        configuration = sources(output / "inputs" / run_id, capture, kind)
        with prepare_run(
            configuration, store=output / "store", run_id=run_id
        ) as prepared:
            commitments[run_id] = prepared.manifest_sha256
    write_document(output / "persisted_manifest_hashes.json", commitments)


def export(output: Path) -> None:
    frozen = json.loads(
        (output / "persisted_manifest_hashes.json").read_text(encoding="utf-8")
    )
    rows: list[dict[str, object]] = []
    for run_id, capture, kind in RUNS:
        with open_prepared_run(run_id, store=output / "store") as prepared:
            assert prepared.manifest_sha256 == frozen[run_id]
            reference = prepared.repository.get_manifest(run_id).artifact
            store = ArtifactStore(prepared.repository.run_directory(run_id))
            manifest_path = store.verify(reference)
            assert manifest_path is not None
            assert bytes_digest(manifest_path.read_bytes()) == frozen[run_id]
            responses = list(prepared.iter_responses())
            pairs = list(prepared.iter_pairs()) if kind == "pairwise" else []
            assert prepared.get_response("001") == responses[0]
            dependencies = list(prepared.iter_request_dependencies())
            if pairs:
                assert prepared.get_pair("002") == pairs[1]
                assert all(
                    dependency.left_sha256 is not None
                    and dependency.right_sha256 is not None
                    for dependency in dependencies
                )
            rows.append(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "capture": capture,
                    "task_kind": kind,
                    "manifest_sha256": prepared.manifest_sha256,
                    "manifest_path": str(manifest_path.resolve()),
                    "manifest": prepared.manifest.model_dump(mode="json"),
                    "response_ids": [response.item_id for response in responses],
                    "response_hashes": [
                        semantic_digest("input-response", response)
                        for response in responses
                    ],
                    "pairs": [
                        {
                            **pair.model_dump(mode="json"),
                            "original_order": pair.original_order,
                        }
                        for pair in pairs
                    ],
                    "request_count": len(dependencies),
                    "dependency_hashes": [
                        dependency.dependency_sha256 for dependency in dependencies
                    ],
                }
            )
    (output / "source_preparation.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("persist", "export"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if options.phase == "persist":
        persist(options.output)
    else:
        export(options.output)


if __name__ == "__main__":
    main()
