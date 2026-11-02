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

- Implement Pydantic models for ResponseItem, Trait, Level, Rubric, Exemplar, PairTask, RunSpec and normalized result types; version schemas.
- Validate ordered levels and explicit/default numeric coding, duplicate IDs, unknown config keys, and explanation capability requirements.
- Implement built-in score-only, score+explanations and pairwise templates; beginner plain-text placeholders and sandboxed advanced Jinja2; offline preview and schema validation.
- Implement stable deterministic task IDs and request grouping plan; independent scoring default; grouped and auto strategies explicit.
- Read CSV/JSONL input and YAML/JSON configs; write normalized JSONL, export CSV.

**Acceptance:** End-to-end *offline* tests build a multi-trait rubric from example files, render exact prompts, compute mapped expected scores from synthetic probability distributions, reject invalid category labels and missing placeholders, and retain all selected exemplars in original order.

## Milestone 2 - Execution engine and persistence (mock provider)

- Implement SQLite registry for runs, requests, tasks, batches, attempts and artifact hashes; immutable manifest on submit; resume states.
- Implement async bounded execution plus clean synchronous wrappers; bounded retry/backoff for transport failures; explicit completed-output retry.
- Implement `validate`, `plan`, `run`, `resume`, `runs list`, and `results export` CLI via shared library.
- Add cost/token estimate interface with unknown values preserved, prompt caching policies and capability validation interfaces.

**Acceptance:** Mock provider can process synthetic large streamed inputs with limited RAM; interrupted run resumes with no duplicate completed tasks; out-of-order and partial task results are correctly matched; ambiguous batch submission never blindly reissues a remote request; raw/prompt capture and redaction work.

## Milestone 3 - Generative live adapters

- Implement OpenAI Responses and Chat; Anthropic Messages; Gemini Developer; Vertex Gemini; Together AI; Ollama local, with maintained SDKs when available.
- Per-adapter offline fixtures validate request building, response parsing, errors, usage, optional explanations, tier/cache settings and output schemas.
- Implement provider model discovery where officially available; unknown-model pass-through with documented limits.

**Acceptance:** Each adapter passes the shared contract tests without API credentials and has an opt-in live smoke test plan. A validated score-only task and paired-choice task can be exercised against each available provider in prerelease qualification.

## Milestone 4 - Native decision/score adapters

- Implement OpenAI Decisions and TypeSafe Jev with separate typed-question request builders.
- Map probability-weighted zero-based score indices to user numeric category coding, preserve the entire distribution and raw index, support pairwise choices.
- Reject explanation generation, unsupported level counts and unsupported batch operations before sending.

**Acceptance:** Synthetic fixtures confirm correct 2-10 level mapping and explicit errors for >=11 categories; no arithmetic or semantics confusion between index score and rubric score; explanation request fails locally.

## Milestone 5 - Native batches, managed bulk-live and caching

- Add native batch adapters for supported providers: OpenAI generative, Anthropic, Gemini Developer, Vertex Gemini, Together (subject to live qualification).
- Implement deterministic splitting, upload, durable submission ID registry, cancel/status/collect, partial outputs, reordered results and safe resumption.
- Deliver managed bulk-live mode for every live provider, including Ollama, Decisions and Jev.
- Implement `off`/`auto`/`explicit` cache policies and provider-specific availability checks, with true usage accounting where reported.

**Acceptance:** Reproduce interruption/partial/duplicate-result fixture scenarios for every provider; no silent fallback across tiers/models; no fabricated cache savings; native-batch unsupported endpoints fail before network. For actual provider batch acceptance, require manual paid qualifications.

## Milestone 6 - Catalog, R bridge, docs and prerelease qualification

- Implement generated `models.json` and `docs/model-support.md` with dated sources/qualifications; GitHub Action opens reviewable PR for catalog changes.
- Document `reticulate` and CLI integration from R; verify minimal examples with synthetic data in R.
- Document install, authentication, cost estimation, privacy, resumption, exemplar scaling, scores vs probability expectations, and per-provider constraints.
- Conduct manual and prerelease provider qualification using limited-budget fixtures and a verification record.
- Evaluate PyPI Trusted Publishing, distribution testing and SemVer release process.

**Acceptance:** Users can follow first-run docs without writing custom templates; published catalog distinguishes discovered/implemented/fixture-tested/live-qualified; Python, CLI and R example produce compatible normalized synthetic results; all CI passes; qualifications report exact version/date and costs.

## Milestone 7 - Local FastAPI interface (committed, late-stage)

**Sequence:** Start only after Milestones 0-6 establish stable Python/CLI contracts, the durable execution engine, and provider integration. Deliver this milestone before considering the planned full feature set complete. Keep HTTP support out of the required core installation; do not block earlier library/CLI prereleases on this work.

- Add an optional `judgmentllm[api]` extra providing FastAPI and a minimal ASGI server dependency. Expose `judgmentllm serve` with a default listener of `127.0.0.1` and a configurable local port; the core CLI and Python library must continue to work without FastAPI installed.
- Implement the versioned, locally bound endpoints defined in [`http-api-spec.md`](http-api-spec.md): health, validation, planning, run submission, status, resumption, batch collection/cancellation, and results retrieval/export.
- Reuse the **same** Pydantic run schemas, validation, execution services, SQLite registry, error categories, and normalized outputs as Python and CLI. Do not build a second scoring engine or new provider-specific logic for HTTP.
- Return a run ID promptly when submitting a potentially long operation; manage it outside the request lifecycle. Status polling, server restart, and resumption must preserve durable state and must not silently duplicate remote provider submissions.
- Keep this version **local, single-user and unauthenticated**: loopback interface only; refuse non-loopback binding; no permissive CORS, and validate Host/Origin and JSON content types to limit untrusted browser access. Do not add user accounts, a web UI, public network deployment, or multi-user scheduling.
- Document local startup/shutdown, example HTTP clients, limits of unauthenticated local services, troubleshooting, privacy/retention implications, and the difference between HTTP requests and provider-native Batch jobs.
- Add deterministic offline API tests using synthetic assessments and mock provider adapters, including invalid inputs, unsupported capabilities, malformed requests, cancellation, persistence, interrupted work, and JSON schema parity.

**Acceptance:** Installing the core package does not require FastAPI; installing `judgmentllm[api]` enables `judgmentllm serve` on loopback. An HTTP client can validate, plan, submit, poll, resume, collect, and export a synthetic run, with outputs matching the Python/CLI contracts. Tests verify non-blocking submission, restart-safe state, no accidental double submissions, safe error responses with no secret leakage, and rejection of non-loopback binding. Offline CI passes without cloud credentials or billable calls. Internet-facing or multi-user use remains explicitly unsupported until separately designed and security-reviewed.

## Always-required review checklist

- What public contracts were changed? Does documentation and migration guidance match?
- Are provider request builders, adapters and parsers isolated and independently tested?
- Are tests offline by default, deterministic and non-billable?
- Are original inputs, IDs, probabilities, failures and cost/caching evidence preserved?
- Can the run be interrupted at each critical transition without losing data or double billing?
- Are provider credentials and student text absent from committed files and CI logs?
- Are unsupported combinations rejected before network calls?
