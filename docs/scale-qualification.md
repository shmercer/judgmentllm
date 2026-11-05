# Offline scale qualification

Milestone 2 executes only the deterministic mock adapter. These tools measure
the existing indexed preparation, bounded executor, durable persistence and
immutable exports without credentials or provider calls. The
[Subphase 6 protocol and engineering record](milestone-2-subphase-6-verification.md)
defines acceptance and retains source-specific earlier evidence. The final
candidate's results are indexed outside Git at
`/mnt/VM_Fast/judgmentllm-subphase6/qualification/FULL_HEAD_SHA/ledger.json`.
Commands below are reproduction instructions, not evidence that a workload
completed. A missing, pending or failed applicable ledger entry does not satisfy
acceptance.

The revised completion scope reuses retained complete executions and adds only
focused checks needed to close acceptance gaps. No further full-size timing
repetitions, complete 100,000-judgment tracemalloc runs or full-run cProfile are
required. Optional status/history/rich-content/stress sweeps are not part of the
completion checklist. Commands below remain reproducible tools, not instructions
to restart those experiments.

## Workloads and acceptance

| Workload | Logical judgments | Planned physical requests | Purpose |
|---|---:|---:|---|
| Required prefixes: 200×20, 2,000×20, 200×200 | 4,000; 40,000; 40,000 | Independent: 4,000; 40,000; 40,000. Grouped by five: 800; 8,000; 8,000 | Existing six fresh-process memory regressions, **64 completed physical requests each** |
| Primary: 5,000×20, grouped by five | 100,000 | 20,000 | Complete preparation, execution, inspection, persistence, export and independent-process verification |
| Full capture: same 5,000×20 | 100,000 | 20,000 | Separate capture/storage cost assessment |
| Focused allocation: 200×20 versus 5,000×20, grouped by five | 4,000 versus 100,000 planned | 800 versus 20,000 planned; **64 completed in each sample** | Compare execution allocations after initialization; not full-run timing |
| Stress: 100,000×20 | 2,000,000 | 400,000 | Documented preparation/prefix/complete commands; execution is not required for acceptance |
| Million-task stress: 50,000×20 | 1,000,000 | 200,000 | Same documented extent distinctions; no completed stress qualification is claimed |

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
Use a short Windows root such as `C:\jbench`: immutable object/reference names
add hash components to that root. Native qualification exposed hard-link failures
with the tests' deeply nested 262/268-character destinations; shorter test-owned
roots preserve the same workload and all resource assertions.

```bash
REF=$(git rev-parse HEAD)
BENCH_ROOT=/mnt/VM_Fast/judgmentllm-subphase6/qualification/$REF
python3 ci/local/source.py --repo . --ref "$REF" --output "$BENCH_ROOT/bundle"
mkdir "$BENCH_ROOT/source"
tar -xf "$BENCH_ROOT/bundle/source.tar" -C "$BENCH_ROOT/source"
cp "$BENCH_ROOT/bundle/manifest.json" "$BENCH_ROOT/source/.judgmentllm-source.json"
```

Run from the matching source checkout using its prepared locked Python
environment. The harness loads the selected source through `--source-root`.
The retained complete runs are the practical qualification evidence. A fresh
single complete run can be reproduced with the command below when separately
needed; the revised completion task does not repeat it:

```bash
python benchmarks/execution_benchmark.py \
  --source-root "$BENCH_ROOT/source" --output "$BENCH_ROOT/primary-ordinary" \
  --work-root "$BENCH_ROOT/primary-work" \
  --workloads scale-rubric --responses 5000 --traits 20 --group-size 5 \
  --concurrency 4 --captures minimal --extent complete --modes plain \
  --warmups 0 --repetitions 1 --timeout 7200 \
  --disk-budget-gib 32 --min-free-gib 10
```

Full capture uses `--captures full` and a fresh output directory. Retain actual
sample counts. The completed minimal ordinary samples support their recorded
median/range/MAD; the limited full-capture pilot/warm-up observations do not
establish a repeated-sample throughput distribution. Exclude the interrupted
full-capture measured sample. Do not collect more full-size samples merely to
complete the earlier three-or-five-sample plan.

The earlier 3,600-second full-benchmark watchdog expired; the later operational
limit was 7,200 seconds. This is a benchmark resource limit, not a public runtime
guarantee or a change to the mandatory 180-second fixture deadline or 20-minute
hosted job limit. Preserve both the timeout and the later completed evidence.

### Focused allocation and export evidence

Run grouped prefixes in fresh processes for both minimal and full capture at
200 and 5,000 responses, twenty traits, concurrency four and five traits per
request. Keep rubric, exemplars, response bytes and seed identical. The focused
tool is `benchmarks/focused_execution.py`; its reports separate initialization
from execution and retain each fresh-process identity:

```bash
python benchmarks/focused_execution.py \
  --source-root "$BENCH_ROOT/source" --output "$BENCH_ROOT/focused-execution" \
  --small-responses 200 --large-responses 5000
```

The default matrix includes both captures and both source scales, with a fresh
process/store for every case and a 180-second deadline per focused child.
Exactly 64 physical requests complete in each execution sample;
prepared/accepted in-flight work is
recorded separately. Trace execution after source/configuration/index/session
initialization. Require execution peak below 32 MiB, peak growth below 3 MiB,
retained growth below 1 MiB and native growth below 48 MiB, with bounded
workers/tasks/queues. Record baseline, live and post-GC retained allocations.
Measure initialization separately from actual configuration loading through
source preparation/indexing and execution-session construction/closure. It must
create zero tasks and accept zero submissions. The focused tool measures this
path directly; preparation-only general-harness measurements may supplement it
without executing the response-by-trait product.

Measure actual serialization/publication of all 100,000 rows from retained
completed stores, with no new judgments. `benchmarks/export_allocation.py`
provides component measurements and an end-to-end service measurement using a
fresh R missing-value identity in a scratch copy. Use fresh external destinations
or disposable copies and preserve the originals. Require the 8 MiB export allocation
limit and exact row counts. Component outputs retain hashes matching the
published canonical JSONL/CSV and R CSV snapshots. The scratch service's explicit
new missing-value marker intentionally creates a different export identity/hash;
record that option and keep the original published snapshots unchanged. For example:

```bash
python benchmarks/export_allocation.py \
  --sample /absolute/path/to/passed-complete-sample.json \
  --output "$BENCH_ROOT/export-service" --kind service --format csv --view r \
  --expected-rows 100000 --na-string ALLOCATION_NA
python benchmarks/export_allocation.py \
  --sample /absolute/path/to/passed-complete-sample.json \
  --output "$BENCH_ROOT/export-jsonl" --kind component --format jsonl --view canonical \
  --metadata-report "$BENCH_ROOT/export-service/allocation.json"
```

Use a fresh output directory for each canonical CSV/R CSV component and each
capture. A fast return of an already published export is not evidence
of serialization cost. Record the measurement method and whether artifact
publication is included. An export-envelope pass must cover validation,
serialization and streamed publication; a serializer-only measurement is partial
evidence, not the complete export-memory claim. The mandatory grouped prefix
export regression remains unchanged.

Complete ordinary native RSS still includes the entire process and must remain
below 256 MiB; initialization is not subtracted. Focused Python prefixes do not
become complete-run allocation measurements. Any retained whole-scope absolute
phase evidence may supplement the record; baseline subtraction alone never
establishes a pass. Instrumented wall time is not ordinary throughput.

The controller writes `raw/*.json`, summaries and invocation/source identities.
Records retain each sample's exact counts, timing/CPU
phases, native/Python memory, concurrency, storage observations, export hashes,
byte lengths and result high-water marks. Retain failures and interrupted samples
with their exit status. Resume collection only with matching source, tool,
fixture and runtime identities. Never reuse completed databases as fresh samples.
Repeated samples in one fixed work root recreate the registry and service ledger;
they replace the previous physical database and artifact tree, while retaining
each raw JSON report and its hashes/counts. Before another mode reuses that path,
copy the last completed representative store for each capture to an external
evidence directory after worker/verifier processes have exited. Retain failed
stores and raw failure records before retrying with a fresh output/work root.
The original pilot's `qualified-work` tree remains untouched. Existing ordinary
runs use a separate `primary-work` directory, supplied with `--work-root` when shared
fixed paths are needed. Store snapshots supplement per-sample reports; do not
claim that every prior physical database was retained.
Once the final candidate is frozen, index resulting reports, logs and hashes in
`$BENCH_ROOT/ledger.json` without editing tracked source. Keep earlier e892/a2
pilot and 5bb7/a3 compatibility reports under their original identities. Evidence
from a later measurement commit also retains its exact identity; reuse for a
benchmark/documentation-only final commit requires an explicit reviewed
equivalence map covering production/fixture bytes and relevant benchmark
behavior. Explain tool-only additions and retain original harness/input hashes;
do not relabel the measurements.

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

These commands satisfy the documented stress-command deliverable. They have not
been executed as part of the revised completion scope and are not acceptance
gates. Run them only for a separately justified need.

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

The existing optional tools can investigate a specific unresolved acceptance
gap. They are not a sweep required for completion; do not run them merely because
they are available:

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
decision for Subphase 6 is to retain the full fixture as separate offline
qualification; no routine scale job or expensive R gate is added. Later promotion requires
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
the full test suite and total coverage ≥90% with branch measurement enabled on
Linux/3.11 only, all durability/privacy/recovery/resource regressions,
all seven strict R/jsonlite gates, lock verification, offline distributions,
strict Twine and clean wheel/source smokes. Commit the final proposed head and run:

```bash
python3 ci/local/grid.py --ref FULL_HEAD_SHA --mode check
```

The historical Subphase 6 qualification used six exact-commit local
configurations. Current publication requires seven (Linux 3.11/3.12/3.15 and
Windows/macOS 3.11/3.12), with coverage measured only on Linux/3.11 and disabled
elsewhere, followed by the five hosted jobs on the exact PR head. Historical
six-lane evidence remains valid for its stated scope; current local qualification
does not replace hosted 3.15. Follow [local CI isolation and locks](local-ci.md); do not overlap
ordinary performance collection with the grid, Docker operations or other heavy
checks. No provider adapter, HTTP server, R wrapper or package release is part of
these commands.
