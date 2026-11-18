stopifnot(requireNamespace("jsonlite", quietly = TRUE))
runs <- jsonlite::fromJSON(commandArgs(trailingOnly = TRUE)[[1]], simplifyVector = FALSE)

read_jsonl <- function(path) {
  lapply(readLines(path, warn = FALSE, encoding = "UTF-8"),
         jsonlite::fromJSON, simplifyVector = FALSE)
}

for (name in names(runs)) {
  run <- runs[[name]]
  rows <- read_jsonl(run$canonical$files[[1]]$path)
  r <- read.csv(run$r$files[[1]]$path, colClasses = "character")
  stopifnot(nrow(r) == length(rows),
            nrow(read.csv(run$csv$files[[1]]$path)) == length(rows))
  for (row in rows) {
    stopifnot(row$provider == "gemini", row$endpoint == "decisions",
              row$model == "gemini-decision-1", is.null(row$input_tokens))
    if (name == "rejection") {
      stopifnot(row$status == "errored")
    } else if (name == "pairwise") {
      stopifnot(row$status == "complete",
                identical(unlist(row$original_order), c("001", "002")),
                row$winner_id == row$presented_order[[2]], row$confidence == 0.8,
                row$p_left == if (row$presented_order[[1]] == "001") 0.2 else 0.8,
                row$p_right == if (row$presented_order[[1]] == "001") 0.8 else 0.2)
    } else if (row$status == "complete") {
      stopifnot(row$item_id == "000", grepl("^0[0-2]$", row$trait_id),
                is.null(row$category_label), row$modal_category_label == "02",
                abs(row$primary_score - 5.15) < 1e-12, row$confidence == 0.65,
                row$provider_expected_index %in% c(1.55, 1.550001),
                identical(vapply(row$probabilities, function(p) p$probability, numeric(1)), c(0.1, 0.25, 0.65)),
                identical(vapply(row$resolved_levels, function(level) level$numeric_value, numeric(1)), c(1, 2, 7)))
    }
  }
  bundle <- run$provenance$manifest
  root <- dirname(dirname(run$provenance$manifest_path))
  bundle_file <- function(name) {
    entry <- Filter(function(x) x$name == name, bundle$files)[[1]]
    file.path(root, entry$artifact$relative_path)
  }
  physical <- read_jsonl(bundle_file("physical.jsonl"))
  questions <- read_jsonl(bundle_file("questions.jsonl"))
  stopifnot(bundle$provenance_manifest_version == 4L, length(questions) == length(rows),
            length(physical) == c(rubric = 2L, pairwise = 4L, rejection = 1L)[[name]])
  for (attempt in physical) {
    stopifnot(attempt$limits$context_fit == "unverified",
              attempt$limits$max_context_tokens == 262144L,
              attempt$limits$local_body_bytes == 262144L)
    if (name == "rejection") {
      stopifnot(attempt$rejection$reason == "max_tokens_exceeded",
                is.null(attempt$complete_response_evidence),
                is.null(attempt$selected_usage$input_tokens))
    } else {
      stopifnot(attempt$complete_response_evidence$response_sha256 == attempt$original_response$sha256,
                attempt$selected_usage$input_tokens == 120L,
                attempt$selected_usage$output_tokens == 0L)
    }
  }
  if (name == "pairwise") {
    stopifnot(all(r$winner_pos == "2"), all(r$result == "0"),
              setequal(vapply(rows, function(x) x$p_left, numeric(1)), c(0.2, 0.8)))
  } else if (name == "rubric") {
    failed <- Filter(function(x) x$status == "invalid", questions)
    retried <- Filter(function(x) !is.null(x$parent_commitment_sha256), questions)
    stopifnot(length(failed) == 1L, length(retried) == 1L,
              failed[[1]]$question$key == retried[[1]]$question$key,
              failed[[1]]$identity$attempt_id != retried[[1]]$identity$attempt_id)
  }
}
cat("Gemini Decisions interoperability passed\n")
