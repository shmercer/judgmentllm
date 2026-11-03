# Milestone 2 Subphase 2 — immutable source preparation verification

Date: 2026-11-02. Branch: `codex/milestone-2-source-preparation`.
Base: `c201bcf8c8af6e31fbc7d6afaea8b33a53ed5529`, freshly fetched
`origin/main`, including merged PRs #3 and #4. The initial checkout was clean.

This record covers the remaining immutable-source preparation work. It does not
claim provider submission, scheduling, execution recovery, or lifecycle CLI.

## Implementation

- Provider-free `judgmentllm.preparation` services discover sources, consistently
  freeze exact bytes, validate and index all records, seal complete version-1
  executable manifests, and reopen independently. Existing assessment validation,
  strict CSV/JSONL conversion, planner identities, and prompt rendering are reused.
- `ArtifactStore` supports bounded streaming staging/publication, incremental
  SHA-256, exact length verification, flush/fsync, corruption detection, and atomic
  no-overwrite publication. Interrupted staging is never promoted implicitly.
- Existing `sources`/`input_records` tables hold file provenance, canonical record
  identities, complete logical byte locators, ordered traversal, and pair
  references. CSV multiline fields and header context remain strict. Both pair
  response texts are resolved through indexes and committed as dependencies.
- Full capture retains exact verified snapshots and requires explicit audited
  authorization to use them after original-source changes. Minimal capture
  retains hashes/structural metadata and requires unchanged available originals;
  it does not write assessment text to SQLite/WAL, staging, manifests, or logs.
- Complete manifests separate exact original hashes from normalized configuration
  identities, include ordered assessment/template/grouping/provider/generation/
  capability/capture/version commitments, and publish a fixed immutable reference.
  Generation-only fixtures remain non-executable.
- Cross-process Python/R fixtures reopen four actual full/minimal rubric/pairwise
  runs and check metadata, sources, hashes, leading-zero IDs, missing fields, and
  original/presented orientation without fabricating model judgments. A fourth
  strict Ubuntu/Python 3.11 R gate preserves all three existing required checks.

## Interfaces and consistency boundaries

`judgmentllm.preparation` is internal. `prepare_run(spec_or_path, *, store,
run_id, capabilities=None, exhaustive_rendering=False)` returns a context-managed `PreparedRun` after complete
validation and sealing. `open_prepared_run(run_id, *, store, spec=None,
capabilities=None, use_frozen_snapshot=False)` verifies/reopens it. `validate_inputs`
uses the same pipeline in a disposable local store. No lifecycle CLI or public
`run_live`/`resume_run` operation is introduced.

Prepared sessions support indexed response/pair lookup, ordered iteration,
`iter_request_dependencies()`, current-capability compatibility checking and
`render()`. Rendering reconstructs verified inputs and checks the dependency
commitment, then calls the existing renderer. Corrected preparation checks static
template configuration per group/criterion and renders verified actual bounded
representatives where available before publishing snapshots; explicit
`exhaustive_rendering=True` also checks every actual request. There are no
provider request bodies, submission records, attempts, or fabricated results.

Source discovery retains every first-observed file identity, including references
read during initial configuration discovery. Full capture copies exact bytes to
private staging, checks descriptor/path signatures, independently hashes originals,
verifies staging, parses/indexes staging and rereads indexed records. Minimal
capture hashes the exact scan stream, verifies independent source hashes and
retains no text-bearing snapshots. The whole source set is rehashed before
publication; a final generation sweep closes the publication window before seal.
These checks detect concurrent changes; they do not claim a filesystem-wide atomic
snapshot. Original-file hashes and validated configuration/assessment identities
are distinct, preserving equivalent omitted/null/empty generation configurations.

CSV locators cover logical records, including quoted multiline fields, using
encoded byte boundaries and verified header context. JSONL locators cover complete
encoded records. Shared strict conversion preserves Unicode, duplicate-key,
header/column and empty-text behavior. SQLite enforces duplicate IDs and ordered
traversal; pair references resolve through its response primary key. Dependencies
include both original response hashes while preserving presented orientation and
all established logical IDs.

Minimal persisted record metadata has an explicit allowlist: responses retain no
structural JSON fields; pairs retain IDs, original/presented order, repetition and
reversal fields only. Neither input metadata dictionary is copied. Source
generation JSON accepts numeric file-identity fields only. Manifest assessment
content is represented by hashes, ordered identities/coding and source references;
full canonical specifications are retained only under full capture. Python-object
specifications never receive invented source-file hashes; minimal object-origin
reopening requires the caller's identical validated `RunSpec`.

## Ownership, interruption and authorization

Source/index mutation, snapshot registration, fixed-manifest publication and sealing
hold the existing per-run OS ownership lock. Index writes use batches of 128 rows
and short SQLite transactions; no transaction spans scans, hashing or rendering.
Competing processes cannot interleave preparation, while independent readers can
inspect committed state.

Interrupted scans retain append-only index rows and an unsealed `created` run.
They require a new run identity; this implementation does not delete/restart partial
ingestion or invent resumable checkpoints. Interrupted artifact publication may
leave an orphan staging file or object, never an implicitly accepted snapshot.
Ordinary exception paths remove their own staging files; orphan files after process
death are ignored, never promoted or mistaken for committed sources.

A canonical fixed `manifest.json` published before SQLite sealing has a registered
immutable artifact. Reopening acquires ownership and independently verifies its
artifact, run/spec/source/index commitments, every indexed record and original
sources before finishing the seal. Missing, corrupt or conflicting commitments
refuse recovery. A file on disk alone never qualifies an unsealed run as executable.
Published manifests are never overwritten, including during recovery.

Full capture blocks changed/missing originals by default, including changes during
an active session. Explicit `use_frozen_snapshot=True` authorizes verified snapshots
for that recovery session and its recorded discrepancies only. A typed authorization
artifact records the explicit caller decision, operation/run/manifest identities,
source discrepancies, scope and time; registration appends its immutable audit
event. `iter_snapshot_authorizations()` independently discovers and verifies this
evidence. Existing artifacts never grant permission to a later operation. Missing
or corrupt required snapshots always block. Minimal capture always requires
unchanged originals. Current capabilities are reapplied without modifying the
original evidence or generation commitment.

## Original local verification — commit `7d38305`

Local environment: Linux, CPython 3.12.3, uv 0.13.6, R 4.6.2 and jsonlite 2.0.0.
Commands used `UV_CACHE_DIR=/tmp/judgmentllm-uv-cache` because the default cache
is read-only inside the sandbox. Tests and interoperability execution were offline
and credential-free; no provider calls were made.

| Check | Result |
|---|---|
| Ruff lint and formatting | Passed; `ruff check .` and `ruff format --check .`, 81 Python files formatted. |
| Strict Pyright | Passed; zero errors or warnings across source, tests and scripts. |
| Pytest and ≥90% coverage | **907 passed**, no skips, 130.96 seconds with `JUDGMENTLLM_R_STRICT=1`. Added 186 tests over the 731-test baseline. Combined coverage **96.92%**; statements 3,520/3,607 (97.59%), branches 1,100/1,160 (94.83%). |
| Lockfile validation | `uv lock --check --offline` passed; 93 packages and no dependency/lock changes. |
| Wheel/source build and strict metadata | `uv build --offline` built wheel and sdist; strict Twine metadata checks passed. |
| Clean wheel/source installation smoke | Both current `0.1.0a2` distributions installed and passed import, CLI, generation, preparation/sealing/reopening/indexed lookup/dependency smoke checks outside the checkout. |
| Three existing strict R/jsonlite checks | Passed standalone offline export/persistence/generation fixtures; six pairwise + three rubric baseline attempts, six rubric + three pairwise persistence records, and six generation fixtures. Optional local pairwiseLLM 1.6.0 checks also passed. |
| New separate-process preparation/R check | Passed standalone strict R/jsonlite check and two focused pytest cases (1.60 seconds): four actual full/minimal rubric/pairwise runs persisted and reopened in separate Python processes before R assertions. |
| Deterministic memory/indexed-lookup regression | Passed; independent processes measured both Python allocations and native RSS, with native peaks below 46 MiB. Detailed measurements below. |
| Diff whitespace and clean commit review | `git diff --check` passed; all changes reviewed before the local commit on the requested branch. No push or merge. |

The initial clean-install invocation could not resolve PyPI from the sandbox.
The retry used the two exact newly built artifacts and network access authorized
for installing locked core dependencies; both clean installations passed. All
installed behavioral verification remained offline. No unverified quality gate
was suppressed.

The full suite includes deterministic same-length/mtime-preserved edits,
append/truncate/replacement/missing sources, changes during copy/hash/discovery/
parsing/indexing/locator/final verification, late changes to earlier sources,
corrupt snapshots and invalid locators/headers/boundaries. It covers duplicate and
leading-zero IDs, multiline CSV pairs and missing response references. Separate
processes exercise competing owners, independent readers, OS process termination
at publication/index/seal boundaries, verified pre-seal manifest recovery and
immutable conflicts. Minimal-mode privacy tests inspect raw database/WAL, decoded
rows, all artifacts/manifests/staging files and captured diagnostics using distinct
student, exemplar, template and free-form metadata sentinels. YAML syntax failures
also produce safe diagnostics. Full-mode snapshots match exact source bytes.

## Original local resource measurements — commit `7d38305`

Fresh-process standalone measurements below include actual source preparation,
incremental renderer validation, first/last indexed lookup and a bounded sample of
64 unique request dependencies. No tasks, requests, attempts or results are persisted.
Times include tracing overhead. The required regression enforces native memory
below 256 MiB, Python allocations below 32 MiB and bounded growth between cases.

| Responses × traits | Source bytes | Python peak bytes | Native peak bytes | Seconds |
|---|---:|---:|---:|---:|
| 200 × 1 | 826,000 | 2,561,302 | 43,294,720 | 1.40 |
| 2,000 × 1 | 8,260,000 | 3,686,249 | 47,439,872 | 10.22 |
| 40 × 20 | 165,200 | 2,662,740 | 44,331,008 | 3.81 |
| 40 × 200 | 165,200 | 3,557,228 | 47,218,688 | 37.60 |

Tenfold source growth added approximately 1.07 MiB Python allocations; tenfold
task-product growth added approximately 0.85 MiB. A separate in-process indexed
lookup regression passed, as did 32 MiB streaming artifact publication with less
than 6 MiB traced memory. These are bounded source/preparation checks; the later
executor's larger qualification objectives are preserved without being claimed.

Reproduce the principal verification commands:

```bash
UV_CACHE_DIR=/tmp/judgmentllm-uv-cache uv run --offline --no-sync ruff check .
UV_CACHE_DIR=/tmp/judgmentllm-uv-cache uv run --offline --no-sync ruff format --check .
UV_CACHE_DIR=/tmp/judgmentllm-uv-cache uv run --offline --no-sync pyright
UV_CACHE_DIR=/tmp/judgmentllm-uv-cache JUDGMENTLLM_R_STRICT=1 uv run --offline --no-sync pytest --cov-report=json:/tmp/judgmentllm-source-preparation-coverage.json
UV_CACHE_DIR=/tmp/judgmentllm-uv-cache uv lock --check --offline
UV_CACHE_DIR=/tmp/judgmentllm-uv-cache uv build --offline
UV_CACHE_DIR=/tmp/judgmentllm-uv-cache uv run --offline --no-sync twine check --strict dist/judgmentllm-0.1.0a2*
UV_CACHE_DIR=/tmp/judgmentllm-uv-cache uv run --offline --no-sync python scripts/smoke_install.py dist/judgmentllm-0.1.0a2-py3-none-any.whl dist/judgmentllm-0.1.0a2.tar.gz
```

All four standalone strict R pipelines also passed. Their exact commands remain
in the required CI steps and interoperability documentation. Source preparation
uses independent `persist` and `export` Python processes followed by
`Rscript --vanilla examples/source_preparation_interoperability.R` with
`JUDGMENTLLM_R_STRICT=1`; missing prerequisites fail the required Ubuntu gate.

Local runtime tests use temporary local storage outside the synchronized checkout.
The OS/Python CI matrix remains Linux/macOS/Windows × Python 3.11/3.15 with its
existing 20-minute timeout. Local Linux results do not establish remote matrix
execution. Filesystems without atomic hard-link publication fail safely; Windows
lacks a standard-library directory-fsync equivalent. No silent durability fallback
is introduced.

## Files and compatibility

Production changes extend configuration/parser/planner helpers and persistence
artifacts/repository operations, add typed source records and the internal
`preparation` package. Tests cover indexing, integrity/privacy, manifests/rendering,
ownership/crashes, source persistence, resource behavior and cross-process R.
Supporting additions include source-preparation Python/R fixtures, a memory fixture,
installation smoke coverage, documentation and the fourth required R CI step.

The commit changes 34 files:

| Area | Added or changed files |
|---|---|
| Preparation (added) | `src/judgmentllm/preparation/{__init__,capture,freezing,index,manifest,records,session}.py` |
| Persistence | Added `src/judgmentllm/persistence/sources.py`; changed `artifacts.py`, `repository.py` in that package. |
| Existing shared boundaries | Changed `src/judgmentllm/{config,io,planning}.py`. |
| Tests | Added `tests/test_persistence_sources.py`, `test_preparation_{index,integrity,manifest,recovery,resources}.py`, `test_source_preparation_r.py`; extended `test_persistence_artifacts.py`. |
| Fixtures/install | Added `scripts/{preparation_memory_fixture,source_preparation_r_fixture}.py` and `examples/source_preparation_interoperability.R`; extended `scripts/smoke_install.py`. |
| CI/docs | Changed `.github/workflows/ci.yml`, `README.md`, `docs/{data-contracts,design-spec,implementation-plan,persistence-contracts,python-api,r-interoperability}.md`; added this verification record. |

No dependencies, lockfile changes, SQLite migration, normalized result schema,
R audit columns, category semantics or attempt-history changes are introduced.
Package version remains `0.1.0a2` for this partial milestone; the Milestone 2
`0.1.0a3` target remains assigned to milestone completion. The deliberate recovery
boundary (new run after incomplete ingestion; verified completion after fixed
manifest publication) implements the approved clarification without expanding into
execution recovery. No scope deviation is intended.

## PR #5 cross-platform corrections

The original [CI run](https://github.com/shmercer/judgmentllm/actions/runs/41556352396)
passed Linux/macOS Python 3.11 but failed both Windows configurations and both
Unix Python 3.15 configurations. The original Linux-only results above did not
establish cross-platform acceptance. The corrections retain the six-job matrix,
four required Ubuntu R gates, 90% coverage requirement and 20-minute timeout.

### Windows identity and generation metadata

The original comparison incorrectly treated `Path.stat()` and `os.fstat()` as
interchangeable observations on Windows. Native probes confirm that Windows
3.15.2 path `st_ctime_ns` equals birth time, while descriptor `st_ctime_ns` exposes
metadata change time. The original Windows 3.15 failures therefore occurred on
ordinary unchanged files. Windows 3.11.9 path and descriptor `ctime` both remained
creation time; it missed an in-place same-length edit with restored mtime because
creation time is not a change detector. The probe's stable device/inode values
agreed on both APIs on this runner. CPython 3.11 additionally has a path-stat
directory-attribute fallback that can omit those identity fields; that separate
fallback was not observed in the captured native probe.

The corrected Windows implementation opens an independent path handle, obtains
both handles' stable device/inode and size/mtime through `fstat`, and obtains
true change time with standard-library `ctypes` calling
[`GetFileInformationByHandleEx`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex),
[`FILE_BASIC_INFO.ChangeTime`](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info).
The probe is rechecked around comparison. Native timestamps are converted from
100 ns Windows-epoch units to Unix nanoseconds. Missing/unusable change metadata
fails closed. Unix behavior is unchanged. See the
[CPython issue](https://github.com/python/cpython/issues/157671) and the
[3.11 path-stat implementation](https://github.com/python/cpython/blob/3.11/Modules/posixmodule.c)
for the API differences.

Stable identity, available generation metadata and exact SHA-256/length remain
distinct. Independent full-byte hashing, complete-source final verification and
per-record canonical verification still apply. No descriptor/path comparison or
content-hash check is removed. Tests cover ordinary files, replaced paths,
different descriptors with equal bytes/timestamps, same-length/restored-mtime edits,
append/truncate, native query failure, simulated API discrepancies and full/minimal
reopening. A Windows-only CI step prints metadata from a synthetic temporary file;
it prints no input paths or assessment content.

The native Windows probes in
[correction run 41558368919](https://github.com/shmercer/judgmentllm/actions/runs/41558368919)
recorded these exact observations with unchanged byte length and restored mtime:

| Python | Raw path ctime before/after | Raw descriptor ctime before/after | Native normalized ChangeTime delta |
|---|---|---|---:|
| 3.11.9 | unchanged creation time | unchanged creation time | +91,781,900 ns |
| 3.15.2 | unchanged birth time | metadata change time advanced | +161,283,300 ns |

That intermediate run passed five jobs, including Windows 3.15, and exposed a
bad assumption in two new Windows 3.11 tests: even true change timestamps can
collide for tiny writes within one clock tick. Those tests now assert metadata
rejection when metadata actually changes and **always** assert exact-hash rejection.
No sleeps, skipped integrity cases or weakened hash checks are used. Minimal
indexed lookup additionally reopens and reparses the selected logical record/header
independently, verifies its ID/canonical hash again and checks generations around
that reread. This remains bounded to selected content and never scans the response
file for each pair. Deterministic CSV/JSONL tests force metadata to stay equal and
modify selected text after the first parse; the independent hash check rejects it.

The same five serialized signature fields and schema versions are retained.
Windows `ctime_ns` now consistently means actual metadata change time. A previously
created Windows manifest containing creation-time signatures is conservatively
incompatible with original-source continuation: full capture can use independently
verified snapshots only with explicit operation-scoped authorization; minimal
capture requires a new run. Frozen evidence is never rewritten. Structural IDs,
exact source hashes and generation commitments are unchanged.

### Memory investigation and resource cleanup

CI Python 3.15 reported in-process peaks of 3,325,360 → 9,689,288 bytes on Linux
and 3,171,893 → 9,762,684 on macOS. The unchanged test was reproduced on local
Python 3.15.2 with coverage 7.17 SysMonitor: 5,355,610 → 10,044,286 bytes, failing
the same +3 MiB bound. Without coverage the original workload peaked at
2,840,472 → 3,507,107 bytes and passed.

Tracemalloc snapshots identified compiled Jinja template code retained through
coverage instrumentation: the large case retained about 5.4 MB at
`jinja2/environment.py:709`, plus SysMonitor code dictionaries and generated
template allocations. Explicit collection barely changed live allocations
(7,777,031 → 7,662,479 bytes). Native peak rose about 78 → 98 MB. This was repeated
template compilation/instrumentation retention, not retained essay text or an
input-sized SQLite result list. Instrument-free subprocess measurements remained
bounded. The production correction removes the unnecessary compilations; the
original in-process +3 MiB assertion is retained unchanged and passes with Python
3.15 coverage. Existing subprocess/native bounds are also unchanged, with added
post-close/post-GC retention checks and optional allocation snapshots. Peak is
measured before collection; collection does not conceal transient allocations.

Python 3.15's SQLite warnings revealed test code using `with sqlite3.connect(...)`,
which controls transactions but does not close the connection. These contexts now
use `contextlib.closing`. Production failure cleanup also closes a constructed
prepared session, including descriptors opened during interrupted sealing recovery;
session close attempts every closer and SQLite even if another closer fails.
Connection configuration is inside the repository's initialization cleanup guard.
Failure-path tests verify closed descriptors/connections and idempotent cleanup.

### Rendering-preflight boundary

All source records, pair references, assessment/category/exemplar definitions,
grouping and generation capabilities are still validated before sealing.
Normal rendering preflight now checks syntax, sandbox and static settings once per
rubric group or pairwise criterion, even with empty inputs, using the existing
renderer's factored AST/compiler boundary. Where records exist, it fully renders
the first verified response/group or first verified pair/criterion, preserving
actual IDs and original/presented orientation. Missing contexts get static checks
only. Invented empty content is never used to decide protected-block inclusion:
doing so incorrectly rejected otherwise valid length/ID-dependent templates.
Regression tests exercise real-content conditions, reversed pair presentation and
empty conditional templates. No response-by-trait product is rendered, and normal
preflight does not claim every actual response will satisfy content-dependent
template conditions or output limits.

`PreparedRun.render()` reconstructs verified selected inputs, checks the dependency
commitment and fully renders/validates each actual request. Subphase 3 must call
this boundary and bind exact prompt plus actual adapter payload hashes before any
submission. Explicit `exhaustive_rendering=True` on internal `prepare_run` or
`validate_inputs` preserves full actual-input pre-rendering when requested. The
flag changes validation effort only and is excluded from RunSpec/manifest identity.
Both modes obey capture policy and do not retain prompts. Tests count renderer
calls independently of input size, verify empty-source validation, defer real-text
limit failures correctly, exercise exhaustive rejection and prove identical
manifest/request identities under both modes. No provider boundary is introduced.

Corrected local and remote verification results follow below; these are separate
from the original local results above.

| Corrected check | Result |
|---|---|
| Targeted reported failures | Initial 140 targeted cases passed; final focused prompt/rendering and integrity suites also passed. Full suites cover all 37 new signature/rendering/resource cases. |
| Full local Python 3.12 / coverage | **944 passed**, no skips or warnings, 53.76 seconds; **97.10%** combined coverage. |
| Full local Python 3.15 / coverage / strict resource warnings | **944 passed**, no skips or warnings, 36.32 seconds; **97.08%** combined coverage. Used `-Werror::ResourceWarning -Werror::pytest.PytestUnraisableExceptionWarning`. Original memory assertion passes unchanged under SysMonitor coverage. |
| Ruff / strict Pyright / lock / builds / metadata / clean installs | Passed: Ruff lint/format (84 Python files), zero Pyright errors/warnings, unchanged 93-package lock, wheel/sdist, strict Twine metadata and both current clean-install smoke checks. Sandbox DNS blocked final dependency resolution; an authorized network retry installed locked core dependencies. All installed behavioral checks remained offline. |
| Four strict R/jsonlite gates | Passed both full local suites and all four standalone offline pipelines: baseline, persistence, generation and source preparation. R 4.6.2/jsonlite 2.0.0; optional local pairwiseLLM checks also passed. |
| Six GitHub CI configurations | **All passed** on implementation commit `dadc41656b911b5c6c04abf3f09aeb312d07d53c`, [run 41559832676](https://github.com/shmercer/judgmentllm/actions/runs/41559832676). Native Windows 3.11 and 3.15 signature, restored-timestamp, lookup, full/minimal and installation checks passed. |

Final implementation CI matrix:

| Configuration | Result | Test evidence |
|---|---|---|
| Ubuntu / Python 3.11 | Passed | 944 tests; 97.10% coverage; all four required standalone R/jsonlite gates, wheel and sdist smoke checks. |
| Ubuntu / Python 3.15 | Passed | 938 passed, 6 expected skips; 97.08% coverage; no SQLite resource warnings. |
| macOS / Python 3.11 | Passed | Full lint/type/test/build/metadata/wheel-install job. |
| macOS / Python 3.15 | Passed | 938 passed, 6 expected skips; 97.08% coverage; original memory regression passes without changing its bound. |
| Windows / Python 3.11 | Passed | 938 passed, 6 expected skips; 96.82% coverage; 141.15-second test suite; build/metadata/wheel smoke passed. |
| Windows / Python 3.15 | Passed | 938 passed, 6 expected skips; 96.80% coverage; 116.62-second test suite; build/metadata/wheel smoke passed. |

The six skips outside the required Ubuntu/Python 3.11 R configuration are the
existing environment-dependent interoperability cases. No signature, memory,
source-integrity or rendering regression is skipped. The required R gates remain
strict and complete on Ubuntu/Python 3.11. This documentation-only follow-up
records the passing implementation run; the PR checks also track its final head.

Corrected independent-process Python 3.15 measurements (un-instrumented child,
tracemalloc enabled) were 2,278,013 → 2,596,668 peak Python bytes for 200 → 2,000
responses (826,000 → 8,260,000 source bytes). Post-GC retained allocations were
171,348 → 260,090 bytes, and native peaks 51,376,128 → 55,201,792 bytes. The original
peak +3 MiB growth bound, 32 MiB Python and 256 MiB native ceilings remain in force;
the resource regression also checks post-GC growth below 1 MiB. The 40-response ×
20/200-trait subprocess regression still samples only 64 dependencies and asserts
no execution rows. Deterministic render-count tests establish that normal preflight
work grows with configured groups/criteria, independently of input record count.

The correction touches only signatures, validation effort, resource cleanup,
focused tests/diagnostics and documentation. There is no schema migration, new
dependency, ID/generation/capture-policy change, provider adapter or Subphase 3
implementation. Native Windows metadata semantics and the conservative old-Windows
signature compatibility restriction above are the only source-compatibility refinement.

## Subphase 3 handoff

1. Consume verified ordered request dependencies incrementally. Preserve all
   existing task, pair-occurrence, request, and scheduling identities.
2. Integrate the mock provider boundary and bounded executor. Provider adapters
   own actual serialized payload construction; no invented provider bodies exist
   in this subphase. Bind exact rendered prompt and actual payload hashes/artifacts
   before submission; configuration/dependency hashes are not substitutes.
3. Apply current capabilities and source/manifest compatibility before further
   preparation/submission. Original capability evidence and snapshots remain
   immutable; generation compatibility alone never authorizes continuation.
4. Enforce executor ownership and journal submission intent before remote work.
   Implement conservative uncertainty/reconciliation and receipt recovery in
   Subphase 3; hashes cannot establish that a response existed or reconstruct
   privacy-minimal observations.
5. Keep grouped outcomes/retry lifecycle in Subphase 4 and lifecycle CLI/R process
   controls in Subphase 5. Retain the later 5,000-response × 20-trait/256 MiB and
   100,000-response stress objectives for full qualification.

PR #5 review now authorizes pushing these corrections. Merge, release and
Subphase 3 implementation remain excluded.
