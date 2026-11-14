args <- commandArgs(trailingOnly = TRUE)
stopifnot(length(args) == 1L, requireNamespace("jsonlite", quietly = TRUE))
root <- args[[1L]]
json_lines <- function(name) lapply(readLines(file.path(root, name), warn = FALSE), function(line) jsonlite::fromJSON(line, simplifyVector = FALSE))
rows <- json_lines("results.jsonl")
physical <- json_lines("physical.jsonl")
rubric <- read.csv(file.path(root, "rubric_r.csv"), colClasses = "character", na.strings = "NA", check.names = FALSE)
pairs <- read.csv(file.path(root, "pairwise_r.csv"), colClasses = "character", na.strings = "NA", check.names = FALSE)
canonical <- read.csv(file.path(root, "results.csv"), colClasses = "character", check.names = FALSE)
stopifnot(length(rows) == 27L, length(physical) == 5L, nrow(canonical) == 27L)
stopifnot(nrow(rubric) == 24L, nrow(pairs) == 3L)
stopifnot(sum(rubric$status == "complete") == 12L, sum(rubric$status == "invalid") == 12L)
stopifnot(all(is.na(rubric$expected_value)), all(is.na(rubric$probabilities_json)))
stopifnot(all(rubric$category_label[rubric$status == "complete"] == "2"))
for (cell in rubric$resolved_levels_json) {
  coding <- jsonlite::fromJSON(cell)
  stopifnot(identical(coding$label, c("0", "1", "2")), identical(as.numeric(coding$numeric_value), c(0, 1, 2)))
}
stopifnot(all(pairs$status == "complete"), all(pairs$better_id == pairs$ID1), all(is.na(pairs$explanation)))
stopifnot(pairs$ID1[[1L]] == pairs$ID2[[3L]], pairs$ID2[[1L]] == pairs$ID1[[3L]])
for (row in rows) {
  stopifnot(is.character(row$run_id), is.character(row$request_uid), is.character(row$attempt_id))
  receipt <- Filter(function(p) p$identity$run_id == row$run_id && p$identity$attempt_id == row$attempt_id, physical)
  stopifnot(length(receipt) == 1L, receipt[[1L]]$identity$request_uid == row$request_uid)
  stopifnot(is.null(row$input_tokens), is.null(row$output_tokens))
  if (row$task_kind == "pairwise") {
    stopifnot(identical(row$winner_id, row$presented_order[[1L]]), !row$tie, is.null(row$p_left), is.null(row$p_right))
  }
}
for (p in physical) {
  stopifnot(is.null(p$generation$top_p), p$usage$provider == "openai")
  stopifnot(is.numeric(p$usage$input_tokens), is.numeric(p$usage$output_tokens))
  if (is.null(p$condition) || p$condition == "A") {
    stopifnot(identical(names(p$generation), "max_output_tokens"), is.null(p$reasoning_policy))
  } else {
    stopifnot(p$generation$reasoning$effort == "none", is.null(p$generation$reasoning$mode))
    stopifnot(p$reasoning_policy$requested$effort == "none")
    if (p$condition == "T1") stopifnot(p$generation$temperature == 0.2) else stopifnot(is.null(p$generation$temperature))
  }
}
stopifnot(length(unique(vapply(physical, function(p) p$identity$submission_id, character(1)))) == 5L)
cat("Control exports passed: 27 actual rows, five physical receipts, explicit/omitted controls and CJ reversal\n")
