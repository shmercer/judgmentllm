"""Measured SDK Batch workload; run only under the existing resource barrier."""

from __future__ import annotations

import argparse
import gc
import importlib
import json
import time
import tracemalloc
from pathlib import Path
from typing import Any

from openai_batch_fixture import Service, create, install
from preparation_memory_fixture import native_peak_bytes
from pytest import MonkeyPatch

from judgmentllm import collect_batch, poll_batch, prepare_batch, submit_batch
from judgmentllm.config import load_run_spec
from judgmentllm.persistence.repository import ReadOnlyRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument(
        "--endpoint", choices=("responses", "chat.completions"), required=True
    )
    parser.add_argument("--capture", choices=("full", "minimal"), required=True)
    options = parser.parse_args()
    options.directory.mkdir()
    create(options.directory)
    directory = options.directory / "openai-batch-responses-full"
    (directory / "responses.jsonl").write_text(
        "".join(
            json.dumps(dict(item_id=f"{index:03d}", text="Synthetic resource response"))
            + "\n"
            for index in range(64)
        )
    )
    config_path = directory / "config.json"
    data = json.loads(config_path.read_text())
    data.update(endpoint=options.endpoint, capture=options.capture)
    trait = data["rubric"]["traits"][0]
    data["rubric"]["traits"] = [
        {**trait, "trait_id": f"{index:02d}"} for index in range(5)
    ]
    config_path.write_text(json.dumps(data))
    spec = load_run_spec(config_path)
    service = Service(directory / "service.sqlite")
    importlib.import_module("openai")
    gc.collect()
    tracemalloc.start()
    started = time.monotonic()
    with MonkeyPatch.context() as patch:
        install(patch, service)
        store = directory / "store"
        prepare_batch(config_path, store=store, run_id="001")
        service.configure(store, spec)
        submit_batch("001", store=store)
        poll_batch("001", store=store)
        collect_batch("001", store=store)
        final = collect_batch("001", store=store)
        assert not final.batch.recovery_required
        with ReadOnlyRepository(store) as repository:
            summary = repository.summary("001")
            members = repository.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            assert summary.successful_results == 320 and members == 64
    _, peak = tracemalloc.get_traced_memory()
    gc.collect()
    remaining = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    measured: dict[str, Any] = dict(
        successful_results=320,
        members=64,
        mutation_forwards=len(
            [event for event in service.events() if event[0] == "POST"]
        ),
        python_peak_bytes=peak,
        python_after_gc_bytes=remaining,
        native_peak_bytes=native_peak_bytes(),
        elapsed_seconds=time.monotonic() - started,
    )
    print(json.dumps(measured))


if __name__ == "__main__":
    main()
