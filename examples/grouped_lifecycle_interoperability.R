# Grouped results and subset history are produced and reopened in separate processes.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) stop("Supply the grouped lifecycle fixture directory")
if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Required jsonlite is unavailable")
root <- args[[1L]]
records <- function(name) lapply(readLines(file.path(root, name), encoding = "UTF-8"),
  jsonlite::fromJSON, simplifyVector = FALSE)
audit <- function(name, missing = "NA") read.csv(file.path(root, name),
  colClasses = "character", na.strings = missing, check.names = FALSE,
  stringsAsFactors = FALSE, fileEncoding = "UTF-8", comment.char = "")
evidence <- records("grouped-lifecycle.jsonl")
stopifnot(length(evidence) == 2L)
stopifnot(identical(vapply(evidence, function(x) x$run_id, character(1)), c("001", "002")))
for (run in evidence) {
  stopifnot(length(run$requests) == 1L, length(run$attempts) == 2L)
  stopifnot(length(run$submissions) == 2L, length(run$observations) == 2L)
  stopifnot(length(run$acceptances) == 2L, length(run$result_links) == 4L)
  stopifnot(length(run$retries) == 1L, run$retries[[1L]]$operation_id == "retry-001")
  parent <- run$requests[[1L]]
  initial <- run$attempts[[1L]]
  retry <- run$attempts[[2L]]
  stopifnot(length(parent$task_ids) == 3L, parent$scheduling_ordinal == 0L)
  stopifnot(identical(parent$task_ids, initial$task_ids), length(retry$task_ids) == 1L)
  stopifnot(initial$attempt_number == 1L, retry$attempt_number == 2L)
  stopifnot(initial$request_uid == retry$request_uid, parent$request_uid == retry$request_uid)
  stopifnot(initial$attempt_id != retry$attempt_id, is.null(initial$authorization_id))
  stopifnot(retry$authorization_id == "retry-001")
  stopifnot(initial$prompt_sha256 != retry$prompt_sha256)
  stopifnot(initial$payload_sha256 != retry$payload_sha256)
  canonical <- records(paste0(run$run_id, ".jsonl"))
  csv <- audit(paste0(run$run_id, ".csv"), "\\N")
  r <- audit(paste0(run$run_id, "_r.csv"))
  stopifnot(length(canonical) == 4L, nrow(csv) == 4L, nrow(r) == 4L)
  stopifnot(!anyDuplicated(paste(r$task_id, r$attempt_id, sep = "|")))
  stopifnot(all(r$item_id == "001"), all(r$response_id == "001"))
  stopifnot(identical(r$attempt_id, csv$attempt_id), identical(r$status, csv$status))
  stopifnot(identical(vapply(canonical, function(x) x$task_id, character(1)), r$task_id))
  stopifnot(all(r$request_uid == parent$request_uid), all(r$run_id == run$run_id))
  stopifnot(identical(r$trait_id, c("001", "002", "003", "003")))
  stopifnot(identical(r$status, c("complete", "complete", "invalid", "complete")))
  bad <- r[r$status == "invalid", , drop = FALSE]
  stopifnot(nrow(bad) == 1L, bad$error_kind == "invalid_output")
  stopifnot(bad$attempt_id == initial$attempt_id, bad$task_id == retry$task_ids[[1L]])
  stopifnot(is.na(bad$category_label), is.na(bad$primary_score), is.na(bad$primary_score_kind))
  stopifnot(is.na(bad$expected_value), is.na(bad$explanation), is.na(bad$probabilities_json))
  stopifnot(sum(r$task_id == bad$task_id) == 2L)
  for (attempt in run$attempts) {
    stopifnot(attempt$state == "normalized")
    submitted <- Filter(function(x) x$attempt_id == attempt$attempt_id, run$submissions)
    stopifnot(length(submitted) == 1L)
    submitted <- submitted[[1L]]
    stopifnot(submitted$state == "resolved", submitted$certainty == "accepted")
    stopifnot(submitted$payload_sha256 == attempt$payload_sha256)
    observed <- Filter(function(x) x$submission_id == submitted$submission_id, run$observations)
    accepted <- Filter(function(x) x$submission_id == submitted$submission_id, run$acceptances)
    stopifnot(length(observed) == 1L, length(accepted) == 1L)
    observed <- observed[[1L]]
    accepted <- accepted[[1L]]
    stopifnot(observed$attempt_id == attempt$attempt_id, observed$kind == "response")
    stopifnot(accepted$acceptance_count == 1L, isTRUE(accepted$accepted))
    stopifnot(accepted$observation_key == observed$observation_key)
    stopifnot(accepted$payload_sha256 == submitted$payload_sha256)
    stopifnot(accepted$correlation_id == submitted$correlation_id)
    links <- Filter(function(x) x$attempt_id == attempt$attempt_id, run$result_links)
    stopifnot(length(links) == length(attempt$task_ids))
    stopifnot(all(vapply(links, function(x) x$observation_id, character(1)) == observed$observation_id))
    stopifnot(identical(vapply(links, function(x) x$task_id, character(1)), unlist(attempt$task_ids)))
  }
  for (index in seq_len(nrow(r))) {
    row <- r[index, ]
    coding <- jsonlite::fromJSON(row$resolved_levels_json)
    stopifnot(identical(coding$label, c("low", "middle", "high")))
    stopifnot(identical(coding$numeric_value, c(1, 3, 8)))
    stopifnot(nchar(row$manifest_sha256) == 64L)
    stopifnot(is.na(row$input_tokens), is.na(row$output_tokens))
    if (row$status == "complete") {
      p <- jsonlite::fromJSON(row$probabilities_json)
      stopifnot(identical(p$level_label, coding$label))
      stopifnot(abs(sum(p$probability) - 1) < 1e-8)
      expected <- sum(p$probability * coding$numeric_value)
      stopifnot(abs(as.numeric(row$primary_score) - expected) < 1e-12)
      stopifnot(abs(as.numeric(row$expected_value) - expected) < 1e-12)
      stopifnot(row$primary_score_kind == "expected", row$category_label == "low")
      stopifnot(as.numeric(row$numeric_category_value) == 1)
      stopifnot(row$modal_category_label == p$level_label[which.max(p$probability)])
      stopifnot(abs(as.numeric(row$provider_expected_index) - sum(p$probability * 0:2)) < 1e-12)
      stopifnot(row$explanation == "Authorized synthetic explanation.")
    }
  }
  if (run$capture == "minimal") {
    stopifnot(all(is.na(r$prompt_path)), all(is.na(r$raw_response_path)))
  } else {
    stopifnot(all(!is.na(r$prompt_path)), all(!is.na(r$raw_response_path)))
  }
  stopifnot(length(run$exports) == 3L, run$prior_export$row_count == 3L)
  stopifnot(run$prior_export$result_high_water_mark < run$exports[[1L]]$result_high_water_mark)
  for (published in run$exports) {
    stopifnot(published$row_count == 4L, nchar(published$artifact$sha256) == 64L)
    stopifnot(isTRUE(published$run_complete), published$run_state == "completed")
    stopifnot(published$artifact$byte_length == file.info(published$artifact_path)$size)
    stopifnot(file.exists(published$artifact_path))
  }
}
cat("Grouped lifecycle interoperability passed: 8 results, 4 accepted physical submissions\n")
