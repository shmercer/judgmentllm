"""Validate Gemini Decisions wire bytes before any SDK decoding or retention."""

from __future__ import annotations

import math
import re
from typing import Any, Literal, cast

from pydantic import JsonValue

from judgmentllm.decisions.interpretation import ChoiceAnswerV1, ScoreAnswerV1
from judgmentllm.decisions.records import GEMINI_DECISIONS_PRECISION
from judgmentllm.decisions.replay import (
    DecisionsReplayMetadataV1,
    GeminiDecisionsReplayV1,
    ReplayMemberV1,
)
from judgmentllm.decisions.transport import PreparedDecisionsRequestV4
from judgmentllm.evidence._base import EvidenceIdentity
from judgmentllm.evidence.usage import (
    ReportedUsageInput,
    UsageBreakdown,
    UsageCounter,
    UsageField,
)
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.receipts import GeminiDecisionsRejectionEvidenceV1
from judgmentllm.planning import stable_id
from judgmentllm.presentation import ESSAY_ALIASES
from judgmentllm.providers.protocol import (
    GeminiDecisionsRequestRejected,
    ProviderIntegrityError,
)

_SAFETY_CODES = frozenset(
    ("safety", "prohibited_content", "blocklist", "image_safety", "content_blocked")
)
_BLOCK_REASONS = _SAFETY_CODES | {"other"}
_REJECTION_STATUSES = (400, 401, 403, 404, 413, 429)
_INPUT_COUNT_LIMIT = re.compile(
    r"(?:the )?input token count \(([0-9][0-9,]{0,11})\) exceeds the maximum "
    r"number of tokens allowed \(([0-9][0-9,]{0,11})\)\.?",
    re.IGNORECASE,
)
_CONTEXT_LIMIT = re.compile(
    r"(?:the )?(?:input|request|shared input and questions) exceeds? (?:the )?"
    r"(?:model's )?(?:context (?:limit|window)|maximum context length)"
    r"(?: of (?:262144|262,144) tokens)?\.?",
    re.IGNORECASE,
)
_USAGE_COUNTERS: tuple[tuple[UsageField, UsageCounter], ...] = (
    ("total_input_tokens", "input_tokens"),
    ("total_output_tokens", "output_tokens"),
    ("total_tokens", "total_tokens"),
)


def _document(raw: bytes) -> dict[str, Any] | None:
    try:
        result = decode_assessment_output(raw)
    except ValueError:
        return None
    return cast(dict[str, Any], result) if isinstance(result, dict) else None


def _usage(raw: object) -> tuple[ReportedUsageInput | None, bool]:
    if raw is None:
        return None, True
    if not isinstance(raw, dict):
        return None, False
    fields = cast(dict[str, object], raw)
    values: dict[str, int] = {}
    details: list[UsageBreakdown] = []
    safe = (
        not set(fields)
        - {key for key, _ in _USAGE_COUNTERS}
        - {"input_tokens_by_modality"}
    )
    source = "gemini.decisions.usage"
    for key, normalized in _USAGE_COUNTERS:
        value = fields.get(key)
        if type(value) is int and value >= 0:
            values[normalized] = value
            details.append(
                UsageBreakdown(
                    field=key, value=value, relationship="reported", usage_source=source
                )
            )
        elif value is not None:
            safe = False
    modalities = fields.get("input_tokens_by_modality")
    if modalities is not None:
        if (
            isinstance(modalities, list)
            and len(cast(list[object], modalities)) == 1
            and isinstance(cast(list[object], modalities)[0], dict)
        ):
            modality = cast(list[dict[str, object]], modalities)[0]
            count = modality.get("tokens")
            parent = values.get("input_tokens")
            if (
                set(modality) == {"modality", "tokens"}
                and modality["modality"] == "text"
                and type(count) is int
                and count >= 0
                and (parent is None or count <= parent)
            ):
                details.append(
                    UsageBreakdown(
                        field="input_tokens_by_modality.text",
                        value=count,
                        relationship="included_in",
                        related_counter="input_tokens",
                        usage_source=source,
                    )
                )
            else:
                safe = False
        else:
            safe = False
    return (
        ReportedUsageInput.model_validate(
            dict(usage_source=source, breakdown=tuple(details), **values)
        )
        if details
        else None,
        safe,
    )


def _context_limit(message: object) -> bool:
    if not isinstance(message, str) or len(message) > 256:
        return False
    if _CONTEXT_LIMIT.fullmatch(message):
        return True
    match = _INPUT_COUNT_LIMIT.fullmatch(message)
    if match is None:
        return False
    count, limit = (int(value.replace(",", "")) for value in match.groups())
    return count > limit > 0


def rejected(
    status: int, raw: bytes, wire: PreparedDecisionsRequestV4
) -> GeminiDecisionsRequestRejected | None:
    if status not in _REJECTION_STATUSES:
        return None
    document = _document(raw) if raw else {}
    if document is None or "answers" in document or "prompt_feedback" in document:
        return None
    reason = "payload_too_large" if status == 413 else "request_rejected"
    if status == 400:
        error = document.get("error")
        if not isinstance(error, dict):
            return None
        error = cast(dict[str, object], error)
        code, canonical = error.get("code"), error.get("status")
        if canonical not in (None, "INVALID_ARGUMENT"):
            return None
        if isinstance(code, str) and code in _SAFETY_CODES:
            reason = "safety_refusal"
        elif (
            (type(code) is int and code == 400)
            or code in ("INVALID_ARGUMENT", "invalid_argument")
            or (code is None and canonical == "INVALID_ARGUMENT")
        ):
            if _context_limit(error.get("message")):
                reason = "max_tokens_exceeded"
        else:
            return None
    evidence = GeminiDecisionsRejectionEvidenceV1.model_validate(
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
    return GeminiDecisionsRequestRejected(
        evidence, usage=_usage(document.get("usage"))[0]
    )


def observation_key(wire: PreparedDecisionsRequestV4) -> str:
    return stable_id(
        "gemini-decisions-local-observation-v1",
        wire.transport_commitment.identity.submission_id,
    )


def _identity(wire: PreparedDecisionsRequestV4) -> EvidenceIdentity:
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


def _answer(
    name: str,
    value: object,
    question: dict[str, Any],
    wire: PreparedDecisionsRequestV4,
    labels: tuple[str, ...],
) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError("invalid Gemini Decisions answer")
    entry = cast(dict[str, Any], value)
    probabilities = entry.get("probabilities")
    if entry.get("type") != question["type"] or not isinstance(probabilities, dict):
        raise ValueError("wrong answer type or distribution")
    probabilities = cast(dict[str, Any], probabilities)
    checked: ChoiceAnswerV1 | ScoreAnswerV1
    if labels:
        if set(entry) != {"type", "score", "probabilities", "legend", "confidence"}:
            raise ValueError("invalid score shape")
        criteria = cast(list[str], question["criteria"])
        keys = tuple(str(i) for i in range(len(labels)))
        if probabilities.keys() != set(keys) or entry["legend"] != dict(
            zip(keys, criteria, strict=True)
        ):
            raise ValueError("incomplete distribution or changed legend")
        checked = ScoreAnswerV1.model_validate(
            dict(
                name=name,
                type="score",
                expected_index=entry["score"],
                confidence=entry["confidence"],
                probabilities=[
                    dict(index=i, probability=probabilities[key])
                    for i, key in enumerate(keys)
                ],
                legend=[
                    dict(
                        index=i,
                        label=label,
                        description=""
                        if text == label
                        else text.removeprefix(label + ": "),
                    )
                    for i, (label, text) in enumerate(
                        zip(labels, criteria, strict=True)
                    )
                ],
            )
        )
    else:
        if set(entry) != {"type", "choice", "probabilities", "confidence"} or set(
            probabilities
        ) != set(ESSAY_ALIASES):
            raise ValueError("invalid choice shape")
        checked = ChoiceAnswerV1.model_validate(
            dict(
                name=name,
                type="choice",
                selection=entry["choice"],
                confidence=entry["confidence"],
                probabilities=[
                    dict(option=alias, probability=probabilities[alias])
                    for alias in ESSAY_ALIASES
                ],
            )
        )
    _validate_precision(checked, wire, len(labels))
    return cast(dict[str, JsonValue], checked.model_dump(mode="json"))


def _validate_precision(
    answer: ScoreAnswerV1 | ChoiceAnswerV1,
    wire: PreparedDecisionsRequestV4,
    levels: int,
) -> None:
    if isinstance(answer, ScoreAnswerV1):
        if (
            [p.index for p in answer.probabilities] != list(range(levels))
            or answer.selected_index is not None
            or answer.expected_index is None
            or not 0 <= answer.expected_index <= levels - 1
            or abs(
                answer.expected_index
                - math.fsum(p.index * p.probability for p in answer.probabilities)
            )
            > wire.transport_commitment.index_tolerance(levels)
        ):
            raise ValueError("score conflicts with distribution")
        compared = (max(p.probability for p in answer.probabilities),)
    else:
        if tuple(p.option for p in answer.probabilities) != ESSAY_ALIASES:
            raise ValueError("incomplete choice distribution")
        maximum = max(p.probability for p in answer.probabilities)
        selected = next(
            p.probability for p in answer.probabilities if p.option == answer.selection
        )
        # Equal wire probabilities can conceal a difference before rounding.
        compared = (maximum, selected)
    if (
        answer.confidence is None
        or max(*compared, answer.confidence) - min(*compared, answer.confidence)
        > GEMINI_DECISIONS_PRECISION
        or abs(math.fsum(p.probability for p in answer.probabilities) - 1)
        > (wire.transport_commitment.probability_sum_tolerance)
    ):
        raise ValueError("probability mass or confidence conflicts")


def analyze(
    raw: bytes, wire: PreparedDecisionsRequestV4
) -> tuple[
    GeminiDecisionsReplayV1,
    bool,
    ReportedUsageInput | None,
    Literal["response", "refusal"],
]:
    request = cast(dict[str, Any], decode_assessment_output(wire.payload))["questions"]
    document = _document(raw)
    fields = document if document is not None else {}
    answers = fields.get("answers")
    feedback = fields.get("prompt_feedback")
    block_reason: object = None
    if isinstance(feedback, dict):
        block_reason = cast(dict[str, object], feedback).get("block_reason")
    refused = (
        isinstance(block_reason, str)
        and block_reason in _BLOCK_REASONS
        and (answers is None or answers == {})
        and "error" not in fields
    )
    recognizable = (
        isinstance(answers, dict)
        and len(cast(dict[str, object], answers)) <= 2000
        and block_reason is None
        and "error" not in fields
        and (feedback is None or isinstance(feedback, dict))
    )
    usage, safe_usage = _usage(fields.get("usage"))
    findings: list[str] = []
    if document is None:
        findings.append("malformed_json")
    elif not recognizable and not refused:
        findings.append("unrecognized_envelope")
    if (
        set(fields) - {"answers", "prompt_feedback", "usage"}
        or not safe_usage
        or (
            isinstance(feedback, dict)
            and set(cast(dict[str, object], feedback)) - {"block_reason"}
        )
    ):
        findings.append("unknown_field")
    observed = cast(dict[str, object], answers) if recognizable else {}
    positions = {name: i for i, name in enumerate(observed)}
    extra = len(positions.keys() - request.keys())
    safe_original = (recognizable or refused) and not findings and not extra
    members: list[ReplayMemberV1] = []
    retained: list[dict[str, JsonValue] | None] = []
    for question in wire.transport_commitment.questions:
        key = question.key
        answer: object = observed.get(key)
        canonical = None
        kind = (
            cast(dict[str, object], answer).get("type")
            if isinstance(answer, dict)
            else None
        )
        if refused:
            failure = "provider_refusal"
        elif not recognizable:
            failure = "invalid_output"
        elif key not in positions:
            failure = "missing_output"
        else:
            try:
                canonical = _answer(
                    key,
                    cast(object, answer),
                    request[key],
                    wire,
                    tuple(level.label for level in question.scale_mapping),
                )
                failure = None
            except (ValueError, TypeError, OverflowError):
                failure = "invalid_output"
                safe_original = False
        members.append(
            ReplayMemberV1.model_validate(
                dict(
                    key=key,
                    answer_ordinals=(positions[key],) if key in positions else (),
                    answer_type="refusal"
                    if refused
                    else "unavailable"
                    if not recognizable
                    else (kind if kind in ("score", "choice") else "invalid"),
                    failure=failure,
                )
            )
        )
        retained.append(canonical)
    metadata = DecisionsReplayMetadataV1.model_validate(
        dict(
            identity=_identity(wire),
            correlation_id=wire.transport_commitment.identity.correlation_id,
            payload_sha256=wire.payload_sha256,
            commitment_sha256=wire.transport_commitment.commitment_sha256,
            original_sha256=bytes_digest(raw),
            original_byte_length=len(raw),
            recognizable=recognizable,
            reported_model=None,
            findings=tuple(findings),
            unexpected_members=extra,
            unattributable_members=0,
            members=tuple(members),
        )
    )
    return (
        GeminiDecisionsReplayV1(metadata=metadata, answers=tuple(retained)),
        safe_original,
        usage,
        "refusal" if refused else "response",
    )


def verify_replay(
    replay: GeminiDecisionsReplayV1, wire: PreparedDecisionsRequestV4
) -> None:
    metadata = replay.metadata
    if (
        metadata.identity != _identity(wire)
        or metadata.correlation_id != wire.transport_commitment.identity.correlation_id
        or metadata.payload_sha256 != wire.payload_sha256
        or metadata.commitment_sha256 != wire.transport_commitment.commitment_sha256
        or metadata.reported_model is not None
        or tuple(member.key for member in metadata.members)
        != tuple(question.key for question in wire.transport_commitment.questions)
    ):
        raise ProviderIntegrityError(
            "Gemini Decisions replay differs from committed invocation"
        )
    for answer, question in zip(
        replay.answers, wire.transport_commitment.questions, strict=True
    ):
        if answer is not None:
            checked = (
                ScoreAnswerV1.model_validate(answer)
                if question.kind == "score"
                else ChoiceAnswerV1.model_validate(answer)
            )
            if checked.name != question.key:
                raise ProviderIntegrityError("Gemini Decisions replay member differs")
            _validate_precision(checked, wire, len(question.scale_mapping))
