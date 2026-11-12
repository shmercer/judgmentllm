# JudgmentLLM

**Milestone 3, Phase 5C — Gemini Developer alongside Anthropic Messages, OpenAI Responses and Chat Completions production adapters. Development version 0.1.0a3; exact-head offline and separately authorized live qualification are recorded separately.**

Milestone 2 adds an [internal preparation foundation](docs/persistence-contracts.md)
for SQLite state, immutable source snapshots, indexed input access, complete run
manifests, generation commitments, and local executor ownership.
Public Python services and the versioned CLI provide complete validation, planning,
mock live execution, durable status, conservative recovery, explicit subset retries,
and immutable all-attempt exports. Production runs require explicit RunSpecV3;
see the [OpenAI user guide](docs/openai.md) and [Anthropic user guide](docs/anthropic.md), and [Gemini Developer guide](docs/gemini.md). Native serving qualification remains pending.
The [approved Milestone 2 plan](docs/milestone-2-execution-plan.md) records the boundaries.
Subphase 6 adds reproducible complete-run benchmarks, focused resource checks and
practical examples. The [qualification protocol](docs/milestone-2-subphase-6-verification.md)
identifies the external exact-source ledger that records acceptance, measured
workloads, exclusions and remaining conditions. Tooling availability alone does
not establish milestone acceptance.

JudgmentLLM provides typed, provider-independent contracts for ordinal rubric scoring and pairwise judgments. It loads synthetic assessments, validates category coding, prepares deterministic task groups, renders prompts, and normalizes supplied categorical or probability results. Synthetic mock execution, OpenAI, Anthropic and Gemini Developer generative adapters are available; model/feature qualification is scoped separately.

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

## Standalone provider evidence

`judgmentllm.evidence` provides strict version-1 contracts and pure offline
services for reported usage, observable JSON property order, reasoning retention
authorization and scoped provenance. Phase 4B attaches safe evidence to physical
submissions and observations through immutable companion receipts and registry
schema 2. Full and minimal capture retain safe metadata; reasoning text requires
an explicit immutable synthetic-fixture grant. Existing result and R exports
remain unchanged. See the [Python API](docs/python-api.md#internal-durable-evidence-inspection-phase-4b)
for internal inspection and compatibility. OpenAI production transport is additive;
companion exports remain deferred and live qualification requires separate consent.

## Offline credential status

```bash
judgmentllm auth status --json
judgmentllm auth status \
  --require openai,anthropic \
  --json
```

Status inspects only documented process-environment API-key sources. It never
contacts providers, initializes SDK clients, reads credential files or creates a
run/store. Configured means present, without authentication or model-access
verification. Vertex ADC remains unknown; ordinary local Ollama needs no
credential and is not probed. Required providers must be configured or not
required: unmet requirements exit 2 while retaining all status rows. See the
[Python API](docs/python-api.md#credential-diagnostics-phase-3-implemented) and
[CLI specification](docs/cli-spec.md#offline-authentication-status-phase-3-implemented).
OpenAI `auth verify` and `providers models` are explicit non-generative online
operations; neither command qualifies a serving model or runs automatically.

## Offline execution lifecycle

`examples/mock-run.yaml` declares the deterministic mock provider. The existing
OpenAI examples remain valid for offline validation/preview; execution rejects
unimplemented providers and never substitutes the mock. New production examples
in the OpenAI guide use V3; older V1/V2 examples do not acquire production execution.

```bash
judgmentllm validate --config examples/mock-run.yaml --json
judgmentllm plan --config examples/mock-run.yaml --json
judgmentllm run --config examples/mock-run.yaml --store /tmp/judgmentllm-example --json
judgmentllm runs list --store /tmp/judgmentllm-example --json
judgmentllm runs status --store /tmp/judgmentllm-example --run RUN_ID --json
judgmentllm resume --store /tmp/judgmentllm-example --run RUN_ID --json
judgmentllm results export --store /tmp/judgmentllm-example --run RUN_ID --format csv --view r --json
python examples/mock_lifecycle.py --store /tmp/judgmentllm-python-example
judgmentllm run --config examples/mock-pairwise.yaml --store /tmp/judgmentllm-pairwise-example --json
python examples/mock_recovery.py --output /tmp/judgmentllm-recovery-example
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
The installed-CLI recovery example inspects partial grouped outcomes, explicitly
retries failed members in later processes, and proves that older exports and
unsuccessful rows remain available. Use a fresh output directory for each example.
The [scale qualification guide](docs/scale-qualification.md) describes the complete
100,000-judgment mock workload, memory regressions, separate stress commands and
the distinction between required CI and offline qualification.

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

The [risk-based verification policy](docs/ci-optimization.md) selects checks by affected
behavior. Linux/Python 3.11 is the primary suite/coverage lane; comprehensive
cross-platform and strict R qualification remains available for releases,
weekly runs and explicit requests. The [local grid](docs/local-ci.md) is an
optional complete campaign, not a prerequisite for routine PRs.

## Optional dependencies and later milestones

Extras remain `openai`, `anthropic`, `google`, `together`, `ollama`, `typesafe`, `parquet`, `api`, and `all`. Installing them adds dependencies, not implemented provider operations. Milestone 2 provides mock execution and persistence; live adapters, batching, catalogs, the functional CLI-first `rubricLLM` wrapper in Milestone 6, and the committed optional local FastAPI interface in Milestone 7 follow the [implementation plan](docs/implementation-plan.md). R calls will operate on runs/files or sized task groups, not spawn one Python process per judgment. The wrapper, batch operations and real-provider services remain future work.

The repository is [shmercer/judgmentllm](https://github.com/shmercer/judgmentllm), licensed under [MIT](LICENSE), and not yet published to PyPI. The core does not implement Bradley–Terry fitting, adaptive pairing, or rubric calibration; these remain in `pairwiseLLM`.

Design references: [design specification](docs/design-spec.md), [decisions](docs/decisions.md), [contributor instructions](AGENTS.md), and [Milestone 1 verification](docs/milestone-1-verification.md).


Vertex AI Gemini single-run implementation and offline SDK fixtures are available
through explicit V3 configurations; see the [Vertex guide](docs/vertex.md).
ADC and a separate explicitly selected Vertex authorization-key source stay
isolated from Gemini Developer. Vertex has no live-qualified models/native routes;
the original [deferred proposal](docs/vertex-live-qualification-proposal.md) is
preserved. Separate complete offline candidate plans now support both API-key
and ADC modes for `gemini-3.1-flash-lite` at `global`, with four cases and zero
authorized calls per plan. Their read-only verifier requires the exact clean
source and an independently retained plan digest. No credentials or `gcloud`
are needed offline; account/key eligibility and live execution remain pending.
Ordinary auth status, planning and validation stay offline.

New prompt previews and preparations use readable Markdown and positional CJ
aliases. See the [renderer and compatibility guide](docs/renderer-revision-2.md)
and [exact offline writing-task examples](examples/rendering-revision2/).
Historical sealed requests retain their original bytes and source-ID wire
interpretation. Existing native qualification does not transfer to the revised
contract; new production requests use text until separately qualified (`native`
and strict unknown `auto` fail before submission).
