"""Audit readers reject damaged evidence and preserve the earlier export cut."""

from pathlib import Path

import pytest
from test_batch_exports_retry import file_path
from test_batch_preparation_rejections import corrupt_row
from test_batch_rejections import prepared_batch as prepared_batch
from test_native_batch import batch_spec

from judgmentllm import (
    RunSpecV2,
    export_batch_provenance,
    prepare_batch,
    resume_batch,
    stop_batch,
)
from judgmentllm.batch_models import (
    AnthropicBatchHTTPDiagnosticV1,
    BatchDiagnosticV1,
    BatchStopV1,
)
from judgmentllm.execution.batch_exports import (
    BatchControlProvenanceV1,
    export_batch_audit,
)
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.native_batch import fact, record_stop
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run


def test_native_audit_rejects_an_ordinary_manifest(
    prepared_batch: tuple[RunSpecV2, Path],
) -> None:
    spec, root = prepared_batch
    spec.execution.mode = "live"
    with prepare_run(spec, store=root, run_id="ordinary"):
        pass
    with pytest.raises(ValueError, match="native batch manifest"):
        export_batch_audit("ordinary", root)


@pytest.mark.parametrize("fault", ["missing", "run", "bundle"])
def test_audit_reference_must_name_its_original_run_and_bundle(
    prepared_batch: tuple[RunSpecV2, Path],
    fault: str,
) -> None:
    _, root = prepared_batch
    original = export_batch_provenance("001", store=root)
    if fault != "missing":
        manifest = original.manifest.model_copy(
            update={"run_id": "other"} if fault == "run" else {"bundle_id": "other"}
        )
        with Repository(root) as repository:
            store = ArtifactStore(repository.run_directory("001"), create=False)
            artifact = store.put(canonical_bytes(manifest), "application/json")
            store.publish_reference(artifact, "batch/audits/forged.json")
    with pytest.raises(LifecycleError) as error:
        export_batch_provenance(
            "001", store=root, bundle_id="missing" if fault == "missing" else "forged"
        )
    assert error.value.category == "execution"
    assert (
        export_batch_provenance(
            "001", store=root, bundle_id=original.manifest.bundle_id
        )
        == original
    )


@pytest.mark.parametrize("fault", ["member", "operation", "control"])
def test_new_audit_rejects_corrupt_records_without_rewriting_old_bundle(
    prepared_batch: tuple[RunSpecV2, Path],
    fault: str,
) -> None:
    spec, root = prepared_batch
    resume_batch("001", store=root, spec=spec)
    stop_batch("001", store=root)
    original = export_batch_provenance("001", store=root)
    before = file_path(original, "results.jsonl").read_bytes()
    if fault == "member":
        corrupt_row(
            root,
            "batch_member_receipts",
            "UPDATE batch_member_receipts SET record_json=json_set(record_json,'$.identity.request_uid','other')",
        )
    elif fault == "operation":
        corrupt_row(
            root,
            "batch_operations",
            "UPDATE batch_operations SET record_json=json_set(record_json,'$.owner_token','other')",
        )
    else:
        corrupt_row(
            root,
            "batch_facts",
            "UPDATE batch_facts SET record_json=record_json || ' ' WHERE kind='stop'",
        )
    with pytest.raises(LifecycleError) as error:
        export_batch_provenance("001", store=root)
    assert error.value.category == "execution"
    reopened = export_batch_provenance(
        "001", store=root, bundle_id=original.manifest.bundle_id
    )
    assert file_path(reopened, "results.jsonl").read_bytes() == before


@pytest.mark.parametrize(
    "run_id",
    [
        pytest.param("openai-diagnostic:call:" + "0" * 32, id="openai-call"),
        pytest.param("openai-result-screen:" + "0" * 64, id="openai-screen"),
        pytest.param("anthropic-http:" + "0" * 32, id="anthropic-http"),
    ],
)
def test_audit_accepts_run_scoped_facts_under_reserved_prefix(
    tmp_path: Path, run_id: str
) -> None:
    root = tmp_path / "store"
    prepare_batch(batch_spec(tmp_path, responses=1), store=root, run_id=run_id)
    with Repository(root) as repository, ExecutorOwnership(repository, run_id) as owner:
        assert owner.owner is not None
        record_stop(repository, run_id, owner.owner.token, remote=True)
        fact(
            repository,
            run_id,
            owner.owner.token,
            "withheld",
            run_id,
            BatchDiagnosticV1(code="unsafe_evidence"),
        )

    audit = export_batch_provenance(run_id, store=root)
    controls = [
        BatchControlProvenanceV1.model_validate_json(line)
        for line in file_path(audit, "controls.jsonl").read_bytes().splitlines()
    ]
    assert [control.kind for control in controls] == ["cancel_all", "stop", "withheld"]
    assert all(control.entity_id == run_id for control in controls)


@pytest.mark.parametrize("fault", ["other_entity", "other_kind", "tag", "stop_binding"])
def test_audit_rejects_reserved_facts_without_run_scope(
    tmp_path: Path, fault: str
) -> None:
    call_id = "0" * 32
    run_id = "anthropic-http:" + call_id
    root = tmp_path / "store"
    prepare_batch(batch_spec(tmp_path, responses=1), store=root, run_id=run_id)
    kind, entity = "withheld", run_id
    record: BatchDiagnosticV1 | AnthropicBatchHTTPDiagnosticV1 | BatchStopV1 = (
        BatchDiagnosticV1(code="unsafe_evidence")
    )
    message = "Anthropic Batch HTTP diagnostic namespace differs"
    if fault == "other_entity":
        entity += "-other"
    elif fault == "other_kind":
        kind = "conflict"
    elif fault == "tag":
        record = AnthropicBatchHTTPDiagnosticV1(
            call_id=call_id,
            stage="create",
            phase="headers",
            intent_id="0" * 64,
            http_status=200,
        )
        message = "Anthropic Batch HTTP diagnostic provider differs"
    else:
        kind = "stop"
        record = BatchStopV1(run_id="other-run", kind="stop")
        message = "batch stop fact binding differs"
    with Repository(root) as repository, ExecutorOwnership(repository, run_id) as owner:
        assert owner.owner is not None
        fact(repository, run_id, owner.owner.token, kind, entity, record)

    with pytest.raises(LifecycleError) as error:
        export_batch_provenance(run_id, store=root)
    assert isinstance(error.value.__context__, IntegrityError)
    assert error.value.__context__.args == (message,)


def test_pairwise_audit_exports_the_presented_and_original_identities(
    tmp_path: Path,
) -> None:
    import csv

    from test_batch_r import make_audit

    report = make_audit(tmp_path, "batch-pairwise")
    with file_path(report, "results_r.csv").open(
        encoding="utf-8", newline=""
    ) as stream:
        records = list(csv.DictReader(stream))
    assert records and all(row["run_id"] == "001" for row in records)
    assert {row["left_item_id"] for row in records} | {
        row["right_item_id"] for row in records
    } == {"001", "002"}
    assert all(row["status"] == "complete" for row in records)
