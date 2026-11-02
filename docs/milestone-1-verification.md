# Milestone 1 implementation and verification

**Latest CLI-first architecture / mandatory R gate follow-up:** 428 tests passed; 99.79% statement and 99.13% branch coverage. Strict R/jsonlite checks passed locally, including optional-pairwiseLLM absence and negative regressions. GitHub CI for this follow-up remains pending; it has not been pushed. Earlier verification figures below are retained as historical records.

Date: 2026-11-02. Version: `0.1.0a2`. Schema version: integer `1`.
Branch: `codex/milestone-1-contracts-prompts`. Base: refreshed `origin/main`,
`62b8bcedfe594b841b1846f36f51b6472f2d4e0a` (merged Milestone 0 PR #1).
The initial working tree was clean. No push, merge, release, or provider call was performed.

## Implementation summary and public decisions

- Implemented strict provider-neutral assessment/run/result schemas, independent output validators, category coding, complete-distribution expectations, and pairwise outcomes.
- Provider selection and probability-derived mode have separate label/code fields. Original probabilities and raw expected indices remain intact. Configurable absolute sum tolerance defaults to `1e-8` and is recorded with probability results; nothing is renormalized.
- Mixed numeric/text labels use indices, warning on misleading numeric labels. Wholly numeric defaults and complete explicit mappings must be finite, unique, and increasing.
- Implemented safe location-aware YAML/JSON loading, streaming CSV/JSONL readers, normalized JSONL, and lossless nested CSV round trips, including large cells above the stdlib default field-size ceiling.
- Implemented lazy structural task/request groups and portable seeded pair repeats/reversals. Existing schedule provenance is never overwritten by the helper. No token/cost estimation or execution engine was introduced.
- Added installed prompt presets, strict simple placeholders, opt-in sandboxed Jinja, configurable rendering safeguards, and ID-based protected-content accounting. Incomplete, altered, duplicated, or reordered exemplars fail clearly.
- Added library preview and `templates list/init/preview`, synthetic examples, documented interfaces, locked dependencies, and extended clean-install smoke verification. Milestone 0 CI configuration is unchanged.

The finalized contracts are in [data-contracts.md](data-contracts.md), signatures and examples in [python-api.md](python-api.md), prompt restrictions in [prompt-guidance.md](prompt-guidance.md), and deliberate draft resolutions in [decisions.md](decisions.md).

## Exact verification commands and results

Commands ran from the repository root with `UV_CACHE_DIR=/tmp/judgmentllm-uv-cache`.
The environment was Linux x86_64, CPython 3.12.3, uv 0.13.6, pytest 9.2.0,
Pydantic 2.14.2, Jinja2 3.1.6, and PyYAML 6.0.3.

| Command | Result |
|---|---|
| `uv lock` | Updated the approved dependency declarations and committed lock resolution; 93 packages. |
| `uv sync --locked --all-extras --group dev` | Passed; installed editable 0.1.0a2 and required dependencies. |
| `uv lock --check` | Passed; 93 packages, no stale metadata. |
| `uv run --offline --no-sync ruff check .` | Passed. |
| `uv run --offline --no-sync ruff format --check .` | Passed; 32 files already formatted. |
| `uv run --offline --no-sync pyright` | Passed; zero errors, warnings, or information diagnostics. |
| `uv run --offline --no-sync pyright examples/offline.py` | Passed; zero diagnostics. |
| `uv run --offline --no-sync pytest` | Passed; **341 tests**, 2.61 seconds for the final full run. |
| `uv run --offline --no-sync coverage json -o /tmp/judgmentllm-m1-coverage.json` | Passed; separate statement/branch totals extracted below. |
| `uv build --offline` | Passed; wheel and source distribution built. |
| `uv run --offline --no-sync twine check --strict dist/judgmentllm-0.1.0a2*` | Both artifacts passed. |
| `uv run --offline --no-sync python scripts/smoke_install.py dist/judgmentllm-0.1.0a2-py3-none-any.whl dist/judgmentllm-0.1.0a2.tar.gz` | Both clean core-only installations passed import, version, metadata, typing resource, all preset resources, normalization, CLI list/init, and offline preview checks outside the checkout. Optional provider/Parquet/API dependencies were absent. |
| `git diff --check` | Passed. |

Initial dependency resolution and a sandboxed clean-install attempt encountered DNS restrictions. Re-running installation with authorized network access succeeded. Only dependency installation permits network access; tests and installed-package checks remain offline. No automatic approval rejection occurred.

Wheel/source archive inspection confirmed all three prompt text resources; the source distribution also includes the offline examples. The unchanged CI matrix covers Linux, Windows, and macOS on Python 3.11 and 3.15, but that remote matrix was not executed in this local session. No real-provider qualification was attempted.

### Coverage

| Measure | Covered / total | Percentage |
|---|---:|---:|
| Statements | 1,319 / 1,322 | **99.77%** |
| Branches | 529 / 534 | **99.06%** |
| Combined coverage gate | 1,848 / 1,856 | **99.57%** |

The existing 90% gate remains enabled. Models, results, prompts, I/O, and preview have 100% statement and branch coverage; remaining uncovered paths are in CLI/configuration/planning diagnostics. Coverage XML is produced by pytest for the preserved CI artifact/Codecov workflow.

## Runnable offline examples

```bash
uv run --offline --no-sync judgmentllm --version
uv run --offline --no-sync judgmentllm templates list
uv run --offline --no-sync judgmentllm templates preview --config examples/run.yaml --item-id essay_001 --trait organization
uv run --offline --no-sync judgmentllm templates preview --config examples/pairwise.yaml --pair-uid comparison_001_reverse
uv run --offline --no-sync judgmentllm templates preview --config examples/advanced.yaml --item-id essay_001
uv run --offline --no-sync python examples/offline.py
```

All commands passed. Preview emits explicit unknown-capability warnings and complete prompts. The Python example reports a mapped expectation of `6.05`, null provider selection, modal category `3`, and four distinct scheduled occurrences for two repetitions with reversals. See [python-api.md](python-api.md) for short rubric preparation, pair preparation, normalization, and JSONL/CSV interfaces.

## Limitations and Milestone 2 handoff

- No unresolved Milestone 1 product decisions remain. Documented choices include ID-only pair references, unscheduled inputs for pair expansion, explicit custom templates for ties, and no abstention contract yet.
- Advanced templates customize layout around complete protected blocks; nested loops and arbitrary Jinja operations are intentionally unavailable. Safeguards fail instead of truncating; their positive limits are configurable.
- Plans are structural. Pairwise request IDs contain references and presentation rather than referenced response text. Milestone 2 must capture exact input/prompt/payload hashes before submission, preserve structural IDs, and distinguish run/request/task/attempt identities.
- Readers are lazy: complete validation requires consuming input. Duplicate detection retains IDs, and each current record must fit memory. Writers can leave partial files and are not transactional journals.
- Execution mode/cache/tier/concurrency/capture fields express future intent. Retry, batch-operation, budget, token/cost estimation, provider clients, persistence, catalogs, API/R interfaces, and statistical models remain outside this milestone. Unknown model IDs remain valid strings without support claims.
- For Milestone 2, build the SQLite registry and mock async execution around these models and iterators; implement immutable manifests, prompt/raw capture, bounded transport retries, cancellation/resumption, out-of-order matching, and duplicate-safe submission. Add the execution CLI through shared services, keeping probability normalization and output validation independent.

## Files created

- `docs/python-api.md`
- `examples/advanced.yaml`
- `examples/advanced_prompt.j2`
- `examples/criteria.yaml`
- `examples/exemplars.yaml`
- `examples/offline.py`
- `examples/pairs.jsonl`
- `examples/pairwise.yaml`
- `src/judgmentllm/config.py`
- `src/judgmentllm/io.py`
- `src/judgmentllm/models.py`
- `src/judgmentllm/planning.py`
- `src/judgmentllm/preview.py`
- `src/judgmentllm/prompts.py`
- `src/judgmentllm/results.py`
- `src/judgmentllm/templates/pairwise_forced_choice.txt`
- `src/judgmentllm/templates/rubric_scores_only.txt`
- `src/judgmentllm/templates/rubric_scores_with_explanations.txt`
- `tests/test_config.py`
- `tests/test_io.py`
- `tests/test_models.py`
- `tests/test_planning.py`
- `tests/test_preview.py`
- `tests/test_prompts.py`
- `tests/test_results.py`
- `docs/milestone-1-verification.md`

## Files modified

- `README.md`
- `docs/cli-spec.md`
- `docs/data-contracts.md`
- `docs/decisions.md`
- `docs/design-spec.md`
- `docs/implementation-plan.md`
- `docs/prompt-guidance.md`
- `examples/rubric.yaml`
- `examples/run.yaml`
- `pyproject.toml`
- `scripts/smoke_install.py`
- `src/judgmentllm/__init__.py`
- `src/judgmentllm/cli.py`
- `tests/test_cli.py`
- `tests/test_package.py`
- `uv.lock`

## Pre-merge hardening and R interoperability corrective pass

The pass began on the existing feature branch at `0b4c829`, matching PR #2 and its tracking branch, with a clean working tree. It adds no Milestone 2 execution, provider adapter, persistence, fitting, dependency, or schema-version changes. No push, merge, or publication was performed.

### Implemented corrections

- All library normalized JSONL/CSV writers and the new R CSV views reconstruct each record from current deep field data immediately before serialization. Nested mutations, invalid `model_copy(update=...)`, copied unknown fields, invalid IDs/outcomes/presentation/distributions/codes/scores, and validator-induced repairs are rejected. The generic typed-record JSONL writer follows the same invariant, including aliased and root models. Direct `.model_dump*()` calls remain lower-level Pydantic operations.
- Added public `export_pairwise_r_csv(path, records, *, na_string="NA")` and `export_rubric_r_csv(...)` functions. These are streaming, provider-independent all-attempts audit views; they retain ties, failures, repeated/reversed occurrences, attempts, score distinctions, probability arrays, and provenance. Canonical formats remain unchanged. A configured missing marker cannot collide silently with an exported value.
- Presented `ID1/ID2` (`A_id/B_id` aliases), observed `better_id`, numeric `result`, `winner_pos`, and presented probabilities are separate from original left/right fields. No winner is inferred. The R example selects explicit successful run/task/attempt triples and retains an audit copy before making binary or tie-capable model inputs.
- Built-in prompts prefer `first_response/second_response`; legacy aliases keep their existing presented-position behavior. A reversal regression covers prompt order, result positions/winner, and both probability orientations together.
- OpenAI Decisions preflight enforces its independently documented 2–10 score-level range even if a caller supplies a wider profile. Other endpoints do not inherit that limit; TypeSafe documentation independently establishes an upper ten-criteria bound and recommends at least two. No Jev adapter or unqualified identical-limit assumption was added.
- Expanded the Milestone 2 acceptance contract: capture immutable original-input, rendered-prompt, and exact-provider-payload hashes before submission; detect changed references on resume; never blindly reuse submission identity for changed content; preserve original JudgmentLLM task/pair/request/attempt IDs. R interoperability remains ongoing, with the fuller bridge/wrapper in Milestone 6.

Full derived schemas, representative CSV rows, missing-value/type conventions, base R imports, probability recovery, and source-qualified pairwiseLLM examples are in [r-interoperability.md](r-interoperability.md). The runnable files are `examples/export_r.py` and `examples/r_interoperability.R`.

### Corrective-pass commands and results

Commands ran from the repository root with `UV_CACHE_DIR=/tmp/judgmentllm-uv-cache`, using Linux/CPython 3.12.3 and the existing locked environment. Dependencies and `uv.lock` are unchanged.

| Exact command | Result |
|---|---|
| `uv run --offline --no-sync ruff check .` | Passed. |
| `uv run --offline --no-sync ruff format --check .` | Passed. |
| `uv run --offline --no-sync pyright` | Passed, zero diagnostics. |
| `uv run --offline --no-sync pyright examples/export_r.py` | Passed, zero diagnostics. |
| `uv lock --check` | Passed, 93 packages. |
| `uv run --offline --no-sync pytest` | **425 passed**, 3.86 seconds in the complete coverage run. |
| `uv run --offline --no-sync coverage json -o /tmp/judgmentllm-m1-hardening-coverage.json` | Passed; separated totals below. |
| `uv build --offline` | Wheel and source distribution built. |
| `uv run --offline --no-sync twine check --strict dist/judgmentllm-0.1.0a2*` | Both artifacts passed. |
| `uv run --offline --no-sync python scripts/smoke_install.py dist/judgmentllm-0.1.0a2-py3-none-any.whl dist/judgmentllm-0.1.0a2.tar.gz` | Both clean core-only installations passed, including both R exporters, invalid-mutated-result rejection, all presets, and offline preview. Dependency installation used authorized network access; package checks were offline. |
| `uv run --offline --no-sync python examples/export_r.py --output /tmp/judgmentllm-m1-r` | Six pairwise and three rubric audit rows exported, plus canonical normalized JSONL. |
| `Rscript --vanilla examples/r_interoperability.R /tmp/judgmentllm-m1-r` | Passed: six audit rows, four explicitly chosen observations, three binary wins, one retained tie, and two selected rubric outcomes; optional pairwiseLLM builder and jsonlite checks also passed. No fitting/network calls. |
| `git diff --check` | Passed. |

| Coverage measure | Covered / total | Percentage |
|---|---:|---:|
| Statements | 1,414 / 1,417 | **99.79%** |
| Branches | 567 / 572 | **99.13%** |
| Combined gate | 1,981 / 1,989 | **99.60%** |

The unchanged coverage gate remains 90%. New R exports and hardened I/O have 100% statement and branch coverage. The existing cross-platform CI and Codecov workflow files are unchanged; the corrected commits have not been pushed, so that remote matrix was not rerun during this pass.

### R source qualification and local verification

The published pairwiseLLM source inspected is commit `3cf49a9a2559f341e30f2e77e8df2ad4883ab7f5` on its `master` branch. Source-pinned links for `build_bt_data`, `fit_bt_model`, and `build_btl_results_data` are recorded in [r-interoperability.md](r-interoperability.md). The binary adapter drops missing winners, including ties; direct sirt can accept ties, while the other documented wrappers reject them. The BTL adapter creates new pair occurrence IDs and synthetic epoch-based timestamps; its output is not a lossless JudgmentLLM provenance import.

Local smoke environment: R 4.6.2 (2026-10-29), pairwiseLLM 1.6.0 with installed RemoteSha `4c4c01453077f90e987040c6a3c3d3cd711c6f21`, jsonlite 2.0.0, sirt 4.2.133, and BradleyTerry2 1.1.3. Published-source inspection and installed-package smoke checks are distinct evidence: both builders were exercised locally, but no statistical model was fitted. All R-side import/builder/JSON smoke checks requested for this environment completed; no R dependency was added to Python.

### Remaining limits

CSV is untyped: R callers must import identifiers as character strings to preserve leading zeros. Choose a different missing marker if a literal value collides. R-friendly CSVs are derived views; retain canonical audits and explicitly selected-attempt policies. Source builders can replace identity/timestamp information, so keep a row crosswalk and original records. Writers remain streaming utilities and may leave a valid prefix/header on failure; no transactional persistence was added. No local checks establish live provider qualification or substitute for the preserved remote cross-platform CI.

### Files changed in this corrective pass

- `README.md`
- `docs/data-contracts.md`
- `docs/decisions.md`
- `docs/design-spec.md`
- `docs/implementation-plan.md`
- `docs/milestone-1-verification.md`
- `docs/prompt-guidance.md`
- `docs/provider-matrix.md`
- `docs/python-api.md`
- `docs/r-interoperability.md`
- `examples/export_r.py`
- `examples/r_interoperability.R`
- `scripts/smoke_install.py`
- `src/judgmentllm/__init__.py`
- `src/judgmentllm/io.py`
- `src/judgmentllm/planning.py`
- `src/judgmentllm/r_exports.py`
- `src/judgmentllm/templates/pairwise_forced_choice.txt`
- `tests/test_export_validation.py`
- `tests/test_planning.py`
- `tests/test_prompts.py`
- `tests/test_r_exports.py`

## CLI-first architecture and mandatory R acceptance follow-up

This focused follow-up began at approved/pushed commit `c9e2905` on `codex/milestone-1-contracts-prompts`, with a clean working tree. It changes architecture documentation, future milestone acceptance criteria, the Ubuntu R CI steps, and the minimum R test assertions needed for that gate. Existing exports, Python implementation, dependencies/lockfile, package/schema versions, and milestone order are unchanged. No Milestone 2 execution commands or R wrapper were implemented. No push, merge, or publication was performed during this follow-up.

### Architecture recorded

JudgmentLLM remains a standalone Python library and CLI. Canonical JSONL and documented R-compatible CSV are the primary cross-language contracts. R normally uses `processx` to invoke the CLI for complete run operations, task groups, or batches, rather than embedding Python or spawning once per judgment. A functional thin R interface, provisionally `rubricLLM`, belongs to Milestone 6 and will cover configuration, validation, planning, submission, status, resumption, and collection. `pairwiseLLM` consumes comparison files for statistics in R. `reticulate` remains optional and secondary. Ordinary local R use needs no HTTP server; optional local FastAPI remains the committed Milestone 7 deliverable.

The [CLI spec](cli-spec.md) now documents the future versioned control envelope, `--json`, command versus run statuses, absolute artifact paths, structured errors, JSON stdout and JSON Lines diagnostics/progress on stderr, noninteractive operation, and exit codes `0/1/2/3/130`. It requires independent-process status/resume/collection via shared Python services and durable IDs/files. These are explicit Milestone 2+ implementation contracts, not commands added by this follow-up.

### Standing gate and milestone-specific acceptance

The exact [Cross-language interoperability acceptance gate](implementation-plan.md#cross-language-interoperability-acceptance-gate) appears immediately before the review checklist and is also required by `AGENTS.md`. Every relevant schema/result/export/service/CLI change must preserve IDs, orientations, observed outcomes, all attempts, full distributions/mappings, selected/modal/expected distinctions, provenance, missing values, and leading-zero character IDs through actual Python-to-R files. Applicable deterministic synthetic tests must run offline in required CI without credentials/paid calls, and failures or missing prerequisites cannot be accepted as silent skips. Existing export verification is a mandatory baseline; new features are tested only as their milestones arrive.

| Milestone | Added acceptance requirement |
|---|---|
| 1 | Actual Python-generated pairwise/rubric files imported in R, required JSON probability/mapping recovery, IDs/types/missing values/attempts/reversals/outcomes and score kinds preserved. |
| 2 | R invokes mock execution through versioned noninteractive CLI controls; independent-process status/resume/export, durable IDs/artifacts, stderr diagnostics, interruption/resumption without duplicate completed judgments, and immutable input/prompt/payload hashes. |
| 3 | Offline fixtures from every generative adapter produce the established normalized/R-facing contracts, including errors and missing provider fields. |
| 4 | Separate Decisions/Jev fixtures preserve complete distributions, nonuniform mapped expectations, raw provider indices, selected/modal/expected distinctions, and pair probability orientation. |
| 5 | R-driven submission/status/resume/collection/export across process restarts, preserving partial/error records, identities, attempts, and provenance using mocks. |
| 6 | Functional CLI-first `rubricLLM`/`processx` lifecycle interface, executable/environment discovery, installation/first-run examples, actual version-recorded pairwiseLLM integration in required tests, and synthetic end-to-end workflows. Reticulate is only an optional justified enhancement. |
| 7 | Normalized-result parity across Python, CLI, HTTP, and R-facing exports through shared services; retain optional installation and local-only HTTP security. |

The review checklist now asks all five requested R-contract/testing/preservation/CI/documentation questions, applying stage-specific checks when relevant while keeping the established export baseline mandatory.

### Required Ubuntu CI configuration

Exactly three required steps were added to the existing `baseline` job and are conditioned on `matrix.os == 'ubuntu-latest' && matrix.python == '3.11'`. The six-entry Python matrix and all previous steps, including lint, formatting, Pyright, pytest/coverage, builds, metadata, clean installations, artifacts, and Codecov, are otherwise unchanged.

1. Set up R 4.6.2 with official `r-lib/actions/setup-r` pinned to `f9a764fea8d5c63df6ef9a5c7795bf7deb5d7e05` (`v2`), with public RSPM disabled.
2. Install only jsonlite 2.0.0 from the immutable CRAN-mirror source commit `2559608891cc42b9f857a5a0aff231738a670576`; confirm namespace and exact version. Its runtime dependency is base R's `methods`. No pairwiseLLM, Stan, processx, or R wrapper installation is required for this baseline.
3. Under `set -euo pipefail`, generate actual synthetic files via the configured uv environment in `$RUNNER_TEMP/judgmentllm-r`, then run `Rscript --vanilla examples/r_interoperability.R` with `JUDGMENTLLM_R_STRICT=1`.

There is no `continue-on-error` or missing-runtime fallback on these steps. Missing Rscript fails invocation/setup; missing jsonlite raises a fatal error in strict mode before any success message. Full probability, numeric mapping, and presentation-order JSON assertions execute in strict mode. Optional local examples may still report JSON recovery pending without jsonlite; that mode does not satisfy CI acceptance. PairwiseLLM adapter checks remain optional until Milestone 6 and are independent of the required file-contract assertions. Dependency setup may download tools/packages; test execution itself is offline and credential-free.

### Exact local verification and outcomes

Commands ran at repository root with `UV_CACHE_DIR=/tmp/judgmentllm-uv-cache` on Linux/CPython 3.12.3. Local R is 4.6.2, jsonlite 2.0.0, and optional pairwiseLLM 1.6.0.

| Command | Result |
|---|---|
| `uv run --offline --no-sync ruff check .` | Passed. |
| `uv run --offline --no-sync ruff format --check .` | Passed; 38 files formatted. |
| `uv run --offline --no-sync pyright` | Passed, zero errors/warnings/information diagnostics. |
| `uv run --offline --no-sync pytest` | **428 passed**, 4.87 seconds in the complete coverage run. |
| `uv run --offline --no-sync coverage json -o /tmp/judgmentllm-r-gate-coverage.json` | Passed; statements 1,414/1,417 (**99.79%**), branches 567/572 (**99.13%**), combined **99.60%**, unchanged 90% gate. |
| `uv lock --check` | Passed, 93 packages; dependency declarations and lockfile unchanged. |
| `uv build --offline` | Wheel and source distribution built. |
| `uv run --offline --no-sync twine check --strict dist/judgmentllm-0.1.0a2*` | Both artifacts passed. |
| `uv run --offline --no-sync python scripts/smoke_install.py dist/judgmentllm-0.1.0a2-py3-none-any.whl dist/judgmentllm-0.1.0a2.tar.gz` | Both clean core-only installations passed; only dependency installation allowed network access, package checks were offline. |
| `uv run --offline --no-sync python examples/export_r.py --output /tmp/judgmentllm-r-gate` | Six pairwise and three rubric audit rows generated by the actual Python export functions. |
| `JUDGMENTLLM_R_STRICT=1 Rscript --vanilla examples/r_interoperability.R /tmp/judgmentllm-r-gate` | Passed all required schema/type/missing/ID/order/outcome/attempt/score/probability/mapping assertions. Log includes `Strict mode: required jsonlite checks completed`. Optional pairwiseLLM builders also passed. |
| `uv run --offline --no-sync pytest tests/test_r_exports.py -q --no-cov -k strict_r_mode` | Three negative regressions passed: strict missing-jsonlite failure, missing required `p_first` column failure, and incorrect probability JSON values failure. Each asserts a nonzero R exit and absence of the smoke-success message. |
| `JUDGMENTLLM_R_STRICT=1 Rscript --vanilla /tmp/judgmentllm-r-without-pairwise.R examples/r_interoperability.R /tmp/judgmentllm-r-gate` | Passed with only the optional pairwiseLLM namespace lookup masked as unavailable; actual jsonlite recovery and required assertions still executed. |
| `git diff --check` | Passed. |
| `git diff --exit-code -- src examples/export_r.py pyproject.toml uv.lock` | Passed; no Python implementation, export generator, dependency, or lock changes. |

Local YAML inspection compared the new workflow against the approved head: the same single matrix job/six variants remain, exactly three R steps target only Ubuntu/Python 3.11, all prior step definitions are unchanged, strict mode is set, temporary files use RUNNER_TEMP, and the required R invocation has no failure suppression. This is workflow inspection plus local execution, not a GitHub-hosted CI run.

### Limitations and Milestone 2 handoff

The new workflow has **not** been pushed or executed on GitHub during this follow-up. GitHub CI remains pending and is not claimed to pass. Local tests exercised the required strict script and failure paths; GitHub setup of the pinned R binary/package must still succeed in the Ubuntu/Python 3.11 job. No expanded Windows/macOS/Python-version R setup was added. Branch protection settings were not changed; failure propagates through the existing baseline job.

There are no unresolved architecture questions blocking Milestone 2. Implement its documented control envelope/command-specific data schemas, shared durable services, independent-process lifecycle tests, content-hash guarantees, and progressive R-facing operations at that milestone. The wrapper/discovery layer remains Milestone 6; HTTP remains Milestone 7. No execution CLI, wrapper, provider adapter, statistical fitting, or persistent engine was added here.

### Files changed in this follow-up

- `.github/workflows/ci.yml`
- `AGENTS.md`
- `README.md`
- `docs/cli-spec.md`
- `docs/decisions.md`
- `docs/design-spec.md`
- `docs/implementation-plan.md`
- `docs/milestone-1-verification.md`
- `docs/r-interoperability.md`
- `examples/r_interoperability.R`
- `tests/test_r_exports.py`
