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

## Implemented Milestone 2 lifecycle commands

The installed `judgmentllm` executable calls the same public services as Python.
Execution currently supports only `provider: mock`, `endpoint: synthetic`, and
`execution.mode: live`; it never substitutes the mock for another provider.
Validation and planning remain offline and can describe other configurations
with capability unknowns retained. Real adapters arrive in later milestones.

| Command | Options and behavior |
|---|---|
| `validate --config FILE` | Complete input/source validation; optional `--exhaustive-rendering` renders every actual request. |
| `plan --config FILE` | Exact logical task/request counts and grouping; token and price unknowns stay null. |
| `run --config FILE` | Blocking execution in this process; optional `--store`. |
| `resume --run RUN` | Blocking conservative recovery; optional `--store`, `--config`, `--use-frozen-snapshot`, and explicit retry controls below. |
| `runs list` | Bounded local listing; optional `--store`, `--limit` (default 100, range 1–1000), and opaque `--cursor`. |
| `runs status --run RUN` | Durable inspection without provider calls or original assessment sources; optional `--store`. |
| `results export --run RUN` | Immutable all-attempt export; optional `--store`, `--format jsonl\|csv`, `--view canonical\|r`, `--na-string` (default `NA`), and `--high-water-mark`. |

Each lifecycle command accepts `--json`. Ordinary help, version, and template
commands retain their text behavior. Lifecycle `--help --json` returns help text
inside a control response without executing the operation. All operations are
noninteractive; no authentication browser or confirmation prompt is started.

### Store discovery and durable identity

Store precedence is explicit `--store`, then `JUDGMENTLLM_RUN_HOME`, then the
available configuration's resolved `output_path`, then `./run-data`.
Run references accept the original run ID or an absolute run-directory path.
A conflicting explicit store and directory reference fails. Returned artifact
paths are absolute; run directories use the established hashed ID mapping and
must not be constructed by joining an original ID to `runs/`.

Keep runtime stores on a local filesystem outside cloud-synchronized directories.
A later process can inspect, resume, retry, or export using the same durable run.
Neither `run` nor `resume` claims that execution continues after its process exits.

`resume --config` supplies the original compatible specification when needed;
it cannot replace the frozen configuration. Minimal Python-origin runs require
the original specification. Minimal capture always requires unchanged sources.
Full capture also requires unchanged originals by default;
`--use-frozen-snapshot` explicitly authorizes verified original snapshots for
that operation. Corrupt required snapshots still fail.

### Explicit subset retries

Retry controls belong on `resume`:

```bash
judgmentllm resume --run RUN_ID --store /local/store \
  --retry-task TASK_ID --retry-id retry-001 --json
```

Repeat `--retry-task` to select multiple tasks from **one original request**.
`--retry-id` is required with a selection. `--retry-id` and `--allow-completed`
are invalid without a selection. Selected previously completed tasks additionally
require `--allow-completed`. Obtain failed task IDs from an explicit result export.

The operation commits its exact membership and permission. Identical operation-ID
reuse resumes that original attempt; conflicting reuse fails. Successful siblings
are not included implicitly. Default bounded retry policy remains three physical
attempts per original request, including the initial attempt; persistent automatic
backoff applies only to eligible transient definitive nonacceptance. Earlier
unsuccessful attempts remain in audit exports after logical completion.

Unknown acceptance, unavailable accepted observations, invalid/refused completions,
and interruption alone never authorize automatic resubmission. Authoritative
nonacceptance discovered during recovery does not manufacture a prior normalized
outcome for explicit subset retry.

### Mock execution inputs

`run` and `resume` accept these synthetic-service options:

- `--mock-ledger FILE`: original independent acceptance ledger; default
  `<store>/mock-service.sqlite3`.
- `--mock-fixture FILE`: strict version-1 JSON with `scenario` and optional
  `scenarios` keyed by original request UID, using documented `MockScenario` fields.
- `--mock-no-retrieval`: make authoritative observation retrieval unavailable.
- `--mock-no-reconciliation`: make authoritative submission reconciliation unavailable.

These controls are not `RunSpec` fields or arbitrary provider request JSON.
Custom ledger and fixture inputs must be supplied again after process restart.
Fixture text is not copied into runtime storage. Resume requires the original
ledger; replacement service identity and changed unfinished payload commitments
fail closed. A new authorized retry may use a changed synthetic outcome after
prior observations commit. A missing minimal-capture response is recovered only
from an established accepted observation with authoritative retrieval, never from
a deterministic recipe or matching hashes alone.

## Version-1 JSON control responses

JSON mode writes exactly one object followed by a newline to stdout, including
handled parsing and execution errors. Assessment rows go to explicit exports,
never to control stdout.

| Field | Contract |
|---|---|
| `schema_version` | Integer `1`, separate from assessment and persistence versions. |
| `command` | Canonical command, e.g. `run`, `runs status`, or `results export`; unresolved invalid commands use `unknown`. |
| `status` | Command outcome `ok`, `partial`, or `error`. |
| `run_id` | Original durable run ID, or null. |
| `run_status` | Durable `created/submitted/running/completed/partial/failed/cancelled`, or null. |
| `artifacts` | Absolute durable paths or explicit nulls; `{}` for commands without artifacts. |
| `data` | Small typed command-specific metadata, described below. |
| `error` | Null, or `{category, message, retryable}` with safe provider-neutral information. |

Artifact-bearing responses use `store`, `run_directory`, `registry`, `manifest`,
`results_jsonl`, `results_csv`, `results_r_csv`, and `export`. Result paths stay
null until an export has been published. `run` and `resume` do not implicitly
serialize the complete result history. No temporary staging path is returned.

Command `data` is the corresponding public model serialized with the top-level
run ID/state/artifacts removed:

- Validation: `valid`, input/source counts, bounded `issues`, `total_issues`,
  and `truncated`. Failed validation retains null unknown counts.
- Planning: `task_kind`, response/pair/source counts, exact `logical_tasks` and
  `requests`, `trait_groups`, scoped `capabilities`, cache policy/report,
  nullable token/cost estimates, and pricing applicability.
- Execution/resumption/status: configuration scope, capture, timestamps,
  planned/materialized/current logical task counts, request/attempt/result and
  unsuccessful historical counts, retry count, submission certainty,
  executor/recovery state, and nullable usage.
- Listing: bounded `runs` and `next_cursor`; each run is a typed status summary.
- Export: `export_id`, format/view, row count, `result_high_water_mark`,
  `incomplete_source`, and `files` containing absolute path, SHA-256, and byte length.

Successful validation example:

```json
{"schema_version":1,"command":"validate","status":"ok","run_id":null,"run_status":null,"artifacts":{},"data":{"valid":true,"response_count":2,"pair_count":0,"source_count":2,"issues":[],"total_issues":0,"truncated":false},"error":null}
```

Unknown-run example (exit 2):

```json
{"schema_version":1,"command":"runs status","status":"error","run_id":null,"run_status":null,"artifacts":{},"data":{},"error":{"category":"not_found","message":"durable run or resource was not found","retryable":false}}
```

Partial processing returns `status: partial`, `error.category: partial_completion`,
and retained durable state/counts. Missing evidence remains unresolved; it does
not become an invalid judgment or an invented result row. A successful retry can
return `ok` while historical unsuccessful-result counts remain positive.

### Stderr JSONL and privacy

In JSON mode every diagnostic/progress line on stderr is a separate JSON object
with integer `schema_version: 1`, `event` (`diagnostic` or `progress`), `category`,
safe `message`, and nullable `run_id`. Coarse progress categories are `started`,
`prepared`, and `finished`. Emitted library warnings are translated to bounded safe
`validation_warning` or `runtime_warning` diagnostics; warning-as-error filters
remain enforced. Command failures appear both as a safe diagnostic and
in the final stdout envelope. Empty stderr is valid for quiet successful queries.

No prompt, explanation, fixture text, raw response, provider body, credential,
traceback, or arbitrary exception/input representation is printed. Human mode
shows the same safe outcome, counts, and paths as readable text.

### Exit codes and durable state

| Code | Meaning |
|---|---|
| `0` | Command succeeded; inspect `run_status` and counts separately. |
| `1` | Execution, integrity/storage, or internal failure. |
| `2` | Usage, validation, unsupported capability/implementation, unknown resource, or invalid retry authorization. |
| `3` | Processing retained current unsuccessful outcomes or unresolved/incomplete work. |
| `130` | Handled interruption after cleanup, with available durable identity and artifacts. |

Error categories are `usage`, `validation`, `unsupported_capability`, `not_found`,
`execution`, `partial_completion`, `interrupted`, and `internal`. Retryability
means an automatic retry is appropriate; manual selection remains explicit.

A successful query of a failed run is exit 0 with `status: ok` and
`run_status: failed`. Exporting an incomplete run can likewise succeed and reports
`incomplete_source: true`. Cancelled runs can recover earlier accepted observations
while remaining cancelled; resume does not restart their scheduling. Local task
cancellation is not evidence of confirmed provider cancellation.

Hard process termination cannot guarantee a control envelope or exit 130. Later
processes use durable state and authoritative recovery. Installed-CLI acceptance
captures stdout, stderr, and exit status independently, including on Windows.

## Future commands and integrations

Provider integrations, managed bulk-live, native `batch submit/status/collect/cancel`,
actual caching controls, and model catalogs remain assigned to later milestones.
The functional CLI-first `rubricLLM` wrapper arrives in Milestone 6; ordinary R
workflows need no resident Python process or HTTP server. Optional local-loopback
FastAPI and `serve` arrive in Milestone 7. No future command is implemented as
an alias for mock execution.
