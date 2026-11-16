"""Offline V4 fixture generation and independent-process exports for strict R."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from pathlib import Path
from typing import Literal

from judgmentllm.decisions.provenance import export_decisions_provenance
from judgmentllm.evidence import ReportedUsageInput
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.exports import export_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation.decisions import prepare_decisions_run
from judgmentllm.providers.decisions_mock import (
    DecisionsScenarioV1,
    MockDecisionsAdapter,
)
from judgmentllm.results import RubricResult

RUNS = tuple(
    (f"{i:03d}", kind, capture)
    for i, (kind, capture) in enumerate(
        (
            ("rubric", "full"),
            ("rubric", "minimal"),
            ("pairwise", "full"),
            ("pairwise", "minimal"),
            ("unknown", "full"),
            ("unknown", "minimal"),
            ("empty", "full"),
            ("empty", "minimal"),
        ),
        1,
    )
)


def scenario(kind: str, *, retry: bool = False) -> DecisionsScenarioV1:
    if kind == "unknown":
        return DecisionsScenarioV1(transport="timeout_after")
    usage = ReportedUsageInput.model_validate_json(
        '{"usage_source":"synthetic.reported_usage","input_tokens":10,"output_tokens":4,"reported_cost":{"amount":"0.125","currency":"USD","source":"synthetic.reported_cost"}}'
    )
    return DecisionsScenarioV1.model_validate(
        dict(
            usage=usage.model_dump(),
            members={}
            if retry
            else {
                "02": dict(outcome="refusal"),
                "03": dict(outcome="missing" if kind == "rubric" else "no_selection"),
            },
        )
    )


def source(root: Path, kind: str, capture: str) -> Path:
    root.mkdir(parents=True)
    (root / "responses.jsonl").write_text(
        '{"item_id":"001","text":"Synthetic café first essay."}\n{"item_id":"002","text":"Synthetic second essay."}\n',
        encoding="utf-8",
    )
    spec: dict[str, object] = dict(
        schema_version=4,
        task_kind="pairwise" if kind == "pairwise" else "rubric",
        input="responses.jsonl",
        provider="mock",
        endpoint="synthetic",
        model="offline-decisions-v1",
        capture=capture,
        execution=dict(
            concurrency=1,
            request_grouping=dict(mode="auto", max_questions_per_request=32),
        ),
    )
    if kind == "pairwise":
        spec["criteria"] = [
            dict(
                trait_id=f"{i:02d}",
                name=f"Criterion {i}",
                description=f"Compare independently using criterion {i}.",
            )
            for i in range(1, 4)
        ]
        (root / "presentations.jsonl").write_text(
            json.dumps(
                dict(
                    source_presentation_id="comparison-001",
                    pair_id="pair-001",
                    left_item_id="001",
                    right_item_id="002",
                    members=[
                        dict(pair_uid=f"criterion-{i:02d}", trait_id=f"{i:02d}")
                        for i in range(1, 4)
                    ],
                    schedule=dict(repetitions=2, include_reversals=True, seed=41),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        spec["pair_presentations"] = "presentations.jsonl"
    else:
        spec["rubric"] = dict(
            rubric_id="001",
            name="Independent criteria",
            traits=[
                dict(
                    trait_id=f"{i:02d}",
                    name=f"Trait {i}",
                    description=f"Assess trait {i} independently.",
                    levels=[
                        dict(
                            label=f"{index:02d}",
                            description=f"Level {index}",
                            numeric_value=code,
                        )
                        for index, code in enumerate((1, 2, 7))
                    ],
                )
                for i in range(1, 2 if kind == "unknown" else 4)
            ],
        )
        if kind == "unknown":
            (root / "responses.jsonl").write_text(
                '{"item_id":"001","text":"Synthetic unknown usage."}\n',
                encoding="utf-8",
            )
        elif kind == "empty":
            (root / "responses.jsonl").write_bytes(b"")
    path = root / "config.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return path


def adapter(root: Path, kind: str, *, retry: bool = False) -> MockDecisionsAdapter:
    return MockDecisionsAdapter(
        root / "service.sqlite3",
        scenario=scenario(kind, retry=retry),
        retrieval_available=kind != "unknown",
        reconciliation_available=kind != "unknown",
    )


async def persist(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    store = root / "store"
    for run_id, kind, capture in RUNS:
        config = source(root / "inputs" / run_id, kind, capture)
        provider = adapter(root, kind)
        with prepare_decisions_run(
            config, store=store, run_id=run_id, context=provider.transport_context
        ):
            pass
        await execute_run(run_id, store=store, adapter=provider)
        if kind == "rubric":
            with Repository(store) as repository:
                failed = next(
                    r
                    for r in repository.iter_results(run_id)
                    if isinstance(r, RubricResult)
                    and r.item_id == "001"
                    and r.trait_id == "03"
                )
            retry_adapter = adapter(root, kind, retry=True)
            first = authorize_subset_retry(
                run_id,
                store=store,
                adapter=retry_adapter,
                task_ids=(failed.task_id,),
                operation_id="retry-001",
            )
            repeated = authorize_subset_retry(
                run_id,
                store=store,
                adapter=retry_adapter,
                task_ids=(failed.task_id,),
                operation_id="retry-001",
            )
            assert first == repeated
            await recover_run(run_id, store=store, adapter=retry_adapter)


async def export(root: Path) -> None:
    store = root / "store"
    rows: list[dict[str, object]] = []
    for run_id, kind, capture in RUNS:
        provider = adapter(root, kind)
        await recover_run(run_id, store=store, adapter=provider)
        assert provider.submit_calls == provider.forward_entries == 0
        with Repository(store) as repository:
            empty_source = (
                repository.get_artifact(run_id, bytes_digest(b""))
                if kind == "empty"
                else None
            )
        bundle = export_decisions_provenance(run_id, store=store)
        assert bundle == export_decisions_provenance(run_id, store=store)
        directory = Path(bundle.manifest_path).parents[1]
        for companion in bundle.manifest.files:
            assert companion.artifact.relative_path is not None
            shutil.copyfile(
                directory / companion.artifact.relative_path,
                root / f"{run_id}-{companion.name}",
            )
        combinations: tuple[
            tuple[Literal["jsonl", "csv"], Literal["canonical", "r"], str], ...
        ] = (
            ("jsonl", "canonical", "jsonl"),
            ("csv", "canonical", "csv"),
            ("csv", "r", "r.csv"),
        )
        for format_, view, suffix in combinations:
            report = export_run(run_id, store=store, format=format_, view=view)
            shutil.copyfile(report.artifact_path, root / f"{run_id}-results.{suffix}")
        if empty_source is not None:
            with Repository(store) as repository:
                assert (
                    repository.get_artifact(run_id, empty_source.sha256) == empty_source
                )
                assert (empty_source.relative_path is None) == (capture == "minimal")
        rows.append(
            dict(
                run_id=run_id,
                kind=kind,
                capture=capture,
                bundle=bundle.model_dump(mode="json"),
            )
        )
    (root / "decisions.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("persist", "export"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(
        persist(args.output) if args.phase == "persist" else export(args.output)
    )


if __name__ == "__main__":
    main()
