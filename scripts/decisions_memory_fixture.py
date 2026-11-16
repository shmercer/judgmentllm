"""Fresh-process bounds for indexed native sources and the grouped lifecycle."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import ExitStack
from pathlib import Path

from preparation_memory_fixture import native_peak_bytes

from judgmentllm.config import RunSpecV4
from judgmentllm.decisions.provenance import export_decisions_provenance
from judgmentllm.execution import execute_run
from judgmentllm.persistence.models import RunRecord
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.presentations import iter_presentations
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import _memory_source  # pyright: ignore[reportPrivateUsage]
from judgmentllm.preparation.capture import SourceBuilder
from judgmentllm.preparation.decisions import index_presentations, prepare_decisions_run
from judgmentllm.preparation.index import read_record
from judgmentllm.providers.decisions_mock import MockDecisionsAdapter


def checkpoint(_stage: str, _source: str | None) -> None:
    pass


def index(root: Path) -> dict[str, str | int]:
    """Measure the complete 100001-source ingest/membership boundary, no calls."""
    responses = root / "responses.jsonl"
    responses.write_text(
        '{"item_id":"001","text":"Synthetic one."}\n{"item_id":"002","text":"Synthetic two."}\n',
        encoding="utf-8",
    )
    source = root / "presentations.jsonl"
    with source.open("w", encoding="utf-8") as stream:
        for i in range(100_001):
            stream.write(
                json.dumps(
                    dict(
                        source_presentation_id=f"{i:06d}",
                        pair_id=f"{i:06d}",
                        left_item_id="001",
                        right_item_id="002",
                        members=[dict(pair_uid=f"member-{i:06d}", trait_id="01")],
                    )
                )
                + "\n"
            )
    spec = RunSpecV4.model_validate(
        dict(
            schema_version=4,
            task_kind="pairwise",
            input=responses,
            pair_presentations=source,
            provider="mock",
            endpoint="synthetic",
            model="synthetic",
            capture="minimal",
            criteria=[dict(trait_id="01", name="Quality", description="Compare.")],
        )
    )
    with Repository(root / "store") as repository:
        repository.create_run(
            RunRecord(
                run_id="001",
                task_kind="pairwise",
                provider="mock",
                endpoint="synthetic",
                model="synthetic",
                capture="minimal",
            ),
            decisions_version=4,
        )
        with ExecutorOwnership(repository, "001") as owner, ExitStack() as stack:
            assert owner.owner is not None
            builder = SourceBuilder(
                repository,
                "001",
                "minimal",
                owner.owner.token,
                stack,
                checkpoint=checkpoint,
                memory_source=_memory_source,
                record_reader=read_record,
            )
            builder.index(spec.input, "response")
            index_presentations(builder, spec)
            sources = builder.finish()
        count = sum(1 for _ in iter_presentations(repository, "001"))
        members = repository.connection.execute(
            "SELECT count(*) FROM presentation_source_members"
        ).fetchone()[0]
        query = repository.connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM presentation_inputs WHERE run_id='001' AND ordinal>1000 ORDER BY ordinal LIMIT 128"
        ).fetchall()
        assert any("idx_presentation_inputs_ordered" in row[3] for row in query)
        assert count == members == 100_001 and sources[-1].record_count == count
        assert repository.summary("001").tasks == 0
        return dict(
            stage="indexed_sources_and_membership",
            sources=count,
            members=members,
            native_peak_bytes=native_peak_bytes(),
        )


async def lifecycle(root: Path) -> dict[str, str | int]:
    source = root / "responses.jsonl"
    with source.open("w", encoding="utf-8") as stream:
        for i in range(513):
            stream.write(
                json.dumps(
                    dict(item_id=f"{i:06d}", text="Synthetic bounded lifecycle essay.")
                )
                + "\n"
            )
    spec = RunSpecV4.model_validate(
        dict(
            schema_version=4,
            task_kind="rubric",
            input=source,
            provider="mock",
            endpoint="synthetic",
            model="synthetic",
            capture="minimal",
            execution=dict(
                concurrency=4,
                request_grouping=dict(mode="auto", max_questions_per_request=32),
            ),
            rubric=dict(
                rubric_id="001",
                name="Synthetic",
                traits=[
                    dict(
                        trait_id=f"{i:02d}",
                        name=f"Trait {i}",
                        description="Assess independently.",
                        levels=[dict(label="low"), dict(label="high")],
                    )
                    for i in range(13)
                ],
            ),
        )
    )
    store = root / "store"
    provider = MockDecisionsAdapter(store / "mock-service.sqlite3")
    with prepare_decisions_run(
        spec, store=store, run_id="001", context=provider.transport_context
    ):
        pass
    report = await execute_run("001", store=store, adapter=provider, spec=spec)
    bundle = export_decisions_provenance("001", store=store)
    assert report.summary.results == 513 * 13
    assert report.request_queue_high_water <= 8 and report.result_queue_high_water <= 4
    assert [f.rows for f in bundle.manifest.files] == [513 * 13, 513 * 13, 513, 513]
    assert provider.forward_entries == 513
    return dict(
        stage="grouped_queues_and_export_joins",
        sources=513,
        results=report.summary.results,
        request_queue_high_water=report.request_queue_high_water,
        result_queue_high_water=report.result_queue_high_water,
        native_peak_bytes=native_peak_bytes(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--stage", choices=("index", "lifecycle"), required=True)
    args = parser.parse_args()
    args.directory.mkdir(parents=True)
    result = (
        index(args.directory)
        if args.stage == "index"
        else asyncio.run(lifecycle(args.directory))
    )
    assert 0 < int(result["native_peak_bytes"]) < 256 * 1024**2
    print(json.dumps(result))


if __name__ == "__main__":
    main()
