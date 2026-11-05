"""Deterministic offline performance inputs; never installed with the library."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Workload:
    name: str
    traits: int
    group_size: int
    requests: int
    kind: str = "rubric"
    prefix: bool = False
    responses: int = 200
    concurrency: int = 4
    response_bytes: int = 4096
    variant: str = "default"
    scale: bool = False

    @property
    def logical_tasks(self) -> int:
        return self.responses * self.traits

    @property
    def physical_requests(self) -> int:
        return self.responses * ((self.traits + self.group_size - 1) // self.group_size)


WORKLOADS = (
    Workload("independent-3", 3, 1, 600),
    Workload("grouped-6", 6, 6, 200),
    Workload("independent-20", 20, 1, 4000),
    Workload("grouped-20", 20, 5, 800),
    Workload("grouped-200-prefix", 200, 5, 64, prefix=True),
    Workload("pairwise", 3, 1, 1200, kind="pairwise"),
    Workload("subset-retry", 20, 5, 1600, kind="retry"),
)


def scale_workload(
    *,
    responses: int = 5000,
    traits: int = 20,
    group_size: int = 5,
    concurrency: int = 4,
    response_bytes: int = 4096,
    variant: str = "default",
    extent: Literal["complete", "prefix", "prepare"] = "complete",
    completed_requests: int = 64,
) -> Workload:
    """An explicit scale case; historical cases above keep their original bytes."""
    if (
        min(
            responses,
            traits,
            group_size,
            concurrency,
            response_bytes,
            completed_requests,
        )
        < 1
    ):
        raise ValueError("scale dimensions must be positive")
    if variant not in {"default", "rich"} or extent not in {
        "complete",
        "prefix",
        "prepare",
    }:
        raise ValueError("unknown fixture variant or execution extent")
    physical = responses * ((traits + group_size - 1) // group_size)
    return Workload(
        "scale-rubric",
        traits,
        group_size,
        min(completed_requests, physical)
        if extent == "prefix"
        else physical
        if extent == "complete"
        else 0,
        prefix=extent == "prefix",
        responses=responses,
        concurrency=concurrency,
        response_bytes=response_bytes,
        variant=variant,
        scale=True,
    )


_TRAITS = (
    ("Claim", "State a clear, defensible central claim that responds to the question."),
    ("Evidence", "Select relevant concrete evidence and connect it to the claim."),
    (
        "Reasoning",
        "Explain why the evidence supports the conclusion without logical gaps.",
    ),
    (
        "Organization",
        "Arrange ideas in a sequence that helps a reader follow the argument.",
    ),
    (
        "Counterargument",
        "Represent a plausible alternative interpretation and respond fairly.",
    ),
    ("Precision", "Use precise terms and distinguish observation from inference."),
    ("Coherence", "Connect sentences and paragraphs with explicit relationships."),
    (
        "Context",
        "Explain the setting and assumptions needed to interpret the argument.",
    ),
    ("Relevance", "Keep examples and explanation focused on the assessment question."),
    ("Synthesis", "Combine evidence from different examples into a coherent account."),
    ("Qualification", "Acknowledge uncertainty and limits in the available evidence."),
    ("Detail", "Supply enough specific detail to make the reasoning assessable."),
    ("Comparison", "Explain meaningful similarities and differences between examples."),
    ("Interpretation", "Interpret the evidence consistently with its stated context."),
    (
        "Introduction",
        "Introduce the issue and establish a useful direction for discussion.",
    ),
    ("Conclusion", "Reach a supported conclusion that follows from the discussion."),
    ("Clarity", "Express ideas in readable sentences with clear referents."),
    (
        "Vocabulary",
        "Choose vocabulary appropriate to the subject and intended audience.",
    ),
    ("Mechanics", "Use sentence boundaries, spelling and punctuation consistently."),
    (
        "Audience",
        "Provide explanations appropriate for a reader unfamiliar with the examples.",
    ),
)


def _prose(length: int, identifier: str) -> str:
    paragraph = (
        f"Synthetic response {identifier}. A class investigated how a community garden "
        "could improve access to fresh food. Students compared observations from two "
        "neighborhoods, recorded the limits of their sample, and explained an alternative "
        "interpretation. Their recommendation connects the evidence to a practical action. "
    )
    return (paragraph * (length // len(paragraph) + 1))[:length]


def inputs(directory: Path, workload: Workload, capture: str) -> Path:
    """Restore an empty run with fixed source content, seed and character IDs."""
    directory.mkdir(parents=True)
    response = directory / "responses.jsonl"
    with response.open("w", encoding="utf-8", newline="") as stream:
        for index in range(workload.responses):
            stream.write(
                json.dumps(
                    {
                        "item_id": f"{index:06d}",
                        "text": _prose(workload.response_bytes, f"{index:06d}")
                        if workload.scale
                        else "x" * 4096,
                    }
                )
                + "\n"
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
            "concurrency": workload.concurrency,
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
    if workload.scale:
        configuration["rubric"] = {
            "rubric_id": "001",
            "name": "Synthetic analytic writing rubric",
            "traits": [
                {
                    "trait_id": f"{trait:03d}",
                    "name": _TRAITS[trait % len(_TRAITS)][0]
                    + (
                        f" {trait // len(_TRAITS) + 1}" if trait >= len(_TRAITS) else ""
                    ),
                    "description": _TRAITS[trait % len(_TRAITS)][1]
                    * (8 if workload.variant == "rich" else 1),
                    "levels": [
                        {
                            "label": label,
                            "numeric_value": code,
                            "description": description,
                        }
                        for label, code, description in (
                            (
                                "emerging",
                                0,
                                "The feature is absent or not yet supported by relevant detail.",
                            ),
                            (
                                "developing",
                                1,
                                "The feature is recognizable but uneven or only partly supported.",
                            ),
                            (
                                "proficient",
                                3,
                                "The feature is clear, relevant and consistently supported.",
                            ),
                            (
                                "advanced",
                                7,
                                "The feature is precise, sustained and thoughtfully qualified.",
                            ),
                        )
                    ],
                }
                for trait in range(workload.traits)
            ],
        }
        configuration["exemplars"] = [
            {
                "exemplar_id": f"{index:03d}",
                "text": _prose(
                    4096 if workload.variant == "rich" else 1024, f"anchor-{index:03d}"
                ),
                "scores": {
                    f"{trait:03d}": ("emerging", "developing", "advanced")[index % 3]
                    for trait in range(workload.traits)
                },
            }
            for index in range(20 if workload.variant == "rich" else 3)
        ]
    path = directory / "run.json"
    path.write_text(json.dumps(configuration), encoding="utf-8")
    return path
