# Required offline acceptance: every lifecycle operation launches the installed CLI.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 4L) stop("Supply output directory, installed CLI, Python, fixture helper")
if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Required jsonlite is unavailable")
root <- normalizePath(args[[1L]], winslash = "/", mustWork = FALSE)
console <- normalizePath(args[[2L]], winslash = "/", mustWork = TRUE)
# Preserve the virtual-environment executable path: resolving its symlink selects system Python.
python <- args[[3L]]
stopifnot(file.exists(python))
helper <- normalizePath(args[[4L]], winslash = "/", mustWork = TRUE)
dir.create(root, recursive = TRUE, showWarnings = FALSE)
setwd(root)
sequence <- 0L
invoke <- function(command, arguments, expected = 0L, json = TRUE, content = FALSE) {
  sequence <<- sequence + 1L
  out <- file.path(root, paste0("stdout-", sequence, ".json"))
  err <- file.path(root, paste0("stderr-", sequence, ".jsonl"))
  status <- suppressWarnings(system2(command, vapply(arguments, shQuote, character(1L)),
    stdout = out, stderr = err, wait = TRUE))
  lines <- readLines(out, encoding = "UTF-8", warn = FALSE)
  diagnostics <- readLines(err, encoding = "UTF-8", warn = FALSE)
  assert_exit <- function() {
    if (!status %in% expected) stop("Unexpected process exit ", status, ": ", paste(diagnostics, collapse = "\n"))
  }
  if (!json) { assert_exit(); return(invisible(status)) }
  stopifnot(length(lines) == 1L)
  value <- jsonlite::fromJSON(lines[[1L]], simplifyVector = FALSE)
  if (identical(command, console)) {
    stopifnot(identical(names(value), c("schema_version", "command", "status", "run_id", "run_status", "artifacts", "data", "error")))
    stopifnot(value$schema_version == 1L, value$status %in% c("ok", "partial", "error"))
    for (line in diagnostics) {
      diagnostic <- jsonlite::fromJSON(line, simplifyVector = FALSE)
      stopifnot(diagnostic$schema_version == 1L, diagnostic$event %in% c("progress", "diagnostic"))
      stopifnot(is.character(diagnostic$category), is.character(diagnostic$message), "run_id" %in% names(diagnostic))
    }
    if (!content) stopifnot(!any(grepl("SOURCE_ONLY_SENTINEL|RAW_ONLY_SENTINEL|Authorized normalized explanation", c(lines, diagnostics))))
    for (path in value$artifacts) if (!is.null(path)) {
      stopifnot(grepl("^/|^[A-Za-z]:[/\\\\]", path), file.exists(path))
    }
  }
  assert_exit()
  value
}
fixture <- function(phase, directory) invoke(python, c(helper, "--phase", phase, "--output", directory), json = phase != "create")
setup_started <- proc.time()[["elapsed"]]
fixture("create", root)
setup_seconds <- proc.time()[["elapsed"]] - setup_started
lifecycle_started <- proc.time()[["elapsed"]]
cli <- function(arguments, expected = 0L, content = FALSE) invoke(console, c(arguments, "--json"), expected, content = content)
records <- function(path) lapply(readLines(path, encoding = "UTF-8"), jsonlite::fromJSON, simplifyVector = FALSE)
audit <- function(path, missing) read.csv(path, colClasses = "character", na.strings = missing,
  check.names = FALSE, stringsAsFactors = FALSE, fileEncoding = "UTF-8", comment.char = "")
# Compare scalar projections without coercing character identifiers.
scalar <- function(expected, observed) {
  if (is.null(expected)) stopifnot(is.na(observed))
  else if (is.logical(expected)) stopifnot(identical(as.logical(observed), expected))
  else if (is.numeric(expected)) stopifnot(abs(as.numeric(observed) - expected) < 1e-12)
  else stopifnot(identical(observed, expected))
}
one <- function(values, predicate) {
  found <- Filter(predicate, values)
  stopifnot(length(found) == 1L)
  found[[1L]]
}
ordered_object <- function(value) {
  if (!is.list(value)) return(value)
  if (!is.null(names(value))) value <- value[order(names(value))]
  lapply(value, ordered_object)
}
assert_evidence <- function(evidence, rows) {
  stopifnot(length(evidence$results) == length(rows))
  for (record in rows) {
    link <- one(evidence$results, function(x) x$task_id == record$task_id && x$attempt_id == record$attempt_id)
    stopifnot(identical(ordered_object(jsonlite::fromJSON(link$record_json, simplifyVector = FALSE)), ordered_object(record)))
    stopifnot(link$run_id == record$run_id, link$request_uid == record$request_uid, link$status == record$status)
    stopifnot(nchar(link$record_sha256) == 64L)
    attempt <- one(evidence$attempts, function(x) x$attempt_id == record$attempt_id)
    stopifnot(attempt$run_id == record$run_id, attempt$request_uid == record$request_uid, attempt$membership_sealed == 1L)
    stopifnot(attempt$state == "normalized", nchar(attempt$prompt_sha256) == 64L, nchar(attempt$payload_sha256) == 64L)
    membership <- one(evidence$attempt_tasks, function(x) x$attempt_id == record$attempt_id && x$task_id == record$task_id)
    stopifnot(membership$run_id == record$run_id, membership$request_uid == record$request_uid)
    task <- one(evidence$tasks, function(x) x$task_id == record$task_id)
    stopifnot(task$trait_id == record$trait_id)
    submitted <- one(evidence$submissions, function(x) x$attempt_id == record$attempt_id)
    stopifnot(submitted$run_id == record$run_id, submitted$payload_sha256 == attempt$payload_sha256)
    if (!is.null(link$observation_id)) {
      observed <- one(evidence$observations, function(x) x$observation_id == link$observation_id)
      stopifnot(is.null(link$failure_submission_id), observed$attempt_id == record$attempt_id)
      stopifnot(observed$submission_id == submitted$submission_id, observed$run_id == record$run_id)
      accepted <- one(evidence$acceptances, function(x) x$submission_id == submitted$submission_id)
      stopifnot(accepted$accepted == 1L, accepted$acceptance_count == 1L)
      stopifnot(accepted$payload_sha256 == submitted$payload_sha256, accepted$correlation_id == submitted$correlation_id)
      stopifnot(accepted$observation_key == observed$observation_key, submitted$certainty == "accepted")
      recipe <- jsonlite::fromJSON(accepted$recipe)
      stopifnot(recipe$response_sha256 == observed$raw_sha256)
      if (!is.null(record$raw_response_path)) stopifnot(basename(record$raw_response_path) == observed$raw_sha256)
    } else {
      failure <- one(evidence$submission_failures, function(x) x$attempt_id == record$attempt_id)
      stopifnot(failure$submission_id == link$failure_submission_id, failure$submission_id == submitted$submission_id)
      stopifnot(failure$kind %in% c("transport_not_submitted", "confirmed_cancellation"))
      stopifnot(record$status %in% c("errored", "cancelled"))
    }
    if (!is.null(record$prompt_path)) stopifnot(basename(record$prompt_path) == attempt$prompt_sha256)
    stopifnot(record$manifest_sha256 == evidence$runs[[1L]]$manifest_sha256)
    if (!is.null(attempt$authorization_id)) {
      authorization <- one(evidence$retry_operations, function(x) x$operation_id == attempt$authorization_id)
      stopifnot(authorization$run_id == record$run_id, authorization$allow_completed == 0L)
    }
  }
}
settings <- function(directory, fixture_name = "fixture.json") c("--mock-ledger", file.path(directory, "ledger.sqlite3"), "--mock-fixture", file.path(directory, fixture_name))
run_arguments <- function(directory) c("--run", directory$run_id, "--store", directory$store)
export <- function(run, format = "jsonl", view = "canonical") {
  result <- cli(c("results", "export", run_arguments(run), "--format", format, "--view", view))
  stopifnot(result$command == "results export", is.null(result$error))
  stopifnot(file.exists(result$artifacts$export))
  result
}
for (kind in c("independent", "grouped", "pairwise")) for (capture in c("full", "minimal")) {
  directory <- file.path(root, paste0(kind, "-", capture))
  config <- file.path(directory, "config.json")
  store <- file.path(directory, "store")
  validated <- cli(c("validate", "--config", config))
  stopifnot(validated$command == "validate", is.null(validated$run_id), validated$data$valid)
  planned <- cli(c("plan", "--config", config))
  stopifnot(planned$command == "plan", is.null(planned$error), is.null(planned$data$estimated_input_tokens), is.null(planned$data$estimated_cost))
  started <- cli(c("run", "--config", config, "--store", store, settings(directory)), if (kind == "grouped") 3L else 0L)
  stopifnot(is.character(started$run_id), nzchar(started$run_id))
  run <- list(run_id = started$run_id, store = store)
  queried <- cli(c("runs", "status", run_arguments(run)))
  stopifnot(queried$run_id == run$run_id, queried$status == "ok")
  before <- export(run)
  before_bytes <- readBin(before$artifacts$export, "raw", n = file.info(before$artifacts$export)$size)
  original <- records(before$artifacts$export)
  if (kind == "grouped") {
    stopifnot(started$status == "partial", started$error$category == "partial_completion")
    failed <- Filter(function(x) x$status != "complete", original)
    stopifnot(length(original) == 6L, length(failed) == 2L)
    for (index in seq_along(failed)) {
      arguments <- c("resume", run_arguments(run), settings(directory, "valid.json"), "--retry-id", paste0("retry-00", index), "--retry-task", failed[[index]]$task_id)
      retried <- cli(arguments, if (index == length(failed)) 0L else 3L)
    }
    stopifnot(retried$status == "ok", retried$run_status == "completed")
    again <- cli(arguments)
    stopifnot(again$run_status == "completed", again$data$results == 8L)
    conflicting <- cli(c("resume", run_arguments(run), settings(directory, "valid.json"), "--retry-id", "retry-001", "--retry-task", original[[1L]]$task_id, "--allow-completed"), 2L)
    stopifnot(conflicting$status == "error")
  }
  # Ordinary resume never authorizes another completed judgment.
  resumed <- cli(c("resume", run_arguments(run), settings(directory, if (kind == "grouped") "valid.json" else "fixture.json")))
  stopifnot(resumed$run_status == "completed")
  canonical <- export(run)
  lossless <- export(run, "csv")
  derived <- export(run, "csv", "r")
  rows <- records(canonical$artifacts$export)
  stopifnot(identical(rows[seq_along(original)], original))
  csv <- audit(lossless$artifacts$export, "\\N")
  r <- audit(derived$artifacts$export, "NA")
  stopifnot(length(rows) == nrow(r), nrow(r) == nrow(csv))
  stopifnot(identical(r$task_id, csv$task_id), identical(r$attempt_id, csv$attempt_id))
  for (field in c("run_id", "request_uid", "provider", "endpoint", "model", "mode", "manifest_sha256")) {
    stopifnot(identical(vapply(rows, function(x) x[[field]], character(1L)), r[[field]]))
    stopifnot(identical(r[[field]], csv[[field]]))
  }
  stopifnot(canonical$data$row_count == length(rows), canonical$data$result_high_water_mark >= length(rows))
  stopifnot(nchar(canonical$data$files[[1L]]$sha256) == 64L, canonical$data$files[[1L]]$byte_length == file.info(canonical$artifacts$export)$size)
  stopifnot(identical(vapply(rows, function(x) x$status, character(1L)), r$status))
  stopifnot(identical(vapply(rows, function(x) x$attempt_id, character(1L)), r$attempt_id))
  stopifnot(all(r$trait_id %in% c("001", "002", "003")), all(nchar(r$manifest_sha256) == 64L))
  stopifnot(all(is.na(r$input_tokens)), all(is.na(r$output_tokens)))
  for (index in seq_along(rows)) for (field in c("tier", "error_kind", "error_message", "explanation", "confidence", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "prompt_path", "raw_response_path", "probability_sum_tolerance")) {
    scalar(rows[[index]][[field]], r[[field]][[index]])
    scalar(rows[[index]][[field]], csv[[field]][[index]])
  }
  stopifnot(all(r$explanation[r$status == "complete"] == "Authorized normalized explanation."))
  stopifnot(!anyDuplicated(paste(r$task_id, r$attempt_id, sep = "|")))
  if (capture == "minimal") stopifnot(all(is.na(r$prompt_path)), all(is.na(r$raw_response_path)))
  else stopifnot(all(!is.na(r$prompt_path)), all(!is.na(r$raw_response_path)))
  if (kind == "pairwise") {
    stopifnot(identical(r$pair_uid, c("001", "002")), all(r$left_item_id == "001"), all(r$right_item_id == "002"))
    stopifnot(identical(r$ID1, c("001", "002")), identical(r$ID2, c("002", "001")))
    stopifnot(identical(r$better_id, r$ID1), all(as.numeric(r$result) == 1))
    stopifnot(identical(as.numeric(r$p_first), c(0.75, 0.75)), identical(as.numeric(r$p_left), c(0.75, 0.25)))
    stopifnot(identical(vapply(rows, function(x) paste(unlist(x$presented_order), collapse = ":"), character(1L)), c("001:002", "002:001")))
    for (index in seq_along(rows)) {
      for (field in c("pair_id", "pair_uid", "left_item_id", "right_item_id", "source_pair_uid", "repetition_index", "is_reversal", "reversal_group", "winner_id", "tie", "allow_ties", "p_left", "p_right", "p_tie")) {
        scalar(rows[[index]][[field]], r[[field]][[index]])
        scalar(rows[[index]][[field]], csv[[field]][[index]])
      }
      for (field in c("original_order", "presented_order")) {
        stopifnot(identical(jsonlite::fromJSON(r[[field]][[index]], simplifyVector = FALSE), rows[[index]][[field]]))
        stopifnot(identical(jsonlite::fromJSON(csv[[field]][[index]], simplifyVector = FALSE), rows[[index]][[field]]))
      }
    }
    stopifnot(identical(as.logical(r$is_reversal), c(FALSE, TRUE)), all(r$source_pair_uid == "001"), all(as.integer(r$repetition_index) == 0L))
  } else {
    stopifnot(all(r$item_id %in% c("001", "002")))
    for (index in seq_len(nrow(r))) {
      row <- r[index, ]
      coding <- jsonlite::fromJSON(row$resolved_levels_json)
      canonical_row <- rows[[index]]
      stopifnot(identical(jsonlite::fromJSON(row$resolved_levels_json, simplifyVector = FALSE), canonical_row$resolved_levels))
      stopifnot(identical(jsonlite::fromJSON(csv$resolved_levels[[index]], simplifyVector = FALSE), canonical_row$resolved_levels))
      for (field in c("item_id", "category_label", "numeric_category_value", "modal_category_label", "modal_numeric_category_value", "provider_expected_index", "expected_value", "primary_score", "primary_score_kind")) {
        scalar(canonical_row[[field]], row[[field]])
        scalar(canonical_row[[field]], csv[[field]][[index]])
      }
      if (is.null(canonical_row$probabilities)) stopifnot(is.na(row$probabilities_json), is.na(csv$probabilities[[index]]))
      else {
        stopifnot(identical(jsonlite::fromJSON(row$probabilities_json, simplifyVector = FALSE), canonical_row$probabilities))
        stopifnot(identical(jsonlite::fromJSON(csv$probabilities[[index]], simplifyVector = FALSE), canonical_row$probabilities))
      }
      stopifnot(identical(coding$label, c("low", "middle", "high")), identical(coding$numeric_value, c(1, 3, 8)))
      if (row$status != "complete") {
        stopifnot(is.na(row$category_label), is.na(row$primary_score), is.na(row$explanation))
      } else if (kind == "independent") {
        stopifnot(row$primary_score_kind == "category_code", as.numeric(row$primary_score) == 1, is.na(row$expected_value), is.na(row$probabilities_json), is.na(row$provider_expected_index), is.na(row$modal_category_label))
      } else {
        distribution <- jsonlite::fromJSON(row$probabilities_json)
        stopifnot(identical(distribution$level_label, coding$label))
        expected <- sum(distribution$probability * coding$numeric_value)
        stopifnot(abs(sum(distribution$probability) - 1) < 1e-8)
        stopifnot(row$primary_score_kind == "expected", abs(as.numeric(row$expected_value) - expected) < 1e-12)
        stopifnot(row$category_label == "low", row$modal_category_label == "middle")
        stopifnot(abs(as.numeric(row$provider_expected_index) - sum((seq_len(nrow(distribution)) - 1L) * distribution$probability)) < 1e-12)
      }
    }
  }
  evidence <- fixture("inspect", directory)
  assert_evidence(evidence, rows)
  stopifnot(all(vapply(evidence$acceptances, function(x) x$acceptance_count, integer(1L)) == 1L))
  stopifnot(length(evidence$acceptances) == if (kind == "grouped") 4L else 2L)
  if (kind == "grouped") {
    stopifnot(length(evidence$retry_operations) == 2L, length(evidence$results) == 8L)
    memberships <- table(vapply(evidence$attempt_tasks, function(x) x$attempt_id, character(1L)))
    stopifnot(identical(sort(as.integer(memberships)), c(1L, 1L, 3L, 3L)))
  }
  stopifnot(file.info(before$artifacts$export)$size == length(before_bytes))
  stopifnot(identical(before_bytes, readBin(before$artifacts$export, "raw", n = length(before_bytes))))
  if (kind == "grouped") {
    historical <- cli(c("results", "export", run_arguments(run), "--high-water-mark", as.character(before$data$result_high_water_mark)))
    stopifnot(historical$artifacts$export == before$artifacts$export)
  }
  repeated_export <- export(run)
  stopifnot(canonical$artifacts$export == repeated_export$artifacts$export)
  listed <- cli(c("runs", "list", "--store", store, "--limit", "1"))
  stopifnot(listed$status == "ok")
}
# Explicit v2 configurations execute through the same installed CLI and seventh gate.
layout_scores <- list()
for (name in c("together", "scores_first", "explanations_first", "scores_only", "partial_full", "partial_minimal", "winner", "tie")) {
  directory <- file.path(root, paste0("v2-", name))
  config <- file.path(directory, "config.json")
  is_pairwise <- name %in% c("winner", "tie")
  is_partial <- startsWith(name, "partial")
  if (name == "together") {
    stopifnot(cli(c("validate", "--config", config))$data$valid)
    stopifnot(is.null(cli(c("plan", "--config", config))$error))
    preview <- cli(c("templates", "preview", "--config", config, "--item-id", "001", "--trait", "001"), content = TRUE)
    stopifnot(grepl("SOURCE_ONLY_SENTINEL", preview$data$text, fixed = TRUE))
    stopifnot(preview$data$renderer_version == 2L, preview$data$output_commitment$selection$mechanism == "text")
    plain_file <- file.path(root, "v2-preview.txt")
    plain_error <- file.path(root, "v2-preview.stderr")
    plain_status <- system2(console, vapply(c("templates", "preview", "--config", config, "--item-id", "001", "--trait", "001"), shQuote, character(1L)), stdout = plain_file, stderr = plain_error)
    stopifnot(plain_status == 0L)
    plain_bytes <- readBin(plain_file, "raw", n = file.info(plain_file)$size)
    # V2 stdout preserves the exact committed UTF-8 prompt bytes on every OS.
    stopifnot(identical(plain_bytes, charToRaw(enc2utf8(preview$data$text))))
    stopifnot(!dir.exists(file.path(directory, "store")))
  }
  started <- cli(c("run", "--config", config, "--store", file.path(directory, "store"), settings(directory)), if (is_partial) 3L else 0L)
  run <- list(run_id = started$run_id, store = file.path(directory, "store"))
  stopifnot(cli(c("runs", "status", run_arguments(run)))$run_id == run$run_id)
  before <- export(run)
  before_bytes <- readBin(before$artifacts$export, "raw", n = file.info(before$artifacts$export)$size)
  original <- records(before$artifacts$export)
  if (is_partial) {
    failed <- Filter(function(x) x$status != "complete", original)
    good <- Filter(function(x) x$status == "complete", original)
    stopifnot(length(original) == 6L, length(failed) == 2L, length(good) == 4L)
    stopifnot(all(vapply(failed, function(x) x$trait_id == "003" && is.null(x$explanation) && is.null(x$primary_score), logical(1L))))
    for (index in seq_along(failed)) {
      retry <- c("resume", run_arguments(run), settings(directory, "valid.json"), "--retry-id", paste0("v2-failed-explanation-", index), "--retry-task", failed[[index]]$task_id)
      retried <- cli(retry, if (index == length(failed)) 0L else 3L)
    }
    stopifnot(retried$run_status == "completed", cli(retry)$data$results == 8L)
  }
  canonical <- export(run)
  derived <- export(run, "csv", "r")
  rows <- records(canonical$artifacts$export)
  imported <- audit(derived$artifacts$export, "NA")
  stopifnot(nrow(imported) == length(rows), identical(rows[seq_along(original)], original))
  stopifnot(all(imported$schema_version == "1"))
  stopifnot(identical(imported$task_id, vapply(rows, function(x) x$task_id, character(1L))))
  stopifnot(identical(imported$attempt_id, vapply(rows, function(x) x$attempt_id, character(1L))))
  stopifnot(!anyDuplicated(paste(imported$task_id, imported$attempt_id, sep = "|")))
  stopifnot(all(is.na(imported$input_tokens)), all(is.na(imported$output_tokens)))
  for (index in seq_along(rows)) {
    row <- rows[[index]]
    for (field in c("status", "trait_id", "explanation", "primary_score", "prompt_path", "raw_response_path")) {
      if (field %in% names(row)) scalar(row[[field]], imported[[field]][[index]])
    }
  }
  if (is_pairwise) {
    stopifnot(identical(imported$ID1, c("001", "002")), identical(imported$ID2, c("002", "001")))
    stopifnot(all(imported$left_item_id == "001"), all(imported$right_item_id == "002"))
    stopifnot(identical(vapply(rows, function(x) paste(unlist(x$presented_order), collapse = ":"), character(1L)), c("001:002", "002:001")))
    stopifnot(all(is.na(imported$p_first)), all(is.na(imported$p_left)))
    if (name == "winner") {
      stopifnot(all(!as.logical(imported$tie)), all(!as.logical(imported$allow_ties)))
      stopifnot(identical(imported$better_id, imported$ID1), all(as.numeric(imported$result) == 1))
    } else {
      stopifnot(all(as.logical(imported$tie)), all(as.logical(imported$allow_ties)))
      stopifnot(all(is.na(imported$better_id)), all(as.numeric(imported$result) == 0.5))
    }
  } else {
    stopifnot(all(imported$item_id %in% c("001", "002")))
    for (index in seq_along(rows)) {
      row <- rows[[index]]
      coding <- jsonlite::fromJSON(imported$resolved_levels_json[[index]])
      stopifnot(identical(coding$label, c("low", "middle", "high")), identical(coding$numeric_value, c(1, 3, 8)))
      stopifnot(is.null(row$expected_value), is.null(row$probabilities), is.null(row$provider_expected_index))
      if (row$status == "complete") {
        stopifnot(row$category_label == "middle", row$primary_score == 3, row$primary_score_kind == "category_code")
        if (name == "scores_only") stopifnot(is.null(row$explanation))
        else stopifnot(row$explanation == paste0("Evidence for trait ", row$trait_id, "."))
      }
    }
    if (name %in% c("together", "scores_first", "explanations_first")) {
      ordering <- order(imported$item_id, imported$trait_id)
      layout_scores[[name]] <- imported[ordering, c("item_id", "trait_id", "category_label", "primary_score", "explanation")]
      rownames(layout_scores[[name]]) <- NULL
    }
  }
  evidence <- fixture("inspect", directory)
  assert_evidence(evidence, rows)
  stopifnot(length(evidence$acceptances) == if (is_partial) 4L else 2L)
  stopifnot(all(vapply(evidence$acceptances, function(x) x$acceptance_count, integer(1L)) == 1L))
  if (is_partial) {
    stopifnot(length(evidence$retry_operations) == 2L)
    memberships <- table(vapply(evidence$attempt_tasks, function(x) x$attempt_id, character(1L)))
    stopifnot(identical(sort(as.integer(memberships)), c(1L, 1L, 3L, 3L)))
    historical <- cli(c("results", "export", run_arguments(run), "--high-water-mark", as.character(before$data$result_high_water_mark)))
    stopifnot(historical$artifacts$export == before$artifacts$export)
  }
  stopifnot(identical(before_bytes, readBin(before$artifacts$export, "raw", n = length(before_bytes))))
  if (name %in% c("explanations_first", "scores_only", "partial_minimal", "tie")) {
    stopifnot(all(is.na(imported$prompt_path)), all(is.na(imported$raw_response_path)))
  }
  stopifnot(cli(c("resume", run_arguments(run), settings(directory, if (is_partial) "valid.json" else "fixture.json")))$run_status == "completed")
  stopifnot(length(fixture("inspect", directory)$acceptances) == if (is_partial) 4L else 2L)
}
stopifnot(identical(layout_scores$together, layout_scores$scores_first), identical(layout_scores$together, layout_scores$explanations_first))
# Accepted observations remain blocked without retrieval, then recover exactly once.
directory <- file.path(root, "recovery-minimal")
started <- cli(c("run", "--config", file.path(directory, "config.json"), "--store", file.path(directory, "store"), settings(directory), "--mock-no-retrieval"), 3L)
run <- list(run_id = started$run_id, store = file.path(directory, "store"))
blocked <- cli(c("resume", run_arguments(run), settings(directory), "--mock-no-retrieval"), 3L)
stopifnot(blocked$data$results == 0L, blocked$data$unresolved_submissions == 2L)
recovered <- cli(c("resume", run_arguments(run), settings(directory)))
stopifnot(recovered$data$results == 2L, recovered$run_status == "completed")
stopifnot(length(fixture("inspect", directory)$acceptances) == 2L)
# Real death while submitting cannot invent a result or retry authority.
directory <- file.path(root, "interrupted-minimal")
# A test-only supervisor retains OS ownership of the installed console it launches.
# It returns only after the actual executor and Windows launcher have exited.
killed <- invoke(python, c(helper, "--phase", "interrupt", "--output", directory, "--executable", console))
run <- list(run_id = killed$run_id, store = file.path(directory, "store"))
stopifnot(killed$accepted == 1L, isTRUE(killed$exit_confirmed))
status <- cli(c("runs", "status", run_arguments(run)))
stopifnot(!isTRUE(status$data$executor_active))
blocked <- cli(c("resume", run_arguments(run), settings(directory, "valid.json")), 3L)
stopifnot(blocked$data$results == 1L)
interrupted_evidence <- fixture("inspect", directory)
stopifnot(sum(vapply(interrupted_evidence$acceptances, function(x) x$acceptance_count, integer(1L))) == 1L)
stopifnot(identical(vapply(interrupted_evidence$results, function(x) x$record_sha256, character(1L)), unlist(killed$result_sha256)))
missing <- cli(c("runs", "status", "--run", "missing", "--store", run$store), 2L)
stopifnot(missing$error$category == "not_found", missing$status == "error")
cat(sprintf("Installed CLI lifecycle interoperability passed: helper setup %.2fs; lifecycle %.2fs\n", setup_seconds, proc.time()[["elapsed"]] - lifecycle_started))
