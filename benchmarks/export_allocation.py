"""Focused fresh-process allocation evidence over already completed results.

No execution or resumption occurs. Component mode reuses production readers,
serializers, staging/fsync and immutable artifact publication in empty scratch.
Service mode runs one real uncached R export on an unchanged scratch store copy
with a new NA marker, measuring the shared metadata branch separately.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import tracemalloc
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any, Literal, cast

from execution_worker import native_peak_bytes
from provenance import host_identity, runtime_identity, source_fingerprint

import judgmentllm
from judgmentllm.execution import exports
from judgmentllm.io import Result, export_results_csv, write_results_jsonl
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import semantic_digest
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.r_exports import export_rubric_r_csv

MIB = 1024**2


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def identity(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def component(
    store: Path, output: Path, original: dict[str, Any], run_id: str, page_size: int
) -> dict[str, Any]:
    """Measure real uncached serialization and ArtifactStore publication."""
    started, cpu = time.perf_counter(), time.process_time()
    gc.collect()
    tracemalloc.start()
    try:
        with ReadOnlyRepository(store) as repository:
            run = repository.get_run(run_id)
            if run.task_kind != "rubric":
                raise ValueError("this focused fixture requires rubric results")
            selected = original["result_high_water_mark"]
            expected = repository.connection.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=? AND result_seq<=?",
                (run_id, selected),
            ).fetchone()[0]
            artifact_store = ArtifactStore(output / "artifacts")

            def records() -> Iterator[Result]:
                for _number, record in enumerate(
                    repository.iter_results(
                        run_id, page_size=page_size, high_water_mark=selected
                    )
                ):
                    yield record

            with tempfile.TemporaryDirectory(
                prefix=".export-", dir=artifact_store.root
            ) as temporary:
                path = Path(temporary) / "results"
                if original["format"] == "jsonl":
                    count = write_results_jsonl(path, records())
                elif original["view"] == "canonical":
                    count = export_results_csv(path, records())
                else:
                    count = export_rubric_r_csv(path, exports._rubric(records()))  # pyright: ignore[reportPrivateUsage]
                if count != expected or count != original["rows"]:
                    raise IntegrityError(
                        "scratch export row count differs from original snapshot"
                    )
                with path.open("r+b") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                artifact = artifact_store.put_stream(
                    exports._chunks(path),  # pyright: ignore[reportPrivateUsage]
                    "application/x-ndjson"
                    if original["format"] == "jsonl"
                    else "text/csv",
                )
            component_live_before_metadata = tracemalloc.get_traced_memory()[0]
            repository_open_at_boundary = not repository.connection.in_transaction
            published = artifact_store.verify(artifact)
            if (
                artifact.sha256 != original["sha256"]
                or artifact.byte_length != original["bytes"]
            ):
                raise IntegrityError(
                    "scratch export bytes differ from recorded original export"
                )
        live, peak = tracemalloc.get_traced_memory()
        gc.collect()
        retained = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    return {
        "format": original["format"],
        "view": original["view"],
        "rows": count,
        "result_high_water_mark": selected,
        "sha256": artifact.sha256,
        "bytes": artifact.byte_length,
        "artifact_path": str(published),
        "recorded_original_hash_match": True,
        "python_peak_bytes": peak,
        "python_live_bytes": live,
        "python_retained_bytes": retained,
        "component_live_before_metadata_bytes": component_live_before_metadata,
        "component_repository_open_at_boundary": repository_open_at_boundary,
        "component_boundary": "after_artifact_publication_and_temp_cleanup_before_final_verification_repository_close_or_gc",
        "component_below_8_mib": peak < 8 * MIB,
        "diagnostic_wall_seconds": time.perf_counter() - started,
        "diagnostic_cpu_seconds": time.process_time() - cpu,
        "scope": "uncached production reader/serializers/fsync/ArtifactStore publication; excludes exports-table metadata transaction",
    }


def scratch_copy(store: Path, destination: Path) -> None:
    """Copy closed artifacts unchanged; SQLite backup preserves all old exports."""
    shutil.copytree(
        store,
        destination,
        ignore=lambda _directory, names: [
            name for name in names if name.startswith("registry.sqlite3")
        ],
    )
    with (
        ReadOnlyRepository(store) as original,
        closing(sqlite3.connect(destination / "registry.sqlite3")) as target,
    ):
        original.connection.backup(target)


def service(
    store: Path,
    output: Path,
    original: dict[str, Any],
    run_id: str,
    page_size: int,
    na_string: str,
) -> dict[str, Any]:
    """Measure a genuine uncached shared export service, including metadata."""
    copied = output / "store-copy"
    scratch_copy(store, copied)
    export_id = semantic_digest(
        "result-export",
        [run_id, "csv", "r", original["result_high_water_mark"], na_string],
    )
    with ReadOnlyRepository(copied) as repository:
        if (
            repository.connection.execute(
                "SELECT 1 FROM exports WHERE run_id=? AND export_id=?",
                (run_id, export_id),
            ).fetchone()
            is not None
        ):
            raise ValueError(
                "new NA marker must create a genuinely uncached export identity"
            )
        old_exports = [
            tuple(row)
            for row in repository.connection.execute(
                "SELECT export_id,artifact_sha256,row_count FROM exports WHERE run_id=? ORDER BY export_id",
                (run_id,),
            )
        ]
    phases: dict[str, int] = {}

    def fault(stage: str) -> None:
        if stage == "after_export_snapshot":
            phases["pre_serialization_live_bytes"] = tracemalloc.get_traced_memory()[0]
        elif (
            stage == "during_export_page"
            and "first_export_page_live_bytes" not in phases
        ):
            phases["first_export_page_live_bytes"] = tracemalloc.get_traced_memory()[0]
        elif stage == "after_export_artifact":
            current, peak = tracemalloc.get_traced_memory()
            phases["pre_metadata_live_bytes"] = current
            phases["through_artifact_peak_bytes"] = peak
            tracemalloc.stop()
            tracemalloc.start()

    gc.collect()
    started, cpu = time.perf_counter(), time.process_time()
    tracemalloc.start()
    try:
        report = exports.export_run(
            run_id,
            store=copied,
            format="csv",
            view="r",
            high_water_mark=original["result_high_water_mark"],
            page_size=page_size,
            na_string=na_string,
            fault=fault,
        )
        metadata_live, metadata_peak = tracemalloc.get_traced_memory()
        gc.collect()
        metadata_retained = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    if report.row_count != original["rows"] or "pre_metadata_live_bytes" not in phases:
        raise IntegrityError(
            "real uncached service did not serialize the complete snapshot"
        )
    with ReadOnlyRepository(copied) as repository:
        current_exports = [
            tuple(row)
            for row in repository.connection.execute(
                "SELECT export_id,artifact_sha256,row_count FROM exports WHERE run_id=? AND export_id<>? ORDER BY export_id",
                (run_id, export_id),
            )
        ]
        if current_exports != old_exports:
            raise IntegrityError("scratch service changed prior immutable exports")
    # The first callback occurs before the first result is yielded to the
    # serializer. It includes fixed invocation/staging context and the first
    # bounded SQLite page; later serializer/cache survivors belong to component
    # peak and must not be added again as supposedly fixed pre-metadata context.
    fixed_context = phases["first_export_page_live_bytes"]
    service_bound = max(
        phases["through_artifact_peak_bytes"],
        phases["pre_metadata_live_bytes"] + metadata_peak,
    )
    return {
        "format": "csv",
        "view": "r",
        "rows": report.row_count,
        "na_string": na_string,
        "result_high_water_mark": report.result_high_water_mark,
        "new_export_id": report.export_id,
        "sha256": report.artifact.sha256,
        "bytes": report.artifact.byte_length,
        "artifact_path": report.artifact_path,
        "old_exports_unchanged": True,
        "old_export_count": len(old_exports),
        **phases,
        "fixed_context_live_bytes": fixed_context,
        "fixed_context_measurement_stage": "first during_export_page before first result yield",
        "metadata_trace_start_stage": "after_export_artifact",
        "metadata_new_peak_bytes": metadata_peak,
        "metadata_new_live_bytes": metadata_live,
        "metadata_new_retained_bytes": metadata_retained,
        "conservative_service_peak_bytes": service_bound,
        "service_below_8_mib_proof": service_bound < 8 * MIB,
        "diagnostic_wall_seconds": time.perf_counter() - started,
        "diagnostic_cpu_seconds": time.process_time() - cpu,
        "scope": "real uncached export_run R CSV on unchanged scratch store copy; new valid NA marker creates new export_id; metadata trace begins after artifact publication",
    }


def run(
    sample_path: Path,
    output: Path,
    *,
    kind: str,
    format: str,
    view: str,
    expected_rows: int = 100000,
    run_id: str = "001",
    page_size: int = 128,
    na_string: str = "ALLOCATION_NA",
    metadata_report: Path | None = None,
) -> dict[str, Any]:
    if (
        page_size < 1
        or expected_rows < 1
        or (format == "jsonl" and view != "canonical")
    ):
        raise ValueError("invalid allocation fixture parameters")
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    if (
        sample.get("status") != "passed"
        or sample.get("verification_complete") is not True
        or sample.get("extent") != "complete"
        or "derived_audit_r_path_map" in sample
    ):
        raise ValueError(
            "require an original independently verified complete producer report"
        )
    source = Path(judgmentllm.__file__ or "").resolve().parents[2]
    store = Path(sample["working_directory"]) / "store"
    if output.resolve().is_relative_to(
        store.resolve()
    ) or output.resolve().is_relative_to(source):
        raise ValueError("scratch output must be outside the source and original store")
    original = next(
        row
        for row in sample["export_files"]
        if row["format"] == format and row["view"] == view
    )
    if original["rows"] != expected_rows:
        raise ValueError(
            "original export row count differs from explicit expected fixture size"
        )
    if kind == "service" and (format != "csv" or view != "r" or na_string == "NA"):
        raise ValueError("service probe requires R CSV and a new valid NA marker")
    database = store / "registry.sqlite3"
    before = identity(database)
    with ReadOnlyRepository(store) as repository:
        manifest = repository.run_directory(run_id) / "manifest.json"
    manifest_before = digest(manifest)
    output.mkdir(parents=True, exist_ok=False)
    result = (
        component(store, output, original, run_id, page_size)
        if kind == "component"
        else service(store, output, original, run_id, page_size, na_string)
    )
    if identity(database) != before or digest(manifest) != manifest_before:
        raise IntegrityError("original registry or manifest identity changed")
    result.update(
        status="passed",
        kind=kind,
        capture=sample["capture"],
        run_id=run_id,
        page_size=page_size,
        expected_rows=expected_rows,
        native_peak_bytes=native_peak_bytes(),
        original_registry_before=before,
        original_registry_after=identity(database),
        original_manifest_sha256=manifest_before,
        original_input_report=str(sample_path.resolve()),
        original_input_report_sha256=digest(sample_path),
        original_producer_commit=sample.get("host_identity", {}).get("git_commit"),
        original_producer_source_sha256=sample.get("source_tree_sha256"),
        original_producer_harness_sha256=sample.get("harness_sha256"),
        source_tree_sha256=source_fingerprint(source),
        runtime_identity=runtime_identity(),
        host_identity=host_identity(source),
        tool_sha256=digest(Path(__file__)),
        diagnostic_only=True,
    )
    # Include provenance/identity collection in the process-lifetime RSS bound.
    result["native_peak_bytes"] = native_peak_bytes()
    if result["native_peak_bytes"] >= 256 * MIB:
        result.update(status="failed", failure_kind="native_peak_exceeds_256_mib")
    if kind == "component" and not result["component_below_8_mib"]:
        result.update(status="failed", failure_kind="component_peak_exceeds_8_mib")
    if kind == "service" and not result["service_below_8_mib_proof"]:
        result.update(status="failed", failure_kind="conservative_service_envelope_gap")
    if metadata_report is not None:
        measured = json.loads(metadata_report.read_text(encoding="utf-8"))
        if (
            kind != "component"
            or measured.get("kind") != "service"
            or measured.get("status") != "passed"
            or any(
                measured.get(key) != result[key]
                for key in (
                    "original_input_report_sha256",
                    "source_tree_sha256",
                    "runtime_identity",
                )
            )
        ):
            raise ValueError(
                "metadata evidence must match source, runtime and original producer report"
            )
        simple_sum = (
            result["python_peak_bytes"]
            + measured["fixed_context_live_bytes"]
            + measured["metadata_new_peak_bytes"]
        )
        serialization_bound = (
            result["python_peak_bytes"] + measured["fixed_context_live_bytes"]
        )
        metadata_bound = (
            result["component_live_before_metadata_bytes"]
            + measured["fixed_context_live_bytes"]
            + measured["metadata_new_peak_bytes"]
        )
        bound = max(serialization_bound, metadata_bound)
        result["conservative_complete_export_envelope"] = {
            "component_peak_bytes": result["python_peak_bytes"],
            "fixed_context_live_bytes": measured["fixed_context_live_bytes"],
            "metadata_new_peak_bytes": measured["metadata_new_peak_bytes"],
            "component_live_before_metadata_bytes": result[
                "component_live_before_metadata_bytes"
            ],
            "simple_conservative_sum_bytes": simple_sum,
            "serialization_publication_bound_bytes": serialization_bound,
            "metadata_phase_bound_bytes": metadata_bound,
            "phase_union_bound_bytes": bound,
            "below_8_mib_proof": bound < 8 * MIB,
            "metadata_report_sha256": digest(metadata_report),
            "interpretation": "Maximum of measured serialization/publication and later metadata bounds for the shared typed branch. Component boundary is inside the open reader after artifact publication/temp cleanup, before verification/close/GC. The simpler sum is explanatory only. False is an envelope gap; no baseline subtraction or invented reserve.",
        }
        if bound >= 8 * MIB:
            result.update(
                status="failed",
                failure_kind="conservative_complete_export_envelope_gap",
            )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kind", choices=("component", "service"), default="component")
    parser.add_argument("--format", choices=("jsonl", "csv"), required=True)
    parser.add_argument("--view", choices=("canonical", "r"), default="canonical")
    parser.add_argument("--expected-rows", type=int, default=100000)
    parser.add_argument("--run-id", default="001")
    parser.add_argument("--page-size", type=int, default=128)
    parser.add_argument("--na-string", default="ALLOCATION_NA")
    parser.add_argument("--metadata-report", type=Path)
    options = parser.parse_args()
    try:
        result = run(
            options.sample,
            options.output,
            kind=options.kind,
            format=cast(Literal["jsonl", "csv"], options.format),
            view=cast(Literal["canonical", "r"], options.view),
            expected_rows=options.expected_rows,
            run_id=options.run_id,
            page_size=options.page_size,
            na_string=options.na_string,
            metadata_report=options.metadata_report,
        )
    except Exception as error:
        result = {
            "status": "failed",
            "failure_type": type(error).__name__,
            "diagnostic_only": True,
        }
    if options.output.exists():
        path = options.output / "allocation.json"
        if not path.exists():
            path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
