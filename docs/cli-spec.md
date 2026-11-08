# Command-line interface

Executable name: `judgmentllm` (distribution and import name planned as `judgmentllm`). CLI must call the same library functions as the Python interface, never parallel implementations.

## Implemented global commands

- `judgmentllm --help` displays the available global options and exits successfully.
- `judgmentllm --version` prints `judgmentllm VERSION` and exits successfully; the current development candidate is `0.1.0a3`.
- `judgmentllm` without arguments displays help and exits successfully.

These operations require no credentials and make no provider requests.

## Implemented Milestone 1 template commands

- `judgmentllm templates list` lists the three installed presets.
- `judgmentllm templates init --preset rubric_scores_only --output prompt.txt` copies a preset and refuses overwrites.
- `judgmentllm templates preview --config examples/run.yaml --item-id essay_001 --trait organization` previews the complete independent or grouped request containing that trait.
- `judgmentllm templates preview --config examples/pairwise.yaml --pair-uid comparison_001_reverse` previews one scheduled pair in its recorded presentation order.
- `judgmentllm templates preview --config examples/advanced.yaml --item-id essay_001` previews the one grouped advanced-template request.

Successful operations exit 0. Invalid arguments, configuration, files, selections, templates, or known capabilities exit 2. Preview prints prompt text to stdout and capability warnings to stderr; it does not require credentials or make requests. All commands reuse the documented Python functions. See [prompt guidance](prompt-guidance.md) for syntax and safeguards.

## Explicit v2 configuration

Existing `validate`, `plan`, `templates preview`, `run`, status, resume and export
commands accept explicit `schema_version: 2` configurations through the shared
Python services. Omitted version and explicit 1 remain v1; existing presets never
upgrade a configuration. V2 adds `prompt.output_order` (`together`,
`scores_first`, `explanations_first`) and `prompt.structured_output` (`auto`,
`text`, `native`). Separated layouts require rubric explanations; pairwise
permits only the default layout. Grouped requests still make one assessment call.

The current mock resolves auto to text with native unavailability recorded;
text explicitly selects textual schema instructions. Native fails preflight:
no provider-native translator or real model scoring is available. Requested
layout, exact generated contract/schema and resolved mechanism are reflected in
preview metadata and immutable request commitments. The CLI control envelope
and normalized exports remain schema version 1. Reserved reasoning, arbitrary
schema/JSON pass-through and incompatible controls are rejected.

`templates preview --json` returns one version-1 control envelope containing the
actual prompt and typed rendering metadata. Plain preview prints the same prompt
text. Preview is an explicit local content display: it requires no credentials,
contacts no provider and creates no run/submission. See the installed-package
[synthetic v2 examples](../examples/v2/README.md) for preview, all layouts,
partial results/retries, winner-only pairwise, export and base-R import.

## Phase 4A evidence boundary

Phase 4A exposes standalone Python evidence records and offline extraction only.
It adds no CLI commands or control fields and does not persist evidence during
`run` or `resume`. Proposed `usage export` / `reasoning export` remain Phase 6;
reasoning settings remain rejected by current RunSpec validation. The existing
version-1 CLI envelope and assessment export formats remain unchanged.

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

Each lifecycle command accepts `--json`. Template preview also supports the
version-1 JSON envelope; other template commands retain their text behavior. Lifecycle `--help --json` returns help text
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

`runs list` and `runs status` open existing registries read-only and validate their
schema without initializing or repairing them. Invalid existing registries fail;
missing stores retain empty-list/`not_found` behavior. Inspection creates no
JudgmentLLM artifacts or ownership records. SQLite-owned WAL/SHM coordination is
allowed so concurrent readers see committed, uncheckpointed work normally.

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

For a practical partial-run/retry/export walkthrough, run
`python examples/mock_recovery.py --output /local/fresh-example`. Each operation
uses the installed console in a separate process. For interruption of a longer
local mock run, Ctrl+C requests cleanup; preserve its returned run ID/store, query
`runs status`, then `resume` with the original configuration and mock inputs.
After hard termination, find the durable run with `runs list`; an inactive
`running` record requires recovery. Never infer that it is still executing.
Unresolved work may export zero rows; inspect `incomplete_source` and submission
certainty rather than manufacturing a score. Exit 3 indicates retained partial
work; historical failures remain after an authorized retry succeeds.

## Milestone 3 authentication and future commands

The authoritative [assessment output design](assessment-output-design.md)
defines these additions. Phase 3 implements offline authentication status.
Live verification and other proposed provider commands remain unimplemented.
Existing commands and version-1 control envelopes retain their meanings.

### Offline authentication status (Phase 3, implemented)

```bash
judgmentllm auth status
judgmentllm auth status --json
judgmentllm auth status \
  --require openai,anthropic \
  --json
judgmentllm auth status --require openai,anthropic --require gemini --json
```

The Python credential-status service and adapters share one resolver. Status
may inspect documented process-environment credential values transiently to
resolve presence, blank handling and precedence; it never prints, logs, persists
or returns those values. It opens no credential files and never constructs a
client that can contact a provider, refresh credentials or query metadata.
Statuses are `configured`, `missing`, `unknown`, `not_required`, `unsupported`.
Configured means a usable-looking source is present, without verifying validity.
Cloud identity state that cannot be established offline is `unknown`; Ollama
`not_required` describes ordinary local authentication, not daemon connectivity.

JSON reuses the version-1 control envelope. Command-specific data contains
`providers` (typed `CredentialStatus` records in stable registry order),
`required_providers` (unique provider IDs in the same registry order) and `requirements_met` (boolean). A record contains
`provider`, `auth_type`, `source`, `status` and nullable `diagnostic_code`;
identifiers and codes are nonsensitive allowlisted values, never arbitrary SDK
messages, usernames, account IDs, credential paths or secrets. Sources are
logical names such as `OPENAI_API_KEY` or `google_adc`. Missing or malformed
API keys have a null source. The human table may show expected variable names.
Rows always follow `openai`, `anthropic`, `gemini`, `vertex`, `together`, `ollama`,
`typesafe`, including when requirements select only a subset. Missing or unknown
optional credentials do not fail an ordinary query. See the [Python API](python-api.md#credential-diagnostics-phase-3-implemented)
for source precedence, blank/control-character policy and fixed diagnostic codes.

```json
{"schema_version":1,"command":"auth status","status":"ok","run_id":null,"run_status":null,"artifacts":{},"data":{"providers":[{"provider":"openai","auth_type":"api_key","source":"OPENAI_API_KEY","status":"configured","diagnostic_code":null},{"provider":"vertex","auth_type":"application_default_credentials","source":"google_adc","status":"unknown","diagnostic_code":"offline_identity_unknown"}],"required_providers":[],"requirements_met":true},"error":null}
```

Ordinary status exits 0 even if optional sources are missing, unknown or
unsupported. A required provider satisfies the check only with `configured` or
`not_required`. An unmet requirement returns the same typed data with
`requirements_met: false`, envelope `status: error`, `error.category: validation`
and exit 2. Unknown provider IDs and malformed/empty `--require` members are
usage/validation errors (exit 2); surrounding whitespace is not normalized. Repeated `--require` comma lists are unioned
and deduplicated; they do not change registry ordering. Do not add a new process
exit code. Status
creates no run, store or authentication artifacts and emits no credential
values, substrings or fingerprints. Handled interruption exits 130. JSON help
uses the existing version-1 envelope with `data.help`. Neither status command
tests authentication success, model access, SDK availability or daemon health.

### Explicit live authentication verification (proposed, not implemented)

```bash
judgmentllm auth verify --provider openai --json
```

This operation is opt-in and separate from status. Use only a documented suitable
non-generative check for the provider through the same execution resolver.
Do not claim that a check is free unless supported by provider documentation.
Use at most one suitable authenticated read, with no automatic pagination or
retries; do not hide this operation inside smoke preflight. If a model-generation
request would be required, return unsupported and direct
the user to explicitly authorized `providers smoke`. No implicit billable probe,
browser login, token refresh during offline status, or model fallback is allowed.

`data` is the typed `AuthVerification` record: requested provider, public source
identifier, operation identifier, UTC date, `verification_status` and nullable
safe `diagnostic_code`. Verification statuses are `authentication_accepted`,
`credentials_rejected`, `permission_denied`, `network_unavailable`, `rate_limited`,
`unsupported`, `inconclusive`, and `not_required`. Ollama may return `not_required`
with a separate connectivity state; daemon connectivity is not authentication
evidence. These states do not infer model access, service tiers, batch permissions
or structured-output support.

| Verification outcome | Envelope status / error category | Exit |
|---|---|---|
| Authentication accepted or not required | `ok` / null | 0 |
| Rejected credentials, permission denied, network/service unavailable, rate limited | `error` / `execution` | 1 |
| Known provider without suitable check | `error` / `unsupported_capability` | 2 |
| Unknown provider or invalid arguments | `error` / `validation` or `usage` | 2 |
| Inconclusive evidence | `partial` / `partial_completion` | 3 |

Only safe classification is displayed; no raw SDK error or response body enters
stdout/stderr. Rate/network inconclusiveness must not be labelled invalid
credentials. A successful check is authentication evidence, not qualification.

### Authorized provider smoke tests

```bash
judgmentllm providers smoke --provider openai --endpoint responses \
  --model MODEL_ID --allow-billable --max-requests 2 \
  --max-output-tokens 256 --json
```

Provider, endpoint and model are required; no default model substitution.
The command must require `--allow-billable` before any potentially billable
model request. Even local endpoints require deliberate command invocation;
remote requests never arise from status, preview or ordinary CI.
`--max-requests` defaults to 2 and permits 1–4; the complete minimum qualification
requires both rubric and forced-choice probes, so a smaller allowed probe run
cannot qualify that full feature set. `--max-output-tokens` defaults to 256 and
is a positive cap that must be supported and enforced by the chosen endpoint;
unsupported enforcement fails before submission. Initial qualification disables
automatic retries and stops when the request allowance is exhausted. Caps bound
requests/output, not a hard monetary cost. Document model-specific reasoning
budget semantics and unknown prices before an authorized run.

Optional `--reasoning-capture summary|provider_text` requires provider support,
full capture and explicit retention authorization through that option; it does
not request hidden internal reasoning. Capture/effort options obey the same
RunSpec validation as normal execution. Live fixtures use synthetic student
responses only. Qualification records include exact scope, SDK version/date,
features, outcomes, reported usage, reliable cost when available and limitations.
Credential presence, successful status/verify, or an SDK install cannot advance
`live_qualified`. JSON returns the existing envelope with bounded summary data
and durable nonsecret qualification/evidence paths; output assessment rows go
to files, never to control stdout. Standard exit categories/codes apply.

### Companion evidence exports and run controls

```bash
judgmentllm usage export --run RUN --store /local/store \
  --format csv --view r --na-string NA --json
judgmentllm reasoning export --run RUN --store /local/store \
  --format jsonl --view canonical --json
```

Both commands accept `--run` (required), `--store`, `--format jsonl|csv`
(default `jsonl`), `--view canonical|r` (default `canonical`), `--na-string`
(default `NA`), `--high-water-mark` and `--evidence-high-water-mark`.
`r` is valid only with CSV; `--na-string` applies only to that R-compatible view.
Canonical CSV retains the established `\N` null/escaping rules and nested JSON
cells; the R view uses the selected marker and rejects literal collisions.

`--high-water-mark` selects the existing result revision boundary;
`--evidence-high-water-mark` selects the new append-only evidence revision
boundary. Both are nonnegative integers. If omitted, capture both current
boundaries in one database snapshot; a supplied result boundary without an
evidence boundary uses that same transaction's current evidence boundary.
Reject unknown future cuts or combinations whose evidence excludes links needed
by selected assessment results. Return both actual cuts in a new typed
`EvidenceManifest`, separate from the unchanged assessment `ResultManifest`.
Repeated matching cuts/options converge to the same immutable export.

Usage export publishes request/observation facts plus revision audit and a
result-to-evidence bridge, including failed/no-result submissions and retries.
Reasoning export publishes only authorized retained records/references; unavailable
or redacted evidence retains availability metadata and null text/paths. Both use
bounded streaming and atomic publication, existing store discovery and safe
artifact reports. Neither contacts a provider or invents missing evidence.
Control stdout contains bounded metadata and file paths, never reasoning text.
The [Python API design](python-api.md#proposed-evidence-export-services) specifies
the corresponding shared service signatures.

Usage exports include a `ProviderObservationMetadata` version-1 companion,
with one row per observation even when usage counts are null. Return its durable
path/hash/length as nullable `EvidenceManifest.observation_file` using the existing
`ExportFile` model; absent legacy metadata stays unavailable. Canonical JSONL/CSV
and R-compatible audit views retain `requested_order` (the rubric layout enum,
null for pairwise), nullable root `observed_key_order`, `member_key_orders`
(`{array, index, trait_id|null, keys|null}`), `order_adherence`
(`adherent|mismatch|unknown`), `order_evidence`
(`raw_output_text|raw_tokens|reassembled_stream|unavailable`), and nullable safe
`unknown_reason`. CSV encodes root/member key lists as JSON cells and preserves
unknowns using its documented missing marker. Minimal capture retains only
safely observable nonsensitive metadata under the authoritative retention policy;
only allowlisted contract keys and known trait IDs enter public metadata. An
unsafe key makes its affected sequence null and adherence unknown with
`unknown_reason: unsafe_key_names`; an unknown trait ID becomes null, with stable
indices and content-free counts. No original order evidence means unknown, never
reconstructed order. Join through the existing identity bridge, without adding fields to every grouped trait row.
Control stdout contains only bounded export metadata. An order mismatch remains
diagnostic, without invalidating scores or enabling retry.

Output layout is configured through implemented schema-version-2
`prompt.output_order: together|scores_first|explanations_first` (`together`
default), independent of `execution.grouping`. Each group makes one call.
Scores-only runs reject a nondefault explanation layout. Implemented
`prompt.structured_output: auto|native|text` defaults to `auto`; selected native
schema presentation is frozen with the request; provider-native transport remains
later work. Future reasoning controls belong under typed `generation.reasoning`, separate from
assessment explanations and `capture: full|minimal`. Credentials never belong
in RunSpec. Version-1 configurations and historical normalized result schema 1
remain readable without rewriting their identities or artifacts.

## Future commands and integrations

Provider integrations, managed bulk-live, native `batch submit/status/collect/cancel`,
actual caching controls, and model catalogs remain assigned to later milestones.
The functional CLI-first `rubricLLM` wrapper arrives in Milestone 6; ordinary R
workflows need no resident Python process or HTTP server. Optional local-loopback
FastAPI and `serve` arrive in Milestone 7. No future command is implemented as
an alias for mock execution.

JSON control and diagnostic records escape non-ASCII characters, preserving exact decoded IDs and paths even through locale-limited redirected Windows streams.

### Phase 4B evidence compatibility

CLI control envelope 1 and existing assessment exports remain unchanged. New
stores internally commit safe provider evidence separately from result rows;
ordinary status/JSON/diagnostics never include provider reasoning text. Existing
nullable result token columns stay unchanged. No `usage export` or `reasoning
export` command is added before Phase 6. Historical schema-1 stores are inspected
and exported without migration or fabricated evidence.

## OpenAI Phase 5A non-generative diagnostics

`auth verify --provider openai [--json]` now performs exactly one protected
OpenAI models-list read through the shared credential resolver. It never generates
an assessment, paginates, retries or qualifies a model. Its typed
`AuthVerification` includes provider, logical source, operation, UTC `checked_at`,
`verification_status` and a fixed nullable diagnostic code. Existing version-1
control envelopes and the verification exit-code table above apply.

`providers models --provider openai [--model EXACT_MODEL_ID] [--json]` returns a
bounded model-list snapshot with `models`, requested-model visibility, completion
state and actual SDK-call count. `not_visible` means absent from a complete list;
a failed or incomplete read leaves missing-model visibility unknown. Account and
owner fields are excluded. Successful authentication/listing does not establish
endpoint, parameter, native schema or assessment support. Other providers remain
unsupported by these commands in Phase 5A. Offline `auth status` is unchanged.

The earlier general `providers smoke` command remains deferred. Phase 5A uses a
separate reproducible qualification harness requiring explicit source, endpoint,
model, mechanism, synthetic data, consent, evidence location and shared request,
token and spending limits. It never runs from ordinary CI or implicitly after CI.


Phase 5A assessment-output diagnostics are available through the separate
read-only Python inspection service documented in `python-api.md`; this adds no
CLI command or control-envelope field. Ordinary status and result exports never
display protected unvalidated assessment text. The qualification harness reports
safe diagnostic metadata, keeps historical plan reports readable, and requires a
fresh source-bound version-2 plan for new execution. Positively terminal invalid
assessments can advance within that fixed matrix; uncertainty cannot.
