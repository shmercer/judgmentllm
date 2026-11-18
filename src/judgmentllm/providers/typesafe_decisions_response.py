"""TypeSafe answer maps and rejection facts from the bounded original HTTP bytes."""

from __future__ import annotations

import math
import re
from typing import Any, Literal, cast

from pydantic import JsonValue

from judgmentllm.assessment_errors import AssessmentOutputError
from judgmentllm.decisions.interpretation import (
    ChoiceAnswerV1,
    IndexedProbabilityV1,
    ScoreAnswerV1,
)
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
from judgmentllm.persistence.receipts import TypeSafeRejectionEvidenceV1
from judgmentllm.planning import stable_id
from judgmentllm.providers.protocol import (
    ProviderIntegrityError,
    TypeSafeRequestRejected,
)

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


def rejected(
    status: int, raw: bytes, wire: PreparedDecisionsRequestV3
) -> TypeSafeRequestRejected | None:
    try:
        value = decode_assessment_output(raw)
    except AssessmentOutputError as error:
        if status == 400 and error.diagnostic_code == "duplicate_keys":
            return None
        value = None
    fields = cast(dict[str, Any], value) if isinstance(value, dict) else {}
    reason: Literal["request_rejected", "payload_too_large", "max_tokens_exceeded"]
    reason = "payload_too_large" if status == 413 else "request_rejected"
    if status == 400:
        detail: object = fields.get("detail")
        codes: list[object] = [fields.get("code")]
        if isinstance(detail, dict):
            structured = cast(dict[str, object], detail)
            codes.extend((structured.get("error_type"), structured.get("code")))
        elif isinstance(detail, str) and re.fullmatch(r"[a-z][a-z0-9_]*", detail):
            codes.append(detail)
        claims = {code for code in codes if isinstance(code, str)}
        if len(claims) > 1:
            return None
        if "max_tokens_exceeded" in claims:
            reason = "max_tokens_exceeded"
    receipt = TypeSafeRejectionEvidenceV1.model_validate(
        dict(
            **wire.transport_commitment.identity.model_dump(
                exclude={"identity_version"}
            ),
            payload_sha256=wire.payload_sha256,
            response_sha256=bytes_digest(raw),
            response_byte_length=len(raw),
            http_status=status,
            reason=reason,
        )
    )
    return TypeSafeRequestRejected(receipt, usage=usage_from(fields)[0])


def matches_rounded_score(
    index: float, probabilities: list[IndexedProbabilityV1]
) -> bool:
    levels = len(probabilities)
    if not 2 <= levels <= 10 or not 0 <= index <= levels - 1:
        return False
    index_cents = round(index * 100)
    cents = [round(p.probability * 100) for p in probabilities]
    if (
        index != index_cents / 100
        or sum(cents) != 100
        or any(
            p.probability != c / 100 for p, c in zip(probabilities, cents, strict=True)
        )
    ):
        return False

    # shortcut: Nearest-hundredth compatibility cells, revisit when TypeSafe specifies precision.
    # Half-cent units make contact at either endpoint exact.
    bounds = [(max(0, 2 * c - 1), min(200, 2 * c + 1)) for c in cents]
    remaining_mass = 200 - sum(lower for lower, _ in bounds)
    lower_moment = sum(i * lower for i, (lower, _) in enumerate(bounds))

    def extremum(order: range) -> int:
        remaining = remaining_mass
        moment = lower_moment
        for i in order:
            lower, upper = bounds[i]
            assigned = min(remaining, upper - lower)
            moment += i * assigned
            remaining -= assigned
        return moment

    reported = 2 * index_cents
    return (
        extremum(range(levels)) <= reported + 1
        and extremum(range(levels - 1, -1, -1)) >= reported - 1
    )


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
        if checked.expected_index is None or (
            abs(
                math.fsum(p.index * p.probability for p in checked.probabilities)
                - checked.expected_index
            )
            > tolerance
            and not matches_rounded_score(checked.expected_index, checked.probabilities)
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
    verify_reported_model(metadata)


def verify_reported_model(metadata: DecisionsReplayMetadataV1) -> None:
    if "model_conflict" in metadata.findings:
        raise ProviderIntegrityError(
            "TypeSafe reported model conflicts with invocation"
        )
