# Native OpenAI Decisions

PR C implements the official OpenAI Decisions SDK mapping through the existing
Python and installed CLI lifecycle. Its acceptance is synthetic and offline;
this does not establish account access, live model qualification, scoring
accuracy, or equivalence between single and grouped judgments. Jev, Gemini
Decisions and cross-provider qualification remain separate follow-ups to Issue #61.

Use the existing `[openai]` extra and locked OpenAI SDK **3.42.1**. The adapter
requires that exact SDK version before resolving credentials or constructing a
client. Offline validation, planning and preview do not resolve credentials.
An explicit execution uses only `OPENAI_API_KEY`; no credential file, keyring,
ambient authorization header, alternate URL or provider discovery is used.

```yaml
schema_version: 4
task_kind: rubric
provider: openai
endpoint: decisions
model: gpt-6-luna
input: responses.jsonl
rubric: rubric.yaml
capture: full
execution:
  request_grouping:
    mode: auto
    max_questions_per_request: 32
```

`gpt-6-luna` is the currently documented model, not a catalog allowlist. Unknown
IDs remain subject to validation and provider acceptance. Ordinary `validate`,
`plan`, `templates preview`, `run`, `resume`, `runs status`, `results export` and
`results provenance` use the same configuration. `run` can incur provider charges
when an operator explicitly executes it; no such operation was part of PR C tests.

One shared essay can carry independently complete trait questions. Explicit
`pair_presentations` support several independent criterion choices over one
presented pair; repetitions and reversals remain distinct physical inputs. The
[shared lifecycle](decisions-lifecycle.md) defines those sources and stable IDs.
Omitted grouping remains one question per request. Automatic grouping splits
contiguous eligible prefixes at the requested maximum, the endpoint's 400-question
cap and the existing local byte safeguard. It never drops instructions, rubric
levels or selected examples. OpenAI score questions require 2–10 ordered levels;
the provider-neutral rubric schema retains its larger category range.

The endpoint-specific context and total-body ceilings are unestablished. Planning
reports that uncertainty; strict capabilities blocks admission. The local 8-MiB
request/response safeguards remain enforced and do not establish a provider token
window. No token-count API call, truncation or transport fallback occurs.

Caching is OFF only: the wire request sends
`prompt_cache_options: {mode: explicit}` without breakpoints. Omission would enable
implicit caching. Generation settings, explanations, ties, explicit tiers,
semantic caching, native batches and managed bulk-live are rejected before
credential resolution. Generic Responses/Chat options are not sent to Decisions.
See the [official Decisions guide](https://developers.openai.com/api/docs/guides/decisions)
and [SDK reference](https://developers.openai.com/api/reference/python/resources/decisions/methods/create).

Scores preserve every probability, reported confidence and native zero-based
expected index. The mapped numeric expectation and derived modal category remain
separate; no selected score category is invented. CJ requires the actual choice
and preserves both presented and original probability orientations. Usage is
recorded once per physical submission, including unsuccessful assessments; null
counters and costs remain unknown. Shared-input cache counters overlap input
usage and are not added to it. No per-question cost allocation is made.

## Production evidence and privacy-filtered recovery

New production runs use protocol `openai-decisions-production-v1`, executable
manifest/preparation 9, limits/commitment 2, complete-response evidence 2,
acknowledgment 3, evidence receipt 3 and forwarding-fact 2. RunSpec remains V4,
SQLite remains 3, normalized/R results and CLI envelopes remain schema 1.
Historical synthetic records retain their exact protocol and versions.

A complete invocation-bound HTTP response proves receipt even without a remote
response ID; local observation keys are explicitly local. Acceptance does not
make an assessment valid. The adapter validates original bytes before SDK JSON
decoding or privacy filtering. Unknown names and unsafe fields cannot become
retained diagnostics or change the validity of otherwise independent siblings.

Full capture retains permitted exact responses. When filtering is required, an
explicitly versioned replay artifact instead retains permitted answers, safe
validation findings, counts and original answer attribution. Evidence separately
binds original response hash/length and retained hash/length. Recovery verifies
that complete chain and commits the same atomic outcome vector, without another
forwarding or usage fact. An invalid member cannot become valid because its
unsafe field was removed. Internal replay artifacts appear in physical provenance
and are not labeled original raw responses.

Minimal capture retains only hashes and safe findings/attribution, without answer
payloads. Lost uncommitted answers remain unresolved. Neither a hash, process
restart nor a timeout authorizes another assessment request. Explicit failed-task
retries create new physical attempts with original question keys and ancestry.

`export_decisions_provenance` returns report/manifest 2 for production, declaring
question rows 1 and physical rows 2. Physical JSONL and JSON-cell CSV distinguish
original/retained representations, production acceptance, nullable provider IDs,
local keys, validation metadata and one selected usage revision. Synthetic
bundles remain version 1. Readers reject unknown and cross-protocol versions.
The [R contract](r-interoperability.md) preserves all attempts and leading-zero IDs.
