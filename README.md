# JudgmentLLM

**Milestone 2, Subphase 5 — mock execution and public lifecycle. Version 0.1.0a2.**

Milestone 2 adds an [internal preparation foundation](docs/persistence-contracts.md)
for SQLite state, immutable source snapshots, indexed input access, complete run
manifests, generation commitments, and local executor ownership.
Public Python services and the versioned CLI provide complete validation, planning,
mock live execution, durable status, conservative recovery, explicit subset retries,
and immutable all-attempt exports. Real provider adapters remain later milestones.
The [approved Milestone 2 plan](docs/milestone-2-execution-plan.md) records the boundaries.

JudgmentLLM provides typed, provider-independent contracts for ordinal rubric scoring and pairwise judgments. It loads synthetic assessments, validates category coding, prepares deterministic task groups, renders prompts, and normalizes supplied categorical or probability results. Synthetic mock execution is available; real provider execution arrives in later milestones.

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

## Offline execution lifecycle

`examples/mock-run.yaml` declares the deterministic mock provider. The existing
OpenAI examples remain valid for offline validation/preview; execution rejects
unimplemented providers and never substitutes the mock.

```bash
judgmentllm validate --config examples/mock-run.yaml --json
judgmentllm plan --config examples/mock-run.yaml --json
judgmentllm run --config examples/mock-run.yaml --store /tmp/judgmentllm-example --json
judgmentllm runs list --store /tmp/judgmentllm-example --json
judgmentllm runs status --store /tmp/judgmentllm-example --run RUN_ID --json
judgmentllm resume --store /tmp/judgmentllm-example --run RUN_ID --json
judgmentllm results export --store /tmp/judgmentllm-example --run RUN_ID --format csv --view r --json
python examples/mock_lifecycle.py --store /tmp/judgmentllm-python-example
```

Use the returned original run ID for `RUN_ID`. On Windows choose a local path,
for example `C:\JudgmentLLM-data`. Keep runtime stores outside synchronized folders.
`run` and `resume` block in their own process. JSON mode writes one control object
to stdout and separate JSONL diagnostics to stderr. A later process can inspect
or export without the original terminal or an HTTP server. Exports retain every
recorded attempt; select successful attempts explicitly for analysis.

Minimal capture omits source snapshots, prompts, payloads and raw responses;
authorized normalized explanations remain sensitive retained results. Recovery
requires unchanged original sources and authoritative observation evidence.
Uncertain submissions are never silently reissued. See [CLI controls](docs/cli-spec.md)
and [public lifecycle APIs](docs/python-api.md) for retry authorization and errors.

## Python preparation

```python
from judgmentllm import load_run_spec, iter_requests, preview_prompt

spec = load_run_spec("examples/run.yaml")
request = next(iter_requests(spec))  # lazy response × trait grouping
print(request.request_uid, request.trait_ids, request.exemplar_ids)
print(preview_prompt(spec, item_id="essay_001", trait_id="organization").text)
```

Independent scoring is the default. Fixed groups preserve their supplied order; auto grouping requires an explicit known trait-count limit. The original `plan_run()` remains structural. `estimate_run()` validates the complete
input and gives exact task/request counts with unavailable token/cost estimates explicit.

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

See [prompt guidance](docs/prompt-guidance.md) and [CLI documentation](docs/cli-spec.md).
The provider-free internal preparation service freezes assessment sources and
seals a complete manifest before submission can begin. Full capture retains
verified exact snapshots; minimal capture retains hashes, structural indexes,
and source references while requiring unchanged originals. Use a local runtime
store outside cloud-synchronized or network-mounted directories. See the
[Python preparation interface](docs/python-api.md#immutable-source-preparation-internal)
and [verification record](docs/milestone-2-subphase-2-verification.md).

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

CI runs Ubuntu/macOS on Python 3.11/3.15 and Windows on Python 3.12, with a complete
manual Windows 3.11 job. Quality checks, builds, clean installations, coverage
artifacts and optional Codecov remain required. Ubuntu/Python 3.11 requires all
seven strict R/jsonlite gates, including installed-CLI lifecycle acceptance. The
[local six-configuration grid](docs/local-ci.md) runs Python 3.11/3.12 on all three
platforms, with all seven R gates in each lane; it does not replace hosted 3.15. See [repository setup](docs/repository-setup.md) for Codecov configuration.

## Optional dependencies and later milestones

Extras remain `openai`, `anthropic`, `google`, `together`, `ollama`, `typesafe`, `parquet`, `api`, and `all`. Installing them adds dependencies, not implemented provider operations. Milestone 2 provides mock execution and persistence; live adapters, batching, catalogs, the functional CLI-first `rubricLLM` wrapper in Milestone 6, and the committed optional local FastAPI interface in Milestone 7 follow the [implementation plan](docs/implementation-plan.md). R calls will operate on runs/files or sized task groups, not spawn one Python process per judgment. The wrapper, batch operations and real-provider services remain future work.

The repository is [shmercer/judgmentllm](https://github.com/shmercer/judgmentllm), licensed under [MIT](LICENSE), and not yet published to PyPI. The core does not implement Bradley–Terry fitting, adaptive pairing, or rubric calibration; these remain in `pairwiseLLM`.

Design references: [design specification](docs/design-spec.md), [decisions](docs/decisions.md), [contributor instructions](AGENTS.md), and [Milestone 1 verification](docs/milestone-1-verification.md).
