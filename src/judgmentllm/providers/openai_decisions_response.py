"""Strict Decisions wire validation before privacy filtering; no SDK decoding."""

from __future__ import annotations

import math
from typing import Any, cast

from pydantic import JsonValue

from judgmentllm.decisions.interpretation import ChoiceAnswerV1, ScoreAnswerV1
from judgmentllm.decisions.replay import (
    DecisionsReplayMetadataV1,
    OpenAIDecisionsReplayV1,
    ReplayMemberV1,
)
from judgmentllm.decisions.transport import PreparedDecisionsRequestV2
from judgmentllm.evidence._base import EvidenceIdentity
from judgmentllm.evidence.usage import OpenAIResponsesUsageInput
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.planning import stable_id
from judgmentllm.providers.protocol import ProviderIntegrityError


def canonical_answer(
    entry: dict[str, Any],
    question: dict[str, Any],
    tolerance: float,
    *,
    score_tolerance: float,
) -> dict[str, JsonValue]:
    if question["type"] == "score":
        if (
            set(entry) != {"name", "type", "score", "probabilities", "confidence"}
            or entry["type"] != "score"
        ):
            raise ValueError("invalid score shape")
        levels = question["levels"]
        probabilities = cast(list[dict[str, Any]], entry["probabilities"])
        if not isinstance(cast(object, probabilities), list) or len(
            probabilities
        ) != len(levels):
            raise ValueError("incomplete score distribution")
        if any(
            not isinstance(cast(object, p), dict)
            or set(p) != {"value", "label", "probability"}
            or type(p["value"]) is not int
            for p in probabilities
        ):
            raise ValueError("invalid score index")
        probabilities = sorted(probabilities, key=lambda p: p["value"])
        if [p["value"] for p in probabilities] != list(range(len(levels))) or [
            p["label"] for p in probabilities
        ] != [level["label"] for level in levels]:
            raise ValueError("score mapping differs")
        checked = ScoreAnswerV1.model_validate(
            dict(
                name=entry["name"],
                type="score",
                expected_index=entry["score"],
                confidence=entry["confidence"],
                probabilities=[
                    dict(index=p["value"], probability=p["probability"])
                    for p in probabilities
                ],
                legend=[
                    dict(
                        index=i, label=level["label"], description=level["description"]
                    )
                    for i, level in enumerate(levels)
                ],
            )
        )
        if (
            checked.expected_index is None
            or abs(
                math.fsum(p.index * p.probability for p in checked.probabilities)
                - checked.expected_index
            )
            > score_tolerance
        ):
            raise ValueError("score conflicts with distribution")
    else:
        if (
            set(entry) != {"name", "type", "choice", "probabilities", "confidence"}
            or entry["type"] != "choice"
        ):
            raise ValueError("invalid choice shape")
        probabilities = cast(list[dict[str, Any]], entry["probabilities"])
        if (
            not isinstance(cast(object, probabilities), list)
            or len(probabilities) != 2
            or any(
                not isinstance(cast(object, p), dict)
                or set(p) != {"value", "probability"}
                or type(p["value"]) is not str
                for p in probabilities
            )
        ):
            raise ValueError("invalid choice distribution")
        by_value = {p["value"]: p["probability"] for p in probabilities}
        if set(by_value) != {"essay_01", "essay_02"}:
            raise ValueError("choice mapping differs")
        checked = ChoiceAnswerV1.model_validate(
            dict(
                name=entry["name"],
                type="choice",
                selection=entry["choice"],
                confidence=entry["confidence"],
                probabilities=[
                    dict(option=k, probability=by_value[k])
                    for k in ("essay_01", "essay_02")
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
    raw: bytes, wire: PreparedDecisionsRequestV2
) -> tuple[OpenAIDecisionsReplayV1, bool]:
    body = cast(dict[str, Any], decode_assessment_output(wire.payload))
    questions = body["questions"]
    expected = {q["name"]: i for i, q in enumerate(questions)}
    findings: list[Any] = []
    try:
        document = decode_assessment_output(raw)
    except ValueError:
        document = None
        findings.append("malformed_json")
    fields = cast(dict[str, Any], document) if isinstance(document, dict) else {}
    recognizable = (
        isinstance(fields.get("answers"), list) and len(fields["answers"]) <= 2000
    )
    if not recognizable and not findings:
        findings.append("unrecognized_envelope")
    if set(fields) - {"answers", "model", "usage"}:
        findings.append("unknown_field")
    try:
        usage = fields.get("usage")
        OpenAIResponsesUsageInput.model_validate({} if usage is None else usage)
    except ValueError:
        findings.append("unknown_field")
    reported = fields.get("model")
    if reported is not None and reported != wire.model:
        findings.append("model_conflict")
        recognizable = False
    buckets: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    extras = unattributed = 0
    order: list[str] = []
    if recognizable:
        for ordinal, value in enumerate(cast(list[Any], fields["answers"])):
            answer = cast(dict[str, Any], value)
            if not isinstance(value, dict) or not isinstance(answer.get("name"), str):
                unattributed += 1
            elif answer["name"] not in expected:
                extras += 1
            else:
                name = cast(str, answer["name"])
                order.append(name)
                bucket = buckets.setdefault(name, [])
                bucket.append((ordinal, answer))
        unique_order = [expected[n] for n in order if len(buckets[n]) == 1]
        if unique_order != sorted(unique_order):
            recognizable = False
            findings.append("answer_order")
    members: list[ReplayMemberV1] = []
    answers: list[dict[str, JsonValue] | None] = []
    safe_original = recognizable and not findings and not extras and not unattributed
    for question in questions:
        entries = buckets.get(question["name"], [])
        failure = (
            "invalid_output"
            if not recognizable
            else "duplicate_output"
            if len(entries) > 1
            else "missing_output"
            if not entries
            else None
        )
        canonical = None
        kind = entries[0][1].get("type") if len(entries) == 1 else None
        if failure is None:
            entry = entries[0][1]
            if entry == {"name": question["name"], "type": "refusal"}:
                failure = "provider_refusal"
            else:
                try:
                    canonical = canonical_answer(
                        entry,
                        question,
                        wire.transport_commitment.probability_sum_tolerance,
                        score_tolerance=wire.transport_commitment.index_tolerance(
                            len(question.get("levels", ()))
                        ),
                    )
                except (ValueError, TypeError, KeyError, OverflowError):
                    failure = "invalid_output"
                    safe_original = False
        if failure in {"duplicate_output", "invalid_output"}:
            safe_original = False
        members.append(
            ReplayMemberV1.model_validate(
                dict(
                    key=question["name"],
                    answer_ordinals=tuple(i for i, _ in entries),
                    answer_type=kind
                    if isinstance(kind, str) and kind in {"score", "choice", "refusal"}
                    else "invalid"
                    if recognizable
                    else "unavailable",
                    failure=failure,
                )
            )
        )
        answers.append(canonical)
    identity = wire.transport_commitment.identity
    metadata = DecisionsReplayMetadataV1(
        identity=EvidenceIdentity(
            run_id=identity.run_id,
            request_uid=identity.request_uid,
            attempt_id=identity.attempt_id,
            submission_id=identity.submission_id,
            observation_id=stable_id(
                "observation", identity.submission_id, observation_key(wire)
            ),
        ),
        correlation_id=identity.correlation_id,
        payload_sha256=wire.payload_sha256,
        commitment_sha256=wire.transport_commitment.commitment_sha256,
        original_sha256=bytes_digest(raw),
        original_byte_length=len(raw),
        recognizable=recognizable,
        reported_model=wire.model if reported == wire.model else None,
        findings=tuple(findings),
        unexpected_members=extras,
        unattributable_members=unattributed,
        members=tuple(members),
    )
    return OpenAIDecisionsReplayV1(
        metadata=metadata, answers=tuple(answers)
    ), safe_original


def observation_key(wire: PreparedDecisionsRequestV2) -> str:
    return stable_id(
        "openai-decisions-local-observation-v1",
        wire.transport_commitment.identity.submission_id,
    )


def verify_replay(
    replay: OpenAIDecisionsReplayV1, wire: PreparedDecisionsRequestV2
) -> None:
    metadata = replay.metadata
    identity = wire.transport_commitment.identity
    if (
        metadata.identity.run_id,
        metadata.identity.request_uid,
        metadata.identity.attempt_id,
        metadata.identity.submission_id,
        metadata.correlation_id,
        metadata.payload_sha256,
        metadata.commitment_sha256,
    ) != (
        identity.run_id,
        identity.request_uid,
        identity.attempt_id,
        identity.submission_id,
        identity.correlation_id,
        wire.payload_sha256,
        wire.transport_commitment.commitment_sha256,
    ) or tuple(m.key for m in metadata.members) != tuple(
        q.key for q in wire.transport_commitment.questions
    ):
        raise ProviderIntegrityError(
            "Decisions replay differs from committed invocation"
        )
    for question, member, answer in zip(
        wire.transport_commitment.questions,
        metadata.members,
        replay.answers,
        strict=True,
    ):
        if answer is not None:
            checked = (
                ScoreAnswerV1.model_validate(answer)
                if question.kind == "score"
                else ChoiceAnswerV1.model_validate(answer)
            )
            if checked.name != member.key:
                raise ProviderIntegrityError("Decisions replay member differs")
    if "model_conflict" in metadata.findings:
        raise ProviderIntegrityError(
            "Decisions reported model conflicts with invocation"
        )
