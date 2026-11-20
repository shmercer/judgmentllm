# Prompt caching

New rubric and comparative-judgment runs default to `execution.cache_policy: off`.
The explicit option has an offline candidate for semantic breakpoints.
[Issue #57](https://github.com/shmercer/judgmentllm/issues/57)

```yaml
execution:
  cache_policy: "off"
```

| Provider | Meaning of OFF for a new run |
|---|---|
| OpenAI Responses / Chat Completions | Commit and serialize `prompt_cache_options: {"mode":"explicit"}` with no `prompt_cache_breakpoint`. Only the exact `gpt-6-luna` and `gpt-6.1-terra` routes with OpenAI SDK 3.42.1 are currently covered. Unsupported models/endpoints and missing or different installed SDKs fail shared, credential-free preflight before a run identity is created. |
| Anthropic Messages | Omit `cache_control`; no application-requested cache writes. |
| Gemini Developer | Omit explicit `cached_content`. Provider-managed implicit caching cannot be disabled; preflight reports this limitation as a warning, including in strict mode. |
| Vertex | Omit application-managed cache controls; this change establishes no provider-level disable guarantee. |
| Mock | Synthetic, uncached execution. |

OpenAI's wire value `mode: explicit` means **only explicit breakpoints may cache**.
With zero breakpoints it disables prompt-cache reads and writes while preserving
the prompt text and output schema. The supported controls are documented in the
[OpenAI caching guide](https://developers.openai.com/api/docs/guides/prompt-caching),
[Responses reference](https://developers.openai.com/api/reference/resources/responses/methods/create)
and [Chat reference](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
Production qualification requires separate evidence. The operator's two
exploratory Responses requests document the earlier OFF work.
[Issue #56](https://github.com/shmercer/judgmentllm/issues/56)

Cache reads use the cached-input rate. New writes are charged even when there
is no later hit. The 2026-11-14 OpenAI documentation listed writes at 1.25 times
ordinary input for this default retention scope. The remaining input tokens
use the ordinary input rate. Apply only qualified model and tier rates to
complete, consistent provider counters. [OpenAI prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
Gemini may still report cached reads under OFF. The separate
[Issue #39 study](https://github.com/shmercer/judgmentllm/issues/39) keeps its
original evidence and authorizations; this change authorizes no provider calls.

Usage stays once per physical observation in existing evidence and study exports.
OpenAI's reported `input_tokens_details.cache_write_tokens` (Responses) and
`prompt_tokens_details.cache_write_tokens` (Chat) map to `cache_write_tokens` under
usage semantics 2; read counters retain their documented parent overlap. Zero
stays zero, missing stays null, and neither counts nor costs are inferred.
Assessment/result schemas and normalized CSV/R columns remain unchanged.

## Explicit caching offline candidate

Use RunSpec V3 with exact OpenAI `gpt-6-luna`, Responses, installed
`openai==3.42.1`, ordinary live mode, Standard service and native structured
output. `validate`, `plan` and `templates preview` can compile this scope.
Provider submission is blocked pending separately authorized qualification.
Managed bulk-live (PR #69), native batch, Decisions, other tiers and other
providers are outside this scope.

```yaml
schema_version: 3
provider: openai
endpoint: responses
model: gpt-6-luna
execution:
  mode: live
  cache_policy: explicit
prompt:
  structured_output: native
  native_mechanism: openai.responses.text.format
  cache_breakpoints:
    - after: assessment
    - after: exemplar
      exemplar_id: reference-example
```

Choose one to four ordered boundaries. `assessment` cuts after the protected
rubric or comparison criterion. `exemplar` cuts after a named configured
exemplar, including its annotations and metadata. `output_instructions` cuts
after the protected native output instructions. The final rendered order governs
validity. Inspect static rubric, template and exemplar text, IDs, annotations and
metadata for approved reusable content before opting in. Keep private student
material out of those static source fields. Student inputs are structurally
protected and appear after the final marker. A branch-dependent hook, advanced
template or misplaced marker fails locally. The matching native schema, role
and concatenated UTF-8 prompt are preserved.

The candidate omits retention overrides and uses the documented 30-minute default
minimum. The provider controls actual retention and eligibility. Confirm provider
retention, account scope and permitted capture before any future live
qualification. [OpenAI prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)

### Scoped transport report and offline verification

The owner reported a standalone provider transport smoke on 2026-11-20 for
requested `gpt-6-luna` / Responses / Standard, in sealed row order 1 → 2 → 8.
All three attempts completed with HTTP200 and no stop. The reported cache counts
were:

| Row | Control | Cache writes | Cache reads |
|---|---|---|---|
| 1 | Explicit, cold | 1513 | 0 |
| 2 | Explicit, warm | 0 | 1513 |
| 8 | OFF | 0 | 0 |

The report describes two byte-identical marked prefixes for cold/warm, with only
the synthetic essay suffix changing. It reports that OFF uses the warm
concatenated text, schema and settings in one unmarked block. The owner's wire
bytes have not been independently verified. This standalone transport report
does not establish JudgmentLLM SDK integration. Exact returned model, served
tier, other usage fields and actual billing are not established by this summary.
The **1.7350344 USD reserve is not actual spend or savings**.

Separate repository tests use generic, newly generated synthetic inputs, the
merged compiler and SDK 3.42.1 with `MockTransport`. They check offline SDK byte
parity against their own compiled sealed payloads only, for a cold/warm/OFF
differential, then exercise response extraction and durable receipt/repository
replay. They do not claim byte parity with the owner's rows or successful
production submission.
Only the three cache-count pairs mirror the report; other fake usage is invented.
Production eligibility is never overridden in this test: explicit requests are
compiled as offline candidates, native OFF uses the existing qualification
candidate context, and the independent production denial remains installed.

The pinned [request type](https://github.com/openai/openai-python/blob/v3.42.1/src/openai/types/responses/response_create_params.py)
documents that `prompt_cache_options.mode="explicit"` disables the implicit
breakpoint; zero explicit markers means no prompt caching. Both compiler paths
retain this control. The pinned [usage types](https://github.com/openai/openai-python/blob/v3.42.1/src/openai/types/responses/response_usage.py)
name integer `input_tokens_details.cached_tokens`,
`input_tokens_details.cache_write_tokens` and
`output_tokens_details.reasoning_tokens`. JudgmentLLM preserves unknown counts
as null and requires strict nonnegative integers when present. Cache details
are included in input; reasoning is included in output. Neither is added again.
Repeated receipt replay adds no usage fact, and grouped trait membership does
not multiply physical usage. Invalid usage does not invalidate otherwise valid
assessment text or authorize another send.

Production explicit caching remains unqualified and denied; OFF remains the
default. A later enablement decision needs separately reviewed exact returned
model/served tier, native schema/presentation and requested controls, SDK
integration and usage evidence, plus scoped authority and negative gate tests.
The existing qualification context rejects explicit revision 3. This report
does not authorize changing that boundary, promoting a registry or making a call.

### Advisory prefix estimates

The optional `openai-cache` extra pins `tiktoken==0.14.0`. Provision the
official `o200k_base` rank file separately. Put the verified file in the
directory named by `TIKTOKEN_CACHE_DIR` under the filename
`fb374d419588a4632f3f557e76b4b70aebbca790`. Its SHA-256 is
`446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d`.
JudgmentLLM reads this file locally and leaves its lifecycle to the operator.
[Official encoding definition](https://github.com/openai/tiktoken/blob/0.14.0/tiktoken_ext/openai_public.py)
[Official rank file](https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken)

Each cumulative marker reports a visible-text estimate over joined text and a
sum of per-block counts. Counts can differ at boundaries, including with Unicode.
They omit provider framing and use a proxy encoding. The provider documents a
1,024-token minimum. Estimator version 2 reports `short` when both estimates are
below 1,024 and `borderline` when they span the minimum or the smaller estimate
is 1,024 or 1,025. It reports `disagreement` when the estimates differ and
`unknown` when a count is unavailable.
These warnings leave the wire, marker placement, exit status and assessment
validity independent of the estimate, including in strict mode. Provider
eligibility, acceptance and cache activity require provider evidence.

Preview the candidate with `templates preview --config … --json` and omit
item, trait and pair selectors. The report contains ordinal coordinates, anchor
kinds, prefix sizes and hashes, estimator provenance and warning codes. Both
preview formats include `pricing_guidance` about cached reads, chargeable
writes and when cost remains unknown. The report omits the prompt, exemplar
IDs, student IDs and student text. `validate` and `plan` expose optional
marker diagnostics. The first 100 requests are shown with the total and
truncation flag.

Companion diagnostics are best effort at
`cache-advisories/<transport-commitment-sha256>.json` inside the run directory.
Publication follows the required seal or subset transaction. Missing,
unreadable, corrupt or unwritable companions have no effect on execution or
recovery. A changed physical subset gets a fresh receipt and its own structural
checks. Same-attempt recovery verifies the original sealed bytes and holds
unresolved sends for reconciliation.

Check provider-reported reads and writes per physical observation. Missing
counters remain unknown and reported zero remains zero. A native assessment
can be valid with zero or missing cache activity. A provider rejection follows
the existing failure path. Prefix warnings and cache outcomes never authorize
a replacement send. Cost remains unknown without qualified exact-scope rates
and complete, consistent usage, including read plus write at most input.

## Historical runs and old configuration files

Sealed manifests omit `cache_policy_version` under historical revision 1. New
OFF manifests and their OpenAI commitments seal revision 2. The explicit
candidate seals revision 3. Omission remains omitted when historical records are serialized. Historical
`auto` and `off` rebuild their original bodies without the new wire control;
resume and retry inherit that policy. There is no migration or cancellation of
in-flight work. Minimal capture still requires unchanged original sources.

For an **unsealed** old YAML/JSON file, replace `cache_policy: auto` with `"off"`
or omit it. New OFF runs select revision 2, including with older RunSpec schemas.
`auto` is rejected with migration guidance. Leave sealed stores,
source files, plans and digests untouched; use their stored policy when reopening.
The CLI's `resume --config` loads the verified run's original revision, including
legacy `auto`; ordinary configuration loading never enables that selection.
