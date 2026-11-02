# Data contracts and scoring semantics

**Draft v0.1.** The following names are conceptual public schema names for Milestone 1 to finalize in Pydantic models. No working code exists yet.

## Inputs

### `ResponseItem`

| Field | Type | Required | Description |
|---|---|---|---|
| `item_id` | nonempty unique string | yes | Stable unique ID |
| `text` | string | yes | Complete original text; empty is permitted if explicitly configured |
| `metadata` | object | no | User annotations, never interpreted as instructions |

### `Trait` and `Rubric`

- `trait_id`: stable unique string; `name`: human-readable; `description`: full scoring instructions.
- `levels`: ordered list, at least two (endpoint may restrict). Each has a distinct `label`, optional `description`, and optional explicit `numeric_value`.
- Validate numeric coding as finite, unique, strictly increasing in rubric order; do not collapse categories.
- If all labels are numeric and strictly increasing, use those numbers as default coding. Otherwise assign 0, 1, ... K-1. This default MUST be explicit in saved results.
- `Rubric` has unique trait IDs and optional shared rubric-level guidance. Individual traits may have different category counts and coding.

### `Exemplar`

`exemplar_id` (unique), `text` (required), `scores` mapping trait ID -> level label (optional), `rationales` mapping trait ID -> text (optional), and opaque `metadata` (optional). Exemplar references may include one or more traits. Exact inclusion order must be recorded.

### `PairTask`

`pair_uid` unique per **scheduled occurrence**; `left_item_id`, `right_item_id` distinct; texts referenced by ID or provided explicitly; `trait_id`; `original_order`, `presented_order`, optional repetition/reversal group, optional metadata. Repeated A-B tasks have different `pair_uid` values.

### `RunSpec`

- `schema_version`, `task_kind` = `rubric` or `pairwise`.
- `input`, `rubric` or pairwise criteria; optional exemplars; `provider`, `endpoint`, `model`.
- `execution`: `mode` = `live`, `bulk_live`, `batch_native`; `grouping` = `independent`, `grouped`, `auto`; `explanations` boolean; `cache_policy` = `off`, `auto`, `explicit`; `tier` when available; concurrency/retry/batch options.
- `prompt`: `preset` or file path, optional advanced Jinja2 setting; `capture`: `full` (default) or `minimal`.
- `seed`, `output_path`, optional estimated budget ceiling, validation strictness.
- All fields versioned and validated before submission. Unexpected config keys cause clear errors rather than being dropped.

## Normalized task result

A rubric output is one row per `run_id` + `task_id` + `attempt_id`, including:

| Column | Meaning |
|---|---|
| `run_id`, `task_id`, `request_uid`, `attempt_id` | Separate immutable identifiers |
| `item_id`, `trait_id` | Original assessment keys |
| `status`, `error_kind`, `error_message` | Complete/invalid/refused/errored/etc. |
| `provider`, `endpoint`, `model`, `tier`, `mode` | Execution provenance |
| `category_label` | Actual selected/mode category if any |
| `numeric_category_value` | Numeric mapping for selected category if any |
| `expected_value` | Probability-weighted expected score when full probabilities provided |
| `primary_score` | `expected_value` when available; otherwise `numeric_category_value` |
| `primary_score_kind` | `expected` or `category_code` (no false equivalence) |
| `provider_expected_index` | Raw zero-based expectation if supplied by native API |
| `probabilities` | Full ordered array of `{level_label, probability}` when available |
| `confidence`, `explanation` | Optional; never invent values when unavailable |
| `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` | Usage where reported |
| `raw_response_path`, `prompt_path`, `manifest_sha256` | Audit references |

Additional provenance (provider request/remote batch IDs, timestamps, retries, SDK/package version, error HTTP status) is always available in JSONL and normalized sidecars even if compact CSV omits nested structures.

**Score derivation example**: ordered labels `Beginning, Developing, Proficient` default to numeric 0,1,2. Probability vector 0.10, 0.25, 0.65 produces `expected_value=1.55`. If mapped explicitly to 1,2,3 the expectation is 2.55. If provider returns index 1.55, do not use it as the actual mapped score until conversion. A generative output `Proficient` yields `numeric_category_value=2` but no `expected_value`.

## Pairwise output

One result row per attempted pair occurrence: `run_id`, `pair_uid`, `attempt_id`, `trait_id`, `left_item_id`, `right_item_id`, `presented_first`, `presented_second`, `winner_id`, optional `tie`, `p_left`, `p_right`, optional explanation, provider metadata and provenance. Reversal tasks preserve the original pair identity while using distinct pair occurrence IDs.

## Persistence schema (conceptual)

- `runs`: immutable manifest hash, creation/submit timestamps and state.
- `requests`: stable ID, sequence, request payload hash, mode/provider/endpoint/model, remote ID, state and attempts.
- `tasks`: stable output unit and mapping to requests.
- `attempts`: start/end timestamps, status, error class, remote receipt and response reference.
- `batches`: provider IDs, submission payload hashes, state, created/polled/completed metadata.
- `artifacts`: content SHA256 and paths for inputs, prompts, raw payloads, results.

Use SQLite transactions and unique constraints to protect against duplicate task/attempt interpretation. Provider results must be joined by stable request IDs, not array position.

## Privacy

Default `capture: full` stores full rendered prompt and raw provider reply. `minimal` can omit sensitive raw text but must document the reproducibility loss. Secrets must never appear in logs or manifests. Examples and tests must contain synthetic writing only.
