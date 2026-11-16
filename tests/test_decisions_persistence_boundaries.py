"""Real files and current DTOs cannot substitute frozen native evidence."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, cast

import pytest
from test_decisions_contracts import pair_spec, presentation
from test_decisions_lifecycle import native_spec
from test_decisions_validation import native_request

import judgmentllm.preparation as preparation
from judgmentllm.config import InputSpec, RunSpecV4
from judgmentllm.decisions.outcomes import (
    DecisionsOutcomeVectorV1,
    outcome_reference,
    publish_outcomes,
    read_outcomes,
)
from judgmentllm.evidence.usage import ProviderUsage
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.decisions_invocations import (
    DecisionsInvocationV1,
    invocation_reference,
    publish_invocation,
    read_invocation,
)
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
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import CapabilityProfile
from judgmentllm.preparation import PreparationError, prepare_run
from judgmentllm.preparation.decisions import DecisionsPreparedRun, presentation_records
from judgmentllm.preparation.manifest import (
    ExecutableRunManifestV8,
    PresentationSourceCommitmentV1,
)


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
