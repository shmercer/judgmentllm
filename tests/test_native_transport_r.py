"""Actual native synthetic executions retain the existing R audit contracts."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from native_transport_fixture import MECHANISMS
from test_native_transport_integration import offline as offline
from test_native_transport_integration import prepared_case

from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.io import write_results_jsonl
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.providers import MockMemberScenario, MockScenario
from judgmentllm.r_exports import export_pairwise_r_csv, export_rubric_r_csv
from judgmentllm.results import PairwiseResult, RubricResult


@pytest.mark.asyncio
async def test_native_outputs_pass_actual_r_jsonlite_assertions(tmp_path: Path) -> None:
    executable = shutil.which("Rscript")
    if executable is None:
        if os.environ.get("JUDGMENTLLM_R_STRICT") == "1":
            pytest.fail("Required Rscript is unavailable")
        pytest.skip("Rscript unavailable locally; strict qualification cannot skip")
    available = subprocess.run(
        [
            executable,
            "--vanilla",
            "-e",
            "quit(status=if(requireNamespace('jsonlite',quietly=TRUE)) 0L else 1L)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if available.returncode:
        if os.environ.get("JUDGMENTLLM_R_STRICT") == "1":
            pytest.fail("Required jsonlite is unavailable")
        pytest.skip("jsonlite unavailable locally; strict qualification cannot skip")
    rubric: list[RubricResult] = []
    pairs: list[PairwiseResult] = []
    for index, mechanism in enumerate(MECHANISMS):
        run_id = f"native-{index:03d}"
        adapter, store, _ = prepared_case(
            tmp_path / f"route-{index}",
            mechanism,
            run_id=run_id,
            capture="full" if index % 2 else "minimal",
            order=("together", "scores_first", "explanations_first")[index % 3],
        )
        await execute_run(run_id, store=store, adapter=adapter)
        with ReadOnlyRepository(store) as repository:
            rubric.extend(
                row
                for row in repository.iter_results(run_id)
                if isinstance(row, RubricResult)
            )
    adapter, store, spec = prepared_case(
        tmp_path / "retry",
        scenario=MockScenario(
            members={"02": MockMemberScenario(outcome="invalid_category")}
        ),
        retry_valid=True,
        run_id="native-retry",
    )
    await execute_run("native-retry", store=store, adapter=adapter)
    with ReadOnlyRepository(store) as repository:
        failed = next(
            row
            for row in repository.iter_results("native-retry")
            if row.status == "invalid"
        )
    authorize_subset_retry(
        "native-retry",
        store=store,
        adapter=adapter,
        task_ids=(failed.task_id,),
        operation_id="r-native-subset",
        spec=spec,
    )
    await recover_run("native-retry", store=store, adapter=adapter)
    with ReadOnlyRepository(store) as repository:
        rubric.extend(
            row
            for row in repository.iter_results("native-retry")
            if isinstance(row, RubricResult)
        )
    for ties in (False, True):
        run_id = f"native-pair-{int(ties)}"
        adapter, store, _ = prepared_case(
            tmp_path / f"pair-{ties}",
            pairwise=True,
            run_id=run_id,
            ties=ties,
            scenario=MockScenario(winner="tie" if ties else "first"),
        )
        await execute_run(run_id, store=store, adapter=adapter)
        with ReadOnlyRepository(store) as repository:
            pairs.extend(
                row
                for row in repository.iter_results(run_id)
                if isinstance(row, PairwiseResult)
            )
    export_rubric_r_csv(tmp_path / "rubric.csv", rubric)
    export_pairwise_r_csv(tmp_path / "pairs.csv", pairs)
    write_results_jsonl(tmp_path / "results.jsonl", [*rubric, *pairs])
    script = tmp_path / "assertions.R"
    script.write_text(
        """library(jsonlite)
args <- commandArgs(trailingOnly=TRUE)
read_audit <- function(name) read.csv(file.path(args[[1]], name), colClasses="character", na.strings="NA", check.names=FALSE, fileEncoding="UTF-8")
r <- read_audit("rubric.csv")
p <- read_audit("pairs.csv")
stopifnot(nrow(r) == 21L, nrow(p) == 2L)
stopifnot(all(r$item_id == "001"), all(r$trait_id %in% c("01", "02")))
stopifnot(sum(r$status == "invalid") == 1L, sum(r$status == "complete") == 20L)
stopifnot(all(is.na(r$probabilities_json)), all(is.na(r$expected_value)))
stopifnot(all(r$primary_score_kind[r$status == "complete"] == "category_code"))
stopifnot(all(is.na(r$primary_score[r$status == "invalid"])))
stopifnot(all(nzchar(r$attempt_id)), length(unique(r$attempt_id)) == 11L)
for (levels in r$resolved_levels_json) {
  coding <- fromJSON(levels)
  stopifnot(identical(as.numeric(coding$numeric_value), c(1, 8)))
}
stopifnot(all(p$ID1 == "002"), all(p$ID2 == "001"))
stopifnot(identical(as.numeric(p$result), c(1, 0.5)))
stopifnot(p$better_id[[1]] == "002", is.na(p$better_id[[2]]))
stopifnot(all(is.na(p$p_left)), all(is.na(p$p_right)), all(is.na(p$p_first)))
for (orientation in p$original_order) stopifnot(identical(fromJSON(orientation), c("001", "002")))
for (orientation in p$presented_order) stopifnot(identical(fromJSON(orientation), c("002", "001")))
records <- lapply(readLines(file.path(args[[1]], "results.jsonl"), encoding="UTF-8"), fromJSON)
stopifnot(length(records) == 23L)
stopifnot(all(vapply(records, function(x) identical(x$schema_version, 1L), logical(1))))
stopifnot(all(vapply(records, function(x) nzchar(x$manifest_sha256) && nzchar(x$request_uid) && nzchar(x$provider), logical(1))))
cat("Native transport R interoperability passed\\n")
""",
        encoding="utf-8",
    )
    checked = subprocess.run(
        [executable, "--vanilla", str(script), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stderr
    assert "Native transport R interoperability passed" in checked.stdout
