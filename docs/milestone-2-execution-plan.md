# JudgmentLLM Milestone 2 — Final Revised Implementation Plan

## A. Architecture and preserved contracts

Deliver the approved local, provider-neutral execution foundation using a deterministic mock provider, SQLite persistence, immutable manifests, incremental request preparation, bounded asynchronous execution, and shared Python/CLI services.

This revision incorporates the six requested clarifications and the generation requirement at the start of Subphase 2. The approved architecture, capture policies, conservative submission recovery, and six implementation subphases remain intact.

At the earlier plan-only revision, the inspected checkout was clean `main` at `d1cb6e86a336bf0601de05a470185f6472f34b35`. No files were modified, tests executed, commits created, or provider requests made at that stage; this is historical context rather than current implementation status.

### Existing implementation remains authoritative

Preserve:

- Structural task/request IDs and pair scheduling.
- Structural-only `validate_run()` and `plan_run()`.
- `render_prompt()` as the sole prompt renderer.
- Existing strict output validators and normalization functions.
- Normalized result schema version 1 and established export columns.
- All-attempts canonical and R-compatible exports.
- Existing template CLI behavior.

Add complete-input validation, execution estimation, persistence, and lifecycle services without duplicating these implementations.

The generation addition preserves existing request IDs when no controls are
requested. Explicit controls enter the request context fingerprint; item, pair
and task identities remain stable in either case.

### Components

| Component | Responsibility |
|---|---|
| Execution models | Typed manifests, policies, states, summaries, estimates, and controls. |
| Source preparation/index | Consistent source freezing, hashing, indexing, and response lookup. |
| Artifact store | Immutable content-addressed files, integrity checks, receipts, and export publication. |
| SQLite repository | Transactions, ownership, checkpoints, attempts, observations, results, and audit events. |
| Provider protocol | Capabilities, exact payload preparation, submission, reconciliation, and parsing. |
| Mock adapter | Deterministic judgments, fault scenarios, and independent acceptance ledger. |
| Executor | Bounded scheduling, concurrency, backpressure, retries, cancellation, and recovery. |
| Result processing | Task matching and established validation/normalization. |
| Shared services | Public Python operations called directly by the CLI. |
| CLI controls | Arguments, versioned envelopes, diagnostics, and exit codes. |

Provider adapters do not own persistence or scoring interpretation. Export does not submit requests or generate missing observations.

## B. Persistence schema and durable outcome rules

### Store and database

Retain one registry per store:

```text
<store>/
  registry.sqlite3
  runs/<run_id>/
    executor.lock
    manifest.json
    objects/<hash-prefix>/<sha256>
    receipts/
    exports/<export_id>/
```

Use standard-library `sqlite3`, foreign keys, WAL, `synchronous=FULL`, bounded busy timeout, and short explicit write transactions. Never hold a transaction across provider calls, artifact I/O, rendering, backoff, or export.

Allow one executor per run using a lifetime OS advisory lock plus SQLite owner token, PID, host, and heartbeat. Heartbeat expiry alone never permits takeover. Different runs may execute concurrently; status/export processes remain independent.

Execution storage remains single-machine and local-filesystem based.

### Schema

All public IDs remain strings. Run-scoped foreign keys prevent cross-run associations. Identity fields are immutable; significant state changes append an event in the same transaction.

| Table | Keys and indexes | Contents and mutation policy |
|---|---|---|
| `schema_migrations` | PK version | Name, checksum, applied timestamp; append-only. |
| `runs` | PK run ID; state/time and pagination indexes | Frozen manifest identity and configuration references; mutable lifecycle, counters, scheduling checkpoint, and ownership. |
| `sources` | PK run/source; unique role/ordinal | Original location, raw digest/length, canonical identity, format, optional snapshot reference, source-generation information; immutable after sealing. |
| `input_records` | PK run/kind/record ID; unique source ordinal; ordered traversal indexes | Canonical record hash, source locator, permitted structural fields, optional snapshot locator; immutable. |
| `tasks` | PK run/task; state and item/pair indexes | Immutable logical identity and scheduling ordinal; mutable aggregate execution state. |
| `requests` | PK run/request; unique scheduling ordinal; state/order index | Immutable original request identity and dependency digest; mutable lifecycle. |
| `request_tasks` | PK run/request/task; unique membership position; reverse task index | Original ordered membership; immutable. |
| `attempts` | PK run/attempt; unique request attempt number; state/eligibility index | Exact prompt/payload commitments, membership digest, retry authorization, lifecycle, usage, and definitive failure evidence. Prepared identity is immutable. |
| `attempt_tasks` | PK run/attempt/task; unique position; FKs to attempt and original request membership | Exact submitted subset; immutable. |
| `submissions` | PK submission ID; unique correlation key; unique attempt or batch target; state/remote-ID indexes | Payload identity, prepared intent, submission boundary, acknowledgment, remote identity, and reconciliation evidence. |
| `observations` | PK observation ID; unique submission/observation key; attempt index | Actual provider response, refusal, or authoritative provider terminal-outcome evidence; optional raw artifact. Evidence immutable. |
| `results` | PK result sequence; unique run/task/attempt; run/sequence and task-history indexes | Fully validated normalized successful **and unsuccessful** task outcomes; append-only. |
| `batches` | PK run/batch; state index | Reserved future batch preparation, remote metadata, and collection state. |
| `batch_members` | PK run/batch/attempt; unique position and attempt membership | Reserved immutable batch membership. |
| `artifacts` | PK run/hash; retention/kind index | Byte identity, length, media type, location or hash-only designation; immutable. |
| `retry_operations` | PK run/operation ID | Frozen authorization, selected task/source-attempt membership, completed-task permission, and progress. |
| `exports` | PK run/export; creation-time index | Format/view, result high-water mark, counts, files/hashes, publication state; published records immutable. |
| `events` | PK event sequence; run/order and entity indexes | Safe state transitions, failure classifications, ownership, authorizations, and recovery events; append-only. |

### Results include unsuccessful outcomes

`results` stores validated `RubricResult` or `PairwiseResult` records with the existing statuses:

```text
complete, invalid, refused, errored, cancelled
```

For a definite grouped response, persist independently valid members and explicit unsuccessful records for invalid, duplicate, or missing expected members. Every record retains the original run/request/task IDs and the relevant attempt ID.

Missing or invalid scoring output receives null scoring fields. Unsuccessful records never claim a primary score or score kind. Preserve applicable coding, orientation, provenance, and independently valid audit evidence according to existing schema rules; never invent a winner or category.

Canonical JSONL, canonical CSV, and R-compatible CSV export these records alongside successful outcomes without status filtering or attempt selection.

### Distinguish evidence sources

| Established outcome | Result representation |
|---|---|
| Valid provider judgment | `complete`, linked to provider observation. |
| Provider-generated refusal | `refused`, `error_kind=provider_refusal`. |
| Received malformed output | `invalid`, `error_kind=invalid_output`. |
| Missing member of a definite grouped response | `invalid`, `error_kind=missing_output`. |
| Duplicate assignment | `invalid`, `error_kind=duplicate_output`. |
| Authoritative provider error response/outcome | `errored`, `error_kind=provider_error`. |
| Submission operation invoked and conclusively failed without acceptance or provider response | `errored`, `error_kind=transport_not_submitted`, linked to durable submission-failure evidence. |
| Confirmed cancellation of an actual attempt | Applicable `cancelled` record, linked to established cancellation evidence. |

Make `results.observation_id` nullable for definitive submission failures without a provider response. Such records must instead reference durable attempt/submission failure evidence. Enforce this alternative through repository validation and database guards.

Do not create provider-observation records for local transport exceptions. Record those as submission/attempt failure evidence.

Do not create normalized results for:

- Preflight or rendering failures.
- Merely planned or claimed work.
- Cancellation before a submission operation was attempted.
- Uncertain acceptance.
- A lost response whose actual outcome cannot be recovered.

Those conditions remain visible through execution state, counts, and audit events.

### Transactions and immutability

Preserve these boundaries:

1. Chunked ingestion/index staging.
2. Verified source/manifest sealing before any submission.
3. Task/request creation and scheduling checkpoint.
4. Exact attempt preparation, membership, artifacts, and submission intent.
5. Claim under owner token.
6. Durable `submitting` transition immediately before the adapter boundary.
7. Acknowledgment/observation evidence.
8. Atomic grouped result insertion, task/request updates, counters, and events.
9. Immutable export publication.

Write artifacts before committing their references. Flush, verify hashes, and atomically publish. Verified receipt sidecars permit recovery of identifiable orphan captures.

Never overwrite results, prior exports, or frozen identities. Repeated identical delivery records a delivery event without inserting another task/attempt result. Conflicting content under an existing observation identity becomes an error.

Use database schema version 1 and checksummed migrations. Refuse unsupported newer schemas. Future migration requires exclusive maintenance and backup.

## C. State machines and crash recovery

### States

Keep the approved public run vocabulary:

```text
created → submitted → running → completed | partial | failed | cancelled
```

`submitted` means the local manifest and executable intent are committed; it does not assert remote acceptance.

Requests retain:

```text
planned → prepared → active → completed | partial | failed | blocked | cancelled
```

Tasks retain:

```text
pending → active → complete | invalid | refused | errored | cancelled | blocked
```

Attempts retain preparation, claim, submission, acknowledgment, response-evidence, and normalized stages. Submission certainty remains independently recorded:

```text
prepared
submitting
acknowledged
confirmed_not_submitted
uncertain
resolved
```

A request may be acknowledged while its observation remains unavailable. Record this as observation recovery required, retaining acknowledgment evidence rather than falsely describing acceptance as unknown.

Status services may repair stale local ownership/state only after acquiring the executor lock. They make no provider calls and never report work as continuing without an executor.

### General recovery table

| Failure window | Recovery |
|---|---|
| Before claim | Claim prepared work safely. |
| Claimed, before committed submission boundary | Reclaim after ownership recovery; use the existing preparation. |
| `submitting` committed, call may or may not have happened | Reconcile; do not automatically submit again. |
| Accepted, acknowledgment not persisted | Recover through authoritative reconciliation. |
| Acknowledged, response unavailable | Retrieve the same submission’s outcome or remain blocked. |
| Full-capture raw receipt published, observation transaction missing | Verify and attach the receipt; otherwise reconcile. |
| Full-capture observation saved, normalization missing | Normalize locally without another submission. |
| During grouped normalization/result transaction | Roll back the entire group; recover the same observation and reprocess. |
| Results committed, process dies before reporting completion | Read existing results and repair summaries; never add another attempt. |
| During export | Preserve prior exports; discard/rebuild incomplete publication. |
| During cancellation | Preserve results; release safe pre-submission work; reconcile in-flight work. |
| During retry authorization/preparation | Resume the committed operation, or recreate after rollback using the same operation ID. |
| During automatic backoff | Preserve retry count and next eligibility. |

Remote exactly-once execution remains conditional on actual provider support. Local uniqueness and conservative recovery do not create that guarantee.

### Exact minimal-capture response recovery

Under minimal capture, response receipt, parsing, validation, and normalization occur in memory. Raw output and rendered prompts are never written to recovery files, temporary tables, or logs.

| Window | Durable state | Required recovery |
|---|---|---|
| Response received, before any receipt metadata is committed | Submission may remain `submitting` or acknowledged | Reconcile/retrieve the same observation; otherwise block. |
| Receipt identity/hash committed, response only in memory | Receipt proves limited facts, but does not contain the observation | Authoritative retrieval required. A hash cannot reconstruct output. |
| During parsing | No committed normalized results | Retrieve that observation and parse again, or block. |
| During member validation | No committed group results | Retrieve and validate again, or block. |
| Normalized records prepared in memory | No committed group results | Retrieve and normalize again, or block. |
| Result inserts/state updates underway, before commit | Transaction rolls back | Recover the same observation; do not submit again. |
| Group transaction committed | All successful/unsuccessful member records and corresponding state updates exist | Use committed records; no retrieval or resubmission is needed. |
| Commit outcome uncertain to the client | Database is authoritative after reopening | Check the unique result keys and transaction state before further action. |

If retrieval is unavailable, retain the submission as unresolved, set `recovery_required`, and block automatic resubmission. Do not turn missing evidence into `invalid`, `refused`, or `errored` model output.

**Request reconstruction and response recovery are separate operations:**

- Matching sources and serializer versions may reconstruct the submitted request.
- They do not establish what a real provider returned.
- A payload hash, local seed, or deterministic algorithm is not evidence of a real provider observation.

Mock retrieval requires a durable acceptance-ledger record bound to the submission identity, payload hash, and accepted observation recipe. The recipe alone is insufficient. Retrieving that accepted synthetic observation must leave the acceptance count unchanged.

## D. Source freezing, hashing, and privacy

### Identity rules

Preserve structural IDs and separate content identities for original sources, canonical records, resolved pair texts, assessment configuration, templates, exact prompts, exact payloads, observations, and normalized records.

Continue versioned canonical JSON: validated deep snapshot, sorted object keys, compact separators, UTF-8, preserved arrays/defaults/nulls, no trimming or Unicode/newline normalization, and no nonfinite numbers.

Exact artifact hashes apply to original bytes, with byte length and media type recorded. Sidecar binary values use declared base64; hashes refer to decoded bytes.

Pairwise dependencies include hashes of both referenced responses, so changing an essay under its existing ID cannot silently reuse frozen content identity.

### Consistent freezing protocol

Source metadata is a change detector, not a substitute for content hashes.

Track descriptor/path identity and available generation metadata, including file identity, size, modification time, and change time. Detect replacement, disappearance, truncation, and in-place edits.

For full capture:

1. Open the original in binary mode and record its initial identity/signature.
2. Stream-copy and hash its exact bytes to private staging.
3. Check descriptor and path identity/signatures after copying.
4. Independently reread/hash the original and require matching digest/length with stable identity/signatures.
5. Verify the staged snapshot digest.
6. Parse and index **the staged snapshot**, not a separately reopened original.
7. Verify indexed canonical records and locators against that snapshot.
8. Before sealing, recheck all discovered sources against their frozen identities, including earlier sources processed before later files.
9. Publish the verified snapshot and manifest only after all checks pass.

Configuration, templates, rubric, criteria, exemplars, responses, and pairs participate in source tracking. The configuration used for source discovery must itself match the captured configuration.

For minimal capture:

1. Hash and index the same input byte stream without persisting its text.
2. Verify stable descriptor/path identity before and after the scan.
3. Independently reread/hash the source and require a matching digest/length.
4. Recheck the complete source set before sealing.
5. On each run/resume session, verify complete required source identities before using stored locators.
6. Keep verified source handles and check their generation/path signatures around indexed reads.
7. Parse each selected record through shared strict reader logic and verify its canonical hash and ID before rendering.
8. If a source generation changes, stop affected execution and require complete source revalidation.

Stored offsets alone never authorize use of a record.

For CSV, locators span complete logical records, including quoted multiline fields. Track raw byte boundaries and retain validated header/format context. Do not treat physical line numbers or text-stream position cookies as byte offsets.

For JSONL, locators identify complete encoded records. Preserve established validation and line diagnostics.

If consistent freezing cannot be established, reject before submission. Do not automatically accept a new source generation within the same staging run. Full snapshots guarantee that submitted content matches the published snapshot and manifest; no filesystem-wide atomic snapshot is claimed.

### Manifest and incremental preparation

Before first submission, seal the source/configuration commitments, grouping, coding, exemplars/order, templates, provider/model/mode/tier/cache settings, explicitly requested generation controls, policies, pairing/presentation seed, software versions, capabilities, and capture policy.

Minimal capture represents text-bearing configuration through hashes and source references rather than retaining its text.

Prepare requests incrementally. Commit exact dependencies, submitted membership, prompt/payload hashes, artifacts where permitted, and submission intent before every submission.

Previously prepared requests must match their recorded commitments. Unprepared work must use compatible frozen renderer/serializer behavior.

### Generation configuration foundation at the start of Subphase 2

Introduce optional strict `RunSpec.generation` with `temperature`, `top_p` and
`max_output_tokens`. Keep these separate from execution controls and top-level
pairing/presentation `seed`. Omitted/null/empty configuration requests no
generation controls; omit it from serialization and retain explicit zero values
when supplied. Never invent provider defaults or silently adjust values.

Establish typed capability evidence for the exact provider/model/endpoint,
including per-control support/bounds and incompatible combinations. Known
incompatibilities fail local preflight. Unknown support is reported explicitly
and obeys `strict_capabilities`; it never establishes successful qualification.
No-generation configurations retain their existing structural IDs,
serialization and diagnostic behavior, without new generation warnings.

Freeze requested generation identity independently of capability evidence. The
complete immutable manifest must include that commitment; every actual prepared
body must include requested controls so its exact-byte identity reflects them.
Changing a requested value is incompatible with resume. Current capability
restrictions must still be revalidated before submission/resume; newly known
incompatibilities block continuation without rewriting frozen evidence.

Implement only configuration, validation and generation-commitment integration
in this initial foundation. Generation-only retained fixtures are not complete
executable manifests: source freezing, complete manifest publication and full
preflight remain required before any submission. Do not add cloud request
builders or fake provider bodies for this work.

Document future typed fields `top_k`, ordered `stop_sequences`,
`repetition_penalty`, `frequency_penalty`, `presence_penalty`, `generation.seed`
and reasoning/thinking controls. Provider-specific extensions
must have documented adapter-owned schemas and validation; unsupported keys
remain errors and unrestricted provider JSON remains forbidden.

Full-capture resume blocks changed/missing originals unless `--use-frozen-snapshot` authorizes verified snapshots. Minimal capture requires matching originals. Corrupted required snapshots always block recovery.

### Minimal-capture privacy

Minimal capture avoids retaining:

- Source-text snapshots.
- Rendered prompts.
- Serialized provider request bodies.
- Raw provider responses.
- Unnecessary response text in metadata, failures, diagnostics, events, or mock ledgers.

It **does retain authorized normalized results**, including explanations. Explanations may quote sensitive student text. Minimal capture is not anonymization or complete sensitive-data removal.

Mock ledgers retain acceptance identity and nonsensitive recipes/references, not copied request or response text. Minimal mode must also avoid sensitive temporary database rows, crash-recovery sidecars, and exception dumps.

## E. Python and CLI contracts

Retain the approved services:

```python
validate_inputs(spec_or_path, *, capabilities=None) -> InputValidationReport
estimate_run(spec_or_path, *, capabilities=None) -> ExecutionPlan

run_live(spec_or_path, *, store=None, progress=None) -> RunSummary
arun_live(spec_or_path, *, store=None, progress=None) -> RunSummary

resume_run(
    run_id, *, store=None, spec=None,
    use_frozen_snapshot=False, retry=None, progress=None,
) -> RunSummary

aresume_run(...) -> RunSummary
list_runs(*, store=None, limit=100, cursor=None) -> RunPage
get_run(run_id, *, store=None) -> RunStatus

export_run_results(
    run_id, *, store=None,
    format="jsonl", view="canonical", na_string="NA",
) -> ResultManifest
```

Use typed summaries/errors. Synchronous wrappers reject invocation inside an active event loop with guidance to use the async equivalent.

Retain:

```text
validate --config ...
plan --config ...
run --config ...
resume --run ...
runs list
runs status --run ...
results export --run ...
```

`run` and `resume` block while executing; neither implies continued work after process exit.

Store discovery remains explicit `--store`, then `JUDGMENTLLM_RUN_HOME`, then configuration `output_path`, otherwise `./run-data`. Accept run IDs or absolute run-directory paths. Return absolute durable artifact paths.

Keep version-1 control fields:

```text
schema_version, command, status, run_id, run_status,
artifacts, data, error
```

Command data remains:

| Command | Data |
|---|---|
| Validate | Validity, source/input counts, bounded issues and totals. |
| Plan | Exact task/request counts, grouping, estimates/unknowns, capabilities/cache report. |
| Run/resume/status | Logical and attempt counts, submission certainty, unresolved work, executor/recovery state, usage, paths, timestamps, applicable retry operation. |
| Runs list | Bounded summaries and opaque pagination cursor. |
| Export | Export identity, format/view, row count, high-water mark, incomplete-source flag, files/hashes. |

Maintain exactly one stdout JSON envelope in JSON mode, structured stderr JSONL, safe messages, noninteractive behavior, and exits `0/1/2/3/130`.

Processing failures retained as normalized outcomes yield partial processing semantics; querying or exporting an incomplete run can succeed while reporting its durable state. Hard termination cannot emit an envelope.

Retain canonical JSONL/CSV and R CSV export options, immutable export publication, and consistent result high-water marks. Never fabricate rows for pending or unresolved tasks.

## F. Execution, grouped processing, and retries

### Bounded execution

Index sources before submission, then stream scheduling from indexed records. Use a fixed worker pool, request queues bounded to twice concurrency, bounded artifact/result queues, and short repository operations outside the event loop.

Reuse planning internals and existing validators. Avoid a response-by-trait product, one asyncio task per judgment, and duplicate in-memory input-ID inventories when SQLite already validates uniqueness.

### Mock and provider boundary

Retain the narrow capabilities/prepare/submit/reconcile/parse protocol and exact serialized-body boundary.

The mock supports deterministic rubric/pairwise judgments, grouped outputs, explanations, ties where permitted, synthetic probability fixtures, controlled delays/errors, and reordered completion.

The independent acceptance ledger distinguishes accepted submissions from local preparations and records acceptance counts. Recovery tests inspect it independently of normalized results.

Real adapter implementation and SDK qualification remain deferred.

### Grouped processing

For a definite received response:

1. Validate the recognizable outer structure.
2. Match members by trait identity.
3. Validate unique attributable members using established trait-level rules.
4. Normalize valid members through existing normalizers.
5. Create validated unsuccessful records for missing, malformed, or duplicate members.
6. Retain safe request diagnostics for unknown/unattributable entries.
7. Commit all member results and corresponding states atomically.

Keep the strict public whole-group validator unchanged. Successful siblings remain complete; missing members do not disappear from audit output.

Attach provenance by validated reconstruction. Keep authoritative grouped usage on attempts rather than duplicating token totals across task rows.

### Subset retry identities

Original `request_tasks` membership and logical `request_uid` remain immutable.

Each physical retry receives:

- A distinct attempt ID.
- Immutable exact `attempt_tasks` subset.
- A fresh preparation record and payload-hash commitment.
- A unique submission/correlation identity.
- A link to its explicit retry authorization and prior outcomes.

Compute the hash of the actual new body. Subset retries change the submitted body and its hash; byte-identical general reattempts may legitimately have identical content hashes. Distinct execution identity comes from attempt/submission IDs, not artificially altered hashing.

A failed-member retry never includes successful siblings unless expressly authorized.

Retain `--retry-task`, required `--retry-id`, and explicit `--allow-completed`. Freeze selection under the executor lock. Reusing an operation ID resumes the same operation; conflicting selection is rejected.

Interrupted creation is atomic: either authorization/selection is committed or it is absent. Committed preparation survives restart without another retry allocation.

### Automatic policy

Preserve default three submission attempts, exponential backoff starting at one second and capped at thirty seconds, jitter/retry-after support, and persisted eligibility.

Automatic retry requires transient failure **and established nonacceptance**. Invalid/refused completions and unresolved submissions do not qualify.

Retain optional timeout and request/token-rate controls. Reject unenforceable token-rate settings when counts are unknown.

Cancellation stops scheduling, drains durable writes, preserves results, and conservatively handles in-flight work.

### Estimates and capabilities

Preserve exact streamed task/request counts, explicit unknown token/pricing values, separate estimated/observed usage, and request-level usage aggregation.

Mock billing is inapplicable; cloud prices and cache savings are not invented.

Keep caching behavior: `off` disables library controls, mock `auto` reports uncached operation, and mock `explicit` fails as unsupported.

Retain additive capability interfaces and future budget-policy boundaries without accepting an unenforced budget setting.

Generation capability facts remain scoped to provider/model/endpoint and
separate from requested configuration identity. Revalidate newly available
restrictions on submission/resume; a previously unknown setting must not bypass
a newly known incompatibility. Real parameter mappings remain Milestone 3.

## G. Acceptance tests and resource qualification

### Generation foundation tests

Before continuing Subphase 2 source preparation, add offline tests for:

- Strict schemas, finite numeric boundaries, positive output limits, unknown
  keys, and rejection of boolean/string coercions.
- Sparse YAML/JSON serialization, explicit zeros, absent/null/empty equivalence,
  and unchanged Milestone 1 serialization, IDs and strict diagnostics when no
  generation controls are requested.
- Model/endpoint-scoped supported, unsupported and unknown controls; bounds,
  incompatible combinations and strict-mode unknown failures.
- Every requested setting changing the generation commitment, immutable retained
  commitment round trips, and changed-setting resume rejection.
- Capability evidence changing independently of requested settings identity;
  current restrictions still blocking newly known incompatibilities.
- Python-generated generation provenance consumed by required offline
  R/jsonlite assertions, preserving the existing export baseline.

Generation-only fixtures prove this foundation only. They do not bypass source
freezing, full preflight or complete-manifest requirements. Exact cloud payload
mapping tests belong to Milestone 3, native applicability to Milestone 4, and
live/batch parity to Milestone 5.

### Persistence, recovery, and retry tests

Add explicit tests proving:

- A grouped response containing complete, invalid, and missing members produces three appropriate immutable result records.
- Those records survive process restart and canonical/R export.
- Confirmed failed submissions remain distinguishable from refusals and invalid outputs.
- Planned/claimed/uncertain work creates no fabricated results.
- Every minimal-capture precommit response-loss window retrieves the same observation or remains blocked.
- Mock acceptance count remains one during same-observation recovery.
- Disabling authoritative retrieval blocks recovery despite matching payload hashes and deterministic recipes.
- Postcommit restart preserves exact prior outcomes without another acceptance.
- A failed-member retry preserves parent request identity, submits only that member, and retains the previous unsuccessful observation.
- Crashes before/after retry authorization, preparation, claim, and submission do not duplicate authorization or successful siblings.

### Source-freezing tests

For CSV, multiline CSV, and JSONL, inject:

- In-place edits.
- Same-length edits.
- Truncation/appending.
- File replacement or disappearance.
- Changes during copying, hashing, parsing/indexing, and final source-set verification.
- Changed records at stored offsets.
- Changed CSV headers or multiline record boundaries.
- Corrupted snapshots and mismatched locators.

Require rejection before first submission when freezing fails; acceptance count must be zero. For post-seal minimal-source changes, stop affected execution before changed record content is submitted.

Verify full captures parse/index exactly the bytes whose identity the manifest records.

### Privacy tests

Use separate synthetic sentinels in source text, raw-only provider output, payload-only material, and an authorized normalized explanation.

Inspect decoded database content, WAL where present, manifests, receipts, ledgers, artifacts, exports, stdout, and stderr.

Assert:

- Unreturned source/raw/payload sentinels do not persist under minimal capture.
- Failure/interruption paths do not leak them.
- An explanation sentinel persists only in authorized normalized-result storage/export representations.
- Known credential sentinels never persist.
- Full capture retains permitted exact assessment artifacts without leaking them to diagnostics.

### Required R gate

Preserve the strict Milestone 1 test and required Ubuntu/Python 3.11 setup.

Extend the installed-CLI R lifecycle test using base R and jsonlite to verify:

- Independent-process execution/status/interruption/resume/export.
- Single stdout envelope and separate stderr diagnostics.
- Durable run IDs and absolute artifact paths.
- Successful **and unsuccessful** grouped observations.
- Definitive submission errors versus refusals/invalid output.
- Failed-member subset retry histories.
- No duplicate completed judgments after ordinary resume.
- Character IDs, reversals, attempts, complete probabilities/mappings, score distinctions, missing values, and provenance.

Missing prerequisites or failed assertions fail the required job. No mandatory processx, rubricLLM, pairwiseLLM, or Stan is added.

### Practical memory qualification

Retain approximately **5,000 responses × 20 traits** and **256 MiB peak memory** as the large qualification objective.

Before making that fixture permanent CI:

1. Benchmark it in the existing Ubuntu configuration.
2. Record runtime, peak RSS, Python allocations, database/artifact size, and variability.
3. Confirm that the complete job fits its existing 20-minute timeout with adequate margin.

Start required CI with smaller deterministic subprocess fixtures, for example 200 responses with 20 versus 200 traits, fixed group size/concurrency, and minimal capture. Measure after configuration/index initialization to separate permitted rubric/index memory from task-product growth.

Assert bounded worker/queue counts, a fixed execution-memory envelope, and the 256 MiB objective. Compare allocations and native RSS; do not rely on Python allocations alone.

Promote the larger fixture only after measurements establish reliable runtime. Otherwise retain the deterministic required regression and document the full fixture as an offline qualification benchmark. Do not skip memory assertions or weaken the requirement that execution memory is independent of the Cartesian product.

Keep the documented 100,000-response and million-task stress commands.

## H. Preserved six implementation subphases

| Subphase | Deliverable and acceptance |
|---|---|
| **1. Durable foundation** | Models, schema/repository, locking, artifacts, hashes. Verify immutable successful/unsuccessful result storage and alternative failure evidence. |
| **2. Immutable preparation** | Begin with strict generation configuration, scoped capability validation and manifest-generation commitments; verify sparse serialization, legacy compatibility, hashing and resume checks. Then source discovery/freezing/indexes, capture policies, complete manifest and incremental preparation. Verify concurrent-source-change rejection, multiline locators, privacy, and unchanged legacy IDs when no generation controls are requested. |
| **3. Submission/recovery core** | Protocol, mock ledger, bounded executor, receipts. Verify submission uncertainty and every minimal response-loss window against independent acceptance counts. |
| **4. Outcomes and retry lifecycle** | Partial grouped outcomes, safe retries, explicit subset operations, cancellation, export. Verify failures survive restarts and prior outcomes remain immutable. |
| **5. CLI controls and R lifecycle** | Commands/envelopes, installed-CLI R tests. Verify unsuccessful observations and subset retry histories across independent processes and exports. |
| **6. Scale and qualification** | Benchmarks, practical mandatory memory regression, examples, verification record. Qualify full fixture runtime before permanent CI inclusion. |

Each subphase preserves the passing Milestone 1 baseline and strict R gate. Run Ruff, formatting checks, Pyright, pytest with coverage, package build/metadata checks, and offline installation smoke tests as applicable.

Milestone acceptance requires the entire suite, not merely completion of intermediate PRs.

## I. Decisions and remaining qualification

No architecture decisions remain unresolved after these clarifications.

The remaining evidence-dependent item is **large-fixture CI placement**. Benchmark results determine whether the 5,000 × 20 fixture runs permanently in required CI or remains a documented offline benchmark alongside a required deterministic bounded-memory regression. This does not change the memory requirement or milestone scope.

Preserve the approved additive public interfaces/configuration, normalized schema version 1, export columns, and planned `0.1.0a3` development version. Document all execution semantics alongside implementation.

## J. Deferred functionality

Deferrals remain unchanged:

- **Milestone 3:** Real generative adapters, supported generation controls per provider/model/endpoint, and SDK/payload qualification.
- **Milestone 4:** Native decision/score adapters, endpoint-specific probability handling, and rejection of inapplicable generative controls.
- **Milestone 5:** Provider-managed batches, live bulk orchestration, generation parameter parity across live/batch modes, and actual caching controls/accounting.
- **Milestone 6:** Catalog, rubricLLM/processx wrapper using the same generation run configuration, required pairwiseLLM integration, prerelease qualification.
- **Milestone 7:** Optional local FastAPI service.

No real provider requests, statistical engine, arbitrary provider request JSON, background web service, or general workflow framework enters Milestone 2.
