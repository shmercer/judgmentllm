# Prompt caching

New rubric and comparative-judgment runs default to `execution.cache_policy: off`.
The new-run choices are `off` and `explicit`. Explicit caching is recognized but
fails preflight until [Issue #57](https://github.com/shmercer/judgmentllm/issues/57)
implements semantic breakpoints. It never silently selects another policy.

```yaml
execution:
  cache_policy: "off"
```

| Provider | Meaning of OFF for a new run |
|---|---|
| OpenAI Responses / Chat Completions | Commit and serialize `prompt_cache_options: {"mode":"explicit"}` with no `prompt_cache_breakpoint`. Only the exact `gpt-6-luna` and `gpt-6.1-terra` routes with OpenAI SDK 3.42.1 are currently covered. Unknown models and older routes fail before submission. |
| Anthropic Messages | Omit `cache_control`; no application-requested cache writes. |
| Gemini Developer | Omit explicit `cached_content`. Provider-managed implicit caching cannot be disabled; preflight reports this limitation as a warning, including in strict mode. |
| Vertex | Omit application-managed cache controls; this change establishes no provider-level disable guarantee. |
| Mock | Synthetic, uncached execution. |

OpenAI's wire value `mode: explicit` means **only explicit breakpoints may cache**.
With zero breakpoints it disables prompt-cache reads and writes. This wire value
does not mean JudgmentLLM accepted `cache_policy: explicit`. Prompt text and output
schemas remain unchanged. No cache keys, retention overrides or arbitrary SDK
parameters are accepted. The supported controls are documented in the
[OpenAI caching guide](https://developers.openai.com/api/docs/guides/prompt-caching),
[Responses reference](https://developers.openai.com/api/reference/resources/responses/methods/create)
and [Chat reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
Offline SDK tests establish serialization, not production assessment qualification.
The operator's two exploratory Responses requests in
[Issue #56](https://github.com/shmercer/judgmentllm/issues/56) grant no new qualification.

Caching can charge for writes even when a changing essay prevents reuse. At the
2026-11-14 documentation snapshot, OpenAI describes default writes at 1.25 times
ordinary input pricing for the relevant models, with discounted reads. Use current
model/tier pricing and observed usage; no savings or bill total is inferred.
Gemini may still report cached reads under OFF. The separate
[Issue #39 study](https://github.com/shmercer/judgmentllm/issues/39) keeps its
original evidence and authorizations; this change authorizes no provider calls.

Usage stays once per physical observation in existing evidence and study exports.
OpenAI's reported `input_tokens_details.cache_write_tokens` (Responses) and
`prompt_tokens_details.cache_write_tokens` (Chat) map to `cache_write_tokens` under
usage semantics 2; read counters retain their documented parent overlap. Zero
stays zero, missing stays null, and neither counts nor costs are inferred.
Assessment/result schemas and normalized CSV/R columns remain unchanged.

## Historical runs and old configuration files

Sealed manifests omit `cache_policy_version` under historical revision 1. New
manifests seal revision 2; new OpenAI output and transport commitments also seal
it. Omission remains omitted when historical records are serialized. Historical
`auto` and `off` rebuild their original bodies without the new wire control;
resume and retry inherit that policy. There is no migration or cancellation of
in-flight work. Minimal capture still requires unchanged original sources.

For an **unsealed** old YAML/JSON file, replace `cache_policy: auto` with `"off"`
or omit it. Creating a new run always selects revision 2, even with an older
RunSpec schema. `auto` is rejected with migration guidance. Leave sealed stores,
source files, plans and digests untouched; use their stored policy when reopening.
The CLI's `resume --config` loads the verified run's original revision, including
legacy `auto`; ordinary configuration loading never enables that selection.
