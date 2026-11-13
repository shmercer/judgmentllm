"""Actual guarded SDK observations exported by Python and independently read in R."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx2
import pytest
from reasoning_fixture import response_body
from study_fixture import PACKAGE, SOURCE, assessment, authorization, case_files
from test_study_native import offline as offline

from judgmentllm.execution import execute_run
from judgmentllm.execution.retry import RetryPolicy
from judgmentllm.io import Result, export_results_csv, write_results_jsonl
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.r_exports import export_pairwise_r_csv, export_rubric_r_csv
from judgmentllm.results import PairwiseResult, RubricResult
from judgmentllm.study.budget import BudgetLedger
from judgmentllm.study.execution import _STUDY_TOKEN, _StudyGuard, adapter_for
from judgmentllm.study.records import Proposal

pytestmark = [pytest.mark.serial, pytest.mark.r_interop]


@pytest.mark.asyncio
async def test_actual_study_exports_in_strict_r(
    tmp_path: Path, pilot: tuple[Path, Proposal], monkeypatch: pytest.MonkeyPatch
) -> None:
    rscript = shutil.which("Rscript")
    assert rscript is not None, (
        "Rscript/jsonlite are required for local study acceptance"
    )
    results: list[Result] = []
    receipts: list[dict[str, Any]] = []
    for index in (1, 0, 5, 6):
        root = tmp_path / str(index)
        root.mkdir()
        monkeypatch.setattr(
            "judgmentllm.study.budget._global_lock_path",
            lambda root=root: root / "study-global.lock",
        )
        directory, proofs, proposal, case, spec = case_files(root, pilot, index)
        body, headers = response_body(spec, "{}" if index == 0 else assessment(spec))

        def handler(request: Any, body: Any = body, headers: Any = headers) -> Any:
            return httpx2.Response(200, json=body, headers=headers, request=request)

        with BudgetLedger(root / "campaign").locked() as ledger:
            ledger.authorize(proposal, authorization(proposal))
            guard = _StudyGuard(
                spec,
                SOURCE,
                token=_STUDY_TOKEN,
                proposal=proposal,
                case=case,
                directory=directory,
                ledger=ledger,
                proofs=proofs,
            )
            adapter = adapter_for(
                spec, guard, http_transport=httpx2.MockTransport(handler)
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
        with ReadOnlyRepository(directory / "preparation" / case.case_id) as repo:
            results.extend(repo.iter_results(case.run_id))
            receipt = next(repo.iter_evidence(case.run_id))
            assert spec.generation is not None
            receipts.append(
                {
                    "identity": receipt.identity.model_dump(mode="json"),
                    "generation": spec.generation.model_dump(mode="json"),
                    "reasoning_policy": None,
                    "usage": receipt.usage.model_dump(mode="json"),
                    "service_tier": receipt.service_tier.model_dump(mode="json")
                    if receipt.service_tier
                    else None,
                }
            )
    write_results_jsonl(tmp_path / "results.jsonl", results)
    export_results_csv(tmp_path / "results.csv", results)
    export_rubric_r_csv(
        tmp_path / "rubric_r.csv", (r for r in results if isinstance(r, RubricResult))
    )
    export_pairwise_r_csv(
        tmp_path / "pairwise_r.csv",
        (r for r in results if isinstance(r, PairwiseResult)),
    )
    (tmp_path / "physical.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in receipts)
    )
    checked = subprocess.run(
        [rscript, "--vanilla", str(PACKAGE / "check_exports.R"), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "Study exports passed" in checked.stdout
