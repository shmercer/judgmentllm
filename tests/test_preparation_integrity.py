"""Offline source freezing, recovery, privacy, and bounded lookup acceptance."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import tracemalloc
from contextlib import closing
from pathlib import Path
from typing import BinaryIO, Literal

import pytest

from judgmentllm import preparation as prep
from judgmentllm.config import GenerationSpec, RunSpec, load_run_spec
from judgmentllm.generation import GenerationCapabilities, GenerationParameterCapability
from judgmentllm.io import read_pairs, read_responses
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.planning import CapabilityProfile, iter_requests, stable_id
from judgmentllm.preparation.index import ParsingContext
from judgmentllm.prompts import get_template

Capture = Literal["full", "minimal"]
STUDENT = "STUDENT_PRIVATE_14b8a66f"
EXEMPLAR = "EXEMPLAR_PRIVATE_26b7c959"
TEMPLATE = "TEMPLATE_PRIVATE_376cc881"
METADATA = "FREEFORM_PRIVATE_4cdef917"
SENTINELS = (STUDENT, EXEMPLAR, TEMPLATE, METADATA)


def _document(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _sources(
    directory: Path,
    capture: Capture,
    format: Literal["csv", "jsonl"] = "csv",
    *,
    pairwise: bool = False,
    count: int = 2,
    essay_length: int = 32,
) -> Path:
    directory.mkdir(parents=True)
    response_path = directory / f"responses.{format}"
    with response_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        if format == "csv":
            writer.writerow(["item_id", "text", "metadata"])
        for ordinal in range(count):
            identifier = f"{ordinal + 1:03d}"
            text = f'{STUDENT}, résumé "{identifier}".\n' + "x" * essay_length
            metadata = {"annotation": METADATA}
            if format == "csv":
                writer.writerow([identifier, text, json.dumps(metadata)])
            else:
                stream.write(
                    json.dumps(
                        {"item_id": identifier, "text": text, "metadata": metadata}
                    )
                    + "\n"
                )
    _document(
        directory / "exemplars.json",
        {
            "exemplars": [
                {"exemplar_id": "001", "text": EXEMPLAR, "metadata": {"note": METADATA}}
            ]
        },
    )
    config: dict[str, object] = {
        "task_kind": "pairwise" if pairwise else "rubric",
        "input": response_path.name,
        "provider": "synthetic",
        "endpoint": "offline",
        "model": "synthetic-v1",
        "capture": capture,
        "exemplars": "exemplars.json",
        "prompt": {"file": "template.txt"},
    }
    if pairwise:
        config.update(criteria="criteria.json", pairs="pairs.jsonl", seed=41)
        _document(
            directory / "criteria.json",
            {
                "criteria": [
                    {
                        "trait_id": "detail",
                        "name": "Detail",
                        "description": "Compare synthetic texts.",
                    }
                ]
            },
        )
        pair = {
            "pair_id": "001",
            "pair_uid": "0001",
            "left_item_id": "001",
            "right_item_id": "002",
            "trait_id": "detail",
            "presented_order": ["002", "001"],
            "metadata": {"private": METADATA},
        }
        (directory / "pairs.jsonl").write_text(
            json.dumps(pair) + "\n", encoding="utf-8"
        )
        preset = "pairwise_forced_choice"
    else:
        config["rubric"] = "rubric.json"
        _document(
            directory / "rubric.json",
            {
                "rubric_id": "001",
                "name": "Synthetic",
                "traits": [
                    {
                        "trait_id": "detail",
                        "name": "Detail",
                        "description": "Assess the complete response.",
                        "levels": [
                            {"label": "Low", "numeric_value": 1.0},
                            {"label": "High", "numeric_value": 5.0},
                        ],
                    }
                ],
            },
        )
        preset = "rubric_scores_only"
    (directory / "template.txt").write_text(
        TEMPLATE + "\n" + get_template(preset), encoding="utf-8"
    )
    path = directory / "run.json"
    _document(path, config)
    return path


def _assert_no_private_bytes(store: Path) -> None:
    for path in store.rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            for sentinel in SENTINELS:
                assert sentinel.encode() not in raw, (path.name, sentinel)


def _assert_unsealed(store: Path, run_id: str = "001") -> None:
    database = store / "registry.sqlite3"
    if database.exists():
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute(
                "SELECT manifest_sha256 FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        assert row is None or row[0] is None


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("format", ["csv", "jsonl"])
@pytest.mark.parametrize("pairwise", [False, True])
def test_prepare_reopen_exact_sources_and_request_identity(
    tmp_path: Path, capture: Capture, format: Literal["csv", "jsonl"], pairwise: bool
) -> None:
    configuration = _sources(tmp_path / "inputs", capture, format, pairwise=pairwise)
    original = load_run_spec(configuration)
    expected_records = (
        list(read_pairs(original.pairs))
        if original.pairs
        else list(read_responses(original.input))
    )
    expected_requests = list(iter_requests(original, expected_records))
    store = tmp_path / "runtime"
    with prep.prepare_run(configuration, store=store, run_id="001") as prepared:
        frozen_hash = prepared.manifest_sha256
        assert prepared.manifest.kind == "executable_run_manifest"
        assert prepared.manifest.configuration_origin == "file"
        assert prepared.get_response("001").item_id == "001"
        assert [
            entry.request for entry in prepared.iter_request_dependencies()
        ] == expected_requests
        dependency = next(prepared.iter_request_dependencies())
        rendered = prepared.render(dependency)
        assert (
            STUDENT in rendered.text
            and EXEMPLAR in rendered.text
            and TEMPLATE in rendered.text
        )
        if pairwise:
            pair = prepared.get_pair("0001")
            assert pair.original_order == ["001", "002"]
            assert pair.presented_order == ["002", "001"]
            assert dependency.left_sha256 and dependency.right_sha256
        artifact_store = ArtifactStore(prepared.repository.run_directory("001"))
        for source in prepared.manifest.sources:
            raw = Path(source.original_location).read_bytes()
            assert bytes_digest(raw) == source.raw_sha256
            assert len(raw) == source.byte_length
            if capture == "full":
                assert source.snapshot is not None
                assert artifact_store.read(source.snapshot) == raw
            else:
                assert source.snapshot is None
        if capture == "minimal":
            _assert_no_private_bytes(store)
    with prep.open_prepared_run("001", store=store) as reopened:
        assert reopened.manifest_sha256 == frozen_hash
        assert [
            entry.request for entry in reopened.iter_request_dependencies()
        ] == expected_requests
    if capture == "minimal":
        _assert_no_private_bytes(store)


def _mutate(path: Path, mode: str) -> None:
    raw = path.read_bytes()
    before = path.stat()
    if mode in {"same_length", "preserved_mtime"}:
        changed = raw.replace(STUDENT.encode(), STUDENT.lower().encode(), 1)
        assert changed != raw and len(changed) == len(raw)
        path.write_bytes(changed)
        if mode == "preserved_mtime":
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif mode == "append":
        with path.open("ab") as stream:
            stream.write(b"\n")
    elif mode == "truncate":
        path.write_bytes(raw[: len(raw) // 2])
    elif mode == "replace":
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(raw)
        os.replace(replacement, path)
    elif mode == "missing":
        path.unlink()
    else:
        raise AssertionError(mode)


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("format", ["csv", "jsonl"])
@pytest.mark.parametrize(
    "mode",
    ["same_length", "preserved_mtime", "append", "truncate", "replace", "missing"],
)
def test_reopen_rejects_changed_or_missing_originals(
    tmp_path: Path, capture: Capture, format: Literal["csv", "jsonl"], mode: str
) -> None:
    configuration = _sources(tmp_path / "inputs", capture, format)
    store = tmp_path / "runtime"
    with prep.prepare_run(configuration, store=store, run_id="001"):
        pass
    _mutate(configuration.parent / f"responses.{format}", mode)
    with pytest.raises(IntegrityError):
        prep.open_prepared_run("001", store=store)
    if capture == "minimal":
        _assert_no_private_bytes(store)


def _matches(source_id: str | None, role: str, path: Path) -> bool:
    return any(
        source_id == stable_id("source", role, ordinal, str(path))
        for ordinal in range(10)
    )


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "stage",
    [
        "source_copied",
        "independent_hash",
        "record_indexed",
        "locator_verified",
        "final_source_verification",
    ],
)
def test_mutation_during_preparation_never_seals(
    tmp_path: Path, capture: Capture, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _sources(tmp_path / "inputs", capture)
    target = configuration.parent / "responses.csv"
    mutated = False

    def checkpoint(actual_stage: str, source_id: str | None = None) -> None:
        nonlocal mutated
        if (
            not mutated
            and actual_stage == stage
            and _matches(source_id, "response", target)
        ):
            mutated = True
            _mutate(target, "preserved_mtime")

    monkeypatch.setattr(prep, "_checkpoint", checkpoint)
    store = tmp_path / "runtime"
    with pytest.raises(IntegrityError):
        prep.prepare_run(configuration, store=store, run_id="001")
    assert mutated
    _assert_unsealed(store)
    if capture == "minimal":
        _assert_no_private_bytes(store)


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_changed_early_configuration_is_rechecked_before_sealing(
    tmp_path: Path, capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _sources(tmp_path / "inputs", capture)
    mutated = False

    def checkpoint(stage: str, source_id: str | None = None) -> None:
        nonlocal mutated
        if stage == "document_parsed" and not mutated:
            mutated = True
            configuration.write_bytes(configuration.read_bytes() + b" ")

    monkeypatch.setattr(prep, "_checkpoint", checkpoint)
    store = tmp_path / "runtime"
    with pytest.raises(IntegrityError):
        prep.prepare_run(configuration, store=store, run_id="001")
    assert mutated
    _assert_unsealed(store)


@pytest.mark.parametrize(
    "failure_stage", ["record_indexed", "locator_verified", "snapshot_published"]
)
def test_minimal_interruption_and_diagnostics_keep_no_private_text(
    tmp_path: Path,
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configuration = _sources(tmp_path / "inputs", "minimal")
    store = tmp_path / "runtime"

    def checkpoint(stage: str, source_id: str | None = None) -> None:
        _assert_no_private_bytes(store)
        if stage == failure_stage:
            raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(prep, "_checkpoint", checkpoint)
    with pytest.raises((IntegrityError, RuntimeError)) as error:
        prep.prepare_run(configuration, store=store, run_id="001")
    _assert_unsealed(store)
    _assert_no_private_bytes(store)
    captured = capsys.readouterr()
    for sentinel in SENTINELS:
        assert sentinel not in str(error.value) + captured.out + captured.err


@pytest.mark.parametrize("format", ["csv", "jsonl"])
def test_duplicate_ids_and_missing_pair_references_cannot_seal(
    tmp_path: Path, format: Literal["csv", "jsonl"]
) -> None:
    configuration = _sources(tmp_path / "inputs", "minimal", format, pairwise=True)
    response_path = configuration.parent / f"responses.{format}"
    raw = response_path.read_bytes()
    if format == "jsonl":
        response_path.write_bytes(raw.replace(b'"002"', b'"001"'))
    else:
        response_path.write_bytes(raw.replace(b"\r\n002,", b"\r\n001,"))
    store = tmp_path / "runtime"
    with pytest.raises(IntegrityError):
        prep.prepare_run(configuration, store=store, run_id="duplicate")
    _assert_unsealed(store, "duplicate")
    response_path.write_bytes(raw)
    pairs = configuration.parent / "pairs.jsonl"
    pairs.write_bytes(pairs.read_bytes().replace(b'"002"', b'"999"'))
    with pytest.raises(IntegrityError):
        prep.prepare_run(configuration, store=store, run_id="missing")
    _assert_unsealed(store, "missing")


def test_changed_pair_text_updates_dependencies_without_structural_ids(
    tmp_path: Path,
) -> None:
    configuration = _sources(tmp_path / "inputs", "full", pairwise=True)
    store = tmp_path / "runtime"
    with prep.prepare_run(configuration, store=store, run_id="before") as prepared:
        before = next(prepared.iter_request_dependencies())
    _mutate(configuration.parent / "responses.csv", "same_length")
    with prep.prepare_run(configuration, store=store, run_id="after") as prepared:
        after = next(prepared.iter_request_dependencies())
    assert before.request == after.request
    assert before.left_sha256 != after.left_sha256
    assert before.right_sha256 == after.right_sha256
    assert before.dependency_sha256 != after.dependency_sha256


def test_generation_and_current_capability_incompatibility_blocks_work(
    tmp_path: Path,
) -> None:
    configuration = _sources(tmp_path / "inputs", "full")
    spec = load_run_spec(configuration)
    spec.generation = GenerationSpec(temperature=0)
    store = tmp_path / "runtime"
    with prep.prepare_run(spec, store=store, run_id="001") as prepared:
        original_evidence = prepared.manifest.generation
        prepared.spec.generation = GenerationSpec(temperature=1)
        with pytest.raises(IntegrityError):
            next(prepared.iter_request_dependencies())
    restrictions = CapabilityProfile(
        generation=GenerationCapabilities(
            provider=spec.provider,
            endpoint=spec.endpoint,
            model=spec.model,
            temperature=GenerationParameterCapability(supported=False),
        )
    )
    with pytest.raises((IntegrityError, ValueError)):
        prep.open_prepared_run("001", store=store, capabilities=restrictions)
    with prep.open_prepared_run("001", store=store) as reopened:
        assert reopened.manifest.generation == original_evidence


def test_direct_python_spec_has_no_invented_configuration_file(tmp_path: Path) -> None:
    configuration = _sources(tmp_path / "inputs", "minimal")
    spec: RunSpec = load_run_spec(configuration)
    with prep.prepare_run(spec, store=tmp_path / "runtime", run_id="001") as prepared:
        assert prepared.manifest.configuration_origin == "python"
        assert prepared.manifest.configuration_location is None
        assert not any(
            source.role == "configuration" for source in prepared.manifest.sources
        )
        assert [item.item_id for item in prepared.iter_responses()] == ["001", "002"]


def test_bounded_preparation_and_lookup_memory(tmp_path: Path) -> None:
    peaks: list[int] = []
    for count in (200, 2_000):
        configuration = _sources(
            tmp_path / f"inputs-{count}",
            "minimal",
            "jsonl",
            count=count,
            essay_length=4_000,
        )
        tracemalloc.start()
        try:
            with prep.prepare_run(
                configuration, store=tmp_path / f"runtime-{count}", run_id="001"
            ) as prepared:
                assert prepared.get_response(f"{count:03d}").item_id == f"{count:03d}"
                assert (
                    next(prepared.iter_request_dependencies()).request.response
                    is not None
                )
                peaks.append(tracemalloc.get_traced_memory()[1])
        finally:
            tracemalloc.stop()
    # The source grows by over 7 MB; bounded chunks/pages may fluctuate, but
    # neither all essays nor the response-by-trait product may accumulate.
    assert peaks[1] < peaks[0] + 3 * 1024 * 1024, peaks


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_reference_generation_cannot_change_between_discovery_and_freezing(
    tmp_path: Path, capture: Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _sources(tmp_path / "inputs", capture)
    target = configuration.parent / "rubric.json"
    original_reader = prep._memory_source  # pyright: ignore[reportPrivateUsage]
    mutated = False

    def changing_reader(path: Path) -> tuple[bytes, prep.FileSignature, str]:
        nonlocal mutated
        result = original_reader(path)
        if path == target and not mutated:
            mutated = True
            target.write_bytes(
                target.read_bytes().replace(
                    b"Assess the complete response.", b"Assess the modified response."
                )
            )
        return result

    monkeypatch.setattr(prep, "_memory_source", changing_reader)
    store = tmp_path / "runtime"
    with pytest.raises(IntegrityError):
        prep.prepare_run(configuration, store=store, run_id="001")
    assert mutated
    _assert_unsealed(store)


@pytest.mark.parametrize("stage", ["before_seal", "final_source_verification"])
def test_late_change_to_earlier_source_cannot_seal(
    tmp_path: Path, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _sources(tmp_path / "inputs", "full")
    mutated = False

    def checkpoint(actual_stage: str, source_id: str | None = None) -> None:
        nonlocal mutated
        last_source = _matches(
            source_id, "response", configuration.parent / "responses.csv"
        )
        if (
            not mutated
            and actual_stage == stage
            and (stage == "before_seal" or last_source)
        ):
            mutated = True
            configuration.write_bytes(configuration.read_bytes() + b" ")

    monkeypatch.setattr(prep, "_checkpoint", checkpoint)
    store = tmp_path / "runtime"
    with pytest.raises(IntegrityError):
        prep.prepare_run(configuration, store=store, run_id="001")
    assert mutated
    _assert_unsealed(store)


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "source_name", ["responses.csv", "rubric.json", "exemplars.json", "template.txt"]
)
def test_existing_session_rejects_changed_assessment_original(
    tmp_path: Path, capture: Capture, source_name: str
) -> None:
    configuration = _sources(tmp_path / "inputs", capture)
    store = tmp_path / "runtime"
    with prep.prepare_run(configuration, store=store, run_id="001") as prepared:
        source = configuration.parent / source_name
        source.write_bytes(source.read_bytes() + b" ")
        with pytest.raises(IntegrityError):
            next(prepared.iter_request_dependencies())


def test_frozen_snapshot_authorization_is_explicit_and_session_scoped(
    tmp_path: Path,
) -> None:
    configuration = _sources(tmp_path / "inputs", "full", pairwise=True)
    store = tmp_path / "runtime"
    with prep.prepare_run(configuration, store=store, run_id="001") as prepared:
        identity = prepared.manifest_sha256
        expected = prepared.get_response("001")
    _mutate(configuration.parent / "responses.csv", "missing")
    with pytest.raises(IntegrityError):
        prep.open_prepared_run("001", store=store)
    with prep.open_prepared_run(
        "001", store=store, use_frozen_snapshot=True
    ) as authorized:
        assert authorized.manifest_sha256 == identity
        assert authorized.get_response("001") == expected
        assert next(authorized.iter_request_dependencies()).left_sha256
    audit = list(prep.iter_snapshot_authorizations("001", store=store))
    assert len(audit) == 1
    assert audit[0].manifest_sha256 == identity
    assert audit[0].discrepancies
    with pytest.raises(IntegrityError):
        prep.open_prepared_run("001", store=store)


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_frozen_authorization_cannot_bypass_snapshot_damage(
    tmp_path: Path, damage: str
) -> None:
    configuration = _sources(tmp_path / "inputs", "full")
    store = tmp_path / "runtime"
    with prep.prepare_run(configuration, store=store, run_id="001") as prepared:
        source = next(
            source for source in prepared.manifest.sources if source.role == "response"
        )
        assert source.snapshot is not None
        artifact_store = ArtifactStore(prepared.repository.run_directory("001"))
        snapshot = artifact_store.verify(source.snapshot)
        assert snapshot is not None
    _mutate(configuration.parent / "responses.csv", "missing")
    if damage == "missing":
        snapshot.unlink()
    else:
        snapshot.write_bytes(b"corrupted frozen snapshot")
    with pytest.raises(IntegrityError):
        prep.open_prepared_run("001", store=store, use_frozen_snapshot=True)


def test_minimal_snapshot_flag_never_restores_changed_input(tmp_path: Path) -> None:
    configuration = _sources(tmp_path / "inputs", "minimal")
    store = tmp_path / "runtime"
    with prep.prepare_run(configuration, store=store, run_id="001"):
        pass
    _mutate(configuration.parent / "responses.csv", "missing")
    with pytest.raises(IntegrityError):
        prep.open_prepared_run("001", store=store, use_frozen_snapshot=True)


@pytest.mark.parametrize(
    "stage", ["record_indexed", "manifest_published", "manifest_sealed"]
)
def test_interrupted_preparation_has_explicit_recovery_boundaries(
    tmp_path: Path, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _sources(tmp_path / "inputs", "full")
    store = tmp_path / "runtime"

    def checkpoint(actual_stage: str, source_id: str | None = None) -> None:
        if actual_stage == stage:
            raise RuntimeError("synthetic process interruption")

    monkeypatch.setattr(prep, "_checkpoint", checkpoint)
    with pytest.raises(RuntimeError):
        prep.prepare_run(configuration, store=store, run_id="001")

    def no_fault(stage: str, source_id: str | None = None) -> None:
        pass

    monkeypatch.setattr(prep, "_checkpoint", no_fault)
    if stage == "record_indexed":
        with pytest.raises(IntegrityError):
            prep.open_prepared_run("001", store=store)
        with pytest.raises(IntegrityError):
            prep.prepare_run(configuration, store=store, run_id="001")
        _assert_unsealed(store)
    else:
        with prep.open_prepared_run("001", store=store) as recovered:
            assert recovered.get_response("001").item_id == "001"
            assert (
                recovered.repository.get_run("001").manifest_sha256
                == recovered.manifest_sha256
            )


def test_generation_empty_equivalence_preserves_semantic_not_file_hash(
    tmp_path: Path,
) -> None:
    configuration = _sources(tmp_path / "inputs", "full")
    config = json.loads(configuration.read_text(encoding="utf-8"))
    semantic: list[str] = []
    exact: list[str] = []
    for ordinal, generation in enumerate((None, {}, {"temperature": None})):
        config["generation"] = generation
        _document(configuration, config)
        with prep.prepare_run(
            configuration, store=tmp_path / "runtime", run_id=str(ordinal)
        ) as prepared:
            semantic.append(prepared.manifest.configuration_sha256)
            exact.append(
                next(
                    source.raw_sha256
                    for source in prepared.manifest.sources
                    if source.role == "configuration"
                )
            )
            assert prepared.manifest.generation.settings is None
    del config["generation"]
    _document(configuration, config)
    with prep.prepare_run(
        configuration, store=tmp_path / "runtime", run_id="omitted"
    ) as prepared:
        semantic.append(prepared.manifest.configuration_sha256)
        exact.append(
            next(
                source.raw_sha256
                for source in prepared.manifest.sources
                if source.role == "configuration"
            )
        )
    assert len(set(semantic)) == 1
    assert len(set(exact)) == 4


def test_selected_record_generation_is_checked_after_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _sources(tmp_path / "inputs", "minimal")
    target = configuration.parent / "responses.csv"
    with prep.prepare_run(
        configuration, store=tmp_path / "runtime", run_id="001"
    ) as prepared:
        original_read = prep.read_record

        def changing_read(
            stream: BinaryIO,
            spec: prep.InputSpec,
            locator: prep.RecordLocator,
            context: ParsingContext,
        ) -> object:
            result = original_read(stream, spec, locator, context)
            _mutate(target, "preserved_mtime")
            return result

        monkeypatch.setattr(prep, "read_record", changing_read)
        with pytest.raises(IntegrityError):
            prepared.get_response("001")


def test_minimal_python_origin_requires_exact_spec_on_reopen(tmp_path: Path) -> None:
    configuration = _sources(tmp_path / "inputs", "minimal")
    spec = load_run_spec(configuration)
    store = tmp_path / "runtime"
    with prep.prepare_run(spec, store=store, run_id="001"):
        pass
    with pytest.raises(IntegrityError):
        prep.open_prepared_run("001", store=store)
    with prep.open_prepared_run("001", store=store, spec=spec) as reopened:
        assert reopened.get_response("001").item_id == "001"
    assert spec.rubric is not None
    spec.rubric.traits[0].description = "Changed assessment with unchanged trait IDs."
    with pytest.raises(IntegrityError):
        prep.open_prepared_run("001", store=store, spec=spec)


def test_complete_validation_and_invalid_template(tmp_path: Path) -> None:
    configuration = _sources(tmp_path / "inputs", "minimal")
    report = prep.validate_inputs(configuration)
    assert report.valid and report.response_count == 2 and report.pair_count == 0
    (configuration.parent / "template.txt").write_text(
        "{{ undeclared_private_variable }}", encoding="utf-8"
    )
    assert not prep.validate_inputs(configuration).valid
    store = tmp_path / "runtime"
    with pytest.raises(IntegrityError):
        prep.prepare_run(configuration, store=store, run_id="001")
    _assert_unsealed(store)


def test_malformed_yaml_source_diagnostics_do_not_leak_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configuration = tmp_path / "run.yaml"
    configuration.write_text(
        f"capture: minimal\nsecret: [{STUDENT}\n", encoding="utf-8"
    )
    with pytest.raises(IntegrityError) as error:
        prep.prepare_run(configuration, store=tmp_path / "runtime", run_id="001")
    captured = capsys.readouterr()
    assert STUDENT not in str(error.value) + captured.out + captured.err
    assert not prep.validate_inputs(configuration).valid


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_multiline_pair_csv_index_and_lookup(tmp_path: Path, capture: Capture) -> None:
    configuration = _sources(tmp_path / "inputs", capture, pairwise=True)
    pair_json = configuration.parent / "pairs.jsonl"
    pair = json.loads(pair_json.read_text(encoding="utf-8"))
    pair_csv = configuration.parent / "pairs.csv"
    with pair_csv.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(list(pair))
        writer.writerow(
            [
                json.dumps(value, indent=2)
                if isinstance(value, (list, dict))
                else value
                for value in pair.values()
            ]
        )
    config = json.loads(configuration.read_text(encoding="utf-8"))
    config["pairs"] = pair_csv.name
    _document(configuration, config)
    store = tmp_path / "runtime"
    with prep.prepare_run(configuration, store=store, run_id="001") as prepared:
        assert prepared.get_pair("0001").presented_order == ["002", "001"]
        assert prepared.get_pair("0001").metadata == {"private": METADATA}
        source = next(
            source for source in prepared.manifest.sources if source.role == "pair"
        )
        assert source.parsing is not None
        indexed = prepared.repository.get_input_record("001", "pair", "0001")
        raw = pair_csv.read_bytes()
        assert indexed.byte_offset == source.parsing.header_byte_length
        assert indexed.byte_offset + indexed.byte_length == len(raw)
        assert len(raw[indexed.byte_offset :].splitlines()) > 2
    with prep.open_prepared_run("001", store=store) as reopened:
        assert reopened.get_pair("0001").pair_uid == "0001"
    if capture == "minimal":
        _assert_no_private_bytes(store)
