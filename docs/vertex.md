# Vertex AI Gemini execution

**Current assessment contract:** new preparations use [presentation revision 2](renderer-revision-2.md), including positional CJ aliases. Historical native evidence
remains scoped to revision 1. New `auto` requests use text until separately
qualified for revision 2; explicit `native` and strict unknown `auto` fail before
submission. Historical sealed requests keep their original commitments.

Vertex single-run execution uses **google-genai 2.34.0**,
`models.generate_content`, **v1beta1**. The [2026-11-11 acceptance](vertex-live-qualification-acceptance.md)
independently verifies separate ADC and API-key evidence and promotes only
`gemini-3.1-flash-lite` / `global` / the respective qualified private-project route /
`vertex.generate_content.response_json_schema`, for one-trait scores and forced-choice
winners. Every other scope remains unqualified. This promotion makes no provider
calls, reads no credentials/keyring and authorizes no additional qualification or
package publication.

Google's current documentation calls this service Gemini Enterprise Agent
Platform. JudgmentLLM retains provider ID `vertex`. Provider `gemini` uses the
separate Developer API and its own keys and qualification. Its accepted live
results confer no Vertex authority.

Install the Google extra from reviewed source, retaining the existing lock:

```bash
pip install '.[google]' 'google-genai==2.34.0'
```

Another installed SDK version fails execution readiness. Ordinary imports, status,
validation, planning and preview construct no SDK client. Copy
[the synthetic configurations](../examples/vertex/) and replace `YOUR_EXACT_MODEL`
with a bare Google publisher model ID. Unknown IDs remain valid configuration;
that does not establish availability. Project-qualified names, other publishers,
URLs, path traversal, query parameters and arbitrary request bodies are rejected.

## Routing and authentication

Choose `schema_version: 3`, `provider: vertex`, and
`endpoint: models.generate_content`. Both implemented modes require explicit
`GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`. Project IDs/numbers are
validated offline; this neither probes Google Cloud nor confirms the project
exists. Locations in this reviewed transport scope are `global`, `us-central1`,
`us-east4`, `europe-west1`, `europe-west4` and `asia-southeast1`. Other regions,
multi-regions, private gateways, custom hosts and mTLS-bound transports need a
separate reviewed extension. Live qualification is confined to the exact global
project/authentication scopes in the acceptance record; other locations remain unknown.
See Google's [location documentation](https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/locations).

`global` uses `https://aiplatform.googleapis.com/`; regional execution uses
`https://LOCATION-aiplatform.googleapis.com/`. Both use the complete resource
`v1beta1/projects/PROJECT/locations/LOCATION/publishers/google/models/MODEL:generateContent`.
The mode, project, location, host, resource template and SDK/API versions enter a
domain-separated route commitment and the complete preparation/source identity.
Only the route digest enters retained commitments; the private project string
stays transient. Resume requires the original route configuration. Changing
region, project or authentication mode cannot reuse a frozen run.

### ADC

`JUDGMENTLLM_VERTEX_AUTH_MODE=adc` selects Google Cloud Application Default
Credentials. This remains the default after deliberately selecting provider
`vertex`, preserving historical ADC status. The
[official ADC chain](https://docs.cloud.google.com/docs/authentication/application-default-credentials)
can resolve eligible service accounts, local ADC, attached identities and
federation. JudgmentLLM delegates resolution/refresh to the SDK during explicit
execution only. It supplies no custom credential-file reader or identity chain.
An ADC path alone establishes nothing. Missing project, malformed project,
missing location and invalid location have separate fixed diagnostics. Unavailable
ADC fails safely before inference forwarding and never falls back to a key.

Offline `credential_status(providers=['vertex'])` and `auth status` return the
historical five-field `unknown`/`google_adc` record in ADC mode. They do not
construct clients, discover ADC, open credential files, access metadata, refresh
tokens or expose project/account identities. `--require vertex` cannot be
satisfied by unknown ADC; this does not mean ADC is invalid.

### Separate Vertex key

Set **`JUDGMENTLLM_VERTEX_AUTH_MODE=api_key`** and securely supply
**`JUDGMENTLLM_VERTEX_API_KEY`** in the invoking process. JudgmentLLM never reads
GNOME Keyring; an external launcher may expose this variable later. No key belongs
in YAML, command arguments, source, a proposal, an artifact or an export.
`GOOGLE_API_KEY` and `GEMINI_API_KEY` cannot authorize this mode. Missing/malformed
Vertex keys fail without ADC or Developer fallback. Key status uses additive
`CredentialStatusV2` with `status_version: 2`; the CLI envelope stays version 1.
Configured means presence only.

The implemented key path sends `x-goog-api-key` to the **standard project/location
resource above**. Google's [key reference](https://docs.cloud.google.com/docs/authentication/api-keys)
distinguishes an unbound API key from an authorization key bound to a service
account. The [Agent Platform key setup](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/api-keys?usertype=standard)
describes billing, organization policy, service-account permissions and key
restrictions. Operators must independently establish eligibility, restrict the
key to `aiplatform.googleapis.com`, and review the bound account's IAM and quota.
The adapter cannot infer these facts from a string. SDK forwarding is fixture-
tested. The accepted API-key evidence establishes only its recorded project/mode
route; another key or account still needs independent authorization.

[Express Mode](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/express-mode/api-reference)
uses a distinct global publisher resource without project/location components.
The SDK supports that constructor shape, but this adapter does not expose the
Express route or reinterpret an Express key as standard-project authorization.
There is no automatic mode/endpoint selection from key contents or ambient SDK
variables.

## Pinned SDK evidence

Actual 2.34.0 source and intercepted HTTP establish `enterprise=True` as the
current flag and `vertexai=True` as its legacy alias; conflicting explicit flags
raise. The adapter sets both true and explicitly fixes base URL, project,
location, API version and HTTP clients. Explicit project/location suppresses
ambient Developer-key selection in ADC mode. Key mode supplies the separate
resolved Vertex key. Source hashes are retained in the
[SDK profile](../tests/fixtures/production_vertex/sdk-profile.json).
See the pinned [client](https://github.com/googleapis/python-genai/blob/v2.34.0/google/genai/client.py),
[HTTP implementation](https://github.com/googleapis/python-genai/blob/v2.34.0/google/genai/_api_client.py)
and [model conversion](https://github.com/googleapis/python-genai/blob/v2.34.0/google/genai/models.py).

Current quickstarts commonly show `v1`. The already registered V2 Vertex
projections and this production transport use **v1beta1**, explicitly, matching
the pinned SDK's Vertex default and the
[versioned endpoint reference](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1beta1/projects.locations.publishers.models/generateContent).
No compatibility version or historical Google projection is changed.

## Assessment and controls

Text mode includes the complete canonical JSON schema in the protected prompt.
Independent/grouped ordinal rubrics, optional exemplars, custom sandboxed
prompts, all three explanation layouts, forced-choice pairwise winners and
explicitly permitted ties have offline fixtures. Separated arrays join by trait
ID. Original and presented positions remain distinct. Category selection creates
a category code, with null probabilities/expected scores.

The separate V2 native projections remain:

- `vertex.generate_content.response_schema`: SDK `Schema`/Cloud Schema.
- `vertex.generate_content.response_json_schema`: JSON Schema.

The first emits `responseSchema`, uppercase types and SDK snake-case schema
fields. The second emits `responseJsonSchema` with the original JSON-schema
casing. Ordered schemas, property ordering, closure, alternatives and enums have
independent exact-byte fixtures. Native prompts omit schema duplication under
the existing route policies; all relational, membership and explanation checks
still run locally. Google's [structured-output guide](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/control-generated-output)
and the pinned types describe different schema surfaces; serialization is not
proof of serving enforcement.

The production registry grants only the exact qualified JSON-schema route above.
For its two accepted features, explicit native and `auto` choose native; explicit
text stays text. Unqualified native fails before forwarding. Nonstrict `auto`
retains text fallback; strict unknown fails. Grouping, explanations, ties and
`response_schema` gain no live authority. Complete mode/project/location routing
is required for selection, and changing any route fact cannot borrow another
record. No runtime registration interface exists.

The [repository-only qualification harness](vertex-qualification-executor.md) has
separate candidate/source/plan authority; ordinary production cannot use its
submission grants or historical consumed allowances. A failed request never
changes its frozen mechanism or causes an automatic fallback call.

Sparse generation maps temperature to `temperature`, top-p to `topP` and output
limit to `maxOutputTokens`. Omitted values stay omitted; SDK sampling numbers
serialize as floats. Endpoint numeric validation bounds temperature to 0–2 and
top-p to 0–1. Model limits remain unknown. For unknown IDs, sampling support is
unknown and obeys strict/nonstrict validation. Documentation establishes sampling
for the reviewed `gemini-2.5-flash`/`gemini-2.5-pro` names; it establishes no live
qualification. Current [inference guidance](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/inference)
says newer models ignore sampling: this adapter rejects explicit sampling for
exact `gemini-3.6-flash`, `gemini-3.8-flash`, `gemini-4-carbon` and
`gemini-4-argon`. A successful SDK constructor cannot justify an ignored setting.

One candidate is fixed. Thinking controls, tools, arbitrary headers/URLs, tier
settings and explicit cache controls are unavailable. `off`/`auto` omit explicit
cache controls; implicit provider caching may still occur. Provider thinking
usage never authorizes thinking-text retention. Native provider batch and managed
bulk-live remain Milestone 5, with no emulation or batch discount here.

## Responses, privacy and recovery

Successful assessment requires exactly one candidate with model role, one
nonblank supported assessment-text part, `STOP`, and no safety block or
continuation. Refusal, truncation, tools, unsupported finish reasons, multiple
outputs, invalid JSON, duplicate keys and malformed assessments remain failures.
No concatenation, JSON repair, category coercion or implicit rescoring occurs.
Grouped sibling salvage follows the existing strict per-member contract.

Requested and reported models remain separate. Missing usage is null. Vertex
[UsageMetadata](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1beta1/GenerateContentResponse)
reports prompt, candidate, thinking, cached and total counters: cached input is
included in prompt input; thinking is separate from candidate output. Preserve
reported totals without recomputing them. Unknown counters have no inferred
meaning. Usage belongs to one physical observation, never one row per trait.
No precise cost, cache savings or zero charge follows from missing counters.

Full capture may retain exact recognized assessment text, including invalid JSON,
as a protected artifact. Minimal capture retains hash/length and safe metadata.
Thought text, signatures, credential echoes and unknown envelope fields are
excluded before retention. Assessment explanations remain separate from provider
reasoning. The response bound is 8 MiB; recognized text is bounded to 1 MiB,
without truncation. SDK/auth error prose and authenticated URLs/headers are never
persisted. A credential-bearing response is withheld without hashing the secret.

Complete SDK HTTP body bytes must match the committed body before forwarding.
The guard fixes method, host, resource path, authentication headers and permitted
header names. SDK and HTTP retries, automatic continuation, function calling,
redirects, proxy routing and debug/replay recording are disabled or rejected.
Connect/write/pool limits are 10 seconds, read 60 seconds, generation deadline
90 seconds. Concurrency bounds connections and pending responses. ADC acquisition
uses Google's own credential transports/timeouts; cancellation of a background
credential refresh cannot promise immediate thread termination.

The shared engine journals physical identities before submission and retains
attempts, observations, diagnostics and exports. HTTP rejection differs from
uncertain forwarding. Restart never replaces an accepted/uncertain non-retrievable
response. Reopening can finish from retained evidence; it cannot manufacture
remote retrieval. Cancellation after forwarding remains uncertain unless a
response is already retained. Explicit subset retries use new attempts and retain
prior outcomes and immutable exports.

## Offline use and future qualification

With explicit nonsecret project/location configuration, these operations remain
offline, including when no credential exists:

```bash
judgmentllm validate --config examples/vertex/rubric.yaml --json
judgmentllm plan --config examples/vertex/pairwise.yaml --json
judgmentllm templates preview --config examples/vertex/rubric.yaml --item-id 001 --trait 01
judgmentllm auth status --require vertex --json
```

`run` and `resume` can make billable inference requests and require separate
operator authorization. Neither was invoked against Google in Phase 5D1.
`auth verify --provider vertex` and `providers models --provider vertex` explicitly
return unsupported with zero SDK calls. No suitable protected bounded operation
has been independently established; public listing, ADC discovery and token
refresh cannot establish inference permission.

The original [Phase 5D1 live proposal](vertex-live-qualification-proposal.md)
remains a preserved deferred version-1 record. Phase 5D2a adds separately versioned
complete offline candidate plans for **both `api_key` and `adc`**, each with four
synthetic cases and **zero authorized requests**. Both explicitly select
`gemini-3.1-flash-lite` / `global` / `v1beta1` / SDK2.34.0. The model card lists
`global`, `us`, `eu`; the earlier deferred `us-central1` example did not establish
availability there. The standard global resource still requires explicit
`GOOGLE_CLOUD_PROJECT` in both modes.

Use repository commands `candidate-plan`, then `verify` with an
independently retained expected plan digest. The [complete instructions](vertex-live-qualification-proposal.md#construct-the-final-immutable-plans-after-merge)
cover clean merged-source construction, private external evidence, redacted
reports, exact SDK byte reconstruction, separate mode identities and reservations.
No actual key, ADC file or `gcloud` is needed offline. Neither credential values,
keyring, OAuth nor metadata servers are consulted.

At offline proposal preparation, account eligibility was unverified. A stored Vertex key does not establish
its type or standard project association: independently verify key type, billing,
enabled API, IAM and restrictions under approved cloud setup before live work.
Express Mode keys are incompatible with this candidate: stop without route or
mode fallback. The operator reports ADC token acquisition, quota-project
configuration, API enablement and billing enabled; this work does not inspect or
verify that state. Inference IAM, exact-model eligibility and API-key compatibility
were unverified at that stage. The later acceptance records only successful
behavior for the two exact authorized routes; wider account eligibility stays unknown.

Phase 5D2b adds a [dedicated qualification executor](vertex-qualification-executor.md)
and read-only execution `report`, while preserving the immutable offline verifier.
It requires a fresh exact-merged-source plan, independent digest, matching project,
mode and location, reviewed count/cost reservations and separate billable/full
synthetic capture consent. A fixed sibling ledger and final HTTP guard consume
each original request once. Recovery never replaces uncertain work. No production
native promotion is included; implementation testing is synthetic and offline.

## Phase 5D3 compatibility and evidence acceptance

Both successful evidence sources and reports remain immutable. The ADC source
precedes the narrowly scoped privacy-safe 403 correction; the successful API-key
source includes it. Their successful request/assessment contracts agree, with
separate original-source provenance. The original unsuccessful API-key run remains
unresolved historical evidence and is never relabeled. See the complete
[source analysis and audit](vertex-live-qualification-acceptance.md).

Promotion changes authority/context/output/transport commitments for the exact
qualified model/project/mode/global scope, including explicit text. New eligible
`auto` selects native. Other scopes keep their historical empty-profile identities.
Frozen old manifests are not migrated or reselected: reconstruct or recover them
using their original clean source/dependencies. Current code rejects changed
authority. Existing normalized results and JSONL/CSV/R contracts are unchanged.
