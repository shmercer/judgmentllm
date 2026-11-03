# Actual immutable preparation outputs reopened by a separate Python process.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) stop("Supply the source preparation fixture directory")
if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Required jsonlite is unavailable")
rows <- lapply(readLines(file.path(args[[1L]], "source_preparation.jsonl"), encoding = "UTF-8"),
  jsonlite::fromJSON, simplifyVector = FALSE)
stopifnot(length(rows) == 4L)
stopifnot(identical(vapply(rows, function(x) x$run_id, character(1)), sprintf("%03d", 1:4)))
for (row in rows) {
  manifest <- row$manifest
  stopifnot(row$schema_version == 1L, manifest$schema_version == 1L)
  stopifnot(manifest$kind == "executable_run_manifest", manifest$run_id == row$run_id)
  stopifnot(manifest$capture == row$capture, nchar(row$manifest_sha256) == 64L)
  stopifnot(file.exists(row$manifest_path))
  stopifnot(nchar(manifest$configuration_sha256) == 64L)
  stopifnot(nchar(manifest$assessment_sha256) == 64L)
  stopifnot(manifest$generation$settings$temperature == 0)
  stopifnot(manifest$generation$settings$max_output_tokens == 127L)
  stopifnot(is.null(manifest$generation$capabilities)) # Unknown is not qualification.
  stopifnot(identical(unlist(row$response_ids), c("001", "002")))
  stopifnot(all(nchar(unlist(row$response_hashes)) == 64L))
  stopifnot(all(nchar(unlist(row$dependency_hashes)) == 64L))
  stopifnot(row$request_count == 2L)
  expected <- if (row$task_kind == "rubric") 5L else 6L
  stopifnot(length(manifest$sources) == expected)
  roles <- vapply(manifest$sources, function(x) x$role, character(1))
  stopifnot(all(c("configuration", "exemplars", "template", "response") %in% roles))
  if (row$task_kind == "pairwise") {
    stopifnot(all(c("criteria", "pair") %in% roles), length(row$pairs) == 2L)
    stopifnot(identical(vapply(row$pairs, function(x) x$pair_uid, character(1)), c("001", "002")))
    for (pair in row$pairs) {
      stopifnot(pair$left_item_id == "001", pair$right_item_id == "002", pair$trait_id == "001")
      stopifnot(identical(unlist(pair$original_order), c("001", "002")))
    }
    stopifnot(identical(unlist(row$pairs[[1L]]$presented_order), c("001", "002")))
    stopifnot(identical(unlist(row$pairs[[2L]]$presented_order), c("002", "001")))
    stopifnot(is.null(row$pairs[[1L]]$source_pair_uid))
    stopifnot(row$pairs[[2L]]$source_pair_uid == "001", isTRUE(row$pairs[[2L]]$is_reversal))
  } else {
    stopifnot("rubric" %in% roles, length(row$pairs) == 0L)
  }
  for (source in manifest$sources) {
    stopifnot(nchar(source$raw_sha256) == 64L, source$byte_length > 0L)
    stopifnot(is.character(source$source_id), is.character(source$original_location))
    stopifnot(is.list(source$generation))
    if (source$role %in% c("response", "pair")) {
      stopifnot(source$record_count == 2L, nchar(source$index_sha256) == 64L)
      stopifnot(is.list(source$parsing))
    }
    if (row$capture == "full") {
      stopifnot(is.list(source$snapshot), is.character(source$snapshot$relative_path))
      stopifnot(source$snapshot$sha256 == source$raw_sha256)
    } else {
      stopifnot(is.null(source$snapshot) || is.null(source$snapshot$relative_path))
    }
  }
  if (row$capture == "minimal") stopifnot(is.null(manifest$specification$relative_path))
}
cat("Source preparation interoperability passed: 4 independently reopened runs\n")
