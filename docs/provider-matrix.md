# Provider and endpoint compatibility: implementation and targets

## PR C production Decisions addition

[Native OpenAI Decisions](openai-decisions.md) now uses the ordinary V4 installed
Python/CLI lifecycle with official SDK 3.42.1 and offline synthetic acceptance.
Production acceptance, privacy-filtered replay and physical provenance have
additive versions; historical synthetic contracts remain exact. Normalized/R
schema 1, SQLite3 and the seven strict local R gates remain unchanged. Live
qualification, Jev/Gemini integration and release remain deferred.


## PR B implementation boundary (2026-11-15)

RunSpec V4 shared-input preparation, explicit CJ scheduling, grouped lifecycle,
physical accounting and narrow provenance export are implemented with the own
`judgmentllm-decisions-offline-v1` synthetic protocol only. Its limits and local
counter fixtures establish no vendor limits or support. No live qualification,
provider calls or credential access are part of B. OpenAI Decisions (C), TypeSafe
Jev (D), Gemini Decisions and separately reviewed SDK compatibility migration (E)
remain dependency-ready follow-ups. No Google package pin changed in B. See
[the implemented contract](decisions-lifecycle.md).

The [README's current route table](../README.md#providers-endpoints-and-execution-modes)
lists executable Python/CLI modes separately from projections and planned adapters.
Implemented generative routes require V3 and `live`: OpenAI `responses` and
`chat.completions`, Anthropic `messages`, and separate Gemini Developer/Vertex
`models.generate_content` adapters. Public V1/V2 mock execution and V4 synthetic
Decisions are offline. V3 private synthetic transports are fixture infrastructure.
Native batch and managed bulk-live remain M5 targets for every provider below;
the table does not establish implemented batch support.

New generative preparations use [presentation revision 2](renderer-revision-2.md).
Retained original-contract live acceptance does not supply its required
`assessment_presentation_v2` authority: new explicit native/strict unknown auto
requests reject, and nonstrict auto selects text. Fixture coverage, current text
implementation and exact-scope historical live qualification are distinct facts.


**Baseline design evidence: 2026-11-02/05; implementation corrections: 2026-11-16.**
A design commitment is not a qualified model/endpoint combination. Current
authority is scoped by the reviewed production registries and dated acceptance
records linked below. A generated per-model/region/account `model-support.md`
remains a planned reporting surface.

**Issue #61 update, 2026-11-15:** M4 now requires three native adapters and
shared-input multi-question requests; Gemini Decisions is separate from Developer
`generateContent` and Vertex. The [PR A design](native-decisions-design.md)
records official-doc evidence, proposed contracts, unknowns and review gates.
All three native rows below are targets, not implemented or live-qualified
features. Together/Ollama live completion is lower priority, still on the roadmap.
First alpha waits for an explicit post-M5 scope/batch-readiness go/no-go; this
does not remove the intended all-provider qualification requirement.

| Adapter | Live | Native provider batch | Managed bulk-live | Rubric | Pairwise | Notes |
|---|---|---|---|---|---|---|
| OpenAI Chat Completions | implemented; reviewed live scope accepted | target | later milestone | generative | generative | Phase 5A, SDK 3.42.1; exact-model capability unknown until qualified |
| OpenAI Responses | implemented; reviewed live scope accepted | target | later milestone | generative | generative | Phase 5A, SDK 3.42.1; exact-model capability unknown until qualified |
| OpenAI Decisions | implemented PR C candidate; live qualification pending | unsupported in planned adapter | M5 target | typed score, 2–10 levels | typed choice | `/v1/decisions`, current documented `gpt-6-luna`; 1–400 ordered named questions; complete probabilities; OFF requires explicit cache mode without breakpoints |
| Anthropic Messages | implemented; live acceptance pending | target | later milestone | generative | generative | Phase 5B; SDK 2.3.0 / API 2026-10-21; text JSON and controlled native output_config.format |
| Gemini Developer | implemented; reviewed four-case live scope accepted | later milestone | later milestone | generative | generative | Phase 5C; google-genai 2.34.0 / v1beta; [exact qualified scope](gemini-live-qualification-acceptance.md); broader mappings remain fixture-only |
| Gemini Decisions (Developer only) | implemented PR E candidate | unsupported | M5 target | typed score, 2–255 levels | typed choice, two presented options | [Developer v1beta](gemini-decisions.md), `gemini-decision-1`, 1–1,000 named keys, 256 KiB exact body cap and unverified token fit |
| Vertex AI Gemini | implemented; historical exact-route live scope accepted | M5 target (Google Cloud setup required) | M5 target | generative | generative | google-genai 2.34.0/v1beta1; independent ADC/API-key private-project/global scopes in [accepted evidence](vertex-live-qualification-acceptance.md); current presentation is unqualified |
| Together AI | lower-priority live target | target, subject to current API qualification | M5/deferred live target | generative | generative | Use official Together Python SDK v3 (`together>=3,<4`); endpoint/model feature coverage must be tested |
| Ollama local | lower-priority live target | no native provider batch | M5/deferred live target | generative | generative | Official Ollama SDK, local daemon; local model discovery; optional later cloud support separate |
| TypeSafe AI Jev | implemented in PR D candidate; exact-source and live acceptance pending | unsupported | M5 target | typed score; documented maximum 10 levels | typed choice | [Pinned System One route](typesafe-decisions.md) with approved non-strict admission and both context fits unverified |

### Native Decisions capability evidence (PR A only)

Official sources checked 2026-11-15: [OpenAI guide](https://developers.openai.com/api/docs/guides/decisions)
and [SDK reference](https://developers.openai.com/api/reference/python/resources/decisions/methods/create),
[TypeSafe System One](https://docs.typesafe.ai/api/system-one),
[Score](https://docs.typesafe.ai/primitives/score) and
[Choice](https://docs.typesafe.ai/primitives/choice), and
[Gemini Decisions](https://aistudio.google.com/docs/decisions-api).
The earlier 2–10-level M4 acceptance language applied to OpenAI/Jev, not the
neutral category schema or Gemini's distinct range. Jev's unknown question cap
must not become 400 or 1,000 by analogy; a user maximum and verified context/body
limits bound grouping. Unknown evidence stays explicit.

All three plans require a one-question baseline and automatic eligible grouping
of the same essay or explicitly identified pair presentation. No unrelated
samples, reversal/repetition merging or mixed controls. One physical usage fact
joins many logical results. Every provider needs its own synthetic SDK/lifecycle
and strict R fixtures before implementation acceptance; docs establish no live
access, exact reported model, pricing, or empirical parity.

Google's documented Decisions SDK requirement `>=3.2` conflicts with both current
`pyproject.toml` Google constraints `>=2.34,<3` and exact 2.34.0 production
Developer/Vertex authority. PR E must resolve and qualify those existing functions
before changing dependencies. PR A upgrades nothing. See the
[compatibility gate](native-decisions-design.md#google-sdk-compatibility-gate-pr-e).

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
adapters; Phase 5B adds Anthropic Messages, Phase 5C adds Gemini Developer and
Phase 5D adds Vertex AI Gemini. Together/Ollama live runners and production
Decisions adapters remain planned. No endpoint/model acquires runtime support or
live qualification merely from documentation or fixtures.

- Shared credential resolution uses official SDK mechanisms: provider-specific
  API-key environment variables, Google ADC for Vertex, and no authentication
  for ordinary local Ollama. The ledger records actual source names, precedence
  and SDK alternate-source constraints. Offline status never initializes ADC,
  refreshes tokens, contacts metadata or checks daemon connectivity.
  **Vertex Phase 5D:** implements separately selected Google Cloud authorization
  API-key and ADC routes. They remain distinct from Developer API credentials and
  from each other's reviewed project/location scope. Offline status does not
  initialize either route; a stored `vertex` keyring entry is not automatically
  used. See the [Vertex guide](vertex.md).
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
- Native OpenAI Decisions/Jev/Gemini Decisions integration stays in Milestone 4. Managed
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

At Phase 4C3 this infrastructure activated no real provider adapter, model,
authentication or endpoint and supplied no production native acceptance. Later
Phase 5 records below remain independently scoped; complete canonical local
validation remains mandatory. Implementation acceptance requires retained
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
