# Milestone 2 Subphase 1 — implementation and verification

Date: 2026-11-02. Branch: `codex/milestone-2-persistence`.
Base: `d1cb6e86a336bf0601de05a470185f6472f34b35`, verified against freshly
fetched `origin/main` before branching. The working tree was initially clean.

The [actual approved revised plan](milestone-2-execution-plan.md) is preserved
verbatim, excluding its conversational XML wrapper. This implementation completes
only Subphase 1. Its [internal contracts](persistence-contracts.md) describe the
typed operations, tables, state/evidence guards, locking, hashing and artifacts.

## Implemented and preserved

- Strict versioned execution states, identities, manifests/references, events,
  failure evidence, retry authorization identities, summaries and safe errors.
- Nineteen SQLite tables: schema_migrations, runs, artifacts, sources,
  input_records, tasks, requests, request_tasks, retry_operations, attempts,
  attempt_tasks, batches, batch_members, submissions, observations, events,
  submission_failures, results, and exports. Sources/batches/exports have reserved
  schema only; no corresponding service workflows were added.
- Run-scoped foreign keys, unique identities and ordered membership, SQL state
  checks/triggers, append-only outcomes/events, no-replacement guards, immutable
  prepared identities and one-time manifests, known-acceptance/remote-ID
  preservation, migration and schema-object checksums.
- Short composable transactions/savepoints, WAL/FULL durability, independent
  readers and process-death rollback. No transaction spans provider work.
- Cross-platform OS ownership with tokens, PID/host/heartbeat diagnostics,
  stale-metadata recovery only after lock acquisition, and no lease takeover.
- Exact-byte immutable artifacts, atomic no-clobber publication, verification,
  explicit hash-only identities, domain-separated semantic hashing and deep
  model revalidation. Structural Milestone 1 IDs are unchanged.
- Validated successful and unsuccessful normalized-result persistence, with
  actual observation or definitive submission-failure evidence. Merely planned,
  unsubmitted and uncertain work cannot manufacture results.
- Actual persisted synthetic rubric/pairwise outputs reopened in a separate
  Python process, exported through existing writers, and read/asserted in R.

No source ingestion/freezing, prompt capture, provider protocol/mock provider,
scheduler/async engine, grouped-output recovery, retry execution, lifecycle CLI,
R wrapper, billing, native batches or HTTP service was implemented. Synthetic
fixtures manually provide fixed facts to test storage; they do not execute a
provider. Existing public assessment/configuration/result schemas, exports,
prompt/planning behavior, CLI and dependencies remain unchanged.

## Local verification

Environment: Linux, CPython 3.12.3, R 4.6.2, jsonlite 2.0.0.
Commands used `UV_CACHE_DIR=/tmp/judgmentllm-uv-cache`.

| Check | Result |
|---|---|
| `uv run --offline --no-sync ruff check .` | Passed. |
| `uv run --offline --no-sync ruff format --check .` | Passed; 55 Python files formatted. |
| `uv run --offline --no-sync pyright` | Passed; zero diagnostics. |
| `uv run --offline --no-sync pytest --cov-report=json:/tmp/judgmentllm-m2-subphase1-final-coverage.json` | **656 passed**, 10.50 seconds; 228 added persistence tests. |
| `uv lock --check --offline` | Passed; 93 packages, no lockfile changes. |
| `uv build --offline` | Wheel and source distribution built. |
| `uv run --offline --no-sync twine check --strict dist/judgmentllm-0.1.0a2*` | Both artifacts passed. |
| `uv run --offline --no-sync python scripts/smoke_install.py dist/judgmentllm-0.1.0a2-py3-none-any.whl dist/judgmentllm-0.1.0a2.tar.gz` | Both clean, core-only installations passed outside the checkout, including persistence import, reopen, artifact verification and OS ownership. |
| Existing strict R export example | Passed: six pairwise and three rubric records, full probabilities/mappings and orientations. |
| New separate-process persistence/R fixture | Passed: six rubric and three pairwise records, unsuccessful outcomes, subset attempt history, leading zeros, reversals/ties, score distinctions and provenance. |
| `git diff --check` | Passed. |

The initial sandboxed clean-install attempt failed resolving pypi.org. The same
check passed with authorized network access for dependency installation. Installed
package verification itself remained offline. No provider request was made.

| Coverage | Covered / total | Percentage |
|---|---|---|
| Statements | 2,164 / 2,193 | 98.68% |
| Branches | 720 / 736 | 97.83% |
| Combined gate | 2,884 / 2,929 | 98.46% |
| New persistence statements | 750 / 776 | 96.65% |
| New persistence branches | 153 / 164 | 93.29% |

Remaining coverage includes Windows-only lock/directory handling and selected
OS/database failure paths. The 90% coverage gate is unchanged. These are local
Linux results, not a claim that the remote Windows/macOS/Python matrix ran.

Exact R commands (after generating the baseline files):

```bash
uv run --offline --no-sync python examples/export_r.py --output /tmp/judgmentllm-m2-baseline-r
JUDGMENTLLM_R_STRICT=1 Rscript --vanilla examples/r_interoperability.R /tmp/judgmentllm-m2-baseline-r
uv run --offline --no-sync python scripts/persistence_r_fixture.py --phase persist --output /tmp/judgmentllm-m2-persistence-r
uv run --offline --no-sync python scripts/persistence_r_fixture.py --phase export --output /tmp/judgmentllm-m2-persistence-r
JUDGMENTLLM_R_STRICT=1 Rscript --vanilla examples/persistence_interoperability.R /tmp/judgmentllm-m2-persistence-r
```

The existing CI matrix, timeout, R setup and strict Milestone 1 step are retained.
A further required Ubuntu/Python 3.11 step runs the persistence phases and R
assertions without failure suppression. Missing R/jsonlite fails that step.
Optional local pytest R invocation can skip absent prerequisites outside strict
mode; this does not satisfy or replace the required CI step. No additional R
dependencies or mandatory pairwiseLLM/Stan/processx installation were introduced.

## Refinements and limits

No approved capability was postponed from this subphase. Small implementation
refinements are the explicit append-only submission_failures table and hashed
filesystem run-directory names, preserving original IDs. The package remains
`0.1.0a2` during this independently reviewable foundation change; the approved
`0.1.0a3` milestone target is unchanged. No dependency/lock changes were needed.

The byte store provides primitives, not capture-policy enforcement. Phase 2 must
construct and seal the complete manifest, freeze inputs consistently, enforce
full/minimal retention, implement multiline-CSV locators, and verify source and
record identities. Its services must not interpret hashes as response evidence.
Later phases own provider reconciliation, scheduling, grouped normalization,
selected retry execution and lifecycle CLI behavior.

No push, merge, release, live qualification, remote CI run or Subphase 2 work was
performed. OS locks remain single-machine/local-filesystem only; Windows branches
are covered by the preserved CI matrix rather than this Linux session.
