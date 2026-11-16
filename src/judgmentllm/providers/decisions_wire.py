"""B's own synthetic wire format; this is not any vendor's transport."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from pydantic import JsonValue

from judgmentllm.config import RunSpecV4, parse_json
from judgmentllm.decisions.records import DecisionQuestionV1
from judgmentllm.decisions.responses import ParsedDecisionsResponseV1
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import IntegrityError

if TYPE_CHECKING:
    from judgmentllm.decisions.transport import (
        DecisionsContextV1,
        PreparedDecisionsRequestV1,
    )
    from judgmentllm.providers.protocol import ProviderResponse


def serialize_synthetic_decisions(
    shared: str,
    questions: Sequence[DecisionQuestionV1],
    spec: RunSpecV4,
    context: DecisionsContextV1,
) -> bytes:
    return canonical_bytes(
        {
            "protocol": context.protocol,
            "service_identity": context.service_identity,
            "model": spec.model,
            "input": shared,
            "questions": [
                {
                    "name": q.key,
                    "type": q.kind,
                    "instructions": q.instructions,
                    "levels": q.trait.model_dump(mode="json") if q.trait else None,
                    "options": ["essay_01", "essay_02"] if q.pair else None,
                }
                for q in questions
            ],
            "controls": {
                "tier": spec.execution.tier,
                "cache": spec.execution.cache_policy,
                "capture": spec.capture,
            },
        }
    )


def parse_synthetic_decisions(
    response: ProviderResponse, wire: PreparedDecisionsRequestV1
) -> ParsedDecisionsResponseV1:
    try:
        raw = parse_json(response.raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raw = None
    fields = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    identity = wire.transport_commitment.identity
    for key, value in {
        "protocol": "judgmentllm-decisions-offline-v1",
        "model": wire.model,
        "submission_id": identity.submission_id,
        "correlation_id": identity.correlation_id,
        "payload_sha256": wire.payload_sha256,
    }.items():
        if key in fields and fields[key] != value:
            raise IntegrityError(
                "Decisions response identity conflicts with invocation"
            )
    recognizable = (
        set(fields)
        == {
            "protocol",
            "model",
            "submission_id",
            "correlation_id",
            "payload_sha256",
            "answer_order",
            "answers",
        }
        and fields.get("answer_order") in ("map", "ordered_array")
        and isinstance(fields.get("answers"), list)
    )
    return ParsedDecisionsResponseV1.model_validate(
        dict(
            identity=identity,
            payload_sha256=wire.payload_sha256,
            response_sha256=bytes_digest(response.raw),
            kind=response.kind,
            recognizable=recognizable,
            reported_model=fields.get("model") if recognizable else None,
            answer_order=fields.get("answer_order") if recognizable else "map",
            answers=tuple(cast(list[JsonValue], fields["answers"]))
            if recognizable
            else (),
        )
    )
