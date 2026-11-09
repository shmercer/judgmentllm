# Data contracts and scoring semantics

**RunSpec versions 1/2 remain historical; explicit version 3 supports independently dispatched synthetic and production transport boundaries. Document and normalized-result schemas remain version 1.** Assessment models live in
`judgmentllm.models`, run configuration in `judgmentllm.config`, and normalized
outputs in `judgmentllm.results`. Public lifecycle services execute synthetic
assessments through the mock provider; explicit V3 also selects the Phase5A OpenAI production boundary. Other provider integrations remain later milestones. The internal
[persistence foundation](persistence-contracts.md) is implemented separately.
Subphase 6 scale tooling uses these same contracts without adding public result
fields or changing schema version 1. Benchmark reports are development evidence,
separate from canonical assessment exports. Milestone 3 Phase 1 adds a separate
standalone assessment-output contract boundary described below; document and
normalized-result schemas remain version 1.

## Explicit RunSpec v2

`RunSpec` remains the public version-1 model. `RunSpecV2` opts into the new
renderer and output contracts; `load_run_spec()` dispatches strictly on an integer
root `schema_version`, defaulting omission to 1. Version 2 is never inferred from
a preset. V1 rejects v2-only prompt fields. The shared `VersionedModel` still
accepts only 1 for rubric, exemplar, criteria, response and result documents.

V2 retains existing input, grouping, explanation, generation, capture and template
controls. `prompt.output_order` defaults to `together`; `scores_first` and
`explanations_first` require rubric explanations. Pairwise permits only
`together`. `prompt.structured_output` defaults to `auto` and accepts `text` or
`native`. The mock selects text for auto and records native unavailability;
explicit native fails preflight. Strict capability rules remain enforced.
Unknown settings, arbitrary provider JSON/schema and reserved reasoning controls
are rejected. Existing generation controls retain sparse serialization.

Each v2 request has an exact `contract_version=2` contract for selected ordered
traits/categories or the presented pair and tie policy. Its immutable commitment
includes renderer version, requested layout/policy, resolved schema presentation,
ordered schema bytes/digest, requested property orders, prompt/payload digests
and source/dependency identities. Resume verifies these original commitments.
V2 wire layouts normalize to existing version-1 results and exports.

## Validation and serialization

Models reject unknown fields, implicit scalar conversions, booleans in numeric
fields, and nonfinite numbers, including numbers nested in metadata. Metadata
is an opaque JSON object. IDs are nonempty strings with no surrounding whitespace
or ASCII control characters; IDs and text are never silently trimmed.

Referenced input documents, response/exemplar/pair records, and normalized results
include `schema_version: 1`. Omission defaults to version 1; an explicitly supplied
version must be the integer `1`, excluding `true`, `1.0`, and `"1"`. Nested models
such as traits and levels belong to their containing document's schema version.

Assessment models remain mutable for Python use; generation settings and capability evidence are frozen. Library writers reconstruct a fresh deep snapshot immediately before serializing each record, including nested lists/models and unknown fields injected by `model_copy(update=...)`. Invalid current data or data that validators would repair is rejected; no cached instance validation is trusted. This invariant covers normalized JSONL, lossless CSV, derived R CSV, and the generic `write_jsonl` typed-record writer. Direct Pydantic serializer calls remain low-level operations and do not provide this export safeguard.

Use `model_dump(mode="json")` for JSON-compatible values and `model_dump_json()`
for JSON text. Default fields and nulls are included, except that absent generation
configuration and its unset members are omitted as described below. Lists retain source order.
`model_validate()` and `model_validate_json()` validate loaded records, including
score consistency. Original label scalars are preserved in `Level.source_label`;
`Level.label` is the canonical string used by scores and probability records.

## Assessment inputs

| Model | Fields and rules |
|---|---|
| `ResponseItem` | Required `item_id`, `text`; optional `metadata={}`. The model can represent empty text. Readers reject it unless `InputSpec.allow_empty_text` or the reader's keyword override permits it. |
| `Level` | Required `label`; optional `description=""`, `numeric_value=null`, `source_label` inferred from the supplied label. Labels accept strings or finite numeric scalars, excluding booleans, objects, and blank strings. An explicit source label must represent the same canonical label. |
| `Trait` | Required `trait_id`, `name`, `description`, and ordered `levels` of length at least two. Category labels must be unique after canonicalization. |
| `Rubric` | Required `rubric_id`, `name`, and nonempty `traits`; optional shared `description=""`. Trait IDs must be unique. Each trait has its own scale and coding. |
| `Exemplar` | Required `exemplar_id`, `text`; optional `scores={}` (trait ID to category label), `rationales={}` (trait ID to text), `metadata={}`. Scores and rationales are independently optional. |
| `PairwiseCriterion` | Required `trait_id`, `name`, `description`. Comparison criteria have no ordinal levels. |

`validate_exemplars(exemplars, rubric=None, criteria=())` rejects duplicate
exemplar/criterion IDs, unknown annotated traits, and unknown rubric categories.
Pairwise exemplars can contain text and criterion-specific rationales; ordinal
score annotations require a rubric and are rejected for pairwise criteria.
Selected exemplar order remains unchanged. There is no schema limit on stored
traits or exemplars and no automatic exemplar selection, omission, or truncation.

### Ordinal numeric coding

`Trait.coding_kind` identifies the rule as `explicit`, `numeric`, or `indices`.
`Trait.numeric_values` returns codes in category order, and
`Trait.resolved_levels` returns ordered `{label, numeric_value}` models.

1. If explicit `numeric_value` is used, supply it for every level. These values
   override default label coding and must be finite, distinct, and strictly
   increasing in rubric order.
2. Otherwise, wholly numeric labels use their numeric values. Nonfinite,
   duplicate numeric values (including `"1"` and `"1.0"`), and decreasing values
   are errors; they do not silently become index codes.
3. Textual and mixed numeric/text scales use `0, 1, ..., K-1`. A mixed scale warns
   if a numeric label differs from its assigned index; preflight also reports
   that warning structurally. For example, `[5, "Advanced"]` receives `[0, 1]`.

Explicit mappings may be nonuniform, for example `[1, 2, 7]`. Category labels
remain categorical; this contract introduces no continuous scoring scale.

### Pair occurrences

`PairTask` requires `pair_id`, `pair_uid`, `left_item_id`, `right_item_id`, and
`trait_id`. The two item IDs must differ. Text is referenced through response
IDs; inline pair texts are not part of version 1.

`original_order` defaults to `[left_item_id, right_item_id]` and must equal that
order if supplied. `presented_order` defaults to the same order and must be a
permutation of the two IDs. Optional fields are `source_pair_uid=null`,
`repetition_index=0`, `is_reversal=false`, `reversal_group=null`, and `metadata={}`.
The repetition index is a nonnegative integer.

`pair_id` identifies the underlying pair; `pair_uid` identifies one scheduled
occurrence. Repetitions and reversal audit occurrences have distinct `pair_uid`
values. The preparation helper preserves the base occurrence as `source_pair_uid`.
Seeded presentation changes order, not occurrence identity. Reversal presentations
are opposite their corresponding base presentation.

## Standalone assessment-output contracts (Milestone 3 Phase 1)

`judgmentllm.output_contracts` defines strict `RubricOutputContract` and
`PairwiseOutputContract` models. Their literal integer `contract_version=2`
identifies output interpretation semantics only. It is neither a RunSpec version
nor a permitted model-output field: closed output validators reject it. The existing shared `VersionedModel`
continues to accept document `schema_version=1` only.

Rubric contracts contain a nonempty, unique ordered tuple of validated `traits`,
`task_kind="rubric"`, `output_order="together"` and `explanations=false` by default.
`scores_first` and `explanations_first` require explanations; each uses separate
`scores` and `explanations` arrays, joined exclusively by exact trait ID. Pairwise
contracts contain the exact `pair`, `task_kind="pairwise"`, `allow_ties=false`,
`explanations=false`, and only the default `output_order="together"`. Tie policy
is not a rubric setting. Unknown or incompatible fields are errors.

`generate_output_schema` produces closed JSON Schema 2020-12 objects from that
validated membership. Scores use disjoint exact-trait alternatives and each
trait's ordered category enum. Arrays have exact membership-count bounds;
local validation additionally enforces complete unique membership. Required
explanations must be nonblank locally. The schema requests meaningful property
order, and `serialize_output_schema` preserves that order in UTF-8 JSON bytes.
Historical canonical semantic hashes continue sorting keys; they are distinct
from ordered provider-facing serialization. No user-authored schema is accepted.

The strict contract validators accept raw JSON text/bytes or parsed data and
return existing `CategoricalOutput`/`PairwiseOutput` selections. Raw bytes must be strict UTF-8. Raw decoding rejects duplicate object keys,
malformed JSON and nonfinite numeric tokens. Parsed data is recursively checked
for finite JSON values, rejecting nonstring object keys and arbitrary Python
objects. Duplicate-key detection must occur before SDK decoding: a parsed dictionary
cannot reveal discarded duplicate keys and carries no such guarantee. Complete
rubric validation rejects every missing, duplicate, unknown or malformed member;
it never reports partial salvage as a valid whole output. Explanation fields,
including explicit nulls, are forbidden when disabled; separated scores forbid
inline explanations. IDs/category labels match exactly without coercion or repair.

The new pairwise schema requests only `winner_id` for forced choice, plus a
required nonblank explanation when enabled. Its compatibility validator also
accepts the closed historical `tie:false` form and normalizes an omitted tie to
false. With ties enabled, explicit `tie` is required and must agree with a null
winner for a tie or a presented winner ID for a non-tie. Neither probabilities
nor missing output imply an observed winner. The historical v1 parser still
requires its explicit `tie` field.

The optional explicit contract on internal `execution.processing.interpret`
allows independent-member salvage from recognizable envelopes. It indexes
scores/explanations separately, invalidates a trait duplicated on either side,
and preserves complete siblings through existing normalizers. Unexpected entries
produce bounded content-free counts. An unrecognizable envelope completes no
members. Contract task kind, exact ordered membership, trait definitions and
explanation/tie settings must match the supplied request/specification. All task/request/attempt IDs, statuses, mapped codes,
probabilities and original/presented orientations use existing result semantics.
Generative categorical selection without probabilities remains `category_code`,
with no fabricated expected score, probability distribution or modal category.

Phase 2 binds this contract-aware boundary to explicit RunSpec v2, shared
rendering/preview and immutable schema/request commitments before mock
submission. V1 retains its original behavior. Real provider adapters/translation
remain later deliverables. Phase 4B now persists safe usage/reasoning/order evidence through separate companion receipts.

## Run configuration

`load_run_spec(path) -> RunSpec | RunSpecV2` loads safe YAML/JSON, rejects duplicate keys,
and resolves references against their containing configuration file.
`load_rubric(path) -> Rubric` loads an individual rubric. Direct Pydantic model
validation never opens files.

| Field | Version 1 behavior |
|---|---|
| `task_kind` | Required `rubric` or `pairwise`. |
| `input` | Required response `InputSpec`, or a path shorthand. `InputSpec` has `path`, optional `format: csv/jsonl` inferred from extension, and `allow_empty_text=false`. |
| `rubric` | Required for rubric tasks; inline rubric or referenced YAML/JSON. |
| `criteria`, `pairs` | Required nonempty criteria and pair input for pairwise tasks; incompatible with rubric tasks. Criteria can be inline or a referenced `{schema_version, criteria}` document. |
| `exemplars` | Ordered inline list or referenced `{schema_version, exemplars}` document; defaults to empty. |
| `provider`, `endpoint`, `model` | Required nonempty strings. Model IDs remain open; these values do not imply an implemented or qualified provider adapter. |
| `generation` | Optional strict `GenerationSpec` with `temperature`, `top_p`, and `max_output_tokens`; omitted/null/empty means no requested controls. Only non-null requested members serialize. No provider defaults are inferred. |
| `execution` | `mode=live` (`live/bulk_live/batch_native` intent), `grouping=independent` (`independent/grouped/auto`), optional fixed `groups`, optional positive `max_traits_per_request`, `explanations=false`, optional `tier`, `concurrency=4`. `cache_policy` defaults to `auto` for rubric and `off` for pairwise; valid values are `off/auto/explicit`. Mock live execution supports `off` and uncached `auto`; `explicit`, non-null tier and other execution modes fail before submission. |
| `prompt` | Choose `preset` or `file`; `advanced=false`; configurable rendering safeguards under `limits`. Default preset follows task kind and explanation setting. Advanced mode requires a custom file. |
| `normalization` | `NormalizationOptions`, with `probability_sum_tolerance=1e-8`, configurable in `[0,1)`. |
| `capture`, `output_path` | `capture=full` (`full/minimal`); output defaults to `run-data` relative to the run configuration. Internal preparation enforces source retention policy; its runtime store is supplied separately and must be local. |
| `seed`, `strict_capabilities`, `allow_ties` | Optional integer pairing/presentation seed, distinct from a future generation seed; strict capabilities defaults false; ties default false and are pairwise-only. Tie prompts require a custom template. |

Pairwise preparation uses independent grouping. Fixed groups must partition all
rubric traits exactly once without empty groups. Auto grouping requires an
explicit or supplied capability limit; it never invents context windows or
splits exemplars. Retry policy is an execution-service control; it is not a RunSpec field. Explicit
retry selection uses the public resume interface. Native-batch, rate/timeout and
budget configuration remain reserved and are rejected as unknown fields today.

### Generation configuration and extensibility

`GenerationSpec` accepts these optional controls; every value defaults to unset:

| Field | Neutral schema rule |
|---|---|
| `temperature` | Finite nonnegative number; no universal upper limit is invented. |
| `top_p` | Finite number in `[0, 1]`. |
| `max_output_tokens` | Strict positive integer; a fractional number, numeric string, or boolean is invalid. |

Numeric controls reject booleans, numeric strings and nonfinite values. Neutral
sampling values preserve integer versus float representation. Schema validity
does not establish provider support. Endpoint/model restrictions
may narrow the accepted range or prohibit combinations such as requesting both
sampling controls; there is no universal prohibition of that combination.

For example, the following block requests two values and leaves `top_p` unspecified:

```yaml
generation:
  temperature: 0
  max_output_tokens: 512
```

Omission, `generation: null`, `{}`, and all-null members normalize to no
generation configuration. With no controls, `RunSpec.model_dump()` and
`model_dump_json()` omit `generation`, preserving Milestone 1 serialization.
Nonempty generation serialization omits null members and retains explicit zero
values. Execution settings and the top-level `seed` do not populate generation.

The extensibility design reserves typed generation fields `top_k`,
`stop_sequences` (ordered strings), `repetition_penalty`, `frequency_penalty`,
`presence_penalty`, and `generation.seed`, plus adapter-owned typed reasoning/
thinking controls. These are not accepted fields in this initial foundation.
Future additions require documented strict schemas and per-adapter validation;
provider-specific extensions must be owned by their adapters. There is no
dictionary for arbitrary provider request JSON. Future stop sequences retain
their exact text and ordering and must obey the capture policy for text-bearing
configuration. A generation seed cannot guarantee identical provider outputs.

### Generation capability and reproducibility boundary

`CapabilityProfile.generation` optionally supplies `GenerationCapabilities` for
an exact `provider`, `endpoint`, and `model`. Each current parameter has a
`GenerationParameterCapability` with `supported` (`true`, `false`, or null for
unknown) and optional `minimum`/`maximum` bounds. Typed
`incompatible_combinations` name controls that cannot be requested together.
Evidence fields (`source`, `fixture_tested`, `live_qualified`) are separate from
the requested settings; neither a schema-valid run nor synthetic evidence
establishes cloud qualification. The foundation embeds no cloud support matrix.

Known unsupported settings, violated bounds, incompatible combinations and
mis-scoped evidence fail preflight. Required unknown support is reported
explicitly and becomes an error under `strict_capabilities`. Runs without
generation controls receive no new generation diagnostics and preserve existing
strict-capability behavior. Unknown model IDs remain valid configuration values.

All explicitly requested controls contribute to a separate generation commitment
for immutable manifests and reproducibility. Item, pair and task IDs stay stable.
Request context incorporates generation only when controls are explicitly
requested; configurations with no generation controls retain their Milestone 1
request IDs. Structural IDs are not exact payload identities.
Capability evidence can change without changing requested settings
identity. Before submission or resume, revalidate against the latest available
restrictions; newly known incompatibilities block continuation. A changed
requested control cannot reuse the frozen configuration or submission identity.

Generation-only commitment fixtures remain distinct from complete executable
manifests. Internal preparation now freezes source identities and indexes,
seals the complete manifest, and combines source and generation verification.
Source provenance retains exact original hashes separately from normalized
semantic configuration identity. Minimal capture commits text-bearing assessment
configuration through hashes and source references. Mock request preparation
includes each requested setting in the actual prepared body and its exact byte
hash, preserving omission and values. Real adapter mappings remain deferred. Milestone
3 implements generative mappings, Milestone 4 rejects inapplicable settings on
native score/decision endpoints, and Milestone 5 verifies live/batch parity.
Public CLI configuration and the Milestone 6 `rubricLLM` wrapper use this same
RunSpec rather than inventing independent defaults or provider payload schemas.

## Normalized results

Both `RubricResult` and `PairwiseResult` require separate `run_id`, `task_id`,
`request_uid`, and `attempt_id`. Logical task IDs remain independent of grouping;
request IDs identify ordered groups and assessment context. The mock executor
allocates distinct durable attempt IDs and stores observed outcomes without changing this normalized result schema.

Common fields include `status` (`complete/invalid/refused/errored/cancelled`),
`error_kind`, `error_message`, `provider`, `endpoint`, `model`, `tier`, `mode`,
`confidence`, `explanation`, optional usage counts (`input_tokens`, `output_tokens`,
`cache_read_tokens`, `cache_write_tokens`), and optional audit references
(`raw_response_path`, `prompt_path`, `manifest_sha256`). Unavailable values stay
null. Confidence is bounded to `[0,1]`; usage counts are nonnegative integers.
The effective `probability_sum_tolerance` is recorded when probabilities exist.
Mock submission/observation identities, immutable manifests and sidecars are
linked through repository evidence. Real provider request/batch IDs and SDK
diagnostics remain assigned to their adapter milestones.

### Rubric results

`normalize_rubric_result(trait, *, run_id, task_id, request_uid, attempt_id,
item_id, category_label=None, probabilities=None, provider_expected_index=None,
explanation=None, options=None, provider=None, endpoint=None, model=None)`
returns `RubricResult` with `task_kind="rubric"`, `trait_id`, and the complete
ordered `resolved_levels` coding.

| Field | Meaning |
|---|---|
| `category_label`, `numeric_category_value` | Observed provider-selected category and its code, if supplied. They are never inferred from probabilities. |
| `modal_category_label`, `modal_numeric_category_value` | Highest-probability category and code when a distribution exists. Tied maxima choose the first category in rubric order. |
| `probabilities` | Complete ordered array of `{level_label, probability}`; null for categorical scoring. |
| `expected_value` | `sum(probability * numeric_value)` over every category; null without a distribution. |
| `provider_expected_index` | Unmodified provider-reported zero-based expectation, if supplied; finite and within the category-index range. Never substituted for a mapped expectation. |
| `primary_score`, `primary_score_kind` | Expected value and `expected` when probabilities exist; selected code and `category_code` otherwise. Unsuccessful results cannot claim a primary score. |

Distributions must match every rubric label exactly once in rubric order. Each
probability must be finite in `[0,1]`, and the sum must differ from one by no more
than the configured absolute tolerance. Zero tolerance requests an exact sum.
No malformed array is silently reordered, filled, or renormalized. A selected
category can differ from the modal category; both observations are retained.
Direct result validation also rejects inconsistent codes, expectations, modes,
and primary scores.

For labels `Beginning, Developing, Proficient` with default codes `[0,1,2]`,
probabilities `[0.10,0.25,0.65]` yield `expected_value=1.55`. Explicit codes
`[1,2,7]` yield `5.15`, while the provider index remains `1.55` if reported.
A categorical `Proficient` yields only its code, without invented probabilities.

`validate_categorical_output(data, traits, *, explanations=False)` independently
validates generative JSON shaped as
`{"scores":[{"trait_id":"quality","category_label":"Proficient"}]}`.
It rejects missing, duplicate, and unrequested traits, unknown categories,
unknown fields, numeric category labels (generative labels must be strings), and
absent/blank explanations when required. If explanations are not requested, the
`explanation` field is forbidden, including an explicitly supplied null. Prompt
edits cannot disable this validation.

### Pairwise results

`normalize_pairwise_result(pair, *, run_id, task_id, request_uid, attempt_id,
winner_id=None, tie=False, p_left=None, p_right=None, p_tie=None,
allow_ties=False, explanation=None, options=None, provider=None, endpoint=None,
model=None)` returns `PairwiseResult` with `task_kind="pairwise"` and the pair's
original/presented order and occurrence/reversal metadata.

A complete result requires a winner among the original two IDs or an explicitly
permitted tie. A tie has `winner_id=null`; forced choice rejects ties. Optional
probabilities must contain both `p_left` and `p_right`; when ties are permitted,
`p_tie` is also required for a complete distribution. Probabilities always refer
to original left/right IDs, regardless of presentation, and use the same sum
validation as rubric distributions. Winners are observed, never inferred from
probabilities. `presented_first` and `presented_second` are convenience Python
properties; serialized records retain `presented_order`.

`validate_pairwise_output(data, pair, *, allow_ties=False, explanations=False)`
validates `PairwiseOutput` separately from normalization. Its shape is
`{"winner_id":"A","tie":false}`; a permitted tie uses
`winner_id=null` and `tie=true`. Both `winner_id` and `tie` must be supplied.
Unknown fields, missing outcomes, invalid winners, forbidden ties, missing required
explanations, and an explanation field when explanations are not requested are errors.

## Streaming file formats

`judgmentllm.io` provides lazy `read_responses`, `read_pairs`, `read_jsonl`,
`read_results`, and `read_results_csv` readers. Response/pair readers retain only
seen IDs for duplicate detection plus the current record; they do not allocate
a response-by-trait Cartesian product. Late errors remain possible during
iteration. CSV requires named columns; metadata and pair order fields contain
JSON. Unknown/duplicate columns, malformed rows, duplicate IDs, and blank JSONL
records fail with a source location.

JSONL is the canonical normalized format. `write_results_jsonl` and generic
`write_jsonl` consume iterables one record at a time and return the count written.
`export_results_csv` uses a fixed union header for rubric/pairwise fields and
preserves nested coding/probability arrays as JSON cells. Null is the literal
`\N`; an empty string stays empty; string values beginning with a backslash gain
one escape backslash. `read_results_csv` reverses these rules and revalidates
models. The result kind's unused union columns must be null.

Writers overwrite their destination and require its parent directory to exist.
Failure can leave a partial file. These utilities are not a persistence journal.
Capture modes govern runtime persistence; offline preview returns full
requested text. Never put credentials in configuration, metadata, or examples.

## Derived R-compatible audit views

`export_pairwise_r_csv(path, records, *, na_string="NA")` and `export_rubric_r_csv(...)` are available from `judgmentllm` and `judgmentllm.r_exports`. These iterator-based views revalidate each current result and retain every supplied attempt, including failures and observed ties. They do not replace the canonical normalized formats. IDs must be imported as character strings in R. Missing values use a configurable marker with literal collisions rejected. See [the complete R export schemas and runnable examples](r-interoperability.md).

Pairwise `ID1/ID2` (`A_id/B_id` aliases) represent presented order. `better_id` preserves an observed winner; numeric `result` is 1/0/0.5 only for successful observed wins/ties. Original `p_left/p_right` and presented `p_first/p_second` remain distinct. Rubric views retain score kinds, selected/modal categories, expected values, and complete probability/coding JSON. Selecting an analysis dataset requires explicit attempt choices; exporters never select or combine retries.

## Internal mock execution evidence (Subphase 3)

Normalized results remain schema version 1 with unchanged canonical/R export
columns. Submission and observation IDs are linked through repository evidence,
not added as competing result columns. Real task/request/attempt identities and
existing provider/model/mode/manifest/audit-path fields are populated through
validated reconstruction. Synthetic distributions preserve selected categories,
modes, expectations, and complete mappings separately; pair probabilities remain
in original orientation despite reversed presentation.

The typed mock wire protocol is `judgmentllm-mock-v1`. It commits original ordered
membership, exact dependency, scope, prompt, output contract, and sparse requested
generation controls. Unspecified generation is omitted. Prompt hash, actual-byte
payload hash, logical request, attempt, correlation, and observation identities
have separate meanings. An independent accepted observation must exist before
retrieval/normalization; deterministic recipes and hashes alone cannot establish
acceptance. Local preparation failures and unresolved submissions create no
normalized judgments. Refusal, invalid received output, authoritative provider
error, and definite invoked transport nonacceptance retain distinct evidence.


## Provider evidence records (Milestone 3 Phase 4A)

`judgmentllm.evidence` supplies standalone strict, frozen public records with their
own `schema_version: 1`. Unknown fields, incompatible semantics and coercions are
rejected with fixed content-free validation diagnostics. Existing `ID` and
`SHA256` types preserve identity compatibility and character IDs with leading
zeros. These records are independent of RunSpec v1/v2, assessment results,
receipts, SQLite and CLI control schemas; none of those changes in Phase 4A.

| Record | Scope and contract |
|---|---|
| `EvidenceIdentity` | Existing run/request/attempt/submission IDs and nullable observation ID, supplied from trusted execution metadata. |
| `ProviderUsage` | Reported facts for one physical submission, including nullable observation/remote/failure references, scoped provider/model/SDK metadata, clock-qualified timing, six nullable strict nonnegative token counters, source/semantics version, typed breakdown and separate reported/estimated cost. |
| `ProviderObservationMetadata` | One established observation: requested rubric layout or null for pairwise, nullable safe emitted root/member key sequences, occurrence indices, known trait IDs, adherence/evidence enums and a safe unknown reason. |
| `ReasoningEvidence`, `ReasoningAuthorization` | Observation-level documented block kind, field/type, ordinal, availability and nullable safe artifact/hash references; explicit retention decisions independent of assessment explanations and computation requests. |
| `ScopedProviderEvidence`, `EvidenceProvenance` | Exact provider/endpoint/model or unresolved model scope, nullable SDK/API version, feature and provenance; separate implementation, fixture and live qualification facts. |

No extractor creates a client, reads credentials, writes artifacts, aggregates a
run or infers unreported counts/costs. `None` is unknown and differs from zero;
missing totals are never calculated. Typed usage breakdowns retain original
provider field paths and explicit inclusion/exclusion semantics. Detail counts
must not be added to parent counts. Repeated retrieval of an observation is not
another physical submission; distinct retries remain distinct submissions.

Order extraction accepts the existing `OutputCommitmentV2` and exact emitted text
with an explicit trustworthy source. It uses the existing duplicate-rejecting
JSON decoder after byte/depth bounds; member occurrences are bounded too.
Duplicate/malformed/truncated/oversized evidence produces safe unknown metadata.
Only contract-authorized key names and trait IDs already in the contract can
survive extraction; unsafe sequences become null and use `unsafe_key_names`.
Array occurrence indices do not claim trait order. A verified mismatch is
strictly diagnostic and never changes normalized validity or retry authority.

Full capture alone does not authorize reasoning retention. Minimal capture cannot
retain reasoning text. Explicit summary authorization covers documented summaries;
provider-text authorization covers documented thinking blocks. Phase 4A represents
these decisions without changing RunSpec or current execution capture.

Exact callable interfaces and limits are in the
[Python API](python-api.md#standalone-evidence-phase-4a-implemented). Durable
attachment, receipts/recovery, redaction and evidence revision persistence remain
Phase 4B; native translation/transport is 4C; real generative adapters Phase 5;
companion exports and new R joins Phase 6; explicit live qualification Phase 7.
Current canonical/R result schemas and all existing strict R gates remain intact.

## Offline credential status (Milestone 3 Phase 3)

`CredentialStatus` is a separate frozen diagnostic record, not run configuration
or assessment evidence. Its five fields and cross-field constraints are
documented in the [Python API](python-api.md#credential-diagnostics-phase-3-implemented).
The CLI uses the existing version-1 control envelope. No RunSpec v1/v2, result,
manifest, database or export schema changes accompany authentication diagnostics;
no credential values or status snapshots enter those contracts.

## Durable provider evidence (Phase 4B)

Normalized result schema 1, RunSpec v1/v2, mock wire v1/v2, canonical/R columns
and CLI envelope 1 are unchanged. New internal stores use registry schema 2;
historical schema 1 is verified without migration/backfill and has unavailable
companion evidence. Usage is once per physical submission, order/reasoning once
per observation, joined through existing attempt/request/submission identities.
No token counts or reasoning blocks are added to assessment rows.

Separate evidence receipts version 1 bind immutable execution identities, payload,
acknowledgment/response commitments and capture/authorization policy. Append-only
usage revisions have explicit source/version and stable sequence high-water
selection; corrections replace selection, never add charges. Safe order metadata
survives minimal capture. Reasoning retention defaults to none and filtering
precedes body retention; positive synthetic fixture grants are separately sealed.
See [persistence contracts](persistence-contracts.md#phase-4b-durable-evidence-integration).

Complete reported Anthropic 5-minute/1-hour cache-write subdivisions cannot sum
to more than their reported parent. Partial subdivisions and absent parents stay
valid unknown/partial facts; equality and missing totals are never manufactured.
`synthetic.reported_cost` cannot accompany a real-provider usage identity.


## Internal native-projection contracts (Phase 4C1)

Standalone internal version-1 projection and transport-decision records do not
change RunSpec v1/v2, normalized results, exports, schema-selection v2 or durable
output commitments. They retain exact ordered schema and format-fragment text,
byte hashes, immutable assessment identity/settings, scope/profile versions,
requested property orders and explicit native/local constraint records. The
semantic projection/decision digest is separate from ordered byte identity and
from existing request identities. Required identifiers/category labels exist in
memory but are not diagnostic or minimal-capture evidence.

The [OpenAI projection matrix](assessment-provider-evidence.md#phase-4c1-openai-projection-matrix)
documents the conservative subset and general schema limits. Every accepted
assessment still passes the complete existing canonical validators. No new
user-authored JSON-schema parameter, arbitrary payload setting, result column,
R export or persistence migration is introduced. Durable integration is 4C3.


Native-record `model_validate_json()` rejects duplicate object keys at every
JSON depth before Pydantic can apply last-key-wins decoding. A duplicate is
invalid even when its final occurrence matches the expected value and all
projection/decision digests would otherwise match. This applies to projection,
decision, native scope and private profile JSON entry points. Both passes consume the same immutable JSON: exact built-in `str`/`bytes`, or
one snapshot of built-in `bytearray`. Input subclasses are unsupported. JSON-mode
validation preserves date/tuple parsing, Unicode and exact version tokens. Errors are content-free. Historical `EvidenceModel` parsing and existing
serialized representations/identities remain unchanged.


## Internal native-projection V2 contracts (Phase 4C2)

The new standalone `NativeSchemaProjectionV2` and `TransportDecisionV2` contracts
coexist with unchanged OpenAI V1 records. Their evidence-envelope `schema_version`
remains 1, while `projection_version` and `decision_version` are independently 2.
No RunSpec, output contract, normalized result, persistence or R schema changes.

V2 retains detached source semantics, exact canonical schema identity, exact
ordered native/SDK-configuration/converted-format representations and hashes,
requested root/member orders, reviewed route/dialect/implementation scope,
constraint coverage and provenance. No universal schema-name field is imposed.
Coverage distinguishes representation, SDK retention, documented support and
independently verified enforcement; the last remains unknown for real models.
Every complete canonical local validator remains mandatory.

Strict JSON ingress inherits the Phase 4C1 immutable snapshot and duplicate-key
preflight, including nested and escaped-equivalent duplicates. Rehashed inconsistent
records fail regeneration checks. V2 identities use distinct versioned domains.
Frozen decisions bind the exact Google schema route and verify without reselection.
Content-bearing schema records are in-memory candidates, not minimal-capture
metadata or authorization for durable retention.


## Explicit RunSpec 3 and durable transport versions

`RunSpecV3` / `PromptSpecV3` are additive typed contracts. Root `schema_version: 3`
is required; omission still selects 1. V1/V2 reject the new field and keep exact
serialization. V3 inherits assessment controls and adds
`prompt.native_mechanism`, a closed literal union of nine reviewed mechanisms.
Auto/native require that explicit route; text may omit it. Unknown mechanisms,
arbitrary provider dictionaries and user-defined schemas are rejected.

`OutputCommitmentV3` commits the unchanged neutral contract 2, renderer 3,
FrozenTransportSelection 1, canonical schema bytes and semantic identity, optional
unchanged projection 1/2, exact SDK fragment, repetition policy and scoped runtime
context identity. Content-bearing fields remain private in representations.
`TransportPlanV1` commits configuration, input dependency, ordered membership,
output, prompt and physical identities before the complete payload is assembled.
`TransportCommitmentV1` adds exact schema/configuration/format/prompt/payload
hashes and lengths plus the final plan digest. Its safe receipt contains no
complete schema, category labels, student responses or exemplars.

These new records inherit duplicate-safe JSON ingress, strict integer version
tokens, frozen/deep-revalidated values and content-free errors. Semantic hashes,
ordered representations and final payload hashes are distinct. Unknown fields,
changed nested values and conflicting rehashed records cannot replace immutable
manifest/attempt bindings. Normalized results, outcome semantics, schema 1
exports and R contracts are unchanged. Production execution still has no real
generative adapters; native preparation uses private synthetic context only.


## Phase 5A production records

RunSpecV3 is required for OpenAI `responses` and `chat.completions`, even with
`prompt.structured_output: text`. Existing V1/V2 configurations never gain
production execution or migrate sealed runs. V3 is shared configuration syntax;
its transport context and sealed manifest distinguish synthetic from production.

Production manifest 4 identifies `openai-production-v1`; output commitment 4,
PreparedRequestV4, and transport commitment 2 have independent version tokens.
RenderedProductionPromptV1 carries output commitment 4 with renderer 3 text.
Historical RenderedPromptV3/output commitment 3 remain unchanged. Production
receipts bind original physical IDs, ordered membership, authority/context,
configuration and generation, exact prompt/schema/body hashes and lengths, SDK
scope, explicit storage=false, and retry lineage. API bodies contain no local
transport-plan or identity fields.

New Responses usage has source `openai.responses.usage`; cached input and
reasoning output counters are included in parent counters, never summed twice.
Optional observation metadata preserves reported model, SDK and completion outcome
separately from requested configuration. Absent metadata retains historical
serialization. Existing result/CSV/JSONL schemas and IDs are unchanged.


## Phase 5A assessment diagnostic record

`AssessmentDiagnosticV1` is an optional nested `assessment_diagnostic` in the
existing version-1 companion evidence receipt. Absence is omitted from serialized
receipts, preserving historical bytes. This addition changes neither RunSpec,
manifest, SQLite schema nor normalized result/JSONL/CSV/R columns.

The record independently versions its diagnostic as integer `1` and binds the
complete run/request/attempt/submission/observation identity, endpoint, requested
and reported models, remote response identity, provider outcome and capture
policy to safe extraction metadata, `parser_diagnostic` and `canonical_valid`.
The latter describes complete canonical output validation, independently of
provider acceptance and any per-member result salvage. Fixed parser diagnostic
codes
cover JSON syntax, duplicate keys, root/field/trait/category/winner errors,
missing text, unsupported/multiple components and size bounds. Provider outcomes
record refusal, truncation and completion independently. No arbitrary provider
values or excerpts enter metadata.

Only one separately recognized documented assessment-text component is eligible.
The 1 MiB component cap is independent of the unchanged 8 MiB HTTP response cap
and whole-envelope safety rules. An oversized diagnostic does not suppress an
existing independently proven-safe whole-envelope retention. Full capture
retains exact UTF-8 text even when local validation fails; minimal capture
stores a hash-only artifact and byte
length. Over-limit or unsupported extraction is withheld without truncation or
hashing unauthorized content. Reasoning, unknown fields, tools and provider
metadata are excluded. Protected text is unvalidated output, not a scoring input
replacement, retry grant or qualification claim. Historical omissions remain
unavailable. See the read-only [inspection service](python-api.md#read-only-assessment-diagnostics).


## Anthropic production additions (Phase 5B)

New Messages execution uses `ExecutableRunManifestV5`, `OutputCommitmentV5`,
`PreparedRequestV5` and `TransportCommitmentV3`, with distinct Anthropic digest
domains and explicit SDK/API/model/endpoint/physical identity. Request policy
records requested versus default4096 max tokens, tier and cache decisions without
rewriting `GenerationSpec`. Configuration remains RunSpecV3. The independent
`AnthropicProductionContext` never reinterprets an OpenAI production context.

Assessment component/diagnostic V2 adds Messages endpoint and safe stop-reason
metadata. Recognition, exact component hash/length and capture authorization stay
separate from validity; no text is serialized in inspection metadata. Existing
OpenAI diagnostics V1, manifests V1–V4, output commitments V1–V4, synthetic
transport receipt1, OpenAI receipt2, SQLite schema and normalized result/export
versions remain unchanged. Anthropic usage keeps total null and distinguishes
uncached input from cache creation/read subdivisions. No distributions are added.

The Anthropic qualification plan has its own provider namespace. Its exact source,
complete request bytes, per-case input estimate evidence and plan digest are
immutable; plan creation grants no physical submission authority. See the
[adapter guide](anthropic.md) for controls and estimate limitations.
