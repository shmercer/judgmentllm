# Evidence privacy and extension guidance

Phase 4A's `judgmentllm.evidence` package accepts explicit typed facts and performs
pure extraction without credential access, provider calls or file I/O. Phase 4B
integrates these services into durable execution. The exact public
interfaces are in the [Python API](python-api.md#standalone-evidence-phase-4a-implemented).

Supply run/request/attempt/submission/observation identifiers and model metadata
from trusted existing execution/configuration records. These are identity fields,
not generic locations for output text, authentication headers, account names,
credential-file paths or arbitrary provider payloads. Do not hash credentials as
evidence. No `.env`, keyring, SDK default authentication or ADC lookup belongs in
an extractor; the Phase 3 resolver remains the sole authentication boundary.

Treat every output key and unknown trait ID as untrusted content. Observable-order
extraction may publish only committed key names and trait IDs found in the
contract; unknown keys never become diagnostics, warnings, exception text or JSON
metadata. Use fixed allowlisted unknown codes and null sequences instead. Do not
recover apparent emission order by serializing a dictionary or SDK object.
Transient parsing is byte/depth/member bounded and uses the existing duplicate-key
rejection boundary. This inspection produces diagnostics without changing score
validity, sibling salvage or retry authority.

Assessment explanations are authorized normalized task output. Provider-exposed
reasoning is separate observation evidence. Neither full capture nor requested
effort/summary exposure authorizes its retention. Explicit retention grants are
type-specific and require full capture. The pure Phase 4A metadata services accept
no reasoning text. Phase 4B's optional transient adapter boundary accepts documented
blocks, applies the immutable authorization, and publishes only permitted artifacts.
Artifact references contain only a digest and byte length. Filtering precedes any
response, debug log or temporary-file capture; redacted derivatives cannot claim
original byte identity. Ordinary RunSpec v1/v2 grants no reasoning retention.

Extend the source/field/kind/relationship allowlists only with documented provider
semantics and synthetic offline fixtures. Keep source counts and unknowns intact;
do not invent totals, sum overlapping cache/reasoning details, estimate cost
implicitly, or duplicate usage over grouped traits. Scoped evidence describes
facts with provenance; it does not grant model access or select transport.
Documentation, SDK installation, credential presence and offline fixtures never
establish independent live qualification.

Validation failures must remain content-free across constructor, JSON parsing,
string-mapping parsing, copying, trusted construction and frozen assignment.
When adding entry points, test both rendered exceptions and structured
`errors()`/`json()` forms with synthetic secret-bearing values and keys. Tests
must exercise standard CLI stderr, captured warnings/logs, reports and scratch
files without printing the canary itself on failure. Keep test data synthetic
and all runtime stores/build/qualification artifacts outside synchronized folders.

Durable attachment, receipt/recovery validation and evidence revisions use the
[Phase 4B persistence path](persistence-contracts.md#phase-4b-durable-evidence-integration).
Native schema translation remains Phase 4C work.

## Phase 4B pre-retention boundary

`ProviderResponse.evidence` is an optional typed in-memory extension. Usage and
exact emitted assessment bytes are separate from transport envelopes and parsed
scoring dictionaries. Unsupported evidence stays unknown. Adapters must supply a
structured credential-free safe derivative or body retention fails closed. Full
capture alone does not authorize reasoning. Filtering occurs before artifact,
staging, receipt or database I/O; unauthorized reasoning hashes are not recorded.
Original-body hashing requires an explicitly credential-free original boundary.
Redacted derivatives have distinct identities and an explicit disposition; omitted
bodies never claim exact capture. Minimal capture publishes safe metadata and
hash-only response identities, without response/reasoning bodies.

An internal version-1 immutable authorization binds the original manifest and run
before submission. Positive grants are restricted to explicit synthetic mock
fixtures; no public RunSpec reasoning setting is added. Summary grants cover
summary blocks; provider-text grants cover documented exposed thinking blocks.
Unsupported types remain unretained. Authorized fixture text is separately
published in the existing protected artifact store and verified on reopening;
public control models contain no text. Assessment explanations retain their
existing normalized-result meaning.

Unknown property names and trait identities cannot enter order metadata. Unsafe
or inseparable content is withheld from body retention while interpretation may
continue in memory. Recovery consumes the original safe companion receipt rather
than reconstructing evidence from results or the sanitized body. Canary tests
inspect runtime stores, auxiliary SQLite files, artifacts and control output.
Synthetic reported cost is accepted only for mock identities and explicitly
synthetic provenance; real billing remains unknown. Missing costs stay null.

The unchanged historical response-receipt format requires a response artifact
identity. When body retention is withheld, the response receipt uses the empty
byte sentinel; the companion explicitly records `response_retention="omitted"`
and never identifies that sentinel as the original provider body. Full capture
may publish that empty artifact, which contains no response content. Routine
result `raw_response_path` remains null for omitted content. Minimal capture
publishes no response-body artifact, including no empty sentinel artifact.
The companion validates the zero byte length and empty-byte SHA-256 directly;
its retained identity equals the embedded response receipt identity and obeys the
same capture policy. A nonempty body cannot be marked omitted. An invocation
failure with no observation has no retained identity or response receipt.

Unsafe output property names, unknown trait IDs and malformed emitted text cause
body withholding when a safe structured derivative cannot be established. The
known synthetic mock envelope is screened against its payload membership before
being declared safe, including version-1 execution. Transient response bytes and
reasoning text are excluded from model dumps and representations; validation errors
remain content-free. Original-body hashes are recorded only when the adapter has
established that authentication-bearing material is excluded. Reasoning block
source hashes are omitted for unauthorized text.

Compiled field validators are reused through a bounded cache keyed only by the
declared evidence model class and field name. Inputs, responses, reasoning text,
authorization decisions and validated records are never cached. Every payload
still passes strict field validation, full cross-field checks and safe error
sanitization. This removes repeated schema compilation without relaxing capture
policy, persistence verification or interoperability deadlines.


## Phase 4C1 projection privacy

Native projection and selection are pure in-memory operations with no credential,
SDK/client, network or file discovery. Exact schema content necessarily includes
requested IDs and category labels; unrelated rubric descriptions, assessment text
and metadata are excluded. Sensitive schema/source/order fields are excluded from
representations. Fixed diagnostics and bounded provenance retain only approved
codes, scope/version identifiers, counts and hashes; projection content must not
be copied into logs or minimal-capture/qualification summaries.

Fresh-process tests install admission guards before imports, forbid SDK imports,
client/credential discovery, sockets/DNS and filesystem mutation, and prove the
guards detect prohibited operations. Secret canaries arrive by pipe, stay out of
fixture files, and are suppressed from failed pytest diagnostics. Constructor,
copying and structured validation failures remain content-free. Standalone
candidate projections are not persisted by any historical lifecycle operation.


The native-record JSON boundary performs strict duplicate-key preflight over the
entire original document, including nested objects and array members. Mutable
`bytearray` input is captured once so preflight and validation cannot consume
different documents; input subclasses are rejected. Duplicate
key names and values never enter error text or structured error details, even
when the final value is legitimate. Fresh-process canary tests exercise this
boundary under the same SDK/network/credential/filesystem admission guards.
The shared historical evidence JSON boundary is unchanged.
