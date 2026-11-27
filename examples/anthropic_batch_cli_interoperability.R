# Gate seven uses the same installed CLI and the private synthetic transport.
source(file.path(dirname(helper), "..", "examples", "anthropic_batch_interoperability.R"), local = TRUE)
anthropic_helper <- file.path(dirname(helper), "anthropic_batch_fixture.py")
for (case in list(c("anthropic-batch-grouped-full", "full"), c("anthropic-batch-pair-minimal", "minimal"))) {
  directory <- file.path(root, case[[1L]])
  selection <- c("--run", "001", "--store", file.path(directory, "store"))
  anthropic_cli <- function(arguments, expected = 0L) {
    value <- invoke(python, c(anthropic_helper, "cli", directory, console, arguments, "--json"), expected)
    stopifnot(identical(names(value), c("schema_version", "command", "status", "run_id", "run_status", "artifacts", "data", "error")),
      value$schema_version == 1L, value$run_id == "001", value$status == if (expected == 0L) "ok" else "partial")
    raw <- unlist(lapply(file.path(root, paste0(c("stdout-", "stderr-"), sequence, c(".json", ".jsonl"))),
      readLines, encoding = "UTF-8", warn = FALSE), use.names = FALSE)
    stopifnot(!any(grepl("synthetic-assessment-private-canary|synthetic-anthropic-batch-key", raw)))
    value
  }
  anthropic_cli(c("batch", "prepare", "--config", file.path(directory, "config.json"), selection))
  anthropic_cli(c("batch", "submit", selection))
  anthropic_cli(c("batch", "status", selection, "--refresh"))
  anthropic_cli(c("batch", "collect", selection, "--max-result-rows", "1"), 3L)
  partial <- anthropic_cli(c("results", "provenance", selection))$data
  pairwise <- case[[2L]] == "minimal"
  check_anthropic_batch(partial$manifest_path, case[[2L]], pairwise, complete = FALSE)
  old_path <- batch_companion(partial, "results.jsonl")
  old_bytes <- readBin(old_path, "raw", n = file.info(old_path)$size)
  anthropic_cli(c("batch", "resume", selection))
  final <- anthropic_cli(c("results", "provenance", selection))$data
  check_anthropic_batch(final$manifest_path, case[[2L]], pairwise)
  stopifnot(identical(readBin(old_path, "raw", n = file.info(old_path)$size), old_bytes))
}
