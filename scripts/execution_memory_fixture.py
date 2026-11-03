"""Measure a bounded live-execution prefix after source/config initialization.

The required small regression exercises the actual executor, response receipts
and durable results. It leaves later scale qualification at its approved phase.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import time
import tracemalloc
from collections.abc import Coroutine, Generator
from pathlib import Path
from types import CoroutineType
from typing import Any
from unittest.mock import patch

from preparation_memory_fixture import native_peak_bytes

from judgmentllm.config import RunSpec
from judgmentllm.execution import execute_run
from judgmentllm.execution.exports import export_run
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.mock import MockAdapter


class SampleComplete(Exception):
    """Deliberately stop the measured prefix after its durable result commit."""


def measure(
    directory: Path,
    responses: int,
    traits: int,
    concurrency: int = 4,
    sample: int = 64,
    group_size: int = 1,
) -> dict[str, int | float]:
    directory.mkdir(parents=True)
    source = directory / "responses.jsonl"
    with source.open("w", encoding="utf-8", newline="") as stream:
        for index in range(responses):
            stream.write(
                json.dumps({"item_id": f"{index:06d}", "text": "x" * 4096}) + "\n"
            )
    spec = RunSpec.model_validate(
        {
            "task_kind": "rubric",
            "input": source,
            "capture": "minimal",
            "provider": "mock",
            "endpoint": "synthetic",
            "model": "fixture",
            "execution": {
                "concurrency": concurrency,
                "grouping": "auto" if group_size > 1 else "independent",
                "max_traits_per_request": group_size,
            },
            "rubric": {
                "rubric_id": "001",
                "name": "Synthetic execution memory fixture",
                "traits": [
                    {
                        "trait_id": f"{index:03d}",
                        "name": "Quality",
                        "description": "Synthetic assessment.",
                        "levels": [{"label": "low"}, {"label": "high"}],
                    }
                    for index in range(traits)
                ],
            },
        }
    )
    store = directory / "runtime"
    adapter = MockAdapter(directory / "service.sqlite3")
    with prepare_run(
        spec, store=store, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass
    completed = created_tasks = execution_tasks = live_tasks = peak_tasks = 0
    native_baseline = 0
    sample = min(sample, responses * ((traits + group_size - 1) // group_size))
    queue_high_water: dict[int, int] = {}

    class MeasuredQueue(asyncio.Queue[Any]):
        """Measure actual scheduler buffers without changing queue capacity."""

        async def put(self, item: Any) -> None:
            await super().put(item)
            queue_high_water[self.maxsize] = max(
                queue_high_water.get(self.maxsize, 0), self.qsize()
            )

    def fault(stage: str, request_uid: str) -> None:
        nonlocal completed, native_baseline
        if stage == "before_request_creation" and not tracemalloc.is_tracing():
            # Source indexes and the compatible session/configuration are ready;
            # only execution growth contributes to the Python measurement.
            gc.collect()
            native_baseline = native_peak_bytes()
            tracemalloc.start()
        if stage == "after_result":
            completed += 1
            if completed >= sample:
                raise SampleComplete

    async def execute() -> None:
        loop = asyncio.get_running_loop()

        def factory(
            loop: asyncio.AbstractEventLoop,
            coroutine: Coroutine[Any, Any, Any] | Generator[Any, None, Any],
            **kwargs: Any,
        ) -> asyncio.Task[Any]:
            nonlocal created_tasks, execution_tasks, live_tasks, peak_tasks
            created_tasks += 1
            if isinstance(
                coroutine, CoroutineType
            ) and coroutine.cr_code.co_qualname.startswith("execute_run.<locals>."):
                execution_tasks += 1
            live_tasks += 1
            peak_tasks = max(peak_tasks, live_tasks)
            task = asyncio.Task(coroutine, loop=loop, **kwargs)

            def finished(task: asyncio.Task[Any]) -> None:
                nonlocal live_tasks
                live_tasks -= 1

            task.add_done_callback(finished)
            return task

        loop.set_task_factory(factory)
        try:
            try:
                with patch("judgmentllm.execution.core.asyncio.Queue", MeasuredQueue):
                    await execute_run(
                        "001", store=store, adapter=adapter, spec=spec, fault=fault
                    )
            except* SampleComplete:
                pass
        finally:
            loop.set_task_factory(None)

    started = time.monotonic()
    try:
        asyncio.run(execute())
        assert completed == sample
        python_live, python_peak = tracemalloc.get_traced_memory()
        gc.collect()
        python_after_gc = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    with Repository(store) as repository:
        summary = repository.summary("001")
        assert sample <= summary.results <= sample * group_size
        request_count = repository.connection.execute(
            "SELECT COUNT(*) FROM requests WHERE run_id='001'"
        ).fetchone()[0]
    tracemalloc.start()
    try:
        exported = export_run("001", store=store, page_size=8)
        export_peak = tracemalloc.get_traced_memory()[1]
        assert exported.row_count == summary.results
    finally:
        tracemalloc.stop()
    return {
        "response_count": responses,
        "trait_count": traits,
        "logical_task_count": responses * traits,
        "sample_count": completed,
        "result_count": summary.results,
        "group_size": group_size,
        "concurrency": concurrency,
        "created_asyncio_tasks": created_tasks,
        "execution_asyncio_tasks": execution_tasks,
        "peak_asyncio_tasks": peak_tasks,
        "prepared_count": summary.tasks,
        "prepared_request_count": request_count,
        "request_queue_high_water": queue_high_water.get(2 * concurrency, 0),
        "result_queue_high_water": queue_high_water.get(concurrency, 0),
        "acceptance_count": adapter.ledger.acceptance_count(),
        "source_byte_length": source.stat().st_size,
        "python_peak_bytes": python_peak,
        "python_live_bytes": python_live,
        "python_after_gc_bytes": python_after_gc,
        "export_python_peak_bytes": export_peak,
        "export_row_count": exported.row_count,
        "native_baseline_bytes": native_baseline,
        "native_peak_bytes": native_peak_bytes(),
        "elapsed_seconds": time.monotonic() - started,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--responses", type=int, required=True)
    parser.add_argument("--traits", type=int, required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--sample", type=int, default=64)
    parser.add_argument("--group-size", type=int, default=1)
    options = parser.parse_args()
    if (
        min(
            options.responses,
            options.traits,
            options.concurrency,
            options.sample,
            options.group_size,
        )
        < 1
    ):
        parser.error("counts must be positive")
    measurement = measure(
        options.directory,
        options.responses,
        options.traits,
        options.concurrency,
        options.sample,
        options.group_size,
    )
    encoded = json.dumps(measurement)
    (options.directory / "measurement.json").write_text(encoded, encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
