# Milestone 4 native Decisions and shared-input requests

**PR A, 2026-11-15: proposed contracts for independent review, not runtime
support.** [Issue #61](https://github.com/shmercer/judgmentllm/issues/61) approves
three native adapters and required multi-judgment requests. It supersedes the
older two-adapter M4 scope and the pre-M5 alpha schedule. It does not authorize
credentials, provider requests, a merge or publication.

This PR changes prose only. No configuration loader, public Python export,
endpoint registration, schema version, fixture, dependency or stored byte changes.
The typed shapes below are specifications for PR B, not accepted YAML today.
Material architecture recommendations remain subject to the decision ledger at
the end; PR B waits for that review.

## 1. Scope and existing boundaries

One essay may have 12 independent trait questions in one physical request. One
presented pair may have 13 independent criterion questions. Every question has
its own complete instructions, rubric/criterion and applicable ordered examples.
An answer cannot depend on another answer. Unrelated essays/pairs, repetitions
and presentation reversals never share a physical request. Shared input is
visible evidence for every question; equal text alone does not prove eligibility.

Native typed scores/choices are separate from generative structured JSON.
Reuse source verification, protected presentation blocks, credentials, ownership,
durable evidence, normalizers and streaming exports. Do not route a Decisions
request through `generateContent`, Responses, Chat or a schema projection.
Reject generative schema mechanisms/output-order controls, requested explanations
or ties, and every inapplicable nonempty generation setting during preflight,
before credential resolution. A default placeholder is not permission to send
an unsupported wire control; the new native configuration branch owns its fields.
No generated explanation, predicate/noul feature, mixed rubric/CJ run, media
ingestion, cross-sample packing, statistical model or new batching engine is in
this issue. M5 owns managed bulk-live and supported native asynchronous batches.

The implementation must reach these existing boundaries in its assigned PR:

| Boundary | Existing behavior and required work |
|---|---|
| `config.py`, `planning.py`, public loaders | V1–V3 use mutable `ExecutionSpec` and rubric-only `grouping/groups/max_traits_per_request`; pairwise requires independent preparation. Add an explicit new configuration boundary; do not broaden old meanings. |
| `preparation/`, `request_rendering.py`, `presentation.py` | Freeze sources, task order, exact presentation and a bounded question crosswalk before sealing. Native questions reuse protected content, not a whole generative prompt per question. |
| `providers/protocol.py`, provider-owned builders/parsers | Current pairwise contract/parsed response represents one pair judgment. Add grouped named-member contracts; keep wire formatting and score/choice extraction adapter-owned. |
| `execution/`, `persistence/` | Reuse owned transactions, immutable artifacts, acknowledgment/response/evidence receipts, original-request membership and subset retry. Add checked crosswalk/acknowledgment versions where existing records cannot express them. |
| `results.py`, `io.py`, `r_exports.py`, public lifecycle/CLI | Reuse long-form normalized results and all-attempt exports; add a lossless grouped provenance companion without copying physical usage into each row. |
| Tests, fixtures, R consumers, impact registry/inventory | PRs B–F add classified synthetic SDK/lifecycle cases and actual Python-to-R consumption. Preserve historical fixtures and all existing assertions. |

## 2. Typed immutable grouping proposal

Recommended public field: `execution.request_grouping`, type
`RequestGroupingSpecV1`, a closed discriminated union. Each branch is strict,
frozen and deeply revalidated at public/preparation boundaries, including copied
or nested mutated models. No dictionaries of arbitrary provider parameters.

| Field | Single-question branch | Automatic eligible-grouping branch |
|---|---|---|
| `grouping_version` | Literal integer `1`, default `1` | Same |
| `mode` | Literal `single` | Literal `auto` |
| `max_questions_per_request` | Literal integer `1`, default `1` | Required strict positive integer, no upper bound invented in the neutral type |

Booleans, strings, floats, zero/negative maxima, unknown fields/modes/versions
and a single-mode maximum other than 1 fail. Integer version tokens also reject
boolean/float equivalents. The new configuration's omitted field resolves to
`single`; a supplied object requires `mode`, and null/empty objects fail.
Automatic grouping is explicit. `auto` with maximum 1 is a useful
comparative baseline and remains a distinct requested policy from `single`.
There is no fixed arbitrary packing or cross-input option.

Illustrative **future configuration fragments**, not runnable configurations:

```yaml
execution:
  request_grouping:
    grouping_version: 1
    mode: single
    max_questions_per_request: 1
```

```yaml
execution:
  request_grouping:
    grouping_version: 1
    mode: auto
    max_questions_per_request: 32
```

Recommend an explicit new RunSpec boundary (V4, if still the next version when
PR B lands). Its Decisions execution branch accepts this control and rejects
legacy `grouping`, `groups` and `max_traits_per_request` rather than combining
two authorities. V1–V3 continue rejecting `request_grouping`; no automatic
conversion, default expansion or changed historical pairwise behavior. Root
version alone never authorizes production transport. Provider-specific protocol,
manifest, output, receipt and crosswalk versions remain independently dispatched.

### Eligibility and deterministic splitting

Eligibility requires the same run and assessment kind, identified source essay
or explicit pair presentation occurrence, exact shared-input bytes/hash and
source dependencies. Require identical provider, endpoint/API route, requested
model, SDK/wire policy, account/routing scope, credential **source policy**,
mode/tier, cache policy, generation intent, capture/output retention and
presentation version/settings. Secrets and private credential identities never
enter the grouping key. Runtime credentials cannot change source midway through
a group. Different question instructions, scales and applicable examples are
expected; they are committed per question rather than made common instructions.

Use input order, then rubric trait order or the explicit pair-presentation
member order. Never sort by model output, hash-table iteration or arrival time.
Build the longest next contiguous eligible prefix that fits all of:

1. The requested positive maximum (1 in single mode).
2. The exact endpoint question cap, when independently documented. An unknown
   cap is null, not infinity or a copied vendor limit; the user maximum still
   bounds memory and membership.
3. The exact serialized body-byte ceiling and complete context ceiling, including
   shared input, every question's instructions/criteria/examples and framing.

Measure bytes after actual pinned-SDK serialization, before forwarding. Record
each known limit's source/date/scope, counter/version and count or conservative
upper bound; estimates must be labeled. Do not infer a Decisions context window
from a generative model's window. Unknown provider ceilings remain unknown;
bounded local transport ceilings still apply. Unknown/estimated context follows
the existing explicit diagnostic/strict-capabilities policy and cannot be
advertised as proven to fit. A known ceiling without a trustworthy counter or
safe upper bound blocks submission. No hidden token-count API call is permitted.

On overflow, close the prefix and start the next request, repeating the complete
shared input. If one question plus shared input cannot fit, fail before submission
with its identity, offending limit and counts. Never truncate an essay, rubric
or example, split one question, drop a criterion, or silently change controls.
Revalidate the sealed request at the final forwarding boundary; changed limits
can block it but cannot repack an existing attempt.

Source/index traversal, grouping buffers, parser limits, queues and export joins
stay bounded for 100,000+ source inputs. Use the existing indexed preparation and
SQLite membership pages; never materialize the response-by-trait product or all
pair presentations in memory. Each provider PR must audit response/body limits
against its largest supported group; do not silently increase shared safety caps.

### Pair presentation identity

`PairTask` V1 is criterion-specific; its `pair_uid` and task ID cannot be replaced
by a group ID. Recommend a new typed **explicit presentation membership** record
in PR B: `presentation_id: ID` and an ordered immutable tuple of existing
criterion-specific `PairTask` members. It binds one source occurrence and its
original/presented IDs, text hashes, repetition index, reversal flag/group and
presentation policy. Every member must agree on that evidence, have a unique
criterion/task identity and belong to that declared occurrence. Different
`pair_uid` values remain intact in results.

V1 pair inputs have no such cross-criterion proof and remain singleton groups;
matching `pair_id`, item IDs or text never invents it. PR B must expose the
versioned membership input through the ordinary Python/CLI source path, with
explicit source provenance, not a private fixture-only switch. The choice
between that additive input and an expanded pair-source schema is a review gate
(D3); neither is implemented here. Repetitions and reversals get distinct
presentation identities. Do not use `reversal_group`, which intentionally links
two orientations, as permission to combine them.

## 3. Identity, rendering and provenance crosswalk

The relationship is `logical_task -> physical_request -> named_question ->
provider_answer`. A physical attempt submits one request containing one or more
named questions; a logical task may have multiple authorized attempts over time.

| Identity/commitment | Meaning |
|---|---|
| `task_id`, item/trait/pair IDs | Preserve existing logical formulas wherever source semantics match; grouping policy cannot rename a judgment. A new input shape needs an explicit old-to-new compatibility fixture rather than a false identity claim. |
| `request_uid` | Original ordered logical request group, still the parent for subset retries. A changed grouping plan has new request identities, even when task IDs match. |
| Physical request identity | Existing run/request/attempt/submission/correlation tuple plus exact rendered body commitment; each retry has a new physical identity. Do not overload `request_uid` to mean a remote ID. |
| Named question | `q_` plus the first 60 lower-case hex characters of SHA-256 over the existing task ID's UTF-8 bytes (62 characters total). Validate uniqueness within the request and fail on a collision; never overwrite or silently suffix. |
| Ordered crosswalk | Version, zero-based question ordinal, key, task/trait IDs, source/presentation/member dependency, question kind, ordered scale/choice mapping, exact question hash/length and retry ancestry. Keys remain stable across regrouping/retries; ordinals are local to that physical request. |
| Returned answer | Exact key/name and answer type, matched to the committed crosswalk before normalization. No positional-only join; no cross-observation assembly. |

The fixed opaque question key fits Gemini's documented ASCII constraint and
avoids embedding student/source IDs in vendor names. It is pseudonymous, not
anonymization. The proposed key derivation/domain is reviewed in PR B; original
task IDs remain authoritative and collision checks are mandatory.

Keep common essay/presented-pair evidence separate from each question's complete
trait/criterion instructions, ordered level descriptions or forced-choice
options and applicable examples. Include shared rubric context where needed
for that question; never rely on another question's rubric or answer. Preserve
all selected exemplar texts/IDs/order and their inclusion mapping. An example
annotated only for another trait cannot become evidence for this question by
accidental placement in shared input. No implicit exemplar selection/removal.

Freeze the grouping policy/version and algorithm, ordered logical membership,
source/presentation hashes, full question map **and order**, scale/alias mapping,
rendered shared input and each question, SDK/API/route/model/control scope,
final nonsecret body bytes/hash/length, parser/normalization policy and physical
identity before the submission intent. Semantic sorted hashes cannot replace
ordered serialization or a final SDK-byte commitment. Full capture retains
approved exact rendered input/questions/body/response; minimal retains safe
hashes/lengths/crosswalk metadata and requires verified original sources for
reconstruction. No credentials, headers, private routing strings, unauthorized
reasoning or arbitrary returned names enter safe evidence.

## 4. Physical acknowledgment, atomic outcomes and retries

Recommended policy: acknowledge a physical submission once; commit a complete
vector of member outcomes atomically, preserving independently valid siblings
only from a recognizable, trustworthy envelope. Whole-response validity and
per-member success are separate facts, as in existing grouped rubric salvage.
This is local atomicity, not a claim of provider-side transactional processing.

### Acceptance and commit boundaries

1. Publish exact request/question commitments and membership, then journal local
   intent under existing ownership before the guarded network boundary. SDK
   retries/redirects/continuations are disabled; one attempt cannot hide calls.
2. Establish acceptance from documented authoritative evidence bound to that
   invocation. Use real remote IDs only when supplied. Native responses may
   omit them: recommend a separately versioned acknowledgment permitting null
   remote ID and committing the complete received HTTP response's status/body
   hash and invocation binding. A local observation key is explicitly local;
   never label it a provider ID. A malformed assessment does not undo otherwise
   proven physical acceptance. Missing bytes/IDs alone prove nothing (D4).
3. Apply existing privacy filtering before durable body retention. Publish
   permitted response artifacts and acknowledgment/response/evidence receipts
   before owned observation/evidence insertion. Record physical usage even when
   every assessment fails. Body hashes do not substitute for replayable content.
4. Validate the envelope and named answers in bounded memory. Commit exactly one
   outcome for every attempted member plus task/request/attempt states in one
   owned transaction. A death inside that transaction commits none; replay of
   the same verified observation commits the same vector once. Usage belongs to
   the already established physical submission, not this result transaction.

### Deterministic answer policy

| Received evidence | Required interpretation |
|---|---|
| Complete recognized envelope, every expected name/type and valid distribution | All members complete; whole response valid. |
| Missing answer, duplicated answer name, wrong answer type or invalid distribution attributable to one expected key | That member invalid; complete validated siblings retained. Whole response invalid/partial; no implicit retry. A duplicate invalidates that member even if one copy is valid. |
| Named provider refusal | That member refused; independent siblings may complete. Whole-request refusal/error applies to all members. |
| Unexpected answer name | No invented task/result; content-free unexpected count; valid expected siblings may survive, but whole response is invalid. |
| Invalid UTF-8/JSON, duplicate raw JSON object keys, unrecognizable/truncated envelope, unbound response or conflicting reported model | No successful members. Malformed received output gives invalid outcomes where observation evidence exists; identity conflicts stop as integrity errors. Never salvage last-key-wins SDK dictionaries. |
| OpenAI named array violates committed relative order among uniquely attributable expected answers | Detect by names, never misjoin by index; reject the ordered envelope and complete no members. Missing/duplicate/unexpected names follow their separate rules above and are excluded from this relative-order check. Map-returning providers may reorder map keys without failure. |
| Partial reception/lost response without terminal authoritative evidence | Remain unresolved; do not fabricate missing/invalid results or assume nonacceptance. |

Score answers must have every index exactly once; choice answers must have every
committed option exactly once. Adapter-owned conversion maps explicit indices
or keys to rubric order before the unchanged strict normalizer; it is not repair.
Validate returned score labels and any legend against the committed index/label
and description mapping. Reject
unknown/missing/duplicate keys, nonfinite/out-of-range values and sums outside
the committed existing tolerance (default `1e-8`); never renormalize or round.
Preserve the native expected index, complete probabilities, reported confidence
and explicit provider selection separately. A distribution mode is derived
evidence, never a provider-selected rubric label or an observed CJ winner.

### Crash and retry rules

| Durable boundary at restart | Permitted next action |
|---|---|
| Prepared, never crossed `submitting` | Verify original sources/bytes and use the existing unsubmitted preparation. |
| `submitting`/unknown forwarding or lost acceptance | Reconcile only through real receipts or a documented provider lookup; otherwise hold unresolved. Process death, timeout or cancellation does not authorize replay. |
| Accepted, response missing | Recover the same retained response or documented authoritative retrieval; otherwise hold. No new score request masquerading as retrieval. |
| Response/evidence published, observation/result transaction interrupted | Verify and replay original receipts/bytes locally; unique identities prevent a second acknowledgment, result or usage fact. |
| Terminal partial/invalid/refused outcomes | Explicit retry selects failed logical tasks under the existing retry authorization. Fresh physical request/attempt/submission commits that ordered subset, parent request/receipt and prior outcome digests; retain original unsuccessful rows. |
| Definitive transient nonacceptance | Existing bounded transport retry authority may allocate a fresh attempt with ancestry; no accepted request is replayed. |

Repeated use of the same retry operation and membership must return its existing
attempt; conflicting reuse fails. Successful members require the existing
explicit completed-task permission for deliberate rescoring. Do not regroup
failures from different original requests, inherit a different route/model/tier,
or change question semantics after a crash. A retry is a new billed physical
submission where accepted; exactly-once means local recording/interpretation,
not guaranteed exactly-once remote execution without a provider guarantee.

## 5. Accounting, results and R

One submission owns one revision-selected usage/cost record and one physical
request count, including unsuccessful and unresolved invocations. Distinguish
prepared, forwarded, acknowledged and unknown counts. Replay/retrieval is not
another submission. Authoritative usage corrections are append-only revisions
selected at a high-water mark, never added charges. Unknown amounts stay null;
no missing total, cache discount or cost is inferred from partial counters.

New adapters leave historical per-result token columns null. Use existing
`ProviderUsage` semantics and a versioned crosswalk companion, one row per
`(run_id, task_id, attempt_id)`, to link request, named question/ordinal,
submission, nullable observation/remote model/IDs, presentation, parent attempt
and usage identity. Export all physical submissions, including no-result
failures. Keep immutable result/evidence snapshot high-water marks together.
Public companion delivery is scoped to this required native provenance; it is
not implementation of every deferred M3 reasoning/export interface.

Recommend no per-question cost allocation in this issue. If later added, it
must be explicitly estimated/allocated and reconcile exactly to the physical
total; it cannot be mistaken for a provider-reported question charge. R sums
one selected usage record per submission **before** a many-to-one result join.
For two traits with physical usage 120 input tokens and reported cost 0.004,
the joined two-row audit still represents 120 and 0.004, not 240 and 0.008.
These numbers are synthetic assertions, not vendor prices or savings claims.

Normalized rubric results retain complete ordered probabilities/mappings,
mapped `expected_value` and `primary_score_kind=expected`, raw zero-based
`provider_expected_index`, separately nullable provider-selected label, derived
modal label and reported confidence. Native score alone never selects a category.
Forced-choice CJ maps exact `essay_01`/`essay_02` choices through the committed
presentation to original IDs. Preserve original `p_left/p_right`, presented
`p_first/p_second`, all criterion/occurrence/repetition/reversal IDs and every
attempt. No inferred winner, tie, explanation or automatic attempt selection.

Canonical JSONL, lossless CSV and R audit rows retain schema 1 unless PR B proves
an unavoidable incompatible change and obtains review. Add independently
versioned companion files rather than extra ambiguous result columns. Preserve
JSON cells, null escaping/NA collision rules and leading-zero character IDs.
R fixtures consume actual Python/SDK-generated exports in fresh processes under
strict jsonlite gates, including full/minimal retention and unknown evidence.

## 6. Provider evidence and adapter plans

The following is **official documentation checked 2026-11-15**, not installed
SDK verification or live qualification. Current model examples are not a neutral
allowlist. Each adapter must validate exact route/model capabilities and retain
requested versus actually reported identity; absent identity stays unknown and
cannot qualify an exact model. Generic generative controls never transfer.

| Native route | Documented request/answer shape and bounds | Implementation owner |
|---|---|---|
| `openai / decisions`, `POST /v1/decisions` | `client.decisions.create`; shared `input`, 1–400 ordered named questions and ordered answers. Score 2–10 levels; choice returns explicit selection and probabilities. Current model `gpt-6-luna`. [Guide](https://developers.openai.com/api/docs/guides/decisions), [Python reference](https://developers.openai.com/api/reference/python/resources/decisions/methods/create). | PR C: separate score/choice builder, parser and SDK-byte fixtures. |
| `typesafe / systemone`, `POST /v1/systemone` | Official `typesafe-sdk`, `TypeSafeClient.system_one(state=..., questions=...)`; named map of `Score`/`Choice` and named answers. Score maximum 10 levels; at least two recommended (neutral rubric already requires two). **Question-count hard cap not established.** [System One](https://docs.typesafe.ai/api/system-one), [SDK](https://docs.typesafe.ai/sdk/python/), [Score](https://docs.typesafe.ai/primitives/score), [Choice](https://docs.typesafe.ai/primitives/choice). | PR D: verify pinned SDK, context/body constraints, alias versus reported exact model and distinct score-index/choice maps. |
| `gemini / decisions`, Developer `POST /v1beta/decisions` | `client.decisions.create(model="gemini-decision-1", input=..., questions={...})`; 1–1,000 keys, `[A-Za-z0-9_-]{1,64}`; score 2–255 levels (2–10 recommended), choice 2–255 options, overall 262,144 tokens. Questions are text-only; shared-input media is documented but outside this issue. [Endpoint/limits](https://aistudio.google.com/docs/decisions-api), [model](https://aistudio.google.com/docs/models/gemini-decision-1). | PR E after SDK compatibility gates below; never Developer `generateContent` or Vertex. |

OpenAI's [cache contract](https://developers.openai.com/api/docs/guides/decisions#reuse-shared-input-with-prompt-caching)
defaults to an implicit shared-input cache boundary. JudgmentLLM must explicitly
send `prompt_cache_options.mode="explicit"` with **no breakpoints** for OFF;
omitting controls is not OFF. Check actual installed SDK support before execution.
Cache reads/writes are details included in input usage, not extra input totals.
Explicit semantic cache intent remains rejected until separately implemented
under [Issue #57](https://github.com/shmercer/judgmentllm/issues/57); no opt-in or
cache key is invented by grouping. Existing generic qualified OpenAI scopes do
not authorize Decisions. Native batch and generative tiers/settings fail locally
unless separately documented and implemented for this exact endpoint.

Jev Score wire probability/legend keys are zero-based strings, while its SDK
documents integer keys. Preserve exact wire evidence and deliberately translate
indices; do not substitute a choice distribution or infer a selected level from
the scalar score. No invented 400/1,000 question cap, undocumented long-context
header, cache behavior or priority control. PR D must report remaining unknown
context/body/cache facts and obey strict preflight rather than claim support.

Gemini documents independent question evaluation and shared-input accounting;
this is a provider claim, not JudgmentLLM statistical-parity evidence.
[Decisions limitations](https://aistudio.google.com/docs/decisions-api#limitations)
exclude native Batch, context caching, Flex and Priority. Reject these intents,
explanations and inapplicable generation controls before credentials. OFF means
no cache feature, with no transfer of `generateContent` implicit-cache warnings.
Its native usage uses `usage`/`total_input_tokens`, modality details and
`total_output_tokens`, not generative `usageMetadata`; keep missing reports null
even where docs describe zero output tokens. The published response shape does
not establish a returned model-version field: PR E must verify SDK/HTTP evidence
and expose unavailable reported identity, never copy requested model into it.

### Google SDK compatibility gate (PR E)

Google requires `google-genai>=3.2` for Decisions. At this base, both `[google]`
and `[all]` specify `google-genai>=2.34,<3`; the lock and Developer/Vertex
production transports use exact 2.34.0. Their converters, protected client
constructors/private SDK checks, HTTP serialization, schema routes, reasoning
spelling, timeouts and exact-scope authority are version-sensitive. Merely
widening the dependency range would leave existing functions incompatible.

Recommend one reviewed 3.x migration, with new exact SDK/protocol authority and
preserved original 2.34.0 checkouts/environments for historical reconstruction.
Two versions of the same `google.genai` distribution cannot simply be installed
as separate extras in one environment. Alternative: a separately installed
official-SDK process, with its substantial packaging/lifecycle cost; a narrow
HTTP adapter is justified only if official SDK suitability is disproven.
No option is selected in code here (D5).

Before changing constraints/lock or integrating Decisions, PR E must:

1. Inspect the chosen official 3.x release/source and supported Python/OS/extras;
   pin the actual version tested, including async/raw response behavior.
2. Compare Developer `models.generate_content` on v1beta, both native schema
   routes, text/native/auto, presentation, reasoning/sampling/service/cache
   controls and exact SDK bytes against retained 2.34.0 fixtures.
3. Do the same for Vertex v1beta1, **both ADC and explicit API-key modes**, route
   isolation, lazy credentials, no ambient fallback/proxies/retries/continuation,
   cancellation, privacy, usage and final HTTP guard.
4. Reopen genuine historical Google stores/plans with original pinned source;
   demonstrate new-reader inspection/export or give explicit original-environment
   execution guidance. Never relabel old authority/bytes as 3.x qualified.
5. Run highest-risk local/hosted acceptance, actual R export/recovery gates and
   affected optional-extra/installed wheel/sdist smokes. Offline migration cannot
   promote exact-SDK live qualification; fresh bounded live approval is separate.

An unexplained serialization/authentication change blocks migration. Preserve
existing OpenAI/Anthropic/synthetic protocols and Google historical fixtures.

## 7. Version and compatibility plan

PR B owns new independently versioned grouping, named-member/output/crosswalk
and durable receipt contracts. Use a new-store registry revision only if the
existing immutable tables cannot express required evidence; review SQL invariants
and genuine old-store tests before assigning it. Do not relax historical
`attempt_tasks` subset constraints to combine unrelated original requests.
Assign provider manifest/transport versions in C/D/E when wire bytes exist;
do not reserve a guessed chain of numeric versions in this prose.

Dispatch historical versions before interpretation. Missing new grouping data
means historical behavior/unavailable crosswalk, not synthesized multi-question
proof. V1–V3 configs, manifests 1–7, receipt versions, normalized schema 1,
CLI envelope 1, original IDs/bytes, cache/service/presentation revisions and
genuine a2/a3 histories remain readable under their supported compatibility
rules. No automatic migration, backfill, native reselection or global hash change.
Changed grouping configuration, membership, SDK/body or question semantics cannot
resume an old frozen run. A failed-member retry is the explicitly authorized
subset exception under its original policy and ancestry, not a config change.

## 8. Acceptance fixture specification and PR ownership

These are **required future synthetic fixtures**, not files or executed tests in
PR A. Each provider suite must use actual official SDK serialization intercepted
offline, plus malformed raw wire bytes before SDK decoding. No credentials,
discovery, token-count or provider network calls. B supplies shared lifecycle
fixtures; C/D/E supply independent vendor shapes; F integrates installed usage.

| Fixture | Exact required assertion | Owner |
|---|---|---|
| G1 / G2 | 1 and 2 independent questions on one essay or one explicit pair presentation; single mode produces 1/2 calls, auto max 2 produces 1/1; each logical member appears exactly once in committed order. | B, C–E |
| G12 / G13 | One essay × 12 traits = 12 tasks/1 auto call with max 32; one declared presentation × 13 criteria = 13 tasks/1 call. Single mode = 12/13 calls. Distinct per-question rubrics/scales/examples remain complete and unaltered. | B, C–E |
| Limits | OpenAI 400 -> [400], 401 -> [400,1]; Gemini 1,000 -> [1,000], 1,001 -> [1,000,1], with tiny questions and fitting bodies. User max 3: 4 -> [3,1] on every provider, including Jev; do not call that a Jev vendor cap. | B, C–E |
| Levels/keys | OpenAI/Jev 2 and 10 valid, 11 rejected; Gemini 2/10/11/255 valid, 256 rejected. Native choice keys/level maps and Gemini 1/64-character boundaries plus invalid/65-character keys checked independently. Neutral >10-level rubrics still load. | C–E |
| Context/body | Exact ceiling fits; +1 byte/token splits into the deterministic next prefix; a single oversized question or shared input fails before forwarding. Unknown counter/ceiling diagnostics and strict refusal are tested separately. | B, C–E |
| Isolation | Different essay identity even with equal text, different pair/presentation/repetition/reversal, route/model/tier/account/cache/generation/capture/presentation/source policy never combine. Unsupported controls fail before credential resolution. | B, C–E |
| Identity/order | Regroup max 1/2/32 and replay in a fresh process: logical IDs and question keys stable, requested policy/request/physical commitments distinct. Jev/Gemini map reorder joins correctly; OpenAI array order mismatch fails per section 4. Collision, changed bytes and rehashed foreign crosswalk fail. | B, C–E |
| Answers | Missing, duplicate, unexpected, wrong-type and refusal members follow the section 4 outcome table; malformed/truncated JSON and escaped duplicate keys fail before SDK last-key-wins parsing. No successful sibling is silently retried or fabricated. | B, C–E |
| Probabilities | Labels `01/02/03`, codes [1,2,7], probabilities [0.10,0.25,0.65] -> mapped 5.15, native index 1.55, mode `03`, selected label null unless explicitly supplied; reported confidence retained. Missing/extra index, bad legend, NaN/negative/>1, wrong sum and distribution/type confusion fail. | B, C–E |
| CJ | Original [001,002], presented [002,001], choice `essay_02`, presented probabilities [0.2,0.8] -> observed winner 001, original [0.8,0.2], R result 0/winner position 2. A separately observed reversal stays separate; repetition/13 criteria keep independent IDs. No choice -> no winner even with probabilities. | B, C–E |
| Atomic/recovery | Real child death before submitting, after uncertain forwarding, after acknowledgment, after response/evidence publication, during member validation and inside/after result commit. Fresh process recovers only proven observations, with zero/whole-vector visibility and no second physical acceptance. Minimal lost response stays unresolved without retrieval. | B, F |
| Retry/accounting | Two-member response: first complete, second missing; explicit retry of second gives a new one-question physical request with original key/task, parent receipt and authorization. Original results retained; same retry ID creates no third call. Usage 120 then 30 sums to 150 once, not per trait; null cost remains null and revisions replace selection. | B, C–F |
| R round-trip | At least one multi-trait and one multi-criterion request **per provider** through Python/CLI persist, separate-process reopen/export, then actual R/jsonlite import of JSONL/lossless CSV/R audit and crosswalk. Assert every ID/order/attempt/reversal, full maps, 5.15 vs 1.55, selected/modal distinction, CJ orientation, partial/null/unknown fields and once-per-submission usage. | B shared; C–E each; F installed parity |

F compares single versus grouped **synthetic** parity and measures physical calls,
rendered repeated-input bytes, bounded memory/queues and retained provenance.
The 12-trait/13-criterion counts establish packing, not statistical equivalence
or a fixed monetary saving. Later separately authorized live plans must record
exact source, SDK/model, capability/limit evidence, calls and observed usage;
missing model identity or unknown prices limits what can qualify. No historical
performance campaign, universal Level 4 or duplicate local grid is automatic.

Follow the post-PR60 [risk policy](ci-optimization.md) and
[testing guide](testing-guide.md): A contains only non-executable prose, but the
automatic plan selects **Level 3** because `docs/implementation-plan.md` is a
protected acceptance-control path. Honor that stronger plan; do not override it
with the generic Level 0 documentation category. B's shared schema/persistence
work is Level 3. C–E use their highest touched boundary
(shared/schema/authentication/dependency work is Level 3, regardless of adapter
size); F follows its actual integration/release scope. Inspect the automatic
plan before qualification. Applicable strict R executes locally; hosted
functional results are separate. Test metadata/selection changes require their
documented selftests. Live calls and publication require separate authority.

## 9. Decision ledger and handoff gate

| ID | Status / options | Recommended resolution / review consequence |
|---|---|---|
| A0 | Approved by Issue #61 | M4: OpenAI Decisions, Jev, Gemini Decisions in order, shared-input multi-question support required. Together/Ollama live completion remains a lower-priority commitment. M5 handles actual batch/bulk modes. |
| A1 | Approved by Issue #61 | Defer first alpha to an explicit post-M5 go/no-go; preserve the intended all-provider qualification question, including Together/Ollama, until that review. No publication in A–F. |
| D1 | **Proposed; needs high-reasoning review before B:** extend V3 versus explicit new RunSpec; reuse legacy grouping versus new immutable control | New boundary and `RequestGroupingSpecV1` single/auto union above; preserve V1–V3. The 62-character task-derived key rule is part of this review. |
| D2 | **Proposed; needs review before B:** whole-request rollback versus independently valid member salvage | Recognizable-envelope sibling salvage with an atomic full outcome vector and group-wide failures exactly as section 4; aligns with existing rubric lifecycle without treating partial output as valid whole output. |
| D3 | **Proposed; needs review before B:** infer common pair occurrence versus explicit typed presentation membership versus new expanded pair-source schema | Explicit membership preserves current criterion-specific PairTask IDs and avoids accidental repeat/reversal merging. Review public source syntax and validation together; no implicit grouping of historical rows. |
| D4 | **Proposed; needs review before B:** require remote IDs on every endpoint versus versioned nullable-ID response-bound acknowledgment | Accept only documented synchronous response evidence bound to one invocation, keep local/remote identities distinct and retain unresolved windows. No fabricated IDs or claimed remote idempotence. |
| D5 | **Proposed; review before E dependency changes:** one Google 3.x migration versus isolated official-SDK process | Prefer one reviewed migration after the Developer/Vertex compatibility gates; retain original 2.34.0 reconstruction environments. Do not widen constraints now. |

PR A stops at this concrete design for independent review. Accept/revise D1–D4
before PR B implements public schemas, pair presentation or durability; D5 needs
confirmation with chosen SDK evidence before PR E. Approval of this design does
not qualify a model or authorize credentials, live requests, merging or alpha.
