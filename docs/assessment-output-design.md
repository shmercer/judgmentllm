# Milestone 3 assessment outputs, provider evidence, and authentication

**Status: proposed, documentation only; 2026-11-05.** This is the authoritative
consolidated Milestone 3 design, prepared from `origin/main` at
`d6ab99f` after Subphase 6 merged in PR #11. It proposes no changes to the
Milestone 2 engine, package version, installed CLI, or implemented schemas.
[Data contracts](data-contracts.md), [persistence contracts](persistence-contracts.md),
and [Python API](python-api.md) remain the descriptions of working behavior.
The [six complete prompt examples](assessment-prompt-examples.md) and
[provider evidence register](assessment-provider-evidence.md) are supporting
appendices; this document resolves cross-cutting policy. Documentation evidence
is not an implementation or live qualification claim.

## 1. Baseline and extension boundaries

Source inspection covered `config.py`, `generation.py`, `prompts.py`,
`results.py`, planning/preparation manifests and identities, `providers/protocol.py`,
mock/ledger, `execution/processing.py`, receipts, repository/schema, immutable
exports, lifecycle/CLI control models, synthetic tests, seven R gates, and CI.

| Implemented baseline | Proposed extension |
|---|---|
| Strict schema-version-1 RunSpec, sparse generation controls | Opt-in version-2 run configuration for new controls; v1 remains readable |
| `scores` with per-entry optional explanation, strict whole-output validation | Three wire layouts; existing per-trait normalization |
| Grouped interpretation salvages independently valid members by trait ID | Same rule applied across score/explanation arrays |
| Pairwise wire requires explicit `tie`; normalized results retain it | New generative forced-choice wire needs only winner |
| Sandboxed Jinja, exact protected blocks and exemplars | Omit empty built-in exemplar section; ordered output instructions |
| Mock/synthetic execution only; immutable receipt/observation identities | Generative adapters plus shared auth and request evidence |
| Nullable token fields in results and reserved attempt counts | Authoritative request/observation usage sidecars, no grouped duplication |
| Full/minimal evidence capture; no reasoning-specific policy | Explicit authorization for sensitive provider reasoning |

All requests remain single assessment calls. No score-then-explain stages,
dependent calls, automatic rescoring of completed/invalid outputs, provider
switching, arbitrary provider JSON, user-defined output schemas, statistical
models, or public HTTP services are introduced. Native Decisions/Jev assessment
work stays in Milestone 4. Batch/bulk orchestration stays in Milestone 5.

## 2. Typed configuration and immutable identity

Illustrative **future version-2 configuration fragment**, combined with the
usual input/rubric/provider/model fields:

```yaml
schema_version: 2
execution:
  grouping: grouped
  explanations: true
prompt:
  output_order: scores_first
  structured_output: auto
  file: templates/my_scoring_prompt.j2
generation:
  reasoning:
    effort: medium
    capture: summary
capture: full
```

| Owner | Addition/default | Validation and commitment |
|---|---|---|
| `ExecutionSpec` | No new ordering or auth field | Existing `grouping=independent`, `explanations=false`, fixed/auto rules remain |
| `PromptSpec` | `output_order: together/scores_first/explanations_first`, default `together` | Rubric-only meaningful control; nondefault requires explanations and a generative endpoint |
| `PromptSpec` | `structured_output: auto/native/text`, default `auto` for v2 | `native` requires compatible documented schema mechanism; `text` explicitly selects textual enforcement plus local validation |
| `GenerationSpec` | Optional `reasoning: ReasoningSpec`; omitted/null/empty = unset | No default effort; optional `effort: low/medium/high`; `capture: none/summary/provider_text`, default `none`; only non-null requested members committed |
| Adapter-owned typed controls | Provider-specific reasoning budgets/modes/efforts where necessary | Separate registered typed models, no generic dict; reject overlap/conflict with neutral effort; add only with adapter evidence/tests |
| Top-level capture | Existing `full/minimal`, default `full` | Retention ceiling; requesting summary/provider_text explicitly authorizes retaining that type, and requires `full` |
| Capability models | Output mechanism/subset/order; reasoning request/exposure types; usage semantics; auth and verify operation | Scoped provider/endpoint/model/SDK evidence, nullable unknowns; no credentials |
| Auth diagnostics | Separate `CredentialStatus` and verification models | No credential configuration in RunSpec; ephemeral environment inspection |

Explicit `scores_first` or `explanations_first` with explanations disabled fails
preflight even for one trait; do not silently ignore it. `together` remains
accepted for scores-only and is harmless for pairwise; pairwise nondefault order
fails. Known native endpoints reject generative layout/reasoning controls they
cannot support. Explicit unsupported reasoning effort/type fails before calls;
unknown reasoning support cannot justify a speculative request. Existing unknown
generation capability diagnostics remain subject to `strict_capabilities`.
Never infer effort from capture or enable reasoning just to obtain an assessment
explanation. `capture: none` can accompany requested effort.

Version-1 run files, prompt previews, mock protocol, frozen sources/manifests,
IDs and reexports retain their original behavior. Version 2 is an explicit
configuration contract boundary; do not change global defaults or rewrite v1
artifacts. New generative winner-only output and empty-exemplar built-in rendering
belong to the v2 renderer/contract path. Historical custom prompts remain usable
with the documented legacy forced-choice compatibility parser. A user wanting
new features opts into v2; copying a template alone does not change a frozen run.

For v2, commit requested layout, explanation setting, schema policy, sparse
reasoning settings, capture authorization, renderer/output-contract versions,
selected traits/categories and ordered grouping in immutable specification and
request context. Keep logical item/trait/pair/task identities stable. Commit the
resolved enforcement mechanism, neutral schema digest, translated schema bytes,
ordered property sequence and exact prompt/payload bytes before submission.
Canonical semantic hashing may sort keys; **ordered schema serialization must
not**. Include ordered property sequences in semantic commitments so sorted
hashes cannot erase an ordering change. Current `json.dumps(sort_keys=True)` in
`prompts.py` is a future implementation touchpoint, not changed here.

Resume must reproduce the original committed representation. Changed settings,
translation, prompts, schema mode or bytes require a new request/run commitment;
a newer capability discovery cannot silently rewrite an unfinished payload.
Recheck known incompatibilities without modifying the sealed specification.
Secrets, credential identities and credential status are absent from commitments.

## 3. Single-call rubric output contract

Exactly one provider call per configured trait group per authorized attempt,
independent of layout and independent/grouped/auto grouping. Independent groups
still use the same array contract with one trait. Existing retry authority is
unchanged and never becomes a second explanation-generation stage.

`together` with explanations (backward-compatible default):

```json
{"scores":[{"trait_id":"cohesion","category_label":"high","explanation":"Ideas progress logically."},{"trait_id":"grammar","category_label":"medium","explanation":"The writing contains a tense error."}]}
```

`scores_first`:

```json
{"scores":[{"trait_id":"cohesion","category_label":"high"},{"trait_id":"grammar","category_label":"medium"}],"explanations":[{"trait_id":"cohesion","text":"Ideas progress logically."},{"trait_id":"grammar","text":"The writing contains a tense error."}]}
```

`explanations_first`:

```json
{"explanations":[{"trait_id":"cohesion","text":"Ideas progress logically."},{"trait_id":"grammar","text":"The writing contains a tense error."}],"scores":[{"trait_id":"cohesion","category_label":"high"},{"trait_id":"grammar","category_label":"medium"}]}
```

Without explanations, use the existing `{"scores":[{"trait_id":"cohesion",
"category_label":"high"},...]}`. Forbid explanation fields, including null.

### Generated schema and local validation

Generate a neutral JSON Schema (2020-12 semantics) from actual request membership,
not user text. The following construction fully specifies each schema; full
concrete serialized schemas accompany every [prompt example](assessment-prompt-examples.md).
No public user-schema escape hatch is provided.

1. For each selected trait T create a closed object with `trait_id: {const: T}`,
   `category_label: {type: string, enum: T's exact ordered labels}`; both required.
   `together` with explanations adds required `explanation: {type: string,
   minLength: 1}`. Every object has `additionalProperties: false`.
2. `scores` is an array with `minItems=maxItems=N` and `items.oneOf` the disjoint
   per-trait objects. Scores-only and separated layouts forbid inline explanation.
3. For separated layouts, `explanations` is an array with the same N bounds;
   each closed object requires `trait_id: {type: string, enum: selected IDs}`
   and `text: {type: string, minLength: 1}`.
4. Root is closed, with exactly its required arrays. Insert `properties` and
   `required` as `[scores]`, `[scores, explanations]`, or `[explanations, scores]`
   for the chosen layout. Together score entries request trait_id, category_label,
   explanation in that order. Schemas do not enforce object serialization order.
5. Local relational validation additionally requires exactly one score per
   selected ID, and exactly one explanation per selected ID when separated;
   reject blank/whitespace-only explanation, numeric labels, nulls, unknown
   fields, wrong types, duplicate IDs and unknown IDs. Array order never joins
   scores and explanations. Match exact strings with no case/Unicode repair.

Strict whole-output validation rejects any missing, duplicate, unknown,
unattributable or malformed entries. Duplicate JSON object keys are invalid;
never use last-key-wins parsing. Invalid JSON, wrong root keys/types or missing
entire required arrays make the envelope unrecognizable; every expected member
is unsuccessful. Refusals, truncation and provider errors retain distinct states.

Execution salvage remains a separate operation, as in `execution/processing.py`:
from a recognizable envelope, index both arrays by trait_id, bounded by group
membership. For each expected trait require one valid score and, when requested,
one valid explanation. Duplicate either side invalidates that trait even if one
candidate is valid. Missing/invalid explanation cannot produce a complete score.
Unknown/unattributable entries produce content-free diagnostic counts and never
results; independently valid expected siblings can still complete. This does
not label the whole output valid. Persist one outcome per attempted member
atomically: missing, duplicate, malformed/category/explanation failures remain
`invalid`; successful siblings remain `complete`; request/run partial accounting
continues to reflect failures. No implicit retry or probability fabrication.

Normalize the validated score and joined explanation through the existing
`normalize_rubric_result`; keep category code versus expected score semantics,
all identities and selected/modal distinctions. No new normalized rubric format.

### What order means

| Observation | Permitted conclusion |
|---|---|
| Requested layout in prompt/schema | Desired visible order |
| Exact decoded model-output JSON substring, inspected with ordered key pairs | Actual emitted key order for that observation |
| Endpoint documentation plus matching offline/live evidence | Scoped provider enforcement/qualification claim |
| Scores/explanations appear first | No conclusion about internal model reasoning order |

Store requested layout and observed top-level key sequence separately. Ordering
mismatch is a diagnostic, not a reason to discard otherwise valid assessment
values or rescore. A valid but differently ordered response cannot qualify the
ordering feature. Preserve the original response-body/output-string bytes under
authorized full capture; SDK object reserialization cannot establish exact wire
order. Adapters must use a supported raw-response hook or mark wire evidence
unavailable. For streams record the supported raw event stream and reassembled
output with derivation metadata. Minimal capture retains only nontext order
metadata and original-byte hashes, never the body. If retention redacts reasoning,
mark retained evidence as derived; it cannot be advertised as the exact original.

## 4. Templates and optional exemplars

Retain `SandboxedEnvironment`, `StrictUndefined`, the AST allowlist, limits and
post-render protected-block accounting. `{{ output_instructions }}` remains the
single required insertion of the generated contract/instructions; users can
edit assessor role, procedure, evidence requirements, explanation style and
section arrangement around it. No new placeholder is needed. Schema generation
and local validation operate regardless of custom prose. Reject templates that
remove/duplicate/alter protected rubric, response, output or supplied exemplar
blocks; preflight cannot establish whether arbitrary prose is semantically wise.

Zero exemplars: v2 built-ins omit the heading and section; custom templates may
omit `exemplars`. Advanced existing context supports:

```jinja2
{% if exemplar_items %}
REFERENCE EXAMPLES
{% for example in exemplar_items %}
{{ example['block'] }}
{% endfor %}
{% endif %}
```

When supplied, every selected exemplar appears exactly once, unchanged, in
selected ID order; unused/missing/duplicated/reordered exemplars fail. Never
select, truncate, summarize or drop to meet provider limits. Unknown context
capacity is diagnosed and obeys strict preflight. Keep presentation aliases and
original pair orientation unchanged. The six appendix examples provide editable
source, fully rendered prompt, complete inline contract and representative
response, plus compact native-mode instructions. They are design examples, not
new installed templates.

## 5. Provider-native structured outputs

Resolve before prompt rendering and request commitment:

| Policy | Behavior |
|---|---|
| `native` | Require compatible endpoint mechanism and a tested translation; unsupported/unknown fails preflight |
| `text` | Render complete neutral schema in `output_instructions`; local validation mandatory |
| `auto` | Prefer native when compatible documented mechanism and fixture-tested mapping exist; otherwise use text with explicit diagnostic; strict unknown support fails |

`auto` is the user's explicit preflight fallback policy. Show chosen mechanism,
limitations and unresolved support in validate/plan/preview. Do not fall back on
schema rejection, refusal, timeout, authorization error or malformed completion
after submission. Do not switch endpoints/models or use a tool invocation as an
undocumented substitute. Raw JSON mode alone is not schema enforcement.

Adapters translate the neutral contract into endpoint-specific schema subsets.
Record the exact subset/dialect, unsupported keywords, enforcement versus local
checks, SDK/API version and evidence scope. A native projection may omit a
constraint the provider cannot express only when the same strict local check
remains mandatory and the limitation is explicit; it cannot relax accepted
assessment values. If the endpoint cannot represent required field types,
category enums, or closed output fields, reject `native` and diagnose `auto`'s
text choice. Relational completeness/uniqueness is always locally enforced.
Disjoint `oneOf` may translate to `anyOf`, `const` to single-value `enum`;
unsupported bounds/nonblank constraints may be local-only. Never drop categories,
truncate schemas/exemplars or change group membership to satisfy schema limits
unless the original `auto` grouping policy authorizes deterministic planning.

For OpenAI, Responses uses `text.format`; Chat uses `response_format`.
Strict schemas require closed objects and all declared fields required; nested
alternatives have subset limits. Official documentation describes schema key
ordering, but a JudgmentLLM ordering claim still needs the exact tested endpoint,
model and feature evidence. [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
Vertex native compatibility remains pending where closed fields cannot be
expressed in the documented subset; use explicit text fallback until verified.
Other provider translations and ordering restrictions are in the
[evidence register](assessment-provider-evidence.md); no cross-provider ordering
guarantee is inferred.

Avoid repeating the full schema in native textual prompts when the endpoint's
documented practice permits: retain terse JSON-only instructions, trait identity,
explanation requirements and requested visible order, with complete schema in
the native request. Some providers recommend/require schema repetition; follow
that evidence instead of claiming universal savings. Commit exact rendered
instructions, neutral/native schemas and nonsecret body evidence for reproduction.

Offline fixtures must cover each supported subset, closed fields, per-trait
categories, native/text selection and rejection, refusals/incomplete responses,
ordered schema bytes and responses that reorder fields. Live qualification must
exercise actual native enforcement, not simply receive coincidentally valid JSON;
record submitted mechanism/schema and supported constraints without overclaiming
what a small positive test establishes.

## 6. Generative comparative judgment

New forced-choice generation requires a closed object with required `winner_id`,
whose string enum is exactly the two **presented** response IDs:

```json
{"winner_id":"essay_002"}
```

When requested, add required nonblank `explanation`; otherwise forbid it. The
new forced-choice generation schema omits `tie`. Its local compatibility parser
also accepts the historical closed shape with an explicitly boolean `tie: false`;
reject null/true/string/number tie and unknown winners/fields. Normalize missing
tie as false. Historical v1 schemas/parsers stay available for old commitments.
Do not accept a separated rubric schema for pairwise output.

Tie-enabled generation requires both winner_id (two IDs or null) and boolean tie:

```json
{"winner_id":null,"tie":true}
```

A non-tie is `{"winner_id":"essay_002","tie":false}`. Generate a closed root
schema with those properties, a required explanation only when requested, and
`oneOf` consistency branches `(null,true)` or `(winner enum,false)`. Translate
provider subsets explicitly and always check this relation locally. Omission of
tie is invalid under tie policy. No abstention state is added.

Existing `PairwiseResult` still contains tie=false for wins, null winner for
permitted ties, all original/presented order, repetitions/reversal provenance,
probabilities and R outcomes. Do not infer a winner from probabilities. Decisions
and Jev use their native contracts in Milestone 4, without assuming generative
winner JSON, explanations, ordering or supported generation controls.

## 7. Usage, reasoning and durable evidence

Use three linked layers: unchanged per-trait/pair assessment results; request/
submission/attempt/observation metadata and usage; authorized detailed evidence.
Do not duplicate usage or reasoning per grouped trait.

### Request usage

Proposed strict `ProviderUsage` public sidecar version 1, one current record per
physical submission, includes:

- `run_id`, `request_uid`, `attempt_id`, `submission_id`; nullable `observation_id`,
  remote response ID and definitive failure reference as applicable.
- Provider/endpoint, requested and reported model, SDK version, observation kind,
  nullable timestamps/durations with clock source, and `usage_source` identifying
  the provider's reported object/semantics version.
- Nullable nonnegative strict integer `input_tokens`, `output_tokens`,
  `total_tokens`, `reasoning_tokens`, `cache_read_tokens`, `cache_write_tokens`.
- An allowlisted typed provider usage breakdown preserving original names,
  reported numeric values, units and inclusion/overlap semantics. No arbitrary
  provider payload/error dictionary in minimal metadata.
- Nullable amount/currency/cost source only when reliably reported; estimates
  separately labeled with date/pricing source. Unknown remains null.

Extract available usage independently of assessment validity, refusal or provider
error. Definitively failed invoked attempts can have a usage record with null
observation; do not fabricate acceptance. Preflight failures incur no submission
or token record. Uncertain acceptance stays unknown, never assumed unbilled.
Preserve every physical invocation's existing identities, including retries.
Disable/coordinate SDK hidden retries so one adapter invocation cannot conceal
multiple billable calls from durable evidence. Do not invent reconciliation or
remote retrieval when a provider lacks it; uncertainty blocks resubmission.

Retain allowed usage under **minimal and full** capture. Reopened exports must
not need raw responses to reconstruct usage. Store sidecar facts durably with the
observation/failure receipt before completing normalization; receipt replay uses
unique identities, not additive counting. Optional later authoritative usage
updates are append-only revisions with source/version, never rewritten evidence;
export a revision-selected snapshot plus explicit revision audit.

Do not derive missing total as input+output and present it as reported. Cache and
reasoning counts can be components of input/output or separate billing classes;
never add all columns together. For example an OpenAI reasoning count is a detail
of output usage; summaries are separately exposed response text, not a complete
internal trace. [OpenAI reasoning guide](https://developers.openai.com/api/docs/guides/reasoning).
Provider-specific definitions and fixtures must document overlap before any cost
calculation. Missing/partial counts remain null, including in failures.

Existing result token fields/CSV columns remain for backward reading and
reexport of historical records. New real adapters leave those fields null and
link authoritative usage through evidence; do not remove fields or copy counts
into grouped results. Request-level attempt counters are references/summaries of
the same fact, never independent additive usage.

### Reasoning retention

| Text | Scope and treatment |
|---|---|
| Requested assessment explanation | Existing per-trait/pair `explanation`; part of assessment output |
| Provider-exposed reasoning/thinking/summary | Observation-level evidence with exact documented type |
| Original provider body/event stream | Optional exact evidence; may contain both kinds and other content |

`ReasoningSpec.effort` requests computation; capture authorizes retention. They
are independent. `capture: summary` requests a documented summary if a provider
needs an explicit exposure flag, and retains only that type. `provider_text`
permits documented exposed thinking blocks, not inaccessible internal reasoning.
Record actual `kind: summary/thinking_block/other_documented`, provider field/type,
ordinal, nullable artifact reference, source-byte hash, and `availability:
retained/not_exposed/not_returned/not_retained/redacted`; no fake empty text.
`other_documented` additionally requires a nonsecret documented type identifier.
Do not map arbitrary assistant text to reasoning or attribute it to traits without
verified attribution. Missing reasoning is not an assessment failure when the
provider simply did not expose it; explicit unsupported requests fail preflight.

A non-none capture setting is explicit authorization for that type and requires
full capture; reject it with minimal. Full alone does **not** authorize retaining
provider reasoning. Suppress exposure controls where possible when capture=none.
If reasoning arrives anyway, discard it from retained response evidence and
persist only a marked sanitized derivative, original hash and retention reason.
Never label reserialized/redacted bytes as original. If exact raw capture cannot
be separated from unauthorized sensitive blocks, do not retain original bytes.
SDK debug logs, exception strings, crash dumps and temporary files must obey the
same policy. Full authorized artifacts use immutable safe relative references,
content hashes and lengths in a local protected store; exports are opt-in and
never print reasoning text to control stdout or routine diagnostics.

### Persistence version boundaries

Keep existing manifests, results, receipt proofs and append-only identities.
New v2 run manifests/renderer identities and provider evidence records require
explicit version dispatch. New usage/reasoning sidecar models/exports start their
own version 1 and document kinds. If repository tables/receipt fields must change,
introduce a checked internal schema/receipt revision with compatibility tests;
do not treat existing strict records as extensible JSON. Prefer narrow linked
tables and new-run stores; no automatic migration of old registries. Old stores
remain inspectable/exportable with supported readers; a new store may be required
for v2 execution. Normalized results and canonical/R assessment columns stay v1.
The existing CLI envelope stays v1; newly typed command data is additive by
command, not a change to existing lifecycle command payloads.

## 8. Secure authentication resolution

A small shared resolver has two operations: offline inspection returning only
safe typed status, and execution-time construction of an ephemeral SDK auth
handle. Adapters, verification and smoke tests use the same provider registration,
source precedence, blank handling and endpoint restrictions. Never implement a
parallel diagnostics auth chain. SDK installation is separate capability evidence.
No credential values in RunSpec, YAML, templates, manifests, payload artifacts,
results, exports or qualification records. No prefixes/suffixes/fingerprints,
headers, private account names, project identities or credential file paths in
status output. Permit only constant public source identifiers.

For API keys, inspect presence without printing values; whitespace-only is
missing. Do not strip/rewrite nonblank keys before SDK use; reject malformed
whitespace-bearing keys safely rather than alter them. With multiple Gemini
variables, ignore blank values and select the documented higher-precedence
nonblank source; diagnostics and execution use the same selection and may
report shadowed source names without any values. Construct the
SDK with the selected key explicitly where needed to prevent divergent fallback,
including Together's optional Colab secret source.
No implicit `.env` loading or arbitrary credential-file reads. Cloud identity
execution can use documented official ADC behavior; offline status cannot call
ADC discovery, metadata endpoints or token refresh.

| Provider ID | Initial source/auth type | Offline result | Live verification design |
|---|---|---|---|
| `openai` (Chat/Responses/Decisions) | `OPENAI_API_KEY`, api_key | configured/missing | Documented models-list read candidate; endpoint/model access unproven |
| `anthropic` | `ANTHROPIC_API_KEY`, api_key | configured/missing | Authenticated models-list where SDK/permission verified |
| `gemini` | `GOOGLE_API_KEY` before `GEMINI_API_KEY`, api_key | configured/missing | Models-list candidate, must prove authentication is checked |
| `vertex` | Google ADC, application_default_credentials | unknown without safe sufficient evidence; no discovery | No generic harmless auth-only operation established; unsupported initially |
| `together` | `TOGETHER_API_KEY`, api_key | configured/missing | Documented v3 `whoami()`; discard private identity response |
| `ollama` local | none, none | not_required | Connectivity via local documented read; does not verify authentication |
| `typesafe` (Jev) | `TYPESAFE_API_KEY`, api_key | configured/missing | Documented models-list candidate, independent of M4 assessment work |

OpenAI's official SDK uses the named environment variable.
[OpenAI quickstart](https://developers.openai.com/api/docs/quickstart).
The [Models API](https://developers.openai.com/api/reference/resources/models)
documents model discovery; verify its protected read and rejection mapping with
the locked SDK before exposing the check.
All other source/precedence/verification claims, citations and SDK differences
are recorded in the [provider evidence register](assessment-provider-evidence.md).
Provider IDs are proposed registration names, not evidence that adapters exist.
Anthropic's broader SDK identity mechanisms are outside the initial API-key
registration; fail closed without its key instead of invoking an undocumented
fallback. Ollama cloud/remote authentication is outside ordinary local scope.
ADC may be configured even when status is unknown; unknown never means invalid.

Authenticated transport headers/query keys are transient. Immutable request
body evidence commits exact **nonsecret** generation/schema/prompt bytes, not an
HTTP request including authentication. Remove auth-bearing URL/query/header
values before any evidence pipeline; never hash/store them as commitments.
Use SDK-supported separation or explicitly define a sanitized body commitment
when an SDK mixes auth and payload. Record that boundary; never claim exact
whole-transport retention. Sanitize errors by allowlisting codes and mapped safe
messages, not regex-redacting arbitrary SDK strings. Never log exception repr,
request URLs containing keys, credential-bearing response echoes or debug headers.

## 9. Offline auth status and explicit verify

Proposed Python API (not yet importable):

```python
from judgmentllm.auth import credential_status

statuses = credential_status()  # tuple[CredentialStatus, ...], no network
for status in statuses:
    print(status.provider, status.status)
```

`credential_status(*, providers: Sequence[str] | None = None)` returns a tuple of
frozen strict `CredentialStatus`: `provider`, `auth_type`, nullable `source`,
`status` (`configured/missing/unknown/not_required/unsupported`), nullable
allowlisted `diagnostic_code`. Stable registration order; requested filtering
preserves that order, rejects unknown IDs, and deduplicates repeated IDs.
No SDK creation, refresh, metadata call, browser, daemon probe or credential read.
A known mechanism unavailable on this platform is unsupported; an installed SDK
is not needed just to inspect a supported environment variable. Configured means
only a plausible source was present, never valid/verified.

```bash
judgmentllm auth status
judgmentllm auth status --json
judgmentllm auth status --require openai,anthropic --json
judgmentllm auth verify --provider openai --json
```

Status human columns are Provider, Credential source, Status; show explicit
`unknown` for ADC and `not_required` for ordinary local Ollama, with no connectivity
claim. Repeated `--require` comma lists are unioned; unknown IDs/empty tokens are
usage errors. Only configured/not_required satisfy required providers. Ordinary
status returns 0 even with missing credentials; unmet requirements return 2,
validation error, while retaining all status rows.

Use the existing [CLI version-1 envelope](cli-spec.md), not a competing bare
`providers` response. Example ordinary status (illustrative environment):

```json
{"schema_version":1,"command":"auth status","status":"ok","run_id":null,"run_status":null,"artifacts":{},"data":{"providers":[{"provider":"openai","auth_type":"api_key","source":"OPENAI_API_KEY","status":"configured","diagnostic_code":null},{"provider":"vertex","auth_type":"application_default_credentials","source":"google_adc","status":"unknown","diagnostic_code":"offline_identity_unknown"}],"required_providers":[],"requirements_met":true},"error":null}
```

Offline Python callers make required-provider decisions from these same enums;
the future R wrapper parses `data.providers` and checks process exit separately.
No auth diagnostic is copied into assessment manifests/exports.

`auth verify` is **always explicitly invoked**, never part of status, plan,
validate, resume or smoke preflight. Use at most one suitable authenticated,
non-generative read; no automatic pagination/retries. Candidate operations in
the evidence register need fixture tests proving unauthenticated/rejected keys
do not receive the same public response. No guarantee they are free. If no
suitable operation is established, return unsupported and refer to explicitly
authorized smoke testing; do not issue a model request, token-count call, IAM
probe or quota-changing operation. Ollama may report `not_required` plus a separate
connectivity state; no-auth connectivity cannot mean credentials accepted.

Typed `AuthVerification` data records provider, source identifier, operation,
`verification_status`, safe diagnostic code, and date; never a credential identity.
Statuses: `authentication_accepted`, `credentials_rejected`, `permission_denied`,
`network_unavailable`, `rate_limited`, `unsupported`, `inconclusive`, and
`not_required`. Definitive invalid-key evidence is required for rejection; HTTP
403 is permission denied, 429 rate limited, transport/5xx unavailable, ambiguous
responses inconclusive. Success proves only that one operation accepted transport
auth. It proves no particular model, endpoint, tier, batch or schema support.

Exit 0 accepted/not_required; 1 rejected/denied/unavailable/rate-limited;
2 unsupported/invalid arguments/configuration; 3 inconclusive. Map to existing
execution/unsupported_capability/validation/partial_completion error categories.
Preserve stdout/stderr separation and handled interruption 130. No check result
automatically changes a qualification classification.

## 10. Explicit live smoke qualification

Proposed command; never called in this design task or mandatory CI:

```bash
judgmentllm providers smoke --provider openai --endpoint responses \
  --model MODEL_ID --allow-billable --max-requests 2 --max-output-tokens 256 --json
```

No implicit model. `--allow-billable` is required for potentially billable remote
model calls; it does not authorize arbitrary extra calls. Local Ollama requires
explicit smoke invocation but no billable flag. Default two model calls: one tiny
synthetic rubric request (two traits in one group) and one winner-only forced
choice. `--max-requests` is a strict positive integer at most four, counting all
physical model submissions, with no automatic retries. Default output cap 256
per request must be mapped to an actually supported cap; otherwise fail preflight.
No model listing or auth verification is hidden in the request count. Bound
input size, concurrency=1 and timeout; uncertainty consumes its submission slot
and blocks blind retry. Token limits and spending estimates are conservative
controls, not a technically unenforceable hard currency ceiling.

Use the same resolver and adapters; preflight checks offline configuration and
capabilities, not a hidden auth request. Exercise normalization, provider usage,
exact endpoint/model provenance, native schema when supported and requested,
and optional reasoning only with `--reasoning-capture summary/provider_text` plus
full capture authorization. Record requested versus returned model. An absent
usage report remains unknown and limits qualification of the usage feature.
Two calls do not qualify every layout/tie/reasoning combination; separate explicit
feature runs use fresh bounded authorizations. Never increase limits to complete
a failed smoke automatically. Synthetic responses only; no real student text.

A typed version-1 qualification record contains provider/endpoint/requested and
reported model, SDK/package and schema/adapter versions, UTC date, command
controls, exercised features, per-feature results/failures/limitations, native
mechanism/schema and order evidence, request/observation/evidence references,
reported usage and reliable actual cost (else null). Credentials, private
identities and auth headers are excluded. Publish only the nonsecret record;
raw evidence is local and follows retention policy. CLI returns the standard
control envelope with durable record path, not reasoning/raw content.

Classifications remain `discovered`, `implemented`, `fixture_tested`,
`live_qualified`. Credential presence, installed SDK and auth success cannot
promote a provider. Qualification is per endpoint/model/feature combination;
failed/missing features remain explicit. Documentation changes or successful
fixtures alone do not establish live support. Prerelease qualification seeks
one rubric and pairwise success per available generative adapter, without making
mandatory CI credential-dependent or claiming unavailable accounts were tested.
Where practical run a local R/jsonlite round trip on the captured normalized
exports; record unavailable R separately from provider behavior. Mandatory R
acceptance always uses deterministic offline synthetic provider fixtures.

## 11. Streaming exports and R joins

Assessment exports stay unchanged, all attempts retained. Add `usage export` and
`reasoning export` commands and corresponding iterator-based Python services as
specified in [CLI](cli-spec.md) and [Python API](python-api.md). JSONL is canonical;
CSV uses documented null escaping (`\N`), nested JSON cells, and an R-compatible
view using the established configurable NA marker/collision rejection. Numeric
unknowns stay missing; IDs always import as character, including leading zeros.

Usage snapshots include all submissions, even without assessment rows; snapshots
record evidence revision high-water marks alongside result snapshot identity.
Reasoning export requires explicit request and returns only authorized retained
records/references; absent/redacted types retain metadata with null paths/text.
Text-bearing exports are written to explicit files, never control stdout.
Use bounded pages and atomic immutable publication; no unbounded in-memory joins.

Provide a bridge with one row per `(run_id, task_id, attempt_id)` to existing
`request_uid`, `submission_id`, nullable `observation_id`, and usage identity,
derived from existing repository result/attempt links. Do not add these IDs to
every normalized result schema. A grouped call links many task rows to one usage
record. Multiple observations/revisions require explicit revision selection;
never infer a one-to-one relationship by provider ID alone.

R callers can join on run/request/attempt through the bridge, but **aggregate
usage before joining to trait rows**. Sum one selected record per physical
submission, preserving retries; never sum per-trait token columns, or multiply
cost by grouping size. Retrieval/replay of the same observation adds no usage.
Use provider-documented disjoint billing classes only; do not add total plus its
components. Report known subtotals and unknown-call counts, rather than coercing
missing usage/cost to zero. Analysis selects attempts explicitly; audit exports
retain unsuccessful originals. Keep complete probability/mapping arrays,
original/presented orientation and R `result` semantics unchanged.

Required offline Python/CLI-to-R fixtures must verify many-traits-to-one-usage
joins, retries and no-result failures, unknown fields, leading zeros, all layouts,
explanations, probabilities, score kinds and omission semantics. Extend required
R/jsonlite CI gates in M3; missing Rscript/jsonlite or failed assertions fails
Ubuntu/Python 3.11 and all seven local lanes. The future `rubricLLM` wrapper passes
these same configs and commands at run/group/file granularity, with no R auth or
assessment engine and no required HTTP server.

## 12. Deterministic acceptance and review sequence

| Area | Mandatory future offline acceptance |
|---|---|
| Rubric | Three layouts, scores-only, independent/grouped, one call/group, different trait orders, missing/duplicate/unknown/malformed entries on either array, wrong categories/types/nulls, blank/missing explanations, whole validation versus salvage, atomic complete/invalid outcomes, no implicit rescore |
| Prompts | Six exact rendered examples, zero exemplars and conditional sections, all supplied exemplars exactly once/in order, protected integrity, custom templates, limits fail without truncation, v1 preview/identity unchanged |
| Schema | Neutral generator by trait/category/tie policy, supported subset translation, unsupported constraints and capability diagnostics, explicit fallback before commitment only, native/text prompt forms, requested/serialized/observed order, raw hook versus reserialization, refusal/truncation |
| Pairwise | Winner-only plus historical explicit false, tie true/null versus false/winner, unknown winner/fields, optional explanations, reversed presentation/repetitions/probabilities, unchanged normalization/R semantics |
| Usage | One fact per submission, grouped nonduplication, partial/missing/cache/reasoning semantics, unsuccessful attempts/retries/uncertainty, receipt replay and immutable revisions, full/minimal retention, no hidden SDK retries |
| Reasoning | Explanations versus exposed text versus raw evidence, effort separate from capture, summary/type fidelity, explicit full authorization, minimal incompatibility, unsolicited blocks redacted, null/availability behavior, no grouped duplication |
| Auth | Missing/blank/configured sources, Gemini precedence/conflicts, unsupported mechanism, cloud unknown/no network, Ollama no-auth versus connectivity, multiple requirements, stable JSON/exits, sanitized synthetic invalid auth/SDK errors, resolver/adapter parity |
| Security | Canary secrets absent from stdout/stderr/logs/temp/manifests/payloads/exports/qualification; echoed body/URL/header/exception tests; no secret fragments/hashes; no implicit .env/file reads; offline status socket/refresh guards |
| Qualification | Offline fixtures credential-free; live disabled by default, explicit model/billable flag and submission/output limits, no hidden verification, feature-scoped classification, captured Python/CLI/R consistency |
| Compatibility | Genuine v1 frozen runs/readers/prompts/mock remain stable; new configuration version explicit; unchanged normalized/R columns, sparse controls, all attempts, orientation, mappings, probabilities and missing values |

[Implementation plan](implementation-plan.md) assigns small independently
reviewable PRs: neutral schemas/normalization; templates; shared auth/status;
provider evidence/usage/retention foundation; generative adapters in provider-sized
PRs with native mappings and supported verify; usage/reasoning exports and R
acceptance; opt-in smoke/qualification. Dependencies must be complete before an
adapter is marked implemented. M4 reuses auth/evidence for native decisions;
M5 extends parity/recovery to bulk/batch; M6 exposes implemented controls/diagnostics
through the thin CLI-first wrapper. No feature starts in this design PR.

Before publication retain exact-commit seven-lane local grid evidence, quality,
full offline tests, package/smokes and all 49 R gates per repository policy;
coverage is blocking only on Linux/3.11. Hosted CI must then qualify the exact
final PR head on its five required configurations, including hosted 3.15;
Windows/3.11 remains manually available. Do not weaken thresholds or edit the
separate CI optimization work. Record environment failures separately and do
not claim unavailable checks passed. Documentation consistency checks validate
links, fenced JSON, six example contracts, source-name consistency, proposed
status labels and documentation-only diff.

## 13. Review decisions and remaining evidence

Review this design's deliberate choices together: explicit opt-in RunSpec v2;
`structured_output=auto` as preflight fallback authorization; field-order mismatch
as diagnostic; request-level usage with legacy result token fields left null for
new adapters; capture setting as explicit reasoning authorization under full;
initial Anthropic API-key-only resolver; conservative unknown ADC status.
These are concrete proposals for approval in this design PR, not implemented
behavior or additional work authorized after it.

Remaining provider evidence is explicitly bounded in the register: exact locked
SDK/endpoint schema subsets, raw-response hooks, protected verification operations
and model-specific reasoning/order/usage behavior must be confirmed in each
implementation PR. In particular Vertex identity/constructor behavior and Together
v3 verification must use current SDK contracts, rather than older examples. These gaps block
claiming those features supported, not writing credential-free adapters with
explicit unsupported diagnostics. No unresolved decision requires paid requests
or expanding scope. Stop at the consolidated reviewable design PR; no merge,
release or Milestone 3 implementation.
