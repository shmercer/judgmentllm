# Command-line interface

Executable name: `judgmentllm` (distribution and import name planned as `judgmentllm`). CLI must call the same library functions as the Python interface, never parallel implementations.

## Implemented global commands

- `judgmentllm --help` displays the available global options and exits successfully.
- `judgmentllm --version` prints `judgmentllm VERSION` and exits successfully; the current development version is `0.1.0a2`.
- `judgmentllm` without arguments displays help and exits successfully.

These operations require no credentials and make no provider requests.

## Implemented Milestone 1 template commands

- `judgmentllm templates list` lists the three installed presets.
- `judgmentllm templates init --preset rubric_scores_only --output prompt.txt` copies a preset and refuses overwrites.
- `judgmentllm templates preview --config examples/run.yaml --item-id essay_001 --trait organization` previews the complete independent or grouped request containing that trait.
- `judgmentllm templates preview --config examples/pairwise.yaml --pair-uid comparison_001_reverse` previews one scheduled pair in its recorded presentation order.
- `judgmentllm templates preview --config examples/advanced.yaml --item-id essay_001` previews the one grouped advanced-template request.

Successful operations exit 0. Invalid arguments, configuration, files, selections, templates, or known capabilities exit 2. Preview prints prompt text to stdout and capability warnings to stderr; it does not require credentials or make requests. All commands reuse the documented Python functions. See [prompt guidance](prompt-guidance.md) for syntax and safeguards.

The remaining commands below are future design contracts. Template rows are already implemented; execution, results/run management, catalogs, and HTTP are not.

## Proposed main commands

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
| `judgmentllm runs status --run RUN_ID` | Read durable run state and artifact paths in a new process (Milestone 2) |
| `judgmentllm models list --provider openai` | List catalog models with discovery/implementation/test status |
| `judgmentllm models inspect --provider ... --model ...` | Show model/endpoint capabilities and qualification timestamps |
| `judgmentllm models refresh` | Refresh local provider model metadata (requires keys for some providers) |
| `judgmentllm templates list` | Show built-in presets |
| `judgmentllm templates init --preset rubric_scores_only --output prompt.txt` | Copy a simple editable template |
| `judgmentllm templates preview --config run.yaml --item-id essay_001 --trait organization` | Render a single example locally |
| `judgmentllm serve --host 127.0.0.1 --port 8765` | Start the optional local FastAPI service (Milestone 7, requires `[api]` extra; refuse non-loopback host) |

All commands must provide structured `--help`, validation errors, and useful exit codes. No command silently changes model, service tier, exemplar selection, rubric categories, or prompt text. `--dry-run` alias may be supported for `plan` but should not send requests.

## Example run configuration

See `examples/run.yaml` and `examples/rubric.yaml`. These examples include synthetic responses, exemplars, pair occurrences, and criteria and support offline loading and preview now. No API key goes in YAML; use environment variables or cloud-default credentials.

## CLI-first cross-language execution contract (Milestone 2 onward)

The CLI is the primary execution bridge for R and other external applications. The planned thin `rubricLLM` R interface uses `processx` to invoke the standalone executable for complete runs, task groups, or batches. It does not embed Python or start one Python process per judgment. `reticulate` is an optional secondary method. Canonical normalized JSONL and documented R-compatible CSV are the primary data interchange contracts; ordinary local R use requires no HTTP server.

Each command calls the same Python validation, planning, execution, and persistence services as the library. `run`, `runs status`, `resume`, collection, and result export must operate across independent CLI processes using durable run IDs and artifact paths. A later action must not require the earlier Python process or R session to remain running. Milestone 2 tests this with the mock provider; native batch operations arrive in Milestone 5. Large results go to durable files rather than a complete dataset on stdout.

No execution command or JSON control response is implemented in this documentation follow-up. Existing Milestone 1 template commands keep their documented text behavior. The requirements below apply to lifecycle commands as they reach their assigned milestones.

### Versioned JSON control responses

Applicable validation, planning, run/submission, status/listing, resumption, collection, and result-export commands must provide a documented `--json` option. Future batch equivalents use the same envelope. In JSON mode:

- Stdout contains exactly one JSON control object followed by a newline, including handled errors; no banners, progress messages, or assessment result streams.
- Diagnostics and progress go to stderr. In JSON mode they use documented JSON Lines records with `schema_version`, `event` (`diagnostic` or `progress`), `category`, safe `message`, and nullable `run_id`; progress records add documented counts when available. Command errors also appear in the final stdout control envelope. Human mode may render readable stderr text.
- Operation is noninteractive: no prompts or browser authentication. Missing configuration/credentials fail predictably. Destructive operations require an explicit documented flag rather than an interactive question when automated.
- The envelope carries a supported integer `schema_version`, command identity/outcome, durable run state when applicable, artifact paths, small command-specific metadata, and a structured error. Incompatible control-envelope changes require a version change and migration documentation.
- Artifact paths returned to external callers are absolute filesystem paths. A path may be null until an artifact exists; a returned usable path must refer to durable data, not a temporary stdout stream. Secrets do not belong in diagnostics or control data.

Intended version-1 envelope for Milestone 2 implementation (illustrative values, not executable output today):

```json
{
  "schema_version": 1,
  "command": "runs status",
  "status": "ok",
  "run_id": "run_example",
  "run_status": "running",
  "artifacts": {
    "manifest": "/absolute/run_example/manifest.json",
    "results_jsonl": null,
    "results_csv": null
  },
  "data": {
    "completed_tasks": 10,
    "failed_tasks": 0,
    "pending_tasks": 90
  },
  "error": null
}
```

| Field | Planned meaning |
|---|---|
| `schema_version` | Integer control-envelope version, initially `1`; separate from assessment/file schema versions. |
| `command` | Canonical command name, such as `validate`, `plan`, `run`, `runs status`, `resume`, or `results export`. |
| `status` | Command outcome: `ok`, `partial`, or `error`. This is distinct from the state of an assessed run. |
| `run_id` | Durable original run identifier, or null for operations that do not address/create a run. |
| `run_status` | Documented durable lifecycle state, or null when inapplicable. The initial vocabulary covers `created`, `submitted`, `running`, `completed`, `partial`, `failed`, and `cancelled`; services and CLI must share the same state contract. |
| `artifacts` | Object of documented artifact-name to absolute path/null entries. Empty for operations without artifacts. Includes canonical results and requested derived exports when available. |
| `data` | Small versioned command-specific object: validation issues, plan summary, counts, or bounded run listings. Document each implemented command's fields; never pass through arbitrary provider bodies. |
| `error` | Null on success; otherwise an object containing stable `category`, safe `message`, and boolean `retryable`. Partial completion includes `category: partial_completion` and retained artifact paths/counts. |

Errors use provider-neutral categories: `usage`, `validation`, `unsupported_capability`, `not_found`, `execution`, `partial_completion`, `interrupted`, and `internal`. Provider/account details may refine a safe diagnostic when adapters exist, without changing the envelope or depending on an R package. Preserve explicit unknowns; never invent estimates or capabilities to fill control fields.

### Exit codes, state, and process independence

| Exit code | Meaning |
|---|---|
| `0` | The command succeeded, including a successful submission or status query. Inspect `run_status` to determine whether asynchronous work completed. A successful query of a failed run still returns `0` with `run_status: failed`. |
| `1` | Execution, authentication/transport/provider, or internal failure; use structured error category/message for detail. |
| `2` | Usage, validation, unsupported capability, or unknown run/resource error; invalid submissions do not send a provider request. |
| `3` | A processing/collection operation completed partially, retaining successful outputs and reporting invalid/refused/failed judgments. |
| `130` | Interrupted operation; report the durable run ID, state, and recoverable artifact paths when available. |

Status, resumption, and collection reload durable state across process restarts. Resumption must not depend on an open terminal or an embedded Python interpreter. Before submission, persist immutable original-input, exact rendered-prompt, and provider-request-payload hashes; structural IDs alone cannot establish content identity. Detect changed referenced text and never blindly reuse an old submission identity for changed content. Preserve task, pair, request, run, and attempt IDs throughout.

Required offline R-driven mock-provider tests in Milestone 2 exercise JSON parsing, exit codes, stdout/stderr separation, submission, independent-process status/export, interruption, and resumption without duplicate completed judgments. Later milestones extend these tests to the operations they introduce. See the [cross-language acceptance gate](implementation-plan.md#cross-language-interoperability-acceptance-gate).

## Python interface alignment

- Implemented: `validate_run(spec, *, capabilities=None) -> ValidationReport` (structural validation)
- Implemented: `plan_run(spec, *, capabilities=None) -> RunPlan` (structural grouping only)
- `run_live(spec) -> RunSummary`
- `submit_batch(spec) -> BatchSummary`
- `resume_run(run_id) -> RunSummary`
- `collect_results(run_id) -> ResultManifest`
- Async variants of the live execution API may be exposed as `arun_live` or through a documented client; the intended future names are synchronous `run_live` and asynchronous `arun_live`; neither is implemented in Milestone 1.

R integration is CLI-first through `processx`, with the functional `rubricLLM` wrapper delivered in Milestone 6 and `reticulate` remaining optional/secondary. Interchange files allow `pairwiseLLM` to consume comparison results without embedded Python. Do not require an HTTP server for local R use. `judgmentllm serve` is introduced only in the committed late-stage Milestone 7; it exposes the same library operations and persistent runs without introducing a parallel scoring implementation. See [`http-api-spec.md`](http-api-spec.md).
