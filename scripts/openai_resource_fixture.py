"""Bound actual SDK serialization, HTTP handoff and durable provider workloads."""

from __future__ import annotations

import argparse
import asyncio
import gc
import importlib
import json
import time
import tracemalloc
from pathlib import Path

from openai_offline_fixture import FixtureTransport, offline, specification
from preparation_memory_fixture import native_peak_bytes

from judgmentllm.execution import execute_run
from judgmentllm.preparation import prepare_run
from judgmentllm.production_transport import production_context


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument(
        "--endpoint", choices=("responses", "chat.completions"), required=True
    )
    parser.add_argument("--kind", choices=("rubric", "pairwise"), required=True)
    options = parser.parse_args()
    endpoint = "responses" if options.endpoint == "responses" else "chat.completions"
    kind = "rubric" if options.kind == "rubric" else "pairwise"
    spec = specification(
        options.directory / "inputs", endpoint, kind, count=64, traits=5
    )
    transport = FixtureTransport(endpoint)
    adapter = transport.adapter(spec)
    store = options.directory / "store"
    # Import the pinned SDK before measuring ongoing lifecycle allocation.
    importlib.import_module("openai")

    gc.collect()
    tracemalloc.start()
    started = time.monotonic()
    # Windows creates its private loopback self-pipe while entering Runner.
    # Initialize it before the full socket/DNS denial guard; all assessment
    # preparation, SDK calls and client cleanup still run under that guard.
    with asyncio.Runner() as runner:
        with offline():
            with prepare_run(
                spec,
                store=store,
                run_id="001",
                capabilities=adapter.capabilities(spec),
                transport_context=production_context(spec),
            ):
                pass

            async def execute() -> dict[str, int | float]:
                try:
                    report = await execute_run(
                        "001", store=store, adapter=adapter, spec=spec
                    )
                    return {
                        "calls": adapter.actual_call_count,
                        "serialization_seconds": adapter.serialization_seconds,
                        "worker_count": report.worker_count,
                        "request_queue_high_water": report.request_queue_high_water,
                        "result_queue_high_water": report.result_queue_high_water,
                        "successful_results": report.summary.successful_results,
                    }
                finally:
                    await adapter.aclose()

            result = runner.run(execute())
    _, peak = tracemalloc.get_traced_memory()
    gc.collect()
    remaining = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    result.update(
        python_peak_bytes=peak,
        python_after_gc_bytes=remaining,
        native_peak_bytes=native_peak_bytes(),
        elapsed_seconds=time.monotonic() - started,
    )
    assert transport.calls == result["calls"] == 64
    print(json.dumps(result))


if __name__ == "__main__":
    main()
