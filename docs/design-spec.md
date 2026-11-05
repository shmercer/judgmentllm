# JudgmentLLM: functional design specification

**Design:** 0.5 (2026-11-02). **Implementation status:** Milestone 1 provides schema-version-1 contracts, synthetic normalization, structural planning, input/output, and offline prompt tools. Milestone 2 has durable persistence, generation configuration, and complete provider-free immutable source preparation with indexed access and independent-process reopening. Subphase 4 adds grouped outcomes, retry/cancellation and immutable exports; Subphase 5 exposes the shared public Python lifecycle and versioned CLI. Subphase 6 is merged in PR #11; its final implementation head `db198aeb0a6387e6a3030c584c34bdd0b41e890f` has retained local and hosted qualification evidence, with the historical six-configuration scope described below. Execution is limited to the deterministic mock provider; real adapters remain later milestones. The sections below describe the full approved direction; [data contracts](data-contracts.md) and [Python API](python-api.md) describe the implemented subset.

## Milestone 3 consolidation and Phase 1 contracts

The [assessment output design](assessment-output-design.md) defines the proposed
next milestone: three single-call rubric layouts independent of grouping,
editable templates and optional exemplars, native structured-output translation,
winner-only generative forced choice, shared secure authentication with offline
status and opt-in verification, request-level usage and explicitly authorized
reasoning evidence. Its [prompt examples](assessment-prompt-examples.md) and
[provider evidence](assessment-provider-evidence.md) separate design from SDK
documentation and live qualification. New controls use an explicit future RunSpec
version 2; implemented version-1 contracts, mock execution and normalized results
remain unchanged by the new standalone Phase 1 contract API. Phase 1 now implements
provider-neutral schemas, strict validation and detached member interpretation;
RunSpec v2 rendering/preparation and lifecycle activation remain Phase 2. See the
[Python contract API](python-api.md#standalone-assessment-output-contracts-milestone-3-phase-1).
Small M3 PRs implement the remaining design;
native decisions remain M4, bulk/batch parity M5 and thin CLI-first R exposure M6.

## 1. Goals

Provide a standalone, maintainable Python library and command-line program for LLM judgment tasks. R workflows primarily call the installed CLI, normally through `processx`, and exchange canonical JSONL or documented R-compatible CSV files. A functional thin `rubricLLM` R wrapper is a Milestone 6 deliverable; `reticulate` is an optional secondary interface rather than the default or a required dependency. Keep data contracts independent of transport and provider. A **locally hosted FastAPI interface** remains a committed Milestone 7 deliverable and optional installation extra; ordinary R use does not require that server.

Two first-class assessment types:

- **Pairwise judgment:** Compare two identified samples on a trait or instruction; forced-choice binary winner by default.
- **Ordinal rubric scoring:** Score one identified response against one or many ordered traits and levels; scores only or scores plus explanations.

Support live and large-volume execution with deterministic task IDs, recovery, explicit costs where determinable, and durable results suitable for research.

## 2. Non-goals (initial versions)

No continuous rubric scales; no statistical BT/TrueSkill estimation, adaptive pairing strategy or rubric calibration engine; no built-in desktop/web UI; no multi-user authentication; no arbitrary user-supplied provider request JSON; no automatic exemplar retrieval, truncation or summarization; no automatic model/provider/tier switching; no assumption that probability-weighted and directly assigned ordinal scores have identical statistical properties.

## 3. Terminology

- **Response/item:** Identified text to assess.
- **Trait:** A named assessment dimension with ordered scoring categories.
- **Exemplar/anchor:** Reference text optionally annotated with scores and rationales; may apply to one or many traits.
- **Task:** One desired output unit. Rubric task = response x trait; pairwise task = scheduled pair occurrence x trait.
- **Request:** One remote call, possibly containing multiple tasks if grouped or supported by native structured-decision endpoints.
- **Attempt:** A submission/processing attempt. Keep distinct from logical task and request.
- **Run:** Immutable scoring specification and its scheduled tasks, batches, requests, results and provenance.
- **Live:** Individual API requests, possibly concurrent.
- **Managed bulk-live:** Locally orchestrated live calls without a provider-managed batch service.
- **Native batch:** Provider-managed asynchronous batch job with remote batch ID and status.

## 4. Required backend endpoints

Implement the following adapters, reusing suitable official SDKs:

1. OpenAI Chat Completions (generative).
2. OpenAI Responses (generative).
3. OpenAI Decisions (typed choice/score; no explanation generation).
4. Anthropic Messages.
5. Google Gemini Developer API.
6. Google Vertex AI with Gemini as the supported initial model family.
7. Together AI generative models.
8. Local Ollama models.
9. TypeSafe AI Jev (`systemone`, typed choice/score).

`openai` and `vertex` represent provider families with endpoint-specific capabilities. Document all tested endpoint/model combinations. Provider SDK/API behavior and account feature availability have precedence over static catalog claims.

Every adapter must expose a narrow **capability description** and implement only its supported operations: validate, build live request, send live, parse response, optional batch create/poll/download/cancel, optional model listing, optional token counting, optional cache controls, and optional service-tier controls. An operation may explicitly be unsupported; never silently emulate native batch unless the user chose managed bulk-live mode.

No provider-specific details should leak into normalized assessment interpretation except via structured metadata.

### Generation and provider request parameters

- `RunSpec.generation` is optional and strictly typed. The initial Subphase 2 foundation accepts `temperature`, `top_p`, and `max_output_tokens`; omitted values stay omitted. It supplies no provider defaults and never changes explicitly requested values.
- Generation controls are separate from `execution` concurrency, retries and rate limits. The existing top-level `seed` controls pairing/presentation; it is not a generation seed or a promise of deterministic model output.
- Validate support, bounds and incompatible combinations for the exact provider/model/endpoint. Known incompatibilities fail locally before submission; missing evidence produces an explicit unknown report, promoted to an error by `strict_capabilities`. An unknown model ID is still permitted subject to this validation. Caller-supplied or offline evidence does not establish live qualification.
- Add `top_k`, ordered `stop_sequences`, `repetition_penalty`, `frequency_penalty`, `presence_penalty`, a separate `generation.seed`, and typed reasoning/thinking controls as adapters implement them. Provider-specific extensions require adapter-owned typed schemas and validation; unrestricted provider JSON remains prohibited. Do not assume different providers' similarly named controls have identical semantics.
- Every requested generation value belongs in immutable manifest/reproducibility commitments. Its exact adapter representation must enter the actual prepared payload and byte hash when request preparation exists. Changing generation settings invalidates resume compatibility; unchanged Milestone 1 configurations retain their structural IDs.
- Subphase 2 implements configuration, capability-validation interfaces and generation commitments only. Milestone 3 must implement and fixture-test supported generative controls per provider/model/endpoint; Milestone 4 must reject generative settings inapplicable to native score/decision endpoints; Milestone 5 must preserve live/batch parameter parity. The CLI and Milestone 6 `rubricLLM` wrapper use this same run configuration.

## 5. Rubric and comparative assessment

### 5.1 Ordinal scoring

- Arbitrary number of distinct ordered category labels for each trait; zero or positive counts of traits only as allowed by input validation (a run must request at least one trait).
- No continuous scale in v1. String or finite numeric labels converted to stable string identifiers while preserving the original scalar in `source_label`; booleans and structured labels are rejected.
- Users may define numeric values for ordered levels. Defaults: numeric category labels use their numeric values when unique and strictly increasing; nonnumeric labels use equally spaced 0-based indices. Explicit mappings override defaults and are retained verbatim in the manifest.
- Native score APIs may restrict to 2-10 categories. A 12-level rubric must fail preflight on these endpoints; do not silently collapse levels.
- For providers supplying a discrete probability distribution, compute expected numeric score from all probabilities and the selected numeric category coding; retain raw provider index score, unchanged per-level probabilities, separate provider-selected and probability-derived modal categories, and confidence if supplied. Probability-sum tolerance is documented and configurable.
- For ordinary generative outputs, record the exact selected category and its coded numeric value, *without labeling it a probability-weighted expectation*.
- Common fields `primary_score` and `primary_score_kind` allow analysis without concealing the scoring distinction.
- A score requiring an explanation on a score-only endpoint must fail during local preflight. No model substitution or second explanation request.
- Preserve model output as actually observed; validate no missing expected tasks, duplicates, invalid categories or malformed format. Reject invalid results at task level; preserve the raw record.

### 5.2 Trait grouping

- Default: independent scoring, one logical response x trait per request.
- Configurable grouped scoring for generative APIs and structured APIs that can natively accept multiple questions.
- Grouping plans must be deterministic and recorded in the manifest as ordered trait IDs and groups. Mock execution binds the actual rendered context and exact provider payload separately; capture policy controls retained bytes. Per-request token estimates remain unknown when no verified counter is available.
- For oversized grouped requests: optionally split into smaller deterministic groups if the user explicitly requested `grouping: auto`; for `grouping: grouped` with a fixed group definition, error rather than alter it. Never split a single trait's exemplars implicitly.
- For Decisions and Jev: questions grouped according to endpoint question count, content and level constraints; no assumption that calling multiple traits together yields an empirically equivalent assessment to independent scoring.
- No hard-coded max number of traits in project schema; provider request limits constrain individual calls.

### 5.3 Comparative judgment

- Pair input includes stable pair occurrence ID, left/right item IDs, content, trait and optional trial metadata.
- Force winner between A and B by default. Optional tie/abstain if supported by a compatible generative rubric, with downstream result schema explicitly distinguishing ties.
- Repeated judgments are distinct task occurrences. Optional deterministic randomization/reversal schedules; record presented order and original pair orientation to support position-bias audits.
- Scores-only equivalent for pairwise is winner-only. Optional explanation for compatible generative endpoints; otherwise preflight error.
- A CJ task may be evaluated through a choice/typed decision endpoint with a separate request adapter and a probability distribution over A/B. Do not pass generative prompt templates to native Decision/Jev APIs.
- Pairwise prompt caching defaults to off where it can be controlled. Provider-imposed implicit caching must be documented.

## 6. Exemplars and anchors

- Support zero to many stored exemplars. Each has an immutable ID, text, optional trait/level labels, optional explanations/rationales and arbitrary user-defined annotations in a documented metadata field.
- A reference can describe a full rubric profile or an individual trait. Validate level labels against the rubric when assignments are present.
- Default behavior includes exactly the selected exemplars in the request; never silently omit, trim, summarize, or retrieve nearest neighbors.
- Preflight token counting via official provider API when available, otherwise conservative local estimation labeled as an estimate. If a definite context/output limit is exceeded, error with counts, limit, problematic request and options. If limit is unknown, warn clearly; offer strict-mode refusal.
- Store exemplar ordering, version/hash, actual rendered text and inclusion mapping per request for reproducibility.

## 7. Prompt system

- Built-in prompt presets: pairwise force-choice, rubric score-only, rubric score-plus-explanation; variants for grouped rubric and native decision APIs as necessary.
- Default user experience: choose a preset, supply rubric and responses, no template coding required.
- Simple custom plain-text templates support placeholders such as `{{ response }}`, `{{ trait }}`, `{{ rubric }}`, `{{ exemplars }}`, `{{ output_instructions }}`, and pairwise counterparts.
- Advanced Jinja2 loops and conditions available, not required. Load templates from files with strict undefined names and sandboxed rendering; only allow controlled data structures and documented filters. No arbitrary Python object access or code execution.
- Output schema enforcement is independent of prompt text. Editing user instructions must not disable local category validation; prefer provider native structured-output facilities when compatible, with parsers and retry rules for other endpoints.
- Support preview/dry-run that renders exact messages and checks placeholders, trait-level restrictions, estimated context, and provider settings before spending money.
- For caching efficiency, default rubric prompt layout puts immutable instructions, rubric, and exemplars before the varying student response when possible. Preserve the user's requested order in fully custom templates; warn if auto caching cannot reuse a stable prefix.
- Do not treat user-provided student response or reference text as instructions to the backend; label and delimit it clearly in built-in templates.

## 8. Prompt caching

- Pairwise default `off`; rubric default `auto`.
- Policies: `off`, `auto`, `explicit`. `off` disables cache controls under library control; do not guarantee the provider cannot cache when its system performs implicit caching.
- In `auto`, assess whether stable reusable prefix is supported and long enough for provider documented thresholds. Do not manufacture economic savings guarantees: log rationale, provider configuration, cache hits/writes when reported, and unknowns. With no supported cache capability, continue uncached or provider-default after a clear status message.
- `explicit` requests eligible provider-specific breakpoints or keys and errors if unsupported. Provider-specific TTL and settings are validated against endpoint/model capabilities.
- Credential secrets, response IDs and private student content must not be embedded into user-visible cache keys. Document per-provider data retention and caching caveats.

## 9. Submission modes, priorities and retries

### Live and managed bulk-live

- Use async concurrency internally via `asyncio`, exposed through documented synchronous wrappers for R, scripts, and ordinary applications.
- Configurable bounded worker concurrency, requests/min, tokens/min, exponential backoff with jitter, cancellation and checkpoint interval. Respect SDK-provided rate-limit guidance and `Retry-After` where possible.
- Priorities/tiers are exact provider options, not universal prices or fabricated tiers. Model/tier compatibility validated against capabilities when known and rejected by provider when unknown.
- Preserve selected model and tier. Never automatically upgrade, downgrade, or switch models/providers. Optional explicit tier-fallback policy must list allowed tiers and be captured in manifest; default has no fallback.

### Native batch

- Where supported, provide `prepare`, `submit`, `status`, `collect`, `cancel`, and safe `resume` operations; split by provider published limits with deterministic task IDs.
- Persist local intent and hash of request payload *before* creating a remote job. Persist remote ID as soon as acknowledged. If submission result is ambiguous, reconcile by provider API / recorded source metadata where feasible; never blindly create a second batch.
- Batch results may be out of order. Match by request UID, check missing and duplicate results, preserve partial completions, errors, expiration, and cancellation states.
- Native batch request submission must not block until completion by default; collection happens in a later process or R session.
- APIs without native batch support must produce a clear error for `mode: batch_native`; `mode: bulk_live` offers the alternative without implying the same pricing or latency.

### Retry semantics

- Retry bounded transient HTTP, network and rate-limit failures with provider-specific defaults and ability to disable.
- Never automatically rescore a *completed* but invalid rubric output, truncated response or refusal. Retain the failed attempt; allow explicit retry of selected logical tasks with new attempt IDs.
- Distinguish connection timeout before submission from unknown outcome after remote acceptance to protect idempotence.

## 10. Planning, cost and context

- Always run inexpensive local schema/capability validation before submission. Users can request `plan` / `--dry-run` for task counts, grouping, token estimates, provider batches, caching expectations, and projected cost where current pricing is available.
- Costs must be labeled estimated and timestamped with sources; separate input/output/cache tiers and native batch discounts where applicable; warn if price or token count is unknown. Do not use unverified provider prices.
- Optional budget cap must be checked against estimated plan and incremental actual usage where available; if actual costs cannot be controlled reliably, explain the limitation.
- Planned scale: 100k input responses, hundreds of traits and thousands of stored exemplars; stream scheduling and results rather than materializing response x trait Cartesian products in RAM.

## 11. Persistence and auditability

- SQLite (`sqlite3`) holds runs, request/batch registry, task IDs, attempts, statuses and file references; use transactions, unique constraints, WAL where appropriate and indexed lookups.
- JSONL is the canonical streaming record format; CSV for tabular interoperability and optional Parquet via a separate extra for larger datasets. Do not require Pandas or PyArrow for basic use.
- A run manifest becomes immutable on submission; it references exact data/rubric/prompt hashes, model ID, SDK/package versions, explicitly requested generation parameters, grouping, caching, tiers, pairing seed, units, selected exemplars and provider qualification snapshot. Requested configuration identity is separate from dated capability evidence and qualification state.
- Default capture stores actual rendered prompts, full original provider responses, error payloads and normalized outputs. Users may explicitly choose privacy-minimal capture and disable retention where supported.
- Never store API keys or other access tokens in logs/results/manifests. Redact request headers. Default output directories excluded from git tracking; documentation warns about student data and provider privacy requirements.
- Store run-id + request-id + task-id + attempt-id separately; support append-only audit trails and resumption after process interruption.
- R interoperability is an ongoing contract across milestones: preserve audit/analysis separation, all attempt IDs, original and presented orientations, observed outcomes, probabilities, and score kinds. Milestone 1 provides derived R CSV views; the full bridge/wrapper remains Milestone 6.
- Before any future submission, capture immutable hashes of original input content, rendered prompts, and the exact provider payload. Structural IDs do not substitute for content hashes. Resumption must detect changed referenced text and must not blindly reuse old submission identities for changed content.
- Provide an export normalized to columns suitable for R `pairwiseLLM`: unique pair UID, presented IDs/order and winner, with raw probability fields preserved when present. Statistical fitting remains in R.

## 12. Model catalog and support classes

- Automatically generate `docs/model-support.md` and a machine-readable `models.json` from relevant provider discovery APIs/documented data, but **never** use this snapshot as a strict allowlist.
- Scope to relevant assessment-capable text/decision models; exclude primarily coding-only or modality-incompatible models from the recommended list, while allowing manual IDs.
- Vertex AI default generated catalog prioritizes Google Gemini models; mention that Vertex supports other provider/model families without duplicating large third-party catalogs.
- Per model/endpoint record: discovered, adapter-implemented, offline-fixture-tested, and live-qualified statuses (separately), source/date, availability and account/region caveats; model ID, known context, native batch, cache and tier capabilities where documented.
- Unknown/unlisted capability: mark unknown and permit a validated attempt if endpoint can accept arbitrary model ID; never infer tier or feature support from an ID string alone.
- Scheduled refresh should create a reviewable pull request when data changes rather than silently edit the default branch. No credentials in public CI artifacts.

## 13. Public interfaces and distribution

- Standalone pure-Python library and `judgmentllm` CLI, backed by the same validated services. Canonical JSONL and documented R-compatible CSV are stable cross-language contracts; R views remain derived audit/analysis views, not replacements for canonical records.
- R integration is CLI-first: normally use `processx` to invoke the installed executable and consume its structured results/files. Calls operate on whole runs, files, or appropriately sized task groups/batches; do not start a Python process for each judgment.
- Milestone 6 delivers a functional thin `rubricLLM` wrapper covering configuration, validation, planning, submission, status, resumption, and collection through the CLI. It reuses Python execution and file contracts rather than duplicating provider adapters, persistence, or scheduling. This remains planned work; Milestone 1 implements only offline preparation/template commands.
- For this integration, `pairwiseLLM` consumes JudgmentLLM result files and performs comparative-judgment statistical analysis in R. It does not embed Python or become a second JudgmentLLM execution engine. Rubric/statistical analysis, calibration, and fitting stay in R.
- `reticulate` remains an optional secondary path for advanced users who deliberately embed Python. Neither JudgmentLLM core installation nor the ordinary CLI-first R workflow requires it.
- Package name `judgmentllm`, optional extras: `openai`, `anthropic`, `google`, `together`, `ollama`, `typesafe`, `parquet`, `api`, `all`; avoid unneeded cloud SDK installs.
- Use semantic versioning, GitHub Actions, PyPI Trusted Publishing once account and name are established; build platform-independent wheels when possible.
- FastAPI is a **required Milestone 7 deliverable**, after the core library, CLI, integrations, batch engine, R bridge and catalog. Users install it only if needed via `judgmentllm[api]`; core Python/CLI dependencies stay independent. Early core prereleases may ship before Milestone 7.
- The first HTTP interface is single-user and local-only: default and enforced loopback binding, no authentication or multi-user features, no public Internet exposure. The service must reuse validated library operations, persistent jobs and result contracts. See [`docs/http-api-spec.md`](http-api-spec.md) for the proposed versioned endpoints and safeguards.
- Ordinary R and `rubricLLM` workflows do not depend on FastAPI or an HTTP server.
- HTTP operations must not block on long-running scoring/batch work. Submit requests return a durable run ID; client polling and resumes survive HTTP client disconnects and service restarts. Preserve the same provider request/retry, provenance and privacy policies as direct Python and CLI use.

## 14. Quality requirements

- Python>=3.11, pure Python core, Linux/Windows/macOS test matrix.
- Offline tests use synthetic fixtures; no paid API calls or cloud credentials in required CI.
- R compatibility is a standing acceptance condition for schema, export, and CLI changes in every milestone. Preserve IDs as character strings, original/presented orientation, all attempts and explicit analysis selection, observed ties/failures, complete probability/mapping data, and selected/category-code versus expected-score semantics.
- Require R round-trip steps in the existing Ubuntu/Python 3.11 matrix job: generate synthetic Python exports, import them using base R, and require `jsonlite` for full distribution/mapping recovery checks. Run with `JUDGMENTLLM_R_STRICT=1`; missing R or `jsonlite` must fail this check, not silently skip it; optional pairwiseLLM checks remain separate. Seven strict gates cover exports through the implemented installed-CLI lifecycle. The future R wrapper remains a separate deliverable.
- Coverage via `pytest-cov` and Codecov; required branch-aware coverage is at least 90%. Target near-complete testing of validation/parsing and recovery edge cases, not an artificial 100% of every line.
- Manual and prerelease live qualification tests with explicit budget ceiling and cleanup of test uploads/jobs.
- Ruff format/lint, Pyright type checks on public modules, build validation with Hatchling, deterministic tests; documentation includes standard installation and CLI workflows.
- Explicit test cases for: ordinal coding, invalid category, score vs probability expectation, missing explanation support, large traits, 0/many exemplars, unknown context limits, duplicate/out-of-order batch results, interrupted resume, cache off/auto/explicit policies, fallback rules, output provenance, malformed transport outcomes and secret redaction.
- Generation acceptance includes offline strict-schema and capability cases, sparse serialization, omitted-versus-explicit settings, immutable generation commitments, stable legacy structural IDs, changed-setting resume rejection, and Python-generated provenance consumed in R. Exact payload tests belong to the implementing adapter milestones.
- Milestone 7 adds offline FastAPI/ASGI tests for schema parity, local-only binding, error responses, non-blocking job submission, status/result polling and recovery after service restart; no provider credentials required.

## 15. Safety/privacy and operational limits

- Never embed secret values into config examples or logs; use environment variables or process-specific secret stores.
- Native batch files and persisted raw prompts can contain sensitive student text; surface retention risks clearly, allow local deletion and capture controls.
- On an unsupported provider/endpoint/capability combination, fail locally with the offending field and suggested compatible operations when known.
- Treat output scores as model-generated assessments, not validated measurement instruments. The library does not guarantee scoring accuracy, calibration or fairness.

## 16. Current reference documentation (verify at implementation time)

- `pairwiseLLM` provider implementation: https://github.com/shmercer/pairwiseLLM
- OpenAI Decisions: https://developers.openai.com/api/docs/guides/decisions
- OpenAI Batch: https://developers.openai.com/api/docs/guides/batch
- Anthropic Message Batches: https://platform.claude.com/docs/en/api/messages/batches/create
- Google Gen AI Python SDK (Developer and Vertex): https://googleapis.github.io/python-genai/
- Gemini Developer Batch: https://ai.google.dev/gemini-api/docs/batch-api
- Vertex Gemini Batch: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/samples/generativeaionvertexai-batch-predict-gemini-createjob
- TypeSafe Python SDK: https://docs.typesafe.ai/sdk/python/
- Ollama API: https://github.com/ollama/ollama/blob/main/docs/api/introduction.mdx
- Codecov Python: https://about.codecov.io/language/python/

All documented capabilities are subject to provider and account change. Date and qualify concrete model combinations rather than assuming live API availability from documentation alone.

## Milestone 2 implementation boundary

Internal mock execution now integrates immutable preparation, exact adapter-owned
payloads, bounded async workers, owner-token-checked durable writes, independent
mock acceptance evidence, response receipts, and conservative recovery. It
supports independent/grouped rubric and pairwise assessments through the established
validators/normalizers; every result remains explicitly synthetic and schema 1.
Full/minimal privacy and source/capability compatibility apply at scheduling and
submission. The service never retries a possibly submitted request because a
process disappeared. Grouped salvage, explicit subset retries, conservative automatic
retry, cancellation, immutable export, shared public Python services and the
versioned lifecycle CLI are implemented. The installed-CLI R lifecycle gate adds
to the six established strict interoperability gates. Subphase 6 supplies complete
scale workloads, resource/storage diagnostics and user examples. Its final
implementation head `db198aeb0a6387e6a3030c584c34bdd0b41e890f` passed the then-required
six local Python 3.11/3.12 configurations and all 42 strict R/jsonlite executions,
plus all five hosted configurations on the PR and literal final head.
[PR #11](https://github.com/shmercer/judgmentllm/pull/11) is merged; the
[final-head hosted run](https://github.com/shmercer/judgmentllm/actions/runs/42077694940)
and external `qualification/db198aeb0a6387e6a3030c584c34bdd0b41e890f/ledger.json`
under `/mnt/VM_Fast/judgmentllm-subphase6/` preserve the measured source/equivalence
and earlier failed/interrupted records. This historical qualification does not
qualify later design heads under the current seven-configuration local policy.
Real providers remain later milestones.
See the dated [Subphase 5 verification record](milestone-2-subphase-5-verification.md)
and current [scale qualification record](milestone-2-subphase-6-verification.md).
