# CI execution profiles and measurement protocol

This workstream starts after merged Phase 5A PR #24 at
`1298bedf5e408d48ef1fd6e113677a0ece27aa24`. Production code and historical
contracts are unchanged. The [baseline and audit](ci-test-audit.md) describe
the reviewed redundancies; [the requirement map](../ci/verification-map.json)
locates every mandatory verification category. No platform, R gate frequency,
resource size/threshold/deadline or security guarantee is removed.

## Development feedback

Prepare the locked environment with `uv sync --locked --all-extras --group dev`.
From the worktree, use:

```bash
python3 ci/check.py --profile fast --test ci/local
python3 ci/check.py --profile fast --test tests/test_execution_retry.py
python3 ci/check.py --profile fast --changed-since origin/main
```

The first two commands select a component explicitly. Essential model, config,
result, package, authentication/privacy and public-lifecycle regressions always
remain. The third selects changed test components, adding infrastructure tests
for CI/workflow edits and conservatively expanding to the ordinary full suite
for unknown production/script/dependency changes. Explicit targets are useful
during implementation, including Codex cycles. Selection is not dependency
proof or PR acceptance. Resource/serial tests are excluded, coverage is disabled,
and the receipt states `qualification: false`.

Ruff lint/format and strict Pyright precede bounded pytest. The default is four
workers; choose an explicit smaller `--workers` value for a constrained machine.
The measured CI-component checkpoint passed 403 cases with all quality checks
in 33.536 seconds. Broader changed-component selections can exceed five minutes.
Every profile keeps stdout/stderr and failure exits; development output defaults
to a unique external temporary directory, or use `--output /external/new/path`.

## Full PR qualification

Commit first. All source changes, including documentation/workflow changes,
require fresh qualification of the final head:

```bash
REF=$(git rev-parse HEAD)
python3 ci/check.py --profile pr --ref "$REF" --mode prepare
python3 ci/check.py --profile pr --ref "$REF"
```

Preparation may download locked prerequisites into JudgmentLLM namespaces.
Checking is offline and uses exact archives/hashes. This profile requires all
seven local configurations, full selected tests, original quality/build/core
installation/offline checks, and seven strict R/jsonlite gates in each lane
(49 executions). Only Linux 3.11 measures branch-enabled coverage. The existing
90% combined gate remains; line coverage and branch coverage must each meet
the baseline and every unchanged production covered line/branch must remain.
Other lanes explicitly disable coverage. See [local CI](local-ci.md) for locks,
guest prerequisites, shutdown and exact-source evidence admission.

The five hosted jobs additionally qualify the identical PR head: Ubuntu/macOS
3.11/3.15 and Windows 3.12. Checkout selects the literal PR head. Each job runs
all ordinary cases plus isolated resource cases; only documented platform and
nonrequired hosted R skips are accepted. Ubuntu 3.11 requires real R/jsonlite.
The complete Windows 3.11 manual-dispatch job remains available. All action and
dependency pins, existing setup caches, cancellation of superseded runs and
50-minute deadlines remain. Local 3.11/3.12 never replace hosted 3.15.

## Release qualification

Release verification contains full PR qualification and genuine installed
a2-to-current compatibility, along with the separate established scale and
release-readiness requirements. It never publishes or makes live provider calls.
Prepare the old/current installed wheels and source archives using
[the existing compatibility guide](version-compatibility-qualification.md), then:

```bash
REF=$(git rev-parse HEAD)
python3 ci/check.py --profile release --ref "$REF" \
  --baseline-python /external/a2-env/bin/python \
  --candidate-python /external/candidate-env/bin/python \
  --baseline-source-archive /external/a2-source.tar \
  --candidate-source-archive /external/candidate-source.tar \
  --release-evidence /external/new-release-evidence
```

The command runs the complete local grid, then the existing sixteen-case
compatibility tool with its original negative checks/deadlines. Missing inputs
fail rather than skip. Its receipt always leaves `release_ready: false`: final
publication also requires exact-head hosted results, the
[scale/allocation/export protocol](milestone-2-subphase-6-verification.md),
and the [alpha readiness checklist](alpha-release-readiness.md). Those include
the permitted explicitly reviewed source-equivalence basis for retained large
scale evidence, independent usability, provider-family live acceptance,
artifact/tag/provenance and human publication approval. Do not substitute small
resource tests or green CI for those requirements. This optimization does not
change their scope or authorize provider usage/PyPI publication.

## Pytest partitions and failure reproduction

`ci/local/suite.py` collects the entire selected suite into exact node IDs before
execution. Ordinary cases use explicit `pytest-xdist` workers and `loadfile`
distribution. Immutable module fixtures stay in one worker. A `serial` marker
reserves a test that needs one pytest process; a `resource` marker selects tests
whose memory/timing acceptance needs controlled allocation. Resource batches
never use xdist. Current process/SQLite/concurrency correctness tests use unique
temporary stores and owned children and can remain ordinary; process creation
alone is not a reason to serialize an entire suite.

The runner verifies complete per-phase receipts, exactly one execution per case,
documented skips, no xfail and failure propagation. Worker crashes are fatal;
automatic worker restart is disabled. `ci/test-contract.json` accounts for the
original selected IDs and permits documented many-to-one replacements. Counts
alone do not establish equivalence. This PR preserves all canonical cases.
Coverage append/xdist combination uses the original source/branch settings;
`coverage-comparison.json` records lost line/branch sets and rejects them.
No exclusion or omission setting is broadened.

The floor records this unchanged-production-source optimization. A later
production milestone must review and update the affected module's source/coverage
contract with its new tests, retaining exact sets for unchanged modules and
explaining denominator changes. Do not regenerate the floor merely to accept a
test deletion, exclusion or missed branch.

Windows early durability/privacy checks remain early and emit receipts. The full
runner validates and accounts for those 69 cases, without rerunning them. Missing,
failed, overlapping or resource-bearing preflight receipts fail closed.
Full receipts/logs are copied out of short Windows scratch paths before cleanup.
Hosted artifacts retain case/selection/allocation receipts and resource JSON;
they omit the bulky synthetic runtime stores.

For an isolated reproduction, use the reported locked interpreter and node ID:

```bash
uv run --offline --no-sync pytest --no-cov 'tests/test_module.py::test_case[param]'
uv run --offline --no-sync python ci/local/suite.py \
  --output /external/new-suite-run --workers 2 --no-coverage tests
```

The first command is focused debugging. The second runs the complete test target
with separate resource partitions. Use the native adapters to reproduce actual
platform failures; a host-only rerun is not replacement platform acceptance.

## Allocation budgets and experiments

The host has sixteen physical / thirty-two logical CPUs and 128 GB RAM. Windows
and macOS each share six guest CPUs across their Python lanes; they retain 12 and
16 GiB respectively. Linux containers receive explicit per-container CPU-time
quotas and 16 GiB limits. Slots coordinate resource batches within each owned
allocation; no workstation-wide resource semaphore is imposed.

| Schedule | Linux workers × active configurations / CPU quota each | Windows workers × active configurations | macOS workers × active configurations | Maximum resource batches Linux / Windows / macOS |
|---|---|---|---|---|
| baseline (A) | 0 × 3 / 6 | 0 × 2 | 0 × 2 | 3 / 2 / 2 |
| parallel (B) | 2 × 3 / 4 | 2 × 2 | 2 × 2 | 2 / 1 / 1 |
| staged (C) | 4 × 1 / 6 | 4 × 1 | 4 × 1 | 1 / 1 / 1 |
| resource-aware (D) | 3 × 2 / 3 | 4 × 1 | 4 × 1 | 2 / 1 / 1 |
| isolated reference | 4 × 1 / 6 | 4 × 1 | 4 × 1 | One platform at a time |

Zero workers means sequential pytest. Guests retain their actual CPU/RAM
allocations under every schedule. Guest CPUs/RAM count once, not once per Python
lane. Grid admission checks at least two logical CPUs and 16 GiB host headroom;
quotas are caps, not reserved physical cores. Case receipts record actual affinity,
cgroup quota and visible/available memory. Guest sharing, disk/memory bandwidth
and non-CI host activity still require measurement.

Use identical acceptance and source scope when comparing schedules:

```bash
uv run --offline --no-sync python ci/local/benchmark.py --ref "$REF" \
  --schedule parallel --output /mnt/VM_Fast/judgmentllm-ci-optimization-evidence/new-B
python3 ci/local/grid.py --ref "$REF" --suite-stage ordinary \
  --schedule staged --benchmark-workers 0,2,4,6
python3 ci/local/grid.py --ref "$REF" --suite-stage resource --schedule isolated
python3 ci/local/grid.py --ref "$REF" --suite-stage resource --schedule parallel
```

Ordinary/resource-only modes and worker sweeps explicitly record incomplete
qualification. They cannot produce `qualification: true`; final acceptance still
requires a full seven-configuration exact-head check. The benchmark wrapper
retains total grid wall time, all host samples, CPU time/utilization, host RAM/swap,
disk counters, source/dependency/worker/budget records and every failed attempt.
Suite records add partition CPU/process-tree RSS and complete test outcomes.
Native resource fixtures retain their own authoritative fresh-process peaks,
queue/process counts and child deadlines. Sampling is labeled and never replaces
their measurements. Compare timing variability and repeat leading schedules
where needed; no claim of stable speedup follows from a single favorable sample.

Begin resource experiments with isolated measurements, then compare identical
workloads at bounded concurrency across separately allocated containers/guests.
The focused three-platform resource comparison uses Python 3.12, one six-CPU
lane in each allocation (16/12/16 GiB). Complete-grid A–D measurements subsequently
exercise all seven versions and original resource acceptance. Choose the fastest
reliable measured schedule; serialize globally only if measurements show that
concurrency invalidates measurements or causes sensitive failures. The original
90/180-second child deadlines, scale, memory/queue/process ceilings never change.

## Temporary guest allocation recovery

No guest allocation override is enabled by this PR. The existing Quickemu CLI
does not offer a supported per-run CPU/RAM override; Dockur stores those values
in the existing container's persistent configuration. Changing those files or
recreating containers is outside this workstream. Disposable Linux quotas are
per-run controls and require no persistent restoration.

If a supported guest runtime control is later confirmed, use the small
`ci/local/override_recovery.py` journal under
`/mnt/VM_Fast/judgmentllm-ci/overrides` while holding the existing guest lock.
Its manifest records guest identity/ownership, original and proposed CPU/RAM,
control mechanism, experiment and lock/owner identities, application/restoration
states and verified readback. Durable intent is fsynced before applying anything.
Inspect identity, idleness and exclusive ownership before each control action.

Handled success, failure, cancellation and interruption restore in `finally` and
verify actual settings. Startup admission in both the grid and direct guest
adapters refuses an incomplete/malformed journal for the affected guest. An
authorized recovery must acquire the existing lock, supply the recorded supported
control, reconcile actual CPU/RAM and restore/verify the original allocation.
Process disappearance never establishes restoration. If control or ownership
differs, retain `needs_review`; do not bypass the guard or edit a manifest to
claim restoration. There is no arbitrary VM shell/configuration manager.

Tests include deterministic ordinary failure, handled interrupts, partial apply,
restore failure/lost ownership, and actual subprocess death before apply, during
partial apply and after apply. Restart tests read independent synthetic actual
settings, block admission, restore and verify before resuming. OS resource-slot
locks are independently tested for release after child death.

## Evidence and final acceptance

All raw baseline/profiling/fault/benchmark/qualification evidence belongs under
`/mnt/VM_Fast/judgmentllm-ci-optimization-evidence`. Native adapter runs remain in
their established JudgmentLLM state namespaces; summaries link those immutable
records. Historical evidence is copied/read, never modified. Failed attempts are
retained and identified separately from passing final qualification.

Freeze the final source commit after selecting the schedule and completing docs.
Run a fresh full local grid, then all five hosted jobs against that exact PR head.
Write the final comparison/report outside Git and link it from the PR so recording
results does not change the qualified head. Keep the PR open for review.
