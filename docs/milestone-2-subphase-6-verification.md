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
for the candidate's measured scale results, compatibility report and local grid;
the successful earlier runs below do not qualify a later source revision.
Completion requires successful full minimal/full 5,000-response by 20-trait runs,
genuine a2-to-a3 qualification and the complete six-configuration local grid on
the frozen final candidate. No partial, projected, timed-out or preparation-only
workload establishes full qualification. Missing, pending or failed applicable
ledger entries leave milestone acceptance unestablished.

Raw inputs, databases, exports, profiler outputs, environments, and final
machine-readable evidence live under `/mnt/VM_Fast/judgmentllm-subphase6/`.
Only the small explicitly authorized synthetic a2 regression fixture is retained
with tests. Exact-final-commit evidence is written outside Git after freezing the
candidate, so recording results does not change the commit being qualified.
The unreleased `0.1.0a3` candidate is a development-version transition, not a
package publication or a claim that every acceptance gate has completed.

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
completed compatibility evidence for `5bb7a39`, not a waiver of the fresh
exact-final-source qualification required by the final ledger. The complete
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
hosted-runner performance. Local ordinary measurements will document practical
runtime and variability; they do not establish hosted Windows headroom.

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

Complete code, tests and this protocol before freezing the candidate. Commit the
proposed head, create an immutable source/archive manifest and run qualification
from that exact source. Record later numeric results in its external ledger;
do not create a results-only commit that changes the revision being qualified.
Any later tracked change requires a new candidate and fresh applicable
exact-commit qualification.

The ledger must identify the full commit, source/archive/lock hashes, package
version, benchmark/tool/fixture/input fingerprints, seed, commands, exits, timing
windows and Python/SQLite/Pydantic/dependency/OS/CPU/RAM/R inventory. Index each raw
report and log by path and SHA-256. Keep historical engineering evidence clearly
separate from evidence counted toward final-candidate acceptance.

The final record must cover:

- Minimal and full capture: 100,000 logical results, 20,000 physical
  requests/attempts/submissions/observations, three complete immutable exports,
  stable provenance and no duplicate accepted submissions after independent
  recovery. Ordinary warm-up/repetitions and separate allocation diagnostics
  follow [the scale guide](scale-qualification.md); retain failures and outliers.
- Overall native peak below 256 MiB; execution allocation below 32 MiB; export
  allocation below 8 MiB; fixed workers/tasks/queues; and the existing growth
  assertions. Record phase baselines and post-GC retained values without excluding
  preparation/index costs from overall RSS. Whole-scope absolute phase peaks may
  establish the stricter allocation proof described in the scale guide; direct
  execution-scope full diagnostics are required only where that proof is absent
  or fails. Baseline-subtracted peaks alone never establish a pass.
- Registry/ledger DB, WAL/SHM observations, artifact/export/staging sizes, row
  counts, hashes, temporary cleanup and reader/checkpoint behavior. Distinguish
  observed peaks, pathname totals and conservative export-overlap estimates from
  unique physical volume use.
- Status-reader, pinned-reader and history diagnostics; actual tested stress
  extents and counts; executable user examples; all sixteen genuine compatibility
  cases and every negative check. Document unexecuted complete stress workloads
  explicitly, without substituting a prefix or projection.
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
