# Instructions for Codex and Contributors

## Read first

Read `docs/design-spec.md`, `docs/decisions.md`, `docs/data-contracts.md`, and the corresponding milestone in `docs/implementation-plan.md` before making a change. For changes to schemas, results, exports, execution services, or CLI interfaces, also read `docs/r-interoperability.md` and the cross-language acceptance gate in the implementation plan. For Milestone 7, also read `docs/http-api-spec.md`.

## Implementation principles

1. Prefer simple, readable Python and small cohesive modules over complex abstractions. Do not introduce LangChain, LiteLLM, or a broad LLM framework.
2. Use official, maintained provider Python SDKs when suitable. Add a narrow `httpx` adapter only where no suitable SDK is confirmed.
3. Keep provider-specific request formatting/parsing in adapter modules. Keep task definitions, validation, execution scheduling, persistence, and result normalization provider-neutral.
4. Keep prompt rendering separate from provider submission. Never accept arbitrary user-supplied JSON API request bodies.
5. Model all public configuration, tasks, and outputs with typed, documented schemas. Reject unknown or incompatible settings with actionable errors.
6. Preserve stable item, trait, pair, and attempt IDs. Do not assume provider batch results arrive in request order.
7. Never silently truncate text or exemplars, collapse categories, switch providers/models/priorities, or rescore failed completions.
8. Batch submissions must be recoverable and duplicate-safe: journal local intent, record remote IDs, and reconcile ambiguous submissions before retry.
9. Capture immutable run manifests, exact rendered prompts, raw responses, and provenance by default, with a documented privacy opt-out. Never persist API keys in manifests, raw logs, fixtures, or examples.
10. Use mocked, synthetic provider fixtures in all standard tests and CI. Real/paid API tests must be explicitly opt-in and budget-limited.
11. Treat model catalogs as informative and capability-tested, not hardcoded allowlists. Unknown model IDs may be passed to providers subject to validation.
12. Python>=3.11; support Linux, Windows, macOS; keep the core pure Python.
13. Before each PR: use the risk decision table in `docs/ci-optimization.md` and record the automatic plan. Run the checks required for the affected behavior; stronger verification is always allowed. Report passed, failed and deferred checks separately. Do not launch a full local grid for routine changes.
14. Implement one milestone at a time; avoid speculative features and unnecessary dependencies. Update tests and user documentation alongside every behavioral change.
15. Use SemVer once public packages are published. Version schemas and record backward-incompatible changes.
16. R interoperability is a local development acceptance condition. Every milestone changing schemas, normalized results, exports, execution services, or CLI interfaces must verify actual Python-generated outputs in R using deterministic offline synthetic tests in required local validation. Preserve IDs, original/presented ordering, outcomes, attempts, scores, complete probabilities/mappings, provenance, missing values, and character IDs with leading zeros. The established export baseline is mandatory: missing Rscript/jsonlite or failed assertions must fail required local Linux/Python 3.11 acceptance; do not silently skip it. GitHub Actions defers all actual R checks. A milestone cannot be accepted with failing applicable local interoperability tests. Add new capabilities only at their assigned milestone.

## Validation workflow

- Follow the single authoritative [risk decision table](docs/ci-optimization.md). Use `ci/check.py --profile fast` for development, `--profile pr --plan-only` to inspect selection, required local PR acceptance and the protected-base hosted functional workflow. Complete PR acceptance requires both applicable local and hosted evidence at the same source. CI, classification, test-selection and coverage-control changes are high risk. Unknown or ambiguous changes escalate; diff size never reduces risk.
- Local Linux/Python 3.11 is the primary coverage environment. Keep the 90% combined floor and the change-aware historical comparison locally. All actual R/jsonlite checks and resource measurements run locally only, including comprehensive verification. Missing required local R dependencies fail acceptance; hosted deferrals are not passed local gates. GitHub retains ordinary and serial correctness tests without coverage instrumentation or R execution. Full local/hosted matrices, repeated R campaigns and package/resource checks are required only where the risk policy calls for them.
- Do not duplicate hosted verification with a seven-lane local campaign. Complete qualification remains available with `--profile comprehensive`; releases also require the independent release-readiness, compatibility, scale and live-capability safeguards. No ordinary verification command authorizes provider calls or publication.
- For native VM work, follow `docs/local-ci.md`: preserve locks, isolated state, graceful shutdown, exact-source evidence, resource thresholds and the 180-second fixture deadline. Benchmark-only stages never establish qualification. Preserve historical failures accurately.

## Proposed tools

- Build: `pyproject.toml` + Hatchling; development: `uv`.
- Configuration: Pydantic 2 and `yaml.safe_load` with strict validation.
- Templates: Jinja2 `SandboxedEnvironment` with `StrictUndefined` and an explicitly limited context; no arbitrary Python execution.
- CLI: Typer; concurrency: stdlib `asyncio` and provider SDKs; SQLite metadata and JSONL/CSV outputs (optional Parquet).
- Tests/quality: pytest, pytest-cov, pytest-asyncio, Ruff, Pyright, Codecov; GitHub Actions.
- FastAPI is a committed, late Milestone 7 deliverable, but is packaged as an optional `[api]` extra. Keep HTTP thin and backed by existing Python services; local loopback only, with no public network or multi-user support in this milestone.

## R integration architecture

Keep JudgmentLLM standalone: Python library plus CLI, with canonical JSONL and documented R-compatible CSV as the primary cross-language contracts. R invokes whole task groups/runs/batches through the CLI, normally using `processx`; do not launch Python for each judgment. The thin `rubricLLM` R interface is a functional Milestone 6 deliverable, and `reticulate` is optional and secondary. `pairwiseLLM` consumes comparison files without embedded Python; statistical modeling stays in R. Ordinary R workflows require no HTTP server; optional local FastAPI remains the committed Milestone 7 deliverable. Keep pairwiseLLM optional for standard CI until Milestone 6; required baseline probability/mapping checks use jsonlite. Environment setup may install test dependencies, but interoperability test execution itself must remain offline and credential-free.

## Scope boundaries

No continuous-scoring scales; ordinal categories only. No statistical Bradley-Terry fitting, adaptive pair selection, linking, or rubric calibration in the Python core (these remain in `pairwiseLLM`). No general multi-user server, web application, desktop UI, arbitrary JSON pass-through, or remote public HTTP deployment. Local FastAPI belongs in Milestone 7, not earlier milestones.

## Public API governance

The names in `docs/cli-spec.md` and `docs/data-contracts.md` are draft contracts. Introduce or change public names deliberately, with tests and doc updates; do not silently diverge from the approved design.
