# Native TypeSafe Jev Decisions

Use the `typesafe` extra with locked `typesafe-sdk==0.7.5`. The direct System One
route accepts text-only RunSpec V4 rubric and forced-choice pairwise runs
with exact model `jev-1.14.0`. Validation, planning and preview are offline.
Execution validates the selected `TYPESAFE_API_KEY` after local admission and
before journaling submission intent. The forward uses that validated key.

```yaml
schema_version: 4
task_kind: rubric
provider: typesafe
endpoint: systemone
model: jev-1.14.0
input: responses.jsonl
rubric: rubric.yaml
strict_capabilities: false
execution:
  request_grouping:
    mode: auto
    max_questions_per_request: 12
```

Omitting grouping selects one question per request. Auto groups complete
questions about the same essay or explicit pair presentation. It splits at the
configured maximum and the existing 8 MiB serialized request safeguard.
Question instructions retain the rubric or criterion and selected examples.
Reversals and repetitions have separate presentation identities.

TypeSafe documents 64k tokens for shared state plus all questions, 32k for
shared state plus one question, a 32 MiB body ceiling and no separate question
count cap. The approved Jev-only non-strict policy delegates context checks to
TypeSafe. The plan and frozen evidence record each context fit as unverified.
Strict capabilities refuses before credentials. Long-context and image headers,
explicit cache controls, generation settings, service tiers, explanations, ties
and batch execution are unsupported. Provider caching and cost remain unknown.
[TypeSafe limits](https://docs.typesafe.ai/models#context-length) · [Request rules](https://docs.typesafe.ai/api/errors)

A complete HTTP 400 with `max_tokens_exceeded` (observed in `detail.error_type`)
or HTTP 413 terminates every member of that attempt, including when the full
response arrives before local cancellation. Python run/status/resume results
and CLI output expose `decisions.diagnostics` with a stable code, guidance, and
the affected physical attempt count. Physical provenance binds the rejection
to the original attempt and response hash. Start a new run with smaller input
or a lower grouping maximum.
Missing responses and HTTP 400 bodies with duplicate keys or conflicting codes
stay unresolved. An explicit subset retry
requires a prior failed member from a recognizable accepted response.

Score answers preserve the complete distribution, native expected index,
mapped numeric mean, derived mode and reported confidence. Choice answers
preserve the returned selection and full probabilities in original and
presented order. Response maps are matched by committed question key.
Usage is reported once per physical attempt. Full capture retains permitted
originals or filtered replay, and minimal capture retains hashes and safe metadata.
A conflicting served model raises an integrity error after the physical evidence
and usage are recorded, including when the run is reopened.

Jev uses manifest and preparation version 10, limits and transport commitment
version 3, acknowledgment and evidence receipt version 4, and physical
provenance row and bundle version 3. Normalized exports and question rows keep
schema version 1. Source-bound offline acceptance and live smoke are separate
from the adapter implementation. [Issue 61](https://github.com/shmercer/judgmentllm/issues/61)
