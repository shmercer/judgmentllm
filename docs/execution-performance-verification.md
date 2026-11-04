# Execution performance verification

Current context (Subphase 5): this is a dated verification record. PR #8 and
PR #9 are merged into main `01fa7e47b86f36370f8df017873cf41668b2f363`;
its five hosted jobs passed in run `41909583052`. Current hosted CI is
Ubuntu/macOS Python 3.11/3.15 plus Windows 3.12, with optional full Windows
3.11 dispatch. Keep the historical six-job results and failures below as evidence.
The [Subphase 5 report](milestone-2-subphase-5-verification.md) records the new
public lifecycle and seventh R gate; it does not replace these measurements.

This is a separate engineering task after Milestone 2 Subphase 4. The baseline
is squash merge `2cd12c5914698ccc0498b3004cb4de03cee397bf`. No Subphase 5
lifecycle, public schema, version, hashing or generation-default change is part
of this work.

## Environment and reproducible method

Local measurements use Linux x86_64, Python 3.12.3, SQLite 3.45.1, Pydantic
2.14.2, coverage 7.17.0, and the locked development environment. R 4.6.2 and
jsonlite 2.0.0 are available. Native Windows/macOS and Python 3.11/3.15 acceptance
remain the six unchanged CI configurations; local Linux results do not replace
their final-head checks.
Collection spans November 3–4, 2026 UTC; retained sample and invocation timestamps
give the individual measurement windows.

The host has an AMD Ryzen 9 5900XT processor (16 cores, 32 logical CPUs) and
125.7 GiB reported RAM.
Runtime stores and restored databases reside under `/tmp` on local ext4
(`/dev/mapper/data-root`, `noatime`), rather than the Dropbox working directory.
CPU frequency, thermals and OS page-cache state are not externally controlled;
warm-ups, repeated ordinary samples and their observed dispersion bound the
claims made here.

The development-only `benchmarks/execution_benchmark.py` launches every sample in
a separate process, serially. It imports the requested checkout's `src` through
`PYTHONPATH`; the baseline is an immutable `git archive` extraction under `/tmp`.
Each sample records a package/assets/config/lockfile content fingerprint and
runtime dependency versions. Resuming rejects differing source/environment
identities; optimized measurements also use an immutable revision archive.
The finalized performance-tool/RSS-helper fingerprint is
`575c55048ecbf4dfd4d3cad1461216fc38e0c3af485d79a3bc6664859dca8b46`;
the fixture-definition fingerprint is
`c9aaa030b5e2f22a8988b3bfe40e693483dbbd1231f682ae9f2f7c54852deff2`.
Assessment source files are written once at fixed paths per case/capture and
remain unchanged across repetitions. Before **each** sample, SQLite's backup API
restores an empty pristine registry and an empty pristine independent acceptance
ledger with fixed synthetic identity 42. Restoration and fixture input creation
are outside measured phases. No completed run, prepared session, accepted
observation, in-process validation cache or populated provider ledger is reused.
Optimized/variant collectors point `--work-root` at the baseline working tree,
preserving original source paths, files/generations and pristine database backups
across revisions while keeping raw evidence in separate output folders.

The filesystem and OS page caches are not flushed. Every case/capture receives
one fresh-process plain warm-up followed by five measured samples. This describes
warm filesystem-cache behavior without claiming controlled cold-disk behavior.
Instrumented passes are separate: one default-timer cProfile/counter sample and
one each with tracemalloc alone, coverage alone, and both together. These single
samples are diagnostic, not statistically reliable overhead or memory-improvement
estimates. Selected configurations receive three runs only if an observed
variability, memory or attribution anomaly warrants the additional evidence.
Ordinary full-capture timing is the primary CPU/wall assessment; minimal capture is also
measured throughout the matrix and remains the resource-regression baseline.

| Case | Responses | Traits/criteria | Physical requests |
|---|---:|---:|---:|
| Independent | 200 | 3 | 600 |
| Grouped, all six | 200 | 6 | 200 |
| Independent | 200 | 20 | 4,000 |
| Grouped, five/member | 200 | 20 | 800 |
| Grouped, five/member, bounded prefix | 200 | 200 | **64 completed sample only** |
| Pairwise | 200 | 3 | 1,200 |
| Failed-member subset retry | 200 | 20 | 800 initial + 800 one-member retries |

All cases use concurrency four, leading-zero character IDs, 4 KiB source text,
and seed 42. The pairwise case has 100 base pairs, three criteria, two repetitions,
and both orientations. Retry groups initially have one invalid member each;
explicit retry operations select that member alone. The 200-trait case stops
after the established 64 physical result commits and reports bounded prepared
and accepted in-flight work separately. It is **not** full 8,000-request
throughput or later large-scale qualification.
The prefix also preserves the established low/high categories without explicit
numeric codes, original rubric name and default mock observation recipe. Other
rubric cases use three nonuniform numeric categories and probability
observations. Pairwise cases use binary probabilities over the compared item IDs.
Preparation/full capture/all-export timing and separate instrumentation make the
new paired measurements distinct from earlier exploratory 62-second timings.

The total timed interval covers preparation, execution/retry authorization, and
all three immutable exports (canonical JSONL, canonical CSV, and R CSV).
`perf_counter_ns` measures wall time and `process_time_ns` measures whole-process
CPU. Light synchronous phase hooks, identical across revisions, additionally
record rendering, provider payload preparation and result processing. Synchronous
thread CPU is exclusive within each thread; inclusive phase wall durations can
overlap and must not be summed. Async submission/retrieval record latency, not
CPU inferred across an `await`.

Profiling probes whether independent per-thread cProfile objects are supported.
Where supported, separate main, I/O and provider-executor profiles are merged
after shutdown. This CPython 3.12 environment instead permits one active
interpreter-wide profiler; it covers all threads and rejects a second profiler.
Function wall durations are advisory under concurrency; explicit synchronous
thread clocks provide CPU attribution. Profiler caller-edge dictionaries are
disabled (`subcalls=False`); function self/cumulative times and
primitive/total counts remain available, with built-in calls enabled. Counters
report core Pydantic validation entry points, `plan_run`/rendering, source guards,
file verification, total JSON serialization bytes (`json.dumps` plus Pydantic's
`model_dump_json`), canonical serialization bytes as a subset, byte digests,
actual prepared payload bytes, and SQLite operations/transactions where observable. Profiling
checks its scope with known main/I/O/provider thread markers and exactly four
native Pydantic entries before collection. Payload-specific byte-digest counts
recognize the fixed mock protocol marker without retaining bytes; streamed
artifact verification is counted separately rather than inferred from that
content counter. Canonical bytes are not added to the JSON total a second time.
Broad validation/planning/hash/serialization/SQLite counters are collected only
in profile mode. Empty counter dictionaries in other modes mean unobserved,
rather than zero operations; their light phase timers remain enabled.
CSV/JSONL published byte lengths are also recorded directly from immutable export
reports. Counters store numeric counts/times only, with no copied assessment content. Allocation
passes report peak, live and retained-after-GC Python bytes, portable native peak
RSS, actual task creation/high-water counts, and both queue high-water marks.
Task/queue tracking is enabled in profile, tracemalloc and combined modes;
coverage-only and ordinary values remain unobserved/null. Observed timing ratios
therefore describe each complete instrumentation configuration, including its
tracking hooks, rather than isolating the tracer's cost alone.
These whole-run allocation figures supplement rather than replace the unchanged
post-initialization resource fixtures and their limits.
The timed/profiled/traced window begins after fixture construction and package
imports, immediately before preparation, and ends after export. Existing fixture
objects allocated before tracing are excluded from Python allocation totals;
native peak RSS covers the process lifetime. This measures preparation and
execution rather than installed-CLI startup.
Pydantic totals count native validation API entries across all measured threads,
with `model_validate` calls reported separately; nested native schema nodes are
not individually enumerated. `plan_run` counts describe complete plan
reconstruction; selected-group identity construction still runs for each request.

Coverage-only and combined diagnostics use the retained
[benchmark coverage configuration](../benchmarks/evidence/benchmark.coveragerc),
SHA-256 `52d463fdace059892e1a59466830532c0c2bdc43378ea73dbed505cf3d8d7bb9`.
It preserves the project's source, branch, relative-file, reporting and 90%
settings and disables only `module-not-measured`: package imports intentionally
precede this execution window. The first baseline independent-3/full coverage
sample completed successfully using the project config, then emitted that
warning during `coverage.save()` after wall/CPU recording. It is retained with
that explicit condition; its data contains 31 nonempty measured baseline source
files. Subsequent diagnostics use the external RC identically for both revisions.
Exact contents, environment and invocation conditions are retained separately
from actual historical tool hashes. All other diagnostic stderr remains fatal.
The benchmark RC is used only for diagnostics. Final pytest qualification
explicitly uses `COVERAGE_RCFILE=pyproject.toml`, preserving the project warning
configuration and all pytest/CI warning gates.

Example commands (all raw output and runtime stores stay outside distributions):

```bash
mkdir -p /tmp/judgmentllm-performance-baseline
git archive 2cd12c5914698ccc0498b3004cb4de03cee397bf | tar -x -C /tmp/judgmentllm-performance-baseline
mkdir -p /tmp/judgmentllm-performance-optimized-snapshot-final
git archive 607249d7746f58b84d80484e89bd6074d699ffa8 | tar -x -C /tmp/judgmentllm-performance-optimized-snapshot-final
.venv/bin/python benchmarks/execution_benchmark.py --source-root /tmp/judgmentllm-performance-baseline --output /tmp/judgmentllm-performance/baseline --modes plain
.venv/bin/python benchmarks/execution_benchmark.py --source-root /tmp/judgmentllm-performance-baseline --output /tmp/judgmentllm-performance/baseline --modes profile tracemalloc --resume
COVERAGE_RCFILE=benchmarks/evidence/benchmark.coveragerc .venv/bin/python benchmarks/execution_benchmark.py --source-root /tmp/judgmentllm-performance-baseline --output /tmp/judgmentllm-performance/baseline --modes coverage combined --resume
.venv/bin/python benchmarks/execution_benchmark.py --source-root /tmp/judgmentllm-performance-optimized-snapshot-final --work-root /tmp/judgmentllm-performance/baseline/work --output /tmp/judgmentllm-performance/optimized --modes plain profile tracemalloc
COVERAGE_RCFILE=benchmarks/evidence/benchmark.coveragerc .venv/bin/python benchmarks/execution_benchmark.py --source-root /tmp/judgmentllm-performance-optimized-snapshot-final --work-root /tmp/judgmentllm-performance/baseline/work --output /tmp/judgmentllm-performance/optimized --modes coverage combined --resume
```

The harness writes per-sample JSON and median/min/max/median-absolute-deviation
summaries. Profiling tools and raw results are excluded from wheel/sdist content.
The [retained numeric evidence](../benchmarks/evidence/execution-performance.json)
(about 428 KiB) contains source/runtime/tool/config fingerprints, all five
ordinary wall/CPU/RSS samples and phase medians per case, and every single
diagnostic timing/allocation/concurrency/counter/CPU-phase summary. It also keeps
exclusion/progress conditions and exact export hash/byte/row metadata. Actual
unknown historical hashes remain null beside explicit compatibility annotations.
Assessment text, prompts, payloads, observations and full profiler listings are
excluded. Large raw profiles/runtime stores remain under `/tmp`.
Six initial ordinary samples overlapped short development checks while the
collector was being finalized. They were quarantined, excluded from summaries,
and replaced at the same case/repetition, with fresh warm-ups.
Subsequent ordinary timing windows exclude lint/type/test/profile subprocesses.
Historical ordinary tool-byte hashes were not recorded initially; their later
compatibility annotations explicitly record that limitation. The ordinary timed
hooks remained unchanged; profile-only counters and out-of-clock provenance were
refined during collection. Finalized instrumentation uses exact tool/fixture
fingerprints and preserves original methodology with append-only invocation logs.
Later collector invocations support a `PAUSE` file in the output directory. The
controller finishes its active child before publishing `PAUSED`; checks may run
only after that marker appears, and removing `PAUSE` resumes the serial matrix.
Existing resource fixture workloads, assertions, 64-request samples and
180-second timeouts remain unchanged. The full benchmark's separate subprocess
timeout accommodates complete 4,000-request cases; it is not a change to a test
timeout.

## Exploratory evidence before implementation

A preliminary 200×200/group-five, 64-request profile confirmed repeated recursive
model extraction as a useful target. With default cProfile and no tracemalloc it
took 40.00 seconds wall and 39.45 process CPU seconds. Cumulative function times
overlap: `_revalidate_record` 31.69 seconds, compatibility verification 28.29,
canonical serialization 16.41, configuration digest 8.00, generation compatibility
6.48, mock preparation 4.56 and mock validation 4.29. Actual selected prompt
rendering was 0.46, planning 0.40, SQLite execute 0.68, and source guards 0.03.
`_record_data` alone consumed 10.66 self seconds across 19.66 million calls.
These exploratory values select investigation targets; they are not comparable
before/after production timing or a speedup claim.

The full 4,000-request independent baseline also exposed a disk-write concern
outside the bounded Python-memory sample. A lightweight read-only progress check
at 3,600 durable results observed a 2,465,354,472-byte registry WAL. This is an
observed file size, **not a measured peak**. Long-lived source cursors can prevent
checkpoint completion while the execution thread writes. Final repeated timings
and byte accounting determine whether a bounded, fully fetched page of immutable
input IDs is justified; WAL/SHM correctness and durability settings must remain
unchanged.

An initial custom thread-CPU cProfile timer combined with tracemalloc produced
impossible negative and oversized per-function durations. That ranking was
rejected. Final profiling uses the default timer, with thread CPU measured only
at explicit synchronous phase hooks. Restricted-sandbox asyncio/thread execution
also stalls in this environment, as previously documented; live fixtures run
outside that sandbox without changing the fixtures or assertions.

## Baseline and optimized measurements

All **70 ordinary baseline samples** are qualified: five fresh-process measurements for each case/capture, after one warm-up. Six independently identified overlapping samples were replaced; their values do not enter these summaries. All request/result/task/attempt and acceptance checks passed.

Times are median ± median absolute deviation; raw JSON also records min/max. RSS is the ordinary process median, not traced allocation data. The 200-trait row remains a 64-completion diagnostic prefix.

| Case | Capture | Wall s | CPU s | CPU ms/physical completion | Native RSS MiB |
|---|---|---:|---:|---:|---:|
| independent-3 | full | 27.366 ± 0.062 | 20.251 ± 0.040 | 33.752 | 66.06 |
| independent-3 | minimal | 24.063 ± 0.142 | 19.311 ± 0.148 | 32.185 | 65.67 |
| grouped-6 | full | 12.817 ± 0.061 | 10.339 ± 0.065 | 51.694 | 59.07 |
| grouped-6 | minimal | 11.761 ± 0.149 | 10.048 ± 0.092 | 50.241 | 61.06 |
| independent-20 | full | 256.371 ± 1.375 | 207.093 ± 0.912 | 51.773 | 77.60 |
| independent-20 | minimal | 244.641 ± 1.903 | 208.460 ± 0.251 | 52.115 | 73.81 |
| grouped-20 | full | 60.325 ± 0.795 | 50.709 ± 0.713 | 63.386 | 69.74 |
| grouped-20 | minimal | 55.138 ± 0.027 | 48.593 ± 0.025 | 60.741 | 69.35 |
| grouped-200-prefix | full | 18.680 ± 0.361 | 15.518 ± 0.049 | 242.470 | 55.56 |
| grouped-200-prefix | minimal | 17.033 ± 0.244 | 15.260 ± 0.030 | 238.437 | 56.10 |
| pairwise | full | 58.338 ± 0.137 | 44.256 ± 0.321 | 36.880 | 68.05 |
| pairwise | minimal | 53.390 ± 0.039 | 43.786 ± 0.068 | 36.489 | 67.31 |
| subset-retry | full | 121.470 ± 1.361 | 103.796 ± 1.084 | 64.873 | 72.80 |
| subset-retry | minimal | 111.042 ± 0.368 | 99.287 ± 0.328 | 62.055 | 71.12 |

All **70 final snapshot-first optimized ordinary samples** passed. Commit
`607249d` was measured from its immutable archive, using the literal baseline
sources/pristine stores and the same five-sample method. All 14 case/capture
configurations have separated baseline/optimized wall ranges and CPU ranges.
No ordinary CPU or wall regression occurred. Superseded source rows are
retained separately and excluded.

Speedups below are baseline median divided by optimized median, shown as
wall/CPU. Times are median ± median absolute deviation; the retained numeric
evidence also includes every sample and min/max. CPU per physical completion
divides the entire timed interval, including preparation and export, by completed
physical requests. The 200-trait row remains a bounded 64-completion prefix.

| Case | Capture | Optimized wall s | Optimized CPU s | CPU ms/physical completion | Wall / CPU speedup | Native RSS MiB, baseline → optimized |
|---|---|---:|---:|---:|---:|---:|
| independent-3 | full | 22.727 ± 0.096 | 16.700 ± 0.045 | 27.834 | 1.204× / 1.213× | 66.06 → 67.19 |
| independent-3 | minimal | 19.631 ± 0.045 | 15.851 ± 0.042 | 26.418 | 1.226× / 1.218× | 65.67 → 66.29 |
| grouped-6 | full | 10.956 ± 0.039 | 8.752 ± 0.036 | 43.760 | 1.170× / 1.181× | 59.07 → 60.62 |
| grouped-6 | minimal | 9.751 ± 0.015 | 8.375 ± 0.003 | 41.876 | 1.206× / 1.200× | 61.06 → 63.18 |
| independent-20 | full | 199.517 ± 0.926 | 158.635 ± 0.539 | 39.659 | 1.285× / 1.305× | 77.60 → 74.55 |
| independent-20 | minimal | 179.890 ± 0.771 | 153.437 ± 0.416 | 38.359 | 1.360× / 1.359× | 73.81 → 70.99 |
| grouped-20 | full | 48.235 ± 0.080 | 39.648 ± 0.102 | 49.560 | 1.251× / 1.279× | 69.74 → 69.21 |
| grouped-20 | minimal | 43.697 ± 0.027 | 38.138 ± 0.024 | 47.673 | 1.262× / 1.274× | 69.35 → 69.11 |
| grouped-200-prefix | full | 10.503 ± 0.056 | 9.720 ± 0.057 | 151.878 | 1.779× / 1.596× | 55.56 → 58.09 |
| grouped-200-prefix | minimal | 10.021 ± 0.045 | 9.565 ± 0.049 | 149.457 | 1.700× / 1.595× | 56.10 → 58.72 |
| pairwise | full | 48.858 ± 0.154 | 37.027 ± 0.060 | 30.856 | 1.194× / 1.195× | 68.05 → 68.59 |
| pairwise | minimal | 46.316 ± 0.090 | 38.578 ± 0.030 | 32.148 | 1.153× / 1.135× | 67.31 → 67.04 |
| subset-retry | full | 107.421 ± 0.256 | 90.777 ± 0.139 | 56.736 | 1.131× / 1.143× | 72.80 → 72.38 |
| subset-retry | minimal | 98.278 ± 0.128 | 87.629 ± 0.142 | 54.768 | 1.130× / 1.133× | 71.12 → 71.59 |

Full-capture CPU falls by **12.5–23.4%** across complete workloads and by
**37.4%** in the bounded 200-trait prefix. Corresponding wall reductions are
11.6–22.2% and 43.8%. The largest complete full case improves from 207.093 to
158.635 CPU seconds and 256.371 to 199.517 elapsed seconds. Minimal-capture
complete workloads improve by 11.7–26.4% CPU. Ordinary RSS is not uniformly
lower: optimized medians are up to 2.63 MiB higher, while the largest complete
full case uses 3.05 MiB less. Allocator/layout effects and retained metadata are
not isolated by these RSS observations. Existing allocation/native-RSS regression
gates pass. Sequential case batches support within-case paired comparisons
rather than a controlled estimate of the privacy mode's cost.

## Ordinary phase comparison

Each cell is baseline → optimized, in seconds, from five ordinary fresh-process
samples per revision. CPU values are medians of exclusive synchronous thread
CPU. Wall values are medians of inclusive hook durations; nested
`render`/`render_attempt` hooks and concurrent work can overlap. Submission and
retrieval are async latency sums and carry no inferred CPU attribution. These
columns must not be summed to reconstruct total execution time.

| Case | Capture | Preparation CPU | Selected rendering CPU | Payload preparation CPU | Result processing CPU | Export CPU |
|---|---|---:|---:|---:|---:|---:|
| independent-3 | full | 0.087 → 0.086 | 5.044 → 4.098 | 1.283 → 1.257 | 3.487 → 3.363 | 0.556 → 0.549 |
| independent-3 | minimal | 0.080 → 0.081 | 5.562 → 4.637 | 1.272 → 1.238 | 2.924 → 2.805 | 0.543 → 0.534 |
| grouped-6 | full | 0.085 → 0.085 | 2.008 → 1.625 | 0.654 → 0.648 | 2.182 → 2.141 | 1.076 → 1.073 |
| grouped-6 | minimal | 0.081 → 0.082 | 2.212 → 1.817 | 0.662 → 0.647 | 1.892 → 1.835 | 1.073 → 1.059 |
| independent-20 | full | 0.117 → 0.119 | 63.474 → 50.921 | 16.085 → 16.018 | 28.377 → 25.777 | 3.524 → 3.552 |
| independent-20 | minimal | 0.113 → 0.114 | 68.928 → 55.017 | 16.557 → 15.979 | 26.309 → 22.061 | 3.558 → 3.489 |
| grouped-20 | full | 0.098 → 0.098 | 13.045 → 10.105 | 3.827 → 3.718 | 8.248 → 8.033 | 3.543 → 3.486 |
| grouped-20 | minimal | 0.088 → 0.091 | 13.638 → 10.831 | 3.770 → 3.718 | 7.027 → 6.875 | 3.530 → 3.458 |
| grouped-200-prefix | full | 0.217 → 0.230 | 5.798 → 4.024 | 1.641 → 1.583 | 0.595 → 0.577 | 0.229 → 0.227 |
| grouped-200-prefix | minimal | 0.215 → 0.228 | 5.856 → 4.070 | 1.636 → 1.584 | 0.501 → 0.481 | 0.226 → 0.223 |
| pairwise | full | 0.800 → 0.775 | 12.679 → 11.390 | 2.185 → 2.026 | 7.163 → 6.677 | 0.902 → 0.882 |
| pairwise | minimal | 0.770 → 0.763 | 15.883 → 15.072 | 2.063 → 2.003 | 5.772 → 5.540 | 0.877 → 0.864 |
| subset-retry | full | 0.096 → 0.096 | 30.347 → 30.265 | 8.815 → 8.587 | 13.039 → 12.759 | 4.161 → 4.109 |
| subset-retry | minimal | 0.089 → 0.091 | 32.064 → 32.046 | 8.613 → 8.547 | 10.893 → 10.847 | 4.055 → 4.025 |

| Case | Capture | Preparation wall | Selected rendering wall | Payload preparation wall | Submission latency | Retrieval latency | Result processing wall | Export wall |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| independent-3 | full | 0.101 → 0.103 | 5.050 → 4.102 | 1.283 → 1.257 | 3.262 → 3.240 | 1.204 → 1.172 | 7.222 → 6.711 | 0.641 → 0.658 |
| independent-3 | minimal | 0.090 → 0.091 | 5.565 → 4.641 | 1.272 → 1.238 | 3.256 → 3.229 | 1.405 → 1.361 | 5.887 → 5.422 | 0.610 → 0.612 |
| grouped-6 | full | 0.100 → 0.099 | 2.009 → 1.627 | 0.656 → 0.650 | 1.219 → 1.192 | 0.525 → 0.519 | 3.551 → 3.402 | 1.132 → 1.154 |
| grouped-6 | minimal | 0.093 → 0.095 | 2.213 → 1.819 | 0.664 → 0.648 | 1.228 → 1.195 | 0.603 → 0.588 | 2.996 → 2.836 | 1.158 → 1.115 |
| independent-20 | full | 0.131 → 0.135 | 63.495 → 50.943 | 16.091 → 16.020 | 22.084 → 21.569 | 7.878 → 7.768 | 53.844 → 48.363 | 3.626 → 3.724 |
| independent-20 | minimal | 0.123 → 0.124 | 68.956 → 55.044 | 16.567 → 15.981 | 22.472 → 21.691 | 9.426 → 9.062 | 47.432 → 40.083 | 3.669 → 3.587 |
| grouped-20 | full | 0.115 → 0.113 | 13.052 → 10.110 | 3.828 → 3.718 | 4.792 → 4.667 | 2.021 → 1.966 | 13.679 → 13.140 | 3.672 → 3.583 |
| grouped-20 | minimal | 0.098 → 0.102 | 13.645 → 10.838 | 3.772 → 3.718 | 4.750 → 4.695 | 2.279 → 2.231 | 11.414 → 10.864 | 3.594 → 3.564 |
| grouped-200-prefix | full | 0.232 → 0.246 | 5.801 → 4.026 | 1.642 → 1.585 | 0.550 → 0.468 | 0.166 → 0.184 | 1.165 → 0.976 | 0.239 → 0.241 |
| grouped-200-prefix | minimal | 0.227 → 0.240 | 5.859 → 4.073 | 1.636 → 1.585 | 0.510 → 0.477 | 0.193 → 0.192 | 0.860 → 0.777 | 0.237 → 0.237 |
| pairwise | full | 0.855 → 0.829 | 12.688 → 11.394 | 2.186 → 2.027 | 6.555 → 6.410 | 2.340 → 2.249 | 14.591 → 13.314 | 1.020 → 0.953 |
| pairwise | minimal | 0.798 → 0.828 | 15.890 → 15.080 | 2.064 → 2.006 | 6.446 → 6.418 | 2.702 → 2.630 | 11.553 → 10.683 | 0.983 → 0.949 |
| subset-retry | full | 0.111 → 0.112 | 30.359 → 30.278 | 8.817 → 8.588 | 9.237 → 9.076 | 3.638 → 3.577 | 23.055 → 22.388 | 4.238 → 4.253 |
| subset-retry | minimal | 0.099 → 0.101 | 32.073 → 32.060 | 8.615 → 8.549 | 9.121 → 9.100 | 4.129 → 4.085 | 18.823 → 18.427 | 4.163 → 4.091 |

Retry authorization/preparation is a separate phase beyond initial preparation: full CPU 7.199 → 5.968s and wall 17.158 → 16.803s; minimal CPU 6.032 → 4.879s and wall 14.430 → 14.125s.

Phase gains are uneven. Preparation, payload preparation and export are mostly
unchanged, with some small phases slower. The final snapshot-first retry
selected-render CPU is nearly unchanged (full 30.347 → 30.265 seconds; minimal
32.064 → 32.046), while whole-case CPU improves by 12.5% and 11.7%. These hook
timings locate boundaries but do not isolate a causal explanation for each small
difference. They do not justify further production tuning after the qualified
complete-case improvements.

## Baseline hotspot ranking before production edits

All seven required full-capture workload profiles were captured **before any production source change**. Each is one diagnostic run: profiler/counter overhead is substantial and these times are not estimates of normal production performance or statistically reliable overhead ratios. Explicit synchronous phase thread-CPU clocks provide the following exclusive attribution; nested phase CPU is subtracted. cProfile function durations use an interpreter wall clock, can overlap under concurrency, and are advisory rather than CPU totals.

| Full case | Completed physical | Diagnostic process CPU s | Record revalidation CPU s | Actual rendering CPU s | Source-guard CPU s |
|---|---:|---:|---:|---:|---:|
| independent-3 | 600 | 42.649 | 18.263 | 2.868 | 0.270 |
| grouped-6 | 200 | 23.930 | 11.747 | 1.040 | 0.100 |
| independent-20 | 4000 | 480.164 | 283.082 | 19.282 | 1.742 |
| grouped-20 | 800 | 118.899 | 69.170 | 4.152 | 0.360 |
| grouped-200-prefix | 64 | 37.327 | 29.033 | 0.440 | 0.032 |
| pairwise | 1200 | 91.980 | 34.258 | 8.505 | 1.916 |
| subset-retry | 1600 | 240.479 | 135.976 | 12.290 | 0.948 |

Record reconstruction/revalidation is the largest measured CPU contributor in every full workload. Actual rendering accounts for roughly 4–7% of diagnostic CPU in the 3-/6-trait cases and about 1% in the 200-trait prefix. This does not justify extending the large-rubric rendering observation to smaller rubrics, nor skipping required source checks.

| Full case | Core validation entries | Record revalidations | Compatibility checks | `plan_run` calls | Selected renders | Payload preparations | Mock validations |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| independent-3 | 234,085 | 78,683 | 3,803 | 5,807 | 1,800 | 1,200 | 2,401 |
| grouped-6 | 135,100 | 38,884 | 1,403 | 2,207 | 600 | 400 | 801 |
| independent-20 | 1,825,540 | 510,517 | 24,203 | 36,407 | 12,000 | 8,000 | 16,001 |
| grouped-20 | 515,044 | 139,301 | 5,003 | 7,607 | 2,400 | 1,600 | 3,201 |
| grouped-200-prefix | 82,682 | 12,697 | 415 | 626 | 205 | 136 | 273 |
| pairwise | 665,253 | 190,100 | 8,403 | 13,209 | 3,600 | 2,400 | 4,801 |
| subset-retry | 1,094,388 | 288,549 | 9,806 | 18,810 | 4,800 | 4,000 | 8,002 |

For the full 4,000-request independent/20-trait workload, the complete measured synchronous phase ranking is:

| Exclusive synchronous phase | Thread CPU s | Calls |
|---|---:|---:|
| cpu_record_revalidation | 283.082 | 510,517 |
| result_processing | 41.063 | 4,000 |
| cpu_compatibility | 21.673 | 24,203 |
| cpu_actual_request_rendering | 19.282 | 12,020 |
| cpu_configuration_digest | 15.864 | 24,205 |
| selected_request_rendering | 14.883 | 12,000 |
| execution | 7.542 | 1 |
| provider_payload_preparation | 6.341 | 8,000 |
| export | 3.998 | 1 |
| cpu_generation_compatibility | 2.170 | 24,203 |
| cpu_source_guard | 1.742 | 48,403 |
| cpu_planning | 1.594 | 36,407 |
| cpu_mock_validation | 1.242 | 16,001 |
| preparation | 0.159 | 1 |
| session_reopening | 0.029 | 1 |

That run serialized **1,718,820,056 JSON bytes**, with **1,050,561,925 canonical bytes as a subset**; these are not additive. It made **28,000 exact-wire byte-digest checks**, **96,036 artifact-verification calls**, **853,000 SQLite execute calls**, and **32,024 observed transactions**. SQLite execute wall time was 53.804 seconds; implicit ledger context-manager commit time is not separately attributed. Observed registry WAL high-water was **2,724,234,672 bytes**. End-of-run WAL sizes cannot reveal this transient growth.

These measurements support a narrow focus on recursive model extraction, repeated compatibility/configuration work, and source cursor lifetime. Arithmetic planning and source checks are relatively small. Prompt/compiler caching and raw/source-record caching are deferred because their smaller measured benefit does not justify added invalidation/privacy complexity in this change. All 14 baseline profiles and all 42 remaining baseline tracing/coverage/combined diagnostics completed successfully against the immutable baseline archive. All 56 diagnostic configurations retain exact export SHA-256, byte-length and row-count equality with their ordinary counterparts. All 70 final-source ordinary samples and all 56 final-source diagnostics are complete. The retained numeric evidence contains both revisions without superseded-source rows.

## Optimized hotspot and counter evidence

All seven full-capture profiles below use the frozen instrumentation and final
snapshot-first source `607249d`/`5aafde3e…`. They are single diagnostic runs, not
additional ordinary speedup estimates. CPU columns are exclusive synchronous
thread CPU seconds.

| Full case | Diagnostic process CPU | Compatibility/token checks | Record revalidation | Actual rendering | Source guards |
|---|---:|---:|---:|---:|---:|
| independent-3 | 34.228 | 4.950 | 8.071 | 2.858 | 0.387 |
| grouped-6 | 19.673 | 2.275 | 6.653 | 1.031 | 0.139 |
| independent-20 | 337.554 | 104.187 | 86.771 | 19.179 | 2.531 |
| grouped-20 | 90.555 | 20.637 | 29.059 | 4.194 | 0.527 |
| grouped-200-prefix | 22.648 | 11.489 | 7.298 | 0.440 | 0.045 |
| pairwise | 77.735 | 8.612 | 17.128 | 8.531 | 2.288 |
| subset-retry | 206.204 | 61.869 | 58.157 | 12.326 | 1.615 |

| Full case | Core validation entries | Record revalidations | Compatibility checks | `plan_run` calls | Selected renders | Payload preparations | Mock validations |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| independent-3 | 184,476 | 55,879 | 7,403 | 6 | 1,800 | 1,200 | 2,401 |
| grouped-6 | 118,491 | 30,480 | 2,603 | 6 | 600 | 400 | 801 |
| independent-20 | 1,204,731 | 365,313 | 48,203 | 6 | 12,000 | 8,000 | 16,001 |
| grouped-20 | 417,435 | 109,297 | 9,803 | 6 | 2,400 | 1,600 | 3,201 |
| grouped-200-prefix | 34,452 | 10,221 | 825 | 6 | 205 | 136 | 273 |
| pairwise | 572,844 | 139,696 | 15,603 | 8 | 3,600 | 2,400 | 4,801 |
| subset-retry | 834,370 | 229,741 | 29,806 | 8 | 4,800 | 4,000 | 8,002 |

For the largest complete full-capture case, native Pydantic validation entries
fall by 34.0%, `plan_run` reconstruction calls by over 99.9%, and record
revalidation CPU from 283.082 to 86.771 seconds. Rendering, payload preparation,
mock validation, artifact verification and exact payload-hash counts remain
unchanged. Source guard calls increase from 48,403 to 72,403 because post-read/
render mutation checks remain active. These reductions reuse verified metadata
while retaining provider and source verification.

Its complete measured synchronous CPU-phase ranking after optimization is:

| Exclusive synchronous phase | Thread CPU s | Calls |
|---|---:|---:|
| cpu_compatibility | 104.186855 | 48,203 |
| cpu_record_revalidation | 86.771035 | 365,313 |
| result_processing | 38.004764 | 4,000 |
| cpu_actual_request_rendering | 19.179426 | 12,020 |
| selected_request_rendering | 8.797005 | 12,000 |
| execution | 7.362360 | 1 |
| provider_payload_preparation | 6.194135 | 8,000 |
| export | 4.015444 | 1 |
| cpu_source_guard | 2.530594 | 72,403 |
| cpu_mock_validation | 1.284153 | 16,001 |
| preparation | 0.158941 | 1 |
| session_reopening | 0.029840 | 1 |
| cpu_configuration_digest | 0.002580 | 4 |
| cpu_planning | 0.000300 | 6 |
| cpu_generation_compatibility | 0.000176 | 2 |

These named phases account for 278.518 of 337.554 diagnostic process CPU
seconds. The remainder includes work outside selected synchronous phase roots
and instrumentation; it is not assigned CPU by subtracting async latency.
Compatibility/token inspection is now the largest measured phase for the
20-trait independent run, the 200-trait prefix and subset retries. Record
validation remains largest in the other four full cases.

The corresponding default-timer cProfile rows locate work inside those
boundaries. Values below are function wall seconds, **not CPU times**; cumulative
values overlap and must not be summed.

| Function, full independent-20 | Calls, baseline → optimized | Self wall s, baseline → optimized | Cumulative wall s, baseline → optimized |
|---|---:|---:|---:|
| `_record_data` | 190,569,429 → 50,954,028 | 89.749 → 25.265 | 202.242 → 55.500 |
| `_revalidate_record` | 510,517 → 365,313 | 2.963 → 1.229 | 281.055 → 85.202 |
| `canonical_bytes` | 358,550 → 213,344 | 0.706 → 0.309 | 171.600 → 37.295 |
| canonical `_snapshot` walk | 28,981,305 → 4,393,089 | 20.762 → 3.869 | 160.624 → 32.383 |
| Pydantic `model_copy` | 8,002 → 8,002 | 0.015 → 0.015 | 0.077 → 0.072 |
| `PreparedRun.render` | 12,000 → 12,000 | 0.082 → 0.063 | 157.913 → 112.763 |
| `PreparedRun.render_attempt` | 4,000 → 4,000 | 0.016 → 0.015 | 52.517 → 37.653 |
| `ExecutionSession.verify` | 8,000 → 8,000 | 0.054 → 0.053 | 122.128 → 41.528 |
| `ExecutionSession.next_work` | 4,001 → 4,001 | 0.135 → 0.132 | 255.925 → 161.933 |
| `ExecutionSession.boundary` | 4,000 → 4,000 | 0.077 → 0.073 | 136.173 → 88.370 |
| `MockAdapter.validate` | 16,001 → 16,001 | 0.052 → 0.054 | 31.236 → 31.459 |
| `MockAdapter.prepare` | 8,000 → 8,000 | 0.137 → 0.132 | 41.369 → 41.207 |
| `MockAdapter._payload` | 8,000 → 8,000 | 0.078 → 0.075 | 8.134 → 7.536 |
| `_render_request` | 12,020 → 12,020 | 0.097 → 0.102 | 20.051 → 19.943 |
| `_rubric_schema` | 12,020 → 12,020 | 0.025 → 0.024 | 0.029 → 0.028 |
| `_compile_prompt_template` | 12,040 → 12,040 | 0.021 → 0.018 | 12.690 → 12.583 |

The structural `_token` walk has 87,684,895 recursive entries, 60.312 self wall
seconds and 100.134 cumulative wall seconds in this diagnostic. Mutable
configuration inspection remains a useful later profiling target. Pydantic
`model_copy` is small and unchanged. Detached snapshot deep-copy work has
382 recursive entries and 0.005660 cumulative wall seconds; removing
those copies would not justify the mutation risk.

The linear trait lookup remains in `_render_request`; its entire self wall time,
including lookup and dispatch, is only 0.102 seconds here. Python 3.12
inlines list comprehensions, so this lookup has no separate function row. Schema
reconstruction is small. Template compilation contributes more within actual
rendering, but actual rendering remains a smaller measured CPU phase than
verification. These bounds do not replace the separately measured rendering
contribution in the smaller 3-/6-trait cases.

The 200-trait full prefix has 245 actual renders in each profile, including
preparation, with `_render_request` self wall 0.0069 → 0.0074 seconds, schema
cumulative wall 0.0012 → 0.0012, and template compilation cumulative
wall 0.304 → 0.303. Both profiles have 69 prepared requests,
67 accepted requests and 64 completed physical results. No lookup/compiler
cache or reduced render count explains the larger-rubric CPU improvement.

`dependency_for_request()` is exercised 4,000 times in the full subset-retry
profile. Its self wall is 0.027 → 0.022 seconds, while cumulative wall
is 4.630 → 20.109; the lookup includes additional compatibility guards.
Its CPU is not independently isolated by the existing phase hooks. This single
advisory wall increase is retained explicitly: ordinary retry selected-render
CPU stays nearly flat and total retry CPU improves by 12.5%.

Total JSON serialization falls from 1,718,820,056 to **609,372,011 bytes**;
canonical bytes, a subset, fall from 1,050,561,925 to **521,068,246**. Exact mock
payload-hash operations remain **28,000**, covering 174,496,000 bytes; prepared
payload bytes remain **49,856,000**. Artifact verification remains **96,036**
calls. SQLite transactions remain **32,024**; execute calls change only from
853,000 to **853,002**, including bounded page reads. Observed execute
wall time is 53.804 → **44.889 seconds**, not an estimate of SQL CPU.

Observed registry WAL high-water for this paired full case falls from
**2,724,234,672 to 4,449,632 bytes**. This is an observed after-result
high-water, not continuous peak sampling. The separate ordinary A/B comparison
and checkpoint regression justify the cursor-lifetime fix without changing
SQLite durability or acceptance semantics.

## Allocations and bounded concurrency

The following is one tracemalloc-only diagnostic per case/capture/revision.
Each cell is baseline → optimized in MiB. Live is sampled after the timed
preparation/execution/export window; retained is sampled after garbage
collection. Tracing starts after fixture construction/imports, whereas native
peak RSS covers the process lifetime. These single samples report observed
allocation costs and do not establish statistically reliable memory improvements.

| Case | Capture | Python peak MiB | Python live MiB | Python retained MiB | Tracing-process native RSS MiB |
|---|---|---:|---:|---:|---:|
| independent-3 | full | 4.886 → 5.087 | 2.934 → 3.134 | 2.245 → 2.231 | 57.867 → 59.879 |
| independent-3 | minimal | 4.501 → 4.716 | 2.652 → 2.867 | 1.964 → 1.964 | 57.953 → 58.328 |
| grouped-6 | full | 4.250 → 4.511 | 2.172 → 2.433 | 1.520 → 1.508 | 58.500 → 59.012 |
| grouped-6 | minimal | 3.196 → 3.471 | 1.160 → 1.435 | 0.509 → 0.509 | 57.535 → 57.656 |
| independent-20 | full | 9.088 → 8.873 | 7.046 → 6.830 | 6.406 → 6.392 | 82.152 → 77.969 |
| independent-20 | minimal | 6.309 → 6.073 | 4.261 → 4.024 | 3.621 → 3.622 | 79.680 → 71.961 |
| grouped-20 | full | 5.068 → 4.958 | 3.026 → 2.915 | 2.439 → 2.423 | 62.945 → 63.863 |
| grouped-20 | minimal | 4.768 → 4.338 | 2.720 → 2.290 | 2.067 → 2.065 | 64.418 → 62.793 |
| grouped-200-prefix | full | 3.223 → 4.702 | 1.564 → 3.233 | 0.339 → 0.331 | 57.734 → 66.078 |
| grouped-200-prefix | minimal | 2.968 → 4.645 | 1.525 → 3.202 | 0.301 → 0.303 | 57.793 → 66.215 |
| pairwise | full | 5.392 → 5.761 | 3.317 → 3.685 | 2.821 → 2.809 | 61.242 → 61.285 |
| pairwise | minimal | 4.855 → 5.170 | 2.822 → 3.136 | 2.320 → 2.324 | 61.844 → 64.445 |
| subset-retry | full | 6.377 → 5.917 | 4.297 → 3.837 | 3.408 → 3.394 | 69.645 → 69.262 |
| subset-retry | minimal | 5.195 → 5.233 | 3.131 → 3.169 | 2.665 → 2.660 | 71.234 → 66.797 |

Two tracemalloc-only retry configurations are slower in these single diagnostic
runs: full wall 352.426 → 373.947 seconds and CPU 335.542 → 357.856; minimal wall
339.267 → 360.392 and CPU 328.202 → 350.540. These are observed increases of
roughly 6–7%, rather than statistically reliable estimates. Ordinary five-sample
retry CPU improves by 12.5% full and 11.7% minimal. This difference reinforces
the distinction between complete instrumentation configurations and ordinary
execution; no speedup claim is based on the tracer timings.

The 200-trait prefix has a higher peak/live allocation footprint, by roughly
1.48–1.68 MiB, while retained-after-GC allocations remain near 0.3 MiB. This
records the bounded static-metadata tradeoff; lifetime tests verify reference
release as qualified below, and privacy tests verify absence from minimal
persistent artifacts. It does not isolate the allocator/layout causes of native
RSS differences. The new cached
configuration snapshots in both capture modes stay in memory and contain no
source-record, rendered-prompt, payload or response cache. Existing memory
scaling/threshold, native RSS and 180-second
resource subprocess assertions pass unchanged.

All 84 observed profile/tracemalloc/combined configurations across both revisions
have four execution worker coroutines plus producer/consumer: six execution
tasks per wave, twelve across the two retry waves. Live asyncio task high-water
is seven against the unchanged bound of eleven. Request/result queue capacities
remain eight/four; observed maxima are four/three, including the completed
combined passes. Tracing's 200-trait prefix observes one/one. Coverage-only and
ordinary task/queue values remain unobserved/null. Across all diagnostic modes,
native process peak RSS is at most 110.719 MiB baseline and 106.637 MiB optimized;
these process-lifetime observations include native allocations. Maximum traced
Python peak is 10,719,224 → 10,544,952 bytes and maximum retained-after-GC is
7,904,233 → 7,940,736 bytes. These maxima come from single diagnostic runs; the
unchanged resource assertions and scaling checks provide the required bounds.

## Instrumentation effects

All 112 single diagnostic configurations completed: four modes × seven cases ×
two capture modes × two revisions. Each cell below is baseline → optimized,
expressed as the observed instrumented time divided by that case's five-sample
ordinary median. These are descriptive n=1 whole-configuration quotients, not
statistically reliable overhead estimates. Profile includes broad counters;
profile/tracemalloc/combined include task/queue tracking, while coverage-only
does not. Exact elapsed/CPU seconds, allocations, phases and counters for every
configuration are retained in the numeric evidence.

Wall-time quotient:

| Case | Capture | Profile × ordinary | Tracemalloc × ordinary | Coverage × ordinary | Combined × ordinary |
|---|---|---:|---:|---:|---:|
| independent-3 | full | 1.80 → 1.75 | 2.26 → 2.45 | 2.15 → 1.99 | 8.40 → 8.26 |
| independent-3 | minimal | 1.89 → 1.84 | 2.35 → 2.61 | 2.18 → 2.12 | 8.95 → 9.02 |
| grouped-6 | full | 2.05 → 1.99 | 2.59 → 2.72 | 2.49 → 2.37 | 10.53 → 10.17 |
| grouped-6 | minimal | 2.08 → 2.08 | 2.65 → 2.84 | 2.53 → 2.52 | 10.76 → 11.09 |
| independent-20 | full | 2.05 → 1.89 | 2.82 → 3.18 | 2.63 → 2.27 | 11.60 → 11.44 |
| independent-20 | minimal | 2.04 → 1.96 | 2.84 → 3.33 | 2.53 → 2.37 | 11.90 → 12.23 |
| grouped-20 | full | 2.12 → 2.05 | 2.81 → 3.16 | 2.69 → 2.46 | 12.36 → 11.86 |
| grouped-20 | minimal | 2.21 → 2.11 | 2.97 → 3.30 | 2.81 → 2.62 | 12.77 → 12.91 |
| grouped-200-prefix | full | 2.09 → 2.33 | 3.53 → 5.08 | 2.84 → 2.96 | 15.98 → 19.18 |
| grouped-200-prefix | minimal | 2.20 → 2.24 | 3.90 → 5.28 | 3.09 → 3.03 | 17.03 → 19.94 |
| pairwise | full | 1.80 → 1.82 | 2.13 → 2.40 | 2.06 → 2.05 | 8.10 → 8.45 |
| pairwise | minimal | 1.94 → 1.90 | 2.33 → 2.53 | 2.26 → 2.18 | 8.33 → 8.71 |
| subset-retry | full | 2.12 → 2.07 | 2.90 → 3.48 | 2.65 → 2.56 | 12.66 → 13.22 |
| subset-retry | minimal | 2.22 → 2.15 | 3.06 → 3.67 | 2.84 → 2.67 | 12.76 → 14.29 |

CPU-time quotient:

| Case | Capture | Profile × ordinary | Tracemalloc × ordinary | Coverage × ordinary | Combined × ordinary |
|---|---|---:|---:|---:|---:|
| independent-3 | full | 2.11 → 2.05 | 2.72 → 3.00 | 2.56 → 2.37 | 10.41 → 10.53 |
| independent-3 | minimal | 2.12 → 2.07 | 2.70 → 3.02 | 2.49 → 2.41 | 10.80 → 10.77 |
| grouped-6 | full | 2.31 → 2.25 | 2.97 → 3.16 | 2.86 → 2.73 | 12.44 → 12.41 |
| grouped-6 | minimal | 2.29 → 2.28 | 2.95 → 3.16 | 2.81 → 2.78 | 12.39 → 12.72 |
| independent-20 | full | 2.32 → 2.13 | 3.28 → 3.75 | 3.03 → 2.61 | 14.10 → 14.04 |
| independent-20 | minimal | 2.25 → 2.15 | 3.19 → 3.75 | 2.83 → 2.62 | 13.80 → 14.15 |
| grouped-20 | full | 2.34 → 2.28 | 3.16 → 3.64 | 3.01 → 2.79 | 14.43 → 14.21 |
| grouped-20 | minimal | 2.39 → 2.29 | 3.24 → 3.65 | 3.06 → 2.86 | 14.35 → 14.60 |
| grouped-200-prefix | full | 2.41 → 2.33 | 4.19 → 5.38 | 3.36 → 3.12 | 19.17 → 20.64 |
| grouped-200-prefix | minimal | 2.42 → 2.30 | 4.31 → 5.49 | 3.41 → 3.13 | 18.97 → 20.84 |
| pairwise | full | 2.08 → 2.10 | 2.51 → 2.87 | 2.42 → 2.41 | 9.82 → 10.16 |
| pairwise | minimal | 2.16 → 2.11 | 2.64 → 2.86 | 2.56 → 2.44 | 9.62 → 10.10 |
| subset-retry | full | 2.32 → 2.27 | 3.23 → 3.94 | 2.94 → 2.85 | 14.58 → 15.45 |
| subset-retry | minimal | 2.37 → 2.30 | 3.31 → 4.00 | 3.07 → 2.88 | 14.14 → 15.90 |

Combined instrumentation is especially expensive: the final 200-trait minimal
prefix is about 20.84 times its ordinary CPU median in this single run. Large
relative overhead does not describe normal production performance. Both long
4,000-request combined samples completed within the benchmark's unchanged
3,600-second limit in both revisions; established resource-test timeouts remain
180 seconds. Read-only numeric progress checks during those long diagnostic
samples are retained as conditions, without source/payload content. No additional
repetitions were selected merely because instrumentation interaction was large.

## Paging decision: interleaved controlled comparison

After the cache optimizations passed qualification, a separate full-capture
200×20 independent comparison isolated the bounded source-ID read from the cache
changes. The cache-only and paging snapshots differed only in source-ID paging.
Both reused the literal baseline source files/paths and pristine database backups.
Each received one warm-up and three measured fresh processes, ordered
A-warm/A0, B-warm/B0, A1/B1, A2/B2. No other checks ran during those samples.

| Variant | Wall median [range] s | CPU median [range] s | CPU ms/physical | Native RSS median bytes |
|---|---:|---:|---:|---:|
| Cache only | 208.664 [208.380, 208.882] | 162.694 [161.680, 162.852] | 40.673 | 82,464,768 |
| Cache + bounded paging | 199.062 [198.597, 199.449] | 158.303 [158.174, 159.000] | 39.576 | 78,041,088 |

Paging reduced median wall time by **4.60%** and CPU by **2.70%**, with separated
ranges in this three-sample comparison. These results support retaining the
narrow cursor-lifetime fix; this is a component comparison, not the final
five-sample baseline-versus-optimized assessment. The paired profile above measures observed registry WAL high-water;
this isolated A/B experiment measures wall/CPU rather than an isolated WAL peak.
End-of-run closed WAL sizes cannot establish transient growth.

Cache-only source fingerprint:
`694dd1fe63aa2d2300072eb6e53b2c8b55bc9630da8d9fbdd93323f149eca514`.
Paging source fingerprint:
`ea9d27c80db6ab109f44e87bd281feb2fef6c6a15e9ff5219c94838092e35c6e`.
Raw evidence is under `/tmp/judgmentllm-performance/ab-cache-only` and
`/tmp/judgmentllm-performance/ab-paging`.
After this component comparison, review added a two-line record-kind guard to
restore the error contract for fabricated incompatible dependencies. Ten early
candidate timing rows from the prior source were retained separately as
provisional evidence and excluded. Later ABA review superseded all 70 ordinary
and 14 profile rows of `67094f…`. Those files remain separately retained and
excluded. The entire final matrix on snapshot-first commit `607249d` is complete; the
paging algorithm is unchanged by these integrity corrections.

## Integrity, parity, privacy and recovery

The snapshot-first candidate passed the complete locked offline suite:
**1,370 tests, 96.16% coverage**, in 302.45 seconds, with `ResourceWarning` and
`PytestUnraisableExceptionWarning` treated as errors. No tests were skipped and
no unexpected warnings were reported. All existing resource, recovery,
cross-process exclusion, grouped atomicity, cancellation and privacy tests ran.
Workloads, worker/task/queue bounds, memory thresholds and 180-second resource
subprocess timeouts are unchanged.

The original [73 added cases](../tests/test_execution_performance_integrity.py) cover
equivalent copied models; accepted raw numeric
representations; changed configuration, capabilities, template and exemplars;
scalar type distinctions and signed zero; unknown fields and cycles; source
replacement and same-length/restored-mtime mutation; exact retry membership;
adapter mutations; mutations during source lookup/rendering; cache lifetime;
wrong-kind dependency rejection; and checkpoint progress while request iteration
is suspended. The original
version passed 50 of the first 52 cases. Two cases exposed existing unsanitized
cyclic-mutation errors and a cursor pinning WAL frames (151 written versus 141
checkpointed). Both pass on the current candidate.

Three later deterministic ABA cases exposed a defect in the superseded
`c308a87` optimization: public values could change from A to B during validation
and return to A before raw snapshot capture, caching A with B's validated plan.
All three failed that source through missing expected rejections; the proof
script excluded setup/teardown failures. The correction captures detached raw
specification, manifest and capability snapshots first, checks their tokens, and
derives validation, planning and fingerprints from those same snapshots. All
three new regressions pass in the 1,370-test suite. The superseded source had
previously passed 1,367 tests at 96.15% coverage in 303.56 seconds; that earlier
qualification and its 70 ordinary/14 profile samples remain provisional audit
evidence, excluded from final-source performance acceptance.

Equivalent configuration does not always mean identical presentation. Changing
an accepted numeric code from `0.0` to `0`, or reordering exemplar metadata,
can preserve configuration identity while changing prompt text. Tokens preserve
these raw distinctions and mapping order. Accepted changes refresh detached raw
snapshots; an incompatible change fails. A presentation change after attempt
preparation still fails the existing exact payload comparison. Unsupported
values use the legacy uncached path, including its validation and serialization
behavior, rather than being silently normalized by the cache.

The development-only parity fixture independently prepares baseline/candidate
runs, restores the same initial registry and service identity, and compares ten
full/minimal independent, grouped, retry, pairwise and advanced-template cases.
It compares exact prompts, wire bytes and responses in memory, all normalized
fields, memberships/IDs/history, independent acceptance records, and all three
immutable export formats. Completed recovery must add no acceptance or
submission. Only declared operational timestamps are excluded. Its retained
report contains hashes/counts and runtime/source fingerprints, not copied raw
minimal-capture text. The cache-only candidate passed all ten comparisons;
final candidate including paging also passed all ten comparisons. Its retained
comparison digest is
`03b46c8aadac0eacad9269974834ae2c6e9255c9ef553c0463bb2822bd708f46`;
the [retained parity report](../benchmarks/evidence/execution-parity.json)
records baseline/candidate source and runtime fingerprints.

The ordinary performance matrix provides additional large-workload export
evidence without another execution. For all 14 case/capture configurations,
canonical JSONL, canonical CSV and R CSV have identical byte lengths, row counts
and SHA-256 hashes across all five baseline and all five optimized repetitions:
**42 export combinations and 420 matching recorded hash observations**. This
includes the complete 4,000-request case, the 1,600-request retry history and the
bounded 200-trait prefix. Input fingerprints also match across each ten-run
comparison. Direct request/prompt/wire/acceptance comparisons use the separate
in-memory parity catalog above; compact export metadata is retained with the
numeric performance evidence.
All 112 diagnostic configurations also match their ordinary export SHA-256,
byte-length and row-count metadata, adding 336 matching recorded hash
observations: **756 matching export-hash observations** across ordinary and
diagnostic runs. The retained evidence asserts these comparisons for both
fingerprinted revisions.

All six independent strict Python-to-R/jsonlite gates passed on the current
candidate: baseline exports, persistence, generation, source preparation,
execution, and grouped retry/export. They remain offline and credential-free.
Before production edits, a targeted baseline privacy/source/capability run passed
24 cases (44 deselected), and eight additional existing full/minimal capture,
loss-window and ledger-sentinel cases passed in a mixed baseline probe. That
mixed invocation was not wholly successful: a newly drafted numeric-replacement
test initially assumed identical prompt text for accepted `0.0` versus `0`
values. Its expectation was corrected to match existing presentation semantics;
the revised focused probe passed 35 cases. These are retained tool-output
results, without a separate baseline test log. All corresponding existing tests
and all original 73 regressions passed in the corrected-source complete suite.
The unchanged full/minimal privacy sentinels cover live database/WAL/SHM,
ledgers, artifacts, receipts, exports, temporary files and diagnostics. Cached
configuration may contain rubric/exemplar/template text but stays in memory;
the private context/token objects are never written. Existing prompt/payload
capture continues through its established privacy-controlled paths. Closing
clears the prepared session's own context reference and prevents verification
from recreating it after closure. An independently retained, suspended request
iterator can still hold the detached context, including rubric/exemplar/template
text, after the session closes. Weak-reference regressions verify both immediate
release when no iterator retains it and collection after the remaining iterator
references are released. This is private in-memory retention; closure does not
promise immediate garbage collection of independently referenced objects. No
source-record, rendered-prompt, provider-payload or response cache was added.

One first full-suite invocation used the virtualenv's pytest executable without
putting its console scripts on `PATH`: 1,364 tests passed and two installed-CLI
lookup checks failed. The complete suite was rerun through `uv run --offline
--no-sync`; all 1,366 passed. This was an invocation-environment correction,
not a test or production-code exception. The final corrected source subsequently
passed all 1,367 tests, including the added wrong-kind dependency regression.
The subsequent snapshot-first correction passed all 1,370 tests, including the
three added ABA regressions.

## Changes, rejected approaches and remaining qualification

The first production change reuses one detached, private configuration/manifest/
template context per verified session, together with its structural plan and
request-context fingerprint. Every existing compatibility boundary still checks
current caller-visible values with a type-aware structural token, including
undeclared fields, presentation order and exact floating-point representation.
Changed tokens rerun configuration validation and canonical identity checks;
unknown types stay uncached. Valid inputs retain their established behavior,
including acceptance of equivalent replacement configurations. Capability
validation is deliberately stricter: the complete supplied profile is now
revalidated, including fields unused by the run. For example, a separate profile
with an injected `explanations=1` was accepted by baseline compatibility checking
for a run without explanations, but is now rejected because that field requires
a boolean or null. This malformed-profile rejection is validation hardening,
not a behavior change for valid profiles. Recursion tracking distinguishes
cycles from shared, acyclic model references. Cached rendering also checks for
changes during source reads, preserving fail-closed behavior when a callback
mutates public objects.

Selected requests reconstruct only their verified trait group through the same
version-one identity formulas. Configuration/capability changes cannot alter
frozen grouping. The public manifest digest is checked against a private sealed
reference. Reopening reconstructs a new verified context; no validation token is
persisted or trusted across processes.

The retained verification responsibilities are:

| Boundary | Work retained or reused |
|---|---|
| Open/reopen, or changed supported configuration token | Full manifest/configuration identity and Pydantic checks, generation/capability compatibility, plan reconstruction and template identity; rebuild detached metadata only after success |
| Every existing compatibility boundary | Source-generation checks, sealed manifest reference, exact current configuration/manifest/template/capability tokens; opaque values always take the legacy validation path |
| Each selected request and render | Original request/membership identity, exact indexed source-record reads and checks, bounded selected-group reconstruction, actual prompt rendering, post-read and post-render mutation guards |
| Each provider submission | Existing fresh rendering/payload preparation and actual byte/hash comparisons, artifact checks and immediately preceding durable `submitting` transition |
| Observation processing, recovery and export | Existing independent acceptance evidence, receipt/observation identity, atomic grouped commits, result validation and immutable serializers |

The optimization changes no `ExecutionSession.verify()`, `next_work()`,
`boundary()`, adapter validation/preparation, retry or acceptance-ledger logic.
Those callers benefit from less repeated work inside the same preparation
boundaries; their individual safety checks remain active.

The separately evaluated paging change fully fetches at most 128 immutable input
IDs using the existing `(run_id, source_id, ordinal)` index before yielding work.
This releases the read cursor between execution transactions. It changes no
journaling, synchronization, ownership, transaction or acceptance boundary.
It was retained after the interleaved ordinary comparison above showed separated
wall/CPU ranges and repeatable improvements over the context-only variant.

Rejected or deferred changes:

- Global caches, cached student records/responses, and timestamp-only source
  identity checks would undermine the required lifetime/privacy/integrity rules.
- Removing the second preparation render would need additional checks for
  adapters mutating their arguments or source files. Adversarial regressions
  demonstrate its existing safety role; rendering is a smaller measured cost.
- Compiled-template/schema caches add mutable-output and rendering-limit
  invalidation work. The measured profile favors configuration verification.
- Repository-wide validation changes were not justified by `_checked()`'s small
  measured contribution and would broaden persistence-boundary review.
- CI sequencing is unchanged. The three-OS/two-Python matrix has one full pytest
  step per job; Windows prechecks repeat selected export/privacy/cancellation
  checks but exclude the resource fixtures. The execution resource tests invoke
  seven subprocess cases: independent 200x20 twice (separate response- and
  trait-scaling baselines), 2,000x20 once, 200x200 once, and each of those three
  grouped cases once. Preparation adds four distinct subprocess cases. Sharing
  the repeated baseline would need a separate test-lifetime/resource review;
  removing it here would change established independent scaling checks.

Current local gates also passed Ruff lint/format (128 Python files), strict
Pyright, lock validation (93 packages), isolated offline wheel/sdist builds,
strict Twine metadata, and clean current-version wheel/source installation
smokes. Archive inspection confirms the new profiling tools/raw results are
absent from both distributions. An initial broad `dist/*` smoke selected an
older pre-existing 0.1.0a1 artifact; current 0.1.0a2 artifacts were rebuilt in a
fresh temporary directory and both passed. No version/schema change was made.

The production implementation is commit `607249d`, after profiling/evidence
commit `5e60aa7`. Its source fingerprint is
`5aafde3e55cbde9a73dd409cc21250ccfd366abf851b9e834bc5ff8c8177328c`.
The complete corrected-source timing matrix uses an immutable archive and the
literal baseline input paths/pristine database snapshots. Regression/verification
commit `f312a31` added tests and this report without changing that measured
production tree. The independent-review corrections likewise change only tests
and this report; production, benchmark tools and retained measurements remain
unchanged, so the expensive performance matrix is not repeated.

Both revisions completed all 70 ordinary samples and 56 diagnostic
configurations. The final source is `607249d`; superseded candidate data remains
separately retained and excluded. No Subphase 5, native Windows/macOS CI, or
full-scale qualification is claimed.

## Independent-review corrections and local qualification (2026-11-04)

The fourth commit contains only test and report corrections. The integrity suite
now has **79 cases**, including full/minimal capture with and without a retained
suspended iterator; collection after remaining iterator references are released;
a separately supplied malformed capability clone and valid-clone acceptance;
and 260-record rubric/pairwise ordering and resume checks across the 128- and
256-record boundaries, including offsets within rubric trait groups. These are
correctness regressions, not additional resource benchmarks. No production code,
iterator lifecycle, resource thresholds or benchmark evidence was changed.

The focused integrity/preparation/rendering suite passed **113 tests** in 12.11
seconds. The complete offline suite passed **1,376 tests, 96.16% coverage**, in
333.46 seconds, with strict R requirements and `ResourceWarning` and
`PytestUnraisableExceptionWarning` treated as errors. There were no skipped tests
or unexpected warnings. The complete suite retained all existing resource,
privacy, recovery, ownership and export assertions. The invocation used the
locked environment through `uv run --offline --no-sync`,
`COVERAGE_RCFILE=pyproject.toml`, and temporary coverage/test output under `/tmp`.

Ruff lint and formatting (128 Python files), strict Pyright (zero errors or
warnings), and offline lock validation (93 packages) passed. All six strict
Python-to-R/jsonlite gates were also rerun directly through their existing
persist/export scripts and R assertions: baseline exports, persistence,
generation, source preparation, execution, and grouped retry/export. R 4.6.2 and
jsonlite 2.0.0 were required and available. Offline isolated wheel/sdist builds,
strict Twine metadata, clean core installation and offline CLI/execution smokes,
and archive exclusion checks passed. Local checks use Linux/Python 3.12.3;
final-head acceptance on the six unchanged CI configurations is recorded in the
PR. The source and benchmark fingerprints above remain unchanged, so no new
performance matrix was needed or run.

## Remaining risks and later qualification

The mutation guard must still inspect current configuration contents at each
required boundary. Its cost scales with static rubric/exemplar structure; this
change does not promise constant-time checking of mutable Python objects.
Generic adapter validation, exact payload parsing/hash checks, result validation,
durable SQLite commits and export validation remain intentionally active.
Network/provider latency and hosted-runner behavior are outside these synthetic
local measurements. The 200-trait prefix provides no full-run throughput or
100,000-response scale estimate.

Some early ordinary samples predate captured harness-byte fingerprints; their
explicit retrospective compatibility records and quarantined timing overlaps
are retained. Final source/fixture/tool fingerprints and repeated ordinary
measurements are the basis of the comparison. Single instrumented samples are
diagnostic; they cannot establish statistically reliable allocation or
instrumentation-overhead improvements.

For Subphase 5, expose the existing shared services through the approved coarse
run/group CLI operations, keep private session/cache details out of the public
contract, reconstruct verified context after each process restart, and preserve
payload/submission/recovery boundaries and actionable content-free errors.
Keep all six existing R/jsonlite gates and add the assigned installed-CLI
independent-process checks. R should invoke whole runs/groups rather than one
Python process per judgment.

For later large-scale qualification, separately test the assigned 5,000x20 and
100,000-response workloads, realistic rubric/exemplar sizes, longer observation
histories, WAL/disk behavior and provider latency. Reprofile remaining validation
and token-comparison costs before adding another cache or weakening a boundary.
Retain original IDs, complete probability/coding data and all-attempt exports.
PR acceptance still requires all six unchanged CI configurations to pass on the
exact final PR head. At completion of the original measurement task, nothing had
been pushed, published, merged or released. Publication and final-head CI results
for the subsequent independent-review corrections are recorded in the PR; merge,
release and Subphase 5 remain outside this task.
