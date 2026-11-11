# Phase 5D2b: Vertex qualification executor

This repository-only executor implements the separately reviewed execution step
for the [version-2 offline candidates](vertex-live-qualification-proposal.md).
Implementation and CI are offline. They authorize no provider calls, credential
access, cloud discovery, spending, registry promotion or publication. At the executor-only stage the Vertex
production native registry remained empty. The later [Phase 5D3 acceptance](vertex-live-qualification-acceptance.md)
adds separate narrowly scoped reviewed production records. Ordinary `run`/`resume`
cannot borrow this harness's authority.

Each plan is independent: `api_key` resolves only `JUDGMENTLLM_VERTEX_API_KEY`;
`adc` uses only Google's official ADC chain at explicit execution. Neither mode
can fall back to the other or use Developer credentials. The operator supplies
the existing key through a separately approved secure launcher; no keyring API is
used. The executor retains no keys, tokens, authorization headers, ADC identities
or credential paths. Account eligibility, key/project association and inference
permissions remain independently unverified. An Express Mode key is incompatible.

The exact scope is SDK google-genai 2.34.0, API v1beta1,
`models.generate_content`, `gemini-3.1-flash-lite`, standard project route at
`global`. Each mode has four original synthetic cases: rubric-text, rubric-native,
pairwise-text, pairwise-native. Only `response_json_schema` is probed natively.
No additional models, explanations, grouping, ties, `response_schema`, batch,
thinking controls or wider generation settings are admitted. Requested and reported
models are separate; `modelVersion` must equal the requested bare model exactly.
No alias mapping has been reviewed.

## Reconstruct plans after merge

Use the final merged source with a clean checkout, including untracked files, and
the pinned SDK. Keep all evidence in a private, unsynced directory outside Git.
On Windows the operator must establish equivalent private ACLs; POSIX mode bits
do not establish Windows privacy. Set only the explicit nonsecret project for
these credential-free commands:

```bash
export GOOGLE_CLOUD_PROJECT='YOUR_EXPLICIT_PROJECT_ID'
SOURCE=$(git rev-parse HEAD)
python scripts/qualify_vertex.py candidate-plan --source-commit "$SOURCE" \
  --auth-mode api_key --location global --model gemini-3.1-flash-lite \
  --evidence-dir /external/private/new-vertex-key-plan
python scripts/qualify_vertex.py candidate-plan --source-commit "$SOURCE" \
  --auth-mode adc --location global --model gemini-3.1-flash-lite \
  --evidence-dir /external/private/new-vertex-adc-plan
```

Preserve each printed SHA-256 independently. Verify each plan from its exact clean
source with the independently retained digest:

```bash
python scripts/qualify_vertex.py verify \
  --evidence-dir /external/private/new-vertex-key-plan \
  --plan-sha256 INDEPENDENT_KEY_PLAN_SHA256
python scripts/qualify_vertex.py verify \
  --evidence-dir /external/private/new-vertex-adc-plan \
  --plan-sha256 INDEPENDENT_ADC_PLAN_SHA256
```

Verification independently rebuilds all four physical requests through the
production builders and pinned SDK's pure converters. It checks complete body,
URL, source, dependency, schema, prompt, identity, SDK-source, artifact and pricing
commitments. This creates no client and reads no credentials. Plan version 2
retains its original zero-authority fields, including its historical executor
status; separate authorization records supply any later execution authority.

The two Phase 5D2a real-project plans at source
`e7c6787b0b66d83bf1f7ddcd18ebaaf5202318d2` are immutable historical evidence:

- API-key: `64efe4d5b1852e62871d829fc897a38ee2b4b7118ed39e0411bd877151a576e1`.
- ADC: `4bbe5244395324f0b2fdccd4c0ccfaf804a83ad9b9bd27cfc6bf30869d1a7a36`.

Do not edit, migrate or execute those plans. They cannot match the final merged
executor source. Generate fresh plans after this PR has been reviewed and merged.

## Separate execution authorization

Before any later run, independently review each credential/account's eligibility,
all request commitments and the actual reservation. Obtain separate explicit
source/digest-bound consent for billable requests and full synthetic assessment
capture, with reasoning retention `none`. Each plan authorizes at most four
requests, sequentially, with one candidate, no retries or automatic continuation.
The default plan reserves 23,552 estimated input and 2,048 output tokens including
thinking: $0.008960 at the sealed standard rates. Lower output limits are allowed
only when already sealed in a new plan. The fixed reviewed estimated ceiling is
$0.02 per plan. It is **not a provider-enforced billing cap**. Excess reservations
require a new reviewed plan; execution never raises the ceiling. Reported usage
above the reservation increases estimated exposure and can stop remaining cases.
Known token components use the sealed standard rates for that stop decision even
when other counters are missing. This partial exposure never becomes a fabricated
usage counter or a complete charge estimate; reservations remain the minimum.

The following is a future operator command template, not authorization to run it
in this implementation PR. Supply each mode's independently reviewed digest and
source, matching explicit project, location, count, reservation and ceiling. The
API-key example assumes an approved launcher has supplied the Vertex key:

```bash
python scripts/qualify_vertex.py execute \
  --evidence-dir /external/private/new-vertex-key-plan \
  --authorize-plan-sha256 INDEPENDENT_KEY_PLAN_SHA256 \
  --authorize-source-commit REVIEWED_MERGED_SOURCE_SHA \
  --auth-mode api_key --location global \
  --reviewed-request-count 4 \
  --reviewed-reserved-estimated-usd 0.008960 \
  --reviewed-estimated-ceiling-usd 0.02 \
  --accept-billable-calls --accept-full-synthetic-capture
```

ADC requires its own plan directory, digest and consent with `--auth-mode adc`.
`GOOGLE_CLOUD_PROJECT` must match that plan. The explicit mode and location select
the sealed runtime; ambient Developer keys, SDK mode switches, URLs and proxy
settings cannot change it. No credential value is an argument or a plan field.
Credential presence cannot establish project association, IAM or serving access.
For ADC, the transient `x-goog-user-project` header must also equal the plan's
explicit project exactly; absent or different quota projects fail before consuming
an allocation. No project alias lookup or ADC identity is retained.

## Durability and recovery

The plan directory's complete hash inventory remains immutable. A fixed sibling
`<plan-directory-name>.execution` contains the separate authorization, FULL-sync
SQLite consumption ledger, exclusive forward intents, permanent plan lock and
existing per-case execution stores. There is no configurable alternate execution
directory. Plan configuration paths bind reconstruction to the original location;
copying the seal elsewhere cannot replenish authority. Keep the sibling private
alongside the seal. A separate immutable
`<plan-directory-name>.authorization.json` anchor prevents loss of the entire
execution directory from resetting the allowance. Both authorization copies must
match. Never delete/reset either authorization, the lock, ledger or intents.
Authorization and intent files are mode 0600 on POSIX; keep their parent private.
Lost or partial state blocks execution rather than reconstructing an allowance.

The existing cross-platform OS lock excludes concurrent executors. All four
prepared commitments are verified before credentials. At the final HTTP boundary
the adapter checks method, complete route, headers, selected credential and exact
committed bytes. The dedicated Vertex guard checks clean source and plan again,
prior terminal evidence, exposure and the current unconsumed allocation. SQLite
`BEGIN IMMEDIATE`, exclusive fsynced intent creation and commit precede forwarding.
A crash between intent creation and HTTP consumes that allocation conservatively.
Resetting a ledger row cannot erase its independent intent.

Recovery requires the same complete explicit consent command. It reopens the
existing shared execution engine and retained receipts. Durable accepted responses
can be recovered without another provider call. An uncertain submission stops the
plan permanently; it cannot authorize a retry, replacement identity, later case,
mode fallback or native substitution. HTTP rejection also stops the matrix.
Only the established matrix rule permits advancing after a positively completed,
exact-model, single supported assessment whose retained JSON is canonically
invalid. Refusal, filtering, truncation, unsupported completion, multiple outputs
and model mismatch stop continuation and fail qualification. Completed failed
assessments are retained without repair or rescoring.

## Read-only execution report

```bash
python scripts/qualify_vertex.py report \
  --evidence-dir /external/private/new-vertex-key-plan \
  --plan-sha256 INDEPENDENT_KEY_PLAN_SHA256
```

`verify` preserves the original offline-plan report. `report` now adds execution
inspection and requires the independent digest even before execution. It creates
no files, opens existing stores read-only and never resolves credentials. Missing,
corrupt or inconsistent evidence cannot qualify a case. The report checks request,
manifest, source, response, diagnostic, observation and normalized-result links,
and independently revalidates retained assessment JSON and successful selections.
Original/presented pair orientation and physical invocation IDs are preserved.

Reports show attempted allocations, confirmed HTTP responses and accepted requests,
successful/invalid assessments, unresolved outcomes, exact-model matches, native
versus text, reported usage and estimated charges. `attempted_requests` is a
conservative upper bound that includes the consumed-before-forward crash window.
`actual_http_forwards` is unknown if any consumed request lacks a confirmed HTTP
response; confirmed accepted counts never include uncertain work. Each case has
an explicit `terminal_state` plus separate submission certainty and assessment
continuation status. A terminal invalid assessment does not qualify a model.

Missing counters stay null; totals are summed only when every consumed invocation
reports that counter. Thinking remains separate and contributes to output charge
estimates only when reported. Cache/tool pricing uncertainty leaves charges null;
no billing cost, total, probability or expected score is fabricated. Per-case
response/evidence/component/result digests support independent inspection.
`provenance_sha256` hashes canonical case JSON without that field;
`report_sha256` hashes canonical report JSON without itself. The retained response
digest identifies the safe retained derivative when privacy filtering was needed.
Digests establish integrity, not authenticity against an actor able to replace all
local evidence. Preserve independently reviewed report hashes outside the store.

Public summaries omit projects, credential identities, complete resources, prompts
and request bodies. Complete seals and raw assessment evidence remain private.
Harness results never publish capabilities. After minimal independently authorized
live execution, inspect evidence independently before proposing any production
qualification promotion in another review.

## Offline review of the first API-key execution

At merged source `59a45ec31e53de7ed9eb833f5876c0000d982bca`, the private API-key
records show one consumed `rubric-text` allocation, one uncertain attempt, no
observation or normalized result, and three unstarted cases. The receipt records
`invocation_acceptance: unresolved` with no failure code, response or exception
details. Its report has zero confirmed HTTP responses and unknown actual forwards.
These records cannot retrospectively establish an HTTP status or transport cause.
The operator independently verified that the Google Cloud API key is bound to a
service account and restricted to Vertex AI. IAM troubleshooting returned
`CANNOT_ACCESS` for `aiplatform.endpoints.predict`, then `CAN_ACCESS` for the same
service account after assigning the permission. These findings do not establish
the earlier HTTP response or confer qualification.

Installed google-genai 2.34.0 raises `ClientError` with integer `.code == 403` for
a permission-denied response, which `VertexAdapter.submit()` already recognizes
as `permission_denied`. Synthetic tests reproduce a separate information-loss
path in the unchanged source: an HTTP 403 body containing the private project
resource is withheld by the transport privacy guard before SDK classification.
That raises `SubmissionUncertain`, which `submit()` preserves, leaving the same
report shape as an unclassified exception or transport failure. Ordinary 403
bodies without private echoes were classified correctly. The missing IAM
permission is consistent with this reproduction, but the historical cause remains
unproven.

The correction recognizes only an actual HTTP response with status 403 returned
through the sealed Vertex transport. It closes that response without reading,
retaining, logging, hashing or exporting its body, and raises the existing
`VertexRequestRejected` with the fixed `permission_denied` failure code. A failed
body close cannot erase the confirmed status. The existing invocation receipt and
read-only report then record rejection before assessment acceptance, one consumed
allocation, one confirmed HTTP response and no retained response or assessment.
The matrix stops, and execution/reopening never replays that allocation.

Unknown errors remain uncertain after forwarding; error prose and JSON body codes
cannot supply an HTTP status. Other statuses keep their existing privacy guards.
API-key and ADC authority remain separate. This correction neither repairs nor
reclassifies historical records: preserve the original uncertain record and
consumed allocation. After merge, any new API-key qualification requires a newly
generated plan bound to the merged source, independent verification and separate
explicit authorization before execution. No live execution or capability promotion
is part of this correction.
