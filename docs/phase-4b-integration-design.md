# Phase 4B integration design

Base: `fa21113ec85f77278cf1d8c36c2fc91444c6e4d6` (PR #18 merged).
This design records the reviewed implementation seams before source changes.

New registries use internal schema 2. The exact schema-1 definitions and checksum
remain accepted, without implicit migration or evidence backfill. Genuine older
stores keep their established execution/resume/export behavior; evidence readers
report unavailable. New evidence writes require schema 2. Normalized assessment,
RunSpec, manifest and historical response/acknowledgment receipt formats stay fixed.

A separate version-1 evidence receipt binds the immutable submission, request,
attempt, payload, acknowledgment and response receipt to typed safe usage, order,
reasoning metadata, capture authorization and original/retained content identities.
Submission usage is never copied to traits. Observation order and reasoning belong
to the established observation. Explicit append-only usage revisions have stable
identities, source/version and deterministic per-submission and global ordering;
a selected revision replaces rather than adds to earlier counters.

The optional provider evidence boundary carries original emitted assessment bytes,
typed usage and documented reasoning boundaries in memory. It is independent of
scoring and provider submission. Unsupported adapters establish unknown evidence;
transport envelopes and parsed dictionaries never establish emitted order.
Default reasoning retention is none. A versioned internal synthetic-only grant is
published before submission and verified on recovery; no public RunSpec control
is introduced. Structured safe response derivatives remove unauthorized reasoning
before the first artifact write. Unseparable envelopes fail closed on retention.
Credentials are excluded from original hashing as well as retained artifacts.

All registry/artifact operations remain on the existing dedicated I/O thread.
Safe extraction precedes artifact publication, then the companion evidence receipt
and historical response receipt publish durably before the short transaction that
commits the observation and linked evidence. Parsing/normalization/results follow.
Replay verifies identities and content, reuses the original safe receipt facts, and
cannot authorize submission or replace order/initial usage. Full safe derivatives
can recover assessment interpretation locally; minimal capture recovers committed
safe metadata independently, but missing assessment bodies require authoritative
retrieval or remain unresolved. Committed results verify evidence on reopening.

Fault checkpoints cover extraction, permitted artifacts, evidence receipt, evidence
transaction, observation, grouped results, final state, revision and replay. Receipt
or artifact disagreement fails with fixed content-free integrity diagnostics.
Phase 4C, provider adapters, companion exports and live qualification stay deferred.
