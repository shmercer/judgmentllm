# Phased implementation and verification plan for Codex

**Rule:** Implement one milestone per small pull request or coherent series of PRs, with the acceptance conditions below. After each milestone, revise `docs/design-spec.md` if evidence requires a change. Do not quietly scope out accepted features; record deferrals explicitly. Milestone 7 (FastAPI) is a **committed, late-stage deliverable** after Milestones 0-6. The FastAPI dependency remains optional for users; early library/CLI prereleases do not need to wait for the HTTP interface.

## Milestone 0 - Repository and engineering baseline

- Create public GitHub repo `shmercer/judgmentllm` (target), MIT, `main` branch, issue templates optional.
- Confirm or reserve PyPI distribution name before release (availability not independently verified).
- Add Python src layout, `pyproject.toml` using Hatchling, `uv` for dev, optional provider extras + `[all]`, CLI entry point and a minimal typed package import.
- Add CI across Linux, Windows, macOS with Python 3.11 and current stable Python; Ruff, Pyright, pytest, package build; generate coverage and upload to Codecov after app connection.
- Add offline synthetic tests and install/import/`--help` smoke tests.

**Acceptance:** Fresh clone can run `uv sync --all-extras --group dev`, lint, type checks, tests and wheel build offline (other than installing dependencies); wheel installs and `judgmentllm --help` works; no secrets in CI. Public release is not required yet.

## Milestone 1 - Data contracts, rubrics, results, prompts

Implemented in `0.1.0a2`; see [public Python API](python-api.md) and [verification record](milestone-1-verification.md). Milestone 1 planning is structural; Milestone 2 adds exact source counts and persistent mock execution. Token/cost estimates remain unknown. Provider-selected and distribution-derived modal categories are separate, and probability-sum tolerance is configurable.

- Implement Pydantic models for ResponseItem, Trait, Level, Rubric, Exemplar, PairTask, RunSpec and normalized result types; version schemas.
- Validate ordered levels and explicit/default numeric coding, duplicate IDs, unknown config keys, and explanation capability requirements.
- Implement built-in score-only, score+explanations and pairwise templates; beginner plain-text placeholders and sandboxed advanced Jinja2; offline preview and schema validation.
- Implement stable deterministic task IDs and request grouping plan; independent scoring default; grouped and auto strategies explicit.
- Read CSV/JSONL input and YAML/JSON configs; write normalized JSONL, export CSV.
- Revalidate current result data before canonical serialization and derived R-compatible pairwise/rubric CSV exports. Preserve all attempts, failures, ties, original/presented orientations, score kinds, full probabilities, and character IDs; do not silently repair, deduplicate, infer winners, or select analysis attempts.
- Establish the required Python-to-R interoperability baseline: Python generates synthetic audit exports, base R imports them with explicit types/missing values, and `jsonlite` recovers complete probability distributions. Run this credential-free check in required CI on Ubuntu/Python 3.11. An R dependency is not added to the Python core; mandatory `pairwiseLLM` integration and the R wrapper remain Milestone 6.

**Acceptance:** End-to-end *offline* tests build a multi-trait rubric from example files, render exact prompts, compute mapped expected scores from synthetic probability distributions, reject invalid category labels and missing placeholders, and retain all selected exemplars in original order. Actual Python-generated pairwise and rubric exports pass the required R/`jsonlite` smoke test, including leading-zero IDs, reversed presentations, binary wins, observed ties, unsuccessful records, multiple attempts with explicit analysis selection, differing/nonuniform scales, selected versus modal categories, and categorical versus expected scores. Invalid top-level/nested/model-copy mutations fail export. Missing R/`jsonlite` or failed applicable interoperability tests cannot be treated as acceptance; the baseline does not require `pairwiseLLM` until Milestone 6.

## Milestone 2 - Execution engine and persistence (mock provider)

The [approved detailed plan](milestone-2-execution-plan.md) preserves the six
subphases. Subphase 1 provides the [internal persistence contracts](persistence-contracts.md)
only; Subphase 2 now supplies immutable source freezing/indexing, complete
executable input manifests, capture policies and independently verified reopening.
Subphases 3–4 implement mock execution, grouped outcomes, retries, cancellation
and immutable exports; Subphase 5 exposes public Python/CLI lifecycle controls
and installed-CLI R acceptance. Subphase 6 scale tooling is implemented;
exact-source qualification is recorded in the external ledger defined below.
Real providers remain later milestones.
See the [scale qualification record](milestone-2-subphase-6-verification.md),
[source-preparation verification record](milestone-2-subphase-2-verification.md) and
[Subphase 1 verification record](milestone-2-subphase-1-verification.md).

Begin Subphase 2 with the generation configuration, capability-validation and
manifest-commitment foundation. Generation-only retained fixtures do not complete
source freezing, complete manifest construction, preflight or operational resume.
See the [generation verification and source-preparation handoff](milestone-2-generation-verification.md).

- Implement SQLite registry for runs, requests, tasks, batches, attempts and artifact hashes; immutable manifest on submit; resume states.
- Add optional strict `RunSpec.generation` with `temperature`, `top_p`, and `max_output_tokens`, separate from execution controls and the pairing/orientation seed. Preserve unspecified values and existing no-generation serialization, structural IDs and diagnostics. Document typed adapter-owned extensions for top-k, stop sequences, penalties, generation seed and reasoning/thinking controls without accepting arbitrary provider JSON.
- Validate model/endpoint-specific parameter support, bounds and combinations; reject known incompatibilities and report unknown support without implying qualification. Freeze requested generation identity separately from capability evidence and revalidate current restrictions before submission/resume. Include requested controls in complete manifests and exact prepared payload identities as preparation is implemented; reject changed controls under the original frozen specification.
- Before submission, capture immutable hashes of original input content, exact rendered prompts, and exact provider request payloads. Structural task/request IDs are not content hashes. Detect changed referenced text during resumption and refuse to reuse a submission identity for changed content; preserve all original task/pair/request/attempt IDs.
- Preserve the R audit/export contracts throughout execution and resumption, including presented orientation, score kinds, complete probabilities, and all attempts. Full R bridge/wrapper work remains Milestone 6.
- Implement async bounded execution plus clean synchronous wrappers; bounded retry/backoff for transport failures; explicit completed-output retry.
- Implement `validate`, `plan`, `run`, `resume`, `runs list`, `runs status --run RUN_ID`, and `results export` CLI via shared library.
- Establish the versioned JSON control contract in [cli-spec.md](cli-spec.md) for R/process callers across validation, planning, submission, status, resumption, and export. Machine-readable mode writes only control JSON to stdout, sends diagnostics to stderr, uses documented predictable exit codes, and supports noninteractive operation. Control responses distinguish command success/partial/error from durable run state and return durable run IDs and artifact references rather than requiring an open process or an always-running Python service.
- Add cost/token estimate interface with unknown values preserved, prompt caching policies and capability validation interfaces.

**Acceptance:** Content-change fixtures verify input/prompt/payload hashes, changed referenced text is detected on resume, and old submission identities are never blindly reused for changed content while original JudgmentLLM IDs are retained. Mock provider can process synthetic large streamed inputs with limited RAM; interrupted run resumes with no duplicate completed tasks; out-of-order and partial task results are correctly matched; ambiguous batch submission never blindly reissues a remote request; raw/prompt capture and redaction work. Required offline R tests invoke the installed CLI against mock execution, parse the versioned JSON control responses, retain durable IDs/artifact paths, terminate the original Python process, and then query status, resume, and export in later processes. Python and CLI reuse the same services; R requires no resident Python process or HTTP server, and existing export/score/provenance guarantees remain intact.

**Generation foundation acceptance:** Offline tests cover strict schemas,
boundary and combination validation, scoped unknown capabilities, sparse
serialization, hashing, immutable commitment retention and resume compatibility.
No-generation fixtures preserve Milestone 1 IDs/serialization and add no
generation warnings, including strict mode. Capability-evidence changes preserve
requested identity; newly known restrictions still block continuation. Actual
Python-generated generation provenance is consumed in the required R/jsonlite
gate. Generation-only fixtures do not establish complete preparation by
themselves; the separate immutable-preparation acceptance covers executable
manifests. Real-provider request builders remain later milestones.

### Milestone 2 implementation status

Mock execution integrates immutable preparation, exact payload commitments,
bounded workers/queues, independent acceptance evidence and conservative recovery.
Grouped salvage, explicit subset retries, cancellation, immutable exports, public
Python services and installed CLI controls are implemented. Seven strict
R/jsonlite gates cover file exports through independent installed-CLI lifecycle
operations. Required hosted CI has five configurations; the exact-head local
grid prospectively requires seven configurations: Linux Python 3.11/3.12/3.15
and Windows/macOS Python 3.11/3.12. See [local CI](local-ci.md); historical
Subphase 6 qualification below retains its original six-configuration scope.

Subphase 6 benchmark tooling, persistence diagnostics and executable examples are
implemented. The revised completion protocol combines retained complete
100,000-judgment minimal/full execution evidence, bounded 64-request execution
allocation comparisons at 200 versus 5,000 responses, separate initialization
and complete-export measurements, genuine package compatibility and the final
six-configuration local grid. Further full-size timing repetitions, full-run
cProfile/tracemalloc and optional diagnostic/stress sweeps are not required.
Limited full-capture sample counts must remain explicit; no qualified throughput
distribution is inferred from them. The unreleased `0.1.0a3` version is not
evidence that acceptance has completed. The
[qualification record](milestone-2-subphase-6-verification.md) distinguishes
executed workloads, outstanding checks and later publication requirements.

## Milestone 3 - Assessment outputs, authentication and generative live adapters

**Phase 1 provider-neutral contracts are implemented; later phases remain planned.**
The authoritative [assessment output design](assessment-output-design.md)
consolidates output layouts, templates, generated schemas, authentication,
provider evidence, usage/reasoning retention and qualification. Phase 1 adds
standalone typed contracts with `contract_version=2`, deterministic ordered
JSON Schema generation, strict whole-output validation, explicit contract-based
member interpretation, and existing-result normalization. The three rubric
layouts and generative winner-only/legacy-false/tie-enabled pairwise forms are
available through the [Python contract API](python-api.md#standalone-assessment-output-contracts-milestone-3-phase-1).
The existing export R gate also checks Python-generated Phase 1 results;
all seven strict gates and the seven-configuration/49-execution policy remain.

The contract version is separate from document `schema_version`: RunSpec,
normalized results, prompts, mock wire protocol and durable artifacts remain
version 1. No new layout is accepted by the CLI, renderer, preparation or executor
submission path. Phase 2 must introduce explicit RunSpec v2 configuration,
rendering/preview and immutable ordered-schema/request commitments together
before enabling these contracts in durable execution. Existing grouped atomic
persistence and recovery remain covered by historical execution tests; the new
contract interpretation entry point itself performs no persistence or submission.
Provider translation/adapters, authentication and usage/reasoning/order-evidence
sidecars remain later work. Exact-head qualification is reported separately;
implemented code alone is not qualification evidence.

Continue through the following small, independently reviewable PRs; each PR
updates its tests and user documentation and passes applicable R acceptance.

1. **Output contracts and interpretation (Phase 1 implemented).** Generated
   provider-neutral JSON Schemas and local validators support `together`
   (default), `scores_first` and `explanations_first` without submission.
   Scores-only behavior, strict incompatible-setting preflight, trait-ID joins,
   complete-member validation and grouped sibling salvage are preserved.
   Future execution remains one assessment call per configured trait group.
   Normalization uses existing `RubricResult`; forced-choice `winner_id` alone
   retains normalized `tie: false` and historical explicit-false compatibility.
   Activation is limited to the explicit standalone Python contract boundary.
2. **Templates and examples.** Extend the sandboxed renderer/output instructions
   for all layouts. Omit empty exemplar sections, allow custom templates without
   an exemplar placeholder when none are selected, and preserve exact inclusion
   and order when supplied. Ship the six editable/rendered examples in the
   [prompt example design](assessment-prompt-examples.md); no two-stage calls.
3. **Shared credentials and offline diagnostics.** Add the typed credential
   resolver reused by adapters; mandatory `auth status`, its Python API,
   versioned JSON response and `--require` checks. Status must be offline, reveal
   no credential values or identities and never claim validity. Unsupported or
   unknowable cloud identity states remain explicit. Use SDK authentication;
   no credentials in RunSpec, immutable evidence, logs or exports.
4. **Provider evidence and schema transport.** Add request/observation-level usage
   and authorized optional reasoning evidence, strict native-schema translation,
   scoped capabilities, controlled explicit textual fallback, sanitized errors
   and exact nonsecret request/response commitments. Version affected public
   schemas independently; do not migrate or rewrite historical runs implicitly.
   Export usage separately from trait results, including unsuccessful attempts.
5. **Generative adapters, one provider/endpoint family per PR.** Implement OpenAI
   Responses/Chat, Anthropic Messages, Gemini Developer, Vertex Gemini, Together
   and local Ollama through maintained SDKs where suitable. Use the shared
   credential resolver and schema/evidence infrastructure. Each PR supplies
   credential-free fixtures for request construction, native schema subset,
   fallback, parsing, errors, usage, optional explanations/reasoning and supported
   generation, tier/cache controls. Preserve omitted generation values; reject
   known incompatibilities before submission. Implement model discovery and
   provider-specific `auth verify` only where documented suitable protected
   non-generative operations exist; unsupported mechanisms remain explicit.
   Unknown-model pass-through does not establish support.
6. **Companion exports and R acceptance.** Add streaming immutable `usage export`
   and authorized `reasoning export`, corresponding shared Python services,
   canonical JSONL/CSV and R-compatible views, consistent result/evidence cuts
   and result-to-evidence bridges. Verify all-attempt joins and aggregation
   without multiplying grouped usage through actual offline Python/CLI/R tests.
7. **Live diagnostics and qualification.** Deliver `providers smoke` with explicit model,
   endpoint and billable-call authorization, conservative enforced request/output
   controls and nonsecret feature-scoped qualification records. Test the command
   orchestration offline. Live runs remain separate and never run implicitly.

**Acceptance:** Deterministic offline tests cover all three layouts, scores only,
independent/grouped traits, ID/order mismatches, duplicates, unknown/malformed
members, category validity, complete explanations, historical outputs and valid
sibling salvage. Template/schema tests preserve protected content and optional
exemplar behavior, verify generated instructions and every provider translation,
and distinguish requested visible order from actually verified serialization.
Native schema support must not be silently weakened. Every generative adapter
passes the shared provider contract suite with synthetic credentials/fixtures.

Authentication tests forbid network access during status, cover blank/missing/
configured/unknown/unsupported mechanisms, Ollama's ordinary local no-auth case,
multiple required providers, JSON/exit codes, sanitized errors and consistency
with execution credential resolution. Assert no secret leakage through stdout,
stderr, artifacts, manifests, exports or qualification records. Verification
outcomes distinguish rejection, permission, network/service, rate limits and
inconclusive evidence; successful authentication does not qualify a model.

Usage/reasoning tests cover minimal/full capture, partial or missing usage,
provider-specific cache/reasoning semantics, grouped calls without duplication,
failed attempts and retries, explicit reasoning authorization and immutable
observation links. Assessment explanations remain separate from provider-exposed
reasoning summaries and raw evidence.

Required offline Python/CLI-to-R fixtures consume actual normalized generative
adapter outputs and companion usage/evidence exports, preserve character IDs,
all attempts, original/presented orientation, full probabilities/mappings and
missing values, and demonstrate joins without multiplying request usage.
Extend rather than replace the seven existing strict R/jsonlite gates. Missing
Rscript/jsonlite or failed assertions fail applicable required acceptance;
ordinary CI remains offline and credential-free.

Each supported generative provider has an opt-in live smoke workflow exercising
synthetic rubric and forced-choice requests, normalization, reported usage,
native structured output where supported, requested reasoning when available,
exact scope provenance and practical export/R consistency. An unexecuted plan,
SDK installation, configured credential or successful `auth verify` establishes
no `live_qualified` status. Record `discovered`, `implemented`, `fixture_tested`
and `live_qualified` separately for the tested endpoint/model/features. Do not
conduct paid calls as part of this design task.

## Milestone 4 - Native decision/score adapters

- Implement OpenAI Decisions and TypeSafe Jev with separate typed-question request builders. Reuse the Milestone 3 secure credential resolver, sanitized diagnostics and request/observation evidence infrastructure; generative output layouts and winner-only JSON must not be imposed on native endpoints without verifying their contracts.
- Map probability-weighted zero-based score indices to user numeric category coding, preserve the entire distribution and raw index, support pairwise choices.
- For both native endpoints, verify complete ordinal and choice distributions through Python normalization and R import, using differing and nonuniform numeric mappings. Preserve provider-selected categories when explicitly supplied, distribution-derived modes, mapped means, raw provider expected indices, and original/presented pairwise probability orientation as distinct evidence.
- Reject explanation generation, unsupported level counts and unsupported batch operations before sending.
- Explicitly validate generation applicability for each native score/decision endpoint and reject every inapplicable requested control before submission rather than dropping it or routing through a generative endpoint.

**Acceptance:** Synthetic fixtures confirm correct 2-10 level mapping and explicit errors for >=11 categories under independently verified endpoint limits; no arithmetic or semantics confusion between index score and rubric score; explanation request fails locally. Required offline Python-to-R fixtures cover OpenAI Decisions and TypeSafe Jev separately, retaining every probability and nonuniform code, raw provider index, selected category when available, mode, expected value, and score kind. Pairwise fixtures verify both original and presented probability orientation after reversal, without inferring an observed winner. Limits are taken from each endpoint's verified contract rather than assumed identical; the neutral assessment schema remains unrestricted by provider bounds.

Endpoint-specific offline tests prove inapplicable generative settings fail
locally and are never silently ignored.

## Milestone 5 - Native batches, managed bulk-live and caching

- Add native batch adapters for supported providers: OpenAI generative, Anthropic, Gemini Developer, Vertex Gemini, Together (subject to live qualification).
- Implement deterministic splitting, upload, durable submission ID registry, cancel/status/collect, partial outputs, reordered results and safe resumption.
- Deliver managed bulk-live mode for every live provider, including Ollama, Decisions and Jev.
- Implement `off`/`auto`/`explicit` cache policies and provider-specific availability checks, with true usage accounting where reported.
- Extend applicable authentication, generated output contracts, optional reasoning, request-level usage and qualification provenance to managed bulk-live and native batch. Preserve observation identities and all attempts, handle reordered/multiple observations without double counting usage, and retain journaled intent/reconciliation before retry. Never infer live feature support establishes native-batch support.
- Preserve generation parameter parity across live, managed bulk-live and native batch. Use the same validated run configuration and adapter mappings; report unsupported mode-specific combinations before submission and retain requested values in manifests, prepared payloads and recovery checks.
- Verify R-driven CLI submission/status/resumption/collection/export across separate processes using offline native-batch and managed bulk-live mocks. Retain durable IDs and artifacts across restarts, including partial collections, reordered outputs, repeated attempts, errors, and provider provenance.

**Acceptance:** Reproduce interruption/partial/duplicate-result fixture scenarios for every provider; no silent fallback across tiers/models; no fabricated cache savings; native-batch unsupported endpoints fail before network. Required R CLI tests complete submit/status/resume/collect/export workflows after process restarts against deterministic mocks, preserving original run/task/pair/request/attempt identities, partial/error records, original/presented probabilities, missing fields, and provenance in audit exports. Collection must not silently deduplicate audit attempts or reinterpret unfinished work as a winner. For actual provider batch acceptance, require manual paid qualifications.

Paired live/batch payload fixtures verify identical requested generation values
and omission semantics, including after recovery and collection.

## Milestone 6 - Catalog, R bridge, docs and prerelease qualification

- Implement generated `models.json` and `docs/model-support.md` with dated sources/qualifications; GitHub Action opens reviewable PR for catalog changes.
- Deliver a functional thin `rubricLLM` R package (provisional name) around the versioned CLI control contract using `processx`, with documented executable/virtual-environment discovery and configuration. Reuse Python services; do not implement a second scoring, normalization, or persistence engine in R.
- Expose the implemented validation, planning, prompt preview, rubric scoring, pairwise judging, run/status/resume, batch submission/status/collection/cancellation, and result-export operations through that wrapper. Provide installation and first-run documentation with actual synthetic end-to-end examples and clear diagnostics for missing executables/environments.
- Expose implemented `auth status`/`auth verify`, output-order controls, usage exports, authorized reasoning evidence and scoped qualification information through the same CLI contracts. Do not introduce R-side credential storage, authentication resolution, assessment logic or provider execution. Offline diagnostics remain offline when invoked from R.
- Supply generation controls through the same RunSpec used by Python and CLI. Test wrapper serialization of absent and explicit values, capability diagnostics and frozen-generation resume compatibility; do not add R-side provider defaults or arbitrary request bodies.
- Verify the actual `pairwiseLLM` source/version in required R integration tests. Demonstrate explicit successful-attempt selection, binary winner preparation for `build_bt_data()`, supported `object1`/`object2`/numeric-`result` workflows including deliberate tie handling, and rubric/probability import. Keep JudgmentLLM's audit IDs/provenance authoritative when conversion helpers generate their own occurrence IDs or timestamps.
- Evaluate `reticulate` as an optional secondary enhancement, implementing it only when justified by a demonstrated need; CLI/`processx` operation must not depend on `reticulate`, a resident Python process, or HTTP.
- Document install, authentication, cost estimation, privacy, resumption, exemplar scaling, scores vs probability expectations, and per-provider constraints.
- Conduct manual and prerelease provider qualification using limited-budget fixtures and a verification record.
- Evaluate PyPI Trusted Publishing, distribution testing and SemVer release process.

**Acceptance:** Users can install the thin `rubricLLM` package and complete documented first-run validation, preview, rubric, pairwise, status/resume, batch, and export workflows through `processx` against installed JudgmentLLM and offline mocks, without writing custom templates. Executable/environment discovery and documented control JSON/stdout/stderr/exit behavior are tested. Actual `pairwiseLLM` integration is installed, version-recorded, and exercised end to end in required R CI; ties, failures, repeated judgments, multiple attempts, leading-zero IDs, score kinds, full probabilities, and generated-versus-original provenance remain explicit. The published catalog distinguishes discovered/implemented/fixture-tested/live-qualified; Python, CLI and R workflows produce compatible normalized synthetic results; all CI passes; live qualifications remain separately opt-in and report exact version/date and costs.

## Milestone 7 - Local FastAPI interface (committed, late-stage)

**Sequence:** Start only after Milestones 0-6 establish stable Python/CLI contracts, the durable execution engine, and provider integration. Deliver this milestone before considering the planned full feature set complete. Keep HTTP support out of the required core installation; do not block earlier library/CLI prereleases on this work.

- Add an optional `judgmentllm[api]` extra providing FastAPI and a minimal ASGI server dependency. Expose `judgmentllm serve` with a default listener of `127.0.0.1` and a configurable local port; the core CLI and Python library must continue to work without FastAPI installed.
- Implement the versioned, locally bound endpoints defined in [`http-api-spec.md`](http-api-spec.md): health, validation, planning, run submission, status, resumption, batch collection/cancellation, and results retrieval/export.
- Reuse the **same** Pydantic run schemas, validation, execution services, SQLite registry, error categories, and normalized outputs as Python and CLI. Do not build a second scoring engine or new provider-specific logic for HTTP.
- Return a run ID promptly when submitting a potentially long operation; manage it outside the request lifecycle. Status polling, server restart, and resumption must preserve durable state and must not silently duplicate remote provider submissions.
- Keep this version **local, single-user and unauthenticated**: loopback interface only; refuse non-loopback binding; no permissive CORS, and validate Host/Origin and JSON content types to limit untrusted browser access. Do not add user accounts, a web UI, public network deployment, or multi-user scheduling.
- Document local startup/shutdown, example HTTP clients, limits of unauthenticated local services, troubleshooting, privacy/retention implications, and the difference between HTTP requests and provider-native Batch jobs.
- Add deterministic offline API tests using synthetic assessments and mock provider adapters, including invalid inputs, unsupported capabilities, malformed requests, cancellation, persistence, interrupted work, and JSON schema parity.
- Extend the interoperability fixtures to compare normalized results and exports across Python, CLI, HTTP, and R clients backed by the same services. R's normal CLI route must continue to work without HTTP; optional R HTTP use must preserve the same identifiers, distributions, outcomes, and missing-value semantics.

**Acceptance:** Installing the core package does not require FastAPI; installing `judgmentllm[api]` enables `judgmentllm serve` on loopback. An HTTP client can validate, plan, submit, poll, resume, collect, and export a synthetic run, with outputs matching the Python/CLI contracts. Required synthetic parity fixtures verify Python/CLI/HTTP/R results and derived exports through shared services, including failures, attempts, orientation, score kinds, probabilities, IDs, and provenance. Tests verify non-blocking submission, restart-safe state, no accidental double submissions, safe error responses with no secret leakage, and rejection of non-loopback binding. Offline CI passes without cloud credentials or billable calls. Internet-facing or multi-user use remains explicitly unsupported until separately designed and security-reviewed.

## Cross-language interoperability acceptance gate

Every relevant milestone that changes assessment schemas, normalized results, data exports, execution/shared services, or CLI interfaces must preserve the established Python-to-R contracts. The required baseline is the Milestone 1 Python-generated pairwise/rubric export smoke test in base R plus `jsonlite`, running in CI on Ubuntu/Python 3.11. It must remain required even before later R wrapper or execution features exist. Add stage-specific execution, adapter, batch, wrapper, and HTTP checks in their assigned milestones; mandatory actual `pairwiseLLM` integration begins in Milestone 6.

Applicable deterministic, synthetic, offline, credential-free CI fixtures must verify:

- Response/item/trait/pair/task/request/run/attempt identifiers, leading-zero character IDs, source/repetition/reversal metadata, order, and original versus presented positions.
- Observed winners, explicitly permitted ties, unsuccessful/partial records, repeated judgments, and every attempt without silent selection or deduplication. Analysis datasets require explicit attempt/outcome selection and remain distinct from audit exports.
- Original-orientation and presented-orientation choice probabilities, complete ordinal distributions, nonuniform numeric mappings, provider expected indices, and separate provider-selected category, modal category, and probability-weighted mean.
- Categorical assignments versus expected-score kinds, unrounded numeric scores, documented missing values, and complete available provider/model/execution/audit provenance.
- Requested generation provenance and omission semantics where applicable, kept distinct from capability evidence; changing generation settings cannot silently resume an existing frozen run.

The relevant R interoperability checks must actually consume Python-generated outputs. Missing required R/`jsonlite` dependencies, failed tests, or silent skips do not satisfy acceptance. A milestone cannot be accepted while an applicable required interoperability check fails. No R runtime or package becomes a compulsory dependency of the Python core; optional live provider qualification remains separate from required offline CI.

## Always-required review checklist

Apply stage-specific questions where relevant; the required baseline for the established export contracts applies to every milestone.

- What public contracts were changed? Does documentation and migration guidance match?
- Are provider request builders, adapters and parsers isolated and independently tested?
- Are tests offline by default, deterministic and non-billable?
- Are original inputs, IDs, probabilities, failures and cost/caching evidence preserved?
- Can the run be interrupted at each critical transition without losing data or double billing?
- Are provider credentials and student text absent from committed files and CI logs?
- Are unsupported combinations rejected before network calls?
- Does this milestone change an R-facing contract?
- Were actual Python-generated outputs tested in R?
- Are IDs, ordering, scores, probabilities, attempts, and provenance preserved?
- Does the required R interoperability CI check pass?
- Are any new R interoperability requirements or limitations documented?
