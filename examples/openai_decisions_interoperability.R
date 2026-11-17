# Strict offline consumer of installed-SDK production exports, never a provider client.
check_openai_decisions_exports <- function(root) {
stopifnot(requireNamespace("jsonlite", quietly = TRUE))
runs <- jsonlite::fromJSON(file.path(root, "exports.json"), simplifyVector = FALSE)
read_lines <- function(path) lapply(readLines(path, warn = FALSE, encoding = "UTF-8"), function(x) jsonlite::fromJSON(x, simplifyVector = FALSE))
for (run in runs) {
  rows <- read_lines(run$canonical$files[[1L]]$path)
  original_rows <- if (run$kind == "rubric") 6L else 12L
  expected <- original_rows + 1L
  original_calls <- if (run$kind == "rubric") 2L else 4L
  calls <- original_calls + 1L
  stopifnot(length(rows) == expected)
  stopifnot(sum(vapply(rows, function(x) x$status == "invalid", logical(1))) == original_calls)
  stopifnot(all(vapply(rows, function(x) is.null(x$input_tokens), logical(1))))
  if (run$capture == "minimal") stopifnot(all(vapply(rows, function(x) is.null(x$raw_response_path), logical(1))))
  for (row in rows) {
    stopifnot(row$schema_version == 1L, row$provider == "openai", row$endpoint == "decisions")
    if (row$status != "complete") next
    if (run$kind == "rubric") {
      stopifnot(row$item_id %in% c("001", "002"), row$trait_id %in% c("01", "02", "03"))
      stopifnot(abs(row$expected_value - 5.15) < 1e-12, abs(row$provider_expected_index - 1.55) < 1e-12)
      stopifnot(is.null(row$category_label), row$modal_category_label == "02", row$confidence == 0.8)
      stopifnot(identical(vapply(row$probabilities, function(x) x$probability, numeric(1)), c(0.1,0.25,0.65)))
      stopifnot(identical(vapply(row$resolved_levels, function(x) x$numeric_value, numeric(1)), c(1,2,7)))
    } else {
      stopifnot(row$winner_id == row$presented_order[[2]], row$left_item_id == "001", row$right_item_id == "002", row$confidence == 0.7)
      stopifnot(row$p_left == if (row$presented_order[[1]] == "001") 0.2 else 0.8)
    }
  }
  bundle <- run$provenance$manifest
  stopifnot(bundle$provenance_manifest_version == 2L, bundle$physical_row_version == 2L, bundle$question_row_version == 1L)
  root <- dirname(dirname(run$provenance$manifest_path))
  file <- function(name) Filter(function(x) x$name == name, bundle$files)[[1]]
  physical <- read_lines(file.path(root, file("physical.jsonl")$artifact$relative_path))
  questions <- read_lines(file.path(root, file("questions.jsonl")$artifact$relative_path))
  stopifnot(length(physical) == calls, length(questions) == expected)
  stopifnot(sum(vapply(physical, function(x) x$selected_usage$input_tokens, numeric(1))) == 120*calls)
  for (p in physical) {
    stopifnot(p$physical_provenance_version == 2L, p$complete_response_evidence$acceptance_evidence_version == 2L)
    stopifnot(is.null(p$provider_remote_id), is.null(p$selected_usage$reported_cost))
    stopifnot(p$selected_usage$usage_source == "openai.decisions.usage", p$selected_usage$cache_read_tokens == 0L)
    if (p$retained_kind == "filtered_replay") {
      stopifnot(p$validation$unexpected_members == 1L)
      stopifnot(p$original_response$sha256 != p$retained_response$sha256)
      stopifnot(identical(lapply(p$validation$members, function(x) x$answer_ordinals), list(list(0L),list(2L),list(3L))))
    } else {
      stopifnot(p$retained_kind == "original", p$validation$unexpected_members == 0L)
      stopifnot(p$original_response$sha256 == p$retained_response$sha256)
    }
    stopifnot(p$complete_response_evidence$response_sha256 == p$original_response$sha256)
    if (run$capture == "minimal") stopifnot(is.null(p$retained_response$relative_path))
  }
  stopifnot(sum(vapply(questions, function(x) identical(x$whole_response_valid, TRUE), logical(1))) == 1L)
  retries <- Filter(function(x) !is.null(x$parent_commitment_sha256), questions)
  stopifnot(length(retries) == 1L)
  previous <- Filter(function(x) x$question$task_id == retries[[1L]]$question$task_id && is.null(x$parent_commitment_sha256), questions)
  stopifnot(length(previous) == 1L, previous[[1L]]$question$key == retries[[1L]]$question$key)
  stopifnot(previous[[1L]]$status == "invalid", retries[[1L]]$status == "complete")
  cells <- read.csv(file.path(root, file("physical.csv")$artifact$relative_path), colClasses="character", check.names=FALSE)
  stopifnot(nrow(cells) == calls)
  stopifnot(all(vapply(cells$physical_provenance_version, function(x) jsonlite::fromJSON(x) == 2L, logical(1))))
  r_table <- read.csv(run$r$files[[1L]]$path, colClasses="character")
  stopifnot(nrow(r_table) == expected)
  if (run$kind == "pairwise") {
    completed <- r_table[r_table$status == "complete", , drop = FALSE]
    stopifnot(all(completed$result == "0"), all(completed$winner_pos == "2"))
    stopifnot(length(unique(vapply(questions, function(x) x$presentation_id, character(1)))) == 4L)
    stopifnot(length(unique(vapply(questions, function(x) x$question$pair$source_pair_uid, character(1)))) == 3L)
  }
  stopifnot(nrow(read.csv(run$csv$files[[1L]]$path, colClasses="character")) == expected)
}
cat("OpenAI Decisions interoperability passed\n")

}
if (sys.nframe() == 0L) {
  args <- commandArgs(trailingOnly = TRUE)
  stopifnot(length(args) == 1L)
  check_openai_decisions_exports(args[[1L]])
}
