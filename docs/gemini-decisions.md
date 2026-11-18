# Gemini Developer Decisions

Install `judgmentllm[google]` from the reviewed source. The adapter uses
`google-genai[httpx]==3.5.0` and `client.aio.decisions.with_raw_response.create`
on Developer `POST /v1beta/decisions`. Its model is `gemini-decision-1`.
Developer `models.generate_content` and Vertex keep their own controls and routes.

```yaml
schema_version: 4
task_kind: rubric
provider: gemini
endpoint: decisions
model: gemini-decision-1
input: responses.jsonl
rubric: rubric.yaml
capture: full
execution:
  request_grouping:
    mode: auto
    max_questions_per_request: 32
```

Use the ordinary Python and CLI validate, plan, preview, run, status, resume,
export and provenance commands. Gemini Decisions accepts one to 1,000 named
questions, keys matching `[A-Za-z0-9_-]{1,64}` and 2–255 rubric levels. Google
allows 2–255 Choice options. JudgmentLLM sends two choices for comparative
judgments. This adapter admits text only and rejects explanations, ties,
generation settings, cache controls, Flex, Priority and native batch before
resolving credentials. Run execution resolves the Developer environment key
only after local preparation.

The local cap is 256 KiB on the exact serialized body, including shared input
and every question. Packing also honors the user's maximum and the
1,000-question ceiling. Plans report measured body bytes, the published
262,144-token context limit and `context_fit=unverified` separately. Strict
admission remains unavailable until a reliable Decisions context counter can
establish fit. A larger singleton fails local preflight. Google can reject an
admitted body for context size.
[Google request and limits guide](https://aistudio.google.com/docs/decisions-api#limits)

## Scores and choices

The sealed mass tolerance defaults to `1e-6`. An explicit value from zero
through `1e-6` governs mass. Gemini score confidence must be within `1e-6` of
the maximum probability. The reported expected index must be on the scale and
within `1e-6 × (levels − 1)` of the distribution-derived index. Export keeps the
reported index, confidence and complete distribution. The mapped ordinal mean
uses the caller's numeric level values.

Google sorts Choice option keys in UTF-8 byte order and chooses the first key
on an exact provider probability tie. JudgmentLLM uses `essay_01` and
`essay_02` for the first and second presented essays. JSON field order has no
effect. The selected probability, maximum and confidence must all lie within
`1e-6` of one another. Either supplied selection is allowed on equal wire
probabilities because wire rounding can hide a provider-side difference.
Admitted near ties also preserve the supplied selection. The adapter requires
a selection and maps both probabilities back to the original pair order.
These tolerances are local validation rules.
[Google Choice guide](https://aistudio.google.com/docs/decisions-api#choice)

## Recovery and SDK compatibility

Valid named siblings survive missing or invalid answers, including numbers
outside the supported range. Usage is counted once per physical attempt when
reported. An acknowledged HTTP 200 safety response with empty answers produces
refusals. Select an acknowledged refusal with `resume --retry-task` and a fresh
`--retry-id` to create a new attempt for that question. A conclusive HTTP 400
safety or validation response produces a terminal rejection receipt. When a complete 400 identifies
a context limit, run, status and resume advise starting a new run with fewer
grouped questions or smaller shared input. Receipts preserve a typed reason,
body hash and byte count. Conflicting HTTP and error metadata leave the
submission unresolved. Before hashing, credential screening decodes JSON string
escapes independently of the enclosing response, including malformed UTF-8.
After four passes, further Unicode escapes or escape sequences that could still
hide a credential leave the submission unresolved. Literal backslash runs can
pass screening. Retries require explicit selection.
[Google response reference](https://aistudio.google.com/api-reference/decisions)

The same pinned 3.5.0 extra supports existing Developer and Vertex generative
behavior under a separately sealed offline parity revision. The existing
2.34.0 live records keep their original SDK, route, model, mechanism and
presentation. Native forwarding requires reviewed authority for its actual
SDK version. Use the current reader to inspect and export retained historical
results. Use the original checkout and its pinned environment to reconstruct or
forward original generative requests. Gemini Decisions has its own live review.
