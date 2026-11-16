# Strict consumption of Python-generated V4 companions and unchanged result schema 1.
check_decisions_exports <- function(root) {
  if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Required jsonlite is unavailable")
  records <- function(name) lapply(readLines(file.path(root, name), encoding = "UTF-8"),
    jsonlite::fromJSON, simplifyVector = FALSE)
  csv <- function(name, missing = "\\N") read.csv(file.path(root, name),
    colClasses = "character", na.strings = missing, check.names = FALSE,
    stringsAsFactors = FALSE, fileEncoding = "UTF-8", comment.char = "")
  decode <- function(value) jsonlite::fromJSON(value, simplifyVector = FALSE)
  canonical_object <- function(x) {
    if (!is.list(x)) return(x)
    if (!is.null(names(x))) x <- x[sort(names(x))]
    lapply(x, canonical_object)
  }
  equal_cells <- function(rows, frame) {
    stopifnot(length(rows) == nrow(frame))
    for (i in seq_along(rows)) for (key in names(rows[[i]])) {
      stopifnot(isTRUE(all.equal(rows[[i]][[key]], decode(frame[i, key]), check.attributes = FALSE)))
    }
  }
  runs <- records("decisions.jsonl")
  stopifnot(length(runs) == 8L,
    identical(vapply(runs, function(x) x$run_id, character(1)), sprintf("%03d", 1:8)))
  for (run in runs) {
    prefix <- paste0(run$run_id, "-")
    questions <- records(paste0(prefix, "questions.jsonl"))
    physical <- records(paste0(prefix, "physical.jsonl"))
    result <- records(paste0(prefix, "results.jsonl"))
    equal_cells(questions, csv(paste0(prefix, "questions.csv"), character()))
    equal_cells(physical, csv(paste0(prefix, "physical.csv"), character()))
    canonical <- csv(paste0(prefix, "results.csv"))
    r <- csv(paste0(prefix, "results.r.csv"), "NA")
    stopifnot(length(result) == nrow(canonical), nrow(r) == length(result))
    manifest <- jsonlite::fromJSON(run$bundle$manifest_path, simplifyVector = FALSE)
    stopifnot(identical(canonical_object(manifest), canonical_object(run$bundle$manifest)), manifest$normalized_result_schema == 1L)
    totals <- manifest$totals
    stopifnot(length(physical) == totals$known_invocations)
    stopifnot(!anyDuplicated(vapply(physical, function(x) x$identity$submission_id, character(1))))
    stopifnot(all(vapply(physical, function(x) is.null(x$provider_remote_id), logical(1))))
    if (run$kind == "empty") {
      stopifnot(length(questions) == 0L, length(physical) == 0L, length(result) == 0L,
        nrow(canonical) == 0L, nrow(r) == 0L, totals$physical_requests == 0L,
        totals$known_invocations == 0L, totals$invocation_unknown == 0L,
        totals$usage_evidence == "not_applicable", totals$cost_evidence == "not_applicable",
        is.null(totals$input_tokens), is.null(totals$output_tokens), is.null(totals$cost))
      stopifnot(all(vapply(manifest$files, function(x) x$rows == 0L, logical(1))))
      for (name in c("questions.jsonl", "physical.jsonl")) {
        file <- Filter(function(x) x$name == name, manifest$files)[[1L]]
        stopifnot(file$artifact$byte_length == 0L, !is.null(file$artifact$relative_path),
          file.info(file.path(root, paste0(prefix, name)))$size == 0L)
      }
      next
    }
    if (run$kind == "unknown") {
      stopifnot(length(questions) == 1L, length(result) == 0L, totals$known_invocations == 1L)
      stopifnot(is.null(totals$input_tokens), is.null(totals$output_tokens), is.null(totals$cost))
      stopifnot(totals$usage_evidence == "unknown", is.null(questions[[1L]]$status), is.null(questions[[1L]]$answer))
      next
    }
    calls <- if (run$kind == "rubric") 3L else 4L
    stopifnot(totals$known_invocations == calls, totals$observed_responses == calls,
      totals$input_tokens == 10L * calls, totals$output_tokens == 4L * calls,
      totals$cost == .125 * calls, totals$cost_currency == "USD")
    stopifnot(length(questions) == length(result), identical(r$status, canonical$status))
    stopifnot(any(r$status == "refused"), any(r$status == "invalid"), any(r$status == "complete"))
    for (q in questions) {
      hit <- Filter(function(x) x$task_id == q$question$task_id && x$attempt_id == q$identity$attempt_id, result)
      stopifnot(length(hit) == 1L, hit[[1L]]$status == q$status,
        q$question$ordinal >= 0L, nchar(q$question$key) == 62L,
        is.null(hit[[1L]]$input_tokens), is.null(hit[[1L]]$output_tokens))
    }
    if (run$kind == "rubric") {
      stopifnot(length(result) == 7L, sum(r$item_id == "001") == 4L,
        sum(r$item_id == "002") == 3L, anyDuplicated(r$task_id) > 0L)
      for (x in Filter(function(x) x$status == "complete", result)) {
        stopifnot(identical(vapply(x$resolved_levels, function(y) y$numeric_value, numeric(1)), c(1,2,7)),
          abs(x$primary_score - 5.15) < 1e-12, abs(x$provider_expected_index - 1.55) < 1e-12,
          is.null(x$category_label), x$modal_category_label == "02", x$confidence == .8,
          identical(vapply(x$probabilities, function(y) y$probability, numeric(1)), c(.10,.25,.65)))
      }
    } else {
      stopifnot(length(result) == 12L, length(unique(vapply(questions, function(x) x$presentation_id, character(1)))) == 4L,
        length(unique(vapply(questions, function(x) x$source_presentation_id, character(1)))) == 1L)
      first <- Filter(function(x) x$trait_id == "01" && !x$is_reversal && x$repetition_index == 0L, result)[[1L]]
      stopifnot(identical(first$original_order, list("001", "002")),
        identical(first$presented_order, list("002", "001")), first$winner_id == "001",
        first$p_left == .8, first$p_right == .2, first$source_pair_uid == "criterion-01")
      stopifnot(all(r$result[r$status == "complete" & r$repetition_index == "0" & r$is_reversal == "FALSE"] == "0"))
    }
    if (run$capture == "minimal") stopifnot(all(is.na(r$prompt_path)), all(is.na(r$raw_response_path)))
    else stopifnot(all(!is.na(r$prompt_path)), all(!is.na(r$raw_response_path)))
  }
  cat("Decisions interoperability passed: named members, all attempts and physical totals\n")
}
if (sys.nframe() == 0L) {
  args <- commandArgs(trailingOnly = TRUE)
  if (length(args) != 1L) stop("Supply the Decisions fixture directory")
  check_decisions_exports(args[[1L]])
}
