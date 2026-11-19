# JudgmentLLM

**Implemented: durable mock execution, OpenAI Responses/Chat Completions, Anthropic Messages, Gemini Developer and Vertex AI generative adapters, and native OpenAI Decisions, TypeSafe Jev and Gemini Decisions. Development version 0.1.0a3.**

Milestone 2 adds an [internal preparation foundation](https://github.com/shmercer/judgmentllm/blob/main/docs/persistence-contracts.md)
for SQLite state, immutable source snapshots, indexed input access, complete run
manifests, generation commitments, and local executor ownership.
Public Python services and the versioned CLI provide complete validation, planning,
mock live execution, durable status, conservative recovery, explicit subset retries,
and immutable all-attempt exports. Generative production runs require RunSpec V3.
see the [OpenAI](docs/openai.md), [Anthropic](docs/anthropic.md),
[Gemini Developer](docs/gemini.md) and [Vertex](docs/vertex.md) guides. Historical
live records do not qualify the current presentation revision or every model.
The [approved Milestone 2 plan](https://github.com/shmercer/judgmentllm/blob/main/docs/milestone-2-execution-plan.md) records the boundaries.
Subphase 6 adds reproducible complete-run benchmarks, focused resource checks and
practical examples. The [qualification protocol](https://github.com/shmercer/judgmentllm/blob/main/docs/milestone-2-subphase-6-verification.md)
identifies the external exact-source ledger that records acceptance, measured
workloads, exclusions and remaining conditions. Tooling availability alone does
not establish milestone acceptance.

JudgmentLLM provides typed, provider-independent contracts for ordinal rubric scoring and pairwise judgments. It validates category coding, prepares deterministic task groups, renders prompts, and normalizes categorical or probability results. Bundled assessment inputs and standard verification are synthetic and offline.

Python 3.11+, Linux, Windows, and macOS are supported. The core uses Pydantic 2, PyYAML, Jinja2, and Typer; pandas and PyArrow are not required.

The R architecture is CLI-first: call the standalone installed executable, normally with `processx`, and exchange canonical JSONL or R-compatible CSV files. `pairwiseLLM` consumes these files for statistical analysis in R. A functional thin `rubricLLM` wrapper for the run lifecycle belongs to Milestone 6; `reticulate` is optional and secondary. Ordinary R use requires no HTTP server. See [R interoperability](https://github.com/shmercer/judgmentllm/blob/main/docs/r-interoperability.md) for the approved architecture and current offline examples.

## Providers, endpoints and execution modes

Implemented adapters have offline SDK/lifecycle fixtures; that does not establish
live acceptance of an arbitrary model, account, control or feature. `live` is the
single-run execution mode, including deterministic mock runs. Production runs are
billable network operations. Choose RunSpec V3 for generative endpoints or V4 for
native Decisions.

| Provider / endpoint | Mode / configuration | Current implementation |
|---|---|---|
| Mock / `synthetic` | `live`, V1/V2; `bulk_live`, V2 | Public generative mock; offline execution, recovery and exports; V3 native transport has private fixtures |
| Mock / `synthetic` | `live`, `bulk_live`, V4 | Native score/choice questions, grouping and provenance; synthetic protocol only |
| OpenAI / `responses` | `live`, `bulk_live`, V3 | Implemented generative adapter; SDK/lifecycle fixtures |
| OpenAI / `chat.completions` | `live`, `bulk_live`, V3 | Implemented generative adapter; SDK/lifecycle fixtures |
| Anthropic / `messages` | `live`, `bulk_live`, V3 | Implemented generative adapter; SDK/lifecycle fixtures |
| Gemini Developer / `models.generate_content` | `live`, `bulk_live`, V3 | Implemented generative adapter; SDK/lifecycle fixtures |
| Vertex AI Gemini / `models.generate_content` | `live`, `bulk_live`, V3 | Implemented generative adapter; separate ADC/API-key routing and SDK/lifecycle fixtures |
| Together / `chat.completions` | Planned live runner | Offline schema-projection/private transport fixtures; no production run/resume adapter |
| Local Ollama / `chat` | Planned live runner | Offline schema-projection/private transport fixtures; no production run/resume adapter |
| OpenAI / `decisions` | `live`, `bulk_live`, V4 | Offline SDK/lifecycle fixtures and bounded `gpt-6-luna` smoke. 2–10 score levels and up to 400 named questions. |
| TypeSafe Jev / `systemone` | `live`, `bulk_live`, V4 | Offline SDK/lifecycle fixtures and bounded `jev-1.14.0` live checks. Up to 10 score levels. Context fit is unverified and the vendor question cap is unknown. |
| Gemini Developer / `decisions` | `live`, `bulk_live`, V4 | Offline SDK/lifecycle fixtures and bounded `gemini-decision-1` live grid. 2–255 score levels, up to 1,000 keys and a 256 KiB local body cap. Token fit is unverified. |

Managed bulk-live schedules the same provider scoring calls locally. Optional
request caps apply per run. Production token caps require a verified bound and
currently fail offline validation. Native batch for eligible endpoints is a
later Milestone 5 target. Generative JSON Schema (`structured_output:
native`) and native Decisions score/choice APIs are different interfaces. New
generative preparations use [presentation revision 2](docs/renderer-revision-2.md):
its native serving scope is unqualified, so nonstrict `auto` selects text, while
explicit `native` and strict unknown `auto` fail before forwarding. Text still
requires valid route/model/cache controls and strict local assessment validation.

Retained live evidence covers only its original sources and contracts:

- OpenAI's [historical acceptance record](docs/provider-matrix.md#phase-5a-openai-production-boundary)
  reports eight text/native rubric/CJ cases on `gpt-4.1-mini-2025-04-14`;
  the production native registry remains empty.
- [Gemini Developer acceptance, 2026-11-10](docs/gemini-live-qualification-acceptance.md)
  covers four original-contract text/native cases on `gemini-3.1-flash-lite`,
  google-genai 2.34.0/v1beta, with one-trait scores and forced-choice winners.
- [Vertex acceptance, 2026-11-11](docs/vertex-live-qualification-acceptance.md)
  and its [independent public audit](docs/vertex-evidence-verification.json) cover
  eight original-contract cases across separately bound ADC/API-key private-project
  routes: `gemini-3.1-flash-lite`, `global`, google-genai 2.34.0/v1beta1,
  one-trait scores and forced-choice winners. Developer evidence grants no Vertex authority.

Those records cover the original generative presentations. Anthropic's reviewed
live model profile is pending. Together and Ollama live runners are planned.
The Decisions adapters have their own scoped live evidence. See the
[provider matrix](docs/provider-matrix.md) for implementation and roadmap details.

## Install and preview

Public wheel and source packages contain the Python library, CLI and runtime templates.
Study tooling, examples, tests and developer documentation remain in the repository.
The checkout commands below require cloning it first:

```bash
git clone https://github.com/shmercer/judgmentllm.git
cd judgmentllm
```

```bash
uv sync --locked --all-extras --group dev
uv run --offline --no-sync judgmentllm templates list
uv run --offline --no-sync judgmentllm templates preview --config examples/run.yaml --item-id essay_001 --trait organization
uv run --offline --no-sync judgmentllm templates preview --config examples/pairwise.yaml --pair-uid comparison_001_reverse
uv run --offline --no-sync judgmentllm templates preview --config examples/advanced.yaml --item-id essay_001
uv run --offline --no-sync judgmentllm templates init --preset rubric_scores_only --output rubric_prompt.txt
uv run --offline --no-sync python examples/offline.py
```

All bundled inputs are synthetic. Preview makes no network requests and needs no credentials. The OpenAI preview examples use a documented cache-OFF route and require the pinned OpenAI SDK installed by the checkout command above. These V1 examples remain offline-only and establish no account access or assessment qualification. The initial dependency installation may download packages. Use `uv sync --locked --no-dev` for core-only installation; mock examples need no provider SDK.

## Standalone provider evidence

`judgmentllm.evidence` provides strict version-1 contracts and pure offline
services for reported usage, observable JSON property order, reasoning retention
authorization and scoped provenance. Phase 4B attaches safe evidence to physical
submissions and observations through immutable companion receipts and registry
schema 2. Full and minimal capture retain safe metadata; reasoning text requires
an explicit immutable synthetic-fixture grant. Existing result and R exports
remain unchanged. See the [Python API](https://github.com/shmercer/judgmentllm/blob/main/docs/python-api.md#internal-durable-evidence-inspection-phase-4b)
for internal inspection and compatibility. OpenAI production transport is additive.
Broad generative usage/reasoning companion exports remain deferred; V4's narrow
question/physical provenance bundle is implemented. Live qualification requires
separate consent.

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
[Python API](https://github.com/shmercer/judgmentllm/blob/main/docs/python-api.md#credential-diagnostics-phase-3-implemented) and
[CLI specification](https://github.com/shmercer/judgmentllm/blob/main/docs/cli-spec.md#offline-authentication-status-phase-3-implemented).
OpenAI `auth verify` and `providers models` are explicit non-generative online
operations; neither command qualifies a serving model or runs automatically.

## Offline execution lifecycle

`examples/mock-run.yaml` declares the deterministic mock provider. The existing
V1/V2 OpenAI examples remain valid for offline validation/preview and do not
acquire production execution. The provider guides above use V3 for implemented
production routes. Unimplemented endpoints fail without substituting the mock.

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
Uncertain submissions are never silently reissued. See [CLI controls](https://github.com/shmercer/judgmentllm/blob/main/docs/cli-spec.md)
and [public lifecycle APIs](https://github.com/shmercer/judgmentllm/blob/main/docs/python-api.md) for retry authorization and errors.
The installed-CLI recovery example inspects partial grouped outcomes, explicitly
retries failed members in later processes, and proves that older exports and
unsuccessful rows remain available. Use a fresh output directory for each example.
The [scale qualification guide](https://github.com/shmercer/judgmentllm/blob/main/docs/scale-qualification.md) describes the complete
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

For V1–V3 generative preparation, independent scoring is the default. Fixed groups preserve their supplied order; auto grouping requires an explicit known trait-count limit. The original `plan_run()` remains structural. `estimate_run()` validates the complete
input and gives exact task/request counts with unavailable token/cost estimates explicit.

V4 uses independently versioned single/auto question grouping and one shared
question template, rendered per criterion. Optional `prompt.output_instructions`
replaces only that question's default instruction text, verbatim; rubric,
exemplars and typed score/choice validation stay intact. Omission or null selects
the task default. See [V4 configuration and exact defaults](docs/decisions-lifecycle.md).

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

See [the finalized data contracts](https://github.com/shmercer/judgmentllm/blob/main/docs/data-contracts.md) and [public API examples](https://github.com/shmercer/judgmentllm/blob/main/docs/python-api.md) for normalization and streaming JSONL/CSV operations. Nested result data survives CSV export as JSON cells. Library writers revalidate each current nested result before export. Derived R-compatible audit exports preserve presented orientation, all attempts, ties/failures, and score kinds; see [R interoperability](https://github.com/shmercer/judgmentllm/blob/main/docs/r-interoperability.md) for character-safe import and explicit analysis selection.

## Templates and validation

Built-in presets cover rubric scores, scores with explanations, and pairwise forced choice. Simple templates use literal placeholders. Advanced Jinja2 is opt-in and supports a documented restricted syntax with rendering limits. Protected content blocks account for selected exemplars by ID, verifying completeness and order before returning a prompt. Omission or alteration fails clearly.

Output-schema validation is separate from rendering. Unknown configuration fields, incompatible presets, invalid categories, duplicate IDs, and known unsupported explanations fail locally. Unknown capabilities remain explicit; arbitrary model identifiers are not blocked by a catalog.

See [prompt guidance](https://github.com/shmercer/judgmentllm/blob/main/docs/prompt-guidance.md) and [CLI documentation](https://github.com/shmercer/judgmentllm/blob/main/docs/cli-spec.md).
The provider-free internal preparation service freezes assessment sources and
seals a complete manifest before submission can begin. Full capture retains
verified exact snapshots; minimal capture retains hashes, structural indexes,
and source references while requiring unchanged originals. Use a local runtime
store outside cloud-synchronized or network-mounted directories. See the
[Python preparation interface](https://github.com/shmercer/judgmentllm/blob/main/docs/python-api.md#immutable-source-preparation-internal)
and [verification record](https://github.com/shmercer/judgmentllm/blob/main/docs/milestone-2-subphase-2-verification.md).

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

Contributors should follow the [testing guide](docs/testing-guide.md) for focused
feedback, automatic PR plans and source-bound local/hosted qualification. A
focused development pass is preliminary; risk-appropriate coverage/R/resource requirements and
the protected-base hosted status remain separate acceptance evidence.
Local Ubuntu qualification automatically prefers the approved 10-worker concurrent
profile with up to two named R gates when ownership and resource safeguards are
available. Use `--local-schedule conservative` for explicit conservative execution;
unsupported systems fall back with a diagnostic. The retained 23.64% improvement
is for a bounded 254-case workload, not a measurement of the entire suite. See
[local scheduling and evidence](docs/ci-issue59.md#local-scheduling-and-evidence).

Use a writable `UV_CACHE_DIR` when the default cache is read-only. Tests are synthetic and offline. Levels 3/4 retain the 90% historical coverage floor; Level 2 requires complete coverage of changed executable lines and relevant branch outcomes. Statement and branch coverage are reported. The installation helper creates clean environments and may download locked core dependencies; installed import, template, and preview checks run offline. Provider SDKs must be absent from these core installations.

The [risk-based verification policy](https://github.com/shmercer/judgmentllm/blob/main/docs/ci-optimization.md) selects checks by affected
behavior. Linux/Python 3.11 is the primary suite/coverage lane; comprehensive
cross-platform and strict R qualification remains available for releases,
weekly runs and explicit requests. The [local grid](https://github.com/shmercer/judgmentllm/blob/main/docs/local-ci.md) is an
optional complete campaign, not a prerequisite for routine PRs.

## Optional dependencies and later milestones

Extras remain `openai`, `anthropic`, `google`, `together`, `ollama`, `typesafe`, `parquet`, `api`, and `all`. Use the route table above for implementation and qualification. Together/Ollama live runners, M5 native batch, the M6 CLI-first `rubricLLM` wrapper and M7 optional local FastAPI remain later work under the [implementation plan](docs/implementation-plan.md). R calls operate on runs/files or sized task groups, not one Python process per judgment.

The repository is [shmercer/judgmentllm](https://github.com/shmercer/judgmentllm), licensed under [MIT](https://github.com/shmercer/judgmentllm/blob/main/LICENSE), and not yet published to PyPI. The core does not implement Bradley–Terry fitting, adaptive pairing, or rubric calibration; these remain in `pairwiseLLM`.

Design references: [design specification](https://github.com/shmercer/judgmentllm/blob/main/docs/design-spec.md), [decisions](https://github.com/shmercer/judgmentllm/blob/main/docs/decisions.md), [contributor instructions](https://github.com/shmercer/judgmentllm/blob/main/AGENTS.md), and [Milestone 1 verification](https://github.com/shmercer/judgmentllm/blob/main/docs/milestone-1-verification.md).


Vertex ADC and separately selected authorization-key sources stay isolated from
Gemini Developer. Historical proposals and accepted exact-scope evidence remain
separate records; ordinary auth status, planning and validation stay offline.

New prompt previews and preparations use readable Markdown and positional CJ
aliases. See the [renderer and compatibility guide](https://github.com/shmercer/judgmentllm/blob/main/docs/renderer-revision-2.md)
and [exact offline writing-task examples](https://github.com/shmercer/judgmentllm/blob/main/examples/rendering-revision2/).
Historical sealed requests retain their original bytes and source-ID wire
interpretation. Existing native qualification does not transfer to the revised
contract; new production requests use text until separately qualified (`native`
and strict unknown `auto` fail before submission).


The [original writing-study package](https://github.com/shmercer/judgmentllm/blob/main/examples/study-native/README.md) provides
50 native Standard/Flex configurations, 30 Flex-first main selections, all 312
verified original previews and a CLI-first guarded qualification workflow.
The 42-call native and 24-call service-tier proposals authorize zero calls;
account eligibility, billable-token bounds and prices remain independent gates.
Ordinary production qualification and the existing service/reasoning rules stay
unchanged. No student-writing study or live provider request is authorized here.

### Native Decisions

RunSpec V4 supports native rubric scores and two-option comparative judgments
through the installed Python and CLI lifecycle. Automatic grouping shares one
essay or one presented pair across eligible questions. Each repetition and
reversal has its own presentation. The adapters have offline SDK, recovery and
R fixtures. Live evidence is scoped to the recorded route, model and cases.

- **OpenAI Decisions, PR #64 (2026-11-17).** The maintainer confirmed a bounded
  `gpt-6-luna` smoke. The retained summary gives no level-by-level or tie
  coverage. [OpenAI Decisions guide](docs/openai-decisions.md),
  [PR #64](https://github.com/shmercer/judgmentllm/pull/64)
- **TypeSafe Jev (maintainer verification, 2026-11-18).** The first smoke stopped
  before any case completed. The follow-up `jev-1.14.0` grid recorded eleven
  passing cases and five cases with score validation failures. All eight
  single-question and grouped Choice cases completed. After the precision
  correction, eight requests accepted all twelve scores at 3, 4, 5 and 10 levels.
  Five scores exercised the rounding compatibility check.
  [TypeSafe Jev adapter guide](docs/typesafe-decisions.md)
- **Gemini Decisions (maintainer verification, 2026-11-19).** The Developer
  v1beta grid on `gemini-decision-1` passed eight requests and fourteen judgments, including
  2, 3, 4, 5 and 10 score levels and Choice in both orientations. Estimated
  total cost was USD 0.000372. No tie was observed.
  [Gemini Decisions adapter guide](docs/gemini-decisions.md)

The current adapters require forced winners for comparative judgments.
The supported score ranges describe adapter limits. The live outcomes above
cover the tested cases. Public alpha requires a separate post-M5 decision.
