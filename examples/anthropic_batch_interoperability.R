# Independent consumer for synthetic Anthropic Batch audit 3.
check_anthropic_batch <- function(manifest_path, capture, pairwise, complete = TRUE) {
  stopifnot(requireNamespace("jsonlite", quietly = TRUE))
  bundle <- jsonlite::fromJSON(manifest_path, simplifyVector = FALSE)
  stopifnot(bundle$schema_version == 3L, bundle$normalized_result_schema == 1L,
    bundle$run_id == "001", length(bundle$files) == 11L)
  artifact <- function(name) {
    entry <- Filter(function(x) identical(x$name, name), bundle$files)
    stopifnot(length(entry) == 1L)
    file.path(dirname(dirname(dirname(manifest_path))), entry[[1L]]$artifact$relative_path)
  }
  records <- function(name) lapply(readLines(artifact(name), warn = FALSE, encoding = "UTF-8"), jsonlite::fromJSON, simplifyVector = FALSE)
  results <- records("results.jsonl")
  members <- records("members.jsonl")
  physical <- Filter(function(x) !is.null(x$evidence), members)
  n <- if (complete) 2L else 1L
  stopifnot(length(physical) == n, length(results) == n * if (pairwise) 1L else 2L)
  stopifnot(length(unique(vapply(physical, function(x) x$evidence$identity$observation_id, character(1L)))) == n)
  for (row in physical) {
    usage <- row$evidence$usage
    stopifnot(row$evidence$capture == capture, row$evidence$provider_result == "succeeded",
      usage$input_tokens == 7L, usage$output_tokens == 3L, is.null(usage$total_tokens),
      is.null(usage$reported_cost), usage$sdk_version == "2.3.0", usage$invocation_kind == "native_batch")
  }
  lossless <- read.csv(artifact("results.csv"), colClasses = "character", na.strings = "\\N", check.names = FALSE)
  audit <- read.csv(artifact("results_r.csv"), colClasses = "character", na.strings = "NA", check.names = FALSE)
  stopifnot(nrow(lossless) == length(results), nrow(audit) == length(results))
  stopifnot(identical(lossless$attempt_id, vapply(results, function(x) x$attempt_id, character(1L))))
  stopifnot(identical(audit$run_id, rep("001", length(results))))
  expected_orders <- list("01" = list("002", "001"), "02" = list("001", "002"))
  expected_winners <- c("01" = "002", "02" = "001")
  if (pairwise) {
    observed <- vapply(results, function(x) x$pair_uid, character(1L))
    stopifnot(identical(sort(observed), if (complete) c("01", "02") else "02"))
  }
  for (i in seq_along(results)) {
    row <- results[[i]]
    stopifnot(row$provider == "anthropic", row$endpoint == "messages", row$mode == "batch_native", row$status == "complete")
    if (pairwise) {
      order <- expected_orders[[row$pair_uid]]
      winner <- expected_winners[[row$pair_uid]]
      stopifnot(identical(row$pair_id, "01"), identical(row$original_order, list("001", "002")),
        identical(row$presented_order, order), identical(row$winner_id, winner),
        identical(jsonlite::fromJSON(lossless$presented_order[[i]], simplifyVector = FALSE), order),
        identical(audit$better_id[[i]], winner), is.null(row$p_left), is.null(row$p_right))
    } else {
      stopifnot(row$item_id %in% c("001", "002"), audit$item_id[[i]] == row$item_id,
        row$numeric_category_value == 2, is.null(row$probabilities), is.null(row$expected_value),
        identical(vapply(row$resolved_levels, function(x) x$numeric_value, numeric(1L)), c(1, 2, 7)))
    }
    stopifnot((capture == "full") == !is.null(row$raw_response_path))
  }
  controls <- records("controls.jsonl")
  headers <- Filter(function(x) identical(x$record$record_type, "anthropic_batch_http_diagnostic_v1") && x$record$phase == "headers", controls)
  stopifnot(length(headers) >= 4L, all(vapply(headers, function(x) x$record$http_status == 200L && x$record$request_id == "req_fixture", logical(1L))))
  invisible(results)
}

if (sys.nframe() == 0L) {
  args <- commandArgs(trailingOnly = TRUE)
  stopifnot(length(args) == 3L)
  check_anthropic_batch(args[[1L]], args[[2L]], args[[3L]] == "pairwise")
  cat("Anthropic Batch R assertions passed\n")
}
