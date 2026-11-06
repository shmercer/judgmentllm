# Read an immutable R-view CSV exported by the installed CLI.
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) stop("Supply the exported R CSV path")
audit <- read.csv(args[[1L]], colClasses = "character", na.strings = "NA",
  check.names = FALSE, stringsAsFactors = FALSE, fileEncoding = "UTF-8", comment.char = "")
stopifnot(all(audit$schema_version == "1"), is.character(audit$trait_id))
stopifnot(!anyDuplicated(paste(audit$task_id, audit$attempt_id, sep = "|")))
if ("expected_value" %in% names(audit)) stopifnot(all(is.na(audit$expected_value)))
print(audit[, intersect(c("item_id", "pair_uid", "trait_id", "status", "primary_score", "explanation", "tie"), names(audit)), drop = FALSE])
