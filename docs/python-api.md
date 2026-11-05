# Public Python API, schema version 1

Milestone 1 supports offline preparation and normalization of supplied synthetic outputs.
Milestone 2 Subphase 2 adds provider-free immutable source preparation, indexed
input access, complete manifests, and generation compatibility validation.
All examples below run without credentials. Public assessment models, common
normalization functions, planning functions, and preview are also exported from
`judgmentllm`. Subphases 3–4 provide mock execution/recovery, grouped outcomes,
retry/cancellation and immutable exports. Subphase 5 exposes the shared public
lifecycle services below; the installed CLI calls these same functions.
Subphase 6 adds development benchmark tooling and executable examples around
these services. Exact-head scale and compatibility qualification is pending;
no additional public lifecycle interface or schema is introduced.

## Public execution lifecycle (Milestone 2 Subphase 5)

Runnable public examples are `examples/mock_lifecycle.py` (validation, planning,
grouped rubric execution, inspection and canonical/R exports) and
`examples/mock_recovery.py` (independent installed-CLI processes, partial outcomes,
explicit failed-member retries and immutable old exports). Pairwise execution
uses `examples/mock-pairwise.yaml`. See the [scale guide](scale-qualification.md)
for local storage and separate benchmark commands. These examples use synthetic
inputs and the mock adapter; real providers remain unimplemented.

The top-level `judgmentllm` package and `judgmentllm.lifecycle` expose:

```python
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

`spec_or_path` accepts a `RunSpec`, string path or `Path`. Synchronous execution
wrappers reject calls inside an active event loop before doing any I/O, with
instructions to call their async equivalent. Both execute in the calling process;
no background service continues after that process exits. Cancellation of async
execution drains durable writes and ownership before propagating `CancelledError`.
Coarse synchronous progress observers receive frozen `ProgressEvent` records with
`stage` (`started`, `prepared`, `finished`), durable `run_id` and absolute `store`.
Progress contains no prompt, assessment text, raw response or exception dump.

Only the exact combination `provider="mock"`, `endpoint="synthetic"` executes in
this release. Other provider identities fail with `unsupported_capability` before
mock construction or submission. Offline validation/planning continue to accept
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

`judgmentllm.config` exposes `InputSpec`, `ExecutionSpec`, `GenerationSpec`, `PromptSpec`, `RunSpec`, `ConfigurationError`, `load_run_spec(path)`, `load_rubric(path)`, and `read_document(path)`. Loaders accept strings or `Path` objects. Every referenced path resolves against the configuration containing it. Direct Python construction uses loaded model instances; path shorthand is accepted through model validation for input references.

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

`preview_prompt(spec_or_config_path, *, item_id=None, trait_id=None, pair_uid=None, capabilities=None)` returns `RenderedPrompt(text, exemplar_ids, output_schema)`. Rubric preview requires an item ID; select a trait when there are multiple request groups. Selecting a trait in a grouped run renders its complete group. Pairwise preview requires its occurrence ID. No output is sent anywhere.

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
