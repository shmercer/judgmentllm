# Milestone 2 — generation foundation verification and handoff

Date: 2026-11-02. Branch: `feat/generation-config-foundation`.
Base: `e5dc79a11c2e73ba4728c06bebb99be46f5d8b28`, verified against freshly
fetched `origin/main` before branching. The working tree was initially clean.

This implements only the approved generation foundation at the beginning of
Subphase 2. The [detailed plan](milestone-2-execution-plan.md) retains its six
subphases and adds the approved generation requirements and acceptance criteria.

## Implemented contracts and safeguards

- Optional strict `GenerationSpec` for `temperature`, `top_p`, and
  `max_output_tokens`, separate from execution and pairing/orientation seed.
  Unset controls stay absent; explicit values, including zero, remain unchanged.
- Omitted, null, empty and all-null generation configurations normalize equally.
  Golden fixtures preserve Milestone 1 request/task IDs and the exact existing
  serialized configuration digest. Existing diagnostics and strict-capability
  behavior remain unchanged when no generation controls are requested.
- Exact provider/endpoint/model-scoped support facts, inclusive bounds and
  incompatible combinations. Unknown support is explicit and never implies
  fixture/live qualification. Only requested controls require support evidence.
- Frozen generation commitments with sparse settings and independent requested
  identity. Evidence changes do not change that identity. Compatibility checks
  apply current capability restrictions and reject newly known incompatibilities
  without modifying requested settings or the original evidence snapshot.
- Immutable artifact/repository seal-and-reopen fixtures, deep mutation rejection,
  cross-process hashing, and actual Python-produced records consumed by R.
  Fixture documents explicitly identify themselves as
  `generation_manifest_fixture`; they are not complete executable run manifests.
- Future typed controls and provider mappings remain explicit requirements in
  Milestones 3–4; live/batch parity is required in Milestone 5, and the same run
  configuration must be exposed through the Milestone 6 `rubricLLM` wrapper.

Generation settings and capability records are frozen value models; `RunSpec`
remains mutable and can replace its generation value. Exact source-file hashes
remain separate from semantic configuration identity. Equivalent settings do not
authorize using changed sources or reusing an old submission identity.

## Local verification

Environment: Linux, CPython 3.12.3, R 4.6.2, jsonlite 2.0.0.
uv commands used `UV_CACHE_DIR=/tmp/judgmentllm-uv-cache` because the default
cache directory is read-only in the sandbox.

| Check | Result |
|---|---|
| Ruff lint and format checks | Passed; 62 Python files formatted. |
| Pyright | Passed; zero errors or warnings. |
| `JUDGMENTLLM_R_STRICT=1 uv run --offline --no-sync pytest --cov-report=json:/tmp/judgmentllm-generation-coverage.json` | **731 passed**, no skips; 12.86 seconds. Includes 71 new generation/configuration/commitment/R cases. |
| Coverage | 2,319/2,348 statements (98.76%); 770/786 branches (97.96%); combined gate **98.56%**, above the unchanged 90% requirement. Both new generation modules have 100% statement/branch coverage. |
| `uv lock --check --offline` | Passed; 93 packages, no dependency or lock changes. |
| `uv build --offline` | Wheel and source distribution built. |
| `uv run --offline --no-sync twine check --strict dist/judgmentllm-0.1.0a2*` | Both distributions passed. |
| Clean wheel and source installation smoke checks | Both passed outside the checkout, including generation imports, sparse settings, commitment construction/compatibility, existing persistence, and template CLI checks. |
| Strict Milestone 1 Python-to-R example | Passed; full distributions, mappings, IDs, orientations, selected attempts, and score distinctions preserved. Optional locally installed pairwiseLLM 1.6.0 adapter checks also passed. |
| Separate-process persistence-to-R example | Passed; six rubric and three pairwise all-attempt records. |
| Separate-process generation-to-R example | Passed; six retained generation-only fixtures, explicit values, unknown evidence, absence equivalence, exact source hashes and leading-zero IDs. |
| `git diff --check` | Passed. |

The first clean-install attempt could not resolve pypi.org inside the sandbox.
The retry passed with network access authorized for installation of locked core
dependencies. Installed verification and all interoperability execution remained
offline and credential-free; no provider requests were made.

The existing Linux/Windows/macOS × Python 3.11/3.15 CI matrix, 20-minute timeout,
required R/jsonlite setup, and both previous R gates are preserved. An additional
required Ubuntu/Python 3.11 step runs generation persistence/export and R
assertions without suppressing failures. These are local Linux results; remote
matrix execution is not claimed.

The standalone R checks used:

```bash
.venv/bin/python examples/export_r.py --output /tmp/judgmentllm-generation-baseline-r
JUDGMENTLLM_R_STRICT=1 Rscript --vanilla examples/r_interoperability.R /tmp/judgmentllm-generation-baseline-r
.venv/bin/python scripts/persistence_r_fixture.py --phase persist --output /tmp/judgmentllm-generation-persistence-r
.venv/bin/python scripts/persistence_r_fixture.py --phase export --output /tmp/judgmentllm-generation-persistence-r
JUDGMENTLLM_R_STRICT=1 Rscript --vanilla examples/persistence_interoperability.R /tmp/judgmentllm-generation-persistence-r
.venv/bin/python scripts/generation_r_fixture.py --phase persist --output /tmp/judgmentllm-generation-final-r
.venv/bin/python scripts/generation_r_fixture.py --phase export --output /tmp/judgmentllm-generation-final-r
JUDGMENTLLM_R_STRICT=1 Rscript --vanilla examples/generation_interoperability.R /tmp/judgmentllm-generation-final-r
```

## Deviations and limits

No scope deviations. No cloud adapters, provider request builders, invented
payload bodies, full executable manifest, source-freezing service, executor,
operational resume, lifecycle CLI, or R wrapper was added. The generation
compatibility helper validates only its section, not complete submission safety.
Existing assessment/result schema version 1, database schema, export columns and
package version `0.1.0a2` remain unchanged. The Milestone 2 `0.1.0a3` target is
preserved. No merge or publication is part of this implementation.

## Remaining Subphase 2 source-preparation handoff

1. Implement consistent source discovery/freezing for configuration, rubric,
   criteria, exemplars, templates, responses and pairs. Parse/index the same
   verified bytes whose identities are recorded; recheck the entire source set
   before sealing. Preserve exact source hashes alongside normalized identity.
2. Add bounded-memory streaming artifact publication as required by the
   Subphase 1 handoff. Retain exact hashes, verification and atomic publication.
3. Build indexed logical-record byte locators for JSONL and multiline CSV.
   Minimal capture must verify source generations, IDs and canonical record
   hashes before using locators, and must not persist assessment text in staging,
   database rows, WAL, diagnostics or artifacts.
4. Construct the complete manifest under full/minimal capture policies and embed
   the generation commitment. A generation-only fixture must never qualify as
   a complete executable manifest or satisfy submission preflight.
5. Integrate current capability validation into preparation and eventual
   submission/resume, preserving frozen original evidence. A newly known
   restriction must block affected work. Matching generation/configuration
   identities must not bypass source-integrity checks or frozen-snapshot policy.
6. Prepare dependencies incrementally and bind exact rendered prompts and actual
   serialized request bytes before submission when the provider protocol exists.
   Include every requested generation control or fail; omit unspecified controls.
   Never substitute a settings digest for an exact payload hash. Cloud mappings
   remain Milestone 3; scheduling/recovery and lifecycle CLI remain their assigned
   later Subphase 3–5 work.
