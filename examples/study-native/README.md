# Original writing study: offline preparation and guarded qualification

This package implements [Issue #39 PR2-B](https://github.com/shmercer/judgmentllm/issues/39#issuecomment-6842469943).
It contains synthetic essays only. It authorizes **zero provider calls**.
The two proposals remain blocked until independently reviewed account binding,
native feature/tier eligibility, token bounds and dated account prices exist.
Passing offline tests never qualifies a production model.

This workflow requires an actual Git checkout and its editable development
environment. The study module, sources, archives, configurations and experiments
are repository tooling, excluded from public wheels and source distributions.
Installing a package artifact provides the ordinary JudgmentLLM library and CLI;
it does not install `python -m judgmentllm.study` or these study assets.

Start from a clean committed checkout with its locked development environment:

```bash
uv sync --locked --all-extras --group dev --python 3.11
uv run --offline --no-sync python -m judgmentllm.study verify
uv run --offline --no-sync python -m judgmentllm.study eligibility
```

`configurations.json` indexes 50 distinct configurations: 30 Standard and 20 Flex.
Its 30 main selections reference the same configurations: OpenAI/Gemini Flex and
Anthropic Standard. General JudgmentLLM defaults remain Standard. Original
text-mode package configurations under `sources/*/configs` are archival inputs;
the runnable native library is under `configs/`.

| Scenario | Requests per essay or pair | Output cap |
|---|---:|---:|
| `grouped_scores` | 1 per essay, all 12 traits | 4,096 |
| `grouped_explanations` | 1 per essay, all 12 traits | 8,192 |
| `independent_scores` | 12 per essay | 2,048 |
| `independent_explanations` | 12 per essay | 4,096 |
| `cj` | 13 per comparison pair | 2,048 |

The full study therefore needs **26 rubric requests per essay per model** and
**13 CJ requests per comparison pair per model**. Reversal is a separately
selected audit. All configurations require RunSpec V3, the exact endpoint's
native mechanism, strict capabilities, `output_order: together`, concurrency one
and no reasoning retention. Optional reasoning, temperature and top-p are omitted.
Provider defaults remain provider defaults; they are not experimental matches.

## Preview without credentials

These commands prepare local synthetic sources and print model-facing text or
the canonical schema and provider projection. They never construct a live client:

```bash
uv run --offline --no-sync python -m judgmentllm.study preview --model gpt-6-luna --scenario grouped_explanations --tier flex
uv run --offline --no-sync python -m judgmentllm.study schema --model claude-haiku-5-5 --scenario grouped_scores --tier standard_only
uv run --offline --no-sync python -m judgmentllm.study preflight --model gemini-3.8-flash --scenario cj --tier flex
uv run --offline --no-sync python -m judgmentllm.study preview --model gemini-3.8-flash --scenario cj --tier flex --trait overall_writing_quality --reverse
uv run --offline --no-sync python -m judgmentllm.study plan --output /tmp/my-study-plan
```

The original rubric demo has one essay; the CJ demo has two essays and 13
criterion-specific pair occurrences. The complete 50-configuration plan contains
390 requests. `requests.jsonl` contains exact prompt, schema, projection and
SDK-payload bytes and hashes; `summary.json` records the selected source.
The plan is preparation evidence, not a submission queue.
`request-plan.json` and `native-previews.jsonl.gz.base64` retain all 390
deterministic request/prompt/schema commitments. Their contents are regenerated
and checked against the actual engine and pinned SDK HTTP in the offline suite.

Canonical/local validation always applies. Projections deliberately report
constraints that remain local, including complete membership and array counts
where the provider subset cannot express them. Offline SDK serialization does
not prove model enforcement. Unsupported projections fail without text fallback.
OpenAI uses Responses; Google uses Developer GenerateContent, never Vertex or
Interactions; Anthropic uses Messages API 2026-10-21. See `documentation.json`
and `eligibility.json` for the distinct documentation/account/SDK/live gates.

| Exact model | Standard | Flex | Premium |
|---|---|---|---|
| `gpt-6-luna` | Candidate, blocked | Candidate, blocked | Fast candidate, blocked |
| `gpt-6.1-terra` | Candidate, blocked | Candidate, blocked | Fast candidate, blocked |
| `gemini-3.5-flash-lite` | Candidate, blocked | Candidate, blocked | Priority candidate, blocked |
| `gemini-3.8-flash` | Candidate, blocked | Candidate, blocked | Priority candidate, blocked |
| `claude-haiku-5-5` | Candidate, blocked | Unsupported | Unsupported |
| `claude-sonnet-5-5` | Candidate, blocked | Unsupported | Unsupported |

Every candidate still lacks independently established account/native/tier
eligibility, billable-token bounds, prices and live observations. The matrix
does not inherit support from a model family or another provider route.

## Verify the original content

```bash
uv run --offline --no-sync python -m judgmentllm.study verify-originals --source /home/sterett/judgmentllm-study-packages-v3
```

Both original inventories and all **312 prompt/schema pairs** were verified
against merged PR2-A before import. `original-manifests.json` retains both original
manifest digests and complete inventories. `original-previews.jsonl.gz.base64`
retains the exact 312 original prompt/schema previews. Standard tests regenerate
every preview from the imported source definitions. `source-verification.json`
records the initial selected source and zero drift. The command above repeats
the full check without writing into the original packages.

The imported inventory now includes all 682 original files and the two manifest
files, including material unused by rendering. Four historical Python programs
are preserved as `.py.txt` byte fixtures; their logical original names and exact
hashes remain in the manifests. No original program is rewritten or run during
ordinary verification. Missing, additional, linked or non-regular entries fail
verification, configuration generation and proposal preparation.
Configuration identities commit this complete inventory. Updating those identities
left all 390 native request bodies, prompts, schemas and projections unchanged.

Rubric descriptors, score-1 OR alternatives, string categories `"0"/`"1"/`"2"`,
role, 15-minute Internet/travel writing task, one-sentence explanation instruction,
short CJ criteria and deep-structure-first holistic guidance remain verbatim.
The short CJ criteria use original score-2 descriptors; the direct-scoring
conditions intentionally show the full rubric instead. CJ schema/wire winners
are `essay_01` and `essay_02`, mapped through each occurrence's presentation
order to the original opaque synthetic essay IDs. Reversal never changes this rule.

The previously uncommitted `expected-previews.jsonl.gz.base64` is preserved
unchanged (SHA-256 `bfe4e4bfa4ba5204345b5c77b006054aa61ffa90382c46dcb4b57632c247fb9b`).
It contains 650 previews from an earlier draft with different synthetic essays;
it is retained provenance, separate from the verified originals and native plan.
No existing work was reset or discarded.

## Two independent proposals

After committing the implementation, generate fresh external directories:

```bash
uv run --offline --no-sync python -m judgmentllm.study proposal --suite native_pilot --output /tmp/my-native-pilot
uv run --offline --no-sync python -m judgmentllm.study proposal --suite tier_feasibility --output /tmp/my-tier-feasibility
```

| Suite | Maximum calls | USD ceiling | Elapsed ceiling |
|---|---:|---:|---:|
| Native pilot | 42 | $5 | 8 hours |
| Tier feasibility | 24 | $5 | 3 hours |
| Combined | 66 | $10 | 11 cumulative active hours |

The pilot seals seven cases per model: grouped scores, grouped explanations,
trait #9 scores, trait #9 explanation, trait #9 CJ, holistic CJ and reversed
holistic CJ. Trait #9 is `rephrasing_of_the_thesis_statement`. The feasibility
suite seals grouped scores and holistic CJ for four OpenAI/Gemini models across
Standard, Flex and Premium (OpenAI Fast; Gemini Priority). Premium is outside
the 50-config library. Ineligible variants make zero calls. This tests service
feasibility, not scoring equivalence or rater validity.

Each proposal is immutable and bound to the clean committed source, exact SDK,
API, model, account, native feature set, tier, case order, request bytes, privacy
policy and deadlines. Missing account or pricing evidence produces `blocked`,
with every missing gate listed. No authoritative account prices or entitlements
are supplied by this package. Approved scenario caps are never lowered to fit.
Every call reserves 32,768 input and 8,192 billable output tokens, including
reasoning under the provider's usage semantics. Those reservations are distinct
from generation caps. Gemini's reasoning counter is separate; OpenAI reasoning
already included in output is never counted twice.

The typed `Eligibility` and `Price` records in `judgmentllm.study.records` define
the future independently reviewed inputs accepted by `--eligibilities` and
`--prices`. Prices must cover all documented possible served outcomes, including
Premium downgrade, in USD. Eligibility must identify the exact feature/control
combination and establish the input and all-billable-output bounds. A private
independently verified `CredentialBinding` binds the approved credential-source
name to the account. Only evidence hashes and allowlisted source names enter
the proposal; keys and private account evidence never enter public artifacts.

`served_outcomes_verified: true` and `billable_served_tiers` require independent
review of every possible billed outcome for the exact account/model/route. Flex
quotes must cover Standard as well as Flex, and Premium quotes must cover its
Standard downgrade. Prices must cover every additional reviewed outcome; the
reservation uses their maximum complete cost. This conservative billing rule
does not change which served tiers pass integrity checks. OpenAI explicitly
allows a returned tier to differ from the request in its
[Responses reference](https://developers.openai.com/api/reference/resources/responses/methods/create).
Exact-account billing outcomes remain unknown until reviewed; documentation for
another Gemini route cannot establish GenerateContent billing eligibility.

## Later approved execution, recovery and exports

**These steps are not authorized by this PR.** An operator must first obtain
independent approval of each sealed proposal digest and the shared $10/66-call
campaign. Supply the exact typed `Authorization` record separately, with its
explicit billable/capture consent. A blocked proposal cannot be authorized.
Account and price proofs remain in a separate private directory, addressed by
their sealed hashes. The existing credential resolver is used only after these
gates pass. Changing credentials invalidates the independently established binding.

The later CLI operations are `execute` and `recover`, with explicit
`--proposal`, `--proposal-sha256`, `--campaign`, `--authorization` and `--proofs`.
`recover` reconciles only previously consumed original physical identities;
it never grants a replacement call. The shared campaign directory must be reused
for both suites and every restart. It retains independent immutable authorizations,
suite plans, forwarding intents, a chained budget journal and a permanent OS lock.
One process owns the entire campaign; SDK and engine automatic retries are zero.
The permanent user-level lock and campaign binding live in `~/.judgmentllm/`.
They bind both suites at one source to the same external campaign state, so a
fresh directory cannot replenish an authorized allocation. Tests isolate this
state with synthetic fixtures. Suite start times are journaled before credentials
are resolved; restart never renews the original deadline.

Independent append-only checkpoints in `~/.judgmentllm/study-campaign-checkpoints/`
commit each budget file before its local publication and before HTTP forwarding.
They contain hashes and budget metadata, never keys or prompts. A missing root,
lost journal/header, interrupted initialization or older restored snapshot blocks
both suites. Deleting local files cannot start a fresh allocation. There is no
automatic reconstruction or reset; damaged state requires separately reviewed
reconciliation. Preserve this independent state alongside the campaign directory.

Each pilot case selects one committed request from its complete study plan.
The other planned requests stay unsubmitted, so an underlying run can remain
partial after the selected case finishes. Case coverage and full-study completion
are reported separately. No command automatically executes the full study matrix.

Reservations are consumed at the final SDK HTTP-byte guard. Uncertain handoff,
ambiguous 429/503, timeout, cancellation, unknown nonstandard served tier or an
integrity mismatch stops later submissions across both suites and restarts.
Terminal invalid assessments remain evidence and are never rescored. A documented
Premium downgrade consumes its call and cannot qualify Premium. Missing tier
evidence preserves independently valid scores. Monetary reservations are retained
conservatively; this executor does not reclaim estimates or move unused suite
money to another suite. Deadlines persist across restart; cumulative active time
reserves each call's complete bounded operation duration, including unresolved calls.
Budget exhaustion reports incomplete coverage and every unrun case.

Use `Ctrl-C` to cancel an active executor through the existing engine's cleanup.
`cancel --proposal DIR --proposal-sha256 DIGEST --campaign DIR` records a durable
stop for an idle/interrupted campaign. Neither promises remote cancellation.
`status` and `report` take the same proposal/digest and optional campaign, recheck
the source and evidence, and expose safe physical usage, tier and assessment
metadata. They never read keys. `export` publishes the existing immutable
normalized JSONL, lossless CSV and R-compatible CSV views for each case. Attempt,
request, task, original/presented order, mapping and missing-value contracts stay
unchanged. Grouped usage stays once per physical receipt, never once per trait.

After execution, status/report automatically follow the proposal's immutable
campaign binding. A different, altered or unavailable campaign is rejected;
missing campaign state cannot be reported as zero consumption.

Reasoning/temperature experimental suites are PR2-C. Production qualification
or promotion requires separately reviewed exact-source live evidence and another
unmerged PR. This workflow never writes a production qualification registry.
