# Provider and endpoint compatibility (design target)

**Baseline matrix as of 2026-11-02; Milestone 3 design evidence reviewed 2026-11-05.** A design commitment is not the same as a qualified model/endpoint combination. Current per-model/region/account status belongs in generated `model-support.md` with evidence dates.

| Adapter | Live | Native provider batch | Managed bulk-live | Rubric | Pairwise | Notes |
|---|---|---|---|---|---|---|
| OpenAI Chat Completions | implemented; reviewed live scope accepted | target | later milestone | generative | generative | Phase 5A, SDK 3.42.1; exact-model capability unknown until qualified |
| OpenAI Responses | implemented; reviewed live scope accepted | target | later milestone | generative | generative | Phase 5A, SDK 3.42.1; exact-model capability unknown until qualified |
| OpenAI Decisions | target | not available | yes | typed score, 2-10 levels | typed choice | Currently GPT-6 Luna; no generated explanations; 1-400 questions/request; cache policy handled separately |
| Anthropic Messages | implemented; live acceptance pending | target | later milestone | generative | generative | Phase 5B; SDK 2.3.0 / API 2026-10-21; text JSON and controlled native output_config.format |
| Gemini Developer | implemented; reviewed four-case live scope accepted | later milestone | later milestone | generative | generative | Phase 5C; google-genai 2.34.0 / v1beta; [exact qualified scope](gemini-live-qualification-acceptance.md); broader mappings remain fixture-only |
| Vertex AI Gemini | target | target (Google Cloud setup required) | yes | generative | generative | `google-genai` SDK; Gemini focus; GCS/BigQuery source and cloud IAM considerations |
| Together AI | target | target, subject to current API qualification | yes | generative | generative | Use official Together Python SDK v3 (`together>=3,<4`); endpoint/model feature coverage must be tested |
| Ollama local | target | no native provider batch | yes | generative | generative | Official Ollama SDK, local daemon; local model discovery; optional later cloud support separate |
| TypeSafe AI Jev | target | not available | yes | typed score; documented maximum 10 levels | typed choice | Official `typesafe-sdk` confirmed; score guide recommends at least two levels; no generated explanations |

### Documented score bounds checked in the Milestone 1 hardening pass

On 2026-11-02, the [official OpenAI Decisions guide](https://developers.openai.com/api/docs/guides/decisions#score-against-a-rubric) specified a 2–10 ordered-level range. Milestone 1 preflight enforces that endpoint bound independently of caller capability overrides. The provider-neutral rubric schema still permits any number of levels above its two-level minimum.

TypeSafe's [official Score guide](https://docs.typesafe.ai/primitives/score#request-structure), checked separately on the same date, documents a maximum of 10 score criteria and recommends at least two. This independently supports the upper bound; it does not establish that the API enforces the same lower bound as OpenAI Decisions. The [System One API reference](https://docs.typesafe.ai/api/system-one) identifies the endpoint as `/v1/systemone`. No request was sent and no Jev adapter or model was live-qualified. Milestone 1 does not embed the Jev bound: callers can supply the independently documented `CapabilityProfile(max_levels=10)` until endpoint-specific adapter validation is implemented. Other providers do not inherit either endpoint's limits.

### Milestone 3 output and authentication contracts

The [unified assessment output design](assessment-output-design.md) is the
normative extension. Its [provider evidence ledger](assessment-provider-evidence.md)
distinguishes documented behavior, planned integration policy and pending
qualification. Phases 1–3 implement neutral output contracts, v2 mock execution
and offline authentication diagnostics. Phase 4A adds standalone strict evidence
records and pure extractors. Phase 5A adds production OpenAI Responses and Chat
adapters; Phase 5B adds Anthropic Messages and Phase 5C adds Gemini Developer. Other real integrations remain proposed. No endpoint/model
acquires runtime support or live qualification from those records.

- Shared credential resolution uses official SDK mechanisms: provider-specific
  API-key environment variables, Google ADC for Vertex, and no authentication
  for ordinary local Ollama. The ledger records actual source names, precedence
  and SDK alternate-source constraints. Offline status never initializes ADC,
  refreshes tokens, contacts metadata or checks daemon connectivity.
  **Planned Vertex adapter follow-up:** support a separately selected Google Cloud
  authorization API key where the Agent Platform endpoint qualifies, alongside
  (not instead of) ADC. This is not implemented by Phase 3; a stored `vertex`
  keyring entry is not automatically used. See the
  [provider evidence](assessment-provider-evidence.md#planned-vertex-authorization-key-route).
- `auth status` and its Python API remain offline. Phase 5A adds explicit OpenAI
  `auth verify` and model discovery through a protected, non-generative models-list
  request. Phases 5B/5C add protected Anthropic and Gemini Developer models-list operations; other providers remain deferred; authentication acceptance and model
  visibility do not qualify an assessment endpoint or mechanism.
- Native structured outputs are preferred where compatible. Generated schemas
  and local validators remain provider-neutral; each adapter reports the native
  subset actually enforced and any locally enforced constraints. Property order
  requires endpoint-specific evidence; schema support does not prove reasoning
  order. Incompatible core schema requirements fail native-mode preflight;
  documented subset projections retain the full local contract.
- Request/observation usage and optional authorized reasoning evidence stay
  separate from normalized per-trait/judgment results. Live qualification remains
  explicitly opt-in; mandatory fixture and Python/R tests remain credential-free.
- Native Decisions/Jev assessment integration stays in Milestone 4. Managed
  bulk-live/native batch parity and recovery stay in Milestone 5; the CLI-first
  R wrapper exposes implemented services in Milestone 6.

Phase 4A scoped descriptors supplement existing `GenerationCapabilities` evidence
without registering native-schema support. Explicit unresolved model scope and
nullable facts preserve unknown support. Documentation and synthetic fixtures
cannot establish `live_qualified`; credential presence and SDK installation are
never qualification provenance. Exact interfaces are in the
[Python API](python-api.md#standalone-evidence-phase-4a-implemented). Durable
capture is Phase 4B, native transport is 4C, real generative adapters are Phase 5,
companion exports Phase 6 and authorized live qualification Phase 7 of Milestone 3.

### Required capability checks

Each provider endpoint or model may differ in:

- Support for `scores_only` and `scores_with_explanations`.
- Number of levels, traits/questions per request and maximum context/output size.
- Structured JSON/schema generation (generative only) or native typed scoring output.
- Exact service tier names, eligibility and fallback behavior.
- Native batch availability, limits, input storage dependencies, polling and result ordering.
- Prompt-cache controls, token threshold, TTL, automatic/implicit behavior and usage reporting.
- Token-counting APIs and available accounting/cost data.
- Model listing and reported capability metadata.

### Evidence classifications

- `discovered`: appears in documented catalog or model list.
- `implemented`: compatible adapter present and local validation available.
- `fixture_tested`: provider-shaped synthetic fixtures pass.
- `live_qualified`: an opt-in real test passed with provider + endpoint + model + feature + date.

Unknown capabilities remain unknown, not false and not true. Avoid hardcoded allowlists blocking unknown but valid model IDs. Provider/model id alone does not prove support for a tier or batch mode.

### Qualification source links

- OpenAI Decisions: https://developers.openai.com/api/docs/guides/decisions
- OpenAI Batch: https://developers.openai.com/api/docs/guides/batch
- Anthropic Batches: https://platform.claude.com/docs/en/api/messages/batches/create
- Google Gen AI SDK: https://googleapis.github.io/python-genai/
- Gemini Batch: https://ai.google.dev/gemini-api/docs/batch-api
- Vertex Gemini batch: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/samples/generativeaionvertexai-batch-predict-gemini-createjob
- TypeSafe Python SDK: https://docs.typesafe.ai/sdk/python/
- TypeSafe Score criteria: https://docs.typesafe.ai/primitives/score
- TypeSafe System One API: https://docs.typesafe.ai/api/system-one
- Ollama local: https://github.com/ollama/ollama/blob/main/docs/api/introduction.mdx
- Together provider documentation: https://docs.together.ai/
- Together SDK v3 release notes: https://github.com/togethercomputer/together-py/releases/tag/v3.0.0


### Phase 4C2 standalone translators

Seven independently identified V2 mechanisms cover Anthropic Messages, Gemini
Developer `response_schema` and `response_json_schema`, Vertex's two corresponding
interfaces, Together Chat and ordinary local Ollama Chat. These are pure candidate
projections, not executable adapters. The [detailed route matrix](assessment-provider-evidence.md#phase-4c2-independent-multi-provider-projection-evidence)
records pinned SDK/API scopes, represented constraints, converter retention,
documented endpoint support, local checks and unknown model/serving enforcement.
No real model is accepted or live-qualified. OpenAI V1 projection/selection APIs
and identities remain unchanged. Property ordering is requested, not observed.
Prompt-schema guidance awaits 4C3; provider calls/authentication await later work.


### Phase 4C3 durable transport support

All two OpenAI V1 and seven provider-neutral V2 mechanisms can enter the additive
RunSpec/manifest/output/prepared-request 3 path under private exact-scope synthetic
fixtures. Request-specific native/text selections, renderer 3 and complete
synthetic payload commitments survive sealing, reopening and subset retry.
The [complete repetition matrix](assessment-provider-evidence.md#phase-4c3-frozen-presentation-and-adapter-handoff)
freezes native guidance separately for every Google route and preserves Together
and Ollama's projected schema guidance.

This infrastructure does not activate any real provider adapter, model,
authentication or endpoint. Production catalogs retain no positive real-model
native acceptance. All serving enforcement stays unknown; complete canonical
local validation remains mandatory. Implementation acceptance requires retained
exact-head qualification; real endpoint construction/authentication and separately
authorized serving qualification remain Phase 5 and later work.

### Phase 5A OpenAI production boundary

Both generative routes use the official asynchronous SDK, pinned to 3.42.1 for
qualification. Production transport commitments describe actual nonsecret HTTP
body bytes, verified before forwarding. Historical synthetic transports and
OpenAI V1/provider-neutral V2 projections remain unchanged. `store=false`,
nonstreaming requests, bounded timeouts and `max_retries=0` are fixed policies.

Text mode and local assessment validation are implemented. Native mode requires
controlled exact-scope evidence; auto selects native only when eligible and obeys
the configured strict capability policy. A separately authorized repository
qualification harness can probe unknown native scopes. Its grants never authorize
ordinary run/resume or populate the production support registry.

The initial live matrix has eight requests: each endpoint × rubric/forced-choice
pairwise × text/native. No serving model is advertised as independently qualified
by the implementation or fixture evidence. The Phase 5A PR must remain open with
**offline-qualified; live qualification pending explicit authorization** after
all offline/hosted gates pass. See the [OpenAI guide](openai.md).

Phase 5A [final acceptance](https://github.com/shmercer/judgmentllm/pull/24#issuecomment-6726984126) verified eight
text/native rubric/pairwise cases on `gpt-4.1-mini-2025-04-14` with SDK3.42.1.
That limited acceptance does not populate the production native registry or
authorize any Anthropic call.

### Anthropic Phase 5B scope

Text/native/auto policies, grouped ordinal rubrics and forced-choice pairs reuse
existing result/evidence contracts. Exact native model support remains unqualified;
installed SDK support, documented endpoint support and synthetic fixture coverage
are separate facts. The [guide](anthropic.md) specifies the required4096 adapter
output default, removed sampling controls, conservative cache/tier policies,
unknown totals, thinking privacy and non-retrievable uncertainty.
The four-case live proposal is prepared offline and requires separate consent
against its exact source and plan digest; no model-list or token-count call is
needed to prepare it.

### Gemini Developer Phase 5C scope

The [Gemini guide](gemini.md) distinguishes implemented request mapping and
synthetic SDK/lifecycle tests from the narrowly accepted live scope. Developer API only:
`models.generate_content`, google-genai 2.34.0, v1beta, explicit API key with
`GOOGLE_API_KEY` before `GEMINI_API_KEY`. Vertex, enterprise, ADC and Interactions
are separate. Independent/grouped rubric layouts and pairwise winners use the
existing strict local output contracts. No probability distribution is invented.

Both existing native projections are preserved and tested through actual SDK
serialization. The [accepted qualification](gemini-live-qualification-acceptance.md)
promotes only `rubric_scores` and `pairwise_forced_choice` for exact
`gemini-3.1-flash-lite` through `response_json_schema`. `auto` selects native for
that feature scope and records text when authority is unknown and strict policy
permits it. Selected native
requests never fall back after failure. Automatic continuation, function calling,
SDK retries, redirects and ambient transport routing are disabled or rejected.
Usage records prompt, candidate, thought, cache and total counts separately, with
missing values unknown. The four original-source text/native rubric/pairwise
cases qualified on 2026-11-10; grouped traits, explanations, ordering variants,
ties, `response_schema`, other models, Vertex and broader generation/reasoning
features remain unknown. Historical verification requires the original checkout
where authority commitments differ; no new live operation is authorized here.
