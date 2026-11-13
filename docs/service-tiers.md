# Service tiers and Issue #39

Issue #39 PR1 implements service policies for OpenAI Responses/Chat Completions,
Anthropic Messages, and Gemini Developer GenerateContent. It does not implement
the study package, authorize provider access, or promote any qualification.

## Standard default and explicit provider values

New requests use service policy version 2. An unset `execution.tier` resolves to
explicit standard processing; it does not inherit a premium project default.
The original setting remains unchanged in configuration and normalized result
`tier`. Requested intent, effective wire setting, and actual served tier are
different facts.

| Provider | Unset setting becomes | Explicit supported adapter values | Wire field |
|---|---|---|---|
| OpenAI Responses/Chat | `default` | `default`, `auto`, `flex`, `fast`, `priority` | `service_tier` |
| Gemini Developer | `standard` | `standard`, `flex`, `priority` | top-level `serviceTier` |
| Anthropic Messages | `standard_only` | `standard_only`, `auto` | `service_tier` |

These are implemented mappings, not model/account eligibility claims. Unknown
strings, batch-as-a-tier, and currently unimplemented tier options fail offline.
Unknown model IDs remain admissible; strict nonstandard-tier validation requires
reviewed evidence for that exact model/route/account/tier. Explicit `auto` uses
the provider's allocation policy and is not a promise of standard processing.
Anthropic `auto` is not a forced Priority request. The two Issue #39 Anthropic
models have no documented Flex or Priority eligibility.

References reviewed for this implementation:
[OpenAI request/response tier semantics](https://developers.openai.com/api/reference/resources/responses/methods/create),
[OpenAI Fast aliases and downgrade](https://developers.openai.com/api/docs/guides/fast-mode),
[OpenAI Flex](https://developers.openai.com/api/docs/guides/flex-processing),
[Gemini GenerateContent Flex](https://ai.google.dev/gemini-api/docs/generate-content/flex-inference),
[Gemini Priority](https://ai.google.dev/gemini-api/docs/generate-content/priority-inference),
[Anthropic service tiers](https://platform.claude.com/docs/en/api/service-tiers).
Documentation and synthetic SDK tests establish no live qualification.

## Immutable policy and account scope

The optional typed `service_policy` record has `service_policy_version: 2` and
commits requested/wire tier, declared account scope, and deadline policy in the
manifest, output commitment, and physical transport receipt. Missing policy means
legacy version 1. Historical serialization omits the new record; historical
payloads, hashes, defaults, timeouts, and request identities remain unchanged.
Reconstruction and subset retries inherit their original policy. Changing tier
or account scope cannot reuse a sealed physical identity. Runs are never migrated.
Legacy OpenAI and Gemini requests still reject explicit tiers; Anthropic retains
its historically supported `auto` and `standard_only` values.

RunSpec V3 adds optional `account_scope`, an opaque nonsecret identifier for the
intended provider account/project binding. Omission preserves configuration
serialization. V1/V2 do not accept it. It is descriptive, not an authentication
selector, credential alias, key fingerprint, or proof of account identity. Never
put keys or private identities in this field.

New native selection additionally requires an exact service qualification scope:
policy version, normalized requested service class, declared account scope, and
the features actually evaluated within that scope. Model-level feature evidence
and tier eligibility must not form an untested Cartesian product.
The enclosing reviewed profile retains exact provider, model, endpoint,
SDK/API, mechanism, presentation, and feature restrictions. Standard evidence
cannot authorize Flex, another account, or premium processing. Legacy profiles
without service scopes cannot authorize policy-2 native requests. No production
service scopes are added by PR1.

The dedicated source-bound qualification boundary can compile unqualified
candidates offline. This does not authorize their execution. The later study
executor must bind the declared scope to the separately approved account and
credential source during authorized preflight, and record the independent
evidence needed for review. An account label alone must never become authority.

## Served tier and local deadlines

Companion evidence stores one validated `service_tier` observation per physical
response, including original requested/reported values, reporting source,
latency, and outcome. Normalized assessment JSONL/CSV/R columns are unchanged.

- OpenAI `default` denotes standard. Requested `fast`/`priority` and reported
  `fast`/`priority` denote the same premium service class; retain exact tokens.
  OpenAI's documentation describes model-dependent returned naming.
- Gemini reports its tier through `x-gemini-service-tier`; arbitrary response
  headers are not retained.
- Anthropic `standard_only` corresponds to returned `standard` in
  `usage.service_tier`. Explicit `auto` returning standard or Priority is an
  expected allocation.
- Documented OpenAI Fast and Gemini Priority downgrades to standard are
  `downgraded`, not integrity failures. Preserve the assessment; the response
  does not qualify the requested premium tier and creates no replacement call.
- Missing/unrecognized tier values are `unknown`; unrecognized text is withheld.
  Assessment validity remains independent: validate and commit the current
  observation's assessment results, then stop nonstandard execution for review.
  Preserve invalid/refused outcomes as such; missing tier does not invalidate
  otherwise valid scores. The stop is derived from immutable tier receipts on
  every restart and blocks later submissions before credential readiness. Already
  accepted observations can still be recovered and interpreted; no replacement
  call or automatic gate override is authorized. With concurrency one, no later
  request is invoked. Requests already admitted concurrently may still finish.
- Undocumented changes, including Flex served as standard, are `unexpected`.
  Save evidence and the assessment diagnostic, then stop with an integrity
  failure. Recovery cannot convert the same observation into success.

These mappings and allowed outcomes are the frozen V2 interpretation, including
when revalidating historical V2 observations. Different equivalence or downgrade
rules require a new policy version and separate interpretation; they must not
change the V2 mappings or reclassify sealed evidence.

| Boundary | Standard/premium | Flex |
|---|---:|---:|
| SDK and final HTTP read timeout | 60 s | 930 s |
| HTTP connect/write/pool limits | 10 s each | 10 s each |
| Overall operation deadline | 90 s | 960 s |
| Gemini SDK `HttpOptions.timeout` | 60,000 ms | 930,000 ms |
| Gemini Flex `X-Server-Timeout` hint | existing standard hint | 900 s |

The pinned google-genai 2.34.0 converter puts typed `service_tier` into the
top-level HTTP `serviceTier`, outside `generationConfig`. Its configuration
conversion affects JSON field ordering; exact committed bytes must match actual
SDK serialization. Per-request SDK options and the final transport enforce the
sealed local limits. The server hint is best effort: neither its presence nor
server compliance establishes a local deadline. SDK retries remain disabled.
For policy 2, the final HTTP guard requires the exact pinned SDK hint (`900` for
Flex, `60` for Standard/Priority); missing, changed, or duplicate values fail
before handoff. Legacy header validation remains unchanged.

For policy 2, an HTTP 429 alone is insufficient to establish nonacceptance:
require the documented provider rate/resource-rejection fields. Ambiguous 429,
503, post-handoff timeout/disconnect, and cancellation preserve uncertainty;
they do not authorize a retry. Known nonacceptance and proof of zero billing are
separate facts. Legacy classification remains unchanged.

## PR2 handoff: configurations and bounded coverage

This changes Issue #39's original Standard baseline to an explicitly selected
Flex-first main study. General application defaults remain standard. PR2 will
provide **30 main-study configurations**: six exact models times four rubric
conditions plus one independent-CJ configuration. Four OpenAI/Gemini models use
Flex; two Anthropic models use Standard. All six also have selectable Standard
configurations.

There are **50 total distinct Standard/Flex configurations**: 30 Standard
configurations plus 20 Flex configurations for four models. The main selection
reuses the 20 Flex and ten Anthropic Standard configurations. Do not duplicate
those ten files or invent configuration inheritance. Use `study-config-<sha256>` IDs from canonical JSON containing study version and
content digests, exact provider/endpoint/model, scenario, tier, account scope, and
service-policy version. IDs may not depend on directory location, timestamps,
execution order, or network discovery. Bind physical attempts separately using
the existing immutable request/attempt/source identities.

Preserve the supplied original content and the four direct-scoring conditions;
keep 13 independent forced-choice CJ criteria and a separate reversal audit.
The full per-model matrix remains 26 requests per essay and 13 per pair. The
seven-case pilot is at most 42 calls, not a promise that every case will run.
Its 24-call tier comparison is a feasibility smoke test, not evidence of scoring
equivalence, reliability, or substantive validity.

The pilot must support **budget-enforced incomplete coverage**. Before approval,
seal a deterministic scenario order, explicit per-scenario `max_output_tokens`,
and conservative input plus all billable output/reasoning reservations. A
scenario's output limit must fit its token and monetary reservation; reject an
inconsistent proposal rather than lower the requested limit. Reserve dated
exact-model/tier/account prices, including documented possible served outcomes.
Stop before a call that cannot fit remaining time, calls, tokens, or money. Report
each unrun scenario and reason separately from failed and passed cases; do not
silently substitute another scenario or increase limits to finish coverage.

Proposed ceilings remain 42 calls/$5/eight elapsed hours for the main pilot and
24 calls/$5/three elapsed hours for the tier smoke, concurrency one and no
automatic retries. Combined limits are 66 calls/$10/eleven active hours. The
32,768-input/8,192-output allowances are ceilings, not a universal per-scenario
output setting. Persist suite start/deadlines and cumulative consumption across
restarts. Uncertain submissions keep reservations and stop further calls;
release cost only on established non-billing. Retain accepted terminal failures.

PR2 implements these budgets and study artifacts; PR1 only supplies the provider
foundation and this handoff contract. Each PR requires independent review and
remains unmerged. Neither PR, a configured key, nor offline acceptance authorizes
credential access, live calls, qualification promotion, merging, or publication.
