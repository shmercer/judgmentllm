"""Native pre-seal preparation uses the actual append-only registry and sources."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError
from test_native_selection import catalog, evidence, scope
from test_native_transport_integration import offline as offline

from judgmentllm.config import RunSpecV3, load_run_spec
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.repository import Repository
from judgmentllm.persistence.transport_receipts import transport_reference
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.preparation.freezing import PreparationError
from judgmentllm.preparation.manifest import ExecutableRunManifestV3
from judgmentllm.transport import TransportContext, physical_identity


def native_specification(
    directory: Path,
    capture: Literal["full", "minimal"] = "full",
    *,
    grouped: bool = True,
) -> tuple[Path, TransportContext]:
    directory.mkdir(parents=True)
    responses = directory / "responses.jsonl"
    responses.write_text(
        "".join(
            json.dumps(
                {
                    "item_id": ident,
                    "text": f"private-student-native-{ident}-canary",
                }
            )
            + "\n"
            for ident in ("001", "002")
        ),
        encoding="utf-8",
    )
    config = directory / "run.json"
    document = {
        "schema_version": 3,
        "task_kind": "rubric",
        "capture": capture,
        "provider": "openai",
        "endpoint": "responses",
        "model": "judgmentllm-synthetic-native-v1",
        "input": str(responses),
        "execution": {
            "grouping": "grouped" if grouped else "independent",
            **({"groups": [["003", "001"], ["002"]]} if grouped else {}),
        },
        "prompt": {
            "structured_output": "native",
            "native_mechanism": "openai.responses.text.format",
        },
        "rubric": {
            "rubric_id": "rubric-001",
            "name": "Synthetic",
            "traits": [
                {
                    "trait_id": ident,
                    "name": "Synthetic",
                    "description": "Synthetic criterion.",
                    "levels": [
                        {"label": "private-native-low-canary"},
                        {"label": "private-native-high-canary"},
                    ],
                }
                for ident in ("001", "002", "003")
            ],
        },
    }
    config.write_text(json.dumps(document), encoding="utf-8")
    selected_scope = scope()
    return config, TransportContext(
        selected_scope, evidence(selected_scope), catalog(selected_scope)
    )


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("grouped", [False, True])
def test_preseal_bijection_and_exact_frozen_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: Literal["full", "minimal"],
    grouped: bool,
) -> None:
    config, context = native_specification(
        tmp_path / "inputs", capture, grouped=grouped
    )
    store = tmp_path / "runtime"
    expected = 4 if grouped else 6
    with prepare_run(
        config, store=store, run_id="001", transport_context=context
    ) as prepared:
        assert isinstance(prepared.manifest, ExecutableRunManifestV3)
        assert prepared.manifest.initial_transport_count == expected
        assert prepared.repository.get_execution_checkpoint("001") == expected
        assert prepared.repository.summary("001").attempts == expected
        assert prepared.repository.summary("001").tasks == 6
        assert tuple(prepared.repository.iter_submissions("001")) == ()
        identities: list[str] = []
        for dependency in prepared.iter_request_dependencies():
            attempt = prepared.repository.get_attempt_for_request(
                "001", dependency.request.request_uid
            )
            assert attempt is not None
            receipt = prepared.transport_for_attempt(attempt.attempt_id)
            identities.append(receipt.commitment_sha256)
            wire = prepared.prepare_transport_attempt(
                dependency,
                physical_identity("001", attempt.request_uid, attempt.attempt_id),
                frozen=receipt,
            )
            assert wire.payload_sha256 == attempt.payload_sha256
            for content in wire.content_artifacts().values():
                artifact = prepared.repository.get_artifact(
                    "001", bytes_digest(content)
                )
                assert (artifact.relative_path is not None) == (capture == "full")
        manifest_bytes = (prepared.store.root / "manifest.json").read_bytes()

    def selection_forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("committed native requests must never reselect")

    monkeypatch.setattr(
        "judgmentllm.transport._resolve_native_transport", selection_forbidden
    )
    with open_prepared_run("001", store=store, transport_context=context) as reopened:
        assert (reopened.store.root / "manifest.json").read_bytes() == manifest_bytes
        assert [
            reopened.transport_for_attempt(a.attempt_id).commitment_sha256
            for a in reopened.repository.iter_attempts("001")
        ] == identities
    if capture == "minimal":
        for path in store.rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
                assert b"private-student-native-" not in raw
                assert b"private-native-low-canary" not in raw


@pytest.mark.parametrize(
    "change",
    [
        "missing_receipt",
        "duplicate_receipt",
        "checkpoint",
        "native_schema",
        "extra_request",
    ],
)
def test_sealed_preparation_rejects_missing_extra_or_changed_commitments(
    tmp_path: Path,
    change: str,
) -> None:
    config, context = native_specification(tmp_path / "inputs")
    store = tmp_path / "runtime"
    with prepare_run(
        config, store=store, run_id="001", transport_context=context
    ) as prepared:
        first = next(prepared.repository.iter_attempts("001"))
        receipt_path = prepared.store.reference_path(
            transport_reference(first.attempt_id)
        )
        if change == "missing_receipt":
            receipt_path.unlink()
        elif change == "duplicate_receipt":
            receipt_path.with_name("0" * 64 + ".json").write_bytes(
                receipt_path.read_bytes()
            )
        elif change == "checkpoint":
            prepared.repository.connection.execute(
                "UPDATE runs SET checkpoint='5' WHERE run_id='001'"
            )
        elif change == "extra_request":
            from judgmentllm.persistence.models import RequestRecord

            prepared.repository.create_request(
                RequestRecord(
                    run_id="001",
                    request_uid="unexpected-original",
                    scheduling_ordinal=4,
                    dependency_sha256="0" * 64,
                ),
                prepared.repository.attempt_task_ids("001", first.attempt_id),
            )
        else:
            receipt = prepared.transport_for_attempt(first.attempt_id)
            assert receipt.native_schema_sha256 is not None
            artifact = prepared.repository.get_artifact(
                "001", receipt.native_schema_sha256
            )
            assert artifact.relative_path is not None
            (prepared.store.root / artifact.relative_path).unlink()
    with pytest.raises((PreparationError, IntegrityError)):
        open_prepared_run("001", store=store, transport_context=context)
    with Repository(store) as repository:
        assert repository.get_run("001").state == "submitted"
        assert tuple(repository.iter_submissions("001")) == ()


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_runtime_authority_and_original_sources_are_required(
    tmp_path: Path,
    capture: Literal["full", "minimal"],
) -> None:
    config, context = native_specification(tmp_path / "inputs", capture)
    store = tmp_path / "runtime"
    with prepare_run(config, store=store, run_id="001", transport_context=context):
        pass
    with pytest.raises(PreparationError):
        open_prepared_run("001", store=store)
    changed = replace(context, service_identity="different-synthetic-service")
    with pytest.raises(PreparationError):
        open_prepared_run("001", store=store, transport_context=changed)
    (tmp_path / "inputs" / "responses.jsonl").unlink()
    with pytest.raises(PreparationError):
        open_prepared_run("001", store=store, transport_context=context)
    if capture == "full":
        with open_prepared_run(
            "001", store=store, transport_context=context, use_frozen_snapshot=True
        ):
            pass
    else:
        with pytest.raises(PreparationError):
            open_prepared_run(
                "001", store=store, transport_context=context, use_frozen_snapshot=True
            )


@pytest.mark.parametrize(
    "stage", ["after_transport_artifacts", "after_transport_preparation", "before_seal"]
)
def test_interrupted_native_preparation_never_adopts_partial_or_new_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    config, context = native_specification(tmp_path / "inputs")
    store = tmp_path / "runtime"

    def interrupted(current: str, request_uid: str | None = None) -> None:
        if current == stage:
            raise KeyboardInterrupt()

    monkeypatch.setattr("judgmentllm.preparation._checkpoint", interrupted)
    with pytest.raises(KeyboardInterrupt):
        prepare_run(config, store=store, run_id="001", transport_context=context)
    monkeypatch.undo()
    with Repository(store) as repository:
        assert repository.get_run("001").state == "created"
        assert tuple(repository.iter_submissions("001")) == ()
    if stage == "before_seal":
        with open_prepared_run(
            "001", store=store, transport_context=context
        ) as reopened:
            assert reopened.manifest.initial_transport_count == 4  # type: ignore[union-attr]
    else:
        with pytest.raises(PreparationError):
            open_prepared_run("001", store=store, transport_context=context)


def test_v3_configuration_remains_deliberate_and_has_no_historical_selection(
    tmp_path: Path,
) -> None:
    config, context = native_specification(tmp_path / "inputs")
    spec = load_run_spec(config)
    assert isinstance(spec, RunSpecV3)
    with prepare_run(
        spec, store=tmp_path / "runtime", run_id="001", transport_context=context
    ) as prepared:
        assert "output_selection" not in prepared.manifest.model_dump()


@pytest.mark.parametrize("location", ["root", "identity", "selection"])
def test_duplicate_receipt_keys_fail_even_when_the_last_value_is_valid(
    tmp_path: Path, location: str
) -> None:
    from judgmentllm.persistence.transport_receipts import read_transport_commitment

    config, context = native_specification(tmp_path / "inputs")
    with prepare_run(
        config, store=tmp_path / "runtime", run_id="001", transport_context=context
    ) as prepared:
        attempt = next(prepared.repository.iter_attempts("001"))
        path = prepared.store.reference_path(transport_reference(attempt.attempt_id))
        raw = path.read_bytes()
        if location == "root":
            raw = raw.replace(
                b'"renderer_version":3', b'"renderer_version":2,"renderer_version":3', 1
            )
        elif location == "identity":
            raw = raw.replace(
                b'"run_id":"001"',
                b'"run_id":"private-native-duplicate-canary","run_id":"001"',
                1,
            )
        else:
            raw = raw.replace(
                b'"policy":"native"',
                b'"policy":"private-native-duplicate-canary","policy":"native"',
                1,
            )
        path.write_bytes(raw)
        with pytest.raises(IntegrityError) as failure:
            read_transport_commitment(prepared.store, attempt.attempt_id)
        assert "private-native-duplicate-canary" not in str(failure.value)


def test_rehashed_standalone_receipt_cannot_replace_an_initial_commitment(
    tmp_path: Path,
) -> None:
    from judgmentllm.persistence.hashing import canonical_bytes, semantic_digest
    from judgmentllm.transport import TransportCommitmentV1, TransportPlanV1

    config, context = native_specification(tmp_path / "inputs")
    store = tmp_path / "runtime"
    with prepare_run(
        config, store=store, run_id="001", transport_context=context
    ) as prepared:
        attempt = next(prepared.repository.iter_attempts("001"))
        receipt = prepared.transport_for_attempt(attempt.attempt_id)
        changed = receipt.model_dump(mode="json", exclude={"commitment_sha256"})
        changed["context_sha256"] = "0" * 64
        changed["plan_sha256"] = TransportPlanV1.model_validate_json(
            json.dumps(
                {
                    key: changed[key]
                    for key in TransportPlanV1.model_fields
                    if key in changed
                }
            )
        ).plan_sha256
        changed["commitment_sha256"] = semantic_digest(
            "transport-commitment-v1", changed
        )
        replacement = TransportCommitmentV1.model_validate_json(json.dumps(changed))
        raw = canonical_bytes(replacement)
        artifact = prepared.store.put(raw, "application/json")
        prepared.repository.register_artifact("001", artifact)
        path = prepared.store.reference_path(transport_reference(attempt.attempt_id))
        path.unlink()
        prepared.store.publish_reference(
            artifact, transport_reference(attempt.attempt_id)
        )
    with pytest.raises((PreparationError, IntegrityError)):
        open_prepared_run("001", store=store, transport_context=context)


@pytest.mark.parametrize(
    "change",
    [
        "version_bool",
        "version_string",
        "unknown_field",
        "duplicate_root",
        "duplicate_nested",
    ],
)
def test_native_manifest_ingress_is_strict_and_sanitized(
    tmp_path: Path, change: str
) -> None:
    from judgmentllm.persistence.hashing import canonical_bytes

    config, context = native_specification(tmp_path / "inputs")
    with prepare_run(
        config, store=tmp_path / "runtime", run_id="001", transport_context=context
    ) as prepared:
        raw = canonical_bytes(prepared.manifest)
    if change == "version_bool":
        raw = raw.replace(b'"schema_version":3', b'"schema_version":true')
    elif change == "version_string":
        raw = raw.replace(b'"schema_version":3', b'"schema_version":"3"')
    elif change == "unknown_field":
        raw = (
            b'{"private-native-manifest-canary":"private-native-manifest-canary",'
            + raw[1:]
        )
    elif change == "duplicate_root":
        raw = raw.replace(
            b'"schema_version":3', b'"schema_version":2,"schema_version":3'
        )
    else:
        raw = raw.replace(
            b'"renderer_version":3',
            b'"private-native-manifest-canary":1,"private-native-manifest-canary":2,"renderer_version":3',
        )
    with pytest.raises(ValueError) as failure:
        ExecutableRunManifestV3.model_validate_json(raw)
    assert "private-native-manifest-canary" not in str(failure.value)
    if isinstance(failure.value, ValidationError):
        assert "private-native-manifest-canary" not in str(failure.value.errors())


def test_native_subset_rendering_inherits_the_original_route_without_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.execution.retry import subset_dependency

    config, context = native_specification(tmp_path / "inputs")
    with prepare_run(
        config, store=tmp_path / "runtime", run_id="001", transport_context=context
    ) as prepared:
        parent = next(prepared.iter_request_dependencies())
        subset = subset_dependency(parent, (parent.request.tasks[1].task_id,))

        def forbidden(*args: object, **kwargs: object) -> object:
            raise AssertionError("subset rendering must inherit transport")

        monkeypatch.setattr(
            "judgmentllm.transport._resolve_native_transport", forbidden
        )
        rendered = prepared.render_attempt(subset)
        from judgmentllm.native_prompts import RenderedPromptV3

        assert isinstance(rendered, RenderedPromptV3)
        assert (
            rendered.output_commitment.selection.mechanism
            == "openai.responses.text.format"
        )
        from judgmentllm.output_contracts import RubricOutputContract

        contract = rendered.output_commitment.contract
        assert isinstance(contract, RubricOutputContract)
        assert (
            tuple(trait.trait_id for trait in contract.traits)
            == subset.request.trait_ids
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "artifact_name", ["native_schema", "format_fragment", "output_commitment"]
)
@pytest.mark.parametrize("change", ["missing", "tampered"])
async def test_completed_subset_retry_requires_its_exact_retained_transport_artifacts(
    tmp_path: Path,
    artifact_name: str,
    change: str,
) -> None:
    from test_native_transport_integration import prepared_case, reopen

    from judgmentllm.execution import execute_run, recover_run
    from judgmentllm.execution.retry import authorize_subset_retry, subset_dependency
    from judgmentllm.persistence.repository import ReadOnlyRepository
    from judgmentllm.providers import MockMemberScenario, MockScenario

    adapter, store, spec = prepared_case(
        tmp_path,
        capture="full",
        scenario=MockScenario(
            members={"02": MockMemberScenario(outcome="invalid_category")}
        ),
        retry_valid=True,
    )
    await execute_run("001", store=store, adapter=adapter)
    with ReadOnlyRepository(store) as repository:
        failed = next(
            result
            for result in repository.iter_results("001")
            if result.status == "invalid"
        )
    retry = authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        task_ids=(failed.task_id,),
        operation_id="subset-artifact-integrity",
        spec=spec,
    )
    await recover_run("001", store=store, adapter=adapter)
    with reopen(adapter, store) as prepared:
        parent = prepared.dependency_for_request(retry.request_uid)
        subset = subset_dependency(parent, (failed.task_id,))
        receipt = prepared.transport_for_attempt(retry.attempt_id)
        wire = prepared.prepare_transport_attempt(
            subset,
            physical_identity("001", retry.request_uid, retry.attempt_id),
            frozen=receipt,
        )
        content = wire.content_artifacts()[artifact_name]
        artifact = prepared.repository.get_artifact("001", bytes_digest(content))
        assert artifact.relative_path is not None
        path = prepared.store.root / artifact.relative_path
    if change == "missing":
        path.unlink()
    else:
        path.write_bytes(content + b" ")
    with pytest.raises((IntegrityError, PreparationError, ExceptionGroup)):
        await recover_run("001", store=store, adapter=adapter)
    assert adapter.ledger.acceptance_count() == 2


@pytest.mark.parametrize(
    "field",
    [
        "run_spec_version",
        "output_contract_version",
        "output_commitment_version",
        "transport_commitment_version",
        "request_identity_version",
        "assessment_schema_version",
        "canonicalization_version",
        "serializer_version",
        "index_version",
    ],
)
def test_native_manifest_raw_version_tokens_cannot_be_normalized(
    tmp_path: Path, field: str
) -> None:
    from judgmentllm.persistence.hashing import canonical_bytes

    config, context = native_specification(tmp_path / "inputs")
    with prepare_run(
        config, store=tmp_path / "runtime", run_id="001", transport_context=context
    ) as prepared:
        data = prepared.manifest.model_dump(mode="json")
    data[field] = float(data[field])
    with pytest.raises(ValueError):
        ExecutableRunManifestV3.model_validate_json(canonical_bytes(data))
