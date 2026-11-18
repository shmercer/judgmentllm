"""TypeSafe answer maps and rejection facts from the bounded original HTTP bytes."""

from __future__ import annotations

import math
from typing import Any, cast

from pydantic import JsonValue

from judgmentllm.decisions.interpretation import ChoiceAnswerV1, ScoreAnswerV1
from judgmentllm.decisions.replay import (
    DecisionsReplayMetadataV1,
    ReplayMemberV1,
    TypeSafeDecisionsReplayV1,
)
from judgmentllm.decisions.transport import PreparedDecisionsRequestV3
from judgmentllm.evidence._base import EvidenceIdentity
from judgmentllm.evidence.usage import ReportedUsageInput
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.planning import stable_id
from judgmentllm.providers.protocol import ProviderIntegrityError

CHOICES = {
    "essay_01": "The first presented essay.",
    "essay_02": "The second presented essay.",
}


def observation_key(wire: PreparedDecisionsRequestV3) -> str:
    return stable_id(
        "typesafe-decisions-local-observation-v1",
        wire.transport_commitment.identity.submission_id,
    )


def metadata_identity(wire: PreparedDecisionsRequestV3) -> EvidenceIdentity:
    identity = wire.transport_commitment.identity
    return EvidenceIdentity(
        run_id=identity.run_id,
        request_uid=identity.request_uid,
        attempt_id=identity.attempt_id,
        submission_id=identity.submission_id,
        observation_id=stable_id(
            "observation", identity.submission_id, observation_key(wire)
        ),
    )


def usage_from(document: dict[str, Any]) -> tuple[ReportedUsageInput | None, bool]:
    raw = document.get("usage")
    if raw is None:
        return None, True
    if not isinstance(raw, dict):
        return None, False
    fields = cast(dict[str, Any], raw)
    counts = {
        name: value
        for name, value in fields.items()
        if name in ("input_tokens", "output_tokens")
        and (value is None or type(value) is int and value >= 0)
    }
    return (
        ReportedUsageInput.model_validate(
            {"usage_source": "typesafe.systemone.usage", **counts}
        )
        if counts
        else None,
        len(fields) == len(counts),
    )


def response_object(raw: bytes) -> dict[str, Any] | None:
    try:
        value = decode_assessment_output(raw)
    except ValueError:
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def canonical_answer(
    name: str, answer: object, question: dict[str, Any], tolerance: float
) -> dict[str, JsonValue]:
    if not isinstance(answer, dict):
        raise ValueError("invalid answer object")
    entry = cast(dict[str, Any], answer)
    kind = question["type"]
    fields = {"type", "probabilities", "confidence"}
    if entry.get("type") != kind:
        raise ValueError("answer type differs from the committed question")
    probabilities = cast(dict[str, Any], entry["probabilities"])
    if not isinstance(cast(object, probabilities), dict):
        raise ValueError("invalid probability map")
    if kind == "score":
        criteria = cast(list[dict[str, str]], question["criteria"])
        keys = tuple(str(i) for i in range(len(criteria)))
        if (
            set(entry) != fields | {"score", "legend"}
            or set(probabilities) != set(keys)
            or entry["legend"] != dict(zip(keys, criteria, strict=True))
        ):
            raise ValueError("score distribution or legend differs")
        checked = ScoreAnswerV1.model_validate(
            dict(
                name=name,
                type=kind,
                expected_index=entry["score"],
                confidence=entry["confidence"],
                probabilities=[
                    dict(index=i, probability=probabilities[key])
                    for i, key in enumerate(keys)
                ],
                legend=[dict(index=i, **level) for i, level in enumerate(criteria)],
            )
        )
        if (
            checked.expected_index is None
            or abs(
                math.fsum(p.index * p.probability for p in checked.probabilities)
                - checked.expected_index
            )
            > tolerance
        ):
            raise ValueError("score conflicts with distribution")
    else:
        if set(entry) != fields | {"choice"} or set(probabilities) != CHOICES.keys():
            raise ValueError("choice distribution differs")
        checked = ChoiceAnswerV1.model_validate(
            dict(
                name=name,
                type=kind,
                selection=entry["choice"],
                confidence=entry["confidence"],
                probabilities=[
                    dict(option=key, probability=probabilities[key]) for key in CHOICES
                ],
            )
        )
    if (
        checked.confidence is None
        or abs(math.fsum(p.probability for p in checked.probabilities) - 1) > tolerance
    ):
        raise ValueError("invalid distribution or confidence")
    return cast(dict[str, JsonValue], checked.model_dump(mode="json"))


def analyze(
    raw: bytes, wire: PreparedDecisionsRequestV3
) -> tuple[TypeSafeDecisionsReplayV1, bool, ReportedUsageInput | None]:
    request = cast(dict[str, Any], decode_assessment_output(wire.payload))
    questions = cast(dict[str, dict[str, Any]], request["questions"])
    document = response_object(raw)
    fields = document or {}
    raw_answers = fields.get("answers")
    reported_model = fields.get("model")
    matching_model = reported_model == wire.model
    recognizable = matching_model and isinstance(raw_answers, dict)
    findings: list[str] = []
    if document is None:
        findings.append("malformed_json")
    elif not isinstance(raw_answers, dict) or reported_model is None:
        findings.append("unrecognized_envelope")
    if reported_model is not None and not matching_model:
        findings.append("model_conflict")
    usage, safe_usage = usage_from(fields)
    if set(fields) - {"answers", "model", "usage"} or not safe_usage:
        findings.append("unknown_field")
    received = cast(dict[str, Any], raw_answers) if recognizable else {}
    ordinals = {key: i for i, key in enumerate(received) if key in questions}
    unexpected = len(received) - len(ordinals)
    safe = recognizable and not findings and not unexpected
    members: list[ReplayMemberV1] = []
    answers: list[dict[str, JsonValue] | None] = []
    for key, question in questions.items():
        canonical = None
        failure = (
            "invalid_output"
            if not recognizable
            else "missing_output"
            if key not in ordinals
            else None
        )
        if failure is None:
            try:
                canonical = canonical_answer(
                    key,
                    received[key],
                    question,
                    wire.transport_commitment.probability_sum_tolerance,
                )
            except (ValueError, TypeError, KeyError, OverflowError):
                failure = "invalid_output"
                safe = False
        members.append(
            ReplayMemberV1.model_validate(
                dict(
                    key=key,
                    answer_ordinals=(ordinals[key],) if key in ordinals else (),
                    answer_type=question["type"]
                    if canonical is not None
                    else "invalid"
                    if key in ordinals
                    else "unavailable",
                    failure=failure,
                )
            )
        )
        answers.append(canonical)
    commitment = wire.transport_commitment
    metadata = DecisionsReplayMetadataV1.model_validate(
        dict(
            identity=metadata_identity(wire),
            correlation_id=commitment.identity.correlation_id,
            payload_sha256=wire.payload_sha256,
            commitment_sha256=commitment.commitment_sha256,
            original_sha256=bytes_digest(raw),
            original_byte_length=len(raw),
            recognizable=recognizable,
            reported_model=wire.model if matching_model else None,
            findings=tuple(findings),
            unexpected_members=unexpected,
            unattributable_members=0,
            members=tuple(members),
        )
    )
    return (
        TypeSafeDecisionsReplayV1(metadata=metadata, answers=tuple(answers)),
        safe,
        usage,
    )


def verify_replay(
    replay: TypeSafeDecisionsReplayV1, wire: PreparedDecisionsRequestV3
) -> None:
    metadata = replay.metadata
    commitment = wire.transport_commitment
    if (
        metadata.identity != metadata_identity(wire)
        or metadata.correlation_id != commitment.identity.correlation_id
        or metadata.payload_sha256 != wire.payload_sha256
        or metadata.commitment_sha256 != commitment.commitment_sha256
        or (metadata.recognizable and metadata.reported_model != wire.model)
        or tuple(member.key for member in metadata.members)
        != tuple(question.key for question in commitment.questions)
        or any(
            answer is not None
            and (answer.get("name"), answer.get("type"))
            != (question.key, question.kind)
            for answer, question in zip(
                replay.answers, commitment.questions, strict=True
            )
        )
    ):
        raise ProviderIntegrityError(
            "TypeSafe replay differs from committed invocation"
        )
