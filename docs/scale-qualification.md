# Offline scale qualification

Milestone 2 executes only the deterministic mock adapter. These tools measure
the existing indexed preparation, bounded executor, durable persistence and
immutable exports without credentials or provider calls. The
[Subphase 6 verification record](milestone-2-subphase-6-verification.md) records
what actually ran and which acceptance gates remain pending. Commands below are
reproduction instructions, not evidence that a workload completed.

## Workloads and acceptance

| Workload | Logical judgments | Planned physical requests | Purpose |
|---|---:|---:|---|
| Required prefixes: 200×20, 2,000×20, 200×200 | 4,000; 40,000; 40,000 | Independent: 4,000; 40,000; 40,000. Grouped by five: 800; 8,000; 8,000 | Existing six fresh-process memory regressions, **64 completed physical requests each** |
| Primary: 5,000×20, grouped by five | 100,000 | 20,000 | Complete preparation, execution, inspection, persistence, export and independent-process verification |
| Full capture: same 5,000×20 | 100,000 | 20,000 | Separate capture/storage cost assessment |
| Stress: 100,000×20 | 2,000,000 | 400,000 | Preparation or explicitly bounded prefix by default; complete execution is separately requested |
| Million-task stress: 50,000×20 | 1,000,000 | 200,000 | Same distinction between preparation, prefix and complete execution |

The primary fixture uses concurrency four, five traits per physical request,
minimal capture, seed 42, leading-zero character IDs, 4 KiB synthetic response
texts, twenty described ordinal traits with nonuniform numeric mappings, three
1 KiB synthetic exemplars, and complete deterministic probability outputs.
Source bytes, rubric and exemplars are frozen and fingerprinted. Full capture
changes retention, not task membership or mock outcome semantics.

The primary complete run must finish all 100,000 judgments/20,000 physical
requests without unresolved work, missing rows or duplicate accepted submissions.
Its overall native process peak must remain **below 256 MiB**, including imports,
configuration, preparation and indexing. Separate execution allocation baselines
explain costs; they do not remove costs from that overall limit. Failure to meet
the complete fixture requirement remains a qualification failure.

The required prefix tests retain their existing parameters and assertions:
180 seconds per child, 64 completions, native RSS below 256 MiB, Python execution
peak below 32 MiB, peak growth below 3 MiB, native growth below 48 MiB, and post-GC
retained growth below 1 MiB. Grouped export allocation remains below 8 MiB with
growth below 1 MiB. Worker, task, queue, accepted/prepared-work and exact output
assertions stay mandatory. These tests establish bounded execution memory at a
controlled prefix; they do not establish complete-run throughput.

Stress commands have explicit bounds rather than inheriting the regression's
180-second deadline. Disk budgets and timeouts are operational stop limits;
reaching one records an incomplete/failed run, not successful qualification.
Large/rich variants do not replace the fixed primary fixture.

## Reproducible commands

Use an immutable checkout and the locked environment. Put source archives,
assessment inputs, databases, profiles and exports outside synchronized folders.
The examples use the existing `/mnt/VM_Fast` local filesystem; choose equivalent
local storage on Windows/macOS. Every output directory must be fresh. Record the
full Git SHA, archive hash and environment before collecting evidence.

```bash
REF=$(git rev-parse HEAD)
BENCH_ROOT=/mnt/VM_Fast/judgmentllm-scale/$REF
python3 ci/local/source.py --repo . --ref "$REF" --output "$BENCH_ROOT/bundle"
mkdir "$BENCH_ROOT/source"
tar -xf "$BENCH_ROOT/bundle/source.tar" -C "$BENCH_ROOT/source"
cp "$BENCH_ROOT/bundle/manifest.json" "$BENCH_ROOT/source/.judgmentllm-source.json"
```

Run from the matching source checkout using its prepared locked Python
environment. The harness loads the selected source through `--source-root`.
The primary ordinary run includes one fresh-process warm-up and three measured
fresh-process repetitions, executed serially:

```bash
python benchmarks/execution_benchmark.py \
  --source-root "$BENCH_ROOT/source" --output "$BENCH_ROOT/primary-ordinary" \
  --workloads scale-rubric --responses 5000 --traits 20 --group-size 5 \
  --concurrency 4 --captures minimal --extent complete --modes plain \
  --warmups 1 --repetitions 3 --timeout 3600 \
  --disk-budget-gib 32 --min-free-gib 10
```

First run a tiny pilot (`--responses 20 --warmups 0 --repetitions 1`), then one
full-size pilot. Review correctness, memory and disk results before repeating.
Stop after a failed pilot; identify a narrow remedy before spending time on more
samples. Full capture is a separate ordinary run using `--captures full` and a
fresh output directory, also with one warm-up and three measured samples. Extend
either capture variant to five when the observed elapsed range exceeds 10% of
its median or a CI-placement decision approaches the allowed budget.

Allocation runs are separate from ordinary timing. Run one complete primary
sample with `--modes tracemalloc --measurement-scope whole`, then one with
`--measurement-scope execution`, both with
`--warmups 0 --instrumented-repetitions 1 --timeout 10800` and fresh
output directories. The first captures preparation through exports; the second
starts execution tracing after source/configuration/index/session initialization
and traces export separately. Native peak always covers process lifetime.
Whole-scope tracing additionally records each phase's absolute traced peak,
baseline live bytes, live/retained values and post-GC growth. Its execution
absolute peak below 32 MiB or export absolute peak below 8 MiB is conservative
evidence for the corresponding allocation envelope because it includes retained
traced preparation state. Baseline-subtracted peak or net growth alone is not
that proof: earlier objects may be freed while new objects are allocated.
The separate execution-scope run remains the direct post-initialization measure
used for the execution-only 32 MiB and export-only 8 MiB targets. Preserve the
required prefix tests and their growth assertions independently of either full
trace. Resetting phase peaks must retain the maximum across all phases as the
whole-run Python peak; it never resets the lifetime native RSS limit.
Use `--modes profile` only if ordinary measurements identify a bottleneck needing
attribution. Traced, profiled or coverage-instrumented wall times are diagnostic
and must never be presented as ordinary throughput.

The controller writes `raw/*.json`, summaries and invocation/source identities;
raw stores remain in its work tree. Records include exact counts, timing/CPU
phases, native/Python memory, concurrency, storage observations, export hashes,
byte lengths and result high-water marks. Retain failures and interrupted samples
with their exit status. Resume collection only with matching source, tool,
fixture and runtime identities. Never reuse completed databases as fresh samples.

Run actual R integrity checks on each retained complete sample report:

```bash
Rscript --vanilla benchmarks/scale_integrity.R /absolute/path/to/raw/sample.json
```

The checker reads canonical JSONL and R CSV in bounded chunks, compares every
identity/outcome, complete probability vector and numeric coding, and verifies
expected scores, missing failure scores, provenance and declared row/byte counts.
The Python verifier separately checks immutable hashes, database/ledger attempt
and submission identities and no-op recovery in another process. Existing small
pairwise R gates retain original/presented-order and categorical-score coverage.

### Stress extents

Preparation/indexing only:

```bash
python benchmarks/execution_benchmark.py \
  --source-root "$BENCH_ROOT/source" --output "$BENCH_ROOT/100k-prepare" \
  --workloads scale-rubric --responses 100000 --traits 20 --group-size 5 \
  --captures minimal --extent prepare --modes plain \
  --warmups 0 --repetitions 1 --timeout 3600 \
  --disk-budget-gib 128 --min-free-gib 10
```

Bounded execution prefix over that input scale uses a fresh output directory,
`--extent prefix --completed-requests 64`. It proves only that prefix completed;
prepared and accepted in-flight counts are reported separately. For approximately
one million logical tasks use `--responses 50000 --traits 20` with either extent.

Actual complete million-task execution requires `--extent complete`, a fresh
directory and an explicit timeout/disk budget. This command is bounded, and is
not a promise of completion:

```bash
python benchmarks/execution_benchmark.py \
  --source-root "$BENCH_ROOT/source" --output "$BENCH_ROOT/million-complete" \
  --workloads scale-rubric --responses 50000 --traits 20 --group-size 5 \
  --captures minimal --extent complete --modes plain \
  --warmups 0 --repetitions 1 --timeout 86400 \
  --disk-budget-gib 128 --min-free-gib 10
```

Complete 100,000-response execution uses the same command with
`--responses 100000 --output "$BENCH_ROOT/100k-complete"`; it has two million
logical judgments. Report
tested counts and extents literally; do not call a prefix or extrapolation a
completed large-scale qualification.

## Persistence diagnostics and disk interpretation

Run targeted experiments separately, after ordinary timing samples finish:

```bash
python benchmarks/scale_diagnostics.py --case status --output "$BENCH_ROOT/status-control"
python benchmarks/scale_diagnostics.py --case status --inspect --output "$BENCH_ROOT/status-1hz"
python benchmarks/scale_diagnostics.py --case wal --output "$BENCH_ROOT/wal-reader"
python benchmarks/scale_diagnostics.py --case history --output "$BENCH_ROOT/history"
Rscript --vanilla benchmarks/scale_integrity.R "$BENCH_ROOT/history/diagnostic.json"
```

Status control and 1 Hz inspection each execute 100×20: 2,000 judgments and
400 physical requests. Compare serial repeated samples before making overhead
claims; one pair is a diagnostic observation. Inspection latency is recorded.

The WAL experiment executes 30×20 (600 judgments/120 physical requests), holds
one real read snapshot through 100 result commits, pauses the writer, records
PASSIVE checkpoint progress, releases the reader and requires checkpoint catchup
within 30 seconds. It preserves WAL and `synchronous=FULL`. Its deliberate
checkpoint operations and pinned-reader growth are excluded from ordinary timing.

The history experiment starts with 20×20 (400 judgments/80 requests). One group
has two valid members plus invalid/missing/duplicate member failures. A three-member
retry produces 403 result rows/81 acceptances. Two waves of fifty explicit
single-member completed retries produce 503 rows/181 acceptances while keeping
every parent within its three-attempt budget. Old 400/403-row exports retain
their IDs/hashes. A separate process reopens/reconciles without submitting again.

Storage records distinguish registry and mock-ledger DB/WAL/SHM, artifact/object
bytes, exports, staging/temporary bytes, logical file size, allocated blocks where
available, and filesystem free space. Temporary classification includes
`.export-*`, `.pending-*`, `.stage-*` and `.tmp*` staging paths. Inventory logical
and allocated sizes sum pathnames: manifest/export hardlink aliases count again,
so these totals conservatively overcount unique physical bytes. Allocated bytes
are actual `st_blocks × 512` where available and null otherwise. Filesystem
free-space samples measure the entire volume, including unrelated writes.

Sampled high-water is an observation, not a guaranteed continuous peak.
End-of-run WAL disappearance does not establish small peak WAL use. Retain
phase-boundary measurements and active-reader evidence. The
`after_export_serialization` inventory includes the export staging file;
`after_export_artifact` runs after staging cleanup. Brief overlap between that
file and ArtifactStore's `.pending-*` publication copy may fall between samples.
Report `maximum observed phase pathname bytes + maximum observed export staging
bytes` as a conservative **single-export copy-overlap estimate**, alongside
sampled volume growth; do not label the estimate a continuously measured peak.
This estimate covers an additional complete staging copy and can overcount
hardlink aliases. Preserve its assumptions: exports run serially, finished export
artifacts remain retained, and no other benchmark write phase overlaps export.
Earlier tool fingerprints lacking `.pending-*` classification must keep that
measurement limitation in their evidence rather than being relabeled.

Multiple immutable history exports remain separate retained artifacts. Cleanup
must target only owned disposable paths after evidence and hashes are saved.

## CI and qualification boundaries

Keep the existing small deterministic memory regressions in required CI. The
full fixture defaults to separate offline qualification. Promotion requires
measured complete-job timings on every affected hosted configuration, including
setup, coverage, seven applicable R gates, build and artifacts, with at least
three minutes remaining below the unchanged 20-minute timeout after accounting
for observed variation. Do not add it to all R gates or run it under coverage by
accident. Do not increase timeouts, reduce existing tests/coverage or relax limits.

Baseline merged run `41951607071` passed all five jobs: Ubuntu 3.11 10:38,
Ubuntu 3.15 7:16, macOS 3.11 6:05, macOS 3.15 7:34, Windows 3.12 16:10.
The reviewed preceding PR run's Windows 18:38 leaves only 82 seconds; the faster
merged sample does not erase that observed variability. Local Ubuntu benchmark
results alone cannot establish hosted Windows headroom. Keep the full fixture
offline unless the stronger promotion criterion is demonstrated.

Final publication qualification still requires Ruff lint/format, strict Pyright,
full branch coverage ≥90%, all durability/privacy/recovery/resource regressions,
all seven strict R/jsonlite gates, lock verification, offline distributions,
strict Twine and clean wheel/source smokes. Commit the final proposed head and run:

```bash
python3 ci/local/grid.py --ref FULL_HEAD_SHA --mode check
```

Require all six exact-commit local configurations, followed by the five hosted
jobs on the eventual exact PR head. Local 3.11/3.12 qualification does not replace
hosted 3.15. Follow [local CI isolation and locks](local-ci.md); do not overlap
ordinary performance collection with the grid, Docker operations or other heavy
checks. No provider adapter, HTTP server, R wrapper or package release is part of
these commands.
