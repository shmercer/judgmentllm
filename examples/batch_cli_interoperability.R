# Sourced by the seventh gate: R owns every installed CLI lifecycle process.
batch_companion <- function(bundle, name) {
  entry <- one(bundle$manifest$files, function(x) x$name == name)
  path <- file.path(dirname(dirname(dirname(bundle$manifest_path))), entry$artifact$relative_path)
  stopifnot(file.exists(path), file.info(path)$size == entry$artifact$byte_length)
  path
}
batch_selection <- function(directory) c("--run", "001", "--store", file.path(directory, "store"))
batch_settings <- function(directory) c("--mock-fixture", file.path(directory, "batch-fixture.json"))
batch_prepare <- function(directory) cli(c("batch", "prepare", "--config", file.path(directory, "config.json"),
  "--run", "001", "--store", file.path(directory, "store"), batch_settings(directory)))

for (name in c("batch-terminal-full", "batch-terminal-minimal", "batch-missing")) {
  directory <- file.path(root, name)
  selection <- batch_selection(directory)
  settings <- batch_settings(directory)
  missing <- identical(name, "batch-missing")
  prepared <- batch_prepare(directory)
  stopifnot(prepared$run_id == "001", prepared$data$batch$jobs == 1L)
  # R starts the installed console. The helper only waits for service evidence
  # and terminates that run's verified owner; it never starts or scores a run.
  out <- file.path(directory, "hard-death.stdout")
  err <- file.path(directory, "hard-death.stderr")
  launched <- system2(console, vapply(c("batch", "submit", selection, settings, "--json"), shQuote, character(1L)),
    stdout = out, stderr = err, wait = FALSE)
  stopifnot(launched == 0L)
  killed <- fixture("batch-kill", directory)
  stopifnot(killed$terminated_owned_executor, length(killed$jobs) == 1L)
  stopped <- cli(c("batch", "cancel", selection, settings, "--mock-no-reconciliation"), expected = 3L)
  stopifnot(stopped$data$batch$local_stop, stopped$data$batch$cancel_all,
    stopped$data$batch$accepted_jobs == 0L, stopped$data$batch$unresolved_operations == 1L)
  local <- cli(c("batch", "status", selection))
  stopifnot(local$data$batch$missing_members == 2L)
  # A fresh process recovers the original job and sends one cancel. Its first
  # finalizing status read fails transiently; creation is never retried.
  cli(c("batch", "resume", selection, settings), expected = 3L)
  known <- fixture("batch-inspect", directory)
  stopifnot(identical(known$jobs, killed$jobs), known$forwards == 3L,
    known$cancelled_jobs == 1L, length(known$attempts) == 2L)
  cli(c("batch", "status", selection, settings, "--refresh"))
  cli(c("batch", "collect", selection, settings, "--max-result-rows", "1"), expected = 3L)
  partial <- cli(c("results", "provenance", selection))$data
  old_file <- batch_companion(partial, "results.jsonl")
  old_bytes <- readBin(old_file, "raw", n = file.info(old_file)$size)
  stopifnot(length(records(old_file)) == 1L)
  cli(c("batch", "resume", selection, settings), expected = if (missing) 3L else 0L)
  final <- cli(c("results", "provenance", selection))$data
  output <- records(batch_companion(final, "results.jsonl"))
  members <- records(batch_companion(final, "members.jsonl"))
  cells <- read.csv(batch_companion(final, "members.csv"), colClasses = "character", check.names = FALSE)
  stopifnot(length(output) == if (missing) 1L else 2L, length(members) == 2L,
    identical(lapply(cells$preparation, jsonlite::fromJSON, simplifyVector = FALSE), lapply(members, function(x) x$preparation)),
    identical(readBin(old_file, "raw", n = file.info(old_file)$size), old_bytes))
  success <- one(output, function(x) x$status == "complete")
  stopifnot(success$item_id == "001", success$trait_id == "01", success$primary_score_kind == "expected",
    abs(success$expected_value - 2.8) < 1e-10, success$provider_expected_index == 1,
    length(success$probabilities) == 3L, identical(vapply(success$resolved_levels, function(x) x$numeric_value, numeric(1L)), c(1, 2, 7)))
  if (!missing) {
    cancelled <- one(output, function(x) x$status == "cancelled")
    stopifnot(cancelled$item_id == "002", is.null(cancelled$primary_score), is.null(cancelled$probabilities))
  }
  physical <- Filter(function(x) !is.null(x$evidence), members)
  usage <- lapply(physical, function(x) x$evidence$usage)
  stopifnot(sum(vapply(usage, function(x) if (is.null(x$input_tokens)) 0L else x$input_tokens, integer(1L))) == 7L,
    sum(vapply(usage, function(x) if (is.null(x$output_tokens)) 0L else x$output_tokens, integer(1L))) == 3L,
    all(vapply(usage, function(x) is.null(x$reported_cost), logical(1L))))
  cli(c("batch", "resume", selection, settings), expected = if (missing) 3L else 0L)
  replay <- fixture("batch-inspect", directory)
  stopifnot(replay$forwards == 3L, identical(replay$attempts, known$attempts),
    length(replay$receipts) == if (missing) 1L else 2L)
  status <- cli(c("batch", "status", selection))
  stopifnot(status$data$batch$missing_members == if (missing) 1L else 0L)
}

directory <- file.path(root, "batch-pairwise")
selection <- batch_selection(directory)
settings <- batch_settings(directory)
batch_prepare(directory)
cli(c("batch", "submit", selection, settings))
cli(c("batch", "status", selection, settings, "--refresh"))
cli(c("batch", "collect", selection, settings))
bundle <- cli(c("results", "provenance", selection))$data
pairs <- records(batch_companion(bundle, "results.jsonl"))
stopifnot(length(pairs) == 2L, all(vapply(pairs, function(x)
  x$mode == "batch_native" && x$winner_id == x$presented_order[[1L]] &&
  identical(x$original_order, list("001", "002")) && abs(x$p_left + x$p_right - 1) < 1e-10, logical(1L))))
pair_audit <- read.csv(batch_companion(bundle, "results_r.csv"), colClasses = "character", na.strings = "NA")
stopifnot(nrow(pair_audit) == 2L, all(pair_audit$better_id %in% c("001", "002")))
