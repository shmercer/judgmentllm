# Milestone 1 implementation and verification

**Latest corrective-pass result:** 425 tests passed; 99.79% statement and 99.13% branch coverage. The R smoke test passed with installed pairwiseLLM/jsonlite. See the pre-merge hardening record below; the original implementation figures are retained as historical verification.

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
