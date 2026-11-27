# M5 native Batch: shared core and generative routes

M5 PR3 adds OpenAI Responses and Chat Completions as separate generative Batch scopes, using RunSpec V3 and manifest 13. The OpenAI contract below supersedes only the provider-specific collection/transport details. Decisions, other providers and explicit caching are outside PR3.

M5 PR4 adds offline preparation and synthetic verification for Anthropic Messages
Batch, using RunSpec V3 and manifest 14. Public Anthropic remote controls are
disabled. The route contract is below.

## Retained PR2 contract

PR2 provides durable batch orchestration with an
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

Fresh cancellation of the public invocation task drains owned work, records one
local scheduling stop under ownership, then propagates cancellation. A deadline,
child self-cancellation, inherited cancellation count or nested payload timeout
does not authorize that stop. Already-returned mutation acknowledgments are
screened and durably published before acceptance is projected and before the
stop; this permits later receipt-first recovery without another send. Integrity,
storage and stop-publication failures retain their existing safe diagnostics.

## Screening and result evidence

The adapter must authorize control/result representations, member keys and
source/cursor identities in memory before any durable hash, staging file,
receipt, SQLite row, WAL, or export. A successful typed parse alone does not
authorize arbitrary provider fields or identifiers. Status must match the exact
accepted submission and remote job.

Every Batch provider must preserve durable private HTTP diagnostics for each
physical call. Record screened status and request ID as soon as headers arrive,
then attach an allowlisted error code or fixed interruption code when known.
Full capture may retain complete eligible control bodies or screened derivatives
under the route's byte and deadline limits. Minimal capture may keep permitted
digests and typed metadata. Withhold unsafe bytes and their hashes in either mode.
Diagnostics do not prove mutation acceptance, source progress or retry eligibility.
PR4 implements this requirement for Anthropic. OpenAI's existing projection-only
control retention is accounted for in its separate diagnostics work.

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


## Anthropic Messages Batch (M5 PR4)

The offline route accepts V3 `anthropic/messages` with `batch_native`, cache
`off` and non-strict `text` or `auto` output. It uses locally validated text
for rubric, grouped traits and pairwise work. Tier, account, bulk-live,
sampling, explicit reasoning, native output and prompt-cache overrides fail
locally. The Sonnet 4.6 output-token bound is 128,000. Unknown models can be
sealed offline with unknown serving capability. The route checks actual and
loaded Anthropic 2.3.0 and HTTPX2 2.16.0 before constructing any client.
Public submit, refresh, collect, resume and remote cancel fail before opening a
writer or constructing a client. Local preparation, status, stop and exports
remain available.
Both V14 resume entry points inspect the registry without changing its files,
including when the original SHM is absent. Dispatch checks the registry's
immutable provider identity against a private snapshot that can tolerate an
verified WAL append. A checkpoint that invalidates the snapshot triggers one
fresh dispatch probe. V12 and V13 controls then use SQLite's usual concurrent
committed reader for preflight.

Each member uses an opaque deterministic `custom_id` and the exact rendered
Messages body. The adapter seals `stream=false`, `service_tier=standard_only`
and the requested `max_tokens` or adapter default 4096. Initial and explicit
scientific retry shards use the same deterministic framing, a JSON object with
a `requests` array. The complete POST body is limited to 1,000 members and
16 MiB. Manifest 14, prepared request 11, output commitment 5 and audit 3
are additive. SQLite 4, the normalized schema-one files and the 11 audit
companions retain their existing layouts.

### Submission, cancellation and recovery

The sealed body goes directly to `POST /v1/messages/batches`. There is no upload
step. The durable journal precedes each mutation. Create and remote cancel
each have one eligible POST for the original intent. A stored acceptance
receipt can repair the SQLite projection after process death. The create receipt
pins the job ID and creation and expiry timestamps before the first status read.
An accepted job ID belongs to one shard in the run. Listing does not
expose an exact correlation for a lost create receipt. Missing or ambiguous
acceptance stays unresolved, even after a rejection or interruption. A GET of
the same accepted job can show cancellation at the job level. That observation
has separate provenance from a POST acknowledgment and never assigns outcomes
to individual members. Explicit scientific retry uses the existing selection
and attempt cap after physical work is resolved.

An `ended` response with no URL can become ready. The immutable ready record
pins the sealed shard, accepted create, job ID, creation/expiry/end timestamps,
terminal counts and the job's same-origin `/results` URL. Archive makes an
unread source unavailable. Collection starts at byte zero, verifies the
screened prefix and pins a receipt published just before the cursor to its
original position. Provider outer classifications are counted once per
attributable member, including succeeded responses with invalid assessments.
Unreported usage remains null. A missing member at EOF remains unresolved.

### Collection limits and private diagnostics

Each invocation can admit one 8 MiB result stream. A framed JSONL record is at
most 1 MiB and delivered blocks are at most 64 KiB. The locked SDK's
context-managed results call first retrieves job metadata. The guarded
transport charges that hidden GET, screens its raw response against a
separate 1 MiB limit and checks the pinned source before the SDK can follow
the URL. Content admission requires 578 steps (64 control steps plus four
times 128 controlled reads plus two final steps) and 17 MiB (two 8 MiB
source allowances plus 1 MiB for final metadata) remaining after the hidden
GET. Register the durable debit at the guarded content forward. An invocation
then makes the results GET and, after real EOF, one confirmation GET.
Noncoalescing raw reads charge actual transport advances and bytes.
Partial responses, ranges, multipart responses and ambiguous framing are
rejected before row ingestion. Already buffered bytes are charged before
framing validation. Each row is checked against the invocation budget even
when one transport chunk contains several rows.
Fragmentation can end an invocation before these upper bounds. Cleanup closes
unread bodies and awaits owned work. Row quotas, a matching length and a stall
after the last row supply no EOF evidence. Valid committed prefix rows and
usage survive interruption. Three admitted attempts without advancing that
source's frontier or verifying EOF terminate collection for that source.

The 1,000-member and token settings are offline input limits. They do not
establish that a result will fit either collector ceiling. A 1,000-member
job with 9 KiB per result already exceeds 8 MiB. Later live admission needs
an independently verified envelope for both framed output size and aggregate
shard size for the exact model and features.

Each physical HTTP call has a private typed header record and, when available,
a completion record linked to it. They bind the call stage to the original
mutation or accepted job and source. A process can recover the header record
after death before the body is read. Full capture can keep eligible job
bodies and sanitized rejection derivatives within the metadata limit.
Minimal capture keeps typed evidence and permitted digests. Unknown fields,
free-form error messages, unsafe IDs, authentication data and remote rows
claiming to be local derivatives cannot acquire an unsafe retained hash.
The protected SDK logging scope covers parsing, streaming and cleanup.
The diagnostic records are independent of the operational receipts and cursor.

The candidate requires protected Level 3 verification, including the locked
SDK seams, fresh-process recovery, local historical and changed-branch
coverage, all seven strict R gates and same-source hosted ordinary and serial
correctness. The seventh R gate keeps its 180-second installed deadline.
Independent review and qualification precede any separately authorized paid
smoke. Its proposed scope is one synthetic Sonnet 4.6 Messages Batch job with
omitted reasoning and one or two identifiable rows. Verify the designated
credential project, endpoint, exact billing and cap, retention and model
scope independently before any activation. The reviewed US$0.056832 estimate
is planning evidence.

## OpenAI generative Batch (M5 PR3)

Use the existing public batch services and CLI with V3 `openai/responses` or
`openai/chat.completions`, `execution.mode=batch_native`, cache OFF, no requested
tier and no bulk-live limits. Mock options are rejected even when explicitly set
to their defaults. Local status, stop and immutable export require neither the
SDK nor credentials. Preparation and wire reconstruction require **actual
installed metadata and loaded OpenAI SDK version 3.42.1**, checked before any
credential resolver or client. `uv.lock` remains unchanged.

These are independently scoped generative routes. Native output serving authority
for Batch starts empty: `native` and strict unknown `auto` fail before submission;
non-strict `auto` selects locally validated text. Live qualification does not
activate native Batch output. Unknown capabilities remain unknown. Neither
Decisions nor an explicit-cache candidate can enter this transport.

Manifest **13**, `openai-native-batch-v1` context/shard records and prepared request
**10** are additive. They use the shared output commitment **4**, protected renderer
and exact generative body builder, without a fabricated live submission identity.
The SQLite schema stays **4**; no migration or changed schema checksum is needed.
Mock manifest 12, old stores, normalized JSONL/CSV/R schema 1 and CLI envelope 1
keep their original encodings. OpenAI audit manifests/reports use **2** and typed
OpenAI job/source/selection/admission records; mock audit version 1 stays strict.

### Exact requests and usage

Each JSONL row binds the immutable member digest through `custom_id`, `POST`, the
exact `/v1/responses` or `/v1/chat/completions` URL and exact shared body bytes.
Grouping, numeric representation, requested controls and omission survive restart.
Responses maps a requested cap to `max_output_tokens`; Chat maps it to
`max_completion_tokens` and sends `n=1`. Both send `stream=false`, `store=false`,
the existing default service representation and OFF cache representation without
breakpoints. Responses sends `truncation=disabled`. An omitted output cap stays
omitted; there is no default, clamp, provider fallback or body-field deletion.
`store=false` is deliberately different from omission and does not remove Batch
input/output Files. Any serving rejection of the exact body fails rather than
silently changing it.

Usage is mapped from each attributable member's documented route-specific usage.
The immutable physical member receipt accounts for it once, including grouped
traits and repeated/reordered rows. Cached/reasoning counters retain their parent
relationships; totals, unknown usage and costs are never inferred. Polls, file
reads and aggregate job counts do not add scientific usage. Reported model,
response ID, SDK and `native_batch` provenance remain attached to that member.

### Mutation uncertainty and privacy

The official asynchronous Files/Batches SDK uses fixed HTTPS routing, explicit
credentials only, no SDK retries, environment proxies or redirects. A final wire
check covers exact multipart fields/file bytes, create metadata/body and the
original cancel target. Upload intent precedes forwarding; screened acceptance
receipts precede SQL projection. An acknowledged upload is reused.

The Batch HTTP client ignores response cookies, and the final wire check
rejects outbound `Cookie` headers.

Lost upload acknowledgment requires a unique bounded Files-list match by opaque
operation filename **and exact reconstructed authorized input bytes**. Lost create
acknowledgment requires positive original operation metadata/input/route matching
and exact job retrieval. Incomplete or multiple matches, absence, 404/503, timeouts
and failed lookups remain unresolved. OpenAI supplies no old-sender fence in this
implementation: it never receives an automatic successor or ambiguous resend.
A finalizing accepted job remains accepted through a transient poll failure and
resumes the same IDs. Sticky stop/cancel-all and late eligible cancellation retain
the original PR2 semantics; collection is allowed after stop/cancellation.

Only screened typed projections cross hashes, immutable artifacts, SQLite/WAL or
exports. Arbitrary control bodies and their hashes are discarded. Full capture
retains approved originals or a labeled safe derivative; minimal capture keeps
approved hashes/typed evidence only. Authentication fields, unsafe identifiers,
unknown member keys and unauthorized text never gain an original hash. No partial
raw/encrypted spool is used to bypass screening. Upload and result Files request
30-day expiry; output expiry is explicit at create. This is not a zero-retention
claim, and `store=false` does not eliminate provider-side Files retention.

#### OpenAI Batch diagnostics

Audit v2's `controls.jsonl` and `controls.csv` include
`openai_batch_diagnostic_v1` records under the `openai-diagnostic:` cursor
namespace. Each call records its phase, operation, and local call ID. Its
neutral header fact preserves a screened HTTP status and request ID even when
the body, SDK wrapper, or local JSON decoder later fails. A second fact can
describe the failure or lookup outcome. SDK validation failure requires the SDK
exception to identify the same successful HTTP response. Unattributed failures
remain indeterminate. Result rows use their own status and
request ID, tied to a sealed member and source position. Initial metadata
binds to the ready slot. File content and the post-EOF metadata check bind to
the pinned source. Optional metadata is explicitly null when unavailable.

One syntactically valid, credential-free `req_` request ID is eligible per
call or row. A code is eligible only when all reported non-null candidates
agree on one of `batch_cancelled`, `batch_expired`, and `invalid_json_line`.
The only sources are buffered HTTP error `error.code`, bound failed or expired
job `errors.data[*].code` (up to 32 entries), and attributed member
`error.code` or `response.body.error.code`. Unread error bodies are closed
without draining. Buffered bodies must pass the existing byte debit, control
ceiling and identity-encoding check before code extraction. The diagnostic record
contains no error text, arbitrary headers, or assessment text.

The sole original control-body retention exception is a screened direct
upload/create `2xx` acknowledgment in full capture. The upload body must match
the sealed filename, purpose, byte size and screened file ID. The create body
must match the sealed input file, endpoint and exact three operation bindings,
with a screened job ID and documented status. Only closed typed fields are
eligible, including documented timestamps, screened file IDs, and the
three integer request counts. Free text, unknown fields, non-null job errors
or usage, and credentials in the original or decoded body disqualify this
private copy. Eligible exact bytes are linked privately to the original intent
and committed acknowledgment. Neither the bytes, digest, nor private reference
appears in audit exports or is captured in minimal mode.

Diagnostics are append-only and independent of acceptance and source progress.
A successful empty lookup can report `no_match`. Empty and inconclusive lookups
leave the original intent unresolved. An observed HTTP rejection authorizes no
retry. A restarted collection leaves
earlier prefixes as recorded and reuses an already published row diagnostic
if interruption preceded its cursor advance. A diagnostic write failure
preserves response cleanup and any valid acknowledgment before surfacing
a local persistence failure. For an already buffered mutation response, the
header diagnostic waits for byte charging and local receipt settlement. Later
diagnostic work on a valid receipt also follows settlement. The existing
deadline still blocks new forwards and body reads.

New result receipts screen the original wire bytes and decoded envelope strings
before retaining an exact body or its digest. This includes metadata omitted by
the derivative. Decoded assessment strings are screened directly before
retention or normalization. A safe derivative may still be kept under the
existing capture rules. A source-bound screening cut preserves committed prefix
positions and a bound historical receipt whose cursor has yet to advance. An
identical repeat may reuse its bound receipt. New evidence uses the current
screen.

### Pinned source bytes and real EOF

A stable source slot binds manifest, shard, original accepted create/submission
and output/error role; returned file IDs or sizes never redefine it. The immutable
binding pins the screened terminal job, accepted input/upload, exact endpoint,
create receipt, role/file ID and **actual Files metadata `id`, `purpose` and strict
nonnegative `bytes`**, including zero. Metadata is read before every content GET
and rechecked after real EOF before exhaustion. Changed association, file ID,
purpose or byte size is a conflict; the earlier pin, results, cursors and exports
remain evidence. Equal bytes from another job or source cannot repair a mismatch.

The local source admission limit is **8 MiB per present file**, independently for
output and error. This is a local safety limit, not the provider's maximum file
size. A legal larger provider file fails collection with `source_limit`, before
content retrieval, and never triggers another submission. Existing input limits
remain 1,000 members/16 MiB per shard and 1 MiB per result record.

Actual content bytes are counted from zero on every complete download, including
replayed prefixes, UTF-8, CRLF, whitespace and lookahead. Before parsing or hashing
any row from a chunk, the entire chunk must fit the pinned byte count. Overrun is
rejected immediately. Exhaustion requires actual iterator EOF **at exactly the
pinned size**, complete framing/admitted rows, a verified saved prefix and the
unchanged final metadata. Matching length, an empty chunk, quota, terminal job or
complete member outcomes does not establish EOF. A present zero-byte file still
requires its content GET and real EOF; an absent slot is a different typed fact.

### Bounded replay and fair ready-source selection

One invocation admits at most one **present** file unit. Defaults stay 30 seconds,
1,024 I/O steps, one poll round and 128 result rows, with chunks at most 64 KiB.
For pinned size B, admission reserves `64 + 4*ceil(B/65536) + 2` steps (at most 578)
for replay, unread tail and EOF/control verification. The received-byte ceiling is
`max_io_steps * 65536`, shared by all control and content reads in that invocation.
Every physical read is charged before SDK buffering, JSON parsing or row handling,
including accumulated metadata, cached transport bodies and replayed prefixes.
The existing per-control 1 MiB and exact pinned content-size ceilings also apply.
Only a 2xx content response uses the pinned file size. Non-2xx responses are
rejected before the SDK can read or parse their error bodies: unread streams are
closed without draining, and already cached/eagerly read bytes are charged to
the control ceiling and shared byte budget. A failed debit or close stops the
invocation without claiming reconciliation. HTTP error text never advances a
cursor, supplies usage, establishes EOF or replaces the accepted job/file evidence.
After the initial metadata has been charged, content admission requires exactly
`2*B + C` remaining bytes, with `C = 1048576` for the bounded final metadata read.
Insufficient caller bounds produce a transient `collection_budget_unfit` and
partial result before content GET, without a collection debit. A fresh invocation
can retry with default bounds. Actual transport advances, including fragmentation,
consume the budget. This reserve is a controlled-delivery
proof, not a minimum-throughput promise from a provider or transport.

A separate durable collection admission is registered only after selected-source
reconstruction, metadata screening and both reservations, immediately before
content retrieval. Expiry before this point leaves no collection debit and is
retryable in a fresh invocation. Only a verified frontier advance or real EOF
resets that source's no-progress
allowance. Metadata, polls, another source's progress, changed options and fresh
processes do not reset it. Three admitted attempts with no advance produce sticky
`collection_no_progress`; process death consumes its registered attempt. There
is no fourth read or silent permanent partial loop. Over/underrun, unsafe metadata,
association conflicts and framing failures terminate that source immediately.
Proven prior outcomes/usage remain intact; independent eligible sources can still
be collected. Current collection failures are surfaced as processing errors
(exit 1) and local status exposes their durable summary; ordinary pending work
continues to use partial/exit 3.

A screened terminal status and both ready slots project atomically. Collect/resume
replays local stop facts and examines a durable finite ready cut **before unrelated
remote reconciliation, submission, cancellation or polling**. Keyset pages hold at
most 128 ready references. The selection and projection cursors include monotonically increasing sequences so append-only fact deduplication cannot suppress a repeated rotation. The selection cursor records the last considered slot,
rotating before reconstruction or I/O, so an earlier stalled collector cannot
starve a later known eligible source across persistent or fresh processes.
Durably exhausted/failed slots are excluded through the existing fact index.
Prelude budget expiry is explicit retryable `selection_pending`; it creates no
sticky failure or collection debit. Earlier budget-only control facts remain
audit evidence and do not exclude eligible sources. Pending selection cannot fall
through to
an unrelated stalled poll. Only the selected shard is wire-reconstructed; required
global local integrity checks remain mandatory and bounded admission failures are
visible. Local receipt projection is separately paged after known ready selection.
Unknown/undiscovered provider files are not promised progress. Explicit poll stays
poll-only; a collector-only resume does not consume a deferred explicit retry.
Continuation SDK readiness failures use the public safe diagnostic boundary before
adapter/client construction, credentials or HTTP; untrusted version/error text is
not included in diagnostics.

### Acceptance and later gates

Synthetic offline regressions cover both routes and capture modes: exact live/body
parity and omission, actual SDK guards before secrets, ambiguous acceptance,
accepted-finalizing poll failure/same-ID resume, reordered/error-only/partial data,
immutable IDs/exports and once-only usage. Fresh real processes use a persistent
independent fake service. Pressure includes 192 roughly 32-KiB valid result rows,
real prefix replay beyond the 128-row quota, an independent error source and a
multi-shard early stalled poll with a later already-ready source. Size tests cover
zero, exactly 8 MiB from bounded records, +1, false metadata, over/underrun and job/
file/role changes. Both capture modes scan retained artifacts, WAL/staging and
prohibited hashes. A slow or over-fragmented transport must surface bounded failure,
not be mislabeled as a successful controlled fixture.

Independent code review and exact-source Level 3 local/hosted qualification remain
separate from focused implementation checks. Preserve all seven strict R gates,
the installed seventh gate's 180-second deadline, historical coverage/floor,
resource/memory/cleanup bounds and Windows/macOS process correctness. A separate
key-safe runner gate must attest writable roots, immutable Git/temp, disabled
external networking for offline work and private scratch. **Live authorization is
another later gate**: exact reviewed source/plan, account, routes/models/cases,
per-row output caps and cumulative jobs/tokens/spend/time/retention/cleanup must be
approved before any credentials or provider calls. Earlier study consent does not
transfer. No implementation, offline fixture, qualification command or SDK match
authorizes paid use, publication or merge.
