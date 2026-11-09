"""Small offline qualification of scale tools, never the costly scale workloads."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"


def benchmark_module(name: str) -> Any:
    # Tools remain outside the installed package; this is not a public API.
    spec = importlib.util.spec_from_file_location(name, BENCHMARKS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(BENCHMARKS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(BENCHMARKS))
    return module


def test_scale_fixture_is_deterministic_and_counts_partial_groups(
    tmp_path: Path,
) -> None:
    workloads = benchmark_module("workloads")
    case = workloads.scale_workload(responses=3, traits=7, group_size=5)
    assert case.logical_tasks == 21
    assert case.physical_requests == 6
    first = workloads.inputs(tmp_path / "first", case, "minimal")
    second = workloads.inputs(tmp_path / "second", case, "minimal")
    assert first.read_bytes() == second.read_bytes()
    config = json.loads(first.read_text())
    assert len(config["exemplars"]) == 3
    assert len(config["rubric"]["traits"]) == 7
    assert [
        level["numeric_value"] for level in config["rubric"]["traits"][0]["levels"]
    ] == [0, 1, 3, 7]
    records = [
        json.loads(line)
        for line in (first.parent / "responses.jsonl").read_text().splitlines()
    ]
    assert records[0]["item_id"] == "000000"
    assert all(len(record["text"].encode()) == 4096 for record in records)


def test_historical_workloads_keep_original_definition(tmp_path: Path) -> None:
    workloads = benchmark_module("workloads")
    case = workloads.WORKLOADS[0]
    assert case.name == "independent-3" and not case.scale
    path = workloads.inputs(tmp_path / "inputs", case, "full")
    config = json.loads(path.read_text())
    assert "exemplars" not in config
    assert len((path.parent / "responses.jsonl").read_text().splitlines()) == 200
    assert config["rubric"]["traits"][0]["levels"] == [
        {"label": "low", "numeric_value": 1},
        {"label": "middle", "numeric_value": 3},
        {"label": "high", "numeric_value": 8},
    ]


def test_repeated_native_measurements_do_not_grow_import_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = benchmark_module("execution_worker")
    monkeypatch.setattr(
        sys, "path", [entry for entry in sys.path if entry != str(ROOT)]
    )
    first = worker.native_peak_bytes()
    assert first > 0 and sys.path.count(str(ROOT)) == 1
    initialized = tuple(sys.path)
    for _ in range(5):
        assert worker.native_peak_bytes() >= first
    assert tuple(sys.path) == initialized


def test_tiny_scale_complete_and_prefix_reports(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    tmp_path = tmp_path_factory.mktemp("s")
    for extent in ("complete", "prefix", "prepare"):
        output = tmp_path / extent
        process = subprocess.run(
            [
                sys.executable,
                str(BENCHMARKS / "execution_benchmark.py"),
                "--source-root",
                str(ROOT),
                "--output",
                str(output),
                "--workloads",
                "scale-rubric",
                "--responses",
                "3",
                "--traits",
                "7",
                "--group-size",
                "5",
                "--concurrency",
                "2",
                "--extent",
                extent,
                "--completed-requests",
                "2",
                "--captures",
                "minimal",
                "--modes",
                "plain",
                "--warmups",
                "0",
                "--repetitions",
                "1",
                "--min-free-gib",
                "0",
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert process.returncode == 0, (process.stdout, process.stderr)
        rows = list((output / "raw").glob("*.json"))
        assert len(rows) == 1
        report = json.loads(rows[0].read_text())
        assert report["logical_task_count"] == 21
        assert report["planned_physical_requests"] == 6
        assert (
            report["completed_physical_requests"]
            == {"complete": 6, "prefix": 2, "prepare": 0}[extent]
        )
        assert report["native_peak_bytes"] < 256 * 1024**2
        assert report["storage"]["samples"] >= 2
        if extent == "complete":
            assert report["result_rows"] == 21
            assert (
                report["prepared_attempts"] == report["accepted_physical_requests"] == 6
            )
            assert report["verification"]["no_op_resume_acceptance_delta"] == 0
            assert all(item["rows"] == 21 for item in report["export_files"])
            assert report["phases"]["provider_payload_preparation"]["calls"] > 0
            assert (
                report["phases"]["provider_payload_preparation"][
                    "exclusive_thread_cpu_ns"
                ]
                >= 0
            )
            for name in ("submission", "retrieval"):
                assert (
                    report["phases"][name]["calls"]
                    == report["completed_physical_requests"]
                )
                assert report["phases"][name]["wall_ns"] > 0
                assert "exclusive_thread_cpu_ns" not in report["phases"][name]
        elif extent == "prepare":
            assert (
                report["prepared_attempts"] == report["accepted_physical_requests"] == 0
            )


def test_storage_budget_failure_keeps_structured_evidence(tmp_path: Path) -> None:
    process = subprocess.run(
        [
            sys.executable,
            str(BENCHMARKS / "execution_benchmark.py"),
            "--source-root",
            str(ROOT),
            "--output",
            str(tmp_path),
            "--workloads",
            "scale-rubric",
            "--responses",
            "2",
            "--traits",
            "2",
            "--captures",
            "minimal",
            "--modes",
            "plain",
            "--warmups",
            "0",
            "--repetitions",
            "1",
            "--min-free-gib",
            "1000000000",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode != 0
    failures = list((tmp_path / "failures").glob("*.json"))
    assert failures
    failure = json.loads(failures[0].read_text())
    assert failure["status"] == "failed"
    assert failure["returncode"] != 0
    controller = benchmark_module("execution_benchmark")
    with pytest.raises(RuntimeError, match="failed sample"):
        controller.summarize(tmp_path / "raw")


@pytest.mark.resource
@pytest.mark.parametrize("scope", ["whole", "execution"])
def test_tiny_scale_allocation_diagnostics(tmp_path: Path, scope: str) -> None:
    process = subprocess.run(
        [
            sys.executable,
            str(BENCHMARKS / "execution_benchmark.py"),
            "--source-root",
            str(ROOT),
            "--output",
            str(tmp_path),
            "--workloads",
            "scale-rubric",
            "--responses",
            "3",
            "--traits",
            "7",
            "--concurrency",
            "2",
            "--captures",
            "minimal",
            "--modes",
            "tracemalloc",
            "--measurement-scope",
            scope,
            "--warmups",
            "0",
            "--min-free-gib",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert process.returncode == 0, (process.stdout, process.stderr)
    report = json.loads(next((tmp_path / "raw").glob("*.json")).read_text())
    assert report["status"] == "passed" and report["python_peak_bytes"] > 0
    assert report["execution_asyncio_tasks"] == 4
    assert report["peak_asyncio_tasks"] <= 7
    if scope == "execution":
        assert report["export_python_peak_bytes"] < 8 * 1024**2
        assert report["allocation_baseline"]["native_peak_bytes"] > 0
    else:
        allocations = report["phase_allocations"]
        assert set(allocations) == {
            "configuration",
            "preparation",
            "execution",
            "inspection",
            "export_jsonl_canonical",
            "export_csv_canonical",
            "export_csv_r",
        }
        assert report["python_peak_bytes"] >= max(
            value["peak_absolute_bytes"] for value in allocations.values()
        )
        assert allocations["execution"]["peak_absolute_bytes"] < 32 * 1024**2
        assert report["phase_envelope_proof"]["execution_below_32_mib"] is True
        assert report["phase_envelope_proof"]["all_exports_below_8_mib"] is True
        assert (
            report["allocation_baseline"]["python_current_bytes"]
            == allocations["execution"]["baseline_live_bytes"]
        )
        for name, value in allocations.items():
            assert value["peak_absolute_bytes"] >= value["baseline_live_bytes"]
            assert (
                value["peak_above_baseline_bytes"]
                == value["peak_absolute_bytes"] - value["baseline_live_bytes"]
            )
            assert (
                value["net_retained_growth_bytes"]
                == value["retained_after_gc_bytes"] - value["baseline_live_bytes"]
            )
            if name.startswith("export_"):
                assert value["peak_absolute_bytes"] < 8 * 1024**2


# Keep the legacy Windows path budget; xdist adds another directory level.
@pytest.mark.serial
def test_shared_fixture_rejects_changed_dimensions_before_removing_store(
    tmp_path: Path,
) -> None:
    work = tmp_path / "shared"
    command = [
        sys.executable,
        str(BENCHMARKS / "execution_benchmark.py"),
        "--source-root",
        str(ROOT),
        "--work-root",
        str(work),
        "--workloads",
        "scale-rubric",
        "--traits",
        "2",
        "--captures",
        "minimal",
        "--modes",
        "plain",
        "--warmups",
        "0",
        "--repetitions",
        "1",
        "--min-free-gib",
        "0",
    ]
    first = subprocess.run(
        [*command, "--responses", "2", "--output", str(tmp_path / "first")],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert first.returncode == 0, first.stderr
    database = work / "scale-rubric-minimal" / "store" / "registry.sqlite3"
    before = database.read_bytes()
    second = subprocess.run(
        [*command, "--responses", "3", "--output", str(tmp_path / "second")],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert second.returncode != 0
    assert "fixture dimensions or input bytes differ" in second.stderr
    assert database.read_bytes() == before
    source = work / "scale-rubric-minimal" / "inputs" / "responses.jsonl"
    source.write_bytes(source.read_bytes().replace(b"Synthetic", b"Different", 1))
    changed = subprocess.run(
        [*command, "--responses", "2", "--output", str(tmp_path / "changed")],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert changed.returncode != 0 and database.read_bytes() == before


def test_verification_watchdog_marks_raw_sample_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = benchmark_module("execution_benchmark")
    monkeypatch.setattr(controller, "runtime_identity", lambda: {"python": "synthetic"})

    def child(arguments: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if arguments[1].endswith("execution_worker.py"):
            destination = Path(arguments[arguments.index("--output") + 1])
            destination.write_text(
                json.dumps({"status": "passed", "wall_seconds": 0.1})
            )
            return subprocess.CompletedProcess(arguments, 0, "{}", "")
        raise subprocess.TimeoutExpired(arguments, 1)

    monkeypatch.setattr(controller.subprocess, "run", child)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(BENCHMARKS / "execution_benchmark.py"),
            "--source-root",
            str(ROOT),
            "--output",
            str(tmp_path),
            "--workloads",
            "scale-rubric",
            "--captures",
            "minimal",
            "--modes",
            "plain",
            "--warmups",
            "0",
            "--repetitions",
            "1",
        ],
    )
    with pytest.raises(RuntimeError, match="watchdog timeout"):
        controller.main()
    report = json.loads(next((tmp_path / "raw").glob("*.json")).read_text())
    assert (
        report["status"] == "failed" and report["failure"]["type"] == "WatchdogTimeout"
    )
    failure = json.loads(next((tmp_path / "failures").glob("*.json")).read_text())
    assert (
        failure["reason"] == "watchdog_timeout"
        and failure["partial_report"]["status"] == "failed"
    )


@pytest.mark.parametrize("capture", ["minimal", "full"])
def test_focused_export_allocations_preserve_original_store(
    tmp_path_factory: pytest.TempPathFactory, capture: str
) -> None:
    tmp_path = tmp_path_factory.mktemp("e")
    producer = tmp_path / "producer"
    process = subprocess.run(
        [
            sys.executable,
            str(BENCHMARKS / "execution_benchmark.py"),
            "--source-root",
            str(ROOT),
            "--output",
            str(producer),
            "--workloads",
            "scale-rubric",
            "--responses",
            "2",
            "--traits",
            "3",
            "--captures",
            capture,
            "--modes",
            "plain",
            "--warmups",
            "0",
            "--repetitions",
            "1",
            "--min-free-gib",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert process.returncode == 0, (process.stdout, process.stderr)
    sample = next((producer / "raw").glob("*.json"))
    original = json.loads(sample.read_text())
    database = Path(original["working_directory"]) / "store" / "registry.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    def measure(
        label: str, kind: str, format: str, view: str, metadata: Path | None = None
    ) -> dict[str, Any]:
        output = tmp_path / label
        command = [
            sys.executable,
            str(BENCHMARKS / "export_allocation.py"),
            "--sample",
            str(sample),
            "--output",
            str(output),
            "--kind",
            kind,
            "--format",
            format,
            "--view",
            view,
            "--expected-rows",
            "6",
        ]
        if metadata is not None:
            command.extend(["--metadata-report", str(metadata)])
        observed = subprocess.run(command, capture_output=True, text=True, timeout=90)
        assert observed.returncode == 0, (observed.stdout, observed.stderr)
        result = json.loads((output / "allocation.json").read_text())
        assert result["status"] == "passed" and result["rows"] == 6
        assert result["native_peak_bytes"] < 256 * 1024**2
        assert result["original_registry_before"] == result["original_registry_after"]
        return result

    metadata = measure("service", "service", "csv", "r")
    assert metadata["old_exports_unchanged"] and metadata["old_export_count"] == 3
    assert (
        metadata["first_export_page_live_bytes"] == metadata["fixed_context_live_bytes"]
    )
    assert (
        metadata["metadata_new_peak_bytes"] > 0
        and metadata["service_below_8_mib_proof"]
    )
    assert metadata["metadata_trace_start_stage"] == "after_export_artifact"
    for format, view in (("jsonl", "canonical"), ("csv", "canonical"), ("csv", "r")):
        result = measure(
            format + "-" + view,
            "component",
            format,
            view,
            tmp_path / "service" / "allocation.json",
        )
        expected = next(
            item
            for item in original["export_files"]
            if item["format"] == format and item["view"] == view
        )
        assert (
            result["recorded_original_hash_match"]
            and result["sha256"] == expected["sha256"]
        )
        assert result["bytes"] == expected["bytes"] and result["component_below_8_mib"]
        assert result["component_repository_open_at_boundary"] is True
        assert result["component_live_before_metadata_bytes"] > 0
        assert (
            result["component_boundary"]
            == "after_artifact_publication_and_temp_cleanup_before_final_verification_repository_close_or_gc"
        )
        envelope = result["conservative_complete_export_envelope"]
        assert (
            envelope["simple_conservative_sum_bytes"]
            == result["python_peak_bytes"]
            + metadata["fixed_context_live_bytes"]
            + metadata["metadata_new_peak_bytes"]
        )
        assert (
            envelope["serialization_publication_bound_bytes"]
            == result["python_peak_bytes"] + metadata["fixed_context_live_bytes"]
        )
        assert (
            envelope["metadata_phase_bound_bytes"]
            == result["component_live_before_metadata_bytes"]
            + metadata["fixed_context_live_bytes"]
            + metadata["metadata_new_peak_bytes"]
        )
        assert envelope["phase_union_bound_bytes"] == max(
            envelope["serialization_publication_bound_bytes"],
            envelope["metadata_phase_bound_bytes"],
        )
        assert envelope["below_8_mib_proof"]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_missing_git_uses_archived_source_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "path",
        [str(Path(__file__).resolve().parents[1] / "benchmarks"), *sys.path],
    )
    provenance = importlib.import_module("provenance")
    monkeypatch.setattr(provenance.platform, "processor", lambda: "synthetic CPU")
    archived: dict[str, object] = {
        "commit": "a" * 40,
        "archive_sha256": "b" * 64,
        "files": {},
    }
    (tmp_path / ".judgmentllm-source.json").write_text(
        json.dumps(archived), encoding="utf-8"
    )
    calls: list[list[str]] = []

    def missing(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(arguments)
        raise FileNotFoundError("synthetic missing Git executable")

    monkeypatch.setattr(provenance.subprocess, "run", missing)
    result = provenance.host_identity(tmp_path)
    assert result["git_commit"] == archived["commit"]
    assert result["archived_source"] == archived
    assert result["git_tracked_status"] is None
    assert calls == [
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=no"],
    ]


@pytest.mark.parametrize("exit_code", [0, 128])
def test_present_git_retains_existing_success_and_nonrepository_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    monkeypatch.setattr(
        sys,
        "path",
        [str(Path(__file__).resolve().parents[1] / "benchmarks"), *sys.path],
    )
    provenance = importlib.import_module("provenance")
    monkeypatch.setattr(provenance.platform, "processor", lambda: "synthetic CPU")
    archived = {"commit": "a" * 40}
    (tmp_path / ".judgmentllm-source.json").write_text(
        json.dumps(archived), encoding="utf-8"
    )
    head = "c" * 40

    def present(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        value = (
            head + "\n"
            if arguments[-2:] == ["rev-parse", "HEAD"]
            else " M modified.py\n"
        )
        return subprocess.CompletedProcess(
            arguments, exit_code, stdout=value, stderr=""
        )

    monkeypatch.setattr(provenance.subprocess, "run", present)
    result = provenance.host_identity(tmp_path)
    assert result["git_commit"] == (head if exit_code == 0 else archived["commit"])
    assert result["git_tracked_status"] == ("M modified.py" if exit_code == 0 else None)
    assert result["archived_source"] == archived
