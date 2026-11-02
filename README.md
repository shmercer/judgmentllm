# JudgmentLLM

**Status: design specification, pre-implementation.**

JudgmentLLM is a planned Python package and command-line tool for conducting LLM-based comparative judgments and ordinal rubric scoring through multiple providers. It is intended as a reusable backend for Python applications, local workflows, and R integrations such as `reticulate`.

The repository currently contains an initial design specification, not a working scoring package. Provider/model compatibility must be tested before an implementation is described as supported.

## Scope

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

Target GitHub repository: `shmercer/judgmentllm`; target PyPI distribution and Python import: `judgmentllm`. PyPI name registration remains to be confirmed. Proposed license: MIT.

This project is inspired by provider request, response normalization and durable-batch patterns in [`pairwiseLLM`](https://github.com/shmercer/pairwiseLLM), but does not aim to port its statistical modeling or adaptive selection estimators to Python.

## Next step

Implement Milestone 0 and Milestone 1 as documented in `docs/implementation-plan.md`. Milestone 7 commits to adding FastAPI once the core library, CLI, provider integrations, batch execution, and R bridge are stable; it does not block early prereleases. Do not use real provider APIs in the regular CI suite.
