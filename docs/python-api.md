# Public Python API and versioned assessment contracts

Milestone 1 supports offline preparation and normalization of supplied synthetic outputs.
Milestone 2 Subphase 2 adds provider-free immutable source preparation, indexed
input access, complete manifests, and generation compatibility validation.
Historical mock examples below run without credentials; explicitly labelled OpenAI execution examples require credentials and incur API charges. Public assessment models, common
normalization functions, planning functions, and preview are also exported from
`judgmentllm`. Subphases 3–4 provide mock execution/recovery, grouped outcomes,
retry/cancellation and immutable exports. Subphase 5 exposes the shared public
lifecycle services below; the installed CLI calls these same functions.
Subphase 6 adds development benchmark tooling and executable examples around
these services. Its final implementation head `db198aeb0a6387e6a3030c584c34bdd0b41e890f`
was qualified and merged in PR #11; the retained historical six-configuration
evidence and current seven-configuration policy are distinguished in the
[design status](design-spec.md#milestone-2-implementation-boundary). No additional
public lifecycle interface or schema is introduced by that subphase.

## Public execution lifecycle (Milestone 2 Subphase 5)

Runnable public examples are `examples/mock_lifecycle.py` (validation, planning,
grouped rubric execution, inspection and canonical/R exports) and
`examples/mock_recovery.py` (independent installed-CLI processes, partial outcomes,
explicit failed-member retries and immutable old exports). Pairwise execution
uses `examples/mock-pairwise.yaml`. See the [scale guide](scale-qualification.md)
for local storage and separate benchmark commands. These examples use synthetic
inputs and the mock adapter. Phase5A OpenAI examples are documented separately in the [OpenAI guide](openai.md).

The top-level `judgmentllm` package and `judgmentllm.lifecycle` expose:

```text
validate_inputs(spec_or_path, *, capabilities=None, exhaustive_rendering=False) -> InputValidationReport
estimate_run(spec_or_path, *, capabilities=None) -> ExecutionPlan
run_live(spec_or_path, *, store=None, progress=None, mock=None) -> RunSummary
arun_live(spec_or_path, *, store=None, progress=None, mock=None) -> RunSummary
resume_run(run_id, *, store=None, spec=None, use_frozen_snapshot=False,
           retry=None, progress=None, mock=None) -> RunSummary
aresume_run(run_id, *, store=None, spec=None, use_frozen_snapshot=False,
            retry=None, progress=None, mock=None) -> RunSummary
list_runs(*, store=None, limit=100, cursor=None) -> RunPage
get_run(run_id, *, store=None) -> RunStatus
export_run_results(run_id, *, store=None, format="jsonl", view="canonical",
                   na_string="NA", high_water_mark=None) -> ResultManifest
```

`spec_or_path` accepts a `RunSpec`, `RunSpecV2`, `RunSpecV3`, string path or `Path`. Synchronous execution
wrappers reject calls inside an active event loop before doing any I/O, with
instructions to call their async equivalent. Both execute in the calling process;
no background service continues after that process exits. Cancellation of async
execution drains durable writes and ownership before propagating `CancelledError`.
Coarse synchronous progress observers receive frozen `ProgressEvent` records with
`stage` (`started`, `prepared`, `finished`), durable `run_id` and absolute `store`.
Progress contains no prompt, assessment text, raw response or exception dump.

V1/V2 execute only the exact combination `provider="mock"`, `endpoint="synthetic"`.
Phase5A adds `provider="openai"` with `endpoint="responses"` or
`endpoint="chat.completions"` through explicit RunSpecV3 production transport.
Other provider identities fail with `unsupported_capability` before mock
construction or submission. Offline validation/planning continue to accept
other valid configurations with explicit capability unknowns; validity does not
claim that a real adapter is implemented. The provider-owned pure mock preflight
rejects unsupported mock modes, tiers and explicit caching before execution.
No price, token count, cache saving or model qualification is invented.

`InputValidationReport` validates the complete source set through the existing
immutable preparation pipeline and returns at most 100 issues, `total_issues`
and `truncated`. Structural warnings remain visible. Counts are null when
validation could not establish them; invalid source/configuration input returns
`valid=False`, while storage/internal failures raise `LifecycleError`.
`exhaustive_rendering=True` additionally renders every actual request; normal
validation uses the established bounded representative rendering checks.
`ExecutionPlan` derives exact logical task/request counts and ordered trait groups
from the same privately prepared sources. Token/cost estimates remain null;
synthetic billing is `not_applicable`. Neither function leaves a durable user run.

Store selection is the explicit `store`, then `JUDGMENTLLM_RUN_HOME`, then the
configuration's resolved `output_path`, otherwise `./run-data`. Commands without
configuration need an explicit store or that environment variable to discover
nondefault stores. Run references accept original string IDs or absolute run
directories. Run directories retain the existing hashed basenames; the library
recovers and verifies the original identity rather than deriving it from that
basename. An exact registry ID in an explicitly selected store takes precedence,
including for IDs that resemble filesystem paths. Paths in returned `ArtifactPaths`
are absolute and durable; unavailable artifacts are null.

Status/list/export do not submit, retrieve provider work or require original
assessment sources. Status uses a short database snapshot and a source-independent
verified manifest. `executor_active` reflects a read-only OS-lock probe (null when
it cannot be established); heartbeat age is never takeover authority. A persisted
`running` state with no executor and `recovery_required=True` describes interrupted
work, not continuing background activity. Queries do not fabricate provider
cancellation or repair execution by making provider calls.

`RunStatus` keeps exact planned counts separate from materialized tasks, physical
attempts and historical successful/unsuccessful result rows. `completed_tasks`
counts current logical task states; `failed_tasks` counts invalid/refused/errored
logical states, and `cancelled_tasks` / `blocked_tasks` remain distinct.
`pending_tasks` includes unfinished active/blocked/unmaterialized work, excluding
terminal failed/cancelled tasks. Planned/pending counts are null for incomplete
unsealed preparation. `submission_certainty` reports not-started, not-submitted,
unknown and accepted counts independently of results. `recovery_required` covers
unresolved submissions and inactive created/submitted/running runs. Usage fields
remain null when unreported; grouped request usage is never multiplied across
traits. `RunSummary.outcome` describes this processing call independently of the
durable `run_status`; a completed successful retry may retain older unsuccessful
result rows while its current logical tasks are complete.

`RunPage.runs` contains at most `limit` summaries (default 100, maximum 1,000),
newest first by creation time and original run ID, plus `next_cursor`. Cursors are
versioned, opaque and tied to the selected store. An absent store lists no runs
without creating a database; a missing status/export resource raises `not_found`.

### Mock reconstruction and explicit subset retries

`MockExecutionOptions(ledger_path=None, fixture_path=None,
retrieval_available=True, reconciliation_available=True)` is frozen and typed.
The default independent acceptance ledger is `<store>/mock-service.sqlite3`.
Custom ledger and fixture paths must be supplied again in later processes.
Resume refuses a missing ledger rather than replacing its service identity.
An optional caller-owned JSON fixture has strict fields `schema_version: 1`,
`scenario` (an existing `MockScenario`) and `scenarios` (a mapping from original
request IDs to scenarios). Unknown fields are rejected. Fixtures are synthetic
adapter controls, never arbitrary provider request bodies, and their text is not
copied into public metadata, diagnostics, manifests or the acceptance ledger.
Missing/changed fixture inputs for an unfinished attempt cannot pass its exact
payload commitments. Ledger recipes and hashes alone never reconstruct an
unobserved minimal-capture response.

```python
from judgmentllm import (
    MockExecutionOptions,
    RetrySelection,
    run_live,
    resume_run,
    export_run_results,
)

summary = run_live("examples/mock-run.yaml", store="/local/runtime/store")
assert summary.run_id
# Later, in an independent process:
resumed = resume_run(summary.run_id, store="/local/runtime/store")
exported = export_run_results(
    summary.run_id, store="/local/runtime/store", format="csv", view="r"
)
```

`RetrySelection(task_ids=(...), operation_id="retry-001", allow_completed=False)`
selects explicit tasks from one original request. Pass it as `retry=` to resume.
The existing service freezes original membership, exact prior outcomes and
permission under exclusive ownership, prepares a fresh physical attempt, and
keeps the parent task/request IDs. Any task with an earlier completed result
requires `allow_completed=True`. Reusing an operation ID resumes its original
attempt; changed membership, permission or authorization is rejected. The existing
three-attempt budget and persistent automatic backoff remain in force. A fresh
retry can select a new mock fixture after earlier outcomes have committed;
recovering an unfinished attempt requires its matching original fixture.

Normal resume never repeats completed judgments or invents missing outcomes.
Uncertain acceptance is reconciled, and acknowledged missing observations require
authoritative retrieval. Unavailable evidence remains unresolved. Invalid/refused
outputs never qualify for automatic retry. Full capture requires matching original
sources unless `use_frozen_snapshot=True` explicitly authorizes verified retained
snapshots. Minimal capture requires matching originals; Python-origin minimal runs
also need their original `spec`. Minimal capture still retains authorized normalized
explanations, which can contain sensitive text.

`LifecycleError` exposes safe `category`, `message`, `retryable`, nullable `run_id`
and nullable absolute `store`. Categories align with the version-1 CLI contract;
retry selection/permission/conflict/budget failures are validation errors, while
corrupt durable evidence remains an execution error. No raw exception or provider
body is exposed through these errors.

`ResultManifest` identifies the selected immutable export, all-attempt row count,
result high-water mark, current run state, `incomplete_source`, exact file
paths/hashes/lengths and artifact references. Select canonical JSONL/CSV or R CSV;
`na_string` applies to the R view. Export never creates rows for pending or
unresolved tasks. Equal snapshots/options return identical export identities and
bytes. An explicit older `high_water_mark` preserves its original file references;
querying/exporting an incomplete run can succeed while reporting that state.

## Models and loading

`judgmentllm.models` exposes `ResponseItem`, `Level`, `ResolvedLevel`, `Trait`, `Rubric`, `Exemplar`, `PairwiseCriterion`, `PairTask`, and `validate_exemplars`. `Trait.numeric_values`, `resolved_levels`, and `coding_kind` expose resolved category coding.

`judgmentllm.config` exposes `InputSpec`, `ExecutionSpec`, `GenerationSpec`, `PromptSpec`, `PromptSpecV2`, `RunSpec`, `RunSpecV2`, `RunSpecification`, `ConfigurationError`, `load_run_spec(path)`, `load_rubric(path)`, and `read_document(path)`. Loaders accept strings or `Path` objects. Every referenced path resolves against the configuration containing it. Direct Python construction uses loaded model instances; path shorthand is accepted through model validation for input references.

```python
from judgmentllm import load_run_spec

spec = load_run_spec("examples/run.yaml")
assert spec.rubric is not None
assert spec.rubric.traits[1].numeric_values == [1.0, 3.0, 8.0]
```

Use Pydantic `model_dump(mode="json")`, `model_dump_json()`, `model_validate(...)`, `model_validate_json(...)`, and `model_json_schema()` for serialization/schema access. Path fields serialize to strings. Root documents and records default to schema version 1; unsupported explicit versions fail. No implicit migration exists. Metadata is the only intentionally extensible JSON object. Error diagnostics omit raw input values where practical.

`GenerationSpec(temperature=None, top_p=None, max_output_tokens=None)` is frozen
and strictly typed. Sampling values accept finite numbers (temperature >= 0,
top-p in `[0, 1]`); output tokens require a positive integer. Booleans and numeric
strings are rejected. Unset members do not serialize; explicit numeric zeros do.
`RunSpec.generation` defaults to `None`; null, empty and all-null objects normalize
to `None` and omit the field, preserving existing no-generation serialization.
These controls do not change execution settings or top-level pairing `seed`.
See [data contracts](data-contracts.md#generation-configuration-and-extensibility)
for the bounded future extension design; those extension keys are rejected today.

`GenerationCapabilities` and `GenerationParameterCapability` are available from
`judgmentllm.generation` and the top-level package. A capability record requires
exact `provider`, `endpoint` and `model` scope, and optionally describes each
current parameter with `supported: bool | None` and inclusive `minimum`/`maximum`.
`incompatible_combinations` is a tuple of tuples of distinct parameter names
that cannot all be requested together. Optional `source` and separate
`fixture_tested`/`live_qualified` flags describe evidence; both flags default false.
No provider support or qualification is inferred from configuration validity.

## Normalization and independent output validation

```python
from judgmentllm import normalize_rubric_result, NormalizationOptions
from judgmentllm.results import CategoryProbability

trait = spec.rubric.traits[1]
result = normalize_rubric_result(
    trait,
    run_id="synthetic-run",
    task_id="task-1",
    request_uid="request-1",
    attempt_id="attempt-1",
    item_id="essay_001",
    category_label="2",
    probabilities=[
        CategoryProbability(level_label="1", probability=0.10),
        CategoryProbability(level_label="2", probability=0.25),
        CategoryProbability(level_label="3", probability=0.65),
    ],
    provider_expected_index=1.55,
    options=NormalizationOptions(probability_sum_tolerance=1e-6),
)
assert result.category_label == "2"  # provider observation
assert result.modal_category_label == "3"  # derived mode
assert result.expected_value == 6.05  # 0.1*1 + 0.25*3 + 0.65*8
assert result.primary_score_kind == "expected"
```

All identity arguments above are required and remain distinct. Without probabilities, supply `category_label`; the result has a category code and no expectation or mode. With probabilities alone, provider-selection fields stay null. Provider, endpoint, model, explanation, and provider expected index are optional inputs. Distribution tolerance is recorded with probability results. Tolerance never changes probability values or rescales expectations.

`normalize_pairwise_result(pair, *, run_id, task_id, request_uid, attempt_id, winner_id=None, tie=False, p_left=None, p_right=None, p_tie=None, allow_ties=False, explanation=None, options=None, provider=None, endpoint=None, model=None)` preserves the original pair orientation and presented order. A completed result requires an observed winner or explicitly allowed tie. Probabilities do not manufacture a winner.

`validate_categorical_output(data, traits, *, explanations=False)` validates a `scores` list of `{trait_id, category_label, explanation?}` objects for complete unique trait coverage and known categories. `validate_pairwise_output(data, pair, *, allow_ties=False, explanations=False)` validates `{winner_id, tie, explanation?}` with both outcome fields required. Both validators permit `explanation` only when requested; generative category labels must be strings matching the rendered schema. These functions are independent of custom prompt text. Normalization returns typed `RubricResult` / `PairwiseResult` records; their validators also reject inconsistent derived values.

## Standalone assessment-output contracts (Milestone 3 Phase 1)

These APIs are imported explicitly from `judgmentllm.output_contracts`; they
perform no provider submission, network operation or durable preparation.
`AssessmentOutputContract` is the union of the rubric and pairwise contract
models accepted by schema generation and explicit interpretation:

```python
from judgmentllm.output_contracts import (
    RubricOutputContract,
    PairwiseOutputContract,
    generate_output_schema,
    serialize_output_schema,
    decode_assessment_output,
    validate_rubric_contract_output,
    validate_pairwise_contract_output,
)

contract = RubricOutputContract(
    traits=tuple(rubric.traits),
    output_order="scores_first",
    explanations=True,
)
schema = generate_output_schema(contract)  # dict[str, JsonValue], JSON Schema 2020-12
ordered_bytes = serialize_output_schema(contract)  # UTF-8 bytes, preserves key order
selection = validate_rubric_contract_output(
    '{"scores":[{"trait_id":"cohesion","category_label":"high"}],'
    '"explanations":[{"trait_id":"cohesion","text":"Ideas connect."}]}',
    RubricOutputContract(
        traits=(rubric.traits[0],), output_order="scores_first", explanations=True
    ),
)
```

The example selection assumes the first rubric trait is `cohesion` with a `high`
category. `RubricOutputContract(*, traits, task_kind="rubric", contract_version=2,
output_order="together", explanations=False)` requires a nonempty unique tuple
of validated traits. The other orders are `scores_first` and
`explanations_first`, both requiring explanations. `PairwiseOutputContract(*,
pair, task_kind="pairwise", contract_version=2, allow_ties=False,
explanations=False, output_order="together")` rejects nondefault ordering.
Neither model accepts arbitrary schemas or incompatible assessment controls.
`AssessmentOutputContract` is the union of these two contract types for shared
schema-generation and interpretation interfaces.

`generate_output_schema(contract) -> dict[str, JsonValue]` and
`serialize_output_schema(contract) -> bytes` generate from exact contract
membership. Intentional root/member order, category order and trait-alternative
order are deterministic; serialization does not alter historical canonical
hashing. `decode_assessment_output(raw: str | bytes) -> object` strictly decodes
JSON, requiring strict UTF-8 for bytes and rejecting duplicate object keys and
nonfinite numeric tokens. Parsed inputs are recursively checked for finite JSON
values; nonstring object keys and arbitrary Python objects are rejected. Use
raw decoding at the original output-text boundary. Already-decoded SDK dictionaries cannot
provide evidence that original JSON keys were unique.

`validate_rubric_contract_output(data, contract) -> CategoricalOutput` accepts
raw JSON or parsed data, requires the complete expected group and joins separated
explanations by exact trait ID. Feed each selection and its corresponding trait
to existing `normalize_rubric_result`; no new normalized model is introduced.
`validate_pairwise_contract_output(data, contract) -> PairwiseOutput` accepts
forced-choice winner-only or historical explicit-false input; ties enabled
require an explicit consistent tie/winner pair. Normalize through
`normalize_pairwise_result` with the original `contract.pair` to retain both
orientations. Requested explanations must be nonblank; disabled explanations
and additional fields are rejected, including null fields.

The internal `execution.processing.interpret(..., contract=contract)` is the
Phase 1 grouped interpretation entry point. Its default remains the historical
v1 parser. An explicit contract must match task kind, exact ordered request
membership, trait definitions and the specification's explanation/tie flags. Recognizable envelopes may preserve
independently valid siblings; failed whole-output validation stays a failure.
Scores and explanations match solely by trait ID, with duplicates invalidating
that trait. Results retain original logical identities and existing error/status
semantics; no retry, explanation follow-up, provider call or persistence occurs.

`contract_version=2` versions output semantics independently of RunSpec.
Phase 2 activates it through explicit `RunSpecV2` configurations and the existing
lifecycle services. V1 remains the default and keeps its historical renderer,
mock wire and persistence semantics. Normalized results remain schema version 1.
The six approved [prompt examples](assessment-prompt-examples.md) are exact
custom-template text-mode fixtures; the installed-CLI R lifecycle gate covers
actual durable v2 execution and exports.

## Explicit v2 configuration and execution

`judgmentllm.RunSpecV2` is the public schema-version-2 model; `RunSpec` retains
its version-1 validation behavior. `load_run_spec(path)` defaults an omitted
version to v1 and dispatches explicit integer 1 or 2 to the corresponding model.
Other versions and coerced versions are rejected. Public `RunSpecification` is the
union of both models. `validate_run_spec(value)` and
`validate_run_spec_json(text)` apply the same strict dispatch to loaded data,
including revalidation of copied or mutated models. Referenced documents retain
their own version-1 contracts. Callers may instantiate `RunSpecV2` with the same
resolved inputs/rubric as `RunSpec`, adding a typed v2 prompt specification, or
load a v2 YAML/JSON configuration:

```python
from judgmentllm import (
    load_run_spec,
    RunSpecV2,
    preview_prompt,
    run_live,
    get_run,
    export_run_results,
)

spec = load_run_spec("examples/v2/scores-first.json")
assert isinstance(spec, RunSpecV2)
preview = preview_prompt(spec, item_id="essay_001", trait_id="organization")
assert preview.text
summary = run_live(spec, store="/tmp/judgmentllm-v2-python")
status = get_run(summary.run_id, store="/tmp/judgmentllm-v2-python")
exported = export_run_results(
    summary.run_id, store="/tmp/judgmentllm-v2-python", format="csv", view="r"
)
```

`arun_live`, `resume_run`/`aresume_run`, validation, planning and immutable exports
use the same path. Ordinary callers never need internal preparation/executor
objects. See [runnable examples](../examples/v2/README.md) for all layouts,
partial grouped results, explicit retries, forced-choice pairwise and R import.

V2 `prompt.output_order` is `together` (default), `scores_first` or
`explanations_first`; separated layouts require rubric explanations. Independent
requests use one-trait array contracts. V2 `prompt.structured_output` is `auto`
(default), `text` or `native`. The mock resolves auto to text and records native
unavailability; text explicitly selects that presentation. Unsupported native
fails preflight, with strict local contract validation always required.
Reasoning settings remain reserved and rejected; assessment explanations are
separate. Requested generation controls preserve existing omission semantics.

Preview reports the exact rendered text/output contract and typed requested
layout/resolved presentation metadata through the same renderer used by
preparation. It makes no model call, requires no credentials and persists no run.
Built-in v2 prompts omit zero-exemplar sections; custom templates may omit the
exemplar placeholder only when no exemplars are selected. Supplied exemplars
remain complete, ordered and protected. V1 templates retain historical bytes.
Native provider translation and live authentication remain later work. Phase 4B
persists usage/reasoning/order metadata through the existing execution service;
its internal inspection seam is documented below.

## Structural plans and pair schedules

`validate_run(spec, *, capabilities=None)` returns `ValidationReport.issues`; `.valid` is false when errors occur. `CapabilityProfile` contains optional `explanations`, `grouped`, `ties`, `modes`, `max_levels`, `max_traits_per_request`, and scoped `generation` facts. Unknown does not mean supported. Default unknowns warn; `strict_capabilities` turns required unknowns into errors. Known score-only explanation incompatibilities fail regardless of a conflicting supplied profile.

For explicit generation controls, preflight rejects known unsupported controls,
out-of-range values, incompatible combinations and mismatched capability scope.
Unknown generation support produces field-specific diagnostics; no-generation
runs retain their prior diagnostics and strict-mode behavior. The foundation
supplies no cloud capability catalog, adapter mapping or successful qualification.

`plan_run(spec, *, capabilities=None)` returns reusable ordered trait groups and selected exemplar IDs, raising `PreflightError` for errors. It does not scan all response records. `iter_tasks(spec, records=None)` and `iter_requests(spec, records=None, *, capabilities=None)` lazily read configured inputs or consume supplied `ResponseItem`/`PairTask` iterables. Task IDs are invariant across grouping. A request contains ordered tasks, trait IDs, exemplar IDs, and its source response or pair reference. Input readers retain seen IDs plus current text; the Cartesian task product is never stored.

`prepare_pairs(pairs, *, repetitions=1, include_reversals=False, seed=None)` yields scheduled `PairTask` occurrences in source/repetition order, placing a reversal immediately after its base occurrence. Repetition indices are zero-based. The helper accepts unscheduled source occurrences only; it rejects existing reversal/repetition metadata instead of overwriting it. Explicit already-scheduled occurrences can be passed directly to task/request iteration. IDs hash schema-versioned canonical JSON encoded as UTF-8, with sorted object keys and compact separators. Seeded orientation uses the parity of the SHA-256 digest's final hexadecimal digit for `[1,"orientation",seed,source_pair_uid,pair_id,repetition_index]`: even preserves original orientation, odd reverses it. Reversal is always the exact opposite. Presentation changes leave logical occurrence/task identity unchanged; request identity includes presentation.

Request IDs identify the structural membership and configured context. Pairwise plans retain references rather than reading response text, so changing a referenced text alone does not change the structural request ID. Exact input/payload hashes and submission idempotence belong to Milestone 2.

Generation controls leave item/pair/task IDs unchanged and participate in
request context identity only when explicitly requested. No-generation
configurations preserve their Milestone 1 request IDs. Separate immutable
generation commitments record reproducibility settings. The internal
`judgmentllm.persistence.generation` helpers build and compare that manifest
section and revalidate current capability restrictions. Their section-only
checks are composed with source verification by the preparation service; see the
[persistence contracts](persistence-contracts.md#generation-manifest-section).

Plans contain no provider request bodies, token/cost estimates, remote submissions, or persistent execution state. Pairwise response lookup is deferred to rendering. Direct rendering accepts resolved `left`/`right` responses, allowing callers to use their own lookup; file-based preview scans and retains only the selected responses.

## Immutable source preparation (internal)

`judgmentllm.preparation` provides the provider-free internal services used by
the public run/CLI lifecycle. `prepare_run(spec_or_path, *, store, run_id,
capabilities=None, exhaustive_rendering=False)` validates all inputs, consistently freezes every applicable
source, builds byte-accurate CSV/JSONL indexes, and seals a complete executable
manifest. A direct `RunSpec` receives a validated canonical identity without an
invented originating configuration-file hash.

Source and record validation always covers the complete input set before sealing.
Normal preflight compiles the full assessment/grouping/template configuration
with the shared syntax, sandbox, unknown-variable, and rendering-setting checks,
including empty inputs. It then renders the first verified real response for
each rubric group, or the first real indexed pair for each represented criterion,
preserving actual IDs and presentation. Empty or unrepresented contexts receive
static checks without evaluating hypothetical content branches. Preparation does
not render every real response-by-trait combination. Content-specific output
limits or conditional inclusion failures in later records are checked when each
actual request is selected.

The internal executor calls `run.render(dependency)` on each selected verified
dependency before submission. This uses the full renderer and validates the real
response texts, trait group, exemplars, exact output limits, and protected blocks.
`iter_request_dependencies()` verifies content dependencies without implying that
the request has rendered successfully.

For an explicit diagnostic scan, `prepare_run(..., exhaustive_rendering=True)`
additionally renders every actual request before sealing.
`validate_inputs(spec_or_path, *, capabilities=None, exhaustive_rendering=False)`
uses the same checks in a temporary local store and returns typed validity/counts;
set its flag to request the same exhaustive render scan. The option changes
validation work only, not frozen configuration, manifest compatibility, request
IDs, or generation commitments. Neither path retains rendered prompts under
minimal capture.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from judgmentllm.preparation import prepare_run, open_prepared_run

# Runtime state belongs on a local filesystem, outside synchronized folders.
with TemporaryDirectory(prefix="judgmentllm-runtime-") as directory:
    store = Path(directory) / "store"
    with prepare_run("examples/run.yaml", store=store, run_id="offline-001") as run:
        assert run.get_response("essay_001").item_id == "essay_001"
        manifest_sha256 = run.manifest_sha256
    with open_prepared_run("offline-001", store=store) as reopened:
        assert reopened.manifest_sha256 == manifest_sha256
```

`PreparedRun` is a context manager; close it when finished. It exposes the typed
`.manifest`, `.manifest_sha256`, `.repository`, `get_response(item_id)`,
`get_pair(pair_uid)`, `iter_responses()`, `iter_pairs()`, and
`iter_request_dependencies()`, and `render(dependency)`. Traversal follows original source order. Selected
records are reparsed through the shared strict reader, and their IDs and canonical
hashes are verified before use. Pairwise lookups resolve both referenced texts
through the index; dependencies include both content identities while preserving
original and presented orientation. Request traversal is incremental and does
not materialize the response-by-trait product.

`open_prepared_run(run_id, *, store, spec=None, capabilities=None,
use_frozen_snapshot=False)` works in an independent process. It verifies the
manifest, sources, semantic configuration, generation settings, and current
capability restrictions. Changed generation settings or newly known incompatible
capabilities block further preparation without rewriting original evidence.
Matching generation settings alone do not authorize continuation.

Full capture retains exact verified snapshots. Changed or missing originals block
reopening by default; explicit `use_frozen_snapshot=True` authorizes the original
verified snapshots and records that authorization in the audit trail. Missing or
corrupt required snapshots still fail. Minimal capture retains no assessment text,
full input documents, rendered prompts, or raw payloads in runtime storage; all
original sources must remain available and unchanged. Hashes cannot reconstruct
them. A direct Python specification must be supplied again when its text was not
retained.

These services build content dependencies, not adapter request bodies. Subphase 3
must journal exact rendered prompts and actual adapter-serialized payload hashes
before submission; a configuration or dependency digest cannot substitute for
either exact-byte hash.

## Streaming input/output and preview

`judgmentllm.io` exposes:

- `read_responses(source, *, allow_empty_text=None)` and `read_pairs(source)` for an `InputSpec`, path string, or `Path`.
- `read_jsonl(path, model)` / `write_jsonl(path, records)` for typed models.
- `read_results(path)` / `write_results_jsonl(path, records)` for normalized mixed result streams.
- `export_results_csv(path, records)` / `read_results_csv(path)` for lossless normalized CSV.

Writers return record counts. Callers create output directories; errors may leave partial files. These utilities do not implement transactional persistence. Readers are lazy and raise `InputError` when an invalid record is encountered; consume the stream to validate the complete input. Duplicate detection retains identifiers, not complete records. CSV readers raise the process-wide stdlib field-size ceiling to the largest platform-supported value so large valid text and probability cells are not rejected by its default 128 KiB limit; the setting is never decreased. Nested CSV cells contain JSON, null is `\N`, and literal leading backslashes are escaped with one additional backslash. Empty strings remain empty cells.

`preview_prompt(spec_or_config_path, *, item_id=None, trait_id=None, pair_uid=None, capabilities=None)` returns historical `RenderedPrompt` for v1 or typed `RenderedPromptV2` for v2. Both include `text`, `exemplar_ids` and `output_schema`; v2 additionally includes `renderer_version` and the exact `output_commitment` with contract, selection and ordered schema evidence. Rubric preview requires an item ID; select a trait when there are multiple request groups. Selecting a trait in a grouped run renders its complete group. Pairwise preview requires its occurrence ID. No output is sent anywhere.

See [prompt guidance](prompt-guidance.md) for `render_prompt`, template APIs, context, sandbox restrictions, and rendering limits. Run `uv run --offline --no-sync python examples/offline.py` for both preparation workflows.

## Export revalidation and R interoperability

Library writers revalidate current complete nested record data immediately before serialization. This includes generic `write_jsonl`, normalized JSONL, canonical CSV, and R views. They reconstruct from a field-name snapshot rather than trust `model_validate(existing_instance)`, reject invalid copies/mutations and validator-induced repairs, and retain only one current record. Direct Pydantic `.model_dump*()` calls are not protected export boundaries.

`judgmentllm.r_exports` and the top-level package expose:

- `export_pairwise_r_csv(path, records, *, na_string="NA") -> int`
- `export_rubric_r_csv(path, records, *, na_string="NA") -> int`

These functions export all supplied attempts without deduplication or status filtering. Missing values use the selected marker, with literal collisions rejected; booleans are `TRUE/FALSE` and nested columns contain JSON. Read IDs as character strings. Use explicit successful-attempt selection before analysis. See [R interoperability](r-interoperability.md) for full columns, source-qualified pairwiseLLM behavior, representative CSV, and base R imports.

OpenAI Decisions preflight enforces its documented 2–10 ordered score levels even if a supplied capability profile is wider. Rubric storage remains unrestricted. Other endpoints receive no inferred Decisions limit; independently verified provider constraints can be supplied through `CapabilityProfile`.

## Mock submission and recovery (internal, Subphase 3)

`judgmentllm.providers.ProviderAdapter` separates capability discovery/validation,
exact-byte preparation, submission, authoritative reconciliation/retrieval, and
transport parsing. `PreparedRequest` is an in-memory strict record; its actual
serialized bytes are the payload commitment. It must never be serialized into
minimal-capture persistence. The mock defines its own versioned canonical JSON
protocol; it does not imitate a cloud endpoint.

`MockAdapter(ledger_path, *, scenario=None, scenarios=None,
retrieval_available=True, reconciliation_available=True)` supports provider
`mock`, endpoint `synthetic`, and arbitrary model IDs. Its live synthetic service
supports explanations, ties with compatible custom templates, the three existing
generation controls, and cache `off`/`auto` (uncached). Explicit cache, tier,
managed bulk/native batch, and real providers fail before submission.
`MockScenario` selects synthetic judgments, distributions, delays, refusals,
invalid outputs, provider errors, and transport failures. Scenario text stays in
memory; recovery needs the same fixture configuration. Lost fixture configuration
cannot be reconstructed from a hash.

```python
import asyncio
from judgmentllm.preparation import prepare_run
from judgmentllm.providers import MockAdapter, MockScenario
from judgmentllm.execution import execute_run, recover_run

adapter = MockAdapter(
    "/local/runtime/mock-service.sqlite3", scenario=MockScenario(probabilities=True)
)
# spec must declare provider="mock", endpoint="synthetic".
with prepare_run(
    spec,
    store="/local/runtime/store",
    run_id="001",
    capabilities=adapter.capabilities(spec),
):
    pass
report = asyncio.run(
    execute_run("001", store="/local/runtime/store", adapter=adapter, spec=spec)
)
# Reopening reconciles original submissions and resumes durable retry authorizations.
report = asyncio.run(
    recover_run("001", store="/local/runtime/store", adapter=adapter, spec=spec)
)
```

Both async internal services accept `use_frozen_snapshot=False` and a content-free
`fault(stage, request_uid)` injection hook. They acquire exclusive lifetime run
ownership before source verification and release it after durable processing.
`ExecutionReport` contains the existing summary, `recovery_required`, fixed worker
count, and request/result queue high-water marks. Minimal Python-origin runs
require their original `spec`; file-origin runs can reconstruct verified sources.
Full-capture changed originals need explicit snapshot authorization on each call.

Rubric requests execute independently, in fixed groups, or with capability-limited
auto grouping. Pairwise requests remain independent. The internal interpreter
matches exact trait IDs and preserves valid siblings. Every expected member gets
one complete or explicitly unsuccessful outcome per received observation; all
members and their lifecycle changes commit together. The public whole-group
validator is unchanged. Unknown acceptance and unavailable observations remain
unresolved; matching hashes and deterministic recipes never justify new outcomes.

The following Subphase 4 interfaces are internal and provider-free during
preparation/export; public lifecycle names and CLI remain Subphase 5 work:

```python
from judgmentllm.execution.retry import authorize_subset_retry, RetryPolicy
from judgmentllm.execution.cancellation import cancel_run
from judgmentllm.execution.exports import export_run

# task_ids must be explicitly selected from one original request.
attempt = authorize_subset_retry(
    "001",
    store=store,
    adapter=adapter,
    spec=spec,
    task_ids=(failed_task_id,),
    operation_id="retry-001",
    allow_completed=False,
)
# Actually submit the authorized subset through the existing bounded executor.
report = asyncio.run(recover_run("001", store=store, adapter=adapter, spec=spec))
artifact = export_run("001", store=store, format="csv", view="r")
```

`authorize_subset_retry()` takes a required stable operation ID, selected task IDs,
`allow_completed=False`, optional `RetryPolicy`, and the existing source/snapshot
arguments. Selection, original prior-outcome basis, policy and completed permission
are frozen under ownership. An identical operation resumes its original attempt;
a conflicting reuse fails. Parent task/request identities and original membership
are retained; only physical attempt membership changes. The helper calls adapter
validation/serialization but never submission, reconciliation or retrieval.

Both executor services accept `retry_policy=RetryPolicy()`: at most three physical
attempts, exponential 1/2-second delays, 30-second cap, optional full jitter and
authoritative retry-after. Only typed transient definitive nonacceptance permits
automatic retry. Eligibility and authorization persist before preparation; caller
must retain the same policy when recovering. Invalid/refused/received outputs,
uncertain submissions and unavailable accepted observations never auto-retry.
These optional internal controls do not change RunSpec or canonical request IDs.
No timeout or rate setting is accepted without an implemented enforcement boundary.

`cancel_run(run_id, *, store)` acquires idle-run ownership. A live caller cancels
the `execute_run` coroutine; cleanup drains the dedicated I/O thread and service
acceptance writes before releasing ownership. Pre-submission work creates no
result. Remote uncertainty/acceptance and committed observations survive, and
recovery can collect their original outcomes while the run stays cancelled.
Only actual confirmed provider cancellation evidence produces cancelled results.

`export_run()` supports canonical JSONL/CSV and R CSV, optional explicit
`high_water_mark`, bounded `page_size`, and the existing R `na_string`. It returns
an `ExportReport` identifying the run/export, selected high-water mark, row count,
exact artifact hash/length/absolute path and current completeness. Repeated equal
snapshots/options converge; publication is atomic and prior files immutable.
All recorded attempts survive export, including earlier failures after retries.
Export does not require an adapter and never generates missing judgments.

`ExecutionSummary` additionally distinguishes `logical_completed_tasks`,
`unsuccessful_results` and `retry_operations` from physical `attempts`, historical
`successful_results` and `unresolved_submissions`. A partially valid request has
state `partial`. Authorized normalized explanations remain exportable under
minimal capture; prompts, payloads and raw output remain unavailable.

## Milestone 3 credential diagnostics and future provider APIs

The [assessment output design](assessment-output-design.md) is authoritative.
Phase 3 implements credential diagnostics below. Phase 4A implements standalone
evidence interfaces; Phase 4B integrates durable capture and recovery. Live
verification and explicitly proposed export APIs remain future work. Existing
normalized results and RunSpec v1/v2 contracts remain the compatibility baseline.

### Credential diagnostics (Phase 3, implemented)

```python
from judgmentllm.auth import CredentialStatus, credential_status

statuses: tuple[CredentialStatus, ...] = credential_status()
for status in statuses:
    print(status.provider, status.status)

openai_status = credential_status(providers=("openai",))
```

`credential_status(*, providers=None) -> tuple[CredentialStatus, ...]` is a
synchronous offline service. Omitted selection returns the documented provider
registry in stable order; an explicit provider filter retains that registry
order, rejects unknown/malformed IDs without echoing them and deduplicates repeated
IDs. Scalar strings and nonstring members are rejected. An explicitly empty
sequence returns `()` without inspecting any credential source; it differs from
`None`. These names are also exported from `judgmentllm`. Return frozen strict models matching existing public
control-model conventions, with `provider`, `auth_type`, nullable `source`,
`status` and nullable `diagnostic_code`. Status values are `configured`, `missing`,
`unknown`, `not_required`, `unsupported`; diagnostics use allowlisted codes.
Cross-field validation binds the provider, authentication type, selected source,
status and code: a configured API-key source must belong to that provider;
missing/rejected keys have a null source. Vertex is always
`application_default_credentials/google_adc/unknown/offline_identity_unknown`;
ordinary local Ollama is `none/local_service/not_required` with a null code.

Absent, empty and whitespace-only keys produce `missing_api_key`. Nonblank keys
containing Unicode whitespace, control/format characters or surrogates produce
`malformed_api_key`; no value is normalized. Gemini selects
`GOOGLE_API_KEY` before `GEMINI_API_KEY`, skipping blanks but never falling back
after a malformed nonblank preferred value. `configured` has a null code. Unavailable environment inspection uses the fixed `credential_source_unavailable`
code. The
remaining allowlisted code, `unsupported_authentication_context`, describes a
known unavailable context, never an absent SDK. No initial default provider
reports unsupported.

`CredentialStatus` constructor, class-owned parsing/validation, copy, and frozen
assignment failures use fixed messages and exclude input values and validator
context from rendered and structured Pydantic errors. Third-party parsers retain
their own error contracts: for example, malformed JSON decoded by an external
`TypeAdapter` fails before model validation. Credential material is never passed
to those parsers by the resolver or status service. Deprecated `parse_raw`,
`parse_file` and `copy` methods reject with a fixed error; use
`model_validate_json` and validated `model_copy` instead. No status parser reads
credential files.
Never return secret values, substrings, hashes, identity/account names, arbitrary
file paths or unsanitized SDK messages. The CLI wraps these same records under
version-1 `data.providers`; `--require` is a CLI policy check over status records,
not another credential resolver.

The shared internal resolver keeps secret material transient in a private,
nonserializable handle with safe representation. Future adapters must explicitly
request that handle and pass the unchanged selected key to the SDK; no client
is constructed here. ADC returns a deferred SDK-authentication marker and local
Ollama a distinct no-auth marker. Its offline inspection path cannot
refresh tokens, contact metadata/provider endpoints, load arbitrary credential
files or implicitly load `.env` files. Google cloud identities can remain
`unknown`. A configured source is not authenticated; local Ollama requiring no
ordinary authentication is not evidence of daemon connectivity.

Provider-specific live verification and smoke services must share that resolver
and the safe `LifecycleError` boundary. Their thin CLI contracts are documented
in [CLI design](cli-spec.md#milestone-3-authentication-and-future-commands).
Explicit authentication verification is never called by status or planning.
No suitable non-generative operation means verification unsupported; model
requests require the separately authorized smoke workflow.

### Output, usage and reasoning boundaries

Implemented output controls opt into RunSpec schema version 2:
`PromptSpecV2.output_order` defaults to `together` and accepts `scores_first` and
`explanations_first`; `PromptSpecV2.structured_output` defaults to `auto` and accepts
`native` or `text`. Typed reasoning remains later work; Phase 2 rejects reasoning
settings rather than ignoring them. `ExecutionSpec` continues
to own grouping and assessment explanation requests; capture policy owns detailed
evidence authorization. Do not add authentication secrets to these models.

Phase 1 supplies standalone generated schemas and local validators. Phase 2
integrates them with versioned rendering, preparation and mock execution;
existing version-1 behavior retains its historical contracts.
Separated arrays join by `trait_id`, never position, and grouped interpretation
continues to preserve valid siblings while recording every failed expected
member. All layouts normalize to existing `RubricResult` fields. Generative
forced-choice output may omit `tie`; normalized `PairwiseResult.tie` remains false,
while historical explicit `tie: false` inputs remain valid. Native decision/score
contracts remain separate Milestone 4 work.

### Standalone evidence (Phase 4A, implemented)

Import public interfaces from `judgmentllm.evidence`. Every evidence record is
strict, frozen and independently versioned with `schema_version: 1`. Unknown
fields, boolean token counters and incompatible semantic combinations fail with
fixed safe validation errors; raw invalid values/keys are excluded from error
strings, representations, `errors()` and JSON forms. `model_copy()` and
`model_construct()` preserve validation. These APIs perform no I/O, credential
inspection, SDK construction or provider submission and are integrated by the Phase 4B execution boundary. Extension and privacy requirements are documented in
[evidence privacy guidance](evidence-privacy.md).

`EvidenceIdentity(run_id, request_uid, attempt_id, submission_id,
observation_id=None)` uses the existing identity type. Supply these fields from
trusted existing records, never from arbitrary model text. Observation order and
reasoning require an established observation; usage permits no observation.
There is no task/trait field on request-level usage or reasoning evidence.

Usage interfaces:

```python
from judgmentllm.evidence import (
    EvidenceIdentity,
    UsageContext,
    ReportedUsageInput,
    extract_provider_usage,
)

identity = EvidenceIdentity(
    run_id="001",
    request_uid="request-001",
    attempt_id="attempt-001",
    submission_id="submission-001",
    observation_id="observation-001",
)
context = UsageContext(
    provider="mock",
    endpoint="synthetic",
    requested_model="synthetic-v1",
    observation_kind="response",
)
usage = extract_provider_usage(
    identity,
    context,
    ReportedUsageInput(usage_source="synthetic.reported_usage", input_tokens=0),
)
assert usage.input_tokens == 0 and usage.total_tokens is None
```

- `extract_provider_usage(identity, context, reported, *, estimated_cost=None)
  -> ProviderUsage` copies explicit typed facts; `reported=None` means absent usage,
  with null counters and unknown source. Repeated extraction never aggregates.
- `extract_openai_chat_usage(identity, context, usage: OpenAIChatUsageInput | None)
  -> ProviderUsage` accepts only scoped `openai` / `chat.completions` usage. Input
  types `OpenAIInputTokenDetails` and `OpenAIOutputTokenDetails` preserve cached
  prompt/reasoning completion counters as included in input/output respectively.
- `extract_anthropic_messages_usage(identity, context,
  usage: AnthropicMessagesUsageInput | None) -> ProviderUsage` scopes to
  `anthropic` / `messages`. Input excludes separately reported cache reads/writes;
  `AnthropicCacheCreationInput` retains 5-minute/1-hour write subdivisions.

`UsageContext` contains requested/reported model, SDK version, observation and
invocation kind, nullable remote/failure references, optional timezone-aware
start/end timestamps and finite nonnegative duration with an explicit clock
source. `ProviderUsage` additionally holds six nullable strict nonnegative integer
counters, `usage_source`, `semantics_version=1`, a tuple of `UsageBreakdown`, and
separate nullable `ReportedCost` / `EstimatedCost`. Breakdown fields, units,
relationships and related counters are allowlisted and source-validated.
Missing totals stay null; no nested counters are added and no totals are checked
against an invented universal equation. Known included components cannot exceed
a reported parent, and breakdown values must agree with their mapped counters.
Unestablished source counters are rejected: OpenAI cache writes and Anthropic
total/reasoning counts have no mapping in this phase. No real-provider cost mapping is enabled:
`ReportedCost` currently admits only explicit synthetic reported-cost semantics,
and `ProviderUsage` rejects such costs on every real-provider identity. The
production ingestion boundary also rejects synthetic fixture provenance for real
providers. Actual unsupported costs remain null.
`EstimatedCost` requires an explicit amount/currency, pricing date/source and
allowlisted assumptions and is never inferred from token counts. A definitive
failure can have usage without a fabricated provider observation. Consumers must
select one fact per physical submission before any grouped-trait join. Phase 4B
provides append-only revision selection and audit; public aggregation/export
services remain deferred. When both disjoint Anthropic 5-minute/1-hour cache-write
subdivisions and the parent are reported, their sum must not exceed the parent.
Equality is not required; missing subdivisions or parents remain valid partial
evidence.

Order extraction:

```text
extract_observation_metadata(
    commitment: OutputCommitmentV2, *, identity: EvidenceIdentity,
    output: str | bytes | None,
    evidence_source: OrderEvidence,
    unavailable_reason: OrderUnknownReason | None = None,
) -> ProviderObservationMetadata
```

Pass exact original emitted assessment text, not an adapter dictionary or
reserialized SDK object. `raw_tokens` / `reassembled_stream` mean the supplied
text is a complete trustworthy decoding/reassembly. An unavailable source yields
unknown; use `sdk_object_only` where applicable. The extractor reads committed
root/member property sequences, reuses the existing duplicate-rejecting JSON
decoder, and bounds input to 1,048,576 UTF-8 bytes, nesting to 64 and member
occurrences to 4,096. Oversize/depth/member limits yield `incomplete_evidence`;
malformed/truncated/duplicate-key JSON yields `malformed_output`, without exception
text. It returns no response text and creates no file.

`ProviderObservationMetadata` contains `requested_order` (null for pairwise),
`observed_key_order`, `member_key_orders: tuple[ObservedMemberOrder, ...]`,
`order_adherence`, `order_evidence` and `unknown_reason`. Each member has `array`,
zero-based `index`, nullable known `trait_id` and nullable `keys`. Array indices
locate occurrences; differing trait-array order is irrelevant. Inspect both root
and member order for separated layouts and score-object order for `together`.
Verified differences are `mismatch`, complete matches `adherent`, and unavailable
or incomplete evidence `unknown`. Pairwise is `unknown` / `not_applicable`.
Allowed unknown codes are `not_applicable`, `not_returned`, `malformed_output`,
`incomplete_evidence`, `sdk_object_only` and `unsafe_key_names`.

Only contract-authorized key names and known trait IDs survive extraction. Unsafe
key sequences become null and force `unknown` / `unsafe_key_names`; unknown trait
IDs become null. This applies to dumps/JSON under full and minimal policies.
Mismatch is diagnostic: it neither invalidates a score nor authorizes retries.
This service makes no internal-reasoning or provider enforcement claim.

Reasoning interfaces:

- `ReasoningAuthorization(capture="full"|"minimal", effort_requested=False,
  summary_requested=False, retention="none"|"summary"|"provider_text")` keeps
  computation, summary exposure and text retention separate. Minimal capture
  with a non-none retention grant is invalid. Full alone grants nothing.
- `authorize_reasoning(policy, kind) -> ReasoningRetentionDecision` returns
  `retain_text`, `request_summary` and a fixed diagnostic. A summary grant covers
  only documented `summary`; provider-text covers only `thinking_block`.
  `other_documented` has no retention grant in this phase. Summary requests and
  effort requests never authorize retention by themselves.
- `extract_reasoning_metadata(identity, blocks: Iterable[ReasoningBlockMetadata],
  *, authorization) -> Iterator[ReasoningEvidence]` validates strictly increasing
  block ordinals incrementally. No text field is accepted. No blocks yields no
  fabricated empty text/record; even authorized blocks are `not_retained` because
  this phase writes no artifacts. Explicit redacted headers yield `redacted`.

`ReasoningEvidence` holds observation identities, `kind`, allowlisted
`provider_field`, `block_ordinal`, nullable `artifact`, nullable
`source_bytes_sha256`, and `availability`:
`retained|not_exposed|not_returned|not_retained|redacted`. A retained record needs
`ReasoningArtifactReference(sha256, byte_length)` supplied by a future retention
layer; references contain no paths or text. Original-byte hashes are supplied
only when actually established. Provider field/kind combinations are validated.
Assessment explanations remain in existing result rows, with no per-trait
reasoning attribution and no missing-reasoning assessment failure.

Scoped provenance:

`ScopedProviderEvidence` composes optional existing `GenerationCapabilities` with
provider/endpoint, exact or explicitly unresolved model scope, paired optional
SDK package/version, nullable API version, feature
`usage|property_order|reasoning|native_schema|generation`, nullable `supported`,
tri-state `implemented`/`fixture_tested`/`live_qualified` and a consistent
`stage: discovered|implemented|fixture_tested|live_qualified`.
`EvidenceProvenance(kind, source_sha256, recorded_on)` records documentation,
offline-fixture or independent-live origin without URLs, accounts or paths.
Fixture success requires implementation and fixture provenance. Live qualification
requires independent live provenance, exact model/SDK scope, fixture success and
explicit feature support. Documentation or synthetic fixtures cannot claim it;
no endpoint/model capability is registered or transport selected by these records.
Composed generation facts must match scope and qualification flags exactly.

Phase 4B adds authorized retention/redaction, receipt/recovery links and
append-only evidence revisions through the
[existing execution service](persistence-contracts.md#phase-4b-durable-evidence-integration).
New stores use registry schema 2; genuine historical schema-1 stores are opened
without migration or fabricated evidence. Historical acknowledgment/response
receipt formats, RunSpec v1/v2, mock protocol bytes, normalized results, CLI
envelope v1 and canonical/R assessment exports remain unchanged.
Phase 4C2 additional translators and 4C3 durable integration, Phase 5 real adapters, Phase 6 companion
exports/R joins and Phase 7 authorized live qualification remain deferred.

### Proposed evidence export services

Expose these typed services from `judgmentllm.lifecycle` and the top-level package,
following the existing synchronous result-export convention:

```text
export_run_usage(run_id, *, store=None, format="jsonl", view="canonical",
                 na_string="NA", high_water_mark=None,
                 evidence_high_water_mark=None) -> EvidenceManifest
export_run_reasoning(run_id, *, store=None, format="jsonl", view="canonical",
                     na_string="NA", high_water_mark=None,
                     evidence_high_water_mark=None) -> EvidenceManifest
```

Arguments follow `export_run_results`: run ID or absolute run directory, optional
store, `format="jsonl"|"csv"`, `view="canonical"|"r"`, configurable R missing marker,
and optional nonnegative result high-water mark. The R view requires CSV;
canonical CSV retains existing null escaping and nested JSON. The new optional
`evidence_high_water_mark` selects an append-only evidence revision cut.
Omitted cuts are captured in one database snapshot. Validate explicit boundaries;
reject future cuts and a result/evidence combination that excludes evidence links
required by the selected results. Usage facts with no normalized result remain
included through the evidence cut. Do not infer an evidence cut solely from the
largest assessment result ID.

`EvidenceManifest` is a new frozen version-1 export control model. It contains
`run_id`, `run_status`, `export_id`, `kind: usage|reasoning`, `format`, `view`,
`row_count`, `result_high_water_mark`, `evidence_high_water_mark`,
`incomplete_source`, immutable `files` (existing `ExportFile` path/hash/length
records), nullable `bridge_file`, nullable `observation_file`, and existing
`ArtifactPaths`. Usage exports publish revision-selected facts, revision audit,
the task-to-submission/observation/usage bridge, and the observation metadata
companion regardless of whether usage counts were reported. `bridge_file` and
`observation_file` use existing `ExportFile` path/hash/length models to identify
those artifacts. Missing legacy evidence can leave `observation_file` null;
do not infer order. Reasoning-only exports set `bridge_file` null and retain
observation identifiers in rows.
Assessment `ResultManifest` and normalized-result schemas stay unchanged.

Both services traverse bounded pages and atomically publish immutable outputs.
Repeated matching cuts/options converge. They read persisted evidence without
provider construction, network activity or original assessment sources. Reasoning
exports include only the authorized retained type and explicit availability/null
metadata for unavailable/redacted records. No text is emitted to control stdout.
CLI `usage export` and `reasoning export` call these services and serialize the
same safe metadata inside their existing version-1 control envelopes.

No proposed API in this section authorizes a live call during ordinary tests.
Offline synthetic adapter/CLI exports must pass the standing R/jsonlite gates;
Milestone 6 exposes the implemented services through CLI-first `rubricLLM` without
creating an R authentication or assessment engine.

### Internal durable evidence inspection (Phase 4B)

The existing `Repository` / `ReadOnlyRepository` expose
`get_evidence_for_submission(run_id, submission_id)`,
`get_evidence_for_observation(run_id, observation_id)` and bounded
`iter_evidence(run_id, page_size=100)`. Returned version-1 companion receipts
contain typed safe metadata and protected artifact references, never reasoning
text. Historical schema-1 stores return unavailable evidence and remain unchanged.
`usage_high_water_mark`, `usage_revision_history` and `authoritative_usage` select
an auditable revision snapshot. Explicit owned `append_usage_revision` requires a
previously published version-1 revision receipt. These are internal Phase 6 seams;
no public companion export commands or R evidence engine are introduced.

Provider adapters may optionally attach `ProviderEvidence` to `ProviderResponse`.
It separates exact emitted text from transport bytes and provides typed reported
usage, documented reasoning boundaries and a credential-free sanitized derivative.
It adds no required protocol methods, scoring behavior or mock wire fields.
Transient text is excluded from routine serialization/representations. Unsupported
adapters retain unknown facts and fail closed on unseparable body retention.
Ordinary RunSpec v1/v2 authorizes no reasoning text. Only internal explicit
synthetic fixture authorizations can retain protected text in this phase.

`prepare_evidence()` is an internal pre-retention service, not a provider request.
It uses executor-supplied immutable identities/configuration, an optional v2
output commitment and the response's typed extension. `ProviderEvidence` carries
only typed usage, exact emitted assessment bytes, documented reasoning block
headers/text and a provider-defined safe derivative. No arbitrary provider metadata
dictionary is accepted. `ProviderResponse.raw` remains transient transport bytes,
excluded from dumps and representations; it cannot establish assessment key order.
`TransportNotSubmitted` and its transient subclass optionally carry typed reported
usage without implying acceptance or billing, and never create an observation.

Evidence readers verify receipt commitments and protected artifact bytes. A
matching receipt replay is idempotent; conflicting identities, metadata,
authorization or hashes raise content-free integrity failures. An omitted body
uses the legacy response receipt's empty identity plus an explicit companion
omission marker and a null result raw-response path. Safe metadata remains
available even when results cannot be recovered without authoritative retrieval;
missing bodies never authorize resubmission. Observation receipts marked omitted
must carry the zero-length, empty-byte SHA-256 sentinel under the matching
full/minimal capture policy; invocation failures have no response identity.
Initial usage readers also require the exact original companion-receipt digest
and fixed initial revision identity/source fields. Rebinding that digest to a
different existing registered artifact fails safely even when foreign keys are
valid, including recovery of already committed results. No public reasoning
reader or companion export is introduced in this phase.


## Internal native-schema foundations (Phase 4C1)

`judgmentllm.native_schema`, `judgmentllm.providers.openai_schema` and
`judgmentllm.native_selection` are internal standalone modules. Their version-1
projection/decision records accept existing typed assessment contracts and exact
nonsecret scope; no arbitrary schema or provider request body is public input.
They are not top-level package exports and are not integrated into any public
lifecycle operation, CLI, prompt renderer or durable manifest.

The pure OpenAI projector returns deterministic ordered native schema/format
fragments or structured incompatibility. Responses and Chat have separate wrapper
structures. Strict immutable records verify exact bytes/digests, source identity,
requested order, scoped implementation/profile provenance and the native/local
constraint partition. Validation errors remain content-free.

The standalone resolver keeps supported/unsupported/unknown model evidence
separate from registered implementation and trusted fixture scope. A descriptive
`ScopedProviderEvidence` record cannot activate translation by itself. Synthetic
fixtures exercise positive selection; no real model is qualified. Decision
verification validates the pinned choice without fallback or reselection.

The conservative profile preserves required closed typed objects, exact categories
and winner IDs, maps disjoint rubric alternatives, and leaves array bounds,
explanation lengths and relational checks explicitly local-only. Existing local
assessment validators remain authoritative. Later expansion requires versioned
profiles without changing historical identities. See the
[projection matrix](assessment-provider-evidence.md#phase-4c1-openai-projection-matrix).
`SchemaSelectionV2` and `OutputCommitmentV2` remain text-only; native execution
activation is deferred to 4C3 and subsequent adapter work.


## Internal multi-provider native APIs (Phase 4C2)

`judgmentllm.native_schema_v2` and `judgmentllm.native_selection_v2` provide separate
internal standalone APIs: `project_native_schema_v2`,
`resolve_native_transport_v2` and `verify_native_transport_decision_v2`.
They accept existing assessment contracts, exact `NativeProjectionScopeV1` scope
and an explicitly selected mechanism. Google schema routes cannot be selected
implicitly or exchanged during verification. V1 OpenAI APIs retain their types,
serialized bytes and identities; no automatic conversion is provided.

The V2 projection commits the canonical schema, native schema, SDK configuration
and converted format fragment separately. Route-owned wrappers accommodate
Anthropic's unnamed format, Google's two interfaces, Together's named format and
Ollama's schema-valued format. Constraint records distinguish represented content,
SDK retention, documented endpoint support and independent enforcement. Exact
schemas carry sensitive IDs/labels and remain excluded from representations and
unrelated diagnostics.

`native`, `auto`, strict `auto` and `text` retain their established policy meanings.
The production catalog contains reviewed translator implementations but no positive
real-model acceptance profiles. Caller-supplied `ScopedProviderEvidence` cannot
supply trust. Synthetic profiles exercise supported decisions offline; live
qualification remains unknown. Explicit text selection bypasses translation;
verification checks the original frozen decision without fallback.

These modules are not top-level public exports and are not imported by ordinary
RunSpec rendering, preview, preparation, execution, recovery or persistence.
No arbitrary provider schema/request JSON, SDK construction or credential lookup
is introduced. Future 4C3 integration must commit exact prompts and payloads before
submission. Together/Ollama schema repetition remains documented prompt guidance.

V2 decisions also commit the supplied metadata-only scoped evidence digest.
Verification compares this snapshot and the exact pinned projection/profile
directly; it does not call the resolver or translate explicit text selections.
Implementation safety budgets are distinct from provider claims: at most 256
traits, 10,000 enum entries and 1,000,000 source string characters before
expansion; each emitted fragment is at most 1,000,000 UTF-8 bytes, 32 generic JSON
levels and 256 alternative branches. Excess produces a controlled failure without
truncation. These limits confer no model acceptance.


## Internal durable native interfaces (Phase 4C3)

Explicit `RunSpecV3` / `PromptSpecV3` add typed `prompt.native_mechanism`; the
public mock lifecycle remains unchanged and cannot execute real providers.
`judgmentllm.transport` contains the independently versioned native-capable seam:

```text
TransportContext(scope, evidence=None, openai_catalog=None, provider_catalog=None,
                 service_identity=None, fixture_sha256=None)
build_output_commitment_v3(spec, request, context, *, inherited_commitment=None)
physical_identity(run_id, request_uid, attempt_id)
prepare_transport_request(spec, dependency, identity, context, *, template=None,
                          lookup=None, frozen=None, parent=None,
                          committed_output=None)
```

The context is runtime authority, supplied explicitly again on reopening; it
contains no credentials and is not reconstructed as trust from persisted metadata.
Default catalogs contain no positive real-model profile. The actual synthetic
preparation requires a private exact-scope synthetic profile. Descriptive evidence
alone does not authorize a native mechanism or a provider operation.

`OutputCommitmentV3`, `FrozenTransportSelectionV1`, `PhysicalRequestIdentityV1`,
`TransportPlanV1`, `TransportCommitmentV1` and `PreparedRequestV3` have independent
strict versions. PreparedRequest 3 binds real logical membership and exact
in-memory synthetic payload bytes. `content_artifacts()` exposes content to the
existing capture-policy publisher; never serialize the entire prepared request
into minimal metadata. `render_request_v3` in `native_prompts` shares the existing
sandbox/protected content implementation and produces `RenderedPromptV3`.

Internal `prepare_run` and `open_prepared_run` accept optional
`transport_context=` only for the explicit v3 path. `PreparedRun` provides
`transport_for_attempt(attempt_id)` and
`prepare_transport_attempt(dependency, identity, *, frozen=None, parent=None)`.
First attempts are fully committed before sealing. Recovery reconstructs pinned
representations and verifies them without auto selection. Retry subsets inherit
the original family/route and create new exact commitments. The synthetic fixture
adapter exposes the same context to existing execute/recover/retry services;
it is test-only, never a registered production provider. Phase 5 must supply
complete real endpoint builders and commit their final wire payload bytes.

## OpenAI production adapters (Milestone 3 Phase 5A)

Install the optional SDK with `pip install 'judgmentllm[openai]'` on Python 3.11
or later. Explicit `RunSpecV3` configurations select `provider="openai"` and
`endpoint="responses"` or `endpoint="chat.completions"`. The shared public
`validate_inputs`, `estimate_run`, `preview_prompt`, `run_live`/`arun_live`,
`resume_run`/`aresume_run`, status and export services use the existing durable
lifecycle. Only run/resume execute assessment requests and may incur charges.
Set `OPENAI_API_KEY` in the process environment; credentials are resolved only at
an explicit execution or online-diagnostic boundary. No `.env` is loaded.

Both endpoints use official asynchronous SDK calls, explicit `store=false`,
nonstreaming generation, bounded clients and zero SDK retries. Responses maps the
output bound to `max_output_tokens`; Chat maps it to `max_completion_tokens`.
Unspecified generation settings stay omitted. Model names never establish
parameter or native-schema support. Ordinary `native` requires reviewed exact
scope authority; `auto` uses native only when eligible and otherwise follows the
existing text/strict-capability policy. A rejected committed request never changes
its output mechanism or silently retries with different parameters.

The production transport commits the complete nonsecret SDK request body before
submission and verifies the actual outgoing HTTP bytes and selected bearer
credential before forwarding. SDK 3.42.1 internally inspects ambient custom-header
configuration; the adapter ignores and clears it, overrides its default header
construction, explicitly disables alternate credential options and verifies the
selected key at the HTTP boundary. Ambient headers cannot select credentials,
organization/project routing or alternate API hosts.

Full capture retains exact original response bytes only when the documented
envelope passes strict safe-field checks. Unknown extensions, reasoning items,
nonempty metadata, tool/audio payloads or annotations produce a labeled filtered
derivative retaining the original assessment string when eligible. A separate
protected assessment component can retain exact invalid output under full capture
without retaining unsafe envelope fields. Unauthorized reasoning text
is never retained. Minimal capture retains safe identifiers, hashes and evidence;
it does not retain provider bodies or student inputs. Provider usage has one record
per physical invocation, with requested/reported models separate, unknown counters
null, cache/reasoning overlap explicit and no invented prices. Safe
`usage.provider_outcome` distinguishes completion, refusal, content filtering,
output limits, missing/multiple/unsupported output, tools and provider failures.

### Read-only assessment diagnostics

```text
from judgmentllm.assessment_inspection import (
    AssessmentOutputInspection,
    inspect_assessment_output,
)

inspect_assessment_output(
    run_id, observation_id, *, store, include_text=False
) -> AssessmentOutputInspection
```

The service verifies an existing observation and companion receipt using the
read-only repository. A running store is inspected through a private, temporary
registry snapshot; protected text artifacts remain in the original run store.
A changing snapshot is rejected. It performs no provider operation and requires no
credentials. The default result contains `label` and nullable `diagnostic`.
`include_text=True` additionally reads exact UTF-8 bytes only for an existing
full-capture artifact. `output_text` is excluded from `repr`, `model_dump()` and
JSON serialization. Minimal capture and unavailable historical artifacts return
no text; no decoding repair, rescoring, recovery or new request occurs.

`AssessmentDiagnosticV1` lives in `judgmentllm.evidence.assessment`. It has its
own `diagnostic_version=1` inside the unchanged evidence schema 1. It binds
`identity`, endpoint, requested/reported model, remote response ID and provider
outcome to `component`, `parser_diagnostic`, `canonical_valid`, capture/retention,
and nullable artifact/hash/length. `component` records allowlisted source/type
metadata and extraction status without generated values. Recognized text has a
separate 1,048,576-byte cap. Full capture stores the entire exact component;
minimal capture records a hash-only artifact; withheld components have no content
hash or artifact. A diagnostic never supplies assessment or retry authority.
The canonical validators still reject invalid outputs without normalization or
repair. Existing normalized result/export contracts are unchanged.

### Recovery and provider diagnostics

A bounded in-memory handoff joins submission acknowledgment to response retrieval.
No remote retrieval is enabled with `store=false`. A new process can recover a
trustworthy retained full-capture response, but lost minimal-capture output or an
interruption before durable response receipt can remain unresolved. Resume never
issues replacement assessments to resolve uncertainty. An interruption does not
establish provider cancellation or nonacceptance.

Explicit non-generative diagnostics are available separately:

```python
from judgmentllm import discover_models, verify_authentication

verification = verify_authentication(provider="openai")
models = discover_models(provider="openai", model="EXACT_MODEL_ID")
```

Their asynchronous counterparts are `averify_authentication` and
`adiscover_models`. Each makes at most one protected `models.list` SDK request,
without pagination or retries, and returns sanitized typed records. Model listing
reports visibility only. Authentication acceptance, visibility, SDK fixture tests
and actual serving qualification remain distinct. Offline `credential_status`
continues to make no network requests. Live assessment qualification uses only the
separate authorized harness; ordinary CI never invokes real providers.

Production `estimate_run()` includes optional `transport_protocol` and
`selected_transports` counts derived from frozen receipts; historical plan output
omits these fields. These describe prepared intent, not serving acceptance.

Model-dependent explanation, grouped-output and tie support remains unknown in
generic capability profiles until reviewed exact-model/endpoint evidence exists.
Nonstrict validation reports unknowns as warnings; strict validation rejects them.
The generic explanation flag covers all rubric layouts and pairwise explanations,
so a record qualifying only one explanation layout does not set that broad flag.
Until a narrower public capability interface exists, strict runs can therefore
remain blocked by partial explanation qualification. Native transport selection
still checks the precise requested feature set. `modes=("live",)` describes the
implemented local execution path and makes no model-access or serving claim.

Strict OpenAI capability failures preserve specific `ValidationIssue` records in
`validate_inputs()` and CLI `validate --json` (validation exit2). Plan, preview,
and run reject before submission with safe setting names such as
`generation.temperature`; provider response text, model values and assessment
content never enter these diagnostics. Invalid capability intent is not reported
as a network or execution failure.


## Anthropic Messages production adapter (Milestone 3 Phase 5B)

The existing `validate_inputs`, `estimate_run`, `preview_prompt`, `arun_live`,
`run_live`, resume/status/result export and read-only assessment inspection
services accept explicit RunSpecV3 `provider="anthropic"`, `endpoint="messages"`,
and, for auto/native policies, native mechanism `anthropic.messages.output_config.format`. Their neutral
control/result contracts are unchanged; new internal manifest5, output/request5
and transport3 records preserve historical dispatch. See [the guide](anthropic.md).
`verify_authentication(provider="anthropic")` and `discover_models(provider=
"anthropic", model="EXACT_MODEL")`, with async counterparts, make one explicit
non-generative protected GET and return separate provider-specific typed records.
Default OpenAI calls retain their existing record classes. No diagnostic operation
runs during ordinary offline validation, preview or `credential_status`.
Native production support remains unqualified and requires reviewed exact scope.


## Gemini Developer production adapter (Milestone 3 Phase 5C)

The existing public lifecycle functions accept explicit `RunSpecV3` with provider
`gemini` and endpoint `models.generate_content`. `preview_prompt`, `validate_inputs`
and `estimate_run` stay offline. Execution uses the shared resolver, maintained
SDK2.34.0/v1beta, immutable source preparation and existing durable services.
`discover_models(provider="gemini")` / `verify_authentication(provider="gemini")`
and their async counterparts return the separate Gemini diagnostic records; each
explicit operation performs at most one non-generative models-list read.

Gemini manifest6/output6/request6/transport4 and diagnostic3 are additive internal
contracts. Existing result records, per-physical usage evidence readers, read-only
assessment inspection and JSONL/CSV/R exports retain their formats. No public
arbitrary SDK config or reasoning-retention interface is added. Details and
unqualified live scope are in the [Gemini guide](gemini.md).
