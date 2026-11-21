# Independent R/jsonlite consumer of a Python-generated native-batch audit cut.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) stop("Supply one native batch audit manifest")
if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Required jsonlite is unavailable")
manifest_path <- normalizePath(args[[1L]], winslash = "/", mustWork = TRUE)
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
cat("Native batch R audit assertions passed\n")
