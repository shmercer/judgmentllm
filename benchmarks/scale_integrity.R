# Offline, bounded-read verification of Python-produced large rubric exports.
# Usage: Rscript --vanilla benchmarks/scale_integrity.R SAMPLE_JSON
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) stop("Supply one scale sample/diagnostic JSON report")
if (!requireNamespace("jsonlite", quietly = TRUE)) stop("Required jsonlite is unavailable")
report <- jsonlite::fromJSON(args[[1L]], simplifyVector = FALSE)
files <- report$export_files
select <- function(format, view) {
  found <- Filter(function(x) x$format == format && x$view == view, files)
  stopifnot(length(found) == 1L)
  found[[1L]]
}
canonical <- select("jsonl", "canonical")
audit <- select("csv", "r")
stopifnot(canonical$rows == audit$rows,
  canonical$result_high_water_mark == audit$result_high_water_mark)
for (entry in files) {
  stopifnot(file.exists(entry$path), file.info(entry$path)$size == entry$bytes,
    nchar(entry$sha256) == 64L)
}
j <- file(canonical$path, "rt", encoding = "UTF-8")
r <- file(audit$path, "rt", encoding = "UTF-8")
header <- names(read.csv(text = readLines(r, n = 1L), check.names = FALSE))
total <- completed <- unsuccessful <- 0L
leading_item <- leading_trait <- FALSE
repeat {
  lines <- readLines(j, n = 128L, warn = FALSE)
  if (!length(lines)) break
  rows <- read.csv(r, header = FALSE, col.names = header, nrows = length(lines),
    colClasses = "character", na.strings = "NA", check.names = FALSE,
    stringsAsFactors = FALSE, comment.char = "", blank.lines.skip = FALSE)
  stopifnot(nrow(rows) == length(lines))
  for (index in seq_along(lines)) {
    observed <- jsonlite::fromJSON(lines[[index]], simplifyVector = FALSE)
    row <- rows[index, , drop = FALSE]
    stopifnot(observed$task_kind == "rubric", observed$schema_version == 1L)
    for (key in c("run_id", "task_id", "request_uid", "attempt_id", "item_id", "trait_id", "status")) {
      stopifnot(is.character(observed[[key]]), identical(observed[[key]], row[[key]][[1L]]))
    }
    stopifnot(grepl("^[0-9]+$", row$item_id), grepl("^[0-9]+$", row$trait_id),
      identical(row$response_id, row$item_id), nchar(row$manifest_sha256) == 64L,
      identical(observed$manifest_sha256, row$manifest_sha256[[1L]]))
    leading_item <- leading_item || grepl("^0[0-9]+$", row$item_id)
    leading_trait <- leading_trait || grepl("^0[0-9]+$", row$trait_id)
    coding <- jsonlite::fromJSON(row$resolved_levels_json)
    stopifnot(nrow(coding) == length(observed$resolved_levels))
    stopifnot(identical(coding$label, vapply(observed$resolved_levels, function(x) x$label, character(1))))
    stopifnot(identical(as.numeric(coding$numeric_value), vapply(observed$resolved_levels, function(x) x$numeric_value, numeric(1))))
    if (row$status == "complete") {
      completed <- completed + 1L
      probabilities <- jsonlite::fromJSON(row$probabilities_json)
      stopifnot(nrow(probabilities) == nrow(coding), identical(probabilities$level_label, coding$label),
        identical(probabilities$level_label, vapply(observed$probabilities, function(x) x$level_label, character(1))),
        identical(as.numeric(probabilities$probability), vapply(observed$probabilities, function(x) x$probability, numeric(1))),
        abs(sum(probabilities$probability) - 1) < 1e-8)
      expected <- sum(probabilities$probability * coding$numeric_value)
      stopifnot(row$primary_score_kind == "expected", observed$primary_score_kind == "expected",
        abs(as.numeric(row$primary_score) - expected) < 1e-10,
        abs(as.numeric(row$expected_value) - expected) < 1e-10,
        abs(observed$expected_value - expected) < 1e-10)
      if (!is.na(row$category_label)) {
        stopifnot(row$category_label %in% coding$label,
          as.numeric(row$numeric_category_value) == coding$numeric_value[match(row$category_label, coding$label)])
      }
    } else {
      unsuccessful <- unsuccessful + 1L
      stopifnot(is.null(observed$primary_score), is.null(observed$probabilities),
        is.na(row$primary_score), is.na(row$probabilities_json), is.na(row$expected_value))
    }
  }
  total <- total + length(lines)
}
stopifnot(length(readLines(r, n = 1L, warn = FALSE)) == 0L, total == canonical$rows,
  total == 0L || (leading_item && leading_trait))
close(j)
close(r)
cat(sprintf("Scale R integrity passed: %d rows; %d complete; %d unsuccessful\n", total, completed, unsuccessful))
