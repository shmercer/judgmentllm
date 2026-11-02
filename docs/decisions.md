# Recorded design decisions

**Date:** 2026-11-02. **Status:** approved design direction; implementation and API qualifications pending.

| Area | Decision |
|---|---|
| Name | JudgmentLLM; repo `shmercer/judgmentllm`, planned PyPI `judgmentllm` |
| Purpose | General LLM judgment infrastructure, both pairwise and direct rubric scoring |
| Providers | Match `pairwiseLLM`: OpenAI Chat/Responses/Decisions, Anthropic, Gemini Developer, Vertex AI (Gemini focus), Together, local Ollama, TypeSafe AI Jev |
| Rubric | Arbitrary ordered categories, no continuous scales in initial release; enforce endpoint-specific limits |
| Scores | Retain all returned score data; expected value is primary if category probabilities exist; do not imply comparability with categorical model outputs |
| Explanations | Scores only or scores plus explanations; fail clearly when an endpoint cannot supply requested explanations |
| Traits | Arbitrary stored trait count; independent evaluation by default, grouped configurable; explicit, deterministic partitioning when necessary |
| Exemplars | Zero to many; IDs, text and optional trait/level scores, rationales, annotations; no automatic dropping/selection; warn/error on context overflow or unknown limit |
| Prompts | Fully editable text prompts with accessible placeholders; advanced Jinja2 opt-in; no arbitrary JSON API requests |
| Pairwise | Forced winner default, optional ties where compatible, repetitions and presentation-reversal audits, optional explanations when available |
| Cache | Pairwise off by default, rubric auto by default; `off`, `auto`, `explicit`; explain provider implicit caching constraints |
| Execution | Live (provider tiers where supported), native Batch (where supported), and managed bulk-live; no unapproved fallback in model/tier |
| Scale | Design for 100,000 response input, hundreds of traits, thousands of stored exemplars; do not hold entire work in RAM |
| Data | CSV/JSONL, optional Parquet, Python and R data frames; stable long-form outputs and complete audit records by default with opt-out |
| Retries | Transient transport errors automatically with bounded backoff; malformed/completed judgments retained, re-score only on explicit request |
| Cost controls | Local validation always; optional plan/dry-run with estimated cost, request count and budget ceiling |
| Runs | Immutable manifest after submit; new run ID when spec changes; resume without blind duplicate submission |
| Models | Auto-generated reference catalog; distinguish discovered/implemented/qualified; omit primarily coding-only models from recommended catalog without blocking user-supplied IDs |
| Dev stack | Python>=3.11, pure Python, uv, Hatchling, Pydantic 2, PyYAML, Jinja2, Typer, asyncio, official SDKs, SQLite, pytest, Ruff, Pyright, Codecov |
| Packaging | Optional provider dependencies plus `[all]`, PyPI and GitHub; later thin R wrapper |
| HTTP | FastAPI is a committed late-stage development milestone (Milestone 7) after core/CLI/providers/batch/R bridge. Installed only via optional `[api]` extra; local loopback-only and single-user without authentication. No remote or multi-user features in this scope. Early library/CLI prereleases may precede HTTP support. |
| Licensing | MIT proposed |
| Qual tests | Manual option and before every release, separate from offline CI |
| Numeric labels | Ordered category indices equally spaced by default for nonnumeric labels; allow explicit numeric mapping; preserve original labels |

## Deliberately unresolved and nonblocking

- PyPI project name can only be claimed by a successful first release / publisher registration; availability not guaranteed by search.
- Provider capability details, quotas, token limits and permitted caching/priority controls must be checked against authoritative docs and live qualification before a feature is marked supported.
- The exact public Python function signatures and file schema require an initial reviewed implementation contract (Milestone 1).
- Support for native Vertex Gemini batches is in scope, but Google Cloud project, location, GCS/BigQuery inputs and IAM dependencies require separate documentation and qualification.
