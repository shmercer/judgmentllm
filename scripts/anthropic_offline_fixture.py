"""Synthetic SDK HTTP fixtures for required R and resource acceptance only.

No provider connection is possible: the transport is injected, credentials are
fixed synthetic values and socket/DNS operations are denied during execution.
This is development tooling, never production capability evidence.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import httpx2

from judgmentllm.anthropic_transport import PreparedRequestV5, production_context
from judgmentllm.assessment_inspection import inspect_assessment_output
from judgmentllm.config import RunSpecV3
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.io import write_results_jsonl
from judgmentllm.output_contracts import RubricOutputContract, wire_winner_ids
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.anthropic import _INVOKING, AnthropicAdapter
from judgmentllm.r_exports import export_pairwise_r_csv, export_rubric_r_csv
from judgmentllm.results import PairwiseResult, RubricResult

# pyright: reportPrivateUsage=false

Endpoint = Literal["messages"]


@contextmanager
def offline() -> Generator[None]:
    def deny(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("Offline Anthropic fixture forbids external network")

    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                "os.environ", {"ANTHROPIC_API_KEY": "synthetic-offline-fixture-key"}
            )
        )
        stack.enter_context(patch.object(socket, "getaddrinfo", deny))
        stack.enter_context(patch.object(socket, "create_connection", deny))
        stack.enter_context(patch.object(socket.socket, "connect", deny))
        yield


def specification(
    directory: Path,
    endpoint: Endpoint,
    kind: Literal["rubric", "pairwise"],
    *,
    count: int = 1,
    traits: int = 2,
    capture: Literal["full", "minimal"] = "minimal",
) -> RunSpecV3:
    directory.mkdir(parents=True)
    responses = directory / "responses.jsonl"
    with responses.open("w", encoding="utf-8") as stream:
        for index in range(count if kind == "rubric" else 2):
            stream.write(
                json.dumps(
                    {
                        "item_id": f"{index + 1:03d}",
                        "text": "Synthetic response: " + "a clear idea. " * 200,
                    }
                )
                + "\n"
            )
    document: dict[str, object] = {
        "schema_version": 3,
        "task_kind": kind,
        "input": responses,
        "provider": "anthropic",
        "endpoint": endpoint,
        "model": "offline-unqualified-model",
        "account_scope": "synthetic-account",
        "capture": capture,
        "generation": {"max_output_tokens": 256},
        "prompt": {"structured_output": "text"},
        "execution": {
            "concurrency": 4,
            "grouping": "grouped" if kind == "rubric" else "independent",
            "explanations": False,
        },
    }
    if kind == "rubric":
        document["rubric"] = {
            "rubric_id": "001",
            "name": "Offline synthetic rubric",
            "traits": [
                {
                    "trait_id": f"{index + 1:02d}",
                    "name": "Quality",
                    "description": "Assess clarity.",
                    "levels": [
                        {"label": "low", "numeric_value": 1},
                        {"label": "middle", "numeric_value": 3},
                        {"label": "high", "numeric_value": 8},
                    ],
                }
                for index in range(traits)
            ],
        }
    else:
        pairs = directory / "pairs.jsonl"
        with pairs.open("w", encoding="utf-8") as stream:
            for index in range(count):
                stream.write(
                    json.dumps(
                        {
                            "pair_uid": f"{index + 1:03d}",
                            "pair_id": "001",
                            "left_item_id": "001",
                            "right_item_id": "002",
                            "trait_id": "01",
                            "presented_order": ["002", "001"],
                        }
                    )
                    + "\n"
                )
        document["pairs"] = pairs
        document["criteria"] = [
            {
                "trait_id": "01",
                "name": "Clarity",
                "description": "Choose the clearer response.",
            }
        ]
    return RunSpecV3.model_validate(document)


class FixtureTransport:
    def __init__(
        self,
        endpoint: Endpoint,
        *,
        invalid_first_group: bool = False,
        assessment_text: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.omit_usage = False
        self.calls = 0
        self.invalid_first_group = invalid_first_group
        self.assessment_text = assessment_text

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        prepared = _INVOKING.get()
        if (
            not isinstance(prepared, PreparedRequestV5)
            or bytes_digest(request.content) != prepared.payload_sha256
        ):
            raise AssertionError("Offline SDK bytes differ from prepared transport")
        contract = prepared.output_commitment.contract
        if isinstance(contract, RubricOutputContract):
            output = {
                "scores": [
                    {
                        "trait_id": trait.trait_id,
                        "category_label": "invalid"
                        if self.invalid_first_group and self.calls == 0 and index == 1
                        else trait.levels[0].label,
                    }
                    for index, trait in enumerate(contract.traits)
                ]
            }
        else:
            output = {"winner_id": wire_winner_ids(contract)[0]}
        self.calls += 1
        text = (
            json.dumps(output) if self.assessment_text is None else self.assessment_text
        )
        body: dict[str, Any] = {
            "id": f"msg_synthetic_{self.calls}",
            "type": "message",
            "role": "assistant",
            "model": "offline-reported-model",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "content": [{"type": "text", "text": text}],
            "usage": {
                "service_tier": "standard",
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 4,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 3,
                    "ephemeral_1h_input_tokens": 1,
                },
            },
        }
        if self.omit_usage:
            del body["usage"]
        return httpx2.Response(200, json=body, request=request)

    def adapter(self, spec: RunSpecV3) -> AnthropicAdapter:
        return AnthropicAdapter(
            self.endpoint,
            spec.model,
            concurrency=4,
            http_transport=httpx2.MockTransport(self.respond),
        )


async def persist_anthropic(output: Path) -> None:
    output.mkdir(parents=True)
    with offline():
        for capture in ("full", "minimal"):
            for kind in ("rubric", "pairwise"):
                run_id = f"messages-{capture}-{kind}"
                spec = specification(
                    output / "inputs" / run_id, "messages", kind, capture=capture
                )
                transport = FixtureTransport(
                    "messages", invalid_first_group=kind == "rubric"
                )
                transport.omit_usage = kind == "pairwise" and capture == "minimal"
                adapter = transport.adapter(spec)
                try:
                    with prepare_run(
                        spec,
                        store=output / "store",
                        run_id=run_id,
                        capabilities=adapter.capabilities(spec),
                        transport_context=production_context(spec),
                    ):
                        pass
                    await execute_run(
                        run_id, store=output / "store", adapter=adapter, spec=spec
                    )
                    if kind == "rubric":
                        with ReadOnlyRepository(output / "store") as repository:
                            failed = next(
                                row
                                for row in repository.iter_results(run_id)
                                if row.status == "invalid"
                            )
                        authorize_subset_retry(
                            run_id,
                            store=output / "store",
                            adapter=adapter,
                            task_ids=(failed.task_id,),
                            operation_id="anthropic-r-retry",
                            spec=spec,
                        )
                        await recover_run(
                            run_id, store=output / "store", adapter=adapter, spec=spec
                        )
                    assert transport.calls == (2 if kind == "rubric" else 1)
                finally:
                    await adapter.aclose()


def export_anthropic(output: Path) -> None:
    rubric: list[RubricResult] = []
    pairs: list[PairwiseResult] = []
    evidence: list[dict[str, object]] = []
    inspections: list[dict[str, object]] = []
    with ReadOnlyRepository(output / "store") as repository:
        for capture in ("full", "minimal"):
            for kind in ("rubric", "pairwise"):
                run_id = f"messages-{capture}-{kind}"
                for row in repository.iter_results(run_id):
                    if isinstance(row, RubricResult):
                        rubric.append(row)
                    else:
                        pairs.append(row)
                for receipt in repository.iter_evidence(run_id):
                    evidence.append(receipt.model_dump(mode="json"))
                    observation = receipt.identity.observation_id
                    assert observation is not None
                    inspection = inspect_assessment_output(
                        run_id, observation, store=output / "store", include_text=True
                    )
                    assert (inspection.output_text is not None) == (capture == "full")
                    inspections.append(inspection.model_dump(mode="json"))
    export_rubric_r_csv(output / "rubric.csv", rubric)
    export_pairwise_r_csv(output / "pairs.csv", pairs)
    write_results_jsonl(output / "results.jsonl", [*rubric, *pairs])
    for name, records in (("evidence", evidence), ("inspections", inspections)):
        with (output / (name + ".jsonl")).open("w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
