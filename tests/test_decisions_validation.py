"""Native authorities and crosswalks fail closed before any physical forwarding."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_decisions_contracts import pair_spec, presentation
from test_decisions_lifecycle import native_spec

from judgmentllm import decisions_types
from judgmentllm.config import (
    AutoRequestGroupingV1,
    QuestionPromptSpecV1,
    RunSpecV4,
    SingleRequestGroupingV1,
)
from judgmentllm.decisions.records import (
    DecisionQuestionV1,
    DecisionsLimitsV1,
    PairPresentationInputV1,
    PreparedPairPresentationV1,
    QuestionCommitmentV1,
    project_presentation,
    question_key,
)
from judgmentllm.decisions.transport import (
    DecisionsCommitmentV1,
    DecisionsContextV1,
    DecisionsDependencyV1,
    PreparedDecisionsRequestV1,
    plan_source,
    prepare_request,
    render_question,
)
from judgmentllm.models import ResponseItem
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.receipts import (
    AcknowledgmentReceiptV2,
    CompleteResponseEvidenceV1,
)
from judgmentllm.planning import CapabilityProfile, plan_run, stable_id, validate_run
from judgmentllm.preview import RenderedDecisionsPreviewV1, preview_prompt
from judgmentllm.transport import physical_identity


def native_request(
    tmp_path: Path,
) -> tuple[RunSpecV4, DecisionsDependencyV1, PreparedDecisionsRequestV1]:
    spec = native_spec(tmp_path)
    record = ResponseItem(item_id="000", text="Synthetic source 0.")
    context = DecisionsContextV1(service_identity="offline")
    dependency = next(plan_source(spec, record, lambda _: record, context, None))
    wire = prepare_request(
        spec,
        dependency,
        physical_identity("001", dependency.request.request_uid, "attempt-01"),
        context,
    )
    return spec, dependency, wire


@pytest.mark.parametrize("kind", [SingleRequestGroupingV1, AutoRequestGroupingV1])
def test_grouping_version_is_not_coerced(
    kind: type[SingleRequestGroupingV1] | type[AutoRequestGroupingV1],
) -> None:
    with pytest.raises(ValueError, match="integer"):
        kind.model_validate(
            dict(
                grouping_version=True,
                mode="single" if kind is SingleRequestGroupingV1 else "auto",
                max_questions_per_request=1,
            )
        )


@pytest.mark.parametrize(
    "case,message",
    [
        ("version", "integer 4"),
        ("ties", "forced choice"),
        ("explanations", "explanations"),
        ("advanced", "prompt.file"),
        ("generation", "generation controls"),
        ("route", "Decisions route"),
        ("rubric", "only a rubric"),
        ("pair_source", "exactly one pair source"),
        ("duplicate_criterion", "duplicate criterion"),
        ("exemplar_map", "explicitly cover"),
        ("unknown_exemplar", "unique known references"),
        ("duplicate_exemplar", "unique known references"),
        ("mapping_type", "dictionary"),
    ],
)
def test_v4_has_one_complete_configuration_authority(
    tmp_path: Path, case: str, message: str
) -> None:
    data: dict[str, Any] = (
        pair_spec(tmp_path)
        if case in {"pair_source", "duplicate_criterion"}
        else native_spec(tmp_path)
    ).model_dump()
    if case == "version":
        data["schema_version"] = True
    elif case == "ties":
        data["allow_ties"] = True
    elif case == "explanations":
        data["execution"]["explanations"] = True
    elif case == "advanced":
        data["prompt"]["advanced"] = True
    elif case == "generation":
        data["generation"] = {"temperature": 1}
    elif case == "route":
        data["endpoint"] = "generative"
    elif case == "rubric":
        data["rubric"] = None
    elif case == "pair_source":
        data["pair_presentations"] = None
    elif case == "duplicate_criterion":
        data["criteria"].append(data["criteria"][0])
    elif case == "mapping_type":
        data["prompt"]["exemplar_ids_by_trait"] = []
    else:
        data["exemplars"] = [
            dict(exemplar_id="example", text="Synthetic exemplar.", scores={})
        ]
        data["prompt"]["exemplar_ids_by_trait"] = (
            {}
            if case == "exemplar_map"
            else {t["trait_id"]: [] for t in data["rubric"]["traits"]}
        )
        if case == "unknown_exemplar":
            data["prompt"]["exemplar_ids_by_trait"]["00"] = ["foreign"]
        if case == "duplicate_exemplar":
            data["prompt"]["exemplar_ids_by_trait"]["00"] = ["example", "example"]
    with pytest.raises(ValueError, match=message):
        RunSpecV4.model_validate(data)


@pytest.mark.parametrize(
    "case", ["version", "same_items", "no_members", "duplicate_member"]
)
def test_source_presentation_identity_is_strict(case: str) -> None:
    data = presentation().model_dump()
    if case == "version":
        data["pair_presentation_version"] = True
    elif case == "same_items":
        data["right_item_id"] = data["left_item_id"]
    elif case == "no_members":
        data["members"] = ()
    else:
        data["members"] = (*data["members"], data["members"][0])
    with pytest.raises(ValueError):
        PairPresentationInputV1.model_validate(data)


@pytest.mark.parametrize(
    "case", ["empty", "presentation", "duplicate", "source_uid", "common_schedule"]
)
def test_prepared_members_cannot_claim_independent_schedules(case: str) -> None:
    prepared = next(project_presentation(presentation()))
    data = prepared.model_dump(mode="json")
    if case == "empty":
        data["members"] = []
    elif case == "presentation":
        data["presentation_id"] = "foreign"
    elif case == "duplicate":
        data["members"] = [data["members"][0], data["members"][0]]
    elif case == "source_uid":
        data["members"][0]["pair_uid"] = "foreign"
    else:
        member = data["members"][1]
        member["pair_id"] = "other-pair"
        member["pair_uid"] = stable_id(
            "pair",
            member["source_pair_uid"],
            member["pair_id"],
            member["left_item_id"],
            member["right_item_id"],
            member["trait_id"],
            member["repetition_index"],
            member["is_reversal"],
        )
        member["reversal_group"] = stable_id(
            "presentation-reversal-v1",
            prepared.source_presentation_id,
            member["pair_id"],
            member["repetition_index"],
        )
    with pytest.raises(ValueError):
        PreparedPairPresentationV1.model_validate_json(canonical_bytes(data))


@pytest.mark.parametrize("case", ["key", "kind", "score_identity", "choice_identity"])
def test_question_binds_complete_semantics_to_its_task(
    tmp_path: Path, case: str
) -> None:
    spec, dependency, _ = native_request(tmp_path)
    q = dependency.questions[0]
    if case == "choice_identity":
        p = next(project_presentation(presentation()))
        q = next(
            plan_source(
                pair_spec(tmp_path / "pairs"),
                p,
                lambda i: ResponseItem(item_id=i, text="Synthetic"),
                DecisionsContextV1(service_identity="offline"),
                None,
            )
        ).questions[0]
    data = q.model_dump()
    if case == "key":
        data["key"] = "q_" + "f" * 60
    elif case == "kind":
        data["trait"] = None
    elif case == "score_identity":
        assert spec.rubric is not None
        data["trait"] = {**spec.rubric.traits[1].model_dump()}
    else:
        data["task_id"] = "foreign"
        data["key"] = question_key("foreign")
    with pytest.raises(ValueError):
        DecisionQuestionV1.model_validate(data)


@pytest.mark.parametrize("case", ["mapping", "membership", "wire", "wire_version"])
def test_sealed_crosswalk_and_wire_are_revalidated(tmp_path: Path, case: str) -> None:
    _, _, wire = native_request(tmp_path)
    if case == "mapping":
        q = wire.transport_commitment.questions[0]
        with pytest.raises(ValueError):
            QuestionCommitmentV1.model_validate({**q.model_dump(), "scale_mapping": ()})
    elif case == "membership":
        receipt = wire.transport_commitment
        with pytest.raises(ValueError):
            DecisionsCommitmentV1.model_validate(
                {**receipt.model_dump(), "membership_sha256": "f" * 64}
            )
    else:
        patch = (
            {"rendered_text": "foreign"}
            if case == "wire"
            else {"preparation_version": True}
        )
        with pytest.raises(ValueError):
            PreparedDecisionsRequestV1.model_validate({**wire.model_dump(), **patch})


def test_rendering_and_frozen_ceiling_fail_without_fallback(tmp_path: Path) -> None:
    spec, dependency, wire = native_request(tmp_path)
    tiny = RunSpecV4.model_validate(
        {**spec.model_dump(), "prompt": {"limits": {"max_output_bytes": 1}}}
    )
    with pytest.raises(ValueError, match="rendering safeguard"):
        render_question(tiny, "00", None)
    production = RunSpecV4.model_validate(
        {**spec.model_dump(), "provider": "openai", "endpoint": "decisions"}
    )
    with pytest.raises(ValueError, match="serialization scope"):
        list(
            plan_source(
                production,
                ResponseItem(item_id="000", text="Synthetic"),
                lambda _: ResponseItem(item_id="000", text="Synthetic"),
                DecisionsContextV1(service_identity="offline"),
                None,
            )
        )
    with pytest.raises(ValueError, match="repacking is forbidden"):
        prepare_request(
            spec,
            dependency,
            wire.transport_commitment.identity,
            DecisionsContextV1(
                service_identity="offline", limits=DecisionsLimitsV1(max_body_bytes=1)
            ),
        )
    with pytest.raises(ValueError):
        QuestionPromptSpecV1(advanced=True)


@pytest.mark.parametrize("case", ["evidence_version", "ack_version", "binding"])
def test_complete_acknowledgment_requires_exact_version_and_binding(case: str) -> None:
    evidence = CompleteResponseEvidenceV1(
        run_id="r",
        request_uid="q",
        attempt_id="a",
        submission_id="s",
        correlation_id="c",
        payload_sha256="a" * 64,
        response_sha256="b" * 64,
        response_byte_length=2,
        http_status=200,
        protocol="judgmentllm-decisions-offline-v1",
    )
    if case == "evidence_version":
        with pytest.raises(ValueError):
            CompleteResponseEvidenceV1.model_validate(
                {**evidence.model_dump(), "acceptance_evidence_version": True}
            )
    else:
        data: dict[str, Any] = dict(
            run_id="r",
            attempt_id="a",
            submission_id="s",
            correlation_id="c",
            payload_sha256="a" * 64,
            remote_id=None,
            observation_key="local",
            response_evidence=evidence,
        )
        data["acknowledgment_version"] = True if case == "ack_version" else 2
        if case == "binding":
            data["correlation_id"] = "foreign"
        with pytest.raises(ValueError):
            AcknowledgmentReceiptV2.model_validate(data)


def test_capabilities_and_preview_are_offline_and_selection_exact(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path)
    assert not validate_run(
        spec, capabilities=CapabilityProfile(max_levels=2, modes=("batch_native",))
    ).valid
    assert validate_run(
        spec, capabilities=CapabilityProfile(max_levels=4, modes=("live",))
    ).valid
    with pytest.raises(ValueError):
        plan_run(spec)
    with pytest.raises(ValueError):
        preview_prompt(spec, trait_id="00")
    with pytest.raises(ValueError):
        preview_prompt(spec, item_id="foreign")
    rendered = preview_prompt(spec, item_id="000", trait_id="01")
    assert isinstance(rendered, RenderedDecisionsPreviewV1)
    assert (
        rendered.shared_input == "Synthetic source 0." and len(rendered.questions) == 3
    )
    pair = pair_spec(tmp_path / "pair")
    first = next(project_presentation(presentation())).members[0]
    shown = preview_prompt(pair, pair_uid=first.pair_uid)
    assert isinstance(shown, RenderedDecisionsPreviewV1)
    assert len(shown.questions) == 3 and "Synthetic source" in shown.shared_input
    assert decisions_types.DecisionsCommitmentV1 is DecisionsCommitmentV1
    unknown = "Unknown"
    with pytest.raises(AttributeError):
        getattr(decisions_types, unknown)
