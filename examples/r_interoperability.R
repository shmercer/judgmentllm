# Offline import/preparation example. No provider calls or statistical fitting.
# First: uv run --offline --no-sync python examples/export_r.py --output /tmp/judgmentllm-r
# Then:  Rscript --vanilla examples/r_interoperability.R /tmp/judgmentllm-r
# CI requires JSON recovery: JUDGMENTLLM_R_STRICT=1 Rscript --vanilla ...
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) {
  stop("Usage: Rscript examples/r_interoperability.R EXPORT_DIRECTORY")
}
export_directory <- args[[1L]]
strict <- identical(Sys.getenv("JUDGMENTLLM_R_STRICT"), "1")
has_jsonlite <- requireNamespace("jsonlite", quietly = TRUE)
if (strict && !has_jsonlite) {
  stop("Strict R interoperability requires jsonlite; JSON checks cannot be skipped")
}

read_audit <- function(filename, required_columns) {
  # Read every column as character before converting named measurements only.
  # This protects identifiers such as '001', category labels, and JSON columns.
  data <- utils::read.csv(file.path(export_directory, filename),
    colClasses = "character", stringsAsFactors = FALSE, check.names = FALSE,
    na.strings = "NA", fileEncoding = "UTF-8", comment.char = ""
  )
  missing <- setdiff(required_columns, names(data))
  if (length(missing)) stop("Missing required CSV columns: ", paste(missing, collapse = ", "))
  stopifnot(all(vapply(data, is.character, logical(1L))))
  data
}
convert_numeric <- function(data, columns) {
  for (column in intersect(columns, names(data))) {
    original <- data[[column]]
    converted <- as.numeric(original)
    stopifnot(all(is.na(original) | is.finite(converted)))
    data[[column]] <- converted
  }
  data
}

convert_logical <- function(data, columns) {
  for (column in intersect(columns, names(data))) {
    original <- data[[column]]
    stopifnot(all(is.na(original) | original %in% c("TRUE", "FALSE")))
    converted <- as.logical(original)
    if (any(!is.na(original) & is.na(converted))) stop("Invalid logical CSV value")
    data[[column]] <- converted
  }
  data
}

# Explicit run/task/attempt selection is mandatory for an analysis view. This
# deliberately does not select 'first', 'latest', or every successful retry.
select_attempts <- function(audit, choices) {
  index <- vapply(seq_len(nrow(choices)), function(i) {
    matching <- which(
      audit$run_id == choices$run_id[[i]] &
        audit$task_id == choices$task_id[[i]] &
        audit$attempt_id == choices$attempt_id[[i]]
    )
    if (length(matching) != 1L) stop("Each chosen attempt must match exactly one audit row")
    matching
  }, integer(1L))
  selected <- audit[index, , drop = FALSE]
  if (anyDuplicated(selected[c("run_id", "task_id")])) {
    stop("Select only one attempt per logical task for this analysis")
  }
  if (any(selected$status != "complete")) stop("Analysis choices must be successful")
  selected
}

identity_columns <- c("run_id", "task_id", "request_uid", "attempt_id", "status", "provider", "model")
pair_audit <- read_audit("pairwise_r.csv", c(
  identity_columns, "ID1", "ID2", "A_id", "B_id", "better_id", "result", "winner_pos",
  "pair_uid", "pair_id", "trait_id", "left_item_id", "right_item_id",
  "original_order", "presented_order", "source_pair_uid", "repetition_index",
  "is_reversal", "reversal_group", "p_left", "p_right", "p_first", "p_second", "p_tie",
  "tie", "allow_ties", "endpoint"
))
pair_audit <- convert_numeric(pair_audit, c(
  "result", "winner_pos", "repetition_index", "p_left", "p_right", "p_first", "p_second", "p_tie"
))
pair_audit <- convert_logical(pair_audit, c("is_reversal", "tie", "allow_ties"))
stopifnot(
  is.logical(pair_audit$is_reversal), is.logical(pair_audit$tie),
  is.logical(pair_audit$allow_ties), is.numeric(pair_audit$result),
  is.numeric(pair_audit$p_first), is.numeric(pair_audit$p_second)
)
stopifnot(nrow(pair_audit) == 6L, all(c("001", "002") %in% pair_audit$ID1))
stopifnot(is.character(pair_audit$ID1), is.character(pair_audit$pair_uid))
stopifnot(
  all(pair_audit$A_id == pair_audit$ID1), all(pair_audit$B_id == pair_audit$ID2),
  all(pair_audit$left_item_id == "001"), all(pair_audit$right_item_id == "002"),
  all(pair_audit$run_id == "synthetic-run"), all(pair_audit$trait_id == "quality"),
  all(pair_audit$provider == "synthetic"), all(pair_audit$endpoint == "offline"),
  all(pair_audit$model == "fixture"), length(unique(pair_audit$pair_uid)) == 5L,
  length(unique(pair_audit$task_id)) == 5L
)
first_is_left <- pair_audit$ID1 == pair_audit$left_item_id
stopifnot(
  identical(pair_audit$p_first, ifelse(first_is_left, pair_audit$p_left, pair_audit$p_right)),
  identical(pair_audit$p_second, ifelse(first_is_left, pair_audit$p_right, pair_audit$p_left))
)
retries <- pair_audit[pair_audit$task_id == "pair-task-001", , drop = FALSE]
stopifnot(
  nrow(retries) == 2L, all(retries$status == "complete"),
  identical(retries$attempt_id, c("attempt-1", "attempt-2")),
  all(retries$pair_uid == "pair-001")
)
no_probs <- pair_audit[pair_audit$task_id == "pair-task-005", , drop = FALSE]
stopifnot(
  nrow(no_probs) == 1L,
  all(is.na(no_probs[c("p_left", "p_right", "p_first", "p_second", "p_tie")]))
)
reversed <- pair_audit[pair_audit$pair_uid == "pair-002", , drop = FALSE]
stopifnot(
  nrow(reversed) == 1L, reversed$is_reversal, reversed$ID1 == "002", reversed$ID2 == "001",
  reversed$better_id == "001", reversed$result == 0, reversed$winner_pos == 2,
  reversed$p_left == 0.7, reversed$p_right == 0.3,
  reversed$p_first == 0.3, reversed$p_second == 0.7
)
failed <- pair_audit[pair_audit$status != "complete", , drop = FALSE]
stopifnot(
  nrow(failed) == 1L, failed$status == "errored", is.na(failed$result),
  is.na(failed$winner_pos), is.na(failed$better_id), failed$p_first == 0.9
)

pair_choices <- data.frame(
  run_id = "synthetic-run",
  task_id = c("pair-task-001", "pair-task-002", "pair-task-003", "pair-task-005"),
  attempt_id = "attempt-1", stringsAsFactors = FALSE
)
pair_analysis <- select_attempts(pair_audit, pair_choices)
stopifnot(nrow(pair_analysis) == 4L, any(pair_analysis$result == 0.5))
# Reversals and independent repeat occurrences stay separate rows. The unused
# second successful attempt and the failed attempt remain in pair_audit.
stopifnot(length(unique(pair_analysis$pair_uid)) == nrow(pair_analysis))

# Direct three-column input preserves the explicitly selected observed tie.
# Only engines documented to support ties may consume this table unchanged.
bt_with_ties <- data.frame(
  object1 = pair_analysis$ID1, object2 = pair_analysis$ID2,
  result = pair_analysis$result, stringsAsFactors = FALSE
)
stopifnot(ncol(bt_with_ties) == 3L, is.numeric(bt_with_ties$result))

# build_bt_data() is a binary-winner adapter. Explicitly separate ties first;
# never rely on its implicit missing-winner drop as an analysis policy.
binary <- pair_analysis[
  !is.na(pair_analysis$result) & pair_analysis$result %in% c(0, 1) &
    !is.na(pair_analysis$better_id), , drop = FALSE
]
observed_ties <- pair_analysis[pair_analysis$result == 0.5, , drop = FALSE]
stopifnot(
  nrow(binary) == 3L, nrow(observed_ties) == 1L,
  observed_ties$tie, observed_ties$allow_ties, is.na(observed_ties$better_id),
  is.na(observed_ties$winner_pos), observed_ties$p_tie == 0.5,
  all(binary$result == ifelse(binary$better_id == binary$ID1, 1, 0)),
  all(binary$winner_pos == ifelse(binary$better_id == binary$ID1, 1, 2))
)
bt_binary <- data.frame(
  object1 = binary$ID1, object2 = binary$ID2,
  result = binary$result, stringsAsFactors = FALSE
)
# Keep identifiers outside the strictly three-column model input.
binary_provenance <- binary[c("run_id", "task_id", "request_uid", "attempt_id", "pair_uid", "pair_id", "trait_id")]
stopifnot(nrow(binary_provenance) == nrow(bt_binary))

if (requireNamespace("pairwiseLLM", quietly = TRUE)) {
  from_builder <- pairwiseLLM::build_bt_data(binary)
  stopifnot(identical(as.data.frame(from_builder), bt_binary))
  # Verify documented behavior: passing the selected tie yields no tie row.
  stopifnot(nrow(pairwiseLLM::build_bt_data(pair_analysis)) == nrow(binary))

  imported <- pairwiseLLM::build_btl_results_data(binary[c("ID1", "ID2", "better_id")],
    phase = "phase2", backend = "judgmentllm_derived_view", model = "synthetic"
  )
  # This builder replaces occurrence IDs and fabricates epoch-based receipt
  # times. It is an adapter, not a lossless import of JudgmentLLM provenance.
  btl_crosswalk <- data.frame(
    judgmentllm_pair_uid = binary$pair_uid,
    judgmentllm_run_id = binary$run_id,
    judgmentllm_task_id = binary$task_id,
    judgmentllm_request_uid = binary$request_uid,
    judgmentllm_attempt_id = binary$attempt_id,
    pairwiseLLM_pair_uid = imported$pair_uid,
    stringsAsFactors = FALSE
  )
  stopifnot(
    nrow(btl_crosswalk) == nrow(binary),
    all(btl_crosswalk$judgmentllm_pair_uid != btl_crosswalk$pairwiseLLM_pair_uid),
    all(imported$A_id == binary$ID1), all(imported$B_id == binary$ID2),
    all(imported$winner_pos == binary$winner_pos),
    as.numeric(imported$received_at[[1L]]) == 0,
    identical(as.numeric(diff(imported$received_at)), rep(1, nrow(imported) - 1L))
  )
  cat("pairwiseLLM", as.character(utils::packageVersion("pairwiseLLM")), "binary/BTL adapters passed\n")
} else {
  cat("pairwiseLLM not installed: optional adapter checks pending\n")
}

rubric_audit <- read_audit("rubric_r.csv", c(
  identity_columns, "response_id", "item_id", "trait_id", "category_label",
  "numeric_category_value", "modal_category_label", "modal_numeric_category_value",
  "expected_value", "primary_score", "primary_score_kind", "provider_expected_index",
  "probabilities_json", "resolved_levels_json"
))
rubric_audit <- convert_numeric(rubric_audit, c(
  "numeric_category_value", "modal_numeric_category_value", "expected_value", "primary_score", "provider_expected_index"
))
rubric_choices <- data.frame(
  run_id = "synthetic-run", task_id = c("rubric-task-001", "rubric-task-002"),
  attempt_id = "attempt-1", stringsAsFactors = FALSE
)
rubric_analysis <- select_attempts(rubric_audit, rubric_choices)
stopifnot(
  nrow(rubric_audit) == 3L, nrow(rubric_analysis) == 2L,
  is.character(rubric_audit$item_id), all(rubric_audit$item_id == "001"),
  is.character(rubric_audit$category_label), is.numeric(rubric_audit$primary_score),
  all(rubric_audit$provider == "synthetic"), all(rubric_audit$model == "fixture")
)
stopifnot(all(rubric_analysis$response_id == rubric_analysis$item_id))
by_item_trait <- split(rubric_analysis, list(rubric_analysis$item_id, rubric_analysis$trait_id), drop = TRUE)
stopifnot(length(by_item_trait) == 2L)
categorical <- rubric_analysis[rubric_analysis$primary_score_kind == "category_code", , drop = FALSE]
expected <- rubric_analysis[rubric_analysis$primary_score_kind == "expected", , drop = FALSE]
stopifnot(
  nrow(categorical) == 1L, nrow(expected) == 1L,
  categorical$category_label == "Proficient", categorical$primary_score == 2,
  is.na(categorical$expected_value), is.na(categorical$probabilities_json),
  expected$category_label == "2", expected$modal_category_label == "3",
  expected$expected_value == 6.05, expected$primary_score == 6.05,
  expected$numeric_category_value == 3, expected$modal_numeric_category_value == 8,
  expected$provider_expected_index == 1.55
)
if (has_jsonlite) {
  for (i in seq_len(nrow(pair_audit))) {
    stopifnot(
      identical(jsonlite::fromJSON(pair_audit$original_order[[i]]),
        c(pair_audit$left_item_id[[i]], pair_audit$right_item_id[[i]])),
      identical(jsonlite::fromJSON(pair_audit$presented_order[[i]]),
        c(pair_audit$ID1[[i]], pair_audit$ID2[[i]]))
    )
  }
  probabilities <- jsonlite::fromJSON(expected$probabilities_json[[1L]])
  coding <- jsonlite::fromJSON(expected$resolved_levels_json[[1L]])
  categorical_coding <- jsonlite::fromJSON(categorical$resolved_levels_json[[1L]])
  stopifnot(
    identical(probabilities$level_label, c("1", "2", "3")),
    identical(probabilities$probability, c(0.1, 0.25, 0.65)),
    identical(coding$label, c("1", "2", "3")),
    identical(coding$numeric_value, c(1, 3, 8)),
    identical(categorical_coding$label, c("Beginning", "Developing", "Proficient", "Advanced")),
    identical(categorical_coding$numeric_value, c(0, 1, 2, 3)),
    sum(probabilities$probability * coding$numeric_value) == expected$expected_value
  )
  cat("jsonlite", as.character(utils::packageVersion("jsonlite")), "probability/mapping recovery and presentation JSON checks passed\n")
} else {
  cat("jsonlite not installed: optional JSON decoding check pending\n")
}
# Phase 1 contracts are interpreted offline; the installed CLI remains v1.
# Separate files preserve the original baseline export rows above.
contract_rubric <- read_audit("assessment_contract_rubric_r.csv", c(
  identity_columns, "schema_version", "item_id", "trait_id", "category_label",
  "numeric_category_value", "primary_score", "primary_score_kind", "expected_value",
  "explanation", "probabilities_json", "resolved_levels_json", "modal_category_label"
))
contract_rubric <- convert_numeric(contract_rubric, c(
  "numeric_category_value", "primary_score", "expected_value"
))
stopifnot(
  nrow(contract_rubric) == 8L, all(contract_rubric$schema_version == "1"),
  all(contract_rubric$item_id == "001"), is.character(contract_rubric$item_id),
  is.character(contract_rubric$trait_id), all(contract_rubric$attempt_id == "attempt-001"),
  all(contract_rubric$provider == "synthetic"), all(contract_rubric$endpoint == "offline"),
  all(contract_rubric$model == "contract-fixture"),
  all(is.na(contract_rubric$expected_value)), all(is.na(contract_rubric$probabilities_json)),
  all(is.na(contract_rubric$modal_category_label))
)
layouts <- c("together", "scores_first", "explanations_first")
layout_rows <- lapply(layouts, function(layout) {
  selected <- contract_rubric[
    contract_rubric$run_id == paste0("contract-", layout), , drop = FALSE
  ]
  selected <- selected[order(selected$trait_id), , drop = FALSE]
  stopifnot(
    nrow(selected) == 2L, identical(selected$trait_id, c("001", "002")),
    length(unique(selected$task_id)) == 2L, length(unique(selected$request_uid)) == 1L,
    all(selected$status == "complete"), all(selected$primary_score_kind == "category_code"),
    identical(selected$category_label, c("middle", "2")),
    identical(selected$primary_score, c(3, 40)),
    identical(selected$numeric_category_value, c(3, 40)),
    identical(selected$explanation, c("Assessment for 001.", "Assessment for 002."))
  )
  selected
})
equivalent_columns <- c(
  "task_id", "trait_id", "category_label", "numeric_category_value", "primary_score",
  "primary_score_kind", "explanation", "resolved_levels_json"
)
for (selected in layout_rows[-1L]) {
  for (column in equivalent_columns) {
    stopifnot(identical(selected[[column]], layout_rows[[1L]][[column]]))
  }
}
partial_contract <- contract_rubric[
  contract_rubric$run_id == "contract-partial", , drop = FALSE
]
stopifnot(
  nrow(partial_contract) == 2L, length(unique(partial_contract$task_id)) == 2L,
  length(unique(partial_contract$request_uid)) == 1L,
  identical(partial_contract$status, c("complete", "invalid")),
  partial_contract$trait_id[[1L]] == "001", partial_contract$primary_score[[1L]] == 3,
  partial_contract$explanation[[1L]] == "Assessment for 001.",
  partial_contract$trait_id[[2L]] == "002", is.na(partial_contract$primary_score[[2L]]),
  is.na(partial_contract$category_label[[2L]]), is.na(partial_contract$explanation[[2L]])
)
contract_pairs <- read_audit("assessment_contract_pairwise_r.csv", c(
  identity_columns, "schema_version", "ID1", "ID2", "left_item_id", "right_item_id",
  "better_id", "result", "winner_pos", "tie", "allow_ties", "is_reversal",
  "source_pair_uid", "repetition_index", "p_left", "p_right", "p_first", "p_second", "p_tie"
))
contract_pairs <- convert_numeric(contract_pairs, c("result", "winner_pos", "repetition_index"))
contract_pairs <- convert_logical(contract_pairs, c("tie", "allow_ties", "is_reversal"))
stopifnot(
  nrow(contract_pairs) == 4L, all(contract_pairs$status == "complete"),
  all(contract_pairs$schema_version == "1"), all(contract_pairs$left_item_id == "001"),
  all(contract_pairs$right_item_id == "002"), all(contract_pairs$source_pair_uid == "001"),
  all(contract_pairs$repetition_index == 1),
  all(is.na(contract_pairs[c("p_left", "p_right", "p_first", "p_second", "p_tie")])),
  length(unique(contract_pairs$task_id)) == 4L,
  contract_pairs$better_id[[1L]] == "002", !contract_pairs$tie[[1L]],
  !contract_pairs$allow_ties[[1L]], contract_pairs$result[[1L]] == 0,
  contract_pairs$is_reversal[[2L]], contract_pairs$ID1[[2L]] == "002",
  contract_pairs$ID2[[2L]] == "001", contract_pairs$better_id[[2L]] == "001",
  contract_pairs$winner_pos[[2L]] == 2, contract_pairs$result[[2L]] == 0,
  contract_pairs$tie[[3L]], contract_pairs$allow_ties[[3L]],
  is.na(contract_pairs$better_id[[3L]]), is.na(contract_pairs$winner_pos[[3L]]),
  contract_pairs$result[[3L]] == 0.5, contract_pairs$allow_ties[[4L]],
  !contract_pairs$tie[[4L]], contract_pairs$better_id[[4L]] == "002"
)
if (has_jsonlite) {
  for (selected in layout_rows) {
    first_coding <- jsonlite::fromJSON(selected$resolved_levels_json[[1L]])
    second_coding <- jsonlite::fromJSON(selected$resolved_levels_json[[2L]])
    stopifnot(
      identical(first_coding$label, c("low", "middle", "high")),
      identical(first_coding$numeric_value, c(1, 3, 8)),
      identical(second_coding$label, c("1", "2")),
      identical(second_coding$numeric_value, c(10, 40))
    )
  }
  canonical <- lapply(readLines(
    file.path(export_directory, "assessment_contract_results.jsonl"), encoding = "UTF-8"
  ), jsonlite::fromJSON)
  stopifnot(length(canonical) == 12L, all(vapply(canonical, function(row) {
    identical(row$schema_version, 1L)
  }, logical(1L))))
}
cat("Assessment contracts R smoke passed: 3 layouts, grouped partial outcomes, winner-only/historical/tie outputs\n")
cat(if (strict) "Strict mode: required jsonlite checks completed\n" else "Local mode: jsonlite checks remain optional\n")
cat("R interoperability smoke passed: 6 pair audit rows, 4 chosen outcomes, 3 binary wins, 1 retained tie, 3 rubric audit rows, 2 chosen rubric outcomes\n")
