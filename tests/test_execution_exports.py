"""Immutable provider-free exports retain every recorded attempt and snapshot."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Literal

import pytest
from test_execution import interrupt, setup

from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.exports import export_run
from judgmentllm.io import read_results, read_results_csv
from judgmentllm.persistence.models import ArtifactIntegrityError
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers import MockAdapter, MockScenario
from judgmentllm.results import RubricResult


@pytest.mark.asyncio
@pytest.mark.parametrize("pairwise", [False, True])
@pytest.mark.parametrize(
    "format,view", [("jsonl", "canonical"), ("csv", "canonical"), ("csv", "r")]
)
async def test_export_uses_existing_serializers_and_exact_byte_identity(
    tmp_path: Path,
    pairwise: bool,
    format: Literal["jsonl", "csv"],
    view: Literal["canonical", "r"],
) -> None:
    spec, store, ledger = setup(tmp_path, pairwise=pairwise, responses=3)
    adapter = MockAdapter(ledger, scenario=MockScenario(probabilities=True))
    await execute_run("001", store=store, adapter=adapter, spec=spec)
    calls = adapter.submit_calls
    published = export_run("001", store=store, format=format, view=view, page_size=1)
    path = Path(published.artifact_path)
    raw = path.read_bytes()
    assert len(raw) == published.artifact.byte_length
    assert hashlib.sha256(raw).hexdigest() == published.artifact.sha256
    assert published.row_count == 3 and published.run_complete
    assert published.run_state == "completed"
    again = export_run("001", store=store, format=format, view=view, page_size=2)
    assert again == published and path.read_bytes() == raw
    assert adapter.submit_calls == calls
    with Repository(store) as repository:
        expected = list(repository.iter_results("001"))
        assert (
            repository.connection.execute("SELECT COUNT(*) FROM exports").fetchone()[0]
            == 1
        )
    if format == "jsonl":
        assert list(read_results(path)) == expected
    elif view == "canonical":
        assert list(read_results_csv(path)) == expected
    else:
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 3 and {row["status"] for row in rows} == {"complete"}
        assert {row["task_id"] for row in rows} == {row.task_id for row in expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["invalid", "refusal", "provider_error"])
async def test_unsuccessful_results_export_without_fabricating_other_work(
    tmp_path: Path, outcome: str
) -> None:
    spec, store, ledger = setup(tmp_path)
    empty = export_run("001", store=store)
    assert empty.row_count == 0 and not empty.run_complete
    assert (
        empty.run_state == "submitted" and Path(empty.artifact_path).read_bytes() == b""
    )
    await execute_run(
        "001",
        store=store,
        spec=spec,
        adapter=MockAdapter(
            ledger, scenario=MockScenario.model_validate({"outcome": outcome})
        ),
    )
    report = export_run("001", store=store)
    assert report.row_count == 1 and not report.run_complete
    assert report.export_id != empty.export_id
    assert Path(empty.artifact_path).read_bytes() == b""
    record = next(read_results(report.artifact_path))
    assert isinstance(record, RubricResult)
    assert record.status != "complete" and record.primary_score is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "after_export_snapshot",
        "during_export_page",
        "after_export_serialization",
        "after_export_artifact",
        "during_export_transaction",
        "after_export_commit",
    ],
)
async def test_interrupted_publication_never_exposes_partial_export(
    tmp_path: Path, stage: str
) -> None:
    spec, store, ledger = setup(tmp_path)
    await execute_run("001", store=store, adapter=MockAdapter(ledger), spec=spec)

    def fail(boundary: str) -> None:
        if boundary == stage:
            raise RuntimeError("synthetic interruption")

    with pytest.raises(RuntimeError, match="synthetic interruption"):
        export_run("001", store=store, fault=fail)
    with Repository(store) as repository:
        assert repository.connection.execute("SELECT COUNT(*) FROM exports").fetchone()[
            0
        ] == (stage == "after_export_commit")
    assert not list(store.rglob(".export-*"))
    published = export_run("001", store=store)
    assert published.row_count == 1


@pytest.mark.asyncio
async def test_concurrent_results_do_not_change_selected_snapshot(
    tmp_path: Path,
) -> None:
    spec, store, ledger = setup(tmp_path, responses=10)
    adapter = MockAdapter(ledger, scenario=MockScenario(delay_seconds=0.01))
    with pytest.raises(ExceptionGroup):
        await execute_run(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            fault=interrupt("after_result"),
        )
    entered, release = threading.Event(), threading.Event()

    def checkpoint(stage: str) -> None:
        if stage == "after_export_snapshot":
            entered.set()
            assert release.wait(20)

    exporting = asyncio.create_task(
        asyncio.to_thread(export_run, "001", store=store, fault=checkpoint, page_size=1)
    )
    assert await asyncio.to_thread(entered.wait, 10)
    try:
        await recover_run("001", store=store, adapter=MockAdapter(ledger), spec=spec)
    finally:
        release.set()
    old = await exporting
    original = Path(old.artifact_path).read_bytes()
    new = export_run("001", store=store, page_size=2)
    # A cancelled worker can leave a submission boundary that authoritative
    # reconciliation proves was never invoked; it has no invented result row.
    assert 1 <= old.row_count < new.row_count <= 10
    assert old.result_high_water_mark < new.result_high_water_mark
    assert old.export_id != new.export_id
    assert Path(old.artifact_path).read_bytes() == original
    replay = export_run("001", store=store, high_water_mark=old.result_high_water_mark)
    assert replay.export_id == old.export_id and replay.row_count == old.row_count


@pytest.mark.asyncio
async def test_corrupted_published_export_fails_closed(tmp_path: Path) -> None:
    spec, store, ledger = setup(tmp_path)
    await execute_run("001", store=store, adapter=MockAdapter(ledger), spec=spec)
    report = export_run("001", store=store)
    Path(report.artifact_path).write_bytes(b"corrupted")
    with pytest.raises(ArtifactIntegrityError):
        export_run("001", store=store)


@pytest.mark.parametrize(
    "options",
    [
        {"format": "xml"},
        {"view": "other"},
        {"view": "r"},
        {"page_size": 0},
        {"page_size": True},
        {"high_water_mark": True},
        {"high_water_mark": -1},
        {"high_water_mark": 100},
        {"na_string": ""},
        {"na_string": "bad\nmarker"},
        {"na_string": "missing"},
    ],
)
def test_invalid_export_options_are_rejected(
    tmp_path: Path, options: dict[str, object]
) -> None:
    _, store, _ = setup(tmp_path)
    with pytest.raises(ValueError):
        export_run("001", store=store, **options)  # pyright: ignore[reportArgumentType]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    [
        "after_export_snapshot",
        "after_export_serialization",
        "after_export_artifact",
        "during_export_transaction",
        "after_export_commit",
    ],
)
async def test_export_process_death_keeps_publication_atomic(
    tmp_path: Path, stage: str
) -> None:
    spec, store, ledger = setup(tmp_path)
    await execute_run("001", store=store, adapter=MockAdapter(ledger), spec=spec)
    code = """import os, sys
from judgmentllm.execution.exports import export_run
def fault(stage):
    if stage == sys.argv[2]:
        os._exit(73)
export_run('001', store=sys.argv[1], fault=fault)
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(store), stage], capture_output=True, timeout=20
    )
    assert process.returncode == 73 and not process.stdout and not process.stderr
    with Repository(store) as repository:
        assert repository.connection.execute("SELECT COUNT(*) FROM exports").fetchone()[
            0
        ] == (stage == "after_export_commit")
    # A separate reopening can safely publish/reuse the complete original bytes.
    recovered = subprocess.run(
        [
            sys.executable,
            "-c",
            "from judgmentllm.execution.exports import export_run; import sys; print(export_run('001', store=sys.argv[1]).model_dump_json())",
            str(store),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["row_count"] == 1


@pytest.mark.asyncio
async def test_concurrent_exporters_converge_on_one_publication(tmp_path: Path) -> None:
    spec, store, ledger = setup(tmp_path)
    await execute_run("001", store=store, adapter=MockAdapter(ledger), spec=spec)
    reports = await asyncio.gather(
        *(asyncio.to_thread(export_run, "001", store=store) for _ in range(4))
    )
    assert all(report == reports[0] for report in reports)
    with Repository(store) as repository:
        assert (
            repository.connection.execute("SELECT COUNT(*) FROM exports").fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_minimal_exports_retain_only_authorized_normalized_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec, _, ledger = setup(tmp_path, explanations=True)
    source, prompt, payload, raw, explanation, credential = (
        "EXPORT_SOURCE_PRIVATE_101",
        "EXPORT_PROMPT_PRIVATE_202",
        "EXPORT_PAYLOAD_PRIVATE_303",
        "EXPORT_RAW_PRIVATE_404",
        "EXPORT_EXPLANATION_AUTHORIZED_505",
        "EXPORT_CREDENTIAL_PRIVATE_606",
    )
    spec.input.path.write_text(
        json.dumps({"item_id": "000", "text": source}) + "\n", encoding="utf-8"
    )
    template = tmp_path / "inputs" / "export-template.txt"
    template.write_text(
        prompt
        + "\n{{ rubric }}\n{{ exemplars }}\n{{ response }}\n{{ output_instructions }}",
        encoding="utf-8",
    )
    spec.prompt.preset, spec.prompt.file = None, template
    store = tmp_path / "private-store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    await execute_run(
        "001",
        store=store,
        spec=spec,
        adapter=MockAdapter(
            ledger,
            scenario=MockScenario(
                payload_marker=payload, raw_marker=raw, explanation=explanation
            ),
        ),
    )
    combinations: tuple[
        tuple[Literal["jsonl", "csv"], Literal["canonical", "r"]], ...
    ] = (
        ("jsonl", "canonical"),
        ("csv", "canonical"),
        ("csv", "r"),
    )
    for format, view in combinations:
        published = export_run("001", store=store, format=format, view=view)
        assert explanation.encode() in Path(published.artifact_path).read_bytes()
    for path in (*store.rglob("*"), *ledger.parent.rglob("*")):
        if path.is_file():
            content = path.read_bytes()
            assert all(
                marker.encode() not in content
                for marker in (source, prompt, payload, raw, credential)
            ), path
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_empty_export_does_not_change_identical_source_artifact_metadata(
    tmp_path: Path, capture: Literal["full", "minimal"]
) -> None:
    _, store, _ = setup(tmp_path, capture=capture, responses=0)
    digest = hashlib.sha256(b"").hexdigest()
    with Repository(store) as repository:
        original = repository.get_artifact("001", digest)
    published = export_run("001", store=store)
    assert (
        published.row_count == 0 and Path(published.artifact_path).read_bytes() == b""
    )
    assert published.artifact.sha256 == digest and published.artifact.byte_length == 0
    assert published.artifact.media_type == "application/x-ndjson"
    assert published.artifact.relative_path is not None
    assert export_run("001", store=store) == published
    with Repository(store) as repository:
        assert repository.get_artifact("001", digest) == original
