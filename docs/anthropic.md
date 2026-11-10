# Assess synthetic responses with Anthropic Messages

Phase 5B implements ordinal rubric assessment, complete grouped rubric outputs,
and forced-choice pairwise judgment over `POST https://api.anthropic.com/v1/messages`.
It uses the official async `anthropic==2.3.0` SDK and API version `2026-10-21`.
Model acceptance and native serving support require separate independent review;
the production qualification registry contains no qualified Anthropic models.
No Anthropic requests or real credentials are used in development or ordinary CI.

Install a reviewed local wheel with its `[anthropic]` extra and the exact SDK:

```bash
pip install './judgmentllm-0.1.0a3-py3-none-any.whl[anthropic]' 'anthropic==2.3.0'
judgmentllm --help
```

This work does not publish a package. The development lock already includes the
required SDK; no dependency upgrade is needed. A mismatched installed SDK fails
execution preflight. Core-only validation and preview do not import the SDK,
initialize a client, or resolve credentials.

Copy [examples/anthropic](../examples/anthropic/) and replace `YOUR_EXACT_MODEL`
with an exact model ID. The synthetic IDs are character strings, including `001`.
New production execution requires explicit `schema_version: 3`, `provider:
anthropic` and `endpoint: messages`. Auto/native policies also require
`prompt.native_mechanism: anthropic.messages.output_config.format`; text mode
permits omission. Historical configurations retain their original behavior. These first-run commands are offline:

```bash
judgmentllm validate --config examples/anthropic/rubric.yaml --json
judgmentllm plan --config examples/anthropic/rubric.yaml --json
judgmentllm templates preview --config examples/anthropic/rubric.yaml --item-id 001 --trait 01
judgmentllm plan --config examples/anthropic/pairwise.yaml --json
```

`prompt.structured_output: text` renders the full JSON instructions and validates
the original output locally. `native` uses `output_config.format` with the existing
Anthropic projection, but requires reviewed exact model/SDK/API/feature authority.
`auto` uses native only with that authority; otherwise it records text selection
when the strict capability policy permits it. An unqualified exact model can be
used descriptively and in permitted text mode. A schema fixture, installed SDK,
visible model, configured credential, or qualification-harness response grants no
native production authority. There is no mechanism or model fallback on failure.

Text-mode rubric instructions explicitly request one raw JSON object without
Markdown code fences or any surrounding text. Prompting does not guarantee
compliance: malformed output, including fenced JSON, remains an unsuccessful
assessment. Prefer native structured output for stronger format enforcement when
the exact model and feature are supported and qualified.

Independent rubric assessment is the default. Grouped rubric outputs must provide
all requested traits under the exact contract; valid siblings can be preserved
beside explicitly unsuccessful traits. Scores-only and supported explanation
layouts use the existing output contracts. Generated categories have selected
numeric scores; the adapter invents no probability distributions or expected
scores. Pairwise winners must match the original character IDs and preserve the
presented orientation.

## Settings and exact requests

The exact rendered assessment is one user message; the adapter omits `system`
and does not split or rewrite its content. Anthropic requires `max_tokens`.
`generation.max_output_tokens` maps directly to that field. If omitted, an explicit
adapter policy seals **4096** in the physical request while leaving the recorded
user generation setting omitted. There is no automatic token adjustment.
The documented Sonnet 4.6 output bound is 128K; unknown model bounds remain unknown.

API `2026-10-21` removes temperature, top-p and top-k. Explicit temperature/top-p
values, including defaults, fail locally. Arbitrary provider JSON, headers, base
URLs, tools, stop sequences and thinking controls are unavailable. Omitted controls
remain omitted; the provider's model-specific defaults still apply.

`execution.tier` accepts omission, `auto`, or `standard_only`. Account eligibility
remains unknown; unsupported tier names fail. `execution.cache_policy: off` omits
cache controls. `auto` records a conservative uncached request and also omits them.
Explicit caching is limited to the documented `claude-sonnet-4-6` scope and marks
the complete user text with a five-minute ephemeral cache control. It never
rewrites the content, promises a cache hit, or silently adds a cache to other
models. Native grammar compilation caching is provider behavior, separate from
this prompt-cache policy.

Usage preserves uncached input, cache creation, cache reads and output separately.
Five-minute/one-hour creation subdivisions retain their provider field paths and
parent relationship. Anthropic supplies no total under this evidence contract;
`total_tokens` remains null. Missing counters remain null and differ from zero.
Grouped trait rows link to one physical usage observation, avoiding double counts.
Unknown cache accounting prevents a fabricated precise cost report.

## Credentials, capture and recovery

Future execution resolves only `ANTHROPIC_API_KEY` through the shared environment
resolver. Configure it securely in the invoking process; keep it out of YAML,
arguments, source and evidence. The pinned SDK sends this key as
`Authorization: Bearer`. Explicit credentials and fixed client settings prevent
fallback to auth tokens, profiles, federation, credential files, custom headers,
proxies or alternate endpoints. The adapter verifies the outgoing authorization,
API header, endpoint and complete body bytes before physical forwarding.

`auth status` remains offline. The explicit `auth verify --provider anthropic`
and `providers models --provider anthropic` commands each make at most one bounded
non-generative models-list read. They are separate network actions; do not run
them during offline qualification. Listing and authentication acceptance do not
establish endpoint or native capabilities. There is no automatic discovery or
token-counting call.

Executing `judgmentllm run --config ... --store ...` can incur provider charges;
preview alone never submits. Once execution is explicitly authorized, the existing
run/status/resume/results-export services handle Anthropic as well as OpenAI.
SDK and HTTP retries are zero; redirects are rejected. Connections and response
queues follow sealed concurrency. Connect/write/pool timeouts are ten seconds,
read timeout is sixty seconds and the SDK operation deadline is ninety seconds.
The envelope limit is 8 MiB and assessment components are limited to 1 MiB.

Only one nonblank text component with completed `end_turn` and absent/null
`stop_details` can become a successful judgment. Structured
`stop_details.type="refusal"` is a refusal even when `stop_reason="end_turn"`;
other non-null stop details are unsupported. Valid JSON cannot override those
completion classifications. Diagnostics retain only fixed refusal/unsupported
flags, never arbitrary refusal explanations or stop-detail fields.
Thinking/redacted-thinking is separate from assessment explanations.
Refusal, tool use, pause, truncation, context limits, unsupported shapes, duplicate
JSON members, unknown categories/IDs and invalid grouped outputs remain failures.
Components are never concatenated or repaired.

Full capture can retain a recognized assessment component for read-only diagnostic
inspection, even when its JSON is invalid. Minimal capture keeps safe metadata,
length and hashes without assessment text. Diagnostics V2 identify Messages and
its stop reason; historical OpenAI V1 diagnostics keep their original contract.
Provider thinking text/signatures are excluded from retained response derivatives
and explanations; full capture alone grants no reasoning-retention authority.
The adapter does not expose a new reasoning authorization or export interface.

Cancellation and restart preserve durable attempt, submission and observation
identities. Definitely rejected/not-forwarded requests differ from accepted
observations and uncertain acceptance. Anthropic offers no Messages response
retrieval/reconciliation operation used here. A lost response remains unresolved
and resume never blindly resends it. Already received observations drain into
existing durable evidence services. Inspect `runs status`, assessment diagnostics
and immutable result exports before making any separate retry decision.

## Independently reviewable live proposal

`scripts/qualify_anthropic.py plan` compiles four synthetic Messages requests:
rubric text/native and pairwise text/native. Planning requires Git and a clean
committed source checkout, and reads source and installed package metadata only.
It creates no execution authority and calls no Anthropic endpoint. The candidate is exactly `claude-sonnet-4-6`; no substitution is allowed.
[Model documentation](https://platform.claude.com/docs/en/models/sonnet-4-6/overview),
[structured-output documentation](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
and [pricing](https://platform.claude.com/docs/en/about-claude/pricing) were reviewed
on 2026-11-09: 1M context, 128K maximum output, standard input/output $3/$15 per
million. These are documented eligibility claims, not independently verified
serving or account acceptance. Recheck them before any later authorization.

Each complete committed body is measured as UTF-8 bytes, including schema and
request controls. The conservative **estimated** input allowance counts those
bytes at one token per byte, repeats native `output_config` bytes, adds 4096 for
framing/provider instructions, rounds up in units of 1024, and uses an 8192 floor.
The four short bodies fit that floor. The assumptions and byte lengths accompany
each plan case for independent review. No tokenizer or provider-injected overhead
bound has been proved, and no token-counting endpoint is called. This is a
conservative reservation for these short cases, not an exact count or enforced
input limit.

With 8192 estimated input tokens and at most 512 output tokens each, four requests
reserve 32768 estimated input and 2048 output tokens: **$0.129024** at those rates.
The proposed **$0.15 estimated authorization ceiling** includes a small margin.
It is not a guaranteed monetary cap: the local ledger bounds physical calls and
estimated reservations, and `max_tokens` bounds requested output, but Anthropic
has no request input-token cap in this adapter. If review rejects the allowance
assumptions, prepare a new plan with a larger reservation before consent.

The immutable proposal records the exact source SHA, SDK/API/model, four physical
identities, complete bodies and hashes, zero retries, concurrency one, standard
service, cache off, full assessment capture and no reasoning retention. Read-only
`report --evidence-dir DIRECTORY` does not execute it. A later request for live
qualification must explicitly authorize billable calls against the exact plan
SHA-256 and source commit. OpenAI authorization does not apply. Uncertain or
terminal-invalid cases never authorize a replacement submission. The harness
requires the completed durable response to report the exact planned model before
continuing or counting model-specific assessment qualification. Missing or
mismatched identities stop the matrix without replacing the consumed call; no
canonicalization equivalence has been independently verified. Report V2 separates
local assessment validity from model identity verification. Unverified identities
are represented by fixed diagnostic codes and a SHA-256; the original usage
receipt remains available for read-only inspection. This harness rule does not
restrict ordinary unknown-model or alias pass-through. Reports cannot populate
the production registry. This phase stops before live execution.

A reviewer can reproduce preparation from a clean source commit and an external
reviewed pricing profile without credentials:

```bash
python scripts/qualify_anthropic.py plan --provider anthropic --endpoint messages \
  --model claude-sonnet-4-6 --source-commit FULL_SOURCE_SHA \
  --pricing-profile /external/anthropic-reviewed-pricing.json \
  --max-requests 4 --max-output-tokens 512 --max-total-tokens 34816 \
  --max-estimated-usd 0.15 --evidence-dir /external/new-anthropic-plan
python scripts/qualify_anthropic.py report --evidence-dir /external/new-anthropic-plan
```

The resulting plan records the body-derived reservations; a larger request
requires a correspondingly larger estimated budget and a newly reviewed digest.

The four-call plan `4f4a4e4d5d361ac45935ad3aa685ba421ac0c5f33b4f2ecf24b90b2b7daf3aa0`
at source `1f5c25be6ce366e8ad42cafe620fc6b99eb4370c` is consumed. Its fenced rubric-text
result remains invalid; the revised wording has not been live-qualified. A future
retest needs a new source-bound plan and independent billable authorization.
Prefer a single rubric-text case, but the current harness requires the four-case
matrix and has no case-selection option. Leave the harness unchanged here; a
follow-up can review a minimal case-selection change or authorize a fresh bounded
four-case plan. Anthropic rubric text mode remains explicitly unqualified;
Milestone 3 Phase 5C (Gemini Developer) can proceed independently after this
correction is reviewed and merged.
