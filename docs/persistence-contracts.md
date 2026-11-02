# Internal persistence contracts — Milestone 2, Subphase 1

The approved [execution plan](milestone-2-execution-plan.md) is preserved as
reviewed. This document describes the implemented durable foundation only.
`judgmentllm.persistence` is an internal Python package. Its records use version
1 independently of the unchanged assessment and normalized-result schemas.
There is no executor, source loader, provider protocol, mock provider, or new
lifecycle CLI command in this subphase.

## Registry and transactions

`Repository(store)` opens `store/registry.sqlite3` with foreign keys, WAL,
`synchronous=FULL`, and a bounded busy timeout (default 5,000 ms). Each process
or thread opens its own connection. Opening verifies both the migration
checksum and the actual schema-object digest. Unknown/newer/unversioned existing
schemas fail; there is no implicit migration. SQLite JSON functions are required
by the schema's result-identity checks.

`transaction()` uses `BEGIN IMMEDIATE` for the outer write and savepoints for
composed operations. Exceptions, including interruption, roll back the current
boundary. Methods can therefore be composed into one atomic result group without
implementing grouped-output recovery here. Provider calls, rendering, artifact
publication, and waiting belong outside transactions. Independently opened
readers see committed data; `summary()` does not commit a caller's transaction.

The connection attribute is available for internal schema tests and migrations.
The typed repository methods are the validation boundary. Direct SQL is not an
alternative normalized-result validation API: SQL guards identities/evidence,
while the existing Pydantic models validate complete scoring semantics.

## Tables and constraints

| Tables | Contract |
|---|---|
| `schema_migrations` | Append-only version, migration checksum, schema digest, application time. |
| `runs` | Run identity/configuration immutable; manifest can be sealed once; lifecycle and ownership diagnostics mutable. |
| `artifacts` | Run-scoped exact digest, byte length, media type, retained relative path or hash-only identity; append-only. |
| `sources`, `input_records` | Reserved source identities, generations, canonical record hashes, byte locators and structural references. No ingestion APIs. Source records freeze when the run leaves `created`; indexed records are append-only. |
| `tasks` | Immutable trait/item/pair identity and ordinal; checked lifecycle. Task kind matches its run. |
| `requests`, `request_tasks` | Immutable original request identity and ordered membership. Membership is sealed atomically and cannot be extended or replaced. |
| `attempts`, `attempt_tasks` | Distinct attempt identity/number, exact prompt/payload commitments, membership digest, optional authorization and reserved nullable usage counts. Sealed ordered membership must be a subset of its original request. |
| `submissions` | Unique correlation identity and attempt/batch target, matching prepared payload, certainty, remote identity. Known acceptance and remote identity cannot be downgraded or replaced. |
| `observations` | Append-only established provider response/refusal/error/cancellation metadata. Requires an accepted submission; raw bytes may intentionally be absent. |
| `submission_failures` | Append-only definitive local submission failure/cancellation evidence linked to its attempt, submission, and failure audit event. Does not invent a provider observation. |
| `results` | Append-only validated normalized records, unique per run/task/attempt, with observation or definitive failure evidence. |
| `retry_operations` | Immutable authorization identity, selection digest and explicit completed-task permission. No retry execution or task selection is implemented. |
| `events` | Append-only typed event kind, entity identity, state transition, owner reference and timestamp. No arbitrary message or payload dictionary. |
| `batches`, `batch_members` | Reserved future batch identities/membership; no batch submission methods. |
| `exports` | Reserved export publication/high-water metadata; published rows immutable. No lifecycle export service yet. |

Primary/foreign/unique constraints are run-scoped, except correlation IDs, which
are globally unique within the registry. Secondary indexes support state/order
traversal, item/pair lookup, attempt eligibility, reverse membership, remote-ID
lookup, result history, and audit ordering. Deletion is restricted; this phase
does not provide retention/deletion maintenance.

The separate `submission_failures` table makes the approved alternative failure
evidence explicit rather than overloading provider observations. It is a narrow
schema refinement, not a second result format.

## Typed operations and transitions

Execution records are strict, frozen, versioned models in `persistence.models`.
Repository boundaries reconstruct current deep data before accepting it, including
copies that attempted to inject undeclared fields. Existing assessment models
remain unchanged.

Operations include run creation/sealing; artifact registration/verification;
task/request/attempt/submission creation; explicit state transitions; observation
and definitive failure recording; result insertion/iteration; authorization
identity registration; ownership metadata; events; and scalar summaries.
They record supplied facts and never infer that a provider accepted a request.

Valid transitions are centralized in `TRANSITIONS` and checked by both repository
code and SQL triggers. Records must start in their initial state. No-op state
transitions through the typed API are rejected. Reactivation of completed work
requires an authorization explicitly permitting completed-task retries.

Run states retain the approved `created/submitted/running/completed/partial/failed/
cancelled` vocabulary. `submitted` describes sealed local intent, not remote
acceptance. Request/task/attempt states distinguish preparation, active work,
terminal outcomes, and uncertainty. Submission states distinguish `prepared`,
`submitting`, `acknowledged`, `confirmed_not_submitted`, `uncertain`, and `resolved`.
The subphase does not automatically transition abandoned submissions or reconcile
remote outcomes; later services must do so conservatively.

## Successful and unsuccessful results

`append_result()` accepts the existing fully validated rubric/pairwise models for
`complete`, `invalid`, `refused`, `errored`, and applicable `cancelled` outcomes.
It verifies identity/membership, provenance consistency, evidence classification,
and uniqueness. Unsuccessful results cannot claim a primary score. There is no
implicit selection of a first/latest successful attempt.

Provider-derived results reference an established observation: ordinary responses
support complete/invalid outcomes, refusals support refused outcomes, provider
errors support errored outcomes, and confirmed cancellations support cancelled
outcomes. A conclusive invoked-but-unaccepted submission may instead store an
`errored` result with `error_kind=transport_not_submitted`, a null observation
reference, and a required definitive-failure reference. A claimed or uncertain
submission alone is insufficient evidence. Provider errors are not local
transport failures.

`iter_results()` captures a result high-water mark, then reads bounded pages,
revalidates normalized data, and verifies its semantic digest. Existing JSONL,
canonical CSV and R writers consume that iterator unchanged and retain failures.
Manually supplied synthetic groups in tests prove atomic storage, not an
implementation of parsing or recovering grouped model output.

## OS ownership

`ExecutorOwnership(repository, run_id)` acquires a nonblocking lifetime advisory
lock (`flock` on Unix; a one-byte `msvcrt` lock on Windows), then records a fresh
token, PID, host, acquisition time, and heartbeat. Metadata replacement is valid
only while holding the OS lock. `heartbeat()` and release use token guards.
Do not call the repository's low-level ownership-metadata methods as a lock API.

Heartbeats never expire ownership. Process death releases the OS lock; a later
owner replaces stale diagnostics and appends `owner_recovered`. Lock files are
permanent and must not be removed/replaced. File descriptors are noninheritable
across exec, and forked children cannot explicitly unlock their parent. A
fork-only child retaining an unused inherited descriptor can conservatively
delay takeover until it exits. No forced/distributed takeover is implemented.

Run IDs are arbitrary validated strings, so filesystem run directories use
`runs/<sha256(UTF-8 run_id)>`. Use `run_directory()` rather than constructing paths
from IDs. This avoids separators/traversal without changing original IDs.

## Artifacts and hashing

`ArtifactStore(run_directory)` stores exact bytes as
`objects/<first-two-hash-characters>/<sha256>`. Publication writes a same-filesystem
temporary file, flushes and fsyncs it, verifies its digest, and atomically links
it into its final location without replacing existing bytes. Existing objects
are verified, never silently repaired. File/directory flushing is performed
where supported; Windows has no standard-library directory-fsync equivalent.
Filesystems lacking hard-link publication fail safely rather than falling back
to replacement. A crash may leave an unreferenced object/temporary file.

`put`, `verify`, and `read` preserve byte identity. Missing/corrupted retained
objects raise safe integrity errors. `hash_only()` describes intentionally absent
bytes; it never implies recoverability. Registered identity/retention cannot be
silently changed. `get_artifact()` and `get_manifest()` verify retained bytes.
Sealing here stores only a retained manifest reference; phase 2 owns its complete
content schema, source commitments, and fixed `manifest.json` publication.

`canonical_bytes()` revalidates model snapshots and emits sorted compact JSON as
UTF-8, preserving list order, defaults, nulls, Unicode and newline distinctions.
It rejects nonfinite/invalid data and arbitrary string conversion.
`semantic_digest(domain, value)` hashes `[1, domain, value]` in that encoding;
`bytes_digest(bytes)` hashes exact bytes without an envelope. Neither changes
Milestone 1 structural IDs.

Minimal capture policy enforcement arrives with source/preparation services;
the generic byte store is not a privacy policy engine. Metadata has no arbitrary
assessment-text field. Normalized outputs, including explanations, remain
authorized sensitive data even under minimal capture. Hashes are identities,
not response evidence or anonymization.

## Subphase 2 handoff

Implement consistent source discovery/freezing, staged/indexed input access,
full/minimal policies and complete manifest construction next. Reuse the reserved
source tables and artifact identities; keep provider/execution work for phase 3.
Build complete byte locators for multiline CSV through shared strict readers.
Verify source generations and record hashes before using minimal-mode locators.
Never put source text in minimal-mode tables, WAL, temporary artifacts or events.

Prepare request dependencies incrementally and bind exact prompt/payload bytes
before submission in later phases. Retain original request membership and use
attempt subsets for retries. Minimal response recovery must retrieve the same
authoritative observation or remain unresolved; a request hash is insufficient.

The package remains at its existing development version during this independently
reviewable subphase; the approved `0.1.0a3` target remains the next milestone
package version. Public assessment, configuration, prompts, planning, CLI and
export schemas are unchanged. See the [verification record](milestone-2-subphase-1-verification.md).
