# R interoperability: explicit audit and analysis views

Milestone 1 supplies provider-independent CSV views for R through
`judgmentllm.r_exports.export_pairwise_r_csv` and `export_rubric_r_csv`. They need
neither R nor pandas in the Python installation. R interoperability remains a
contract for later milestones; the fuller R bridge/wrapper work stays in
Milestone 6. Statistical estimation belongs in R, not JudgmentLLM's Python core.

These exports are **all-attempts audit views**: every supplied record remains,
including failures, observed ties, reversals, repeated judgments, and multiple
attempts for the same task. They perform no implicit success filter, retry
selection, aggregation, or winner inference. The canonical normalized JSONL and
lossless normalized CSV remain authoritative and keep their existing formats.

## Generate and read synthetic results

From the repository checkout:

```bash
uv run --offline --no-sync python examples/export_r.py --output /tmp/judgmentllm-r
Rscript examples/r_interoperability.R /tmp/judgmentllm-r
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

## Verification and limits

The Python suite verifies export columns, reversal mappings, all-attempt
retention, failed outcomes, ties, absent probabilities, leading-zero IDs, lossless
nested data, mutation rejection, and iterator consumption. The runnable R script
verifies the same synthetic import without fitting a model. The verification record
includes exact local R/package versions and command results in the
[Milestone 1 verification report](milestone-1-verification.md).

If R or optional packages are absent, run the two commands at the top after
installing them separately; Python tests remain required, and R-side checks are
reported pending rather than treated as completed. No R package becomes a core
Python dependency, and no network/provider request is made by the examples.
