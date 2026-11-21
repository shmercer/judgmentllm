# Independent R/jsonlite consumer of a Python-generated native-batch audit cut.
check_batch_audit <- function(manifest_path) {
if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Required jsonlite is unavailable")
manifest_path <- normalizePath(manifest_path, winslash = "/", mustWork = TRUE)
bundle <- jsonlite::fromJSON(manifest_path, simplifyVector = FALSE)
stopifnot(bundle$kind == "native_batch_audit", bundle$normalized_result_schema == 1L,
  bundle$run_id == "001", bundle$cut$operation_count >= 2L)
artifact <- function(name) {
  found <- Filter(function(x) x$name == name, bundle$files)
  stopifnot(length(found) == 1L)
  file.path(dirname(dirname(dirname(manifest_path))), found[[1L]]$artifact$relative_path)
}
jsonlines <- function(path) lapply(readLines(path, warn = FALSE, encoding = "UTF-8"),
  jsonlite::fromJSON, simplifyVector = FALSE)
results <- jsonlines(artifact("results.jsonl"))
members <- jsonlines(artifact("members.jsonl"))
lossless <- read.csv(artifact("results.csv"), colClasses = "character", na.strings = "\\N", check.names = FALSE)
audit <- read.csv(artifact("results_r.csv"), colClasses = "character", na.strings = "NA", check.names = FALSE)
stopifnot(nrow(lossless) == length(results), nrow(audit) == length(results),
  all(lossless$run_id == "001"), all(audit$run_id == "001"),
  length(unique(vapply(members, function(x) x$preparation$attempt_id, character(1L)))) == length(members))
for (row in results) {
  stopifnot(row$schema_version == 1L, row$run_id == "001", row$mode == "batch_native", row$trait_id == "01")
  found <- Filter(function(x) x$preparation$attempt_id == row$attempt_id, members)
  stopifnot(length(found) == 1L, found[[1L]]$evidence$identity$attempt_id == row$attempt_id,
    found[[1L]]$evidence$identity$request_uid == row$request_uid)
  if (row$task_kind == "rubric" && row$status == "complete") {
    stopifnot(row$item_id == "001", row$category_label == "001", row$modal_category_label == "middle",
      row$primary_score_kind == "expected", abs(row$expected_value - 2.8) < 1e-10,
      row$provider_expected_index == 1, length(row$probabilities) == 3L,
      identical(vapply(row$resolved_levels, function(x) x$numeric_value, numeric(1L)), c(1, 2, 7)))
  } else if (row$task_kind == "rubric") {
    stopifnot(row$status == "cancelled", row$item_id == "002", is.null(row$primary_score), is.null(row$probabilities))
  } else {
    stopifnot(row$winner_id == row$presented_order[[1L]],
      identical(row$original_order, list("001", "002")), abs(row$p_left + row$p_right - 1) < 1e-10)
    index <- which(audit$attempt_id == row$attempt_id)
    stopifnot(length(index) == 1L, audit$ID1[[index]] == row$presented_order[[1L]], audit$better_id[[index]] == row$winner_id)
  }
}
reported <- Filter(function(x) !is.null(x$evidence) && !is.null(x$evidence$usage$input_tokens), members)
if (length(results) && results[[1L]]$task_kind == "rubric") {
  stopifnot(length(reported) == 1L, reported[[1L]]$evidence$usage$input_tokens == 7L,
    reported[[1L]]$evidence$usage$output_tokens == 3L, is.null(reported[[1L]]$evidence$usage$reported_cost))
}
# Compare the actual lossless and R audit files against every canonical row.
check_cell <- function(table, index, field, value) {
  stopifnot(field %in% names(table))
  cell <- table[[field]][[index]]
  if (is.null(value)) stopifnot(is.na(cell))
  else if (is.list(value)) stopifnot(identical(jsonlite::fromJSON(cell, simplifyVector = FALSE), value))
  else if (is.numeric(value)) stopifnot(isTRUE(all.equal(as.numeric(cell), value, tolerance = 1e-12)))
  else stopifnot(identical(cell, value))
}
for (index in seq_along(results)) {
  row <- results[[index]]
  for (field in c("run_id", "task_id", "request_uid", "attempt_id", "trait_id", "status",
    "provider", "endpoint", "model", "mode", "manifest_sha256", "prompt_path", "raw_response_path")) {
    check_cell(lossless, index, field, row[[field]])
    check_cell(audit, index, field, row[[field]])
  }
  if (row$task_kind == "rubric") {
    for (field in c("item_id", "category_label", "modal_category_label", "primary_score_kind",
      "primary_score", "numeric_category_value", "expected_value", "provider_expected_index")) {
      check_cell(lossless, index, field, row[[field]])
      check_cell(audit, index, field, row[[field]])
    }
    check_cell(lossless, index, "probabilities", row$probabilities)
    check_cell(lossless, index, "resolved_levels", row$resolved_levels)
    check_cell(audit, index, "probabilities_json", row$probabilities)
    check_cell(audit, index, "resolved_levels_json", row$resolved_levels)
    check_cell(audit, index, "response_id", row$item_id)
  } else {
    for (field in c("pair_id", "pair_uid", "left_item_id", "right_item_id", "winner_id", "p_left", "p_right", "p_tie")) {
      check_cell(lossless, index, field, row[[field]])
      check_cell(audit, index, field, row[[field]])
    }
    for (field in c("original_order", "presented_order")) {
      check_cell(lossless, index, field, row[[field]])
      check_cell(audit, index, field, row[[field]])
    }
    check_cell(audit, index, "ID1", row$presented_order[[1L]])
    check_cell(audit, index, "ID2", row$presented_order[[2L]])
    check_cell(audit, index, "better_id", row$winner_id)
    check_cell(audit, index, "p_first", if (row$presented_order[[1L]] == row$left_item_id) row$p_left else row$p_right)
    check_cell(audit, index, "p_second", if (row$presented_order[[1L]] == row$left_item_id) row$p_right else row$p_left)
  }
}
for (member in members) if (!is.null(member$evidence)) {
  preparation <- member$preparation
  evidence <- member$evidence
  stopifnot(identical(evidence$member_key, preparation$member_key),
    identical(evidence$identity$run_id, preparation$run_id),
    identical(evidence$identity$request_uid, preparation$request_uid),
    identical(evidence$identity$attempt_id, preparation$attempt_id),
    is.character(evidence$identity$observation_id), nzchar(evidence$identity$observation_id),
    is.character(evidence$identity$submission_id), nzchar(evidence$identity$submission_id),
    is.character(evidence$observation_key), nzchar(evidence$observation_key))
  if (evidence$kind != "response") {
    stopifnot(is.null(evidence$usage$input_tokens), is.null(evidence$usage$output_tokens),
      is.null(evidence$usage$total_tokens), is.null(evidence$usage$reported_cost))
  }
}
invisible(list(results = results, members = members))
}
if (sys.nframe() == 0L) {
  args <- commandArgs(trailingOnly = TRUE)
  if (length(args) != 1L) stop("Supply one native batch audit manifest")
  check_batch_audit(args[[1L]])
  cat("Native batch R audit assertions passed\n")
}
