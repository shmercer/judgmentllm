# R interoperability: explicit audit and analysis views

JudgmentLLM is a standalone Python library and CLI. Its primary R integration
uses the installed CLI, normally through `processx`, and exchanges canonical
JSONL or documented R-compatible CSV files. `reticulate` is an optional secondary
interface; an embedded Python runtime is not required for the ordinary R
workflow. Statistical estimation remains in R.

Milestone 1 supplies provider-independent CSV views through
`judgmentllm.r_exports.export_pairwise_r_csv` and `export_rubric_r_csv`, needing
neither R nor pandas in the Python installation. The functional thin `rubricLLM`
R wrapper is a Milestone 6 deliverable. FastAPI remains committed for Milestone 7
as an optional local service; ordinary R use does not require that server.
Seven strict offline R/jsonlite gates now cover the implemented mock lifecycle,
including independent installed-CLI processes. Subphase 6 adds a separate
bounded-read checker for large rubric exports and is merged with retained
qualification evidence for its final implementation head; see the
[historical qualification scope](design-spec.md#milestone-2-implementation-boundary).
Large exports are not added to every routine R gate.

These exports are **all-attempts audit views**: every supplied record remains,
including failures, observed ties, reversals, repeated judgments, and multiple
attempts for the same task. They perform no implicit success filter, retry
selection, aggregation, or winner inference. The canonical normalized JSONL and
lossless normalized CSV remain authoritative and keep their existing formats.

## Milestone 3 Phase 1 contract acceptance

The existing export gate (`examples/export_r.py` followed by
`examples/r_interoperability.R`) also creates separate
`assessment_contract_rubric_r.csv`, `assessment_contract_pairwise_r.csv` and
`assessment_contract_results.jsonl` fixtures. Historical baseline files and their
row counts remain unchanged. The new files use existing result models and
exporters; they add no columns or R-side scoring logic.

Python validates complete raw responses and invokes explicit contract-based
member interpretation offline, without provider submission or durable v2
execution. R checks equivalent categorical results/explanations for `together`,
`scores_first` and `explanations_first`, mixed score/explanation array order,
nonuniform numeric mappings, distinct grouped tasks, incomplete-member failure,
missing expected values/probabilities and character IDs with leading zeros.
Winner-only, historical explicit-false, allowed ties and reversed-presentation
outputs retain existing pairwise outcome semantics. Existing fixtures continue
checking genuine probabilities, expected/modal scores and original-orientation
probability mappings.

This extends one of the seven strict gates; all seven local configurations still
run all 49 gate executions. Missing R/jsonlite or failed assertions remain
blocking in required acceptance. Phase 2 v2 rendering/preparation and mock execution now extend the existing
installed-CLI lifecycle gate described below. Real provider adapters and
usage/reasoning/order-evidence sidecars remain later work.

## Milestone 3 Phase 2 installed-CLI acceptance

The seventh gate (`scripts/cli_lifecycle_r_gate.py` and
`examples/cli_lifecycle_interoperability.R`) executes actual explicit v2
configurations using the installed `judgmentllm` console, then reopens durable
results and immutable exports through independent processes. The fixture helper
writes synthetic sources/scenarios and reads evidence; it does not score or
construct normalized outputs. The original v1 fixtures and assertions remain.

R checks equivalent normalized categorical scores/explanations for all three
layouts, trait-ID explanation joins after reordered wire arrays, scores without
explanations, incomplete-explanation failures and valid sibling salvage,
nonuniform category mappings, absent fabricated expectations/probabilities,
character IDs with leading zeros, winner-only `tie=false`, allowed ties and
original/presented reversal orientation. Explicit failed-member retries retain
every prior attempt and immutable old exports. Acceptance evidence verifies one
accepted request per original group and no explanation-stage request.

The gate covers full and minimal capture and uses the same strict jsonlite,
installed-console and timeout requirements. All seven local configurations still
run all seven gates (49 executions); missing dependencies or failed assertions
remain blocking. No second R assessment engine, R wrapper package or HTTP server
is introduced. Runnable installed-package commands and a base-R importer are in
[examples/v2](../examples/v2/README.md).

## Approved R architecture and delivery boundaries

| Component | Role |
|---|---|
| JudgmentLLM Python library and CLI | Own validation, prompt preparation, provider execution, scheduling, persistence, and durable identities as their milestones are implemented. |
| Canonical JSONL / R-compatible CSV | Stable file exchange between Python and R, with original audit records retained. |
| `rubricLLM` R wrapper, Milestone 6 | Provide a functional thin CLI client for configure, validate, plan, submit, status, resume, and collect operations. |
| `pairwiseLLM` | Consume result files for comparative-judgment statistical analysis in this integration, without embedding Python or duplicating JudgmentLLM execution. |
| `reticulate`, optional | Secondary direct-Python route for users deliberately choosing embedding. |
| FastAPI, Milestone 7 | Committed optional local HTTP interface for applications needing it, separate from ordinary R use. |

The wrapper must perform the complete listed run lifecycle, not merely document
manual invocations. It will use shared Python/CLI contracts rather than recreate
provider adapters, scheduling, persistence, or result normalization in R. These
are future wrapper requirements; no `rubricLLM` package is implemented in this milestone. Subphase 5 exposes
execution through the shared public Python services and the installed CLI;
Milestone 1 template behavior remains unchanged.

Generation settings use the same optional `RunSpec.generation` through Python,
the installed lifecycle CLI configuration, and the future Milestone 6 wrapper. Preserve omitted
controls and explicit zeros; do not add R-side provider defaults, merge them into
execution controls, or reinterpret the pairing `seed` as a generation seed.
Capability support and qualification evidence remain separate from requested
settings. Changing requested generation settings requires a new frozen run;
current capability restrictions must still be checked before resume.

Call Python once per run operation, file, or appropriately sized group/batch of
tasks. Do not spawn a new Python process for every judgment. Request-level
concurrency and durable submission belong to the Python service, while model
fitting, rubric calibration, and comparative statistics remain in R. The user
chooses attempts and downstream analysis policies explicitly as shown below.

For example, when `processx` and the standalone CLI are installed, this already
supported command lists templates without embedding Python:

```r
command <- Sys.which("judgmentllm")
if (!nzchar(command)) stop("Install JudgmentLLM and put its executable on PATH")
result <- processx::run(command, c("templates", "list"), error_on_status = FALSE)
if (result$status != 0L) stop(result$stderr)
cat(result$stdout)
```

`processx` is an R-side integration dependency, not a Python-core dependency.
The base-R CSV examples below require neither `processx` nor `reticulate` once
files have been produced. The future wrapper should report process failures and
reuse durable run IDs; it must not confuse a process restart with permission to
submit changed content under an old submission identity. See the Milestone 2
content-hash acceptance requirements in the [implementation plan](implementation-plan.md).

## Generate and read synthetic results

From the repository checkout:

```bash
uv run --offline --no-sync python examples/export_r.py --output /tmp/judgmentllm-r
Rscript --vanilla examples/r_interoperability.R /tmp/judgmentllm-r
```

The Python example writes `pairwise_r.csv` and `rubric_r.csv`. It uses synthetic
provider observations and does not call an API. The R script uses base R for
import, numeric/logical type conversion, attempt selection, and verification. It additionally
checks pairwiseLLM adapters and JSON recovery when `pairwiseLLM` and `jsonlite`
are installed. It performs no statistical fitting and installs no packages.

For arbitrary normalized result streams:

```python
from judgmentllm.io import read_results
from judgmentllm.r_exports import export_pairwise_r_csv, export_rubric_r_csv
from judgmentllm.results import PairwiseResult, RubricResult

export_pairwise_r_csv(
    "pairwise_r.csv",
    (row for row in read_results("results.jsonl") if isinstance(row, PairwiseResult)),
)
export_rubric_r_csv(
    "rubric_r.csv",
    (row for row in read_results("results.jsonl") if isinstance(row, RubricResult)),
)
```

Both functions have signature `(path, records, *, na_string="NA") -> int`, consume
iterators one record at a time, revalidate the current complete result data, and
return the number of records written. They do not buffer the dataset or create
parent directories. As with canonical writers, a late invalid record can leave
a partial file; these are not transactional execution journals.

Missing cells use the literal `NA`, booleans use `TRUE`/`FALSE`, and nested cells
contain JSON. A literal value equal to the missing marker is rejected rather
than conflated with missing data; choose another `na_string` and the same R
`na.strings` value if needed. Empty strings stay empty. Quoting CSV identifiers
alone does not protect their types, so import every column as character first:

```r
pair_audit <- read.csv("pairwise_r.csv",
  colClasses = "character", stringsAsFactors = FALSE, check.names = FALSE,
  na.strings = "NA", fileEncoding = "UTF-8", comment.char = ""
)
pair_audit$result <- as.numeric(pair_audit$result)
pair_audit$p_first <- as.numeric(pair_audit$p_first)
pair_audit$is_reversal <- as.logical(pair_audit$is_reversal)
# Convert other named numeric columns explicitly; never convert IDs wholesale.
stopifnot(is.character(pair_audit$ID1)) # "001" stays "001"
```

Base R numeric values are doubles. The exporter preserves Python float values
without intentional rounding; it does not turn ordinal category strings into
numbers or equate categorical assignments with expected scores.

## Pairwise export schema and presentation orientation

The pairwise view contains all scalar/nested fields of `PairwiseResult`, plus:

| Column | Meaning |
|---|---|
| `ID1`, `ID2` | First and second **presented** response IDs. |
| `A_id`, `B_id` | Aliases of `ID1`, `ID2`, respectively. |
| `better_id` | Observed `winner_id`, when available; never inferred from probabilities. |
| `result` | For a complete observation: `1` if `ID1` wins, `0` if `ID2` wins, or `0.5` for a permitted observed tie. Missing for unsuccessful records. |
| `winner_pos` | `1` or `2` for a complete observed win; missing for ties/unsuccessful records. |
| `p_first`, `p_second` | Optional probabilities mapped to first/second presented IDs. |

Original `left_item_id`, `right_item_id`, `original_order`, `presented_order`,
`p_left`, `p_right`, and `p_tie` remain. `p_left`/`p_right` always refer to original
orientation. When presentation reverses, `p_first=p_right` and
`p_second=p_left`. A probability mode never becomes an observed winner or tie.
An unsuccessful record can retain observed evidence, such as a winner or
probabilities; its missing `result` still prevents treating it as a completed
judgment. Always filter status explicitly for analysis.

The view also retains `pair_uid`, `pair_id`, `trait_id`, `source_pair_uid`,
`repetition_index`, `is_reversal`, `reversal_group`, run/task/request/attempt IDs,
status/errors, provider/endpoint/model/tier/mode, confidence/explanation, reported
usage, probability tolerance, and available prompt/raw/manifest references.
Nested original/presented orders are JSON cells. No adapter regenerates these
JudgmentLLM IDs during export.

A compact projection of the synthetic output is:

```csv
ID1,ID2,better_id,result,winner_pos,pair_uid,attempt_id,status,p_left,p_right,p_first,p_second,p_tie
001,002,001,1,1,pair-001,attempt-1,complete,0.8,0.2,0.8,0.2,NA
001,002,002,0,2,pair-001,attempt-2,complete,0.4,0.6,0.4,0.6,NA
002,001,001,0,2,pair-002,attempt-1,complete,0.7,0.3,0.3,0.7,NA
001,002,NA,0.5,NA,pair-003,attempt-1,complete,0.25,0.25,0.25,0.25,0.5
001,002,NA,NA,NA,pair-004,attempt-1,errored,0.9,0.1,0.9,0.1,NA
001,002,002,0,2,pair-005,attempt-1,complete,NA,NA,NA,NA,NA
```

The duplicate `pair-001` rows are two attempts of one logical judgment, not two
independent scheduled judgments. `pair-002` is a distinct reversal occurrence;
`pair-005` is a distinct scheduled judgment. Preserve all rows in the audit file.

Built-in pairwise prompts and examples prefer `first_response` and
`second_response`. Legacy `left_response`/`right_response` aliases retain their
existing presentation-position behavior; they do not mean original left/right
orientation. The render arguments `left`/`right` and original IDs continue to use
original orientation. See [prompt guidance](prompt-guidance.md).

## Choose an analysis dataset explicitly

The runnable R example declares a specific set of `(run_id, task_id, attempt_id)`
triples, requires each to match one row, and rejects selecting more than one
attempt per `(run_id, task_id)`. It chooses `attempt-1` for `pair-task-001` despite
another successful attempt. There is no generally correct automatic “latest” or
“first successful” policy, so callers must make and retain their choice.

The example selects four complete pairwise observations: three binary wins and
one permitted tie. It retains all six audit rows separately. It never drops
reversed or repeated occurrences merely because their two item IDs match.
Different traits must also be analyzed separately unless an explicit model
supports their combination.

Once `pair_analysis` has been selected using that policy:

```r
binary <- pair_analysis[
  pair_analysis$status == "complete" & !is.na(pair_analysis$result) &
    pair_analysis$result %in% c(0, 1) & !is.na(pair_analysis$better_id),
  , drop = FALSE
]
observed_ties <- pair_analysis[pair_analysis$result == 0.5, , drop = FALSE]
bt_binary <- data.frame(
  object1 = binary$ID1, object2 = binary$ID2, result = binary$result,
  stringsAsFactors = FALSE
)
bt_with_ties <- data.frame(
  object1 = pair_analysis$ID1, object2 = pair_analysis$ID2,
  result = pair_analysis$result, stringsAsFactors = FALSE
)
```

Keep the original selected rows or a parallel provenance table: three-column
model inputs cannot carry JudgmentLLM's task/occurrence/attempt identities.

## Verified pairwiseLLM source contracts

The following behavior was checked against published repository commit
`3cf49a9a2559f341e30f2e77e8df2ad4883ab7f5` on 2026-11-02. These are adapter
contracts, not a claim that all future versions behave identically.

- [`build_bt_data()`](https://github.com/shmercer/pairwiseLLM/blob/3cf49a9a2559f341e30f2e77e8df2ad4883ab7f5/R/bt_model.R#L65) accepts either `ID1/ID2/better_id` or `A_id/B_id/better_id`, preferring the former when both are present. It creates numeric `1`/`0` from the observed winner and **drops every unmatched/missing winner**, including observed ties. It returns only `object1`, `object2`, `result`; source IDs/provenance are not preserved in that table.
- [`fit_bt_model()`](https://github.com/shmercer/pairwiseLLM/blob/3cf49a9a2559f341e30f2e77e8df2ad4883ab7f5/R/bt_model.R#L454) requires exactly three columns in object1/object2/result order. Direct sirt input permits `0.5` ties, while the BradleyTerry2 wrapper rejects ties. A binary-winner adapter dropping ties and a direct tie-capable model are different workflows. Choose an engine and treatment of ties deliberately; do not use automatic engine fallback as a tie policy.
- Other explicit engines also require binary outcomes at this source revision: [`alpha`](https://github.com/shmercer/pairwiseLLM/blob/3cf49a9a2559f341e30f2e77e8df2ad4883ab7f5/R/bt_alpha.R#L147), [`brglm2`](https://github.com/shmercer/pairwiseLLM/blob/3cf49a9a2559f341e30f2e77e8df2ad4883ab7f5/R/bt_firth.R#L46), and [`lapse`](https://github.com/shmercer/pairwiseLLM/blob/3cf49a9a2559f341e30f2e77e8df2ad4883ab7f5/R/bt_lapse.R#L151) each reject `0.5`. Do not assume all BT engines support ties merely because the common input validator accepts them.
- [`build_btl_results_data()`](https://github.com/shmercer/pairwiseLLM/blob/3cf49a9a2559f341e30f2e77e8df2ad4883ab7f5/R/bayes_btl_mcmc.R#L98) requires `ID1/ID2/better_id` with an observed winner on every row. It maps presented IDs into `A_id/B_id` and positions into `winner_pos`, but creates **new** `pair_uid` values from unordered pair keys plus row occurrences. It also creates sequential `iter` and **synthetic** `received_at` values, defaulting to the Unix epoch plus one-second increments. It does not preserve original JudgmentLLM occurrence IDs or event times.
- The [pairwise result normalizer](https://github.com/shmercer/pairwiseLLM/blob/3cf49a9a2559f341e30f2e77e8df2ad4883ab7f5/R/normalize_results.R) likewise maps `ID1/ID2` into first/second `A_id/B_id`, and can preserve explicitly supplied occurrence IDs in its own collection workflow. This does not change the behavior of `build_btl_results_data()`.

```r
if (requireNamespace("pairwiseLLM", quietly = TRUE)) {
  bt_from_adapter <- pairwiseLLM::build_bt_data(binary)
  stopifnot(nrow(bt_from_adapter) == nrow(binary))
  # Optional downstream fitting, not performed by the interoperability smoke:
  # fit_binary <- pairwiseLLM::fit_bt_model(bt_binary, engine = "BradleyTerry2")
  # fit_ties <- pairwiseLLM::fit_bt_model(bt_with_ties, engine = "sirt", ignore.ties = FALSE)
}
```

For `build_btl_results_data()`, retain an explicit row crosswalk between original
run/task/request/attempt/pair IDs and the generated pairwiseLLM `pair_uid`. The
runnable script constructs that crosswalk and verifies the synthetic timestamps.
Keep the canonical audit export alongside it; do not overwrite JudgmentLLM's
identities with generated adapter identities or interpret fabricated times as
provider receipt times. Current JudgmentLLM result records have no event-time
field; any future event timestamps require their own preserved provenance.

## Rubric long-form view and probability recovery

The rubric view has one row per supplied item/trait/attempt. `response_id` aliases
`item_id`; every normalized scalar field remains, including selected label/code,
modal label/code, expected value, `primary_score`, `primary_score_kind`, provider
expected index, status, identities, and provenance. Two nested names change only
in this derived view:

- `probabilities_json`: complete ordered `{level_label, probability}` array, or missing when no probabilities exist.
- `resolved_levels_json`: complete ordered `{label, numeric_value}` mapping, including nonuniform mappings.

The example rubric audit contains three rows. Explicitly select one attempt for
each logical rubric task before analysis; the example keeps the probability
attempt for Evidence and leaves the alternate categorical attempt in the audit.
Then keep score kinds distinct:

```r
rubric_audit <- read.csv("rubric_r.csv", colClasses = "character",
  stringsAsFactors = FALSE, check.names = FALSE, na.strings = "NA"
)
# After explicit run/task/attempt selection:
expected <- rubric_analysis[rubric_analysis$primary_score_kind == "expected", ]
categorical <- rubric_analysis[rubric_analysis$primary_score_kind == "category_code", ]
expected$expected_value <- as.numeric(expected$expected_value)
categorical$numeric_category_value <- as.numeric(categorical$numeric_category_value)
by_item_trait <- split(rubric_analysis,
  list(rubric_analysis$item_id, rubric_analysis$trait_id), drop = TRUE
)
if (requireNamespace("jsonlite", quietly = TRUE)) {
  probabilities <- jsonlite::fromJSON(expected$probabilities_json[[1L]])
  coding <- jsonlite::fromJSON(expected$resolved_levels_json[[1L]])
  recovered <- sum(probabilities$probability * coding$numeric_value)
  stopifnot(recovered == expected$expected_value[[1L]])
}
```

For the synthetic Evidence mapping `[1,3,8]` and probabilities `[0.1,0.25,0.65]`,
the expectation is `6.05`. The selected category is `"2"`, the mode is `"3"`, and
any provider-reported index remains separate. The Organization categorical
assignment `Proficient` has numeric code `2`, no invented probabilities, and no
expected value. Do not average these two score kinds as if they were the same
measurement, and do not silently round expected scores.

## Standing R acceptance requirements

Schema, export, and CLI changes in every subsequent milestone must continue to
pass the Python-to-R file round trip. Preserve character IDs and leading zeros;
original and presented orientation; repeated occurrences and every attempt;
observed wins/ties versus unsuccessful records; exact category labels and
mappings; complete probabilities; and categorical versus expected-score kinds.
Analysis examples must select attempts explicitly and retain the audit source.
Compatibility work cannot be postponed entirely until the Milestone 6 wrapper.

The initial Subphase 2 generation foundation extends offline provenance checks
to actual Python-generated generation commitments consumed with `jsonlite` in R.
Verify exact requested values, unset-member omission and separate evidence/
requested-settings identity without changing normalized result columns. These
generation-only fixtures are not complete executable manifests: they do not
replace source freezing, full preflight or the later installed-CLI lifecycle
checks. Milestone 6 additionally tests that `rubricLLM` supplies this same run
configuration without silently changing generation settings.

Run the generation-only provenance check independently of the later lifecycle CLI:

```bash
uv run --offline --no-sync python scripts/generation_r_fixture.py --phase persist --output /tmp/judgmentllm-generation-r
uv run --offline --no-sync python scripts/generation_r_fixture.py --phase export --output /tmp/judgmentllm-generation-r
JUDGMENTLLM_R_STRICT=1 Rscript --vanilla examples/generation_interoperability.R /tmp/judgmentllm-generation-r
```

Subphase 2 additionally tests complete source preparation through separate Python
freeze and reopen/export processes followed by R/jsonlite. The fixture uses actual
full/minimal rubric and pairwise manifests, exact source commitments, multiline
CSV and JSONL indexes, character IDs with leading zeros, original and presented
pair orientation, and permitted missing retention/evidence fields. It creates no
provider judgments or attempts and does not require lifecycle CLI commands.

```bash
uv run --offline --no-sync python scripts/source_preparation_r_fixture.py --phase persist --output /tmp/judgmentllm-source-preparation-r
uv run --offline --no-sync python scripts/source_preparation_r_fixture.py --phase export --output /tmp/judgmentllm-source-preparation-r
JUDGMENTLLM_R_STRICT=1 Rscript --vanilla examples/source_preparation_interoperability.R /tmp/judgmentllm-source-preparation-r
```

All three existing export/persistence/generation gates remain mandatory alongside
this fourth required source-preparation gate.

Required steps in the existing Ubuntu/Python 3.11 matrix job generate the
synthetic exports, import them in R, and require `jsonlite` to recover complete
distributions and mappings. CI installs R 4.6.2 and pinned `jsonlite` 2.0.0 and
sets `JUDGMENTLLM_R_STRICT=1`. Missing R or `jsonlite` is a failure of that check,
not a skipped success. Optional pairwiseLLM adapter checks remain useful but are
not prerequisites for the mandatory file-contract test; CI does not install
pairwiseLLM or Stan. The existing cross-platform Python/coverage workflow remains
in place.

Run the strict file-contract check locally after generating the synthetic files:

```bash
JUDGMENTLLM_R_STRICT=1 Rscript --vanilla examples/r_interoperability.R /tmp/judgmentllm-r
```

The strict flag requires `jsonlite`; it does not require the optional
pairwiseLLM adapters or enable statistical fitting.

## Verification and limits

The Python suite verifies export columns, reversal mappings, all-attempt
retention, failed outcomes, ties, absent probabilities, leading-zero IDs, lossless
nested data, mutation rejection, and iterator consumption. The runnable R script
verifies the same synthetic import without fitting a model. The verification record
includes exact local R/package versions and command results in the
[Milestone 1 verification report](milestone-1-verification.md).

For a local exploratory run, missing optional packages are reported separately;
JSON recovery remains pending without `jsonlite`, and pairwiseLLM adapter checks
remain pending without that package. This does not satisfy the required CI gate,
which installs R/`jsonlite` and fails if full JSON verification cannot run. Python
tests remain required. No R package becomes a core Python dependency, and no
network/provider request is made by the examples.

Subphase 3 adds a fifth strict offline execution gate without introducing the
Subphase 5 lifecycle CLI. One Python process executes four full/minimal rubric/
pairwise runs; another reopens them, resumes committed results, and exports actual
normalized results plus an evidence sidecar. Base R/jsonlite checks the joins to
immutable attempts/submissions/observations and independent acceptance counts,
character IDs, reversed orientation, explanations, nonuniform coding, complete
probabilities, score kinds and missing retained paths. No v1 result or R-export
column changes are required.

```bash
uv run --offline --no-sync python scripts/execution_r_fixture.py --phase persist --output /tmp/judgmentllm-execution-r
uv run --offline --no-sync python scripts/execution_r_fixture.py --phase export --output /tmp/judgmentllm-execution-r
JUDGMENTLLM_R_STRICT=1 Rscript --vanilla examples/execution_interoperability.R /tmp/judgmentllm-execution-r
```

Missing Rscript/jsonlite fails the required Ubuntu/Python 3.11 step. This gate
uses neither processx nor an installed rubricLLM wrapper. CLI lifecycle and
failed-member retry acceptance remain in their assigned later subphases.

Subphase 4 adds a sixth required offline gate. Its first Python process executes
grouped rubric observations with two successful members and one invalid member,
publishes an incomplete export, and authorizes an exact failed-member retry.
Another Python process reopens the durable history and publishes canonical JSONL,
canonical CSV and R-compatible CSV through the internal immutable export service.
Base R/jsonlite verifies original and retry membership, all result/attempt/
submission/observation links, independent acceptance counts, complete nonuniform
category mappings and distributions, selected versus expected scores, leading-zero
IDs, unsuccessful null fields and minimal-capture paths. Original unsuccessful
rows and earlier immutable export snapshots remain available after retry.

```bash
uv run --offline --no-sync python scripts/grouped_lifecycle_r_fixture.py --phase persist --output /tmp/judgmentllm-grouped-lifecycle-r
uv run --offline --no-sync python scripts/grouped_lifecycle_r_fixture.py --phase export --output /tmp/judgmentllm-grouped-lifecycle-r
JUDGMENTLLM_R_STRICT=1 Rscript --vanilla examples/grouped_lifecycle_interoperability.R /tmp/judgmentllm-grouped-lifecycle-r
```

All five earlier strict gates remain required. This sixth gate uses only base R
and jsonlite. At this stage installed-CLI lifecycle testing was assigned to
Subphase 5; the next section documents that implemented seventh gate.


## Subphase 5: installed-CLI lifecycle acceptance

A seventh required gate launches the installed `judgmentllm` console executable
for validation, planning, execution, status/listing, recovery, explicit subset
retry and immutable export. Preserve all six earlier strict gates. The fixture
uses independent processes for whole run operations and requires only base R
and jsonlite; it does not install processx, rubricLLM, pairwiseLLM or Stan.

```bash
uv run --offline --no-sync python scripts/cli_lifecycle_r_gate.py --output /tmp/judgmentllm-cli-lifecycle-r
```

Use a fresh output directory. The wrapper resolves the installed console beside
its Python interpreter and passes explicit absolute paths to R. The equivalent
R invocation takes the output directory, installed executable, fixture Python,
and helper path:

```bash
Rscript --vanilla examples/cli_lifecycle_interoperability.R OUT ABS_CONSOLE ABS_PYTHON ABS_HELPER
```

Each synchronous base-R `system2()` invocation writes stdout and stderr to
separate files. R independently validates one version-1 stdout control object,
every stderr diagnostic/progress JSONL record, and the process exit status.
This avoids platform-dependent stdout/stderr merging, especially on Windows.
Execution and export data are read from returned durable artifacts, not from
stdout result streams.

Small full/minimal independent/grouped rubric and pairwise fixtures retain
leading-zero character IDs, original/reversed presentations, nonuniform category
mappings, complete probabilities, selected/modal/expected distinctions, missing
values and all attempts. Grouped failed-member retries retain unsuccessful
originals and use one durable retry operation per original request. Reusing an
operation cannot create another attempt; conflicting reuse and unapproved
completed-task selection fail. Independent ledger reads verify acceptance counts
rather than infer duplicate safety from result counts.

The accepted-but-unavailable fixture recovers the original observation when
retrieval becomes available. A separate real-process termination fixture waits
for one committed result and a later durable submitting boundary, terminates
the actual owner, then inspects/resumes/exports in new processes. Authoritative
nonacceptance does not fabricate a result or create missing retry authority.
Hard termination need not emit a final control envelope. POSIX graceful SIGINT
and portable cancellation-boundary tests cover handled exit 130 separately.

The helper writes synthetic fixture inputs and inspects independent SQLite
records. It does not perform lifecycle operations in place of the executable.
Expensive fault-window matrices and scale tests remain Python regressions;
the R gate does not wait out synthetic long delays or run scale qualification.
Missing executable/Rscript/jsonlite or failed assertions fail required acceptance.
Hosted Ubuntu/Python 3.11 and all seven current local OS/Python configurations
require this gate: Linux Python 3.11/3.12/3.15 and Windows/macOS Python 3.11/3.12.
Together the seven strict gates require 49 local executions. Historical Subphase 6
qualification used six local Python 3.11/3.12 configurations and 42 executions;
that retained evidence is not relabeled as the current seven-configuration grid.


## Proposed Milestone 3 usage and evidence joins (proposed, not implemented)

See the [consolidated assessment output design](assessment-output-design.md)
for explicit future version-2 RunSpec/renderer controls, unchanged normalized
result schema 1, linked request/submission/observation usage and authorized
reasoning records, streaming companion exports and R join/aggregation rules.
Existing columns, frozen records, identities and missing-value semantics in
this document remain implemented contracts. New sidecars require their own
typed versions and deterministic offline Python/CLI-to-R acceptance; grouped
requests cannot multiply token usage or duplicate reasoning by trait. Any
internal persistence revision requires deliberate compatibility handling, not
implicit mutation/migration of an existing registry. Credential diagnostics
never add credential values or private identities to assessment evidence.


The proposed `ProviderObservationMetadata` sidecar version 1 is exported once
per provider observation, including observations with null usage. Usage exports
include this canonical JSONL/CSV or R-compatible audit companion and identify it
through nullable `EvidenceManifest.observation_file` (`ExportFile` path/hash/
length); absent legacy metadata remains unavailable. Retain `requested_order`
(`together|scores_first|explanations_first|null`, null for pairwise),
`observed_key_order` (original emitted root key list or null), `member_key_orders`
(a list of `{array, index, trait_id|null, keys|null}` records for per-entry order),
`order_adherence` (`adherent|mismatch|unknown`), `order_evidence`
(`raw_output_text|raw_tokens|reassembled_stream|unavailable`) and nullable safe
`unknown_reason`.

CSV root/member key lists use JSON cells, and unavailable order uses explicit
missing values rather than an empty list or inferred sorted order. Import IDs
as character and recover nested values with `jsonlite`. Use the existing
run/request/attempt/submission/observation bridge for joins to assessment rows,
without duplicating observation metadata across grouped traits. Deterministic R
acceptance covers adherent, mismatched and unknown root/member order under
full/minimal capture; minimal retains only safely observable nonsensitive fields
under the authoritative retention policy. Public metadata retains only
allowlisted contract key names and known trait IDs. Unsafe keys produce a null
affected sequence, unknown adherence and `unknown_reason: unsafe_key_names`;
unknown trait IDs become null with stable indices and content-free counts. R
fixtures must verify those nulls and codes without retaining arbitrary names.
Order mismatch remains diagnostic and does not alter valid scores, retry authority or normalized-result/R outcome
semantics. Visible order is not evidence of internal model reasoning order.


## Phase 5A OpenAI adapter acceptance

Required deterministic fixtures exercise actual pinned-SDK HTTP transports through
production preparation and durable execution for both OpenAI generative endpoints.
Existing result exports and evidence readers feed R assertions for character IDs,
category mappings, original/presented pair orientation, unsuccessful attempts,
nullable usage, and grouped result-to-physical-usage joins. Companion usage and
reasoning export interfaces remain Phase 6. None of these tests contacts OpenAI.


The same required execution gate also consumes additive assessment diagnostics
from new OpenAI receipts and separately reopened read-only inspections. It checks
identity joins, fixed parser codes, exact text hash/length, full versus minimal
retention, missing historical metadata, and exclusion of text from serialized
inspection metadata. Synthetic malformed assessments remain invalid; their
protected text is retained only for full capture. Existing result rows, complete
category coding, leading-zero IDs, pair orientation, missing values and physical
usage aggregation remain independently asserted. These fixture sidecars are test
artifacts, not Phase 6 companion export interfaces or live qualification evidence.


## Phase 5B Anthropic adapter acceptance

The existing required execution gate now also generates Anthropic rubric and
pairwise outputs through the actual pinned SDK with a simulated HTTP response.
R/jsonlite checks full and minimal capture, character IDs with leading zeros,
multiple traits, selected categorical scores with null expected scores and
distributions, original/presented orientation, unsuccessful grouped siblings,
explicit retry attempts, physical observations and durable diagnostic links.
Known uncached/cache creation/read counters and missing usage remain distinct;
totals stay null. Read-only reopened diagnostics retain their original identity
and never expose assessment text in serialized metadata. The existing seven
mandatory gates and49 local executions are preserved; no separate pipeline or
Phase6 usage/reasoning export API is introduced. No real provider is contacted.

## Phase 5C Gemini Developer adapter acceptance

The same execution gate adds actual pinned google-genai SDK execution through
synthetic HTTP. Python creates full/minimal rubric and reversed pairwise records,
an invalid grouped sibling and an explicit subset retry, then reopens immutable
evidence and exports. R checks character IDs, all attempts, category mappings,
null probabilities/expected values, original/presented order and observation
joins. Usage preserves prompt/candidate/thinking/cache/total classes once per
physical observation and missing counters as null. Diagnostic 3 keeps safe finish
reasons and full versus hash-only assessment capture; thought text has no retained
artifact. No result/export columns or Phase 6 APIs change. Applicable gate
selection follows [the risk policy](ci-optimization.md); Level 3 requires the seven
strict Linux R gates, without repeating a full local grid.
