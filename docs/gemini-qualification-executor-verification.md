# Gemini qualification executor: review and verification

This follow-up starts at merged PR #29 (`72520a2a97ec2d84cc86fafe5b1ee8dd785808ff`).
It implements the repository-only executor; implementation and all verification
are offline. No Gemini credentials, authentication checks, discovery, token
counting, provider requests, paid execution, capability promotion or publication
are authorized in this PR. The PR remains open for independent review.

Scope is exactly `gemini` / `models.generate_content`, `v1beta`,
`google-genai==2.34.0`, `gemini-3.1-flash-lite`: rubric text/native and pairwise
text/native, with only `response_json_schema` for native. The original synthetic
inputs, contracts, task IDs and body construction are preserved. Report ingress
now independently reconstructs the dependency hashes as well as physical bodies.
The existing OpenAI harness is `qualification.py`; there is no
`openai_qualification.py` in this baseline.

The executor adapts Anthropic's immutable authorization, FULL-synchronous SQLite
consumption plus exclusive forwarding intent, retained-response recovery,
read-only evidence inspection and terminal assessment continuation checks.
These controls have Gemini-specific authority; historical OpenAI and Anthropic
source, rules and evidence are unchanged. The actual Gemini HTTP boundary still
verifies outgoing bytes and disables SDK/HTTP retries and automatic continuation
and function calling. A candidate context alone cannot submit. All four prepared
requests are verified before credential access. Only the existing shared resolver
selects a key, with GOOGLE_API_KEY before GEMINI_API_KEY and no ADC fallback.

Authorization requires exact plan/source hashes and separate billable and full
synthetic assessment capture consent. Four physical identities, sequential calls,
at most 512 requested output tokens each, no replacements and no reasoning
retention are fixed. Durable consumption precedes forwarding and cannot be reset
by process death or by resetting a SQLite row. Missing, contradictory or uncertain
evidence stops execution. Exact `modelVersion` equality and strict canonical
assessment validity are independent qualification requirements. No alias mapping
has been independently established; an unexpected version fails conservatively.

Official model availability, pricing and the generateContent response contract
were rechecked on 2026-11-10 through Google's public documentation. Paid standard
rates remain [$0.25 input / $1.50 output per million tokens, including thinking](https://ai.google.dev/gemini-api/docs/pricing?hl=en),
for the documented [model](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite).
The API defines [modelVersion](https://ai.google.dev/api/generate-content) as the
version generating the response. Account eligibility and actual serving identity
remain unverified. Reservations are 32,768 estimated input + 2,048 requested output
tokens, **$0.011264**, within a **$0.02** estimated authorization ceiling. Input
allowances and the monetary ceiling are not provider-enforced billing caps.
Thinking is added to candidate output once for estimated actual cost; missing
usage or unresolved cache cost stays unknown. Reported estimates are not invoices.

Acceptance is Level 3 because credential access and physical forwarding authority
are affected. Record the automatic plan from the committed head:

```bash
python3 ci/check.py --profile pr --plan-only --changed-since origin/main
```

Development checks cover qualification, provider integrity, crash/recovery and
historical provider behavior with synthetic credentials and intercepted SDK HTTP.
Process tests independently log forwards, exit before forwarding, after forwarding
and after durable evidence, then reopen twice. They assert unique submission IDs
and a total of at most four forwards. Reports are compared byte-for-byte across
read-only inspections. Missing prerequisites and failed checks are never passed.
The sandbox's asyncio thread notification failure was reproduced with a standalone
0.1-second sleep probe; offline tests use the approved unsandboxed command path,
with fixture network/ADC denial still enforced.

Use the protected-base hosted **CI acceptance** status for the literal final PR
head, including primary coverage, historical comparison, representative Windows
and macOS lanes and all seven strict Linux R/jsonlite gates. The PR verification
record supplies the exact head SHA, run link, automatic plan and passed, failed
and deferred checks. No repeated full local suite or seven-lane grid is needed.
A failed or interrupted development run remains failed/interrupted; it never
establishes qualification. CI thresholds and historical coverage are unchanged.

After independent review and merge, follow the [proposal instructions](gemini.md#unexecuted-live-qualification-proposal)
to generate a fresh immutable plan from the eventual clean merged SHA. Recheck
availability and rates and obtain new exact-digest/source consent before any
future execution. Do not modify or execute the PR #29 proposal. This PR provides
no live authorization and spends no money.
