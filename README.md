# JudgmentLLM

**Status: Milestone 0 — repository and engineering baseline. Version: 0.1.0a1.**

JudgmentLLM is a Python package and command-line foundation for planned LLM-based comparative judgments and ordinal rubric scoring through multiple providers. The intended interfaces serve Python applications, local workflows, and R integrations such as `reticulate`.

Milestone 0 provides an importable typed package, version information, CLI help, packaging, offline tests, and CI. Scoring, data contracts, prompt rendering, provider calls, persistence, batching, model discovery, R integration, and the FastAPI server are not implemented. Provider/model compatibility must be tested before it is described as supported.

## Install from source

Python 3.11 or later and [uv](https://docs.astral.sh/uv/getting-started/installation/) are required. Development and CI use uv 0.13.6. Use an existing uv installation; all project dependencies are installed in the repository's Git-ignored `.venv`.

```bash
git clone https://github.com/shmercer/judgmentllm.git
cd judgmentllm
uv --version
uv sync --locked --no-dev
uv run --no-sync judgmentllm --help
uv run --no-sync judgmentllm --version
```

`uv sync` installs the local package in editable mode. Add `--no-editable` for a non-editable source installation. Environment activation is optional; `uv run` selects the project environment.

The version command prints `judgmentllm 0.1.0a1`. Calling `judgmentllm` without arguments also displays help. These commands run locally without credentials or provider requests.

```bash
uv run --no-sync python -c "import judgmentllm; print(judgmentllm.__version__)"
```

## Local development

From the repository root, install all optional dependencies and development tools into `.venv` using the committed lockfile:

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
uv run --offline --no-sync judgmentllm --help
uv run --offline --no-sync judgmentllm --version
```

The initial sync may download dependencies. Subsequent checks and builds use installed tools and cached build dependencies; standard tests make no network requests and require no API credentials. pytest reports statement and branch coverage and writes `coverage.xml`; the coverage target is 90%.

The installation smoke helper creates a clean temporary environment, installs the built wheel with core dependencies from the uv cache, and verifies imports and CLI execution outside the checkout. On Linux, also verify the source distribution with `uv run --offline --no-sync python scripts/smoke_install.py dist/*.tar.gz`.

`uv.lock` is committed for reproducibility. Keep `--locked` for ordinary setup and CI so stale dependency metadata fails visibly. To update dependencies deliberately, run `uv lock --upgrade-package PACKAGE`, sync with the same extras and development group, review the lockfile diff, and rerun the checks before committing both metadata and lockfile changes. Do not commit `.venv` or install project dependencies into the system Python.

## Optional dependencies

The core package requires Typer and its dependencies. Feature dependencies are grouped into these extras:

| Extra | Installed dependencies |
|---|---|
| `openai` | Official OpenAI Python SDK |
| `anthropic` | Official Anthropic Python SDK |
| `google` | `google-genai`, for Gemini Developer API and Vertex AI Gemini |
| `together` | Official Together Python SDK v3 |
| `ollama` | Official Ollama Python SDK |
| `typesafe` | Official `typesafe-sdk` |
| `parquet` | PyArrow |
| `api` | FastAPI and the minimal Uvicorn server dependency |
| `all` | All the feature dependencies above; development tools remain a separate group |

For example, install the local package with just the OpenAI extra:

```bash
uv sync --locked --no-dev --extra openai
```

Installing an extra only installs dependencies. Provider adapters and Parquet operations arrive in later milestones; the HTTP server and `serve` command arrive in Milestone 7.

## Continuous integration

GitHub Actions runs Ruff, Pyright, offline tests with coverage, distribution builds and validation, and clean wheel installation smoke tests on Linux, Windows, and macOS with Python 3.11 and 3.15. Linux also verifies installation from the source distribution. Coverage reports are retained as workflow artifacts.

Codecov upload is optional. Connect the public repository to Codecov, then set the GitHub Actions repository variable `CODECOV_ENABLED` to `true`. If the Codecov configuration requires an upload token, store it as the repository secret `CODECOV_TOKEN`. Upload errors do not fail the other CI checks; local coverage checks remain required.

## Planned assessment features

- Comparative judgment (forced-choice default) and ordinal rubric scoring.
- Any number of rubric traits and stored exemplars, subject to provider request limits.
- Independent-trait scoring by default, explicit grouped scoring when wanted.
- Customizable prompts, score-only results, and scores with optional explanations.
- Live requests, provider-native asynchronous batches, and managed bulk-live scheduling.
- Reproducible request records, resilient job resumption, and a common result schema.
- A **committed late-stage** local FastAPI HTTP interface for applications, distributed as optional `judgmentllm[api]` so library and CLI installations remain lightweight.
- OpenAI (Chat, Responses, Decisions), Anthropic, Gemini Developer API, Vertex AI Gemini, Together AI, local Ollama, and TypeSafe AI Jev.

## Design documentation

- [`docs/design-spec.md`](docs/design-spec.md) - scope, requirements and behavior
- [`docs/data-contracts.md`](docs/data-contracts.md) - data structures and normalized outputs
- [`docs/prompt-guidance.md`](docs/prompt-guidance.md) - approachable prompts and validation
- [`docs/provider-matrix.md`](docs/provider-matrix.md) - per-provider adapters and verification
- [`docs/cli-spec.md`](docs/cli-spec.md) - command-line design
- [`docs/http-api-spec.md`](docs/http-api-spec.md) - committed late-stage local FastAPI interface and HTTP contract
- [`docs/implementation-plan.md`](docs/implementation-plan.md) - milestones and acceptance criteria
- [`docs/decisions.md`](docs/decisions.md) - recorded design choices
- [`AGENTS.md`](AGENTS.md) - directions for Codex and contributors
- [`docs/repository-setup.md`](docs/repository-setup.md) - steps to create the public GitHub repository

## Distribution and licensing

GitHub repository: [`shmercer/judgmentllm`](https://github.com/shmercer/judgmentllm). The Python import and target PyPI distribution name are `judgmentllm`. The package has not been published to PyPI, and name registration remains to be confirmed. License: [MIT](LICENSE).

This project is inspired by provider request, response normalization and durable-batch patterns in [`pairwiseLLM`](https://github.com/shmercer/pairwiseLLM), but does not aim to port its statistical modeling or adaptive selection estimators to Python.

## Next step

Milestone 1 implements the data contracts, rubrics, results, and prompt system described in [`docs/implementation-plan.md`](docs/implementation-plan.md). Milestone 7 commits to adding FastAPI once the core library, CLI, provider integrations, batch execution, and R bridge are stable; it does not block early prereleases. Real provider calls remain outside the regular CI suite.
