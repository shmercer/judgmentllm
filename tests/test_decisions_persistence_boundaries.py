"""Real files and current DTOs cannot substitute frozen native evidence."""

from __future__ import annotations

import csv
import io
import json
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from test_decisions_contracts import pair_spec, presentation
from test_decisions_lifecycle import native_spec
from test_decisions_validation import native_request
from test_preparation_rendering import specification
from test_typesafe_decisions import intercept_jev, jev_envelope, jev_spec, sealed_jev

import judgmentllm.preparation as preparation
from judgmentllm.config import InputSpec, RunSpecV4
from judgmentllm.decisions.outcomes import (
    DecisionsOutcomeVectorV1,
    outcome_reference,
    publish_outcomes,
    read_outcomes,
)
from judgmentllm.decisions.provenance import (
    decisions_status,
    export_decisions_provenance,
)
from judgmentllm.decisions.transport import PreparedDecisionsRequestV3
from judgmentllm.evidence.usage import ProviderUsage
from judgmentllm.execution.session import ExecutionSession
from judgmentllm.lifecycle import get_run, run_live
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.decisions_invocations import (
    DecisionsInvocationV1,
    invocation_reference,
    publish_invocation,
    read_invocation,
)
from judgmentllm.persistence.evidence import (
    DecisionsEvidenceReceiptV3,
    DecisionsEvidenceReceiptV4,
    EvidenceReceipt,
    read_evidence_receipt,
)
from judgmentllm.persistence.generation import build_generation_commitment
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.models import (
    ArtifactIntegrityError,
    IntegrityError,
    RunRecord,
)
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.presentations import (
    add_presentation,
    get_presentation,
    iter_presentations,
    verify_members,
)
from judgmentllm.persistence.receipts import (
    AcknowledgmentReceiptV3,
    AcknowledgmentReceiptV4,
    CompleteResponseEvidenceV3,
    GeminiDecisionsRejectionEvidenceV1,
    TypeSafeRejectionEvidenceV1,
    publish_acknowledgment,
    read_acknowledgment,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import CapabilityProfile
from judgmentllm.preparation import PreparationError, prepare_run
from judgmentllm.preparation.decisions import DecisionsPreparedRun, presentation_records
from judgmentllm.preparation.manifest import (
    ExecutableRunManifestV8,
    ExecutableRunManifestV10,
    PresentationSourceCommitmentV1,
    parse_executable_manifest,
)
from judgmentllm.providers.decisions_mock import MockDecisionsAdapter
from judgmentllm.providers.mock import MockAdapter
from judgmentllm.providers.protocol import TypeSafeRequestRejected
from judgmentllm.providers.typesafe_decisions import TypeSafeDecisionsAdapter


def forwarding_fact(tmp_path: Path) -> DecisionsInvocationV1:
    _, _, wire = native_request(tmp_path)
    identity = wire.transport_commitment.identity
    usage = ProviderUsage(
        run_id=identity.run_id,
        request_uid=identity.request_uid,
        attempt_id=identity.attempt_id,
        submission_id=identity.submission_id,
        observation_id=None,
        provider="mock",
        endpoint="synthetic",
        requested_model=wire.model,
        observation_kind="invocation",
    )
    return DecisionsInvocationV1(
        identity=identity, payload_sha256=wire.payload_sha256, usage=usage
    )


@pytest.mark.parametrize("family", ["outcomes", "invocation"])
@pytest.mark.parametrize(
    "state", ["missing", "unavailable", "malformed", "noncanonical", "foreign", "valid"]
)
def test_named_evidence_readers_require_exact_original_bytes(
    tmp_path: Path, family: str, state: str
) -> None:
    fact = forwarding_fact(tmp_path)
    vector = DecisionsOutcomeVectorV1(
        identity=fact.identity,
        observation_id="observation",
        response_sha256="c" * 64,
        commitment_sha256="d" * 64,
        recognizable=True,
        whole_response_valid=True,
        unexpected_members=0,
        unattributable_members=0,
        answers=(),
        result_sha256=(),
    )
    record = vector if family == "outcomes" else fact
    store = ArtifactStore(tmp_path / "artifacts")
    read = read_outcomes if family == "outcomes" else read_invocation
    ref = outcome_reference if family == "outcomes" else invocation_reference
    path = store.reference_path(ref(fact.identity.submission_id))
    if state == "missing":
        assert read(store, fact.identity.submission_id) is None
        return
    if state == "valid":
        publish_outcomes(store, vector) if family == "outcomes" else publish_invocation(
            store, fact
        )
        assert read(store, fact.identity.submission_id) == record
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if state == "unavailable":
        path.mkdir()
    else:
        data = record.model_dump(mode="json")
        if state == "foreign":
            data["identity"]["submission_id"] = "foreign"
            if family == "invocation":
                data["usage"]["submission_id"] = "foreign"
        raw = (
            b"{"
            if state == "malformed"
            else canonical_bytes(data) + (b"\n" if state == "noncanonical" else b"")
        )
        path.write_bytes(raw)
    with pytest.raises(ArtifactIntegrityError):
        read(store, fact.identity.submission_id)


def test_forwarding_fact_cannot_invent_observation_or_acceptance(
    tmp_path: Path,
) -> None:
    fact = forwarding_fact(tmp_path)
    data = fact.model_dump()
    data["usage"]["request_uid"] = "foreign"
    with pytest.raises(ValueError, match="forwarding fact"):
        DecisionsInvocationV1.model_validate(data)


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_csv_presentation_and_shared_template_use_ordinary_preparation(
    tmp_path: Path, capture: str
) -> None:
    spec = pair_spec(tmp_path)
    record = presentation().model_dump(mode="json")
    path = tmp_path / "presentations.csv"
    header = list(record)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerow(
            [
                json.dumps(record[k])
                if k
                in {
                    "schema_version",
                    "pair_presentation_version",
                    "members",
                    "schedule",
                }
                else record[k]
                for k in header
            ]
        )
    template = tmp_path / "question.jinja"
    template.write_text(
        "{{ rubric }}\n{{ exemplars }}\n{{ output_instructions }}", encoding="utf-8"
    )
    spec = RunSpecV4.model_validate(
        {
            **spec.model_dump(),
            "capture": capture,
            "pair_presentations": path,
            "prompt": {"file": template},
        }
    )
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        assert isinstance(prepared, DecisionsPreparedRun)
        assert prepared.get_presentation("comparison-001") == presentation()
        dependencies = list(prepared.iter_request_dependencies(start_ordinal=1))
        assert (
            len(dependencies) == 3
            and sum(len(d.request.tasks) for d in dependencies) == 9
        )
        with pytest.raises(ValueError):
            list(prepared.iter_request_dependencies(start_ordinal=-1))
        with pytest.raises(PreparationError):
            prepared.dependency_for_request("foreign")
        with pytest.raises(PreparationError):
            prepared.verify_compatibility(capabilities=CapabilityProfile(grouped=False))
        with pytest.raises(PreparationError):
            prepared.verify_compatibility(
                capabilities=CapabilityProfile(modes=("batch_native",))
            )
        prepared.transport_context = cast(Any, object())
        with pytest.raises(PreparationError, match="context"):
            prepared.verify_compatibility()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"wrong\n",
        b"source_presentation_id,pair_id,left_item_id,right_item_id,members\na,b,c,d\n",
    ],
)
def test_invalid_presentation_csv_never_projects_members(raw: bytes) -> None:
    reference = InputSpec(path=Path("synthetic.csv"), format="csv")
    with pytest.raises(PreparationError):
        list(presentation_records(io.BytesIO(raw), reference))


@pytest.mark.parametrize(
    "case",
    [
        "version",
        "renderer",
        "origin",
        "full_retention",
        "minimal_retention",
        "sources",
        "pair_source",
    ],
)
def test_native_manifest_version_authority_and_retention_are_strict(
    tmp_path: Path, case: str
) -> None:
    spec = native_spec(tmp_path)
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        data: dict[str, Any] = prepared.manifest.model_dump()
    if case == "version":
        data["schema_version"] = True
    elif case == "renderer":
        data["question_renderer_version"] = True
    elif case == "origin":
        data["configuration_origin"] = "file"
    elif case == "full_retention":
        data["recovery_policy"] = "unchanged_originals"
    elif case == "minimal_retention":
        data["capture"] = "minimal"
    elif case == "sources":
        data["sources"] = ()
    else:
        data["pair_presentations"] = spec.input
    with pytest.raises(ValueError):
        ExecutableRunManifestV8.model_validate(data)
    if case == "version":
        with pytest.raises(ValueError):
            PresentationSourceCommitmentV1.model_validate(
                dict(presentation_source_commitment_version=True)
            )


@pytest.mark.parametrize("source", ["python", "file"])
def test_legacy_transport_context_cannot_gain_v4_authority(
    tmp_path: Path, source: str
) -> None:
    spec = native_spec(tmp_path)
    candidate: RunSpecV4 | Path = spec
    if source == "file":
        candidate = tmp_path / "run.json"
        candidate.write_bytes(canonical_bytes(spec))
    with pytest.raises(PreparationError, match="legacy transport"):
        prepare_run(
            candidate,
            store=tmp_path / "store",
            run_id="001",
            transport_context=cast(Any, object()),
        )


def test_duplicate_run_and_mutated_discovery_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    with pytest.raises(PreparationError, match="exists"):
        prepare_run(spec, store=store, run_id="001")
    config = tmp_path / "run.json"
    config.write_bytes(canonical_bytes(spec))
    original = preparation._memory_source  # pyright: ignore[reportPrivateUsage]
    root_reads = 0

    def race(path: Path) -> Any:
        nonlocal root_reads
        result = original(path)
        if path == config:
            root_reads += 1
            if root_reads == 2:
                path.write_bytes(result[0] + b"\n")
        return result

    monkeypatch.setattr(preparation, "_memory_source", race)
    with pytest.raises(PreparationError, match="changed"):
        prepare_run(config, store=tmp_path / "raced", run_id="001")


def test_native_source_index_rejects_invalid_current_locators_and_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.preparation.decisions as native

    spec = pair_spec(tmp_path)
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        assert isinstance(prepared, DecisionsPreparedRun)
        index = get_presentation(prepared.repository, "001", "comparison-001")
        with pytest.raises(ValueError):
            list(iter_presentations(prepared.repository, "001", page_size=0))
        with pytest.raises(IntegrityError):
            get_presentation(prepared.repository, "001", "foreign")
        with pytest.raises(IntegrityError):
            verify_members(prepared.repository, index, presentation(criteria=2))
        wrong = index.model_copy(update={"record_sha256": "f" * 64})
        with ExecutorOwnership(prepared.repository, "001") as owner:
            assert owner.owner is not None
            with pytest.raises(IntegrityError):
                add_presentation(
                    prepared.repository, wrong, presentation(), owner.owner.token
                )
            past_end = index.model_copy(update={"byte_length": 999_999})
            with pytest.raises(IntegrityError):
                add_presentation(
                    prepared.repository, past_end, presentation(), owner.owner.token
                )
        with monkeypatch.context() as patch:

            def truncated(*_args: Any) -> Any:
                return past_end

            patch.setattr(native, "get_presentation", truncated)
            with pytest.raises(PreparationError, match="truncated"):
                prepared.get_presentation("comparison-001")
        with monkeypatch.context() as patch:

            def changed_index(*_args: Any) -> Any:
                return wrong

            patch.setattr(native, "get_presentation", changed_index)
            with pytest.raises(PreparationError, match="source changed"):
                prepared.get_presentation("comparison-001")
    with Repository(tmp_path / "marker") as repository:
        with pytest.raises(IntegrityError):
            repository.create_run(
                RunRecord(
                    run_id="001",
                    task_kind="rubric",
                    provider="mock",
                    endpoint="synthetic",
                    model="synthetic",
                ),
                decisions_version=5,
            )


@pytest.mark.parametrize(
    "fault", ["manifest_version", "manifest_model", "strict", "wire_version"]
)
def test_jev_sealed_configuration_requires_exact_versions_and_scope(
    tmp_path: Path, fault: str
) -> None:
    wire = sealed_jev(tmp_path)
    if fault == "wire_version":
        data = wire.model_dump()
        assert PreparedDecisionsRequestV3.model_validate(data) == wire
        data["preparation_version"] = "10"
        with pytest.raises(ValueError, match="preparation_version must be integer 10"):
            PreparedDecisionsRequestV3.model_validate(data)
        return

    with Repository(tmp_path / "store") as repository:
        raw = (repository.run_directory("001") / "manifest.json").read_bytes()
    manifest = parse_executable_manifest(raw)
    assert isinstance(manifest, ExecutableRunManifestV10)
    data = manifest.model_dump()
    assert ExecutableRunManifestV10.model_validate(data) == manifest
    if fault == "manifest_version":
        data["schema_version"] = "10"
        diagnostic = "schema_version must be integer 10"
    else:
        if fault == "strict":
            data["strict_capabilities"] = True
        else:
            foreign = jev_spec(native_spec(tmp_path / "foreign"), model="jev-1.13.0")
            data["generation"] = build_generation_commitment(foreign)
        diagnostic = "TypeSafe Decisions manifest exceeds approved scope"
    with pytest.raises(ValueError, match=diagnostic):
        ExecutableRunManifestV10.model_validate(data)


@pytest.fixture
def committed_jev_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str, DecisionsEvidenceReceiptV4]:
    raw = b"provider body-size rejection: UNSAFE_JEV_CANARY"
    forwards: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        forwards.append(request.content)
        return httpx2.Response(413, content=raw)

    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-jev-key")
    intercept_jev(monkeypatch, respond)
    store = tmp_path / "store"
    result = run_live(jev_spec(native_spec(tmp_path / "input")), store=store)
    assert not result.recovery_required
    assert result.decisions is not None
    (diagnostic,) = result.decisions.model_dump()["diagnostics"]
    assert diagnostic["code"] == "payload_too_large"
    assert "HTTP body was too large" in diagnostic["message"]
    assert len(forwards) == 1
    with Repository(store) as repository:
        (receipt,) = repository.iter_evidence(result.run_id)
        assert (
            isinstance(receipt, DecisionsEvidenceReceiptV4)
            and receipt.rejection is not None
        )
        assert receipt.rejection.response_sha256 == sha256(raw).hexdigest()
        assert receipt.rejection.response_byte_length == len(raw)
        assert receipt.correlation_id == receipt.rejection.correlation_id
        assert receipt.retained is None and receipt.usage.input_tokens is None
        assert [row.status for row in repository.iter_results(result.run_id)] == [
            "errored"
        ] * 3
    return store, result.run_id, receipt


@pytest.mark.parametrize(
    "fault",
    [
        "receipt_version",
        "missing_rejection",
        "mismatched_rejection",
        "reason_status",
        "usage",
    ],
)
def test_jev_rejection_ingress_rechecks_versions_binding_and_usage(
    committed_jev_rejection: tuple[Path, str, DecisionsEvidenceReceiptV4], fault: str
) -> None:
    _, _, receipt = committed_jev_rejection
    assert receipt.rejection is not None
    data = receipt.model_dump()
    assert DecisionsEvidenceReceiptV4.model_validate(data) == receipt
    if fault == "reason_status":
        with pytest.raises(ValueError, match="reason conflicts with HTTP status"):
            TypeSafeRejectionEvidenceV1.model_validate(
                {**receipt.rejection.model_dump(), "reason": "request_rejected"}
            )
        return
    if fault == "usage":
        with pytest.raises(ValueError, match="Invalid evidence record"):
            ProviderUsage.model_validate(
                {
                    **receipt.usage.model_dump(),
                    "total_tokens": 1,
                    "usage_source": "typesafe.systemone.usage",
                }
            )
        return
    if fault == "receipt_version":
        data["evidence_receipt_version"] = "4"
    elif fault == "missing_rejection":
        del data["rejection"]
    else:
        data["rejection"]["correlation_id"] = "valid-but-foreign"
        # The rejection is valid alone and conflicts only at its receipt binding.
        assert (
            TypeSafeRejectionEvidenceV1.model_validate(data["rejection"]).correlation_id
            == "valid-but-foreign"
        )
    with pytest.raises(ValueError, match="Invalid evidence record"):
        DecisionsEvidenceReceiptV4.model_validate(data)


@pytest.mark.asyncio
async def test_jev_rejection_write_reuses_only_the_same_durable_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = jev_spec(native_spec(tmp_path / "input"))
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-jev-key")
    forwards: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        forwards.append(request.content)
        return httpx2.Response(413)

    def stop(stage: str, _request: str) -> None:
        if stage == "after_failure_evidence_receipt":
            raise RuntimeError("interrupted rejection write")

    adapter = TypeSafeDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    session = ExecutionSession("001", store, adapter, spec, False, stop)
    try:
        work = session.next_work()
        assert work is not None and session.boundary(work)
        with pytest.raises(TypeSafeRequestRejected) as rejected:
            await adapter.submit(
                work.prepared,
                submission_id=work.submission_id,
                correlation_id=work.correlation_id,
            )
        rejection = rejected.value.rejection
        with pytest.raises(RuntimeError, match="interrupted rejection write"):
            session.not_submitted(work, invoked=True, rejection=rejection)
        assert session.prepared is not None
        artifacts = session.prepared.store
        original = read_evidence_receipt(artifacts, work.submission_id)
        assert (
            isinstance(original, DecisionsEvidenceReceiptV4)
            and original.rejection == rejection
        )
        assert session.repository.usage_high_water_mark("001") == 0

        different = rejection.model_copy(update={"response_sha256": "f" * 64})
        with pytest.raises(
            IntegrityError, match="TypeSafe rejection evidence conflicts"
        ):
            session.not_submitted(work, invoked=True, rejection=different)
        assert read_evidence_receipt(artifacts, work.submission_id) == original
        assert session.repository.usage_high_water_mark("001") == 0
        assert list(session.repository.iter_results("001")) == []

        session.not_submitted(work, invoked=True, rejection=rejection)
        assert (
            session.repository.get_evidence_for_submission("001", work.submission_id)
            == original
        )
        assert session.repository.usage_high_water_mark("001") == 1
        assert [r.status for r in session.repository.iter_results("001")] == [
            "errored"
        ] * 3
    finally:
        session.close()
        await adapter.aclose()
    assert forwards == [work.prepared.payload]
    status = get_run("001", store=store)
    assert status.decisions is not None
    (diagnostic,) = status.decisions.model_dump()["diagnostics"]
    assert diagnostic["code"] == "payload_too_large"


def test_jev_rejection_cannot_enter_a_historical_decisions_session(
    tmp_path: Path,
) -> None:
    _check_historical_session_rejects_foreign_rejection(
        tmp_path,
        TypeSafeRejectionEvidenceV1,
        "TypeSafe rejection exceeds the sealed provider scope",
    )


def test_gemini_rejection_cannot_enter_a_historical_decisions_session(
    tmp_path: Path,
) -> None:
    _check_historical_session_rejects_foreign_rejection(
        tmp_path,
        GeminiDecisionsRejectionEvidenceV1,
        "Gemini Decisions rejection exceeds the sealed provider scope",
    )


def _check_historical_session_rejects_foreign_rejection(
    tmp_path: Path,
    evidence_type: type[TypeSafeRejectionEvidenceV1]
    | type[GeminiDecisionsRejectionEvidenceV1],
    diagnostic: str,
) -> None:
    spec = native_spec(tmp_path / "input")
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockDecisionsAdapter(store / "mock-service.sqlite3")
    session = ExecutionSession("001", store, adapter, spec, False, None)
    try:
        work = session.next_work()
        assert work is not None and session.boundary(work)
        rejection = evidence_type(
            run_id="001",
            request_uid=work.prepared.request_uid,
            attempt_id=work.attempt_id,
            submission_id=work.submission_id,
            correlation_id=work.correlation_id,
            payload_sha256=work.prepared.payload_sha256,
            response_sha256="a" * 64,
            response_byte_length=16,
            http_status=413,
            reason="payload_too_large",
        )
        with pytest.raises(IntegrityError, match=diagnostic):
            session.not_submitted(work, invoked=True, rejection=rejection)
        assert session.prepared is not None
        assert read_evidence_receipt(session.prepared.store, work.submission_id) is None
        assert session.repository.usage_high_water_mark("001") == 0
        assert list(session.repository.iter_results("001")) == []
    finally:
        session.close()


@pytest.mark.asyncio
async def test_jev_export_and_resume_reject_wrong_acknowledgment_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = jev_spec(native_spec(tmp_path / "input"))
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-jev-key")

    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=jev_envelope(request))

    adapter = TypeSafeDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    session = ExecutionSession("001", store, adapter, spec, False, None)
    try:
        work = session.next_work()
        assert work is not None and session.boundary(work)
        session.acknowledge(
            work,
            await adapter.submit(
                work.prepared,
                submission_id=work.submission_id,
                correlation_id=work.correlation_id,
            ),
        )
        submission = session.repository.get_submission("001", work.submission_id)
        assert submission.certainty == "accepted"
        assert (
            session.repository.get_evidence_for_submission("001", work.submission_id)
            is None
        )
        assert session.prepared is not None
        artifacts = session.prepared.store
        receipt = read_acknowledgment(artifacts, work.submission_id)
        assert isinstance(receipt, AcknowledgmentReceiptV4)
        with pytest.raises(
            ValueError, match="unsupported TypeSafe acknowledgment version"
        ):
            AcknowledgmentReceiptV4.model_validate(
                {**receipt.model_dump(), "acknowledgment_version": "4"}
            )
        acceptance = receipt.response_evidence.model_dump()
        with pytest.raises(
            ValueError, match="unsupported TypeSafe acceptance evidence version"
        ):
            CompleteResponseEvidenceV3.model_validate(
                {**acceptance, "acceptance_evidence_version": "3"}
            )

        foreign = AcknowledgmentReceiptV3.model_validate(
            {
                **receipt.model_dump(),
                "acknowledgment_version": 3,
                "response_evidence": {
                    **acceptance,
                    "acceptance_evidence_version": 2,
                    "protocol": "openai-decisions-production-v1",
                },
            }
        )
        (reference,) = (artifacts.root / "acknowledgments").glob("*.json")
        reference.unlink()
        publish_acknowledgment(artifacts, foreign)
        assert read_acknowledgment(artifacts, work.submission_id) == foreign
        foreign.verify_submission(submission)
        with pytest.raises(
            IntegrityError, match="Decisions acknowledgment protocol differs"
        ):
            session.saved_acknowledgment(work)
    finally:
        session.close()
        await adapter.aclose()

    with pytest.raises(IntegrityError, match="Decisions acknowledgment scope differs"):
        export_decisions_provenance("001", store=store)


def test_historical_saved_acknowledgment_cannot_adopt_jev_acceptance(
    tmp_path: Path,
) -> None:
    spec = specification(tmp_path / "input", responses=1, traits=1)
    spec.provider, spec.endpoint = "mock", "synthetic"
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockAdapter(store / "mock.sqlite3")
    session = ExecutionSession("001", store, adapter, spec, False, None)
    try:
        work = session.next_work()
        assert work is not None and session.boundary(work)
        assert session.prepared is not None
        binding = dict(
            run_id="001",
            attempt_id=work.attempt_id,
            submission_id=work.submission_id,
            correlation_id=work.correlation_id,
            payload_sha256=work.prepared.payload_sha256,
        )
        acceptance = CompleteResponseEvidenceV3.model_validate(
            {
                **binding,
                "request_uid": work.prepared.request_uid,
                "response_sha256": "a" * 64,
                "response_byte_length": 16,
            }
        )
        foreign = AcknowledgmentReceiptV4.model_validate(
            {
                **binding,
                "remote_id": None,
                "observation_key": "foreign",
                "response_evidence": acceptance,
            }
        )
        artifacts = session.prepared.store
        publish_acknowledgment(artifacts, foreign)
        assert read_acknowledgment(artifacts, work.submission_id) == foreign
        foreign.verify_submission(
            session.repository.get_submission("001", work.submission_id)
        )
        with pytest.raises(
            IntegrityError,
            match="historical attempt cannot acquire Decisions acceptance",
        ):
            session.saved_acknowledgment(work)
        assert list(session.repository.iter_results("001")) == []
    finally:
        session.close()


@pytest.mark.parametrize(
    "case", ["export_foreign_evidence_version", "status_missing_rejection_evidence"]
)
def test_jev_provenance_requires_its_own_committed_evidence(
    committed_jev_rejection: tuple[Path, str, DecisionsEvidenceReceiptV4],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    store, run_id, receipt = committed_jev_rejection
    if case == "export_foreign_evidence_version":
        data = receipt.model_dump(exclude={"rejection"})
        data["evidence_receipt_version"] = 3
        data["usage"].update(provider="openai", endpoint="decisions")
        replacement: EvidenceReceipt | None = DecisionsEvidenceReceiptV3.model_validate(
            data
        )
    else:
        replacement = None

    original = Repository.get_evidence_for_submission
    inspected: list[str] = []

    def evidence(
        repository: Repository, identifier: str, submission_id: str
    ) -> EvidenceReceipt | None:
        if (identifier, submission_id) == (run_id, receipt.identity.submission_id):
            assert original(repository, identifier, submission_id) == receipt
            inspected.append(submission_id)
            return replacement
        return original(repository, identifier, submission_id)

    # The real reader verifies cross-provider linkage first. The substitute
    # exercises the independent provenance guard with the actual ledger row.
    monkeypatch.setattr(Repository, "get_evidence_for_submission", evidence)
    if case == "export_foreign_evidence_version":
        with pytest.raises(IntegrityError, match="Decisions evidence scope differs"):
            export_decisions_provenance(run_id, store=store)
    else:
        with Repository(store) as repository:
            manifest = parse_executable_manifest(
                (repository.run_directory(run_id) / "manifest.json").read_bytes()
            )
            assert isinstance(manifest, ExecutableRunManifestV10)
            with pytest.raises(
                IntegrityError, match="TypeSafe rejection lacks its evidence receipt"
            ):
                decisions_status(repository, manifest)
    assert inspected == [receipt.identity.submission_id]
