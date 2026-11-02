# Required Subphase 1 persistence round trip. No provider, CLI lifecycle, or R wrapper.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) stop("Supply the persistence fixture output directory")
if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Required jsonlite is unavailable")
root <- args[[1L]]
read_audit <- function(name) {
  read.csv(file.path(root, name), colClasses = "character", na.strings = "NA",
    check.names = FALSE, stringsAsFactors = FALSE, fileEncoding = "UTF-8", comment.char = "")
}
rubric <- read_audit("rubric_r.csv")
pairs <- read_audit("pairwise_r.csv")
index <- jsonlite::fromJSON(file.path(root, "persistence_index.json"))
stopifnot(index$schema_version == 1L)
stopifnot(identical(index$request_members, c("evidence", "organization", "clarity")))
stopifnot(identical(index$retry_members, "organization"))
stopifnot(index$transport_observations == 0L, index$transport_failure_evidence == 1L)
stopifnot(nrow(rubric) == 6L, nrow(pairs) == 3L)
stopifnot(all(rubric$item_id %in% c("001", "002", "003")))
stopifnot(all(pairs$left_item_id == "001"), all(pairs$right_item_id == "002"))
for (audit in list(rubric, pairs)) {
  identities <- paste(audit$run_id, audit$task_id, audit$attempt_id, sep = "|")
  stopifnot(!anyDuplicated(identities))
  stopifnot(all(audit$provider == "fixture"), all(audit$endpoint == "offline"))
  stopifnot(all(audit$model == "synthetic-v1"), all(audit$mode == "live"))
  stopifnot(all(nchar(audit$manifest_sha256) == 64L))
  stopifnot(all(is.na(audit$raw_response_path)), all(is.na(audit$prompt_path)))
  stopifnot(all(is.na(audit$input_tokens)), all(is.na(audit$output_tokens)))
}
first <- rubric[rubric$attempt_id == "grouped-1", ]
stopifnot(nrow(first) == 3L)
stopifnot(identical(first$status, c("complete", "invalid", "invalid")))
stopifnot(identical(first$error_kind[-1L], c("invalid_output", "missing_output")))
failed <- rubric[rubric$status != "complete", ]
stopifnot(nrow(failed) == 4L)
stopifnot(all(is.na(failed$primary_score)), all(is.na(failed$primary_score_kind)))
stopifnot(all(is.na(failed$expected_value)), all(is.na(failed$numeric_category_value)))
expected <- first[first$task_id == "evidence", ]
stopifnot(expected$primary_score_kind == "expected")
stopifnot(expected$category_label == "2", expected$modal_category_label == "3")
stopifnot(as.numeric(expected$expected_value) == 6.05)
stopifnot(as.numeric(expected$provider_expected_index) == 1.55)
probabilities <- jsonlite::fromJSON(expected$probabilities_json[[1L]])
coding <- jsonlite::fromJSON(expected$resolved_levels_json[[1L]])
stopifnot(identical(probabilities$level_label, c("1", "2", "3")))
stopifnot(identical(probabilities$probability, c(0.1, 0.25, 0.65)))
stopifnot(identical(coding$numeric_value, c(1, 3, 8)))
stopifnot(sum(probabilities$probability * coding$numeric_value) == 6.05)
retry <- rubric[rubric$attempt_id == "grouped-2", ]
stopifnot(nrow(retry) == 1L, retry$task_id == "organization", retry$status == "complete")
stopifnot(retry$request_uid == "grouped-request", retry$primary_score_kind == "category_code")
stopifnot(as.numeric(retry$primary_score) == 8, is.na(retry$expected_value))
stopifnot(nrow(rubric[rubric$task_id == "organization", ]) == 2L)
stopifnot(nrow(rubric[rubric$task_id == "evidence", ]) == 1L)
stopifnot(rubric$status[rubric$task_id == "refusal"] == "refused")
stopifnot(rubric$error_kind[rubric$task_id == "refusal"] == "provider_refusal")
stopifnot(rubric$status[rubric$task_id == "transport"] == "errored")
stopifnot(rubric$error_kind[rubric$task_id == "transport"] == "transport_not_submitted")
reversed <- pairs[pairs$task_id == "reversed", ]
stopifnot(reversed$ID1 == "002", reversed$ID2 == "001", reversed$better_id == "001")
stopifnot(as.numeric(reversed$result) == 0, as.numeric(reversed$winner_pos) == 2)
stopifnot(as.numeric(reversed$p_left) == 0.8, as.numeric(reversed$p_first) == 0.2)
stopifnot(as.numeric(reversed$p_right) == 0.2, as.numeric(reversed$p_second) == 0.8)
stopifnot(reversed$is_reversal == "TRUE", reversed$source_pair_uid == "source-reversed")
stopifnot(as.numeric(reversed$repetition_index) == 1, reversed$reversal_group == "audit-group")
stopifnot(identical(jsonlite::fromJSON(reversed$original_order), c("001", "002")))
stopifnot(identical(jsonlite::fromJSON(reversed$presented_order), c("002", "001")))
tied <- pairs[pairs$task_id == "tie", ]
stopifnot(tied$tie == "TRUE", is.na(tied$better_id), as.numeric(tied$result) == 0.5)
stopifnot(as.numeric(tied$p_left) == 0.2, as.numeric(tied$p_right) == 0.3, as.numeric(tied$p_tie) == 0.5)
invalid <- pairs[pairs$task_id == "invalid", ]
stopifnot(invalid$status == "invalid", is.na(invalid$result), is.na(invalid$better_id))
for (name in c("rubric", "pairwise")) {
  canonical <- lapply(readLines(file.path(root, paste0(name, ".jsonl")), encoding = "UTF-8"),
    jsonlite::fromJSON, simplifyVector = FALSE)
  audit <- if (name == "rubric") rubric else pairs
  stopifnot(length(canonical) == nrow(audit))
  stopifnot(identical(vapply(canonical, function(x) x$status, character(1)), audit$status))
  stopifnot(identical(vapply(canonical, function(x) x$attempt_id, character(1)), audit$attempt_id))
}
cat("Persistence interoperability passed: 6 rubric and 3 pairwise all-attempt records\n")
