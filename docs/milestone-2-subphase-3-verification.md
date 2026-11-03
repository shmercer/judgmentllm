# Milestone 2 Subphase 3 — submission and recovery verification

Branch: `codex/milestone-2-submission-recovery`. Base: freshly fetched
`origin/main` at `8a6b4ca2ca444dc15242ea202dc61319f84fd980` (merged PR #5).
The initial checkout was clean. Verification below distinguishes local results
from the preserved cross-platform CI requirements.

## Architecture and scope

Subphase 3 connects the existing immutable `PreparedRun` source/dependency
pipeline, persistence repository, artifact store, ownership lock, validators and
schema-version-1 normalizers to an internal bounded executor and synthetic mock
provider. Structural task, pair-occurrence, request and orientation identities
remain authoritative. Public lifecycle CLI controls remain Subphase 5 work.

The provider boundary separates scoped capabilities, exact serialized request
preparation, submission, authoritative reconciliation/retrieval, and response
parsing. Prompt, payload, logical request, attempt, submission/correlation and
observation identities remain distinct. Requested generation settings must appear
in the actual supported mock payload; unspecified controls remain omitted.

The deterministic mock uses its own typed canonical JSON protocol. It performs
no network requests, uses no credentials, and incurs no billing. Real provider
adapters and managed batches remain deferred.

The principal additions are `providers/protocol.py` (typed boundary),
`providers/mock.py` (canonical synthetic wire protocol), `providers/ledger.py`
(independent durable acceptance), and `persistence/receipts.py` (identifiable
response evidence). Execution is divided into `execution/core.py` for bounded
async orchestration, `execution/session.py` for operations on the dedicated I/O
thread, and `execution/processing.py` for existing validation/normalization.
Repository additions supply owner-authorized transactions and bounded ordered
lookups; artifact additions publish immutable named receipt references.

## Durability, ownership and evidence

Executable request membership and exact dependency, prompt and payload
commitments must be durable before submission. The `submitting` transition is
committed immediately before the adapter call and means acceptance may be
unknown. Preparation and local request identities alone never establish remote
acceptance.

The mock acceptance ledger is independent of the executor's journal. An accepted
record binds correlation/submission identity, exact payload hash, stable accepted
observation identity and a nonsensitive reproducible recipe/reference. Repeated
retrieval must preserve acceptance count. A deterministic recipe without an
accepted ledger record is not observation evidence.

The executor holds `ExecutorOwnership` for its lifetime. Its active owner token
must authorize claims and advancement; stale heartbeat metadata never permits
takeover. Fixed workers and bounded pending/result queues provide backpressure.
SQLite transactions remain short and do not span rendering, artifact I/O or
provider calls. Pending request capacity is exactly twice concurrency and result
capacity is concurrency. All SQLite/source/artifact work stays on a dedicated
I/O thread with thread-owned connections, while the event loop runs fixed workers.

Full capture retains verified prompt, payload and response artifacts. Identifiable
response receipts support binding a published response to its original submission
after a crash. Missing, corrupt, mismatched or conflicting evidence must block
recovery instead of being silently accepted.

Minimal capture never writes rendered prompts, payload bodies or raw responses,
including temporary files, SQLite/WAL rows, receipt files or ledger records.
Parsing, validation and normalization occur in memory. Only permitted identity/
hash metadata and validated results, including authorized explanations, persist.
A receipt hash cannot recover a lost observation; authoritative retrieval is
required until results have committed.

## Recovery and fault-injection evidence

Deterministic tests cover each requested durable/external
boundary: before membership creation; artifact publication before preparation
commit; durable preparation before claim; claim before submitting; submitting
before adapter call; acceptance before acknowledgment; acknowledgment before
observation; acknowledgment sidecar publication before its transaction; full receipt publication
before observation; observation before
normalization; interrupted result transaction; and result commit before final
state/reporting.

For each boundary, independent-process tests establish safe reclaiming of
pre-submission work, recovery of the same accepted observation, or an unresolved
state that prohibits resubmission. Minimal-capture response-loss tests must also
cover receipt metadata, parsing, validation, in-memory normalized records and
transaction rollback. Retrieval unavailability must leave the original submission
unresolved without fabricated `invalid`, `refused` or `errored` results.

Additional tests cover authoritative nonacceptance, unknown timeouts,
invalid/refused/provider-error observations, receipt corruption/conflicts, source
mutation, current capability restrictions, missing/corrupt artifacts, competing
executors, owner-token enforcement and repeated postcommit recovery. Independent
mock acceptance counts are the acceptance oracle.

| Boundary | Required recovery and independent acceptance evidence |
|---|---|
| Before request creation / preparation artifacts only | Recreate the same preparation; zero acceptances before recovery. |
| Durable preparation / claim before submitting | Reclaim original prepared work; the submission call has not occurred. |
| Submitting committed before adapter call | Authoritative lookup may establish nonacceptance; no automatic retry or fabricated result. |
| Mock acceptance before acknowledgment | Recover the original accepted observation; acceptance count stays one. |
| Acknowledgment / received response before observation | Retain accepted certainty; retrieve the same observation or remain unresolved. |
| Full receipt before observation transaction | Verify identity and exact retained bytes, then bind the receipt without another acceptance. |
| Observation / parsing / validation / normalization before result commit | Full capture reprocesses verified receipt bytes; minimal capture requires authoritative retrieval. |
| Interrupted result transaction | Roll back the incomplete result; reprocess original evidence, never resubmit. |
| Result committed before final state/report | Reuse immutable result records and repair summaries; count stays one. |

The focused safety review added twenty-one passing regressions for changed adapter
request/dependency/provider/endpoint/model/generation metadata, on-disk manifest
corruption/replacement before preparation or submission, changed original sources
after claim, newly unsupported generation controls, and local `ValueError` faults
during parsing/validation. Local faults leave the accepted observation
without fabricating an invalid model result; recovery normalizes that same
accepted observation. Current capability changes block submission while preserving
the original sealed manifest. These tests passed, and their Ruff
and strict Pyright checks passed.

Privacy verification inspects decoded database records and raw SQLite/WAL,
all runtime artifacts/receipts/ledgers/staging and temporary files, logs, stdout
and stderr. Separate synthetic source, prompt, payload-only, raw-only, authorized
explanation and credential sentinels distinguish permitted result text from
forbidden retention.

Acknowledgments also publish strict immutable metadata binding the accepted remote
ID and expected observation key. Recovery can replay a published acknowledgment
whose SQLite transaction did not commit, including when lookup is unavailable.
Every retrieved observation must match that accepted key; conflicting identities
remain blocked. The ledger checks its service namespace and acceptance row inside
one transaction, rejecting ledger replacement between request validation and
lookup. Cancellation drains an in-flight acceptance transaction before releasing
the executor lock.

The process-death fixture uses `os._exit` across thirteen boundaries for both
capture modes, then reopens independently. Pre-boundary interruptions have zero
acceptances before recovery and one afterward. A crash after `submitting` but
before the call remains zero after authoritative nonacceptance, without a retry
or result. Accepted and postcommit windows retain exactly one acceptance.
The 32 process tests also cover ownership exclusion, changed sources, missing or
corrupted receipts/raw artifacts, unavailable retrieval, and transaction rollback.

## Local quality gates and interoperability

Local environment: Linux, Python 3.12.3, SQLite 3.45.1, R 4.6.2, jsonlite 2.0.0.
Final suite command: `UV_CACHE_DIR=/tmp/judgmentllm-uv-cache JUDGMENTLLM_R_STRICT=1
uv run --offline --no-sync pytest -q` (one shell command).
The first direct `.venv/bin/pytest` run omitted the installed CLI from `PATH`,
failing two unchanged entry-point tests; the standard `uv run` invocation passes
those tests. Initial wildcard distribution smoke also included stale `0.1.0a1`
artifacts; final checks target the newly built `0.1.0a2` files explicitly.

| Check | Result |
|---|---|
| Ruff lint and format check | Passed: `ruff check .`; `ruff format --check .`, 103 files. |
| Strict Pyright | Passed across source, tests and scripts; zero errors/warnings. |
| Pytest and at least 90% coverage | **1,122 passed**, no skips, 195.83 seconds. Combined coverage **96.43%**; statements 4,697/4,826 (97.33%), branches 1,405/1,502 (93.54%). |
| Offline lockfile validation | Passed: `uv lock --check --offline`, 93 packages; no dependency/lock changes. |
| Wheel and source builds | Passed: `uv build --offline`, wheel and sdist for `0.1.0a2`. |
| Strict distribution metadata checks | Passed: `twine check --strict` on both current distributions. |
| Clean wheel and source installation smoke | Passed both current distributions in isolated environments outside the checkout, including mock execution/recovery with no optional providers installed. |
| Four existing strict R/jsonlite checks | Passed in the full suite with `JUDGMENTLLM_R_STRICT=1`: export, persistence, generation, source preparation. |
| New separate-process execution/R check | Passed targeted and final full-suite strict checks; actual persisted observations/results survive independent-process recovery/export and R import. |
| Fixed workers, bounded queues and memory regression | Passed both required bounded-prefix subprocess regressions; measurements below. |
| Diff and final commit review | `git diff --check` passed; reviewed scope and unchanged registry/result/export schemas. |

The new execution fixture consumes actual executor-generated results and
attempt/submission/observation evidence, reopens them in another Python process,
and verifies them in base R with jsonlite. Character IDs with leading zeros,
original/presented pair orientation, complete probabilities/category mappings,
score kinds, missing values and provenance must survive unchanged. The existing
four R checks remain required. Missing Rscript/jsonlite or failed assertions fail
the Ubuntu/Python 3.11 gate.

The targeted execution/R fixture executed eight synthetic judgments across four
full/minimal rubric/pairwise runs. A second Python process reopened completed
runs and verified unchanged immutable result digests and exactly eight independent
ledger acceptances, each with acceptance count one. R verified attempt/submission/
observation/acceptance joins, category-versus-modal distinctions, complete
nonuniform score mappings/probabilities, leading-zero IDs, retained/missing paths,
explanations, provenance and reversed pair presentation.

## Resource measurements

Local Linux/Python 3.12.3, concurrency 4, 4 KiB synthetic responses; each fixture
intentionally interrupts after 64 committed results. Python allocation tracing
begins after source/index/configuration initialization. Native RSS is process
lifetime high-water including initialization.

| Input | Peak Python bytes | Peak native RSS bytes | Execution seconds |
|---|---:|---:|---:|
| 200 responses × 20 traits | 2,083,450 | 54,550,528 | 10.22 |
| 2,000 responses × 20 traits | 2,066,049 | 55,566,336 | 10.25 |
| 200 responses × 200 traits | 1,653,189 | 55,357,440 | 64.55 |

All three runs create six fixed executor tasks (producer, consumer, four workers)
and peak at seven concurrent asyncio tasks including short shielded acceptance
writes. At interruption, 69 requests are prepared and 67 accepted; those extra
in-flight acceptances remain durable recovery work, not discarded judgments.
Both required memory regressions preserve 32 MiB Python and 256 MiB native limits,
small/large memory deltas, task/queue bounds, and no upfront task-product creation.
The 180-second child deadline allows platform runtime margin without relaxing
memory or safety assertions. These are bounded-prefix regressions, not complete
4,000/40,000-task or later large-fixture qualification.

## Limitations, deviations and Subphase 4 handoff

Runtime stores remain single-machine local-filesystem state and must live outside
Dropbox/cloud synchronization or network mounts. Artifact/receipt publication
requires atomic no-overwrite hard links and fails safely when unsupported.
Windows has no standard-library directory-fsync equivalent. No distributed
ownership or forced stale-heartbeat takeover is provided. Source safeguards do
not claim a filesystem-wide atomic snapshot against undetectable concurrent
changes. Accepted fixture recovery needs the original mock service ledger and
matching fixture configuration; neither hashes nor arbitrary deterministic
recomputation can replace those records. No real-provider exactly-once guarantee
is claimed by this offline mock qualification.

Linux/macOS/Windows × Python 3.11/3.15 CI remains required and unchanged. Local
checks used Linux/Python 3.12.3; the six CI combinations were not run locally
and remain review/CI qualification. Local results cannot qualify unexecuted platforms. Native RSS and Python allocations must both be
measured for increasing source/trait fixtures. The approximately 5,000-response ×
20-trait, 256 MiB objective and 100,000-response stress qualification remain
Subphase 6 work.

The local sandbox blocks socketpair sends used by asyncio to wake its event loop
from I/O threads. Initial sandboxed execution probes timed out before their async
workers started. The targeted tests passed with sandbox escalation permitting
local IPC; provider execution and R verification remained offline and credential-
free. No timeout, ownership or safety assertion was weakened to accommodate this
environment restriction.

No scope deviation: schema versions, public IDs, category semantics, generation
commitments, and established R export columns remain unchanged. Package version
remains `0.1.0a2`; the complete milestone retains its planned `0.1.0a3` target.
No runtime dependency or schema migration was added. The internal source reopening
API gained an optional owner token so execution can hold its lock while verifying
explicit frozen-snapshot recovery without recursively acquiring ownership.

Subphase 4 receives durable exact-payload preparation, conservative submission
certainty, independent acceptance/retrieval evidence, receipts, supported
single-member normalization and recovery. It owns complete grouped-member
matching and salvage, failed-member subset selection, automatic retry/backoff,
explicit subset retries, generalized cancellation and export lifecycle services.
Unsupported grouped execution must fail before submission in this subphase.
No push, merge, release or Subphase 4 implementation is part of this delivery.

## PR #6 portability and SQLite warning corrections

The original [CI run 41568469641](https://github.com/shmercer/judgmentllm/actions/runs/41568469641)
passed Ubuntu/macOS on Python 3.11/3.15 and failed both Windows configurations
in live privacy inspection. Its `read_bytes()` traceback did not identify the
file whose `read()` failed. Investigation distinguishes the permanent one-byte
executor lock from SQLite database/WAL/SHM files; no runtime directory receives
a blanket inspection exception.

The privacy test now identifies the exact lock through `Repository.run_directory`.
A live permission failure is deferred only on Windows for that exact non-symlink,
one-byte file, then the complete store is inspected after release and the lock
must contain exactly one NUL byte. Unexpected permission failures report only
the relative runtime path and remain failures. Native lock regressions verify
platform behavior directly, and injected loss-window tests must prove they reached
the intended fault boundary rather than accidentally accepting an earlier error.
Windows runs perform these targeted privacy checks before the complete suite.
Production OS ownership and all submission/recovery contracts are unchanged.

The sole SQLite leak was test-only: `_accepted()` in
`tests/test_execution_processes.py` used the connection transaction context without
closing the connection. The original process fixture invoked it exactly 86 times,
matching the Python 3.15 warnings. It now uses `contextlib.closing`; regressions
verify explicit closure after both a successful query and a query error. The audit
found explicit cleanup in the production repository/preparation/execution services,
all mock-ledger connection paths, and other Subphase 3 fixtures. Intentional
`os._exit` process-death tests remain essential recovery coverage.

Targeted local verification passed **48 tests** with both
`-Werror::ResourceWarning` and
`-Werror::pytest.PytestUnraisableExceptionWarning`. The complete CI matrix now
uses those same error filters; no warning suppression was introduced. Local full
verification passed **1,135 tests**, no skips or warnings, in 195.54 seconds with
**96.43%** combined coverage and both warning categories treated as errors. All
five strict R/jsonlite tests, Ruff lint/format (104 files), strict Pyright, offline
lockfile validation, wheel/sdist builds, strict metadata checks and clean installed
wheel/sdist smoke passed. Actual Windows/path and final matrix evidence remain
pending remote verification; Linux results alone do not establish them.
