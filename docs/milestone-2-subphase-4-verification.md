# Milestone 2, Subphase 4 verification

Implemented on `codex/milestone-2-outcomes-retry-lifecycle`, from clean
`origin/main` at `ae26665fffbba551f22b18a7321b539358d34692` (PR #6).
Package version remains `0.1.0a2`; normalized results and registry schema remain
version 1. No push, merge, release or Subphase 5 CLI implementation is included.

## Interfaces and files

- `providers/mock.py`, `protocol.py`, `ledger.py`: grouped synthetic member
  recipes, per-member probabilities, terminal evidence and typed transient
  definitive nonacceptance. The independent ledger retains identities, hashes
  and synthetic numeric/enum recipes, with no assessment text.
- `execution/processing.py`: private partial-member interpretation; public
  `validate_categorical_output()` remains unchanged.
- `persistence/repository.py`, `models.py`: ordered attempt result groups,
  atomic group commits, parent aggregate states, snapshot reads and distinct
  logical task/result/retry counters.
- `preparation/session.py`: checkpoint-based streaming of unscheduled work,
  reconstruction of individual durable parents, verified ordered subset rendering.
- `execution/retry.py`: typed policy, explicit retry authorization/preparation,
  exact prior-outcome commitment, durable automatic authorization and eligibility.
- `execution/core.py`, `session.py`: grouped normalization, retry scheduling,
  conservative recovery and cancellation cleanup on the dedicated I/O thread.
- `execution/cancellation.py`: owner-checked durable cancellation and resumable drain.
- `execution/exports.py`: immutable provider-free paged all-attempt export.
- Grouped, retry, cancellation, export and resource tests; independent-process
  Python/R fixtures and the sixth required CI interoperability gate.

These interfaces are internal. Existing public APIs, configuration serialization,
CLI behavior, result columns and R serializers remain compatible.

## Grouped outcomes and evidence

Rubric `independent`, explicit/fixed `grouped` and capability-limited `auto`
requests retain their original ordered task/trait membership and logical IDs.
Pairwise assessment remains independent. The mock supports valid/invalid/missing/
duplicate/malformed members, invalid categories/explanations, unexpected or
unattributable entries, reordering, unrecognizable structures, refusals, provider
errors, probabilities and authoritative confirmed cancellation.

The interpreter recognizes the outer structure, matches exact trait IDs, and
passes each uniquely attributable member through the existing strict validator
and `normalize_rubric_result()`. It retains valid siblings. Missing, duplicate
and invalid members receive `missing_output`, `duplicate_output` and
`invalid_output`; refusal/error evidence uses the established classifications.
Unrecognizable output yields unsuccessful expected-member records justified by
receipt of that response, with no inferred categories or identities for entries.
Unexpected/unattributable entries produce content-free counts. Unsuccessful
results have null scoring/explanation fields and retain ordered category coding.
Applicable distributions, expected/selected distinctions and score kinds survive.
Unavailable usage stays unavailable; grouped token totals are never duplicated
onto individual trait rows.

## Atomic persistence and recovery

One short transaction inserts every attempt member's result and audit event,
updates task states, aggregates the original request, and transitions attempt and
submission. Request state is `partial` when only some original members succeed.
A fault inside any member insert rolls back the entire group. Repeated identical
observation delivery compares the exact committed group/evidence and inserts
nothing; conflicting content or membership fails closed. Single-result callers
retain `get_result_for_attempt()`; grouped callers use `get_results_for_attempt()`
or select an explicit member.

Recovery verifies receipts and byte commitments. Accepted observations without
committed results are retrieved/replayed under their original observation ID.
Committed groups use their exact immutable original results. Minimal receipts
cannot reconstruct raw output: unavailable authoritative retrieval leaves work
unresolved and never authorizes another submission. The producer reopens durable
attempt histories and starts new source scheduling at its committed checkpoint.

## Explicit retries and automatic eligibility

An explicit operation selects task IDs from one immutable parent request. It
requires a stable operation ID and completed-task permission when applicable.
Selection follows original order, never response order. The authorization digest
commits exact task membership, prior result hashes/attempt identities, policy and
permission. Authorization, exact attempt subset and submission intent commit
together. Same-ID reuse resumes that attempt using its original prior-outcome
basis; conflicting reuse fails. Pending automatic authorizations reserve their
basis against competing retry operations.

Each retry has a new attempt number/ID and submission/correlation identity. Prompt
rendering and provider serialization use only the selected subset; actual bytes
determine hashes. Equal bodies may keep equal hashes. Original request membership,
scheduling identity, successful siblings and earlier unsuccessful results remain
intact. Preparing a retry makes a previously completed run incomplete, without
claiming an executor continues after the provider-free operation exits.

Default automatic policy permits three physical attempts including the initial,
with exponential 1/2-second delays, a 30-second cap, optional deterministic full
jitter and authoritative retry-after. The adapter must provide typed transient
nonacceptance evidence; durable failure evidence is independently checked.
Authorization and `next_eligible_at` persist in the failure transaction before
rendering/preparation. Reopening retains counts and eligibility. Invalid/refused/
missing/duplicate/accepted outputs, uncertainty, missing observations and local
render/validation errors never qualify. Prior successful work requires explicit
permission rather than automatic retry.

Backoff uses an asynchronous wait after ready requests and bounded queues drain;
no worker or SQLite transaction waits for eligibility. Other ready requests run
before the wait. No unenforced timeout/token-rate controls are introduced.
Internal recovery callers retain the same explicit policy; these settings do not
change RunSpec or canonical identities.

## Cancellation

Live callers cancel the executor coroutine; idle/interrupted callers use
`cancel_run()`. Both honor lifetime OS ownership and owner-token checks. Cleanup
queues behind initialization and in-progress durable writes, and tolerates further
cancellation while draining. Mock acceptance writes finish before ownership is
released. Competing executors/cancellers cannot take over a live owner.

The cancelled run state commits before work drains, so interruption resumes the
same cancellation. Prepared/claimed work becomes locally cancelled with no
fabricated result or provider observation. Submitting/accepted work retains its
original certainty, accepted observations and results. Recovery may collect those
same observations while the run remains cancelled; it does not schedule new work.
Only a mock/provider observation explicitly establishing confirmed cancellation
creates null-score cancelled result rows. Cancelling an asyncio task never
establishes provider-side cancellation.

## Immutable exports

`export_run()` supports canonical JSONL, canonical CSV and R-compatible CSV
through the existing serializers. All recorded attempts are included, including
failures before successful retries and confirmed cancellations. Planned, blocked,
unobserved or unresolved work creates no export rows.

Each export selects one result high-water mark before paging. Identity commits
run, snapshot and options; reports include row count, exact byte length/SHA-256,
absolute immutable artifact path and current completeness. Publication writes
verified content-addressed bytes before a short export-record transaction.
Interrupted staging/publication is not a published export. Identical concurrent
exporters converge under a publication transaction, and prior exports stay
unchanged when new results arrive. Buffers/pages are bounded. Exports require no
adapter and make no provider calls, retries or missing-result reconstruction.

## Privacy and fault injection

Tests inspect source/prompt/payload/raw/credential sentinels separately from an
authorized explanation sentinel across database/WAL/SHM, ledgers, artifacts,
receipts, exports, temporary files and diagnostics. Minimal capture retains only
authorized normalized data, including explanations. Existing Windows live SHM
mapping and executor-lock checks and warning-as-error policies remain intact.

Faults and actual subprocess deaths cover response/receipt publication, member
validation, group transactions and postcommit reporting; authorization/preparation/
submission and durable backoff; cancellation transaction/drain and competing
ownership; and interrupted/concurrent export publication. Independent acceptance
counts prove same-observation recovery does not duplicate accepted submissions.

## Memory and concurrency

The dedicated nonblocking I/O boundary, fixed worker pool and queues bounded at
2× concurrency (requests) and concurrency (results) remain. Required subprocess
regressions compare 200×20, 2,000×20 and 200×200 configurations with groups of five.
They retain only a bounded prefix of 64 physical requests (320 results), rather
than materializing pending response-by-trait work. They assert fixed executor
task counts, bounded native RSS below 256 MiB, execution allocations below 32 MiB,
and export allocations below 8 MiB. Tenfold work growth adds less than 3 MiB peak
execution allocation, 1 MiB retained/export allocation and 48 MiB native RSS.
Retry selection retains one bounded group and export iterates pages.

A separately retained 200×20 sample (groups of five, concurrency four, 16 physical
outcomes/80 rows) measured 53,952,512 bytes native peak RSS, 2,034,706 bytes peak
Python execution allocation and 1,206,460 bytes peak export allocation. It created
six executor asyncio tasks, peaked at seven live tasks, and observed both queues
at one entry. This diagnostic sample took 3.69 seconds; it is not the full
qualification workload.

The approximately 5,000×20 full qualification and 100,000-response stress workload
remain Subphase 6. The regression limits are unchanged and no large-scale
qualification claim is made here.

## Local gates and interoperability

Local platform: Linux, Python 3.12.3, uv 0.13.6, R 4.6.2 and jsonlite 2.0.0.
Verification commands use the locked environment. The initial complete integrated
run passed 1,266 tests with 96.01% branch-inclusive coverage in 347.35 seconds;
the final integrated run passed **1,271 tests with 96.10% coverage** in 346.44
seconds, including the additional edge-case regressions.

| Gate | Local result |
|---|---|
| Ruff lint and formatting | Passed; 117 Python files formatted |
| Strict Pyright | Passed; zero errors/warnings |
| pytest, coverage >=90%, both warning classes errors | Passed: 1,271 tests; 96.10% |
| `uv lock --offline --check` | Passed, 93 packages resolved |
| Isolated wheel/sdist build | Passed offline with existing dependency cache |
| `twine check --strict` | Passed both current distributions |
| Clean wheel/source installation smoke | Passed, including grouped salvage, exact subset retry and all export views |
| All six strict independent-process R/jsonlite gates | Passed |
| Diff whitespace and unchanged version/schema | Passed |

Build command: `UV_CACHE_DIR=/tmp/judgmentllm-uv-cache uv build --offline`.
The default global cache lacked Hatchling; using the existing project cache
satisfied the isolated offline build without changing dependencies or checks.
Threaded asyncio tests stalled inside the restricted sandbox; the same commands
completed outside that sandbox. This is an execution-environment limitation,
not a suppressed test or warning. No test limits or privacy assertions changed.

All five original strict Python/R gates and the new grouped/retry/export gate
passed in independent offline processes. The new fixture verifies full/minimal
captures, leading-zero IDs, exact task/request/attempt/submission/observation joins,
original failed outcomes after subset retries, mappings/probabilities/score kinds,
canonical/R exports, missing retained paths and independent acceptance counts.
Only base R/jsonlite is required. The unchanged six OS/Python CI configurations
and required Ubuntu/Python 3.11 R gates remain; this local Linux run does not claim
macOS, Windows or Python 3.11/3.15 CI execution.

## Compatibility and deviations

No migration is necessary: existing version-1 tables, checksummed schema and
append-only guards represent exact memberships, authorizations, eligibility,
grouped results, failure evidence and exports. No schema/checksum is silently
rewritten. Package version, normalized result schema and R columns are unchanged.

No scope features were added outside Subphase 4. Cancellation uses the existing
confirmed-cancellation observation kind instead of adding a provider cancellation
endpoint. Export bytes use the established immutable object store rather than
requiring a duplicate copy under an `exports` directory. Public timeout/rate
configuration remains absent because no enforcement boundary requires it here.

## Subphase 5 handoff

After review, Subphase 5 should expose shared public Python/CLI lifecycle services
using these internals, without duplicating scheduling, normalization or exports.
Implement the approved `run`, `resume`, `status`, explicit retry and `results export`
contracts, stable JSON envelopes, safe stderr diagnostics and exit semantics.
Carry explicit retry IDs/selection/completed permission and recovery policy through
the public boundary, distinguish logical completion from physical/history counts,
and report cancelled/incomplete/unresolved runs accurately.

Add installed-CLI independent-process R lifecycle acceptance with base R/jsonlite;
keep all six current gates mandatory. Do not infer provider continuation after a
command exits, reconstruct lost minimal responses from recipes, or resubmit
uncertain work. Real providers, provider-managed batches, rubricLLM/FastAPI and
full scale qualification remain assigned to their approved later milestones.

## PR #7 Windows CI corrections

The initial [CI run 41654229976](https://github.com/shmercer/judgmentllm/actions/runs/41654229976)
passed all four Linux/macOS configurations and failed both Windows configurations.
The follow-up preserves the reviewed Subphase 4 architecture and all schema,
normalization, export, privacy, concurrency and retry contracts.

- Export staging was reopened as `rb` before `os.fsync()`. Windows rejects
  synchronization through that read-only descriptor with `EBADF`. The owned,
  completed staging file now reopens as `r+b`, without truncation, and explicitly
  flushes before the real fsync. Publication occurs only after synchronization
  succeeds. Nine new filesystem regressions exercise all three export views,
  empty/populated results, actual descriptor writes/fsync and exact SHA-256/length;
  injected sync failure must propagate and leave no published export.
- Grouped live privacy inspection omitted the required `mapped` tracker. It now
  uses the established exact SHM mapping and one-byte lock deferral rules, scans
  all files after release before/after recovery, and checks authorized explanations
  separately. Explicit injection tracking proves interruption variants reached
  the requested validation/result-transaction boundary instead of passing on a
  premature inspection failure.
- The Windows/Python 3.15 cancellation test terminated a marker PID and immediately
  reaped/killed its launcher. Its previous cleanup did not wait on the actual
  owner process object. The repeated native test now captures that live owner's
  handle, waits for termination to complete, and only then reaps the launcher and
  attempts takeover. It records both PIDs and native exit/wait diagnostics, and
  immediately checks SQLite integrity, foreign keys, stale ownership recovery,
  zero fabricated results and zero accepted submissions. The PID marker itself
  is atomically published. No production SQLite change, WAL/SHM deletion, ignored
  integrity error or database reconstruction is introduced.

The six-configuration CI matrix includes a focused native Windows regression step
before the full quality gates. This runs export durability, all export tests,
all grouped privacy interruption variants, three repeated exclusive-owner death
cycles, and independent/grouped subprocess memory fixtures with the unchanged
warning/error and memory limits. Native CI evidence and final results follow.
