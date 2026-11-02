# Public Python API, schema version 1

Milestone 1 supports offline preparation and normalization of supplied synthetic outputs. All examples below run without credentials. Public assessment models, common normalization functions, planning functions, and preview are also exported from `judgmentllm`.

## Models and loading

`judgmentllm.models` exposes `ResponseItem`, `Level`, `ResolvedLevel`, `Trait`, `Rubric`, `Exemplar`, `PairwiseCriterion`, `PairTask`, and `validate_exemplars`. `Trait.numeric_values`, `resolved_levels`, and `coding_kind` expose resolved category coding.

`judgmentllm.config` exposes `InputSpec`, `ExecutionSpec`, `PromptSpec`, `RunSpec`, `ConfigurationError`, `load_run_spec(path)`, `load_rubric(path)`, and `read_document(path)`. Loaders accept strings or `Path` objects. Every referenced path resolves against the configuration containing it. Direct Python construction uses loaded model instances; path shorthand is accepted through model validation for input references.

```python
from judgmentllm import load_run_spec

spec = load_run_spec("examples/run.yaml")
assert spec.rubric is not None
assert spec.rubric.traits[1].numeric_values == [1.0, 3.0, 8.0]
```

Use Pydantic `model_dump(mode="json")`, `model_dump_json()`, `model_validate(...)`, `model_validate_json(...)`, and `model_json_schema()` for serialization/schema access. Path fields serialize to strings. Root documents and records default to schema version 1; unsupported explicit versions fail. No implicit migration exists. Metadata is the only intentionally extensible JSON object. Error diagnostics omit raw input values where practical.

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

`validate_run(spec, *, capabilities=None)` returns `ValidationReport.issues`; `.valid` is false when errors occur. `CapabilityProfile` contains optional `explanations`, `grouped`, `ties`, `modes`, `max_levels`, and `max_traits_per_request`. Unknown does not mean supported. Default unknowns warn; `strict_capabilities` turns required unknowns into errors. Known score-only explanation incompatibilities fail regardless of a conflicting supplied profile.

`plan_run(spec, *, capabilities=None)` returns reusable ordered trait groups and selected exemplar IDs, raising `PreflightError` for errors. It does not scan all response records. `iter_tasks(spec, records=None)` and `iter_requests(spec, records=None, *, capabilities=None)` lazily read configured inputs or consume supplied `ResponseItem`/`PairTask` iterables. Task IDs are invariant across grouping. A request contains ordered tasks, trait IDs, exemplar IDs, and its source response or pair reference. Input readers retain seen IDs plus current text; the Cartesian task product is never stored.

`prepare_pairs(pairs, *, repetitions=1, include_reversals=False, seed=None)` yields scheduled `PairTask` occurrences in source/repetition order, placing a reversal immediately after its base occurrence. Repetition indices are zero-based. The helper accepts unscheduled source occurrences only; it rejects existing reversal/repetition metadata instead of overwriting it. Explicit already-scheduled occurrences can be passed directly to task/request iteration. IDs hash schema-versioned canonical JSON encoded as UTF-8, with sorted object keys and compact separators. Seeded orientation uses the parity of the SHA-256 digest's final hexadecimal digit for `[1,"orientation",seed,source_pair_uid,pair_id,repetition_index]`: even preserves original orientation, odd reverses it. Reversal is always the exact opposite. Presentation changes leave logical occurrence/task identity unchanged; request identity includes presentation.

Request IDs identify the structural membership and configured context. Pairwise plans retain references rather than reading response text, so changing a referenced text alone does not change the structural request ID. Exact input/payload hashes and submission idempotence belong to Milestone 2.

Plans contain no provider request bodies, token/cost estimates, remote submissions, or persistent execution state. Pairwise response lookup is deferred to rendering. Direct rendering accepts resolved `left`/`right` responses, allowing callers to use their own lookup; file-based preview scans and retains only the selected responses.

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
