# Recorded design decisions

**Date:** 2026-11-02. **Status:** approved design direction; implementation and API qualifications pending.

| Area | Decision |
|---|---|
| Name | JudgmentLLM; repo `shmercer/judgmentllm`, planned PyPI `judgmentllm` |
| Purpose | General LLM judgment infrastructure, both pairwise and direct rubric scoring |
| Providers | Match `pairwiseLLM`: OpenAI Chat/Responses/Decisions, Anthropic, Gemini Developer, Vertex AI (Gemini focus), Together, local Ollama, TypeSafe AI Jev |
| Rubric | Arbitrary ordered categories, no continuous scales in initial release; enforce endpoint-specific limits |
| Scores | Retain all returned score data; expected value is primary if category probabilities exist; do not imply comparability with categorical model outputs |
| Explanations | Scores only or scores plus explanations; fail clearly when an endpoint cannot supply requested explanations |
| Traits | Arbitrary stored trait count; independent evaluation by default, grouped configurable; explicit, deterministic partitioning when necessary |
| Exemplars | Zero to many; IDs, text and optional trait/level scores, rationales, annotations; no automatic dropping/selection; warn/error on context overflow or unknown limit |
| Prompts | Fully editable text prompts with accessible placeholders; advanced Jinja2 opt-in; no arbitrary JSON API requests |
| Pairwise | Forced winner default, optional ties where compatible, repetitions and presentation-reversal audits, optional explanations when available |
| Cache | Pairwise off by default, rubric auto by default; `off`, `auto`, `explicit`; explain provider implicit caching constraints |
| Execution | Live (provider tiers where supported), native Batch (where supported), and managed bulk-live; no unapproved fallback in model/tier |
| Generation | Optional strict RunSpec generation controls, separate from execution and pairing seed; preserve omission, validate exact model/endpoint capabilities, commit requested values for reproducibility |
| Scale | Design for 100,000 response input, hundreds of traits, thousands of stored exemplars; do not hold entire work in RAM |
| Data | CSV/JSONL, optional Parquet, Python and R data frames; stable long-form outputs and complete audit records by default with opt-out |
| Retries | Transient transport errors automatically with bounded backoff; malformed/completed judgments retained, re-score only on explicit request |
| Cost controls | Local validation always; optional plan/dry-run with estimated cost, request count and budget ceiling |
| Runs | Immutable manifest after submit; new run ID when spec changes; resume without blind duplicate submission |
| Models | Auto-generated reference catalog; distinguish discovered/implemented/qualified; omit primarily coding-only models from recommended catalog without blocking user-supplied IDs |
| Dev stack | Python>=3.11, pure Python, uv, Hatchling, Pydantic 2, PyYAML, Jinja2, Typer, asyncio, official SDKs, SQLite, pytest, Ruff, Pyright, Codecov |
| Packaging | Standalone Python/CLI; optional provider dependencies plus `[all]`, PyPI and GitHub; functional CLI-first `rubricLLM` wrapper in Milestone 6 |
| R integration | Canonical JSONL/R-compatible CSV file contracts; CLI-first through `processx`; `reticulate` optional secondary; statistics remain in R |
| HTTP | FastAPI is a committed late-stage development milestone (Milestone 7) after core/CLI/providers/batch/R bridge. Installed only via optional `[api]` extra; local loopback-only and single-user without authentication. No remote or multi-user features in this scope. Early library/CLI prereleases may precede HTTP support. |
| Licensing | MIT; see the repository LICENSE |
| Qual tests | Manual option and before every release, separate from offline CI |
| Numeric labels | Ordered category indices equally spaced by default for nonnumeric labels; allow explicit numeric mapping; preserve original labels |

## Milestone 0 implementation decisions (2026-11-02)

- The initial package development version is `0.1.0a1`. The baseline implements import/version information and CLI help/version only; assessment schemas and operations remain assigned to later milestones. README, CLI, and data-contract status wording now distinguish the working foundation from the unimplemented assessment features.
- Use the existing uv 0.13.6 installation for local development. Keep dependencies in the repository's ignored `.venv`, commit `uv.lock`, and use locked syncs in CI and routine development for reproducible environments. Typer is the only direct core runtime dependency; provider, Parquet, and HTTP dependencies are optional extras.
- Update the Together dependency baseline from SDK v2 to `together>=3,<4`, as explicitly selected during Milestone 0 planning. The [official v3.0.0 release](https://github.com/togethercomputer/together-py/releases/tag/v3.0.0), published 2026-10-22, supersedes the version named in the original provider matrix. No adapter implementation or live qualification is implied.
- Use the official [`typesafe-sdk`](https://docs.typesafe.ai/sdk/python/) rather than a new HTTP adapter dependency. The official maintained Python client was confirmed during dependency inspection. Replace the original Jev reference URL, which identifies itself as unaffiliated with TypeSafe AI, with official SDK documentation.
- Record MIT as the existing repository license, correcting the earlier proposed-license wording without changing the license terms.

## Deliberately unresolved and nonblocking

- PyPI project name can only be claimed by a successful first release / publisher registration; availability not guaranteed by search.
- Provider capability details, quotas, token limits and permitted caching/priority controls must be checked against authoritative docs and live qualification before a feature is marked supported.
- The initial public Python function signatures and schema version 1 are finalized in Milestone 1; see `python-api.md` and `data-contracts.md`.
- Support for native Vertex Gemini batches is in scope, but Google Cloud project, location, GCS/BigQuery inputs and IAM dependencies require separate documentation and qualification.

## Milestone 1 implementation decisions (2026-11-02)

- Advance the development version to `0.1.0a2`; schema version remains integer `1`, including when omitted for convenient Python/YAML construction. Explicit unsupported versions are rejected; no migration is implied.
- Use strict Pydantic models with unknown fields forbidden and JSON-only metadata. Numeric scalar labels are retained in `source_label` and normalized to string category keys. Require complete explicit mappings; invalid wholly numeric defaults fail instead of the draft's ambiguous index fallback. Mixed scales use indices and warn when numeric labels differ from those codes.
- Provider-selected categories (`category_label`) and probability-derived modes (`modal_category_label`) remain separate, with separate numeric codes. Never infer selection. Use mapped expectations as primary when a complete valid distribution exists. Store the original probabilities, raw provider expected index, and effective configurable absolute sum tolerance (default `1e-8`). Equal probability maxima use the first category in rubric order.
- Preserve exact exemplar blocks and selected ID order in simple and advanced prompts. A small Jinja AST validator and post-render identity accounting enforce inclusion without a custom interpreter or text-substring matching. Advanced layout supports nonnested loops over documented lists; protected assessment blocks remain verbatim. Rendering limits fail rather than truncate and do not limit stored assessment size.
- Pair inputs use response ID references, with separate `PairwiseCriterion` instructions. Inline pair text and abstention are deferred; explicit ties are supported by compatible custom templates. Pairwise exemplars cannot carry ordinal scores without a rubric.
- Pair scheduling is a lightweight pure iterator. Input/repetition order remains stable; a portable versioned SHA-256 rule chooses presentation independently for each source occurrence/repetition. Reversals use the exact opposite. Every scheduled judgment has a unique occurrence ID, separate from original pair identity. The helper rejects already-expanded inputs rather than overwrite their scheduling provenance.
- Structural plans are lazy, independent by default, and accept explicit fixed groups or deterministic auto splitting against known positive trait-count limits. Do not infer context capacity or provider capability. No token/cost estimation, provider-specific scheduling, persistent execution, or submission is implemented.
- Keep execution mode/cache/tier/concurrency as typed intent declarations. Retry, batch-operation, and budget fields remain deferred and are rejected as unknown rather than accepted in arbitrary dictionaries. Capture intent does not create audit stores in this milestone.
- Normalized JSONL retains all fields; CSV uses JSON cells for nested values, `\N` for null, and escapes literal leading backslashes. Streaming readers store seen IDs and current records, not the response-by-trait product. Pair preview retains only the two selected texts.
- Implement only `templates list/init/preview` CLI additions. Reserve the full execution CLI for Milestone 2 and future synchronous `run_live` / asynchronous `arun_live` services.

## Milestone 1 pre-merge hardening and R interoperability (2026-11-02)

- Keep schema version 1 and unreleased package version `0.1.0a2`; canonical normalized JSONL/lossless CSV columns are unchanged. Writers, rather than all Python model assignments, form the export-validation boundary. Reconstruct complete current data per record, including copied unknown fields, with field-name validation for aliased generic models. Reject invalid data and validator-induced repair. Direct Pydantic serialization stays a low-level operation.
- Add streaming `export_pairwise_r_csv` and `export_rubric_r_csv` as derived all-attempts audit views. Keep all supplied records. Use a configurable `NA` missing marker, rejecting collisions rather than losing literal strings. Preserve leading zeros through explicit character import in R. Original and presented probability orientations are separate; outcomes always come from observed judgments.
- Require explicit `(run_id, task_id, attempt_id)` selection for analysis examples. The binary `pairwiseLLM::build_bt_data()` adapter drops ties/missing winners, whereas direct tie-capable models have a separate contract. Its BTL results builder replaces occurrence IDs and fabricates timestamps; retain original audit data and a row crosswalk. See commit-pinned source references in `r-interoperability.md`.
- Prefer `first_response` and `second_response` in built-in templates; retain legacy presentation-order aliases unchanged. Do not reinterpret left/right render arguments or original item IDs.
- Enforce the independently documented OpenAI Decisions 2–10 level boundary in preflight; do not infer identical constraints for other endpoints. TypeSafe documents a maximum of ten criteria and recommends at least two, without independently establishing the same enforced minimum. Its verified maximum may be supplied by `CapabilityProfile`; no new Jev adapter/rule is implemented.
- Treat R interoperability as a continuing design requirement, while the fuller bridge/wrapper remains Milestone 6. Milestone 2 must capture immutable input/prompt/provider-payload hashes before submission, detect changed references on resume, and never blindly reuse submission identity for changed content. Original task/pair/request/attempt IDs remain preserved. No execution/persistence work is included in this pass.

## Approved R architecture and standing acceptance (2026-11-02)

1. JudgmentLLM remains a standalone Python library and CLI. R clients use the same validated services and result contracts.
2. Canonical JSONL and documented R-compatible CSV are the cross-language exchange formats. Preserve canonical audit records; derived R tables retain explicit outcome/score semantics and provenance.
3. The ordinary R workflow is CLI-first, normally using `processx` to invoke the installed executable and read files/structured output.
4. Milestone 6 must deliver a functional thin R wrapper, provisionally named `rubricLLM`, for configure, validate, plan, submit, status, resume, and collect operations. Documentation alone does not satisfy that deliverable. The wrapper does not duplicate Python execution, provider adapters, persistence, or scheduling.
5. For this integration, `pairwiseLLM` consumes JudgmentLLM files for statistical analysis rather than embedding Python or becoming another execution engine.
6. `reticulate` is an optional secondary route for users choosing to embed Python. It is not the primary R architecture or a compulsory dependency of the core or ordinary R workflow.
7. FastAPI remains a committed Milestone 7 deliverable, installed as an optional extra. An HTTP server is not required for ordinary R or `rubricLLM` use.
8. R-to-Python calls operate on whole runs/files or appropriately sized task groups/batches, not one Python process per judgment. The Python service owns request execution and durable identities.
9. Statistical fitting, comparative-judgment analysis, and calibration remain in R; no statistical engine is added to the Python core.

R interoperability is an acceptance condition throughout subsequent milestones, not work deferred entirely to Milestone 6. Required R round-trip steps in the existing Ubuntu/Python 3.11 CI matrix job generate synthetic Python exports and import them in R with `JUDGMENTLLM_R_STRICT=1` and required `jsonlite` distribution/mapping checks. Missing prerequisites fail that check; optional pairwiseLLM checks may report separately. Retain the existing Python matrix and 90% coverage gate. Current export, tie, attempt-selection, orientation, and provenance contracts remain unchanged. The functional wrapper, execution CLI, and HTTP service are future deliverables, not implemented by this documentation correction.

## Milestone 2 generation configuration foundation (2026-11-02)

- Begin Subphase 2 with optional `RunSpec.generation`, accepting strictly typed `temperature`, `top_p`, and `max_output_tokens`. Omission, null, and an empty generation object all mean no requested controls. Sparse serialization retains explicit zero values and never supplies provider defaults.
- Keep generation separate from execution controls and top-level pairing/presentation `seed`. Reserve `top_k`, ordered `stop_sequences`, `repetition_penalty`, `frequency_penalty`, `presence_penalty`, a separate `generation.seed`, and typed reasoning/thinking controls for additions owned and validated by the implementing adapters. Unsupported extension keys remain errors; arbitrary provider JSON is not accepted.
- Scope generation capability evidence to provider, endpoint and model. Known unsupported values/combinations fail preflight; unknowns remain explicit and obey `strict_capabilities`. No built-in cloud capability matrix, model allowlist, provider request builder or live qualification is introduced by this foundation.
- Commit requested generation identity separately from capability evidence. Include it in the eventual full manifest and prepared-payload commitments; changing a requested value is incompatible with resume under the same frozen specification. Preserve global canonical hashing behavior and existing Milestone 1 request IDs for no-generation configurations; explicit controls enter request context identity while item/pair/task IDs stay stable.
- Retained generation-only fixtures exercise immutability and compatibility foundations; they are not complete executable run manifests. Source freezing/indexing, complete manifest publication and operational resume remain subsequent Subphase 2/lifecycle work.
- Milestone 3 implements and tests provider/model/endpoint parameter mappings; Milestone 4 rejects generative settings inapplicable to native score/decision endpoints; Milestone 5 proves live/batch parameter parity; Milestone 6 passes the same configuration through `rubricLLM`. Required offline R provenance checks continue from the current foundation.


## Milestone 2 Subphase 5 public lifecycle decisions (2026-11-04)

- Retain package `0.1.0a2`, all existing schema versions and normalized/R columns.
  Expose lifecycle services through thin public wrappers; structural `validate_run`
  and `plan_run` keep their existing meanings. Complete validation and exact input
  counts use the immutable preparation pipeline.
- Execute only exact `mock` / `synthetic` configurations. Other provider names
  remain valid for existing offline use but cannot silently execute through a
  substitute adapter. Native batches, managed bulk-live, cloud adapters and
  actual caching controls remain deferred.
- Put explicit subset retry controls on `resume`: repeated `--retry-task`, required
  `--retry-id`, and explicit `--allow-completed`. One operation selects from one
  original request and retains the existing prior-outcome and attempt-limit rules.
- Keep mock fixtures outside RunSpec and runtime persistence. Default service
  ledger is `<store>/mock-service.sqlite3`; custom fixture/ledger inputs must be
  supplied again. Missing or changed evidence cannot authorize resubmission.
- `run` and `resume` block in their CLI process. JSON mode separates one stdout
  control envelope from stderr diagnostics and process exit status. Hard death
  cannot promise a final envelope or provider cancellation.
- Add installed-CLI lifecycle acceptance as the seventh strict R/jsonlite gate;
  retain all six existing gates. Base R uses separate stdout/stderr files and
  independent CLI processes. Heavy fault matrices and scale qualification stay
  outside this small R fixture.
- Require six local OS/Python 3.11/3.12 lanes on the final commit. Hosted CI retains
  Ubuntu/macOS 3.11/3.15 plus Windows 3.12 and optional full Windows 3.11 dispatch.
  Local results do not establish hosted 3.15 qualification.

## Milestone 2 Subphase 6 candidate (2026-11-05)

- Prepare development version `0.1.0a3` before freezing final qualification.
  This identifies the candidate, rather than declaring acceptance or publishing
  a release. Exact-commit local evidence remains outside the tracked source tree.
- Permit only the explicitly qualified a2 producer under the a3 consumer, in
  addition to each supported version reading itself. Preserve original manifests,
  payload/attempt/submission identities, schema versions and historical exports.
  Unknown producer/consumer transitions remain rejected; no migration framework
  or schema migration is introduced.
- Retain a small genuine baseline-a2 synthetic fixture in routine CI and require
  the broader installed a2-to-a3 boundary matrix separately. Never manufacture
  old state by substituting a version field in an a3-produced manifest.
- Keep complete 5,000-by-20 qualification separate from routine CI unless actual
  hosted complete-job measurements establish adequate margin. Preserve all
  existing resource parameters, limits, R gates and job deadlines.
