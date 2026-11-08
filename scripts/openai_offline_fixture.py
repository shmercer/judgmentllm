"""Synthetic SDK HTTP fixtures for required R and resource acceptance only.

No provider connection is possible: the transport is injected, credentials are
fixed synthetic values and socket/DNS operations are denied during execution.
This is development tooling, never production capability evidence.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import httpx2

from judgmentllm.config import RunSpecV3
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.io import write_results_jsonl
from judgmentllm.output_contracts import RubricOutputContract
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.preparation import prepare_run
from judgmentllm.production_transport import PreparedRequestV4, production_context
from judgmentllm.providers.openai import _INVOKING, OpenAIAdapter
from judgmentllm.r_exports import export_pairwise_r_csv, export_rubric_r_csv
from judgmentllm.results import PairwiseResult, RubricResult

# pyright: reportPrivateUsage=false

Endpoint = Literal["responses", "chat.completions"]


@contextmanager
def offline() -> Generator[None]:
    def deny(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("Offline OpenAI fixture forbids external network")

    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                "os.environ", {"OPENAI_API_KEY": "synthetic-offline-fixture-key"}
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
        "provider": "openai",
        "endpoint": endpoint,
        "model": "offline-unqualified-model",
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
        self, endpoint: Endpoint, *, invalid_first_group: bool = False
    ) -> None:
        self.endpoint = endpoint
        self.calls = 0
        self.invalid_first_group = invalid_first_group

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        prepared = _INVOKING.get()
        if (
            not isinstance(prepared, PreparedRequestV4)
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
            output = {"winner_id": contract.pair.presented_order[0]}
        self.calls += 1
        text = json.dumps(output)
        if self.endpoint == "responses":
            body: dict[str, Any] = {
                "id": f"resp_synthetic_{self.calls}",
                "object": "response",
                "created_at": 1,
                "model": "offline-reported-model",
                "status": "completed",
                "output": [
                    {
                        "id": f"msg_synthetic_{self.calls}",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": text, "annotations": []}
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "total_tokens": 110,
                    "input_tokens_details": {"cached_tokens": 5},
                    "output_tokens_details": {"reasoning_tokens": 2},
                },
            }
        else:
            body = {
                "id": f"chatcmpl_synthetic_{self.calls}",
                "object": "chat.completion",
                "created": 1,
                "model": "offline-reported-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text},
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "total_tokens": 110,
                    "prompt_tokens_details": {"cached_tokens": 5},
                    "completion_tokens_details": {"reasoning_tokens": 2},
                },
            }
        return httpx2.Response(200, json=body, request=request)

    def adapter(self, spec: RunSpecV3) -> OpenAIAdapter:
        return OpenAIAdapter(
            self.endpoint,
            spec.model,
            concurrency=4,
            http_transport=httpx2.MockTransport(self.respond),
        )


async def persist_openai(output: Path) -> None:
    output.mkdir(parents=True)
    with offline():
        for endpoint in ("responses", "chat.completions"):
            for kind in ("rubric", "pairwise"):
                run_id = endpoint + "-" + kind
                capture = "full" if endpoint == "responses" else "minimal"
                spec = specification(
                    output / "inputs" / run_id, endpoint, kind, capture=capture
                )
                transport = FixtureTransport(
                    endpoint, invalid_first_group=kind == "rubric"
                )
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
                            operation_id="offline-r-retry",
                            spec=spec,
                        )
                        await recover_run(
                            run_id, store=output / "store", adapter=adapter, spec=spec
                        )
                    assert transport.calls == (2 if kind == "rubric" else 1)
                finally:
                    await adapter.aclose()


def export_openai(output: Path) -> None:
    rubric: list[RubricResult] = []
    pairs: list[PairwiseResult] = []
    evidence: list[dict[str, object]] = []
    with ReadOnlyRepository(output / "store") as repository:
        for endpoint in ("responses", "chat.completions"):
            for kind in ("rubric", "pairwise"):
                run_id = endpoint + "-" + kind
                for row in repository.iter_results(run_id):
                    if isinstance(row, RubricResult):
                        rubric.append(row)
                    else:
                        pairs.append(row)
                evidence.extend(
                    receipt.model_dump(mode="json")
                    for receipt in repository.iter_evidence(run_id)
                )
    export_rubric_r_csv(output / "rubric.csv", rubric)
    export_pairwise_r_csv(output / "pairs.csv", pairs)
    write_results_jsonl(output / "results.jsonl", [*rubric, *pairs])
    with (output / "evidence.jsonl").open("w", encoding="utf-8") as stream:
        for receipt in evidence:
            stream.write(json.dumps(receipt, ensure_ascii=False) + "\n")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("persist", "export"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if options.phase == "persist":
        asyncio.run(persist_openai(options.output))
    else:
        export_openai(options.output)


if __name__ == "__main__":
    main()
