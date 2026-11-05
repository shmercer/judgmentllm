# Milestone 2 Subphase 6 qualification protocol and engineering evidence

Implementation starts from merge `4b4563102d1bf2dbb7ede8e40b87a7734f9437dd`
on `codex/milestone-2-subphase-6-scale-qualification`, in an independent
worktree. Remote main was fetched and matched that commit; PR #10 is merged.
The five post-merge jobs in [run 41951607071](https://github.com/shmercer/judgmentllm/actions/runs/41951607071)
passed. Their durations were Ubuntu 3.11 10m38s, Ubuntu 3.15 7m16s, macOS
3.11 6m05s, macOS 3.15 7m34s, and Windows 3.12 16m10s. The final PR #10
Windows job took 18m38s, leaving 82 seconds under the unchanged 20-minute limit.
These observations are not a statistically qualified runtime distribution.

## Acceptance status and evidence

This tracked document defines the acceptance protocol and records engineering
evidence for the exact revisions named below. Final results belong in the
external ledger at:

```text
/mnt/VM_Fast/judgmentllm-subphase6/qualification/FULL_HEAD_SHA/ledger.json
```

Use the complete final candidate SHA in that path. Its ledger is the entry point
for the candidate's measured scale results, compatibility report, explicit source
equivalence mapping where applicable, and exact-final-commit local grid.
Completion requires retained complete minimal/full 5,000-response by 20-trait
execution, focused allocation/initialization/export evidence, genuine a2-to-a3
compatibility and the complete six-configuration local grid. The revised scope
permits carrying retained results forward only through the reviewed equivalence
rules below; their original source/tool identities are never changed. No partial,
projected, timed-out or preparation-only workload substitutes for complete
practical execution. Missing or failed applicable requirements leave acceptance
unestablished; optional work removed by the scope revision is recorded as
unexecuted rather than treated as an outstanding acceptance gate.

Raw inputs, databases, exports, profiler outputs, environments, and final
machine-readable evidence live under `/mnt/VM_Fast/judgmentllm-subphase6/`.
Only the small explicitly authorized synthetic a2 regression fixture is retained
with tests. Exact-final-commit evidence is written outside Git after freezing the
candidate, so recording results does not change the commit being qualified.
The unreleased `0.1.0a3` candidate is a development-version transition, not a
package publication or a claim that every acceptance gate has completed.

## Revised completion scope and retained complete executions

The completion scope was reduced after sufficient complete execution evidence
was available. No further full-size timing repetitions, full-run cProfile or
complete 100,000-judgment tracemalloc runs are required. Optional status-reader,
pinned-reader, rich/static-content, history and stress sweeps are not completion
requirements unless a specific unresolved acceptance gap justifies one.

Actual retained ordinary evidence from
`937352a9693fda1471a137ee06e5017347789745` consists of three measured minimal
samples plus one minimal warm-up, one full-capture pilot and one full-capture
warm-up. Each of those six completed executions produced 100,000 results and
20,000 physical completions, complete canonical/R exports and independent
verification. The minimal measured lifecycle wall samples were 3,682.639,
3,686.329 and 3,716.769 seconds. The full pilot took 4,724.718 seconds; its
75,399,168-byte native peak was below 256 MiB. The full warm-up completed in
4,762.246 seconds. Raw reports, exact native measurements, phase timings and
output hashes remain authoritative.

The later full-capture measured sample was deliberately interrupted with SIGINT
when the user revised scope. Its retained inspection recorded 14,995 results,
2,999 completed requests, 3,004 prepared requests and 3,003 acceptances, each
accepted once. Its driver exited 130 and retains the original failed/interrupted
stage journal. It is
excluded from complete-run counts and throughput summaries. The limited
full-capture observations do not establish a repeated-sample timing distribution;
do not infer a speedup or stable runner variability from them. The earlier
3,600-second timeout and later 7,200-second watchdog are operational benchmark
limits, not public runtime guarantees; neither changes the 180-second required
memory-fixture deadline or hosted 20-minute job limit. The earlier 5bb7 full
attempt timed out with 80,310 results, 16,062 completed requests and 16,065
acceptances, each accepted once; it did not complete qualification. Its retained
forensics and the separate e252 profiling incident remain historical/excluded
evidence, alongside their original source/tool hashes. Read-only SQLite
inspection's volatile SHM read marks are distinguished from unchanged recorded
database/WAL/manifest hashes.

Remaining resource evidence uses fresh minimal/full-capture 64-request prefixes
at 200 versus 5,000 responses with twenty traits and groups of five, tracing only
after source/configuration/index/session initialization. Initialization is
measured separately; actual complete export serialization/publication is measured
from retained 100,000-row stores without executing new judgments. These focused
measurements retain the 32 MiB execution, 8 MiB export and existing growth
thresholds. The complete ordinary runs retain the overall native 256 MiB
requirement. A prefix is not relabeled as complete-run traced allocation evidence.

## Initial complete minimal-capture pilot

Commit `e89252d6c6dedf2ccebb6cf3ef5caae7af0f0a9d`, still genuinely a2, completed
5,000 responses by 20 traits with concurrency four and five traits per request.
The fixture has four described levels, codes `[0,1,3,7]`, three scored 1 KiB
exemplars, 4 KiB synthetic prose responses and complete mock distributions.

| Check | Observed result |
|---|---:|
| Logical tasks/results | 100,000 |
| Requests/attempts/submissions/observations | 20,000 each |
| Canonical JSONL/CSV and R CSV rows/high-water marks | 100,000 each |
| Native worker peak | 82,706,432 bytes |
| Execution Python peak / retained after GC | 4,575,472 / 2,736,397 bytes |
| Largest export Python allocation peak | 7,130,307 bytes |
| Prepared scheduling lead / executor coroutines | 5 / 6 |
| No-op resume additional acceptances | 0 |
| Registry / observed WAL high-water | 703,967,232 / 4,635,032 bytes |

The separate representative 200-by-20 control completed 4,000 judgments and
800 requests. Execution-peak growth was 1,047,684 bytes, retained growth
1,025,273 bytes and native growth 16,781,312 bytes: all below their respective
3 MiB, 1 MiB and 48 MiB comparison limits. The retained-growth margin is narrow
and remains subject to final-source qualification; no limit is relaxed.

This was an **execution-allocation diagnostic**, not ordinary throughput.
Configuration/preparation/execution took 0.001/1.741/6,626.705 seconds; three
exports took 98.064/100.928/100.060 seconds. Independent completed-run recovery
took 469.581 seconds (469.408 process CPU seconds). Total measured lifecycle
plus verification was 7,411.437 seconds. Integrity checks, exact membership and
submission/ledger joins, probabilities/coding and immutable export hashes passed.
Runtime was Ubuntu/glibc 2.39, CPython 3.11.18, SQLite 3.53.1 and Pydantic 2.14.2.
Raw source/tool/input/environment identities and hashes remain in
`pilot-minimal/raw/scale-rubric-minimal-tracemalloc-measured-0.json` under the
external evidence root. Source and timing identities are never relabeled as a3.
This a2 run established the feasibility of that complete diagnostic fixture and
identified a recovery bottleneck. It establishes neither ordinary throughput nor
final a3 scale acceptance. Its sampler did not classify `.pending-*` staging
files; final tooling records that limitation separately from newer measurements.

The recovery interval exposed a query-plan risk: the sole-observation lookup's
`ORDER BY observation_id LIMIT 2` selected an index restricted only by run ID,
even though a `(run_id,submission_id,observation_key)` index already exists.
The active recovery I/O thread was CPU-bound; actual SQLite 3.53.1 EXPLAIN
confirmed the run-wide scan. The read-only experiment below preceded the narrow
correction; the observation alone did not justify changing schema, transactions,
privacy, provider evidence or source-integrity checks.

## Evidence-supported observation lookup correction

The read-only experiment used the pilot's unchanged 20,000-observation database
in the same Ubuntu/CPython 3.11.18/SQLite 3.53.1 environment. Eight fixed lookups
per batch, one warm-up and five interleaved ordinary samples measured median
batch wall time 0.157347670 seconds for the original query and 0.000056190
seconds for the indexed-order candidate. Separate exact SQLite VM counters were
100,015 versus 20 steps per lookup. Returned identities matched and the database
SHA-256 remained `c97dd19004bd7d5e25b7502949b1ad12f6690724d8fd59e17c30fcd0a9426495`.
These are query measurements, not an end-to-end execution speedup claim.

The correction changes only the sole-observation lookup to order by
`observation_key`, using the existing composite index. Zero rows still return
no observation, one returns that same immutable observation, and two or more
still raise a conflict. Schema/migration checksums, transactions, ownership,
source/payload verification, evidence requirements and privacy are unchanged.

A small production-getter regression first failed the original query: growing
unrelated observations from five to fifty increased VM work from 47 to 262
steps. It passes after the correction, alongside 184 persistence, execution,
safety, process-recovery, grouped and lookup-tool cases. The ten-case full/minimal
parity catalog against pinned a3 baseline
`3ad90aca02c374efc7ecdd6870e8eb0ea709bcbd` passed exact prompt, wire, response,
identity/history and export comparisons. Its comparison digest is
`5d1429972af2c4107b94212773a45e5bdbd227824272ae997cf78c6d6d6b904d`.
The pilot's 100,000 actual canonical/R rows also passed the separate R checker
using R 4.6.2/jsonlite 2.0.0. Raw query counters/timings, red evidence, parity
and R output remain outside Git under the external evidence root.

## Genuine sixteen-case a2-to-a3 compatibility result

The installed a2 producer from
`4b4563102d1bf2dbb7ede8e40b87a7734f9437dd` and installed a3 consumer from
`5bb7a39ecb1004f5e74a53deef8f7f9b30ad7ebf` passed the separate compatibility
qualification. Installed metadata and package resources were checked against
their respective Git source archives; neither saved producer versions nor the
consumer package version were relabeled.

| Genuine a2 state consumed by a3 | Full capture | Minimal capture |
|---|---|---|
| Completed | Passed | Passed |
| Partially successful grouped output | Passed | Passed |
| Independently interrupted execution | Passed | Passed |
| Accepted submission with uncertain local evidence | Passed | Passed |
| Sealed preparation | Passed | Passed |
| Durable prepared attempt before submission | Passed | Passed |
| Published response receipt | Passed | Passed |
| Durable observation before normalization | Passed | Passed |

All sixteen cases and all nine negative checks passed. Negative checks covered
unsupported package transitions; incompatible context, missing service ledger
and altered manifests under both capture policies; a corrupt full-capture frozen
snapshot; and a changed minimal-capture original. Public Python and installed CLI
status/list/resume/retry/export operations retained immutable prior manifests,
identities, unsuccessful results, probabilities/coding and older exports. Accepted
uncertainty remained unresolved until authoritative evidence was restored;
recovery did not duplicate accepted submissions.

The result is `qualified: true` in
`/mnt/VM_Fast/judgmentllm-subphase6/version-compatibility-5bb7a39/report.json`.
Production took 20.890 seconds and consumption 45.148 seconds. Both installed
environments used Linux/glibc 2.39, CPython 3.12.3, SQLite 3.45.1 and Pydantic
2.14.2; these differ from the Ubuntu/Python 3.11/SQLite 3.53.1 scale environment.
Exact phase exits, inventory and case evidence remain in `report.json`,
`identify-a2.stdout.json`, `identify-a3.stdout.json` and
`fixtures/consumer-report.json` in that directory.

The baseline source archive SHA-256 is
`0f6226b7fb1588aab3b9efd56c27867a6308bd84d372bd88652b4cbfd52b3828`;
the candidate archive SHA-256 is
`3d0144655cb394a34ce078cb7ef34b91a8d3a760158f4ee586adc183dc7c5758`.
The supervisor/tool/fixture fingerprints are retained in the report. This is
completed compatibility evidence for `5bb7a39`. A fresh installed matrix also
passed all sixteen cases and nine negative checks for actual candidate
`937352a9693fda1471a137ee06e5017347789745`, with candidate archive SHA-256
`017b270589caee1b3c46ff62c16d8fa5e97468f9f1584292f7079618604fd581`.
Its production/consumption phases took 20.881/45.084 seconds in the same
CPython 3.12.3/SQLite 3.45.1/Pydantic 2.14.2 environment. The report is retained
at `qualification/937352a9693fda1471a137ee06e5017347789745/version-compatibility/report.json`
under the external evidence root. Use of that evidence for the final candidate
requires the explicit equivalence mapping below, not a relabeled qualification
commit. The complete
sixteen-case protocol remains in
[the compatibility guide](version-compatibility-qualification.md).

## Milestone-wide acceptance matrix

| Subphase | Required acceptance evidence |
|---|---|
| 1: persistence | Registry/schema guards, immutable outcomes and artifacts, atomic writes, ownership and process exclusion |
| 2: preparation | Strict generation commitments, legacy IDs, source freezing/indexing, changed-source rejection, full/minimal privacy |
| 3: recovery | Exact prompt/payload commitments, independent acceptance ledger, every response-loss boundary, conservative unresolved work |
| 4: grouped lifecycle | Partial-member outcomes, exact-subset retry, cancellation evidence, historical outcomes and immutable exports |
| 5: public lifecycle | Shared Python/installed CLI services, versioned envelopes/exits, independent processes, all seven strict R gates |
| 6: scale | Complete 100,000-judgment minimal/full runs, bounded memory/scheduling, storage and export integrity, genuine a2 compatibility, examples and reproducible commands |

Historical reports remain evidence of their identified revisions. Merged PRs
alone do not establish final milestone acceptance. The final local grid must
rerun all applicable assertions, including the unchanged resource regressions.

## CI placement

**Decision for this subphase: retain the complete practical workload as separate
offline qualification.** The required workflow and its 20-minute job timeout
remain unchanged. The observed Windows 18m38s job leaves only 82 seconds, and
no measured complete hosted job including the new workload establishes adequate
margin. The initial traced pilot cannot be used as ordinary timing or projected
hosted-runner performance. Retained minimal ordinary measurements document their
observed range; limited full-capture observations do not qualify variability.
Neither establishes hosted Windows headroom.

Future promotion requires a separately reviewed change supported by measured
complete-job timings with at least three minutes of remaining margin in every
affected hosted lane. Account for setup, existing tests and coverage, R, builds,
clean installations, artifact uploads and observed variability. Ordinary benchmark
time is not coverage-instrumented pytest time. No full fixture is added to every
R gate or to the current required CI critical path.

Both independent and grouped memory regressions remain mandatory, with their
64 physical completions, concurrency four, three existing input scales,
180-second subprocess deadline, native RSS below 256 MiB, execution allocation
below 32 MiB, grouped export allocation below 8 MiB and all existing growth,
task and queue assertions unchanged.

## Final candidate freeze and external ledger

Complete the focused measurement tools, tests and this protocol before freezing
the final candidate. Record final results in its external ledger; do not create
a results-only commit that changes the revision being qualified. The complete
six-configuration grid must always run on that exact final commit.

Under the revised approved scope, retained complete execution and compatibility
evidence may carry forward from `937352a9693fda1471a137ee06e5017347789745` to a
benchmark/documentation-only final commit through an explicit equivalence audit.
That audit must compare all production package resources, assets, pyproject/lock
identity, workload/configuration/input definitions and relevant benchmark
behavior. It must identify any tool-only changes and explain their effect on the
measurement being reused. Raw commits, archive hashes, original harness/fixture
hashes, commands and environments remain unchanged in the record. Matching
production bytes alone is not permission to relabel a measured harness or claim
that new tools ran. Material behavioral differences leave an acceptance gap;
they are not waived by the mapping.

The first complete local grid for
`0ac76248d4089fd2d536cb5911c3eb1dc16c2550` exposed benchmark-tool portability
defects on both Windows lanes. Missing Git raised `FileNotFoundError` before
the existing archived-source fallback; Pyright also rejected POSIX-only
`sysconf` and test `st_blocks` references. Ten tool tests failed in each Windows
lane. All forty-two strict R gates, resource regressions, builds and installation
smokes passed, but that grid remains failed and does not establish acceptance.
The correction is confined to metadata collection and platform guards, with a
small missing-Git regression. It changes no production package, workload,
serialization, execution, storage measurement or resource threshold.

Retained focused execution/initialization and eight complete-export allocation
reports identify that actual `0ac7624` source. Their reuse, alongside the `937352a`
complete runs, requires an explicit review of this metadata-only tool delta.
On the measured Ubuntu environment Git was available and the POSIX branch was
already taken; its successful probe and measured execution/allocation paths are
unchanged. The ledger must retain the earlier harness fingerprint and record the
corrected final harness fingerprint separately. It must also index the failed
grid and require a new complete six-configuration grid on the corrected final
commit. Neither earlier passing lanes nor unchanged production bytes replace
that final grid.

The subsequent `d426dc04e99979e2a37d9d638fcc233b35d69821` grid passed
type checking and all forty-two R assertions, but three benchmark tests per
Windows lane still failed. Retained worker reports and two small native
reproductions identified test-directory depth: a 252-character object path
produced a 262-character acknowledgment reference, and the nested allocation
fixture used a 268-character object path. Native hard-link calls failed with
Windows error 3; the identical short-root fixture completed all 21 judgments and
six requests. These are failed qualification records, not passing lanes.
The correction gives those three tests short, owned temporary roots. It preserves
every workload parameter, completed count, capture condition, assertion and
deadline, and changes no production or benchmark algorithm. Verify the three
cases natively on both Windows versions before the new complete exact-head grid.

The ledger must identify the full commit, source/archive/lock hashes, package
version, benchmark/tool/fixture/input fingerprints, seed, commands, exits, timing
windows and Python/SQLite/Pydantic/dependency/OS/CPU/RAM/R inventory. Index each raw
report and log by path and SHA-256. Keep historical engineering evidence clearly
separate from evidence counted toward final-candidate acceptance.

The final record must cover:

- Minimal and full capture: 100,000 logical results, 20,000 physical
  requests/attempts/submissions/observations, three complete immutable exports,
  stable provenance and no duplicate accepted submissions after independent
  recovery. State the actual limited sample counts and exclusions; no additional
  full-size timing repetitions are required by the revised scope.
- Overall native peak below 256 MiB; execution allocation below 32 MiB; export
  allocation below 8 MiB; fixed workers/tasks/queues; and the existing growth
  assertions. Record phase baselines and post-GC retained values without excluding
  preparation/index costs from overall RSS. Use focused direct execution
  allocation prefixes at 200 versus 5,000 responses, separate initialization and
  complete-export measurements as described in the scale guide. Retained
  whole-scope evidence is supplemental; no complete traced execution is required.
  Baseline-subtracted peaks alone never establish a pass.
- Registry/ledger DB, WAL/SHM observations, artifact/export/staging sizes, row
  counts, hashes, temporary cleanup and reader/checkpoint behavior. Distinguish
  observed peaks, pathname totals and conservative export-overlap estimates from
  unique physical volume use.
- Executable user examples and all sixteen genuine compatibility cases plus
  every negative check, with the approved equivalence mapping where applicable.
  Record the interrupted earlier driver stage and the scope revision explicitly.
  Optional diagnostic sweeps and stress executions are unexecuted unless an
  identified acceptance gap justified them; retain runnable stress commands,
  without claiming a prefix, projection or documented command was executed.
- Ruff lint/format, strict Pyright, complete pytest with branch-aware coverage
  at least 90%, durability/privacy/recovery/ownership/resource regressions, seven
  strict R/jsonlite gates, lock verification, offline wheel/sdist builds, strict
  Twine and clean wheel/source installation smokes.

After final-source measurements and checks, run:

```bash
python3 ci/local/grid.py --ref FULL_HEAD_SHA --mode check
```

Require all six Linux/Windows/macOS Python 3.11/3.12 lanes, every scheduled gate,
source verification before/after, branch-aware coverage at least 90%, all seven
strict R/jsonlite gates, locked offline builds, strict metadata checks and both
clean-install smokes. Final evidence records the exact SHA, source/archive/lock
hashes, lane summaries, failures and environmental differences. Link the new
`/mnt/VM_Fast/judgmentllm-ci/grid/<run-id>/summary.json` from the candidate ledger
and require `qualification: true`; previous grid or successful preparation
results do not qualify the candidate. Keep benchmark collection separate from
the grid and VM/Docker operations.

No push, PR, hosted final-head run, merge, package publication, release or
Milestone 3 work is part of this local implementation task. Hosted qualification
on the eventual exact PR head remains a subsequent publication requirement.
