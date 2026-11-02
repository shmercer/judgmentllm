# Proposed command-line interface (draft)

Executable name: `judgmentllm` (distribution and import name planned as `judgmentllm`). CLI must call the same library functions as the Python interface, never parallel implementations.

## Main commands

| Command | Function |
|---|---|
| `judgmentllm validate --config run.yaml` | Validate rubrics, data, placeholders, model controls, supported modes; no provider calls |
| `judgmentllm plan --config run.yaml` | Show task/request/batch counts, grouping, context estimates and cost estimate with unknowns labeled |
| `judgmentllm run --config run.yaml` | Submit live or managed bulk-live run according to config and return run ID |
| `judgmentllm batch submit --config run.yaml` | Create provider-managed batches, persist registry and return without polling to completion |
| `judgmentllm batch status --run RUN_ID` | Show persisted and refreshed job state (provider calls only if explicitly requested or documented) |
| `judgmentllm batch collect --run RUN_ID` | Retrieve completed results, normalize, preserve errors |
| `judgmentllm batch cancel --run RUN_ID` | Request cancellation with confirmation and audit record |
| `judgmentllm resume --run RUN_ID` | Continue safe live jobs or reconcile pending batches |
| `judgmentllm results export --run RUN_ID --format csv` | Export normalized long-form rubric/pairwise results |
| `judgmentllm runs list` | List local run IDs, states and timestamps |
| `judgmentllm models list --provider openai` | List catalog models with discovery/implementation/test status |
| `judgmentllm models inspect --provider ... --model ...` | Show model/endpoint capabilities and qualification timestamps |
| `judgmentllm models refresh` | Refresh local provider model metadata (requires keys for some providers) |
| `judgmentllm templates list` | Show built-in presets |
| `judgmentllm templates init --preset rubric_scores_only --output prompt.txt` | Copy a simple editable template |
| `judgmentllm templates preview --config run.yaml --item-id essay_001 --trait organization` | Render a single example locally |
| `judgmentllm serve --host 127.0.0.1 --port 8765` | Start the optional local FastAPI service (Milestone 7, requires `[api]` extra; refuse non-loopback host) |

All commands must provide structured `--help`, validation errors, and useful exit codes. No command silently changes model, service tier, exemplar selection, rubric categories, or prompt text. `--dry-run` alias may be supported for `plan` but should not send requests.

## Example run configuration

See `examples/run.yaml` and `examples/rubric.yaml`. These examples are synthetic and require the user to supply their own local `examples/responses.csv` file in later milestones. No API key goes in YAML; use environment variables or cloud-default credentials.

## Output behavior

`run`, `batch submit`: print run ID and run directory; optional `--json` machine-readable summary for R/shell integration. `status` supports concise table and `--json`. `collect` returns status counts, identifies missing/invalid tasks, and reports exported path(s). Streaming output should be available for long running live jobs, but resumption must never depend on an open terminal.

## Error classes and exit codes (to finalize in Milestone 1)

- Invalid configuration, invalid scoring schema or unsupported capability: nonzero error, no request sent.
- Authentication/permission error: nonzero, with provider, endpoint and safe diagnostic but never raw key.
- Transient failure: retry within bounded policy, then preserve partial state and nonzero exit.
- Completed run with some invalid model outputs: do not mark fully successful; preserve all valid outputs and produce a structured partial-completion status.

## Python interface alignment (conceptual, not implemented)

- `validate_run(spec) -> ValidationReport`
- `plan_run(spec) -> RunPlan`
- `run_live(spec) -> RunSummary`
- `submit_batch(spec) -> BatchSummary`
- `resume_run(run_id) -> RunSummary`
- `collect_results(run_id) -> ResultManifest`
- Async variants of the live execution API may be exposed as `arun_live` or through a documented client; choose one clear style in Milestone 1.

R integration should initially call the installed Python library or the CLI via documented commands. Do not require an HTTP server for local R use. `judgmentllm serve` is introduced only in the committed late-stage Milestone 7; it exposes the same library operations and persistent runs without introducing a parallel scoring implementation. See [`http-api-spec.md`](http-api-spec.md).
