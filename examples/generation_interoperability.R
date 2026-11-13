# Generation manifest sections only; no provider request or executable manifest.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) stop("Supply the generation fixture output directory")
if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Required jsonlite is unavailable")
rows <- lapply(readLines(file.path(args[[1L]], "generation_manifests.jsonl"), encoding = "UTF-8"),
  jsonlite::fromJSON, simplifyVector = FALSE)
stopifnot(length(rows) == 6L)
stopifnot(identical(vapply(rows, function(x) x$run_id, character(1)), sprintf("%03d", 1:6)))
for (row in rows) {
  stopifnot(row$kind == "generation_manifest_fixture", row$schema_version == 1L)
  stopifnot(row$generation$schema_version == 1L)
  stopifnot(row$generation$provider == "fixture", row$generation$endpoint == "generative")
  stopifnot(row$generation$model == "synthetic-v1")
  stopifnot(nchar(row$configuration_source_sha256) == 64L)
  stopifnot(nchar(row$generation$requested_sha256) == 64L)
}
full <- rows[[1L]]$generation
stopifnot(setequal(names(full$settings), c("temperature", "top_p", "max_output_tokens")))
stopifnot(full$settings$temperature == 0, full$settings$top_p == 0.8)
stopifnot(full$settings$max_output_tokens == 127L)
stopifnot(full$capabilities$provider == full$provider)
stopifnot(full$capabilities$endpoint == full$endpoint, full$capabilities$model == full$model)
stopifnot(isTRUE(full$capabilities$fixture_tested), identical(full$capabilities$live_qualified, FALSE))
for (name in c("temperature", "top_p", "max_output_tokens")) {
  stopifnot(isTRUE(full$capabilities[[name]]$supported))
  stopifnot(is.null(full$capabilities[[name]]$minimum), is.null(full$capabilities[[name]]$maximum))
}
partial <- rows[[2L]]$generation
stopifnot(identical(names(partial$settings), "temperature"), partial$settings$temperature == 0)
stopifnot(is.null(partial$capabilities)) # Explicitly unknown, never qualified.
stopifnot(!identical(full$requested_sha256, partial$requested_sha256))
omitted <- rows[3:6]
stopifnot(all(vapply(omitted, function(x) length(x$generation$settings) == 0L, logical(1))))
stopifnot(all(vapply(omitted, function(x) is.null(x$generation$capabilities), logical(1))))
for (name in c("normalized_configuration_sha256", "request_uid")) {
  stopifnot(length(unique(vapply(omitted, function(x) x[[name]], character(1)))) == 1L)
}
stopifnot(length(unique(vapply(omitted, function(x) x$generation$requested_sha256, character(1)))) == 1L)
stopifnot(length(unique(vapply(omitted, function(x) x$configuration_source_sha256, character(1)))) == 4L)
stopifnot(!identical(partial$requested_sha256, omitted[[1L]]$generation$requested_sha256))
cat("Generation interoperability passed: 6 persisted generation manifest fixtures\n")

reasoning <- lapply(readLines(file.path(args[[1L]], "reasoning_generation_manifests.jsonl"), encoding = "UTF-8"),
  jsonlite::fromJSON, simplifyVector = FALSE)
stopifnot(length(reasoning) == 3L)
stopifnot(is.null(reasoning[[1L]]$generation$settings$reasoning))
for (row in reasoning[2:3]) {
  stopifnot(identical(row$generation$settings$reasoning$effort, "none"))
  stopifnot(is.null(row$generation$settings$reasoning$mode))
  stopifnot(identical(row$generation$capabilities$reasoning$control_version, 1L))
  stopifnot(identical(row$generation$capabilities$reasoning$live_qualified, FALSE))
}
stopifnot(is.null(reasoning[[2L]]$generation$settings$temperature))
stopifnot(reasoning[[3L]]$generation$settings$temperature == 0.2)
stopifnot(all(vapply(reasoning, function(x) x$generation$settings$max_output_tokens == 256L, logical(1))))
stopifnot(length(unique(vapply(reasoning, function(x) x$generation$requested_sha256, character(1)))) == 3L)
stopifnot(length(unique(vapply(reasoning, function(x) x$request_uid, character(1)))) == 3L)
cat("Reasoning interoperability passed: omitted effort, explicit none, and custom temperature\n")
