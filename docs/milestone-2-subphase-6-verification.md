# Milestone 2 Subphase 6 verification

Implementation starts from merge `4b4563102d1bf2dbb7ede8e40b87a7734f9437dd`
on `codex/milestone-2-subphase-6-scale-qualification`, in an independent
worktree. Remote main was fetched and matched that commit; PR #10 is merged.
The five post-merge jobs in [run 41951607071](https://github.com/shmercer/judgmentllm/actions/runs/41951607071)
passed. Their durations were Ubuntu 3.11 10m38s, Ubuntu 3.15 7m16s, macOS
3.11 6m05s, macOS 3.15 7m34s, and Windows 3.12 16m10s. The final PR #10
Windows job took 18m38s, leaving 82 seconds under the unchanged 20-minute limit.
These observations are not a statistically qualified runtime distribution.

## Acceptance status and evidence

This report is prepared before exact-head qualification. Completion requires
successful full minimal/full 5,000-response by 20-trait runs, genuine a2-to-a3
qualification, and the complete six-configuration local grid. No partial,
projected, timed-out or preparation-only workload establishes full qualification.

Raw inputs, databases, exports, profiler outputs, environments, and final
machine-readable evidence live under `/mnt/VM_Fast/judgmentllm-subphase6/`.
Only the small explicitly authorized synthetic a2 regression fixture is retained
with tests. Exact-final-commit evidence is written outside Git after freezing the
candidate, so recording results does not change the commit being qualified.

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

The recovery interval exposed a query-plan risk: the sole-observation lookup's
`ORDER BY observation_id LIMIT 2` selected an index restricted only by run ID,
even though a `(run_id,submission_id,observation_key)` index already exists.
The active recovery I/O thread was CPU-bound; actual SQLite 3.53.1 EXPLAIN
confirmed the run-wide scan. A reproducible read-only query experiment must
precede any narrow correction. No schema, transaction, privacy, provider-evidence
or source-integrity change is authorized by that observation alone.

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

Keep the full practical workload outside routine required CI unless measured
complete-job timings demonstrate at least three minutes of remaining margin in
every affected hosted lane. Hosted timing includes setup, existing tests and
coverage, R, builds, clean installations, and artifact uploads. Instrumented
benchmark wall time is not ordinary throughput. Ordinary benchmark time is not
coverage-instrumented pytest time. No new full workload is added to Windows.

Both independent and grouped memory regressions remain mandatory, with their
64 physical completions, concurrency four, three existing input scales,
180-second subprocess deadline, native RSS below 256 MiB, execution allocation
below 32 MiB, grouped export allocation below 8 MiB and all existing growth,
task and queue assertions unchanged.

## Final local qualification

After the final commit, run:

```bash
python3 ci/local/grid.py --ref FULL_HEAD_SHA --mode check
```

Require all six Linux/Windows/macOS Python 3.11/3.12 lanes, every scheduled gate,
source verification before/after, branch-aware coverage at least 90%, all seven
strict R/jsonlite gates, locked offline builds, strict metadata checks and both
clean-install smokes. Final evidence records the exact SHA, source/archive/lock
hashes, lane summaries, failures and environmental differences.

No push, PR, hosted final-head run, merge, package publication, release or
Milestone 3 work is part of this local implementation task. Hosted qualification
on the eventual exact PR head remains a subsequent publication requirement.
