# Execution performance verification

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
variability, memory or attribution anomaly warrants the additional evidence. Ordinary
full-capture timing is the primary CPU/wall assessment; minimal capture is also
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
cases use three nonuniform numeric categories and probability observations.
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
thread clocks provide CPU attribution. Profiler caller-edge
dictionaries are disabled (`subcalls=False`); function self/cumulative times and
primitive/total counts remain available, with built-in calls enabled. Counters
report core Pydantic validation entry points, planning/rendering, source guards,
file verification, total JSON serialization bytes (`json.dumps` plus Pydantic's
`model_dump_json`), canonical serialization bytes as a subset, byte digests, actual prepared
payload bytes, and SQLite operations/transactions where observable. Profiling
checks its scope with known main/I/O/provider thread markers and exactly four
native Pydantic entries before collection. Payload-specific byte-digest counts
recognize the fixed mock protocol marker without retaining bytes; streamed
artifact verification is counted separately rather than inferred from that
content counter. Profiling
does not add canonical bytes to the JSON total a second time. CSV/JSONL published
byte lengths are also recorded directly from immutable export reports. Profiling
stores numeric counts/times only, with no copied assessment content. Allocation
passes report peak, live and retained-after-GC Python bytes, portable native peak
RSS, actual task creation/high-water counts, and both queue high-water marks.
These whole-run allocation figures supplement rather than replace the unchanged
post-initialization resource fixtures and their limits.

Example commands (all raw output and runtime stores stay outside distributions):

```bash
git archive 2cd12c5914698ccc0498b3004cb4de03cee397bf | tar -x -C /tmp/judgmentllm-performance-baseline
.venv/bin/python benchmarks/execution_benchmark.py --source-root /tmp/judgmentllm-performance-baseline --output /tmp/judgmentllm-performance/baseline --modes plain
.venv/bin/python benchmarks/execution_benchmark.py --source-root /tmp/judgmentllm-performance-baseline --output /tmp/judgmentllm-performance/baseline --modes profile tracemalloc coverage combined --resume
.venv/bin/python benchmarks/execution_benchmark.py --source-root /tmp/judgmentllm-performance-optimized --work-root /tmp/judgmentllm-performance/baseline/work --output /tmp/judgmentllm-performance/optimized
```

The harness writes per-sample JSON and median/min/max/median-absolute-deviation
summaries. Profiling tools and raw results are excluded from wheel/sdist content.
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

Optimized paired timings and remaining diagnostic allocation/coverage results are pending. No before/after speedup is claimed yet.

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

| Full case | Core validation entries | Record revalidations | Compatibility checks | Planning calls | Selected renders | Payload preparations | Mock validations |
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

These measurements support a narrow focus on recursive model extraction, repeated compatibility/configuration work, and source cursor lifetime. Arithmetic planning and source checks are relatively small. Prompt/compiler caching and raw/source-record caching are deferred because their smaller measured benefit does not justify added invalidation/privacy complexity in this change. Minimal-capture profiles and allocation/coverage diagnostics continue against the immutable baseline archive and remain pending qualification.

## Integrity, parity, privacy and recovery

Qualification is in progress. Functional parity, mutable-object changes,
source/file identity, payload commitments, grouped atomicity, conservative
recovery, privacy sentinels and cross-process exclusion must all pass alongside
performance measurements. No performance result substitutes for these checks.

## Changes, rejected approaches and remaining qualification

Final production changes, measured rationales, rejected optimizations, complete
local gates and concrete later-qualification recommendations will be recorded
after paired measurement. No Subphase 5 or full-scale qualification is claimed.
