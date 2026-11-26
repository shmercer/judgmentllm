# Sourced within gate seven, retaining its original overall 180-second deadline.
source(file.path(dirname(helper), "..", "examples", "openai_batch_interoperability.R"), local = TRUE)
openai_helper <- file.path(dirname(helper), "openai_batch_fixture.py")
for (case in list(c("openai-batch-responses-full", "responses", "full"), c("openai-batch-chat-minimal", "chat.completions", "minimal"))) {
  directory <- file.path(root, case[[1L]])
  selection <- c("--run", "001", "--store", file.path(directory, "store"))
  openai_cli <- function(arguments, expected = 0L) {
    value <- invoke(python, c(openai_helper, "cli", directory, console, arguments, "--json"), expected)
    stopifnot(identical(names(value), c("schema_version", "command", "status", "run_id", "run_status", "artifacts", "data", "error")), value$schema_version == 1L)
    for (line in readLines(file.path(root, paste0("stderr-", sequence, ".jsonl")), warn = FALSE)) {
      diagnostic <- jsonlite::fromJSON(line, simplifyVector = FALSE)
      stopifnot(diagnostic$schema_version == 1L, diagnostic$event %in% c("progress", "diagnostic"))
    }
    value
  }
  openai_cli(c("batch", "prepare", "--config", file.path(directory, "config.json"), selection))
  invoke(python, c(openai_helper, "configure", directory), json = FALSE)
  openai_cli(c("batch", "submit", selection))
  openai_cli(c("batch", "status", selection, "--refresh"))
  openai_cli(c("batch", "collect", selection, "--max-result-rows", "1"), 3L)
  partial <- openai_cli(c("results", "provenance", selection))$data
  old_path <- batch_companion(partial, "results.jsonl")
  old_bytes <- readBin(old_path, "raw", n = file.info(old_path)$size)
  openai_cli(c("batch", "status", selection))
  openai_cli(c("batch", "resume", selection), 3L)
  openai_cli(c("batch", "resume", selection))
  final <- openai_cli(c("results", "provenance", selection))$data
  checked <- check_openai_batch_audit(final$manifest_path, case[[2L]], case[[3L]])
  stopifnot(length(checked$members) == 2L, length(checked$results) == if (case[[2L]] == "responses") 4L else 2L,
    identical(readBin(old_path, "raw", n = file.info(old_path)$size), old_bytes))
  openai_cli(c("batch", "resume", selection))
  replay <- openai_cli(c("results", "provenance", selection))$data
  replayed <- check_openai_batch_audit(replay$manifest_path, case[[2L]], case[[3L]])
  stopifnot(identical(checked$members, replayed$members), identical(checked$results, replayed$results))
}
