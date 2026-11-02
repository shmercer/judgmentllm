# Milestone 1 implementation and verification

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
