# Evidence privacy and extension guidance

Phase 4A's `judgmentllm.evidence` package accepts explicit typed facts and performs
pure extraction. It neither reads credentials nor calls providers, writes files,
changes current capture behavior, or creates durable evidence. The exact public
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
type-specific and require full capture. Phase 4A accepts no reasoning text, writes
no artifacts, and never fabricates unavailable text. Artifact metadata contains
only a digest and byte length. A future 4B retention layer must filter unsolicited
or unauthorized blocks before any response, debug log or temporary-file capture;
redacted derivatives cannot claim original byte identity.

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

Durable attachment, receipt/recovery validation and evidence revisions belong to
[Phase 4B](persistence-contracts.md#phase-4b-evidence-integration-seams-deferred).
Native schema translation belongs to 4C. Neither extension is implemented here.
