# Independent R consumer: generative OpenAI Batch preserves normalized schema 1.
check_openai_batch_audit <- function(manifest_path, endpoint, capture, require_diagnostics = FALSE) {
  if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Required jsonlite is unavailable")
  bundle <- jsonlite::fromJSON(manifest_path, simplifyVector = FALSE)
  stopifnot(bundle$schema_version == 2L, bundle$normalized_result_schema == 1L,
    bundle$run_id == "001", bundle$kind == "native_batch_audit")
  artifact <- function(name) {
    entry <- Filter(function(x) x$name == name, bundle$files)
    stopifnot(length(entry) == 1L)
    file.path(dirname(dirname(dirname(manifest_path))), entry[[1L]]$artifact$relative_path)
  }
  records <- function(path) lapply(readLines(path, warn = FALSE, encoding = "UTF-8"), jsonlite::fromJSON, simplifyVector = FALSE)
  results <- records(artifact("results.jsonl"))
  members <- records(artifact("members.jsonl"))
  jobs <- records(artifact("jobs.jsonl"))
  controls <- records(artifact("controls.jsonl"))
  diagnostic_indices <- which(vapply(controls, function(row)
    identical(row$record$record_type, "openai_batch_diagnostic_v1"), logical(1)))
  if (require_diagnostics) stopifnot(length(diagnostic_indices) > 0L)
  if (length(diagnostic_indices) > 0L) {
    legal_status <- function(x) is.null(x) || (is.integer(x) && length(x) == 1L && x >= 100L && x <= 599L)
    control_cells <- read.csv(artifact("controls.csv"), colClasses = "character", check.names = FALSE)
    stopifnot(nrow(control_cells) == length(controls))
    for (i in diagnostic_indices) {
      row <- controls[[i]]
      record <- row$record
      stopifnot(identical(row$kind, "cursor"), startsWith(row$entity_id, "openai-diagnostic:"),
        identical(record, jsonlite::fromJSON(control_cells$record[[i]], simplifyVector = FALSE)),
        all(c("http_status", "http_request_id", "member_status", "member_request_id", "provider_code") %in% names(record)),
        legal_status(record$http_status), legal_status(record$member_status))
      if (identical(record$reason, "headers")) {
        stopifnot(identical(record$outcome, "observed"), is.null(record$provider_code))
      }
      if (identical(record$operation, "result_row")) {
        stopifnot(is.null(record$http_status), is.null(record$http_request_id))
      } else {
        stopifnot(is.null(record$member_status), is.null(record$member_request_id))
      }
    }
  }
  lossless <- read.csv(artifact("results.csv"), colClasses = "character", na.strings = "\\N", check.names = FALSE)
  audit <- read.csv(artifact("results_r.csv"), colClasses = "character", na.strings = "NA", check.names = FALSE)
  stopifnot(length(results) > 0L, nrow(lossless) == length(results), nrow(audit) == length(results), length(jobs) == 1L,
    jobs[[1L]]$preparation$context$endpoint == endpoint,
    jobs[[1L]]$preparation$context$sdk_version == "3.42.1")
  cell <- function(table, index, field, expected) {
    stopifnot(field %in% names(table))
    value <- table[[field]][[index]]
    if (is.null(expected)) stopifnot(is.na(value))
    else if (is.list(expected)) stopifnot(identical(jsonlite::fromJSON(value, simplifyVector = FALSE), expected))
    else if (is.numeric(expected)) stopifnot(isTRUE(all.equal(as.numeric(value), expected, tolerance = 1e-12)))
    else stopifnot(identical(value, expected))
  }
  for (index in seq_along(results)) {
    row <- results[[index]]
    stopifnot(row$schema_version == 1L, row$provider == "openai", row$endpoint == endpoint,
      row$mode == "batch_native", row$status == "complete")
    member <- Filter(function(x) x$preparation$attempt_id == row$attempt_id, members)
    stopifnot(length(member) == 1L, member[[1L]]$evidence$identity$request_uid == row$request_uid,
      member[[1L]]$evidence$capture == capture, is.null(row$probabilities))
    for (field in c("run_id", "task_id", "request_uid", "attempt_id", "trait_id", "status", "provider", "endpoint", "model", "mode", "manifest_sha256", "prompt_path", "raw_response_path")) {
      cell(lossless, index, field, row[[field]])
      cell(audit, index, field, row[[field]])
    }
    if (row$task_kind == "rubric") {
      stopifnot(row$item_id %in% c("001", "002"), row$trait_id %in% c("01", "02"), row$category_label == "001",
        row$primary_score_kind == "category_code", row$numeric_category_value == 1,
        identical(vapply(row$resolved_levels, function(x) x$numeric_value, numeric(1L)), c(1, 2, 7)),
        is.null(row$expected_value), is.null(row$modal_category_label))
      for (field in c("item_id", "category_label", "primary_score_kind", "primary_score", "numeric_category_value", "expected_value", "modal_category_label")) {
        cell(lossless, index, field, row[[field]])
        cell(audit, index, field, row[[field]])
      }
      cell(lossless, index, "resolved_levels", row$resolved_levels)
      cell(audit, index, "resolved_levels_json", row$resolved_levels)
      cell(audit, index, "probabilities_json", NULL)
    } else {
      stopifnot(identical(row$original_order, list("001", "002")), row$winner_id == row$presented_order[[1L]],
        is.null(row$p_left), is.null(row$p_right), is.null(row$p_tie))
      for (field in c("pair_id", "pair_uid", "left_item_id", "right_item_id", "winner_id", "original_order", "presented_order", "p_left", "p_right", "p_tie")) {
        cell(lossless, index, field, row[[field]])
        cell(audit, index, field, row[[field]])
      }
      cell(audit, index, "ID1", row$presented_order[[1L]])
      cell(audit, index, "better_id", row$winner_id)
    }
  }
  physical <- Filter(function(x) !is.null(x$evidence), members)
  stopifnot(length(unique(vapply(physical, function(x) x$evidence$identity$observation_id, character(1L)))) == length(physical))
  for (member in physical) {
    usage <- member$evidence$usage
    stopifnot(usage$input_tokens == 20L, usage$output_tokens == 10L, usage$total_tokens == 30L,
      usage$cache_read_tokens == 3L, usage$reasoning_tokens == 2L, usage$sdk_version == "3.42.1",
      usage$invocation_kind == "native_batch", is.null(usage$reported_cost), is.null(usage$estimated_cost),
      identical(member$preparation$member_key, member$evidence$member_key))
  }
  cells <- read.csv(artifact("members.csv"), colClasses = "character", check.names = FALSE)
  stopifnot(identical(lapply(cells$preparation, jsonlite::fromJSON, simplifyVector = FALSE), lapply(members, function(x) x$preparation)))
  invisible(list(results = results, members = members, controls = controls))
}

check_openai_batch_fixture_diagnostics <- function(controls) {
  diagnostics <- lapply(Filter(function(row)
    identical(row$record$record_type, "openai_batch_diagnostic_v1"), controls), function(row) row$record)
  headers <- Filter(function(record) identical(record$reason, "headers"), diagnostics)
  rows <- Filter(function(record) identical(record$operation, "result_row"), diagnostics)
  stopifnot(length(headers) > 0L, length(rows) > 0L,
    all(vapply(headers, function(record) identical(record$http_status, 200L), logical(1))),
    all(vapply(rows, function(record) identical(record$member_status, 200L), logical(1))))
}

if (sys.nframe() == 0L) {
  args <- commandArgs(trailingOnly = TRUE)
  stopifnot(length(args) == 3L || (length(args) == 4L && args[[4L]] == "--diagnostics"))
  audit <- check_openai_batch_audit(args[[1L]], args[[2L]], args[[3L]], length(args) == 4L)
  if (length(args) == 4L) check_openai_batch_fixture_diagnostics(audit$controls)
  cat("OpenAI Batch R assertions passed\n")
}
