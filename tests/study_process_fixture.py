"""Synthetic crash at the guarded HTTP handoff; never connects to a provider."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import httpx2
from study_fixture import KEY, SOURCE, authorization, spec_for

from judgmentllm.execution import execute_run
from judgmentllm.execution.retry import RetryPolicy
from judgmentllm.study import budget, execution
from judgmentllm.study.records import Proposal


async def main(root: Path) -> None:
    execution._source_commit = lambda: SOURCE
    budget._global_lock_path = lambda: root / "study-global.lock"
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    ):
        os.environ[name] = KEY
    directory = root / "proposal"
    proposal = Proposal.model_validate_json((directory / "proposal.json").read_bytes())
    spec = spec_for(proposal)
    case = proposal.calls[0]

    def crash(request: Any) -> Any:
        assert request.content == case.request_body_utf8.encode()
        os._exit(86)

    with budget.BudgetLedger(root / "campaign").locked() as ledger:
        ledger.authorize(proposal, authorization(proposal))
        guard = execution._StudyGuard(
            spec,
            SOURCE,
            token=execution._STUDY_TOKEN,
            proposal=proposal,
            case=case,
            directory=directory,
            ledger=ledger,
            proofs=root / "proofs",
        )
        adapter = execution.adapter_for(
            spec, guard, http_transport=httpx2.MockTransport(crash)
        )
        try:
            await execute_run(
                case.run_id,
                store=directory / "preparation" / case.case_id,
                spec=spec,
                adapter=adapter,
                retry_policy=RetryPolicy(max_attempts=1),
            )
        finally:
            await adapter.aclose()
    raise AssertionError("Synthetic handoff did not reach its crash boundary")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
