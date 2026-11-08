# Provider and endpoint compatibility (design target)

**Baseline matrix as of 2026-11-02; Milestone 3 design evidence reviewed 2026-11-05.** A design commitment is not the same as a qualified model/endpoint combination. Current per-model/region/account status belongs in generated `model-support.md` with evidence dates.

| Adapter | Live | Native provider batch | Managed bulk-live | Rubric | Pairwise | Notes |
|---|---|---|---|---|---|---|
| OpenAI Chat Completions | target | target | yes | generative | generative | Use official OpenAI SDK; model-dependent structured output and tiers |
| OpenAI Responses | target | target | yes | generative | generative | Use official OpenAI SDK; model-dependent structured output and tiers |
| OpenAI Decisions | target | not available | yes | typed score, 2-10 levels | typed choice | Currently GPT-6 Luna; no generated explanations; 1-400 questions/request; cache policy handled separately |
| Anthropic Messages | target | target | yes | generative | generative | Official SDK; token counting/batch/caching controls may vary |
| Gemini Developer | target | target | yes | generative | generative | `google-genai` SDK; batch via generateContent |
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
records and pure extractors; real integrations remain proposed. No endpoint/model
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
- `auth status` and its Python API are implemented in Phase 3. Separate opt-in `auth verify`
  implementations remain deferred and will use documented protected non-generative operations where
  available; authentication acceptance does not qualify an assessment model.
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
