"""Independent synthetic executor; its only HTTP boundary is a MockTransport."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import json
import os
import socket
import sys
import tracemalloc
from pathlib import Path
from typing import Any

import httpx2
import psutil
from reasoning_fixture import response_body
from study_fixture import KEY, SOURCE, assessment, authorization

from judgmentllm.config import RunSpecV3
from judgmentllm.study import budget, execution, plans
from judgmentllm.study.records import Proposal


async def main(root: Path, mode: str) -> None:
    execution._source_commit = plans._source_commit = lambda: SOURCE
    budget._global_lock_path = lambda: root / "study-global.lock"
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        os.environ[name] = KEY

    def deny(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External network is forbidden")

    socket.getaddrinfo = socket.create_connection = deny
    directory = root / "proposal"
    proposal = Proposal.model_validate_json((directory / "proposal.json").read_bytes())
    original = execution.adapter_for

    def adapter(spec: RunSpecV3, guard: execution._StudyGuard) -> Any:
        assert guard.case is not None
        case = guard.case
        body, headers = response_body(spec, assessment(spec))

        def handler(request: Any) -> Any:
            assert request.content == case.request_body_utf8.encode()
            with (root / "http-calls.jsonl").open("a") as stream:
                stream.write(json.dumps(case.case_id) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            if mode == "crash":
                os._exit(86)
            return httpx2.Response(200, json=body, headers=headers, request=request)

        return original(spec, guard, http_transport=httpx2.MockTransport(handler))

    execution.adapter_for = adapter
    measured: dict[str, int] = {}
    if mode == "resource":
        original_execute = execution.execute_run

        async def measured_execute(*args: Any, **kwargs: Any) -> Any:
            # Measure the durable handoff, not repeated offline compilation of 42 cases.
            threads_before = psutil.Process().num_threads()
            tracemalloc.start()
            try:
                return await original_execute(*args, **kwargs)
            finally:
                measured.update(
                    python_peak_bytes=tracemalloc.get_traced_memory()[1],
                    rss_after_bytes=psutil.Process().memory_info().rss,
                    threads_before=threads_before,
                    threads_after=psutil.Process().num_threads(),
                )
                tracemalloc.stop()

        execution.execute_run = measured_execute
    result = await execution.execute(
        directory,
        proposal.digest,
        root / "campaign",
        authorization(proposal),
        root / "proofs",
        max_new_cases=None if mode == "recover" else 1,
        recover_only=mode == "recover",
    )
    if mode == "resource":
        result["resources"] = measured
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
