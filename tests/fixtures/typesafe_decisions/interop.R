stopifnot(requireNamespace("jsonlite", quietly = TRUE))
runs <- jsonlite::fromJSON(commandArgs(trailingOnly = TRUE)[[1]], simplifyVector = FALSE)
stopifnot(identical(names(runs), c("single", "grouped", "pairwise")))

read_jsonl <- function(path) {
  lapply(readLines(path, warn = FALSE, encoding = "UTF-8"),
         jsonlite::fromJSON, simplifyVector = FALSE)
}

for (case in names(runs)) {
  run <- runs[[case]]
  rows <- read_jsonl(run$canonical$files[[1]]$path)
  expected <- c(single = 1L, grouped = 4L, pairwise = 8L)[[case]]
  calls <- c(single = 1L, grouped = 2L, pairwise = 4L)[[case]]
  r <- read.csv(run$r$files[[1]]$path, colClasses = "character")
  stopifnot(length(rows) == expected, nrow(r) == expected)
  stopifnot(nrow(read.csv(run$csv$files[[1]]$path, colClasses = "character")) == expected)
  for (row in rows) {
    stopifnot(row$provider == "typesafe", row$endpoint == "systemone",
              row$model == "jev-1.14.0", is.null(row$input_tokens))
    if (row$status != "complete") next
    if (case == "pairwise") {
      stopifnot(row$left_item_id == "001", row$right_item_id == "002",
                row$winner_id == row$presented_order[[2]], row$confidence == 0.7,
                row$p_left == if (row$presented_order[[1]] == "001") 0.2 else 0.8,
                row$p_left + row$p_right == 1)
    } else {
      stopifnot(row$item_id == "000", abs(row$expected_value - 5.15) < 1e-12,
                row$provider_expected_index == 1.55, row$confidence == 0.8,
                is.null(row$category_label), row$modal_category_label == "02",
                identical(vapply(row$probabilities, function(p) p$probability, numeric(1)), c(0.1, 0.25, 0.65)),
                identical(vapply(row$resolved_levels, function(level) level$numeric_value, numeric(1)), c(1, 2, 7)))
    }
  }
  if (case == "pairwise") {
    stopifnot(all(r$winner_pos == "2"), all(r$result == "0"),
              setequal(vapply(rows, function(x) x$p_left, numeric(1)), c(0.2, 0.8)))
  }
  bundle <- run$provenance$manifest
  root <- dirname(dirname(run$provenance$manifest_path))
  bundle_file <- function(name) {
    entry <- Filter(function(x) x$name == name, bundle$files)[[1]]
    file.path(root, entry$artifact$relative_path)
  }
  physical <- read_jsonl(bundle_file("physical.jsonl"))
  questions <- read_jsonl(bundle_file("questions.jsonl"))
  stopifnot(bundle$provenance_manifest_version == 3L, bundle$question_row_version == 1L,
            length(physical) == calls, length(questions) == expected)
  for (attempt in physical) {
    stopifnot(attempt$limits$request_context_fit == "unverified",
              attempt$limits$question_context_fit == "unverified",
              attempt$complete_response_evidence$response_sha256 == attempt$original_response$sha256,
              attempt$selected_usage$input_tokens == 120L,
              is.null(attempt$selected_usage$output_tokens), is.null(attempt$selected_usage$reported_cost))
    if (case == "single") stopifnot(is.null(attempt$retained_response$relative_path))
  }
  csv <- read.csv(bundle_file("physical.csv"), colClasses = "character")
  stopifnot(nrow(csv) == calls, all(csv$physical_provenance_version == "3"))
  if (case == "grouped") {
    failed <- Filter(function(x) x$status == "invalid", questions)
    retried <- Filter(function(x) !is.null(x$parent_commitment_sha256), questions)
    stopifnot(length(failed) == 1L, length(retried) == 1L,
              failed[[1]]$question$key == retried[[1]]$question$key,
              failed[[1]]$identity$attempt_id != retried[[1]]$identity$attempt_id,
              any(vapply(physical, function(x) x$retained_kind == "filtered_replay", logical(1))))
  }
}

cat("Jev Decisions interoperability passed\n")
