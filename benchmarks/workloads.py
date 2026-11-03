"""Deterministic offline performance inputs; never installed with the library."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workload:
    name: str
    traits: int
    group_size: int
    requests: int
    kind: str = "rubric"
    prefix: bool = False


WORKLOADS = (
    Workload("independent-3", 3, 1, 600),
    Workload("grouped-6", 6, 6, 200),
    Workload("independent-20", 20, 1, 4000),
    Workload("grouped-20", 20, 5, 800),
    Workload("grouped-200-prefix", 200, 5, 64, prefix=True),
    Workload("pairwise", 3, 1, 1200, kind="pairwise"),
    Workload("subset-retry", 20, 5, 1600, kind="retry"),
)


def inputs(directory: Path, workload: Workload, capture: str) -> Path:
    """Restore an empty run with fixed source content, seed and character IDs."""
    directory.mkdir(parents=True)
    response = directory / "responses.jsonl"
    with response.open("w", encoding="utf-8", newline="") as stream:
        for index in range(200):
            stream.write(
                json.dumps({"item_id": f"{index:06d}", "text": "x" * 4096}) + "\n"
            )
    configuration: dict[str, object] = {
        "task_kind": "pairwise" if workload.kind == "pairwise" else "rubric",
        "input": response.name,
        "provider": "mock",
        "endpoint": "synthetic",
        "model": "fixture",
        "capture": capture,
        "seed": 42,
        "execution": {
            "concurrency": 4,
            "grouping": "auto" if workload.group_size > 1 else "independent",
            "max_traits_per_request": workload.group_size,
        },
    }
    if workload.kind == "pairwise":
        configuration["criteria"] = [
            {
                "trait_id": f"{trait:03d}",
                "name": "Quality",
                "description": "Compare the synthetic responses.",
            }
            for trait in range(3)
        ]
        configuration["pairs"] = "pairs.jsonl"
        with (directory / "pairs.jsonl").open(
            "w", encoding="utf-8", newline=""
        ) as stream:
            for pair in range(100):
                left, right = f"{2 * pair:06d}", f"{2 * pair + 1:06d}"
                for trait in range(3):
                    for repetition in range(2):
                        original = f"{pair:03d}-{trait:03d}-{repetition}-0"
                        for reversal in range(2):
                            stream.write(
                                json.dumps(
                                    {
                                        "pair_id": f"{pair:03d}",
                                        "pair_uid": f"{pair:03d}-{trait:03d}-{repetition}-{reversal}",
                                        "left_item_id": left,
                                        "right_item_id": right,
                                        "trait_id": f"{trait:03d}",
                                        "presented_order": [right, left]
                                        if reversal
                                        else [left, right],
                                        "repetition_index": repetition,
                                        "is_reversal": bool(reversal),
                                        "source_pair_uid": original
                                        if reversal
                                        else None,
                                        "reversal_group": original,
                                    }
                                )
                                + "\n"
                            )
    else:
        configuration["rubric"] = {
            "rubric_id": "001",
            "name": "Synthetic execution memory fixture"
            if workload.prefix
            else "Synthetic execution performance fixture",
            "traits": [
                {
                    "trait_id": f"{trait:03d}",
                    "name": "Quality",
                    "description": "Synthetic assessment.",
                    "levels": [{"label": "low"}, {"label": "high"}]
                    if workload.prefix
                    else [
                        {"label": "low", "numeric_value": 1},
                        {"label": "middle", "numeric_value": 3},
                        {"label": "high", "numeric_value": 8},
                    ],
                }
                for trait in range(workload.traits)
            ],
        }
    path = directory / "run.json"
    path.write_text(json.dumps(configuration), encoding="utf-8")
    return path
