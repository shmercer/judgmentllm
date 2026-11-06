# Internal persistence contracts — Milestone 2 mock execution and lifecycle

The approved [execution plan](milestone-2-execution-plan.md) is preserved as
reviewed, with the generation requirement added at the start of Subphase 2.
This document describes the implemented durable foundation, generation
commitments, and immutable source-preparation services.
`judgmentllm.persistence` is an internal Python package. Its records use version
1 independently of the unchanged assessment and normalized-result schemas.
Subphases 3–4 implement mock execution, conservative recovery, retries, cancellation
and immutable exports. Subphase 5 exposes those services through public Python
functions and the versioned CLI without changing registry schema version 1.
Subphase 6 adds qualification tooling around these services. Its exact-head
scale and compatibility evidence remains pending; no new registry, manifest,
assessment, result, export or control schema version is introduced.

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
| `sources`, `input_records` | Exact source identities/generations, canonical record hashes, logical byte locators, and permitted structural references. Typed preparation/index operations populate them; source records freeze when the run leaves `created`; indexed records are append-only. |
| `tasks` | Immutable trait/item/pair identity and ordinal; checked lifecycle. Task kind matches its run. |
| `requests`, `request_tasks` | Immutable original request identity and ordered membership. Membership is sealed atomically and cannot be extended or replaced. |
| `attempts`, `attempt_tasks` | Distinct attempt identity/number, exact prompt/payload commitments, membership digest, optional authorization and reserved nullable usage counts. Sealed ordered membership must be a subset of its original request. |
| `submissions` | Unique correlation identity and attempt/batch target, matching prepared payload, certainty, remote identity. Known acceptance and remote identity cannot be downgraded or replaced. |
| `observations` | Append-only established provider response/refusal/error/cancellation metadata. Requires an accepted submission; raw bytes may intentionally be absent. |
| `submission_failures` | Append-only definitive local submission failure/cancellation evidence linked to its attempt, submission, and failure audit event. Does not invent a provider observation. |
| `results` | Append-only validated normalized records, unique per run/task/attempt, with observation or definitive failure evidence. |
| `retry_operations` | Immutable authorization identity, selected membership/prior-outcome digest and explicit completed-task permission. The shared retry service prepares exact subsets under ownership. |
| `events` | Append-only typed event kind, entity identity, state transition, owner reference and timestamp. No arbitrary message or payload dictionary. |
| `batches`, `batch_members` | Reserved future batch identities/membership; no batch submission methods. |
| `exports` | Internal all-attempt export publication/high-water metadata; published rows immutable. Publication references retained exact output bytes and never overwrites earlier snapshots. |

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
The Subphase 1 foundation supplies these transitions; Subphase 3 execution now
reconciles abandoned submissions conservatively using authoritative evidence.

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

`iter_results()` captures a result high-water mark (or accepts an explicit
`high_water_mark`), then reads bounded pages,
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

`put_stream(chunks, media_type, *, expected_sha256=None, expected_length=None)`
incrementally hashes and publishes byte chunks. `staged_stream(...)` holds verified
private bytes for parsing/indexing and exposes explicit publication; its context
cleans its own pending file. Verification uses bounded chunked reads. Interrupted
process leftovers remain inert and unreferenced; discovery never promotes them.

`put`, `verify`, and `read` preserve byte identity. Missing/corrupted retained
objects raise safe integrity errors. `hash_only()` describes intentionally absent
bytes; it never implies recoverability. Registered identity/retention cannot be
silently changed. `get_artifact()` and `get_manifest()` verify retained bytes.
Preparation publishes the fixed `manifest.json` reference atomically without
replacement and seals its retained content-addressed identity in the repository.
A retained generation-only fixture is not an executable manifest.

`canonical_bytes()` revalidates model snapshots and emits sorted compact JSON as
UTF-8, preserving list order, defaults, nulls, Unicode and newline distinctions.
It rejects nonfinite/invalid data and arbitrary string conversion.
`semantic_digest(domain, value)` hashes `[1, domain, value]` in that encoding;
`bytes_digest(bytes)` hashes exact bytes without an envelope. Neither changes
Milestone 1 structural IDs.

The preparation service enforces minimal capture; the generic byte store is not
a privacy policy engine. Metadata has no arbitrary
assessment-text field. Normalized outputs, including explanations, remain
authorized sensitive data even under minimal capture. Hashes are identities,
not response evidence or anonymization.

## Generation manifest section

`judgmentllm.persistence.generation` provides the internal frozen version-1
`GenerationCommitment` and two helpers:

- `build_generation_commitment(spec, *, capabilities=None)` deep-validates and
  snapshots exact provider/endpoint/model scope, requested settings, original
  capability evidence and `requested_sha256`. Invalid generation preflight fails
  before returning a section.
- `validate_generation_compatibility(commitment, spec, *, capabilities=None)`
  returns the existing `ValidationReport`, rejecting changed scope/settings and
  applying current supplied capability restrictions. It does not reuse frozen
  evidence as current authorization.

`requested_sha256` uses the existing domain-separated semantic digest with
`generation-request-settings`, exact provider/endpoint/model and sparse requested
numeric settings. It excludes capability evidence. Settings preserve explicit
integer/float values and omission; unset settings serialize as an empty object
inside this section. Source, capture and pairing-seed commitments belong to the
enclosing complete manifest rather than this generation digest.

Retain the original evidence in immutable manifest bytes. Changing evidence
can change those bytes without changing requested-settings identity. Before
submission or resume, validate against the latest available applicable evidence;
newly known incompatibilities block continuation while the original snapshot
remains intact. Unknown support obeys the run's `strict_capabilities` policy and
does not establish fixture or live qualification.

The section may be retained under full or minimal capture because implemented
controls are numeric. Future text-bearing extensions such as stop sequences
must obey capture policy rather than being copied into minimal metadata.
Repository/artifact tests may retain a generation-only fixture to demonstrate
immutability, hashing and compatibility checks. That fixture neither completes
source freezing/full preflight nor authorizes submission. Actual provider payload
hashes continue to hash actual prepared bytes when adapters exist; no cloud
body construction or operational resume is implemented here.

## Immutable preparation and reopening

`judgmentllm.preparation.prepare_run(spec_or_path, *, store, run_id,
capabilities=None, exhaustive_rendering=False)` validates the complete assessment/input set, discovers all
configuration/rubric/criteria/exemplar/template/response/pair files, builds ordered
record indexes in the existing source tables, and seals a typed version-1
`ExecutableRunManifest`. A direct Python `RunSpec` records its canonical identity
without fabricating an originating source-file hash. These preparation services remain internal; the public lifecycle APIs/CLI delegate
to them rather than implementing another validation or execution pipeline.

Source parsing, record validation, references, locators, hashes, and source-set
consistency are always checked in full before sealing. Normal render preflight
compiles the static assessment/grouping/template/settings context through the
shared renderer's syntax/AST/sandbox checks for every group or criterion, including
empty sources. Bounded full rendering uses the first verified real response per
rubric group or the first indexed pair per represented criterion, retaining actual
IDs and orientation. Empty/unrepresented contexts are compiled without evaluating
invented content or demanding hypothetical protected-block inclusion. The complete
response-by-trait rendering product is not performed; content-dependent failures
in later records remain request-boundary checks by default.

Each selected dependency must pass `PreparedRun.render(dependency)` before future
submission. This fully validates its verified real content through the existing
renderer; dependency iteration alone does not prove rendering success. Explicit
`prepare_run(..., exhaustive_rendering=True)` or
`validate_inputs(..., exhaustive_rendering=True)` additionally scans and renders
all actual requests, so those failures occur before sealing. This internal option
does not enter configuration/manifest identities or change compatibility, request
IDs, or generation commitments. Rendered prompts are not persisted under minimal
capture, including in the exhaustive diagnostic path.

Every file receives an initial descriptor/path signature and exact SHA-256/length.
Full capture copies into private staging, independently rereads the original,
verifies staging, and parses/indexes staged bytes. Minimal capture hashes/indexes
without staging source text. Both independently verify original bytes and stable
identity, validate locators/record hashes, and recheck the complete source set
before any manifest seal. Any detected change fails the attempt; a newly observed
generation is never silently adopted. The protocol does not claim a filesystem-wide
atomic snapshot against undetectable changes between checks.

CSV offsets span complete logical records, including quoted multiline fields.
Indexes retain verified header/parsing context. JSONL locators span complete
encoded records. Reconstruction uses shared strict parsers, checks IDs and
canonical hashes, and preserves leading-zero IDs. Pair response references use
indexed lookup and both referenced content identities enter request dependencies.

The manifest commits source provenance, normalized configuration/assessment,
ordered traits/category coding and exemplars, template/rendering settings,
indexes, grouping/orientation, provider/model/mode/tier/cache, generation and
original capabilities, capture/recovery policy, and software/schema/serializer
versions. Text-bearing content is represented by hashes/source references under
minimal capture. Exact file provenance is distinct from semantic configuration;
equivalent omitted/null/empty generation settings normalize identically.

`open_prepared_run(run_id, *, store, spec=None, capabilities=None,
use_frozen_snapshot=False)` independently verifies the complete manifest and
sources before returning a `PreparedRun` context manager. It reconstructs verified
assessment context and provides `get_response`, `get_pair`, ordered iterators,
and incremental `iter_request_dependencies`. Minimal sessions keep verified
source handles and check their generation around selected record reads. Changed
settings, content, missing sources, corrupt snapshots, incompatible serializers,
or newly restrictive current capabilities block further preparation.

Full capture blocks changed/missing originals by default. Explicit
`use_frozen_snapshot=True` authorizes only the original verified snapshots for
that session and records an audit trail; it never substitutes changed content.
Required missing/corrupt snapshots always block. Minimal capture requires unchanged
originals and cannot reconstruct them from hashes. Never put source text in
minimal-mode SQLite/WAL, temporary artifacts, events, manifests, or diagnostics.

Place the runtime store on a local filesystem, outside Dropbox/cloud sync or a
network mount. SQLite WAL and advisory ownership do not establish distributed
durability. Artifact publication requires atomic no-overwrite hard links;
unsupported filesystems fail safely. Flush/fsync is used where supported;
Windows has no stdlib directory-fsync equivalent. No overwrite fallback is used.

## Historical Subphase 3 handoff

This handoff records the preparation-stage boundary. The following sections
describe the subsequently implemented execution and public lifecycle services.

Subphase 3 consumes request dependencies incrementally and binds exact prompt/
payload bytes before submission. Retain original request membership and use
attempt subsets for retries. Minimal response recovery must retrieve the same
authoritative observation or remain unresolved; a request hash is insufficient.

The package remained `0.1.0a2` during Subphases 1–5; the approved `0.1.0a3`
transition belongs to the Subphase 6 candidate and its compatibility qualification.
Generation configuration and capability interfaces are additive;
no-generation serialization/IDs/diagnostics and normalized result/export schemas
remain unchanged. The [Subphase 1 verification record](milestone-2-subphase-1-verification.md)
continues to describe its earlier durable-foundation checks only.
The [generation verification record](milestone-2-generation-verification.md)
records its earlier foundation only. The
[source-preparation verification record](milestone-2-subphase-2-verification.md)
records complete input preparation and the then-remaining submission-boundary work.

## Subphase 3 execution ownership, receipts, and provider evidence

The internal `judgmentllm.execution` service executes prepared independent/grouped
rubric and pairwise mock requests. It reuses the version-1 registry, memberships, result schemas,
validators, and normalizers. No registry migration or result/export-column change
is introduced.

`execution_transaction(run_id, owner_token)` checks ownership inside the short
write transaction and attaches that owner to its events. Executable preparation,
claims, submission-boundary changes, observations, and result commits use it.
The lower-level foundation methods remain available for existing preparation and
synthetic persistence fixtures; they are not an executor ownership API. Typed
record/membership/result reads and bounded keyset iterators support reopening.
The existing run checkpoint records the next durable preparation ordinal, not
completion or permission to skip prior uncertain work.

One I/O thread owns preparation, SQLite connections, source handles, artifacts,
and receipts. It acquires `ExecutorOwnership` before reopening the manifest and
retains it through cleanup. Async provider workers and queues remain bounded:
request queue capacity is twice concurrency; result queue capacity is concurrency.
No request/task product is materialized. Every selected request is rendered and
revalidated again at the final submission boundary. Source signatures and selected
record hashes, disk manifest/object identity, current capability restrictions,
generation, and exact payload commitments are checked without modifying the seal.

Tasks, original request membership, attempt identity, byte commitments, and local
submission intent commit before claiming. The `submitting` transaction commits
immediately before the adapter call and means the call might have occurred.
Acceptance requires authoritative evidence. Pre-boundary work may reuse the same
preparation; post-boundary work must reconcile. Authoritative nonacceptance is
recorded independently of missing acknowledgments; it does not authorize this
subphase to allocate another attempt. A conclusively failed invoked transport
operation uses `submission_failures`, never a provider observation.

`ResponseReceipt` binds run/attempt/submission/correlation/payload identities to
observation identity/key/kind, receipt time, and exact raw artifact identity.
Raw objects and canonical receipt references publish atomically without overwrite
before the observation transaction. Recovery verifies all identities and retained
bytes; mismatches or corruption block recovery. Observation insertion and
normalization/result commits are separate durable boundaries. The result commit
atomically writes the result and corresponding task/request/attempt/submission
states. An interrupted transaction rolls back; committed records are reused.

Full capture retains exact prompt/payload/raw artifacts. Minimal capture retains
only their hash/length identities and receipt metadata, parsing/normalizing in
memory. A hash-only observation still requires retrieval after response loss.
Authorized normalized explanations remain permitted result data. No raw bytes
are written to minimal artifacts, receipts, WAL, temporary files, or ledger rows.

The mock service uses a separate SQLite acceptance ledger with FULL synchronous
commits. Its service namespace is bound into actual payload bytes, preventing a
replaced ledger from claiming nonacceptance for old preparations. The ledger
records immutable acceptance/nonacceptance, payload/correlation identity, stable
observation key, acceptance count, retrieval status, and a non-sensitive numeric/
enum recipe with response and fixture hashes. Text-bearing fixture configuration
must be supplied again in memory. Retrieval requires an existing accepted row
and verifies its exact response hash; it never increases acceptance count. Missing
reconciliation or retrieval leaves work blocked and `recovery_required` true.

See [Subphase 3 verification](milestone-2-subphase-3-verification.md) for fault
windows, privacy checks, independent-process/R tests, and the Subphase 4 handoff.

Acknowledgment sidecars retain strict accepted remote/observation identities before
the acknowledgment transaction. Recovery replays verified sidecars before remote
lookup, keeping known acceptance even when lookup is unavailable. Retrieved
observation keys must match durable acknowledgments. Completed runs require all
current logical tasks to be complete. Earlier unsuccessful attempt rows remain
in all-attempt histories after a successful explicit retry; they do not prevent
logical completion. Unresolved evidence remains independently reported by
`recovery_required`.

## Subphase 4 immutable export service

`judgmentllm.execution.exports.export_run(run_id, *, store, format="jsonl",
view="canonical", high_water_mark=None, page_size=128, na_string="NA")` is an
internal provider-free service. Supported combinations are canonical JSONL,
canonical CSV and R-compatible CSV. It streams the existing serializers over a
fixed result high-water mark, including every recorded successful and unsuccessful
attempt. It never reconstructs missing outcomes, selects a preferred attempt or
calls providers. Empty and incomplete runs can be exported.

Export identity commits run, format/view, selected high-water mark and applicable
R missing-value marker. Each report returns row count, exact SHA-256 and byte
length, an absolute immutable content-addressed artifact path and the current run
state/completeness. Current lifecycle status is independent of the immutable
selected result snapshot. Explicit high-water marks can reopen earlier snapshots
without modifying their bytes.

Output is serialized to private staging, flushed and atomically published through
the existing artifact store before a short transaction registers the artifact and
commits its `exports` publication. A process death before that transaction leaves
no published export; an unreferenced artifact is inert. Concurrent identical
exports converge on one publication identity, conflicting content fails closed,
and existing published bytes are verified. Result pages and byte-copy buffers
remain bounded. Under minimal capture, staging and published files contain only
authorized normalized result data, including authorized explanations.

If identical bytes already identify a source artifact (including an empty source),
its media type and full/hash-only retention metadata remain immutable. The export
publication separately authorizes retention of its normalized output and reports
the verified canonical object path and export media type. It does not change the
original source's capture or recovery contract. Shared SHA-256 identities must
have the same exact byte length.

This service introduces no registry migration, result schema or R column changes.
Public `export_run_results()` and `results export` delegate to this service.

Confirmed cancellation is a distinct established provider observation kind. The
mock can return an authoritative `confirmed_cancellation` outcome bound to its
accepted submission and immutable ledger observation recipe. The shared
interpreter creates null-score `cancelled` results for that actual observation,
including every expected grouped member, and records its observation relationship
atomically. Receipt replay and authoritative retrieval recover the same evidence.
This mock outcome is explicit provider evidence; cancelling an asyncio task or
safe unsubmitted work never generates it. No provider-side cancellation call or
public cancellation CLI is introduced by this seam.


## Public lifecycle boundary (Subphase 5)

The public `validate_inputs`, `estimate_run`, `run_live`/`arun_live`,
`resume_run`/`aresume_run`, `list_runs`, `get_run` and `export_run_results`
services compose the existing preparation, execution, retry and export services.
The CLI calls these same public functions. No migration or normalized-output
schema change is introduced.

Inspection checks an existing registry before opening it, uses bounded queries,
and needs neither original assessment sources nor a provider adapter. Returned
run IDs remain original strings; absolute run directories are checked against
the existing hash-based mapping. Stored run state and an instantaneous OS-lock
probe remain separate. Inspection never repairs stale ownership or starts work.

`ReadOnlyRepository` opens the existing registry with SQLite URI `mode=ro`,
plus connection-local `query_only`. It verifies the existing schema version,
migration checksum and structural digest through the same verifier as the writer;
it never initializes, migrates, checkpoints or repairs a registry. Empty,
uninitialized, incompatible and malformed registries fail safely. Missing stores
still produce empty listings or `not_found` for addressed runs. Inspection creates
no application state, manifests, artifacts, run directories or lock files.
SQLite-owned WAL/SHM coordination is permitted: normal locks and committed,
uncheckpointed WAL visibility are preserved, including during active execution.
Readers do not use SQLite's immutable or lock-bypassing modes.

Runtime retry selection and mock fixture controls do not enter RunSpec. Custom
mock ledgers and caller-owned fixture files must be supplied again on resume;
missing ledgers are rejected before construction. Service-identity, source and
exact payload commitments remain mandatory. Only current compatible evidence
permits recovery; matching recipes or hashes alone never establish observations.

Public summaries distinguish planned logical work from materialized rows,
current task states from historical result counts, and uncertain submissions from
acknowledged observations awaiting recovery. Public export reports return durable
absolute file paths; they preserve the same immutable snapshot and all-attempt
semantics described above. See [Python services](python-api.md) and
[CLI controls](cli-spec.md).


## Phase 2 versioned request commitments

An explicit v2 run uses versioned manifest/request commitment semantics while
v1 parsing, hashes, replay and verification remain unchanged. The SQLite registry
is reused without implicit migration. Before submission, the immutable request
commits the RunSpec/output-contract/renderer versions, exact ordered membership
and trait definitions, explanation/tie flags, requested layout and structured
output policy, resolved mechanism and selection diagnostic.

The ordered schema representation is retained with its digest and requested root
and member property-order sequences. It is distinct from sorted canonical
semantic hashing: `scores_first` and `explanations_first` cannot collapse to the
same presentation identity. Exact rendered prompt and mock payload bytes/digests,
requested generation settings, exemplar IDs/order, configuration/source hashes
and dependency identities remain bound to the same attempt.

Recovery reuses the original committed contract and text mechanism; changed
capabilities never silently change a prepared request. Contract, source, schema,
prompt or payload mismatches fail before another provider request. Full/minimal
capture policies and original source-integrity requirements remain enforced.
Minimal durable commitments retain bounded hashes/identifiers rather than
assessment source text. No credentials or credential-status evidence is recorded.
Phase 4B adds observed-order metadata, submission usage and authorized reasoning through separate versioned companion receipts described below.

## Provider evidence extension boundary (proposed, not implemented)

See the [consolidated assessment output design](assessment-output-design.md)
for explicit version-2 RunSpec/renderer controls, unchanged normalized
result schema 1, linked request/submission/observation usage and authorized
reasoning records, streaming companion exports and R join/aggregation rules.
Existing columns, frozen records, identities and missing-value semantics in
this document remain implemented contracts. New sidecars require their own
typed versions and deterministic offline Python/CLI-to-R acceptance; grouped
requests cannot multiply token usage or duplicate reasoning by trait. Any
internal persistence revision requires deliberate compatibility handling, not
implicit mutation/migration of an existing registry. Credential diagnostics
never add credential values or private identities to assessment evidence.


## Phase 4B durable evidence integration

New stores use internal registry schema **2**, with append-only evidence receipts,
submission usage revisions, observation order and reasoning metadata tables. The
schema-1 statements/checksum remain exact. Opening a historical store verifies it
without migration, new tables, receipt changes or evidence backfill. Its original
execution/resume/retry/export behavior remains supported; evidence inspection
returns unavailable. New evidence insertion requires a schema-2 store and reports
a safe diagnostic for a historical store. New observations requiring redaction or
body omission also require schema 2, since legacy receipts cannot label those
derivatives; ordinary exact-safe mock execution and existing receipt replay remain
supported. Assessment/result schemas remain 1.

`EvidenceReceipt` version 1 is a separate companion under `evidence/`. It commits
physical run/request/attempt/submission/observation identity, payload hash,
acknowledgment hash and remote/key relationship, historical response receipt and
its hash, typed usage/order/reasoning facts, authorization commitment, capture
policy, and distinct original/retained identities. It contains no response body,
reasoning text, arbitrary provider dictionary or unrecognized output key.

One I/O thread still owns registry, artifacts and receipts. Execution verifies
acceptance first, prepares safe evidence and filters the response **before** body
publication, publishes permitted content, publishes the companion, publishes the
unchanged historical response receipt, and commits observation plus linked
metadata in one short owned transaction. Interpretation/result commit follows.
The companion embeds safe response-receipt metadata so recovery can repair a
missing historical receipt after interruption without retrieving the provider.
Exact matching replay verifies every immutable identity/digest; conflicting replay
fails. Receipt/artifact verification also runs for completed observations.

Usage initial facts belong to physical submissions, including invoked definitive
failures without an observation. An unaccepted attempt is not evidence of billing.
Retries have separate submission IDs. Unknown counters/costs stay null. Usage
revision 0 is initial; explicit authoritative revisions have consecutive
per-submission revision numbers, stable revision IDs, allowlisted source/version,
content digests and a global monotonic sequence. Selection at a high-water mark
chooses the last revision; revisions and overlapping counters are never summed.
Published revision receipts precede owned database insertion, permitting exact
idempotent replay after interruption. No trait-level usage records are created.

Internal repository readers retrieve submission/observation evidence, iterate a
run with bounded indexed keyset pages, return revision audit history and select
authoritative usage at a revision high-water mark. `ReadOnlyRepository` shares
these readers without creating application files. Safe metadata remains available
under minimal capture independently of response bodies. Missing assessment text
still requires authoritative retrieval or remains unresolved; evidence recovery
never authorizes another submission or fabricates normalized results.

Version-1 internal authorization commits run/manifest/provider/capture before the
submission boundary. Ordinary RunSpec v1/v2 implies retention none. Only explicit
synthetic mock fixtures can grant positive retention in this phase. Reopening
verifies the committed policy rather than current environment/default values.
Permitted reasoning text uses the existing protected content-addressed artifact
store; metadata contains safe references only. Ordinary CLI/status/result outputs
never include reasoning text. Missing or modified artifacts, changed usage/order,
identity/capture conflicts and unsupported schema versions fail safely.

Fault checkpoints cover extraction, permitted artifacts, companion publication,
observation/evidence transaction, evidence replay, grouped result commit and final
run status. The [integration design](phase-4b-integration-design.md) records the
pre-implementation seam review. Native translation, real provider adapters,
companion exports and live qualification remain deferred.
