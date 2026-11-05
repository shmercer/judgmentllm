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
13. Before each PR: run Ruff, Pyright, pytest with coverage, package build, and offline smoke tests. If checks cannot run, report exactly what was and was not verified.
14. Implement one milestone at a time; avoid speculative features and unnecessary dependencies. Update tests and user documentation alongside every behavioral change.
15. Use SemVer once public packages are published. Version schemas and record backward-incompatible changes.
16. R interoperability is a development acceptance condition. Every milestone changing schemas, normalized results, exports, execution services, or CLI interfaces must verify actual Python-generated outputs in R using deterministic offline synthetic tests in required CI. Preserve IDs, original/presented ordering, outcomes, attempts, scores, complete probabilities/mappings, provenance, missing values, and character IDs with leading zeros. The established export baseline is mandatory: missing Rscript/jsonlite or failed assertions must fail the Ubuntu/Python 3.11 check; do not silently skip it. A milestone cannot be accepted with failing applicable interoperability tests. Add new capabilities only at their assigned milestone.

## Validation workflow

- Before publication, commit the proposed head and run `python3 ci/local/grid.py --ref FULL_HEAD_SHA --mode check`. Require all six local configurations (Linux/Windows/macOS, Python 3.11/3.12) to pass in parallel, including quality, coverage, package/offline smokes and all seven strict R/jsonlite gates per configuration.
- Then require GitHub CI on the exact final PR head: Ubuntu/macOS Python 3.11/3.15 and Windows Python 3.12. Keep the complete Windows 3.11 job available through manual dispatch; local 3.11/3.12 checks do not replace hosted 3.15 qualification.
- Follow `docs/local-ci.md` for preparation, VM locks, isolation and graceful shutdown. Preserve resource thresholds (including the 180-second fixture deadline); retain exact-commit evidence and distinguish environment limitations from test failures.

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
