# Assess synthetic responses with Gemini Developer

**Current assessment contract:** new preparations use [presentation revision 2](renderer-revision-2.md), including positional CJ aliases. Historical native evidence
remains scoped to revision 1. New `auto` requests use text until separately
qualified for revision 2; explicit `native` and strict unknown `auto` fail before
submission. Historical sealed requests keep their original commitments.

Phase 5C implements ordinal rubric scoring and pairwise comparison using the
official `google-genai==2.34.0` SDK, endpoint `models.generate_content`, API
`v1beta`. This is the **Gemini Developer API** at
`https://generativelanguage.googleapis.com/`. Vertex/enterprise, ADC and the
Interactions API are separate integrations.

The [accepted four-case live qualification](gemini-live-qualification-acceptance.md)
on 2026-11-10 establishes exact-model scope for `gemini-3.1-flash-lite`:
single-trait ordinal scores and forced-choice pairwise winners, without
explanations or ties, in text and native `response_json_schema` modes. Production
native authority contains exactly `rubric_scores` and `pairwise_forced_choice`
for the SDK/API/endpoint above. Other features and models remain unknown.
Implementation and deterministic SDK fixtures alone establish no live support;
standard tests still use synthetic keys and intercepted HTTP transports.

Install from a reviewed source checkout with the Google extra:

```bash
pip install '.[google]' 'google-genai==2.34.0'
judgmentllm --help
```

This does not publish a package. The existing dependency lock already contains
the reviewed SDK. Execution rejects another installed SDK version; core-only
offline validation and preview construct no SDK client and resolve no key.

Copy [examples/gemini](../examples/gemini/) and replace `YOUR_EXACT_MODEL` with an
exact model ID. Character IDs such as `001` remain strings. These commands are
offline and work without credentials:

```bash
judgmentllm validate --config examples/gemini/rubric.yaml --json
judgmentllm plan --config examples/gemini/rubric.yaml --json
judgmentllm templates preview --config examples/gemini/rubric.yaml --item-id 001 --trait 01
judgmentllm plan --config examples/gemini/pairwise.yaml --json
```

New execution requires `schema_version: 3`, `provider: gemini` and
`endpoint: models.generate_content`. Historical configurations and sealed runs
retain their original protocols. A syntactically valid unknown model ID is
allowed; model availability and assessment behavior remain unknown.

## Output and settings

`prompt.structured_output: text` includes the complete neutral schema in the
protected prompt. Assessment JSON is validated without repairing JSON, removing
Markdown fences or changing identifiers. Native and auto policies require an
explicit `prompt.native_mechanism`:

- `gemini.generate_content.response_schema` uses the SDK's typed `Schema`.
- `gemini.generate_content.response_json_schema` preserves the JSON Schema form.

Native production requests require reviewed authority for the exact model, route,
SDK/API and requested assessment features. `auto` chooses native only with that
authority; otherwise it records text selection when the existing strict policy
permits it. Strict unknown capabilities fail before submission. No response
failure authorizes a native-to-text fallback or a replacement request.

The existing frozen Google projections remain unchanged. The SDK emits uppercase
types and snake_case schema fields for `response_schema`; the JSON Schema route
preserves its field casing. Projection representation, SDK retention, documented
API support and independently demonstrated serving enforcement are separate facts.
Trait completeness, uniqueness, category membership, nonblank explanations and
tie consistency remain strictly validated locally. Very complex schemas may be
rejected by the service; local size limits are not proven compilation limits.

Rubrics support independent and grouped traits, scores only, inline explanations,
`scores_first` and `explanations_first`. Separated arrays join by exact trait ID.
Complete valid siblings can survive an invalid grouped member. Optional exemplars
and custom templates retain the shared sandbox and protected-content safeguards.
Pairwise supports forced winners, optional explanations, and explicit ties with
the established compatible custom-template policy. Original and presented item
orders remain distinct. Selected categories produce category-code scores, with
unknown probabilities and expected scores.

Those broader mappings are fixture-tested. Live native qualification covers only
the two feature sets above; grouped traits, explanations, alternative ordering,
ties and `response_schema` remain unqualified. Explicit native requests for them
fail preflight. The acceptance record also documents why historical frozen
requests for this model require their original source checkout after promotion.

Supported `generation` mappings are `temperature` (0–2), `top_p` (0–1), and
`max_output_tokens` (positive integer). Omitted controls remain omitted: the
adapter supplies no sampling or output-token default. The SDK serializes sampling
numbers as floats; the original requested representation remains committed.
The documented candidate model's output ceiling is 65,536; other model ceilings
remain unknown. Model-specific restrictions can still reject a request.
The live matrix exercised only `max_output_tokens=512`. Temperature/top-p were
omitted; aggregate generation `live_qualified` remains false because the current
descriptor cannot express that partial qualification.

Each request explicitly asks for one candidate. Tools, thinking controls, stop
sequences, arbitrary provider JSON, headers, URLs and generation seeds are not
exposed. `execution.tier` and explicit caching fail preflight. `off` and `auto`
omit explicit cache controls; Google may still apply implicit caching. No cache
saving or deterministic-output guarantee is made.

## Credentials and explicit operations

Execution uses the shared resolver with **`GOOGLE_API_KEY` before
`GEMINI_API_KEY`**. Configure a key securely in the invoking process, outside YAML,
arguments, source and evidence. Blank sources and malformed selected sources
follow the established resolver rules. A configured key says nothing about
authentication, account access or model qualification.

The resolved key is passed explicitly to a fixed Developer client with
`enterprise=False` and `vertexai=False`. ADC, cloud identity, debug recording,
alternate URLs, proxies, redirects and ambient certificate paths cannot select
the authenticated transport. A boundary verifies the exact endpoint, key header,
allowed headers and complete outgoing body before forwarding.

`auth status --require gemini` stays offline. Explicit future
`auth verify --provider gemini` and `providers models --provider gemini` each
perform at most one bounded non-generative SDK models-list read. They never
paginate automatically and do not qualify any assessment capability. These
commands were tested only with synthetic HTTP responses in this phase.

`judgmentllm run --config ... --store ...` is a live operation and can incur
charges; invoke it only when live submission has been explicitly authorized.
Planning, importing, validation and normal CI never run it against Google.

## Responses, usage and recovery

Only one completed candidate, index zero (or omitted default), model content and
one nonblank non-thinking text part can become a successful assessment. The finish
reason must be `STOP`, without blocking feedback or a continuation token. Safety
blocks, refusals, truncation, continuation, tool calls, missing/multiple candidates,
unsupported parts and invalid assessments remain unsuccessful. Text parts and
candidates are never concatenated. Provider error messages are not diagnostics.
The [documented HTTP 400 `API_KEY_INVALID` ErrorInfo code](https://ai.google.dev/gemini-api/docs/generate-content/api-errors) and HTTP 401 map to
`credentials_rejected`; permission and rate-limit failures have separate fixed
codes. Arbitrary error prose is never used to infer authentication state.

Full capture can retain exact recognized assessment text, including invalid JSON,
as a protected diagnostic artifact. Minimal capture keeps only its safe metadata,
hash and length. Thought text, signatures and arbitrary envelope fields are
excluded from retained derivatives. Assessment explanations do not authorize
reasoning retention. Components are limited to 1 MiB and HTTP bodies to 8 MiB.

Usage belongs to one physical observation, independent of grouped trait count.
Prompt tokens include cached input; candidate tokens and thinking tokens stay
separate. Reported totals include those classes and any reported tool-prompt
tokens. No missing total is calculated and absent counters remain null. Unknown
counters do not establish new semantics. No exact monetary cost is inferred.

Automatic continuation and function calling are explicitly disabled. SDK/HTTP
retries are zero, including errors and rate limits. The HTTP guard also rejects a
second forwarding within one invocation. There are no discovery or token-count
requests during generation. Concurrency bounds connections and handoffs; connect,
write and pool timeouts are ten seconds, read timeout sixty seconds and the SDK
operation deadline ninety seconds.

The existing run/status/resume/cancel/retry/export lifecycle owns durable IDs and
attempts. Definite HTTP rejection differs from uncertain acceptance. A missing
non-retrievable response stays unresolved; restart never blindly resubmits it.
Completed observations and immutable historical exports survive retry. Explicit
subset retries require existing lifecycle authorization and a new attempt.

## Unexecuted live qualification proposal

This section preserves the proposal/execution workflow and its historical anchor.
The merged-source plan at `a77e700f550e68b382c1734d270b8bfa9eb8b181` was subsequently
authorized and completed successfully. See the
[acceptance record](gemini-live-qualification-acceptance.md) for exact plan/report
digests and reviewed scope. Any future qualification still needs fresh consent.

The proposed candidate is exactly **`gemini-3.1-flash-lite`**. Official
[model documentation](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite),
[structured output guidance](https://ai.google.dev/gemini-api/docs/structured-output),
[generateContent reference](https://ai.google.dev/api/generate-content) and
[pricing](https://ai.google.dev/gemini-api/docs/pricing) were reviewed on
2026-11-10. They document text output, structured output, a 1,048,576 input-token
limit and paid standard rates of $0.25 input / $1.50 output per million tokens,
including thinking output. These were proposal assumptions; the accepted calls
establish only the recorded scope. Recheck availability and rates before future
consent.

From a clean committed checkout, prepare and inspect four synthetic cases:
rubric text/native and pairwise text/native. Native cases propose only the
`response_json_schema` route; the other route remains separately unqualified.

```bash
python scripts/qualify_gemini.py plan --source-commit FULL_SOURCE_SHA \
  --max-output-tokens 512 --max-total-reserved-tokens 34816 \
  --proposed-budget-usd 0.02 --evidence-dir /external/new-gemini-plan
python scripts/qualify_gemini.py report --evidence-dir /external/new-gemini-plan
```

The plan contains exact committed source, SDK/API/model/endpoint, complete physical
bodies and hashes, stable submission IDs, concurrency one, zero retries and no
continuation. Full synthetic assessment capture is explicit; reasoning retention
is none. Read `plan.json` and `plan.sha256` with a local file viewer. `report`
verifies the digest and reconstructs expected bodies without writing or calling a
provider. Read-only review does not create execution authority.

Each input reservation counts complete body UTF-8 bytes, repeats native schema
bytes, adds 4096 for provider framing/instructions, rounds up by 1024 and applies
an 8192 floor. The four short cases reserve 32,768 estimated input and at most
2048 requested output tokens: **$0.011264**, with a proposed **$0.02** ceiling.
Tokenization and injected overhead are unproved, and the endpoint has no enforced
input-token cap here; this is an estimated reservation, not a guaranteed bill.

The repository executor is a separate, billable operation. Its implementation
was verified offline before the separately authorized execution accepted above.
For any future execution, generate a **new** plan from the intended clean
committed source, recheck official model availability/pricing, and independently
review its bytes and digest before obtaining exact-plan/source billable and full
synthetic capture authorization.
The PR #29 proposal is historical; its source does not authorize this executor.
Do not edit a proposal to enable it. `execution_enabled: false`, zero authorized
requests and the deferred status in plan version 1 remain its original immutable
zero-authority declarations. Execution uses a separate version-1
`execution-authorization.json` bound to the plan and source.

Only after that separate consent, a future operator may invoke:

```bash
python scripts/qualify_gemini.py execute --evidence-dir /external/new-gemini-plan \
  --authorize-plan-sha256 EXACT_REVIEWED_PLAN_SHA256 \
  --authorize-source-commit EXACT_REVIEWED_MERGED_SOURCE_SHA \
  --accept-billable-calls --accept-full-synthetic-capture
```

This is limited to the four original cases, sequentially, at most 512 requested
output tokens per call, one candidate, no retries, continuation, replacement,
discovery or token-count calls. The $0.02 ceiling is conservative estimated
**authorization policy, not a provider-enforced billing cap**. The SDK can use
thinking within its output budget; no thinking text or signatures are retained.

The private Gemini guard validates source, plan, exact prepared bytes, prompt and
payload hashes, physical IDs, case, mechanism and unused allocation at the actual
HTTP forwarding boundary. SQLite FULL-synchronous consumption and an exclusive
fsynced intent both precede forwarding. A crash after consumption may prevent a
call from reaching Google; it permanently consumes that allocation regardless.
Do not delete, relocate or reset evidence to retry. Reopening can recover already
retained responses, never replace an uncertain call. Definitive HTTP rejection,
refusal, truncation, missing response or identity mismatch stops the remaining
matrix. Only a normally completed, durably terminal assessment with exact model
identity and retained diagnostic can permit the next committed case, including
strictly invalid JSON; invalid assessments never qualify.

`report` is deterministic, read-only JSON and does not retry, resume or mutate
anything. It distinguishes consumed allocations (a conservative upper bound on
physical forwards) from unknown exact network delivery, outstanding, unresolved
and completed cases, local validity, model identity, finish/refusal diagnostics,
evidence integrity and actual reported usage. Missing usage stays null. Estimated
actual cost requires reported input, candidate and thinking counters and explicit
zero cached input; otherwise cost remains unknown. Reservations stay in the exposure estimate when
actual costs are unknown; a reported overrun stops the remaining calls. Thinking is added to candidate
output once, and usage is never multiplied by trait count. Estimates are not
invoices. Full synthetic capture includes invalid recognized assessment text;
unsafe components remain withheld under the existing privacy boundary.

Official documentation identifies `modelVersion` as the generating model version
but supplies no independently verified mapping for a version-specific response to
this requested ID. Qualification therefore accepts **only** exact
`modelVersion == "gemini-3.1-flash-lite"`. Missing identities, `models/` prefixes,
suffixes, aliases and substitutions stop qualification. Requested and actual
reported identities remain separate in the source-linked durable evidence/report.
A future alias mapping needs documented evidence and separate code review.

All four cases must be valid, identity-verified and backed by intact evidence for
an overall successful report. A report grants no production capability promotion.
Historical OpenAI/Anthropic rules and exports remain unchanged. Production
promotion is a separate reviewed registry edit recorded in the acceptance record;
no arbitrary report grants run/resume authority. Acceptance/promotion makes no
additional live request and authorizes no package publication.

The [executor verification record](gemini-qualification-executor-verification.md)
describes offline controls and the exact-head acceptance boundary.
