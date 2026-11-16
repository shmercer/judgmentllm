# Native Decisions lifecycle, PR B

PR B implements provider-neutral preparation and durable named-member results
with the **offline mock adapter only**. The protocol
`judgmentllm-decisions-offline-v1` is a synthetic test protocol. It is not an
OpenAI, TypeSafe or Gemini wire format, support claim or live qualification.
PRs C–E own those adapters and their official SDK enforcement. Dependencies are
unchanged. M5 batch/bulk-live, caching, cross-provider qualification and alpha
publication remain outside this change.

Use `schema_version: 4`, `provider: mock`, `endpoint: synthetic` and any nonempty
model ID. `load_run_spec`, `validate_inputs`, `estimate_run`, `preview_prompt`,
`run_live` and `resume_run` dispatch V4 deliberately. V1–V3 retain their existing
configuration, rendering, IDs and serialized outputs. The older structural
`plan_run`/`iter_requests` APIs require V1–V3; V4 uses indexed `prepare_run` or
`estimate_run`, whose independently versioned plan has no fictitious trait groups.

```yaml
schema_version: 4
task_kind: rubric
provider: mock
endpoint: synthetic
model: offline-decisions
input: responses.jsonl
rubric: rubric.yaml
capture: minimal
execution:
  request_grouping:
    grouping_version: 1
    mode: auto
    max_questions_per_request: 32
```

Omitted `request_grouping` resolves to single mode, maximum 1. A supplied object
must include `mode`; `auto` requires a positive strict integer maximum. `auto/1`
remains a distinct policy. Unknown fields, boolean/string/float maxima and mixed
legacy grouping/output authorities are rejected. The grouping models are frozen
and current nested data is revalidated before preparation/reconstruction. There
is no universal ten-level cap. Explanations, ties, nonempty generation controls,
cache policies other than `off`, and bulk/batch modes are not V4 authorities in B.

Shared essay/presented-pair text is separate from complete independently rendered
question instructions. `prompt.file` is one shared template rendered separately
for each trait or criterion. Its narrow sandbox context includes `rubric`,
`trait`, `criterion`, `exemplars`, `traits`, `exemplar_items` and
`output_instructions`; essay text is not a question-template variable. All
protected content must survive unchanged. With exemplars, explicitly provide
`prompt.exemplar_ids_by_trait` for every trait, including `[]` where intended.
Each ordered list selects known IDs; another trait's annotations cannot silently
become evidence for this question.

Only one source occurrence or one explicit prepared pair presentation may supply
a group. Text equality never groups different items. All questions share the
run's route, model, physical controls, capture and cache policy. Synthetic tier
requests fail clearly. Future adapters must bind their credential-source and
service policy at the same boundary. Split contiguous prefixes at the user's
maximum and independently known endpoint question/body/context limits. Unknown
limits stay null; a known context ceiling without a trustworthy local counter
blocks preparation. B's UTF-8 fixture counter establishes synthetic limits only.
An oversized singleton fails; sealed attempts are never repacked or truncated.

## Explicit pair presentations

V4 pairwise input selects exactly one of `pairs` (historical prepared criterion
rows, always singleton) or `pair_presentations` (CSV/JSONL source occurrences).
`PairPresentationInputV1` has `source_presentation_id`, `pair_id`, original left
and right item IDs, ordered `members` with source `pair_uid` and `trait_id`, and
`schedule` containing repetitions, `include_reversals` and optional seed. The
source owns scheduling once; a supplied run seed must agree. JSON-valued CSV
`members`/`schedule` cells preserve the same structure.

`prepare_pair_presentations` and durable preparation project each common schedule
into criterion tasks using the existing pair/task formulas. Every crosswalk
retains both `source_presentation_id` and the member's `source_pair_uid`.
SCHED1 is in [the accepted design](native-decisions-design.md#sched1-worked-synthetic-presentation-case)
and the shared fixtures under `tests/fixtures/decisions`. Three criteria, two
repetitions and reversals yield four presentations, twelve logical tasks, and
4/8/12 physical requests for auto32/auto2/single. Seeds 41 and 42 preserve IDs
while changing the committed orientation. Independent historical criterion rows
are never merged or rescheduled.

## Storage and acceptance

New stores use SQLite schema 3. Schema 1 and 2 statements/checksums remain exact,
with explicit 1/2/3 initialization and verification dispatch. Existing stores
are reopened without migration, backfill or hash reinterpretation. V4 requires
a fresh schema-3 store and is rejected in old stores before creating a run.

Schema 2's `input_records` kind constraint and criterion-specific `PairStructure`
cannot represent an unscheduled shared source with independently unique members.
Schema 3 therefore adds `presentation_inputs` and `presentation_source_members`,
their source/membership foreign keys, unique identities/order/criteria, ordered
index, immutable/no-replace and frozen-source guards. It also adds a nullable
`runs.decisions_version` fixed in the run's original INSERT. UPDATE, DELETE and
REPLACE cannot retrofit this marker. Seal/reopen bind marker 4 to RunSpec V4 and
executable manifest 8. Absent marker retains historical behavior.
Native sealing also requires the deeply revalidated V4 specification: its exact
artifact bytes/hash, configuration/assessment digests, controls and run identity
must match the manifest. Reopening a published but unsealed run repeats this
binding; minimal capture supplies the verified specification without retaining
its text. Legacy seal callers retain their existing interface and authority.

The other required SQL difference is narrow: a marked run may commit a
`refused`/`provider_refusal` member from a `response` observation. Every existing
accepted-submission, observation, attempt, identity and membership guard remains.
No other response/status relaxation is introduced. This expresses a named
refusal beside a valid sibling without fabricating a whole-envelope refusal.

Manifest 8 freezes the source index, grouping and initial transport digest/count.
Each physical attempt has a version-1 Decisions commitment and an ordered task /
question crosswalk: stable opaque question key, exact input/question/body hashes
and lengths, complete scale or pair mapping, source layers and retry parent.
Full capture retains approved bodies; minimal retains safe commitments and needs
verified original sources. Historical manifests and receipts keep their versions.

The executor journals submitting intent before one guarded forwarding entry.
The synthetic receiver observes entries **before** acceptance deduplication;
one acceptance record alone cannot prove one call. Retry/redirect/continuation
scripts are blocked by the single-use guard. A first complete response survives
a later guard defect. C–E must separately enforce actual SDK retry, redirect and
continuation behavior; no such vendor behavior is claimed here.

Acknowledgment v2 permits a null real remote ID only with complete, invocation-
bound response evidence (status, exact body hash/length and physical identity).
A local observation key is a separate identity. Missing acknowledgments or lost/
partial responses are held; recovery never blindly resubmits. Known forwarding
without acceptance has its own immutable invocation receipt, allowing later
authoritative reconciliation without rewriting an uncertain fact.
A missing synthetic acceptance row stays unresolved, including death after
receiver entry and before acceptance is recorded. Only an explicit negative
service record matching correlation and payload can establish nonacceptance.

Typed adapter parsing rejects duplicate raw JSON keys and preserves named answer
order. Shared interpretation salvages siblings only from a recognizable bound
envelope; missing, duplicate, wrong-type or invalid-distribution members fail
individually. Named refusals remain refused. Unexpected names create counts,
never tasks. Conflicting identities stop as integrity errors; malformed JSON
cannot create successful members. Ordered-array violations invalidate the
envelope. Map answer order may vary. No winner, selected score or probability
repair is inferred.

The existing `commit_results` transaction commits the entire ordered vector and
states or none. Its safe named-answer attribution artifact is registered in that
same transaction. Fresh-process death/replay tests cover acknowledgment, receipt,
evidence, member validation, vector publication and mid-result insertion.
Explicit failed-subset retry preserves original request ancestry and question
keys, creates a new physical identity, and resumes the same operation ID once.
Unresolved prior attempts cannot be replaced by a retry.

## Inspection and companion exports

Normalized results, canonical/R result writers and CLI envelope **remain schema
1**. New status/summary/validation detail appears under optional typed
`decisions` only for verified V4 runs. Historical outputs omit it entirely,
including nested run listings; old usage/artifact nulls and golden bytes remain.
The independently versioned V4 plan does not alter the old plan serializer.

`decisions.known_invocations`, `invocation_unknown` and `observed_responses` are
distinct counts. Zero known invocations with one unknown invocation is not a
claim of zero calls. Unreported counters/cost remain null. A reported zero is
numeric zero. Partial or unknown totals stay null rather than summing an
incomplete population; usage/cost evidence states distinguish those cases.
No-invocation usage is unavailable, not measured zero. Currency conflicts do not
produce a monetary total. Highest authoritative usage revision is selected once
per submission; revisions and per-question results are never added together.
The legacy top-level `usage` placeholders retain their historical serialization;
V4 physical accounting is the independently versioned `decisions` detail.

```bash
judgmentllm validate --config run.yaml --json
judgmentllm plan --config run.yaml --json
judgmentllm run --config run.yaml --store ./runtime --json
judgmentllm results export --run RUN_ID --store ./runtime --format jsonl --json
judgmentllm results provenance --run RUN_ID --store ./runtime --json
```

Python exposes `export_decisions_provenance(run_id, store=...)`. Its narrow bundle
contains questions JSONL/CSV and physical-submission JSONL/CSV; existing result
exports remain independently available at the recorded result high-water mark.
Each CSV cell is a JSON value (`json_cells_v1`): decode with `jsonlite` after a
character-only CSV read. This distinguishes null, an empty string and the literal
string `"null"`, preserves nested maps/vectors and leading-zero IDs losslessly.
Question rows retain all attempts and immutable normalized-result hashes, both
source layers, exact question/answer attribution and ordered mappings. Physical
rows carry selected usage once, nullable real remote IDs and separate local keys.
Costs are not allocated to questions. Mapped mean 5.15, native expected index
1.55, provider-selected category, modal category and confidence remain distinct.

The bundle manifest commits source/result/usage/event high-water marks and four
complete artifacts. All files and the manifest are registered before the
discoverable immutable `decisions-provenance/BUNDLE_ID.json` reference is
published. Repeated export of the same snapshot converges, including process
death before/after reference publication. No bundle is stored as a legacy
`exports` result row. Status exposes the latest published manifest reference;
inspection reads safe metadata, while export independently verifies all bytes.

Preparation, membership, work queues and export joins use bounded pages/groups.
Resource tests cover 100001 indexed presentation sources/members and a separate
6669-result grouped queue/export workload under the existing 180-second fixture
deadline and 256-MiB cap. These are boundary tests, not a 100001-assessment
completion or vendor performance qualification. Strict local R/jsonlite consumes
actual full/minimal Python exports, failed/refused members, all attempts,
probabilities/mappings, CJ orientation, unknown evidence and physical totals.
