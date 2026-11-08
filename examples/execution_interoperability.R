# Actual offline mock execution, independently reopened by a second Python process.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) stop("Supply the execution fixture output directory")
if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Required jsonlite is unavailable")
root <- args[[1L]]
read_records <- function(name) {
  lapply(readLines(file.path(root, name), encoding = "UTF-8"),
    jsonlite::fromJSON, simplifyVector = FALSE)
}
read_audit <- function(name) {
  read.csv(file.path(root, name), colClasses = "character", na.strings = "NA",
    check.names = FALSE, stringsAsFactors = FALSE, fileEncoding = "UTF-8", comment.char = "")
}
evidence <- read_records("execution.jsonl")
stopifnot(length(evidence) == 4L)
stopifnot(identical(vapply(evidence, function(x) x$run_id, character(1)), sprintf("%03d", 1:4)))
rubric <- read_audit("rubric_r.csv")
pairs <- read_audit("pairwise_r.csv")
stopifnot(nrow(rubric) == 4L, nrow(pairs) == 4L)
for (row in evidence) {
  stopifnot(row$schema_version == 1L, row$run$schema_version == 1L)
  stopifnot(row$run$run_id == row$run_id, row$run$state == "completed")
  stopifnot(row$run$provider == "mock", row$run$endpoint == "synthetic")
  stopifnot(row$run$model == "synthetic-v1", row$run$capture == row$capture)
  stopifnot(nchar(row$run$manifest_sha256) == 64L)
  stopifnot(length(row$requests) == 2L, length(row$attempts) == 2L)
  stopifnot(length(row$submissions) == 2L, length(row$observations) == 2L)
  stopifnot(length(row$acceptances) == 2L)
  stopifnot(identical(vapply(row$requests, function(x) x$scheduling_ordinal, integer(1)), 0:1))
  audit <- if (row$task_kind == "rubric") rubric else pairs
  audit <- audit[audit$run_id == row$run_id, ]
  stopifnot(nrow(audit) == 2L)
  for (attempt in row$attempts) {
    stopifnot(attempt$attempt_number == 1L, attempt$state == "normalized")
    stopifnot(nchar(attempt$payload_sha256) == 64L, nchar(attempt$prompt_sha256) == 64L)
    stopifnot(nchar(attempt$membership_sha256) == 64L)
    stopifnot(attempt$request_uid %in% audit$request_uid, attempt$attempt_id %in% audit$attempt_id)
    submissions <- Filter(function(x) x$attempt_id == attempt$attempt_id, row$submissions)
    stopifnot(length(submissions) == 1L)
    submission <- submissions[[1L]]
    stopifnot(submission$payload_sha256 == attempt$payload_sha256)
    stopifnot(submission$certainty == "accepted", submission$state == "resolved")
    stopifnot(is.character(submission$remote_id), nzchar(submission$correlation_id))
    observations <- Filter(function(x) x$submission_id == submission$submission_id, row$observations)
    stopifnot(length(observations) == 1L)
    observation <- observations[[1L]]
    stopifnot(observation$attempt_id == attempt$attempt_id, observation$kind == "response")
    stopifnot(nchar(observation$raw_sha256) == 64L, nzchar(observation$observation_key))
    acceptances <- Filter(function(x) x$submission_id == submission$submission_id, row$acceptances)
    stopifnot(length(acceptances) == 1L)
    accepted <- acceptances[[1L]]
    stopifnot(accepted$acceptance_count == 1L, accepted$payload_sha256 == submission$payload_sha256)
    stopifnot(accepted$correlation_id == submission$correlation_id)
    stopifnot(isTRUE(accepted$accepted), accepted$observation_key == observation$observation_key)
    stopifnot(accepted$retrieval_status == "available")
  }
  if (row$capture == "minimal") {
    stopifnot(all(is.na(audit$prompt_path)), all(is.na(audit$raw_response_path)))
  } else {
    stopifnot(all(file.exists(file.path(row$run_directory, audit$prompt_path))))
    stopifnot(all(file.exists(file.path(row$run_directory, audit$raw_response_path))))
  }
}
for (audit in list(rubric, pairs)) {
  stopifnot(!anyDuplicated(paste(audit$run_id, audit$task_id, audit$attempt_id, sep = "|")))
  stopifnot(all(audit$status == "complete"), all(audit$trait_id == "001"))
  stopifnot(all(audit$provider == "mock"), all(audit$endpoint == "synthetic"))
  stopifnot(all(audit$model == "synthetic-v1"), all(audit$mode == "live"))
  stopifnot(all(!is.na(audit$explanation)), all(nzchar(audit$explanation)))
  stopifnot(all(is.na(audit$input_tokens)), all(is.na(audit$output_tokens)))
  stopifnot(all(is.na(audit$error_kind)), all(nchar(audit$manifest_sha256) == 64L))
}
stopifnot(all(rubric$item_id %in% c("001", "002")))
for (index in seq_len(nrow(rubric))) {
  row <- rubric[index, ]
  probabilities <- jsonlite::fromJSON(row$probabilities_json)
  coding <- jsonlite::fromJSON(row$resolved_levels_json)
  stopifnot(identical(probabilities$level_label, c("low", "middle", "high")))
  stopifnot(identical(coding$numeric_value, c(1, 3, 8)))
  stopifnot(abs(sum(probabilities$probability) - 1) < 1e-8)
  expected <- sum(probabilities$probability * coding$numeric_value)
  stopifnot(row$primary_score_kind == "expected")
  stopifnot(abs(as.numeric(row$expected_value) - expected) < 1e-12)
  stopifnot(as.numeric(row$primary_score) == as.numeric(row$expected_value))
  stopifnot(row$category_label %in% coding$label)
  stopifnot(row$modal_category_label == probabilities$level_label[which.max(probabilities$probability)])
  stopifnot(row$category_label == "low", row$modal_category_label == "middle")
  stopifnot(as.numeric(row$numeric_category_value) == 1)
  expected_index <- sum(probabilities$probability * (seq_len(nrow(probabilities)) - 1L))
  stopifnot(abs(as.numeric(row$provider_expected_index) - expected_index) < 1e-12)
}
stopifnot(all(pairs$left_item_id == "001"), all(pairs$right_item_id == "002"))
for (index in seq_len(nrow(pairs))) {
  row <- pairs[index, ]
  original <- jsonlite::fromJSON(row$original_order)
  presented <- jsonlite::fromJSON(row$presented_order)
  stopifnot(identical(original, c("001", "002")))
  stopifnot(row$ID1 == presented[[1L]], row$ID2 == presented[[2L]])
  stopifnot(row$tie == "FALSE", row$better_id == row$ID1)
  stopifnot(as.numeric(row$result) == 1, as.numeric(row$winner_pos) == 1)
  stopifnot(abs(as.numeric(row$p_left) + as.numeric(row$p_right) - 1) < 1e-8)
  expected_first <- if (presented[[1L]] == "001") row$p_left else row$p_right
  stopifnot(as.numeric(row$p_first) == as.numeric(expected_first))
  if (row$pair_uid == "002") {
    stopifnot(identical(presented, c("002", "001")))
    stopifnot(row$source_pair_uid == "001", row$is_reversal == "TRUE", row$reversal_group == "001")
  } else {
    stopifnot(identical(presented, c("001", "002")), is.na(row$source_pair_uid))
  }
}
for (name in c("rubric", "pairwise")) {
  canonical <- read_records(paste0(name, ".jsonl"))
  audit <- if (name == "rubric") rubric else pairs
  stopifnot(length(canonical) == nrow(audit))
  stopifnot(all(vapply(canonical, function(x) x$schema_version, integer(1)) == 1L))
  stopifnot(identical(vapply(canonical, function(x) x$attempt_id, character(1)), audit$attempt_id))
}
cat("Execution interoperability passed: 8 judgments, 8 independently accepted observations\n")

# The same strict gate consumes real SDK/fake-HTTP adapter outputs and existing
# evidence readers. These are fixture files, not new public evidence exports.
openai_root <- file.path(root, "openai")
read_openai <- function(name) lapply(readLines(file.path(openai_root, name), encoding="UTF-8"), jsonlite::fromJSON, simplifyVector=FALSE)
openai_rows <- read_openai("results.jsonl")
openai_evidence <- read_openai("evidence.jsonl")
stopifnot(length(openai_rows) == 8L, length(openai_evidence) == 6L)
key <- function(x) paste(x$run_id, x$request_uid, x$attempt_id, sep="|")
result_keys <- vapply(openai_rows, key, character(1))
evidence_keys <- vapply(openai_evidence, function(x) key(x$identity), character(1))
stopifnot(!anyDuplicated(evidence_keys), all(result_keys %in% evidence_keys))
stopifnot(sum(vapply(openai_evidence, function(x) x$usage$total_tokens, integer(1))) == 660L)
stopifnot(sum(vapply(openai_evidence[match(result_keys, evidence_keys)], function(x) x$usage$total_tokens, integer(1))) == 880L)
stopifnot(sum(vapply(openai_rows, function(x) x$status == "invalid", logical(1))) == 2L)
for (entry in openai_evidence) {
  stopifnot(entry$usage$provider == "openai", entry$usage$reported_model == "offline-reported-model")
  stopifnot(entry$usage$usage_source %in% c("openai.responses.usage", "openai.chat.usage"))
  stopifnot(entry$usage$input_tokens == 100L, entry$usage$output_tokens == 10L)
  stopifnot(entry$usage$cache_read_tokens == 5L, entry$usage$reasoning_tokens == 2L)
  stopifnot(is.null(entry$usage$reported_cost), is.null(entry$usage$cache_write_tokens))
  stopifnot(entry$authorization$retention == "none")
}
openai_r <- read.csv(file.path(openai_root, "rubric.csv"), colClasses="character", na.strings="NA", check.names=FALSE)
openai_p <- read.csv(file.path(openai_root, "pairs.csv"), colClasses="character", na.strings="NA", check.names=FALSE)
stopifnot(nrow(openai_r) == 6L, all(openai_r$item_id == "001"))
stopifnot(all(openai_r$trait_id %in% c("01", "02")))
stopifnot(all(is.na(openai_r$probabilities_json)), all(is.na(openai_r$expected_value)))
for (coding in openai_r$resolved_levels_json) stopifnot(identical(jsonlite::fromJSON(coding)$numeric_value, c(1, 3, 8)))
stopifnot(nrow(openai_p) == 2L, all(openai_p$ID1 == "002"), all(openai_p$ID2 == "001"))
stopifnot(all(openai_p$better_id == "002"), all(as.numeric(openai_p$result) == 1))
stopifnot(all(is.na(openai_p$p_left)), all(is.na(openai_p$p_first)))
for (order in openai_p$original_order) stopifnot(identical(jsonlite::fromJSON(order), c("001", "002")))
for (order in openai_p$presented_order) stopifnot(identical(jsonlite::fromJSON(order), c("002", "001")))
cat("OpenAI adapter interoperability passed: 8 audit rows, 6 physical usage records\n")


# Additive diagnostics join physical observations without adding result columns.
for (entry in openai_evidence) {
  diagnostic <- entry$assessment_diagnostic
  stopifnot(diagnostic$schema_version == 1L, diagnostic$diagnostic_version == 1L)
  stopifnot(identical(diagnostic$identity, entry$identity))
  stopifnot(diagnostic$endpoint == entry$usage$endpoint)
  stopifnot(diagnostic$requested_model == "offline-unqualified-model")
  stopifnot(diagnostic$reported_model == "offline-reported-model")
  stopifnot(diagnostic$remote_response_id == entry$remote_id)
  stopifnot(diagnostic$component$extraction_status == "extracted")
  stopifnot(diagnostic$parser_diagnostic %in% c("valid", "category_error"))
  stopifnot(isTRUE(diagnostic$canonical_valid) == (diagnostic$parser_diagnostic == "valid"))
  stopifnot(nchar(diagnostic$content_sha256) == 64L)
  stopifnot(diagnostic$artifact$sha256 == diagnostic$content_sha256)
  stopifnot(diagnostic$byte_length == diagnostic$artifact$byte_length)
  if (entry$capture == "full") {
    stopifnot(diagnostic$retention == "retained", is.character(diagnostic$artifact$relative_path))
  } else {
    stopifnot(diagnostic$retention == "hash_only", is.null(diagnostic$artifact$relative_path))
  }
}
stopifnot(sum(vapply(openai_evidence, function(x) x$assessment_diagnostic$parser_diagnostic == "category_error", logical(1))) == 2L)
for (row in openai_rows) stopifnot(!"assessment_diagnostic" %in% names(row))
for (audit in list(openai_r, openai_p)) stopifnot(!"assessment_diagnostic" %in% names(audit))
inspections <- read_openai("assessment-inspections.jsonl")
stopifnot(length(inspections) == 4L)
stopifnot(!anyDuplicated(vapply(inspections, function(x) x$receipt_identity$run_id, character(1))))
for (entry in inspections) {
  inspection <- entry$inspection
  diagnostic <- inspection$diagnostic
  result <- entry$normalized_result
  stopifnot(!"output_text" %in% names(inspection))
  stopifnot(!isTRUE(entry$metadata_only_has_text))
  stopifnot(grepl("Unvalidated model output", inspection$label, fixed=TRUE))
  stopifnot(identical(diagnostic$identity, entry$receipt_identity))
  stopifnot(diagnostic$identity$run_id == result$run_id)
  stopifnot(diagnostic$identity$request_uid == result$request_uid)
  stopifnot(diagnostic$identity$attempt_id == result$attempt_id)
  stopifnot(diagnostic$component$extraction_status == "extracted")
  stopifnot(diagnostic$provider_outcome == "completed")
  stopifnot(diagnostic$parser_diagnostic == "json_syntax", !isTRUE(diagnostic$canonical_valid))
  stopifnot(diagnostic$content_sha256 == entry$expected_content_sha256)
  stopifnot(diagnostic$byte_length == entry$expected_byte_length)
  stopifnot(result$schema_version == 1L, result$status == "invalid", result$error_kind == "invalid_output")
  stopifnot(result$item_id == "001", result$trait_id == "01")
  stopifnot(is.null(result$category_label), is.null(result$primary_score), is.null(result$probabilities))
  stopifnot(isTRUE(entry$explicit_inspection_has_text) == (diagnostic$capture == "full"))
  stopifnot(diagnostic$retention == if (diagnostic$capture == "full") "retained" else "hash_only")
}
absent <- jsonlite::fromJSON(file.path(openai_root, "diagnostic-absent.json"), simplifyVector=FALSE)
stopifnot(is.null(absent$diagnostic), !"output_text" %in% names(absent))
cat("Assessment diagnostic interoperability passed: exact protected invalid text, hash-only minimal capture, unchanged exports\n")
