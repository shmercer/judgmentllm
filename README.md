# JudgmentLLM

**Milestone 1 — assessment contracts and offline prompts. Version 0.1.0a2.**

Milestone 2 Subphase 1 adds an [internal durable foundation](docs/persistence-contracts.md)
for SQLite state, immutable artifacts, hashing, and local executor ownership.
Execution and lifecycle commands remain unimplemented. The
[approved Milestone 2 plan](docs/milestone-2-execution-plan.md) records the next steps.

JudgmentLLM provides typed, provider-independent contracts for ordinal rubric scoring and pairwise judgments. It loads synthetic assessments, validates category coding, prepares deterministic task groups, renders prompts, and normalizes supplied categorical or probability results. Provider execution arrives in later milestones.

Python 3.11+, Linux, Windows, and macOS are supported. The core uses Pydantic 2, PyYAML, Jinja2, and Typer; pandas and PyArrow are not required.

The R architecture is CLI-first: call the standalone installed executable, normally with `processx`, and exchange canonical JSONL or R-compatible CSV files. `pairwiseLLM` consumes these files for statistical analysis in R. A functional thin `rubricLLM` wrapper for the run lifecycle belongs to Milestone 6; `reticulate` is optional and secondary. Ordinary R use requires no HTTP server. See [R interoperability](docs/r-interoperability.md) for the approved architecture and current offline examples.

## Install and preview

```bash
uv sync --locked --all-extras --group dev
uv run --offline --no-sync judgmentllm templates list
uv run --offline --no-sync judgmentllm templates preview --config examples/run.yaml --item-id essay_001 --trait organization
uv run --offline --no-sync judgmentllm templates preview --config examples/pairwise.yaml --pair-uid comparison_001_reverse
uv run --offline --no-sync judgmentllm templates preview --config examples/advanced.yaml --item-id essay_001
uv run --offline --no-sync judgmentllm templates init --preset rubric_scores_only --output rubric_prompt.txt
uv run --offline --no-sync python examples/offline.py
```

All bundled inputs are synthetic. Preview makes no network requests and needs no credentials. Example provider/model fields declare future intent; they do not establish provider support. The initial dependency installation may download packages. Use `uv sync --locked --no-dev` for core-only installation.

## Python preparation

```python
from judgmentllm import load_run_spec, iter_requests, preview_prompt

spec = load_run_spec("examples/run.yaml")
request = next(iter_requests(spec))  # lazy response × trait grouping
print(request.request_uid, request.trait_ids, request.exemplar_ids)
print(preview_prompt(spec, item_id="essay_001", trait_id="organization").text)
```

Independent scoring is the default. Fixed groups preserve their supplied order; auto grouping requires an explicit known trait-count limit. Planning is structural and provides no token/cost estimates or execution.

```python
from judgmentllm import load_run_spec, prepare_pairs
from judgmentllm.io import read_pairs

spec = load_run_spec("examples/pairwise.yaml")
assert spec.pairs is not None
source = next(read_pairs(spec.pairs))
schedule = list(prepare_pairs([source], repetitions=2, include_reversals=True, seed=41))
assert len({pair.pair_uid for pair in schedule}) == 4
```

Pair and repetition order stay fixed. A seed selects presentation orientation independently for each source occurrence/repetition; every reversal presents its exact opposite. Original pair IDs, occurrence IDs, repetition indices, and presentation metadata remain separate.

## Results and ordinal semantics

`normalize_rubric_result` accepts a provider-selected category, a complete ordered probability distribution, or both. Numeric category labels default to their numeric values; textual/mixed scales default to zero-based indices; complete explicit mappings override defaults. Invalid numeric orderings fail. Mixed numeric/text scales warn when a numeric label differs from its assigned index.

`category_label` records only provider selection. `modal_category_label` records the distribution-derived mode. `expected_value` uses the complete distribution and numeric mapping; `primary_score_kind` distinguishes `expected` from `category_code`. Original probabilities and provider-reported expected indices are retained. `NormalizationOptions.probability_sum_tolerance` controls the documented absolute sum tolerance, default `1e-8`, without renormalization.

See [the finalized data contracts](docs/data-contracts.md) and [public API examples](docs/python-api.md) for normalization and streaming JSONL/CSV operations. Nested result data survives CSV export as JSON cells. Library writers revalidate each current nested result before export. Derived R-compatible audit exports preserve presented orientation, all attempts, ties/failures, and score kinds; see [R interoperability](docs/r-interoperability.md) for character-safe import and explicit analysis selection.

## Templates and validation

Built-in presets cover rubric scores, scores with explanations, and pairwise forced choice. Simple templates use literal placeholders. Advanced Jinja2 is opt-in and supports a documented restricted syntax with rendering limits. Protected content blocks account for selected exemplars by ID, verifying completeness and order before returning a prompt. Omission or alteration fails clearly.

Output-schema validation is separate from rendering. Unknown configuration fields, incompatible presets, invalid categories, duplicate IDs, and known unsupported explanations fail locally. Unknown capabilities remain explicit; arbitrary model identifiers are not blocked by a catalog.

See [prompt guidance](docs/prompt-guidance.md) and [CLI documentation](docs/cli-spec.md). Default capture intent is `full`; `minimal` declares reduced future retention. Milestone 1 does not create manifests, raw-response stores, or persistent runs.

## Development and verification

Development uses uv 0.13.6 and the committed lockfile. Run from the repository root:

```bash
uv sync --locked --all-extras --group dev
uv lock --check
uv run --offline --no-sync ruff check .
uv run --offline --no-sync ruff format --check .
uv run --offline --no-sync pyright
uv run --offline --no-sync pytest
uv build --offline
uv run --offline --no-sync twine check --strict dist/*
uv run --offline --no-sync python scripts/smoke_install.py dist/*.whl
uv run --offline --no-sync python scripts/smoke_install.py dist/*.tar.gz
```

Use a writable `UV_CACHE_DIR` when the default cache is read-only. Tests are synthetic and offline. The coverage gate remains 90%; statement and branch coverage are reported. The installation helper creates clean environments and may download locked core dependencies; installed import, template, and preview checks run offline. Provider SDKs must be absent from these core installations.

CI retains the Milestone 0 Linux/Windows/macOS matrix on Python 3.11 and 3.15, lint/type checks, builds, clean installations, coverage artifacts, and optional Codecov upload. A required Ubuntu/Python 3.11 R round trip also checks synthetic file imports and full probability/mapping recovery with `jsonlite`; R compatibility is a standing requirement for later schema/export/CLI changes. See [repository setup](docs/repository-setup.md) for Codecov configuration.

## Optional dependencies and later milestones

Extras remain `openai`, `anthropic`, `google`, `together`, `ollama`, `typesafe`, `parquet`, `api`, and `all`. Installing them adds dependencies, not implemented provider operations. Milestone 2 adds mock execution and persistence; live adapters, batching, catalogs, the functional CLI-first `rubricLLM` wrapper in Milestone 6, and the committed optional local FastAPI interface in Milestone 7 follow the [implementation plan](docs/implementation-plan.md). R calls will operate on runs/files or sized task groups, not spawn one Python process per judgment. These future services and wrapper operations are not implemented by the current offline tools.

The repository is [shmercer/judgmentllm](https://github.com/shmercer/judgmentllm), licensed under [MIT](LICENSE), and not yet published to PyPI. The core does not implement Bradley–Terry fitting, adaptive pairing, or rubric calibration; these remain in `pairwiseLLM`.

Design references: [design specification](docs/design-spec.md), [decisions](docs/decisions.md), [contributor instructions](AGENTS.md), and [Milestone 1 verification](docs/milestone-1-verification.md).
