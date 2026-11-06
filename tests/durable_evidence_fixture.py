"""Synthetic evidence adapter; credentials and reasoning never enter mock bytes.

The wrapper is a test transport around the unchanged canonical mock protocol.
Its safe derivative is the exact inner response. Nothing contacts a provider.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from judgmentllm.evidence import (
    ReasoningAuthorization,
    ReasoningBlockMetadata,
    ReportedUsageInput,
)
from judgmentllm.providers import (
    MockAdapter,
    MockScenario,
    ParsedResponse,
    PreparedRequest,
    ProviderResponse,
)
from judgmentllm.providers.evidence import ProviderEvidence, ProviderReasoningBlock
from judgmentllm.providers.mock import MockPayload

OutputTransform = Callable[[bytes], bytes]


def output_substring(raw: bytes) -> tuple[int, int, bytes] | None:
    """Locate the known fixture envelope's output without reserializing output."""
    text = raw.decode("utf-8")
    marker = '"output":'
    start = text.find(marker)
    if start < 0:
        return None
    start += len(marker)
    while text[start].isspace():
        start += 1
    _, end = json.JSONDecoder().raw_decode(text, start)
    return (
        len(text[:start].encode()),
        len(text[:end].encode()),
        text[start:end].encode(),
    )


class EvidenceFixtureAdapter(MockAdapter):
    """Hash-bound synthetic service responses with documented fixture boundaries."""

    def __init__(
        self,
        ledger_path: str | Path,
        *,
        scenario: MockScenario | None = None,
        retrieval_available: bool = True,
        transform: OutputTransform | None = None,
        reasoning: bytes | None = None,
        credential: str = "",
        authorization: ReasoningAuthorization | None = None,
        evidence_available: bool = True,
    ) -> None:
        super().__init__(
            ledger_path, scenario=scenario, retrieval_available=retrieval_available
        )
        self.transform = transform
        self.reasoning = reasoning
        self.credential = credential
        self.evidence_available = evidence_available
        self.evidence_authorization = authorization

    def _response(
        self,
        payload: MockPayload,
        scenario: MockScenario,
        observation_key: str,
        selection_index: int,
    ) -> ProviderResponse:
        response = super()._response(
            payload, scenario, observation_key, selection_index
        )
        inner = response.raw
        emitted: bytes | None = None
        try:
            bounds = output_substring(inner)
        except (ValueError, UnicodeError):
            bounds = None
        if bounds is not None:
            start, end, emitted = bounds
            if self.transform is not None:
                emitted = self.transform(emitted)
                inner = inner[:start] + emitted + inner[end:]
        evidence = response.evidence
        if evidence is None:
            evidence = ProviderEvidence(provenance="synthetic_fixture")
        blocks: tuple[ProviderReasoningBlock, ...] = ()
        if self.reasoning is not None:
            blocks = (
                ProviderReasoningBlock(
                    metadata=ReasoningBlockMetadata(
                        kind="summary",
                        provider_field="openai.responses.reasoning.summary",
                        block_ordinal=0,
                    ),
                    text=self.reasoning,
                ),
            )
        wrapped = self.reasoning is not None or bool(self.credential)
        evidence = evidence.model_copy(
            update={
                "usage": ReportedUsageInput(
                    usage_source="synthetic.reported_usage",
                    input_tokens=11,
                    output_tokens=7,
                    total_tokens=18,
                ),
                "output_text": emitted,
                "output_source": "raw_output_text"
                if emitted is not None
                else "unavailable",
                "reasoning": blocks,
                "safe_response": inner,
                "original_body_safe": not wrapped,
            }
        )
        return response.model_copy(
            update={
                "raw": inner,
                "evidence": evidence if self.evidence_available else None,
            }
        )

    async def retrieve(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse:
        response = await super().retrieve(
            prepared, submission_id=submission_id, correlation_id=correlation_id
        )
        if self.reasoning is None and not self.credential:
            return response
        # Add sensitive test transport fields only after the service verified its
        # clean inner response. The mock ledger never hashes auth-bearing bytes.
        raw = json.dumps(
            {
                "fixture_transport": 1,
                "assessment": response.raw.decode(),
                "reasoning": self.reasoning.decode() if self.reasoning else None,
                "authorization": self.credential,
                "api_key": self.credential,
            },
            ensure_ascii=False,
        ).encode()
        return response.model_copy(update={"raw": raw})

    def parse(
        self, response: ProviderResponse, *, strict_output: bool = False
    ) -> ParsedResponse:
        try:
            wrapper = json.loads(response.raw)
        except ValueError:
            wrapper = None
        if isinstance(wrapper, dict):
            fields = cast(dict[str, object], wrapper)
            if fields.get("fixture_transport") == 1:
                assessment = fields["assessment"]
                assert isinstance(assessment, str)
                response = response.model_copy(update={"raw": assessment.encode()})
        return super().parse(response, strict_output=strict_output)


def specification(
    directory: Path,
    *,
    capture: Literal["full", "minimal"] = "minimal",
    order: str = "together",
    traits: int = 2,
    grouping: str = "grouped",
    explanations: bool = True,
    pairwise: bool = False,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    responses = directory / "responses.jsonl"
    responses.write_text(
        "".join(
            json.dumps({"item_id": ident, "text": "Synthetic assessment input."}) + "\n"
            for ident in ("001", "002")[: 2 if pairwise else 1]
        ),
        encoding="utf-8",
    )
    document: dict[str, object] = {
        "schema_version": 2,
        "task_kind": "pairwise" if pairwise else "rubric",
        "input": str(responses),
        "provider": "mock",
        "endpoint": "synthetic",
        "model": "offline-evidence-fixture",
        "capture": capture,
        "execution": {
            "grouping": "independent" if pairwise else grouping,
            "explanations": explanations,
            "concurrency": 1,
        },
        "prompt": {"output_order": order} if not pairwise else {},
    }
    if pairwise:
        pairs = directory / "pairs.jsonl"
        pairs.write_text(
            json.dumps(
                {
                    "pair_id": "0001",
                    "pair_uid": "pair-0001",
                    "left_item_id": "001",
                    "right_item_id": "002",
                    "trait_id": "01",
                    "presented_order": ["002", "001"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        document.update(
            pairs=str(pairs),
            criteria=[
                {
                    "trait_id": "01",
                    "name": "Synthetic",
                    "description": "Synthetic comparison.",
                }
            ],
        )
    else:
        document["rubric"] = {
            "rubric_id": "001",
            "name": "Synthetic",
            "description": "Synthetic rubric.",
            "traits": [
                {
                    "trait_id": f"{index + 1:02d}",
                    "name": "Synthetic",
                    "description": "Synthetic criterion.",
                    "levels": [
                        {"label": "low", "numeric_value": 1},
                        {"label": "high", "numeric_value": 8},
                    ],
                }
                for index in range(traits)
            ],
        }
    path = directory / "run.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path
