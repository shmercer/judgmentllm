# Milestone 3 provider evidence ledger

**Reviewed 2026-11-05. Documentation research only; no provider request or live qualification.**
This supports the normative [assessment output design](assessment-output-design.md).
It records provider documentation, planned integration policy and remaining
qualification separately. Documentation is mutable: implementation PRs must pin
SDK versions and preserve the exact endpoint/model/feature evidence exercised.
The baseline lock contains Anthropic 2.3.0, google-genai 2.34.0, Together 3.0.0,
Ollama 0.6.3 and typesafe-sdk 0.7.5. Installation establishes no capability.
OpenAI-specific evidence is recorded in the authoritative design.

## Authentication sources and offline limits

The following is the **proposed JudgmentLLM resolver policy**, not a claim that
the underlying SDK supports only these sources. API-key resolution examines
process environment variables only; absent, empty or whitespace-only values are
missing. Never print or persist a value, fragment, hash, credential identity or
credential-file path. An explicit runtime key is passed to the official SDK only
after resolution succeeds, avoiding undocumented fallthrough when it is missing.
Provider-required key syntax checks may reject unusable values with sanitized
diagnostics; `configured` establishes presence, never validity.

| Provider ID | Proposed source / type | Offline status rule | Official evidence and constraints |
|---|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` / `api_key` | Nonblank key: configured; otherwise missing | [SDK 2.3.0 client](https://github.com/anthropics/anthropic-sdk-python/blob/v2.3.0/src/anthropic/_client.py) accepts an explicit key. Its default chain additionally considers `ANTHROPIC_AUTH_TOKEN`, profiles and federation; these are outside the initial resolver policy. Explicit key resolution prevents diagnostics/execution disagreement. |
| `gemini` | `GOOGLE_API_KEY`, then `GEMINI_API_KEY` / `api_key` | Select the SDK-precedence source; configured or missing | [Google Gen AI SDK](https://googleapis.github.io/python-genai/) documents both names and precedence of `GOOGLE_API_KEY`. Document shadowed sources by name only. A blank higher-priority variable is treated as absent consistently by diagnostics and execution. |
| `vertex` | `google_adc` / `application_default_credentials` | Unknown by default; no discovery, file opening, metadata request or refresh in status | [ADC search order](https://docs.cloud.google.com/docs/authentication/application-default-credentials) is `GOOGLE_APPLICATION_CREDENTIALS`, local gcloud ADC, then attached service account via metadata. An environment path alone does not prove usable ADC. Execution uses the official ADC chain, including supported federation/cloud identities. |
| `together` | `TOGETHER_API_KEY` / `api_key` | Configured or missing | [Together authentication](https://docs.together.ai/docs/api-keys-authentication) and [quickstart](https://docs.together.ai/docs/quickstart) document the environment source; constructor `api_key` overrides it. The [SDK client](https://github.com/togethercomputer/together-py/blob/main/src/together/_client.py) can also consult Colab secrets; initial JudgmentLLM resolution deliberately does not. |
| `ollama` | `local_service` / `none` | `not_required` for ordinary local operation, without probing the daemon | [Local authentication](https://docs.ollama.com/api/authentication) requires no authentication at localhost:11434. Cloud inference, private models and remote authenticated proxies are separate capabilities and cannot inherit this status. |
| `typesafe` | `TYPESAFE_API_KEY` / `api_key` | Configured or missing | [SDK quickstart](https://docs.typesafe.ai/sdk/python) establishes the actual name. [Client reference](https://docs.typesafe.ai/sdk/python/api/clients/sync) specifies explicit-option precedence and key validation. No invented Jev-specific key variable. Assessment execution remains Milestone 4. |

No `.env` loading is introduced. SDK recommendations to use python-dotenv do not
constitute SDK automatic loading or JudgmentLLM authorization to open `.env`.
Vertex project/location are execution configuration, not proof of credentials;
status does not expose private project/account/service-account identities.
The [locked Google client](https://github.com/googleapis/python-genai/blob/v2.34.0/google/genai/client.py)
accepts `enterprise=True` and a legacy `vertexai` flag. Current docs redirect
Vertex pages to Gemini Enterprise Agent Platform. Keep JudgmentLLM's `vertex`
provider identity; fixture-test the selected SDK flag and endpoint explicitly,
including preventing Developer API-key environment settings from selecting the
wrong authentication mode. Do not rename public providers because docs moved.

## Feasibility of explicit live authentication verification

These are candidate implementations for the separate, opt-in `auth verify`
operation. No operation below is asserted to be free. None may run during
`auth status`, preflight, ordinary CI or this design task. One bounded request,
short timeout and disabled automatic SDK retries are the conservative default.
Only an explicitly authenticated protected response supports
`authentication_accepted`.
A public catalog or connectivity response cannot establish authentication.

| Provider | Documented non-generative candidate | Proposed Milestone 3 disposition |
|---|---|---|
| Anthropic | [GET /v1/models](https://platform.claude.com/docs/en/api/models/list), via SDK models list; request one result | Implement using shared API-key resolution and sanitized status mapping. Proves only acceptance for that operation. |
| Gemini Developer | SDK `models.list()` is documented in the [Google SDK](https://googleapis.github.io/python-genai/) | Candidate; require exact protected-endpoint/API-key rejection evidence and bounded pagination fixtures before exposing verification as supported. Current direct REST documentation retrieval was unavailable during this review. Until established, return unsupported/inconclusive and point to authorized smoke testing. |
| Vertex Gemini | ADC refresh by itself is not an inference-service check; a generic public model catalog is insufficient | No universally suitable check established here. Return unsupported until a documented protected, non-generative operation with the required project/location/IAM scope is qualified. Do not silently substitute token counting or generation. |
| Together v3 | [`client.whoami()` / GET /v3/whoami](https://docs.together.ai/docs/sdk-v3-migration-guide#whoami-returns-a-typed-identity) | Implement. Discard its private nested identity response; retain only authentication acceptance and safe operational metadata. Do not reuse pre-v3 models-list assumptions. |
| Ollama local | No credential is required | Report `not_required`; daemon health may be reported separately through an explicitly requested connectivity read. Never label a listening daemon authentication-verified. |
| TypeSafe | [GET /v1/models](https://docs.typesafe.ai/api/list-models), SDK `client.models.list()` | Implement shared credential verification independently of the Milestone 4 Jev assessment adapter. The documented endpoint requires Bearer authentication. No System One question is needed. |

HTTP 401 is rejected only when documented as credential rejection. HTTP 403 is
permission/access denied, 429 rate limited, timeouts/connection/5xx unavailable;
ambiguous bodies, unknown errors, or an unprotected response are inconclusive.
Never serialize SDK exception text or verification response bodies. In
particular Together's identity response is not a qualification artifact.
Authentication success establishes neither model access nor structured-output,
reasoning, service-tier or batch support.

## Native schema enforcement and visible order

Every adapter must retain the full local contract and report the exact native
subset applied. Unsupported native keywords may be removed only through a
reviewed adapter translation that explicitly identifies the constraints enforced
locally; do not claim full native enforcement of those constraints. The
`structured_output: native` policy requires a compatible tested translation as
defined in the authoritative design; limitations such as cardinality remain
explicit and locally enforced. Reject native mode when its required core schema
cannot be represented. There is no separate complete-native-enforcement option.
No property-order statement below establishes internal reasoning order.

| Provider / endpoint | Documented mechanism and subset | Order and implementation qualification |
|---|---|---|
| Anthropic Messages | `output_config.format` JSON Schema; basic types, scalar enums, const, anyOf, internal references, required, closed objects. Array cardinality above minItems 1 and string lengths are not supported. | Required properties precede optional ones; use all required properties for the chosen layout. Refusals and max-token stops can violate the structure. [Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs). Exact trait uniqueness/completeness and nonblank explanations remain local. |
| Gemini Developer | `response_mime_type='application/json'` plus `response_schema` / JSON-schema route in [SDK examples](https://googleapis.github.io/python-genai/) | Verify the exact API version/model and keyword translation; do not assume Developer and Vertex share subsets. Direct current structured-output guide could not be fetched in this review, so its detailed subset/order guarantees remain pending. |
| Vertex Gemini | Cloud schema subset includes anyOf, string enums, items, min/maxItems, nullable, properties and required; unsupported fields can be ignored. `propertyOrdering` controls emission order. | Explicitly generate propertyOrdering and consistent prompt examples. Cloud guide advises against duplicating the full schema in prompt text. [Cloud structured output](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/multimodal/control-generated-output). Local unknown-field checks remain mandatory. |
| Together Chat | `response_format` with `type: json_schema` on supported models | [Guide](https://docs.together.ai/docs/inference/chat/structured-outputs) instructs including schema text as well. Do not assume OpenAI's strict subset or property-order behavior; precise supported keywords/order remain fixture/live qualification work. |
| Ollama local chat | JSON Schema in `format` | [Guide](https://docs.ollama.com/capabilities/structured-outputs) recommends schema text in the prompt too. Precise keyword/order coverage depends on daemon/model and must be tested; cloud support is outside the local adapter. |
| TypeSafe System One | Native typed questions and answers, not generated assessment JSON | Generative layouts and explanation requests do not apply. Preserve native distributions and mappings in Milestone 4; see [System One](https://docs.typesafe.ai/api/system-one). |

Token efficiency is endpoint-specific: omit schema repetition where documented
and verified safe; retain it where provider guidance requires or recommends it
until comparative qualification establishes a safe alternative. Freeze the
actual rendered instructions and translated schema. Schema examples, arbitrary
SDK `extra_body` dictionaries and SDK automatic function execution must never
become public pass-through settings. Tool-call loops cannot implement these
single-call assessment layouts.

## Usage and provider-exposed reasoning

| Provider | Documented observations | Accounting / retention implication |
|---|---|---|
| Anthropic | [Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) distinguishes input_tokens, cache_creation_input_tokens and cache_read_input_tokens. [Thinking](https://platform.claude.com/docs/en/build-with-claude/thinking) separates thinking blocks and bills thinking within output. | Anthropic input_tokens excludes cache reads/writes: preserve that semantic and the separate breakdown. Do not count cache subdivisions or thinking twice. Thinking text may be summarized/withheld and encrypted signatures are not readable reasoning. |
| Gemini / Vertex | SDK types include `usage_metadata`, `thoughts_token_count`, cached-content counts and `Part.thought` / `thought_signature`; see [SDK](https://googleapis.github.io/python-genai/) | Record only reported values and exact fields. Establish total/input/output inclusion equations per endpoint fixture; no common accounting formula is assumed. Signatures are opaque evidence, not text. Direct Developer thinking documentation was unavailable during this review; model-specific exposure and effort controls remain pending. |
| Together | [Reasoning guide](https://docs.together.ai/docs/inference/chat/reasoning) documents model-dependent `reasoning` / `reasoning_content` and some content `<think>` variants, toggles and effort differences | Parse only documented fields for the exact model. Preserve original usage names and distinguish content from separately exposed reasoning. No universal effort mapping or complete-trace claim. |
| Ollama | [Chat API](https://docs.ollama.com/api/chat) reports prompt_eval_count, eval_count, cached prompt count and timing; think controls are model-dependent | Map reported token counts with their source paths, keep total null when absent, and do not invent a reasoning token count from thinking-text length. |
| TypeSafe | [Response types](https://docs.typesafe.ai/sdk/python/api/types/responses) document nullable usage input_tokens/output_tokens and native answer distributions | Shared request-level accounting can be reused in Milestone 4. Do not infer reasoning text or generative explanations. |

Full capture plus explicit reasoning retention authorization is required before
retaining exposed reasoning or opaque reasoning evidence. If original response
bytes contain unapproved reasoning, retain a labeled privacy-filtered derivative
and appropriate nonsecret commitments, never call it the exact retained raw
response. Minimal capture retains usage, status and safe provenance only.
Assessment explanations remain distinct normalized per-trait/judgment fields.

## Required evidence before implementation acceptance

Credential-free fixtures must test every supported request mapping, subset
translation, unsupported combination, refusal, truncated/malformed result,
partial usage/error usage, grouping and separated-array salvage. Block network
access during status tests, including DNS, metadata endpoints, token refresh and
SDK client initialization that can trigger discovery. Inject conspicuous fake
secrets and assert absence from every stdout/stderr/log/artifact/export path.

Live qualification is explicitly authorized, synthetic and model-selected,
separate from mandatory offline CI. Record SDK/API/daemon version, provider,
endpoint, model, date and feature combination, exact nonsecret request/response
evidence, visible ordering observations, usage and limitations. Inspect original
text/bytes or preserved JSON pair order before canonicalizing. Repeated polls or
stream snapshots of one call are not separate usage charges. Verify actual
Python-generated exports in R with leading-zero IDs and request/attempt joins.
Documentation research establishes candidate capabilities only; all real
adapters remain unimplemented and unqualified on this design branch.
