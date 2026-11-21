# M5 PR2: provider-neutral native batch core

This slice of JudgmentLLM #61 provides durable batch orchestration with an
offline `mock/synthetic` consumer. It requires RunSpec V2, `batch_native`, cache
`off`, no tier and no bulk-live limits. Other routes, Decisions, explicit caching
and incompatible modes fail before credentials, SDK clients or network access.
The existing production registry and live/bulk-live gates are unchanged.

## Ownership and versions

| Owner | Contract |
| --- | --- |
| `preparation/batch.py` | Frozen original task/request/attempt membership, exact V2 prompt/payload commitments, deterministic ordered shards |
| `batch_models.py`, `persistence/native_batch.py`, SQLite schema 4 | Immutable operation intents, receipts, job-to-member evidence bridge, stop commands, cursors and audit facts |
| `execution/native_batch.py` | Exclusive ownership, bounded invocation admission, receipt-first recovery, cancellation, collection and existing result interpretation |
| `execution/retry.py`, `preparation/batch_retry.py` | Existing explicit scientific retry authorization and cap; a new attempt and child shard, never a submission retry disguised as a new judgment |
| `providers/batch_protocol.py`, `providers/batch_mock.py` | Narrow encoding/evidence seam and independent synthetic service ledger; no live provider submission is reused |
| `native_batch.py`, `batch_cli.py` | Public Python services and thin machine-envelope CLI controls |
| `execution/batch_exports.py` | Normalized files and job/member/operation/control companions from one SQLite audit cut |

Fresh stores use schema 4. Readers verify historical schema 1/2/3 checksums and
structures without migration, backfill or changing their statements. Native batch
requires a fresh schema-4 store. A historical run keeps its original executable
manifest; only native batch uses manifest 12. Normalized JSONL, lossless CSV and
R audit schemas remain version 1. Historical status serialization omits the new
optional `batch` detail.

Preparation freezes every scientific request before any remote operation. Member
keys bind run, request and attempt; shard identities and order are deterministic
for that sealed preparation. Splitting respects immutable member and byte limits
(mock defaults: 1,000 members, 16 MiB input and 1 MiB result record). It never
truncates a member, changes request grouping or drops exemplars. A child retry
uses the original authorization, selected task subset and proven parent receipt.

PR3+ own real routes, wire formats, service idempotency/fencing capabilities,
transport qualification and paid acceptance. The synthetic protocol, IDs, limits
and fixture flags declare no real provider behavior. In particular #249's
`store=false`, provider output caps and adapter wire fixtures remain deferred.
The core preserves existing immutable omission/presence and payload distinctions;
it adds no provider policy. #57 is no caching qualification.

## Operations, recovery and stopping

A scientific attempt is not an upload/create/cancel operation. Each logical
operation has immutable instances numbered 1–3, including the initial instance.
SQL and service guards enforce one forward per instance and the cumulative cap
across restarts. An immutable intent and exact authorized body commitment are
durable before forwarding. An authorized receipt is durable before acceptance,
member accounting or results are projected into SQL. Recovery reads that receipt
first, then reconciles only the original operation if needed.

The operation journal is its transactionally registered immutable intent artifact
and `batch_operations` row. Unregistered content-addressed artifacts authorize
no forward and consume no instance debit. No named owner-specific intent reference
is published before that transaction; death there cannot poison a later permitted
initial send. A registered operation with unknown acceptance still requires the
original receipt/reconciliation and cannot use this uninvoked boundary to resend.

Unknown acceptance remains unresolved. Absence, stale status, timeout, a 503 or
an unavailable lookup never proves nonacceptance. A later explicitly initiated
invocation may allocate a successor only after authoritative nonacceptance for
that exact immutable operation and a fence that prevents the earlier sender from
forwarding. A delayed stale sender must be rejected before its effect; server
deduplication alone is insufficient. No invocation automatically allocates a
successor after learning rejection. Reading again never authorizes sending again.

`stop_batch` and ordinary `cancel_run` durably stop local scheduling. Remote
`cancel_batch` additionally records a distinct sticky cancel-all intent, including
when no job is known. The command reference precedes SQL projection and is replayed
before control work after a crash. Stop prevents upload/create and scientific
retry. Receipt-first late discovery may establish the original accepted job and
admit its eligible initial cancellation. An uncertain cancel is reconciled first;
it is never redelivered without authoritative nonacceptance plus the old sender's
fence. Known terminal jobs need no new cancel. Results remain collectible after
either stop or cancellation.

Each control invocation has a shared finite deadline and counters. Defaults are
30 seconds, one status poll, 1,024 I/O steps and 128 returned rows. All adapter
operations, reconciliation, reads, payload chunks and stream advances consume
that budget; result chunks are at most 64 KiB. Poll sleeps occur only between
admitted polls. Exhaustion stops admission and leaves unknown work unresolved.
Cancellation/timeout drains in-flight work and closes streams while retaining
the lifetime run lock. Cleanup can outlast the admission deadline; there is no
detached sender or background mode. These are admission bounds, not guaranteed
provider spending limits. Unknown token counts and costs remain null.

## Screening and result evidence

The adapter must authorize control/result representations, member keys and
source/cursor identities in memory before any durable hash, staging file,
receipt, SQLite row, WAL, or export. A successful typed parse alone does not
authorize arbitrary provider fields or identifiers. Control receipts retain only
the authorized typed representation; raw control bytes and their hashes are not
retained. Status must match the exact accepted submission and remote job.

Member receipts distinguish:

* **Safe original:** the original representation is authorized; its hash may be
  recorded. Full capture retains the canonical screened row separately.
* **Allowed derivative:** documented authentication/raw-marker fields are removed
  before hashing or persistence. The original hash is absent; disposition is
  `redacted`. A synthetic fixture commitment also excludes raw markers.
* **Withheld:** unsafe/unknown member or source keys and unapproved representations
  yield only a fixed diagnostic tied to an already authorized source position.
  Neither prohibited bytes nor their hashes are retained. No outcome is invented.

Full capture retains the approved row; minimal capture records its permitted hash
and typed evidence only. Minimal recovery requires the original service to return
that row when normalization was interrupted. An existing full receipt can recover
without result retrieval. Original source-generation/snapshot permissions still
apply. API keys are never read by this consumer.

Output and error sources are consumed independently and may arrive out of order
or contain only errors. Attribution uses immutable member keys, never response
position. A typed cursor binds the accepted submission, immutable source,
position and approved-prefix chain. It advances after durable member/result work.
Member completeness is separate from source exhaustion. Collection keeps reading
bounded unread output and error suffixes after every member has an outcome.
The original mock service commits an immutable, job/payload-scoped index of
screened prefix hashes before returning results. Collection authenticates that
bounded typed metadata and compares the saved prefix before requesting its unread
range. Each new screened row must match the index before receipt/cursor admission.
The cursor pins the service commitment; a historical cursor without that pin must
still match the authoritative prefix. This is source evidence, not a cached local
verification claim. Index reads and range streams share the fixed invocation
byte, deadline and step budgets; exhaustion cannot advance the cursor. No budget
is raised to fit a prefix, and no scientific attempt or send is added. Only an
observed source EOF records `exhausted=true`. Historical cursor bytes omit this
additive false-by-default field and establish no EOF proof. Unread sources keep
current recovery status unresolved even when all member outcomes are proven.
Duplicates are idempotent. A conflicting member or terminal status preserves
proven prior results and old exports while adding current unresolved conflict
evidence. Terminal job status never fills in missing members. Usage belongs once
to each proven scientific member receipt, not to every trait result or poll.

## Public controls and audit files

Python exports `prepare_batch`, `submit_batch`, `get_batch_status`, `poll_batch`,
`collect_batch`, `resume_batch`, `cancel_batch`, `stop_batch` and
`export_batch_provenance`. Submit/poll/collect/resume/cancel have `a`-prefixed async
counterparts. Synchronous controls reject use inside a running event loop.
`BatchMockOptions` selects the original independent service ledger, immutable
synthetic fixture and limits; `BatchControlOptionsV1` supplies invocation bounds.
An explicit `RetrySelection` is accepted only by resume, preserves the existing
scientific-attempt cap and requires existing completed-task permission when
applicable. Stop cannot be undone by supplying another retry ID.

```console
judgmentllm batch prepare --config run.json --run 001 --store private/store --json
judgmentllm batch submit --run 001 --store private/store --json
judgmentllm batch status --run 001 --store private/store --refresh --json
judgmentllm batch collect --run 001 --store private/store --max-result-rows 1 --json
judgmentllm batch resume --run 001 --store private/store --json
judgmentllm results provenance --run 001 --store private/store --json
```

`status` without `--refresh` is a local snapshot with no adapter or source access.
The ordinary CLI JSON envelope and partial exit 3 remain unchanged. The fixture
path and any nondefault mock limits must be supplied consistently when reopening.
No command accepts arbitrary provider request JSON. `run live`/bulk entry points
keep their existing mode gates; they do not become native-batch entry points.

`export_batch_provenance` publishes `results.jsonl`, `results.csv`, `results_r.csv`
and `jobs`, `members`, `operations`, `controls` companions in JSONL and JSON-cell
CSV. One read transaction freezes event/result/fact high-water marks, operation
and member counts and source-manifest hash for all files. Artifacts are published
before the bundle manifest; an interrupted publication exposes no partial bundle.
Reopening an explicit bundle ID verifies its original artifacts and never
rewrites that cut. The current run can independently require recovery. Read CSV
companion columns as characters and decode every cell with jsonlite; this
preserves nulls, nested vectors and leading-zero IDs.

## Contract-first acceptance

| Boundary | Required synthetic evidence |
| --- | --- |
| Death and receipt recovery | Intent; forward before ACK; receipt before SQL; SQL before projection; member receipt/observation; during atomic grouped results; results before cursor; cursor; retry attempt before child shard |
| Stop and late discovery | Lost create ACK → stop/cancel-all with no known job → fresh process discovers same job → exactly one eligible cancel; ambiguous cancel remains unresolved without redelivery |
| Terminal two-row case | Terminal job first, then one proven success with 7 input/3 output tokens and one explicitly cancelled sibling; fresh-process partial collection/replay retains both identities/outcomes and usage once; missing sibling variant stays unresolved |
| Other returned evidence | Reordered output, error-only, idempotent duplicates, conflicting terminal/member evidence, bounded chunks/records/cursors and slow I/O |
| Negative gates and privacy | Wrong mode/route/cache/tier rejected before service/auth/network; full/minimal safe-original/derivative/withheld scans include WAL, staging and temp files and prohibited hashes |
| Compatibility and exports | Genuine historical schemas; no migration; exact parent/task crosswalk; one-cut partial/full exports; immutable prior bundle and null unknown cost |
| Separate processes and R | Source-process OS correctness separately from installed acceptance; actual R/jsonlite independently reads Python-generated files |

The existing **seventh** strict local R gate remains R-driven: outside the
checkout, it launches installed separate CLI processes for prepare, submit,
status, partial collect, resume, recovery and export. R checks process exits,
single stdout envelopes, stderr diagnostics, artifacts, both retention modes,
late cancellation and original/presented pair identities. It retains its existing
**180-second total deadline**. All seven gates and separate Windows/macOS process
correctness remain mandatory; focused R export checks do not replace this gate.
The installed driver reuses `check_batch_audit` from the standalone R consumer for
actual canonical and R CSV contents, compares complete member/evidence records on
replay, and requires unknown sibling usage to remain NULL, including output counts.

This change is at least **Level 3**. After independent code review and a committed
candidate freeze, record the protected-base automatic plan and require complete
local Linux/Python 3.11 historical and change-aware coverage with the unchanged
90% combined floor, all seven strict local R gates, affected resources/durability
and package checks, and exact-source hosted Linux 3.11 plus extended affected
Windows 3.12/macOS 3.11 ordinary and serial correctness. Preserve systemd ownership,
locks, cleanup, memory/pressure gates and original fixture deadlines. No focused
check or PR acceptance authorizes paid tests, registry promotion or publication.
