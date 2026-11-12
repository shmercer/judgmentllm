# Verification policy and CI profiles

This is the authoritative development/PR decision table. It supersedes earlier
universal seven-local/five-hosted requirements, including historical milestone
records. Select the highest affected boundary; few changed lines never lower
risk. Unknowns and ambiguity escalate. Deferred checks are **not passed checks**.

| Level | Typical change | Required development/PR acceptance |
|---|---|---|
| 0 — Documentation | Non-executable prose | Relevant offline links and example syntax checks |
| 1 — Low | Designated prompt-instruction wording or template prose with unchanged schema, selection, sandbox and control flow | Affected Ruff/format/Pyright, focused unit/regression and prompt/serialization contracts; no comprehensive coverage, platform, resource, R or package campaign |
| 2 — Moderate | Independent adapter or response handling | Primary Linux/Python 3.11 functional suite and coverage; affected integration/R gates, platforms and packaging; affected resource measurements locally |
| 3 — High | Execution, persistence/recovery, schemas/exports, authentication/privacy, CI/selection/coverage controls | Primary suite/coverage, representative full functional Windows 3.12 and macOS 3.11, all seven strict Linux R gates, relevant security/durability/concurrency/compatibility tests; affected resource measurements locally and package checks when affected |
| 4 — Release/comprehensive | Publication, compatibility milestone, weekly/manual complete check | Supported platform/version union, all strict R gates across platforms, primary coverage, resource workloads locally, wheel/sdist metadata and isolated installs; releases additionally require compatibility, scale/reproducibility and alpha-readiness evidence |

All existing critical tests remain available. No provider calls are authorized.
Synthetic offline implementation acceptance and explicitly authorized live
capability qualification remain separate. Resource sizes, 90/180-second child
deadlines, memory ceilings, ownership/credential/privacy safeguards and historical
fixtures remain unchanged. The [requirement map](../ci/verification-map.json)
records the protected assertions and applicability, rather than a test-count gate.

## Hosted Linux setup recovery (2026-11-11)

The successful [pre-merge Linux run](https://github.com/shmercer/judgmentllm/actions/runs/42951676363)
used Ubuntu24.04 and installed R4.6.2 in under a minute. The subsequent
[main run #122](https://github.com/shmercer/judgmentllm/actions/runs/43038579450)
resolved `ubuntu-latest` to Ubuntu26.04. Its R setup repeatedly failed DNS for all
three Ubuntu mirrors, continued using old package indexes and exhausted the
50-minute job while downloading system requirements. Tests and R gates never ran;
that cancelled setup and missing diagnostic artifact remain failed evidence.

Pin hosted Linux jobs and all Linux matrix entries to the tested `ubuntu-24.04`
image. Bound only the R dependency-setup step to five minutes so another network
failure cannot consume the full functional-suite job. The 50-minute complete-job
limit, Python/R/jsonlite versions, all seven strict R gates, coverage floors,
resource deadlines, full platform scopes and protected-base admission are unchanged.
The image difference is an observed change, not proof that the OS caused DNS
failure. Restoring the known image is a narrow mitigation; network failure still
fails acceptance. A candidate CI edit does not alter its own protected-base run:
review it independently before updating trusted main. Never count unrun R gates
or a manual candidate-workflow run as protected-base acceptance of another PR.

## Commands and ordinary workflow

### Resource measurements execute locally

Resource measurement workloads run only in local verification, including at
Level 4. Hosted GitHub Actions lanes defer them. The plan retains their risk-based
applicability as `resources` and declares `resource_execution: local`.
`ci/check.py --profile pr` explicitly selects local execution; native comprehensive
and release runners retain their existing workloads. Workload dimensions, memory
ceilings and child deadlines are unchanged.

Hosted lanes explicitly select hosted execution, retain every functional case,
the isolated cold SDK byte-identity correctness probe, strict primary coverage and
all applicable R/jsonlite gates. If deferring measurements loses required coverage,
acceptance fails. Local receipts identify their execution scope and cannot satisfy
hosted admission; quality receipts also require the same scope. GitHub Actions
rejects the local-execution option.

The [Issue #38 run](https://github.com/shmercer/judgmentllm/actions/runs/43125144700)
passed all 7,035 tests and the strict coverage comparison. Its ordinary partition
took 33m29s and resource measurements another 13m14s. The 50-minute job expired in
the last R CLI gate after six separate R gates passed. The same local ordinary and
complete suites both covered 18,804 of 19,580 statements: the resource partition
added no covered production lines in that run. This supports moving measurement
work locally, retaining functional tests, coverage, R gates and fixture deadlines.
The timeout remains failed evidence.

This is a protected policy change. Its PR's automatic base-policy workflow is
cancelled before resource execution, and the proposed hosted workflow is tested
with an explicit manual dispatch for review. A candidate policy cannot reduce its
own protected-base requirements. Independent review and an accepted update to
trusted main are required before dependent PRs can use the new hosted policy.

Prepare once with `uv sync --locked --all-extras --group dev --python 3.11`.
Use these from the checkout; outputs default to fresh external temporary paths:

```bash
# Development feedback; explicit component selection remains non-qualifying.
python3 ci/check.py --profile fast --test ci/local
python3 ci/check.py --profile fast --test tests/test_anthropic_transport.py
# Commit first, inspect automatic selection and its per-file reasons.
python3 ci/check.py --profile pr --plan-only --changed-since origin/main
# Documentation/low/moderate/high minimums; classification may strengthen them.
python3 ci/check.py --profile pr --risk 0
python3 ci/check.py --profile pr --risk 1
python3 ci/check.py --profile pr --risk 2
python3 ci/check.py --profile pr --risk 3
# Explicit complete native grid (preparation may download; checking is offline).
REF=$(git rev-parse HEAD)
python3 ci/check.py --profile comprehensive --ref "$REF" --mode prepare
python3 ci/check.py --profile comprehensive --ref "$REF"
# Complete hosted matrix, also scheduled weekly on Monday.
gh workflow run ci.yml -f risk=4
```

Local `pr` runs the primary lane only, with a clean committed source and Python
3.11. Its exit status reports that lane; its output lists pending hosted lanes.
Use hosted **CI acceptance** for complete PR acceptance. Do not routinely run
both the primary suite locally and again hosted, or both complete grids. Use
`--risk` to request stronger acceptance; it never downgrades inferred risk.
A manual hosted run can select a feature branch with `--ref BRANCH`.
The complete hosted matrix includes Linux 3.11/3.12/3.15, Windows 3.11/3.12,
and macOS 3.11/3.12/3.15: the union of former local/hosted scopes. This includes
complete Windows 3.11 troubleshooting without a lightweight success status.

Static checks and CI selftests execute once, before scheduling expensive lanes.
When packaging is affected, build/metadata/install smokes precede the full suite.
Prepare locked core/build dependencies in the separate environment preparation
stage. For lanes that install distributions, export the locked core requirements
with hashes and install them into a separate temporary target during preparation.
`uv sync --locked` can cache wheels without caching the package-index metadata
needed by a later isolated installation. This explicit preparation supplies both;
it does not change the verification environment or substitute for the clean
installation checks. Acceptance passes `--offline` explicitly to wheel/sdist installation
smokes, matching the native runner; missing cache entries fail rather than trying
package-index DNS from an isolated check. Native complete-grid check mode retains
its prepared offline caches.
Pytest uses the existing complete collection/partition/case-receipt runner.
Ordinary and serial functional cases remain; expensive resource measurements are
local and conditional outside Level 4. Hosted lanes never run measurement
workloads. Cold pinned-SDK byte-identity probes retain their
isolation and remain correctness requirements even when resource measurements
are deferred. Case existence/replacement, crash propagation, no unexpected
skips/xfails and selection completeness stay enforced. Coverage is explicitly
disabled in every nonprimary lane. If deferring resource tests loses required
coverage, acceptance fails; investigate that dependency or request resources,
never credit old measurements as fresh execution.

The expanded multi-provider execution-to-R fixture runs in the existing serial
partition. Its two original tests, complete persist/export pipeline, immutable
artifact checks and 60-second subprocess deadline are unchanged. Serial execution
avoids competing xdist workers during that bounded setup; the seven separate
strict R gates remain mandatory. Local PR planning disables bytecode before
importing policy modules, and its acceptance subprocess uses `-B`, so a copied
policy tree does not gain untracked bytecode during source verification.

The fast profile remains a development aid with caller-selected targets. Its
receipt cannot establish PR acceptance. Benchmarks and partial suite stages
remain non-qualifying. Native grid commands and isolation requirements are in
[local CI](local-ci.md); that optional complete grid still retains its 49 R gates.

## Classification and trust

`ci/risk.py` uses boundary rules, content checks and conservative defaults.
Python wording edits may replace only designated instruction literals in
`output_instructions_v3`, `_output_instructions_v2` and `render_prompt`;
all other AST content stays exact. Schema keys, selection values (including
`canonical_neutral`), sandbox rules (including the Jinja filter allowlist), call
arguments and control-flow predicates are Level 3 unless a separate narrow rule
independently establishes safety. Template wording must preserve control
blocks/placeholders. The sole predicate exception recognizes PR #27's additive
literal `instructions[0]` replacement under exactly
`not native and output.commitment_version == 5`, first inside the rubric guard,
with its specific type-only import/annotation. Editing that predicate escalates;
protected renderer logic and calls must remain unchanged.
Unknown prompt control flow, unknown paths and changed/removed existing tests
escalate. Additive prompt tests may stay focused; existing statements/fixtures
remain exact. This deliberately avoids general semantic/dependency analysis.

Independent adapters/response handling for known provider families start at Level 2.
Shared provider protocols, ledgers, mock execution, shared schemas and authentication
CLI routes are Level 3. Credential/privacy/authority/recovery edits inside an adapter
also escalate, including predicate-only edits to a sensitive helper. Unknown provider
modules escalate; a directory name never downgrades a shared boundary. Touching
shared execution raises the whole change to Level 3. Path/process/OS-sensitive moderate code adds
representative platforms. All CI, risk-map, coverage, dependency, selection and
exclusion controls are Level 3, regardless of their size. Level 3 runs all seven
R gates on Linux; Level 2 runs relevant export/execution gates for provider output
changes. Unknown R-facing modules escalate to Level 3. Packaging is conditional;
CI/package/dependency changes exercise it. Resource-sensitive execution,
preparation, persistence and CI orchestration changes request resource tests.

For PRs, `pull_request_target` runs the workflow from the protected base and
checks out the literal head separately. The classifier, runner, coverage floor
and historical test contract come from that base. Proposed replacements cannot
reduce their own acceptance. Candidate code runs with read-only repository
permissions, no secrets, no persisted Git credentials and no shared uv cache.
Only the separate trusted aggregator can write a commit status; it never runs
candidate code. It rejects missing/extra/duplicate/failed/stale lane receipts,
wrong source/plan/run identities, partial quality receipts and omitted required
steps (including R/coverage). It independently reconstructs the plan from the
protected policy and literal base/head commits, so a replaced plan artifact
cannot reduce acceptance. It also requires successful plan/matrix job results.
Diagnostic artifacts are retained on failure and superseded runs are cancelled.
The final acceptance artifact must upload successfully before the aggregator
publishes success. Status publication is its last step and includes every
preceding step's outcome, including a failed or missing artifact upload.

Read-only settings inspection on 2026-11-09 found `main` unprotected, with no
required status checks and no repository rulesets. This PR does not modify those
settings. **An administrator must configure protection:** require the commit
status named **CI acceptance** from GitHub Actions, remove obsolete matrix
status requirements if present, require review of CI/coverage/selection changes and a branch
up to date with protected `main`. Protect the base workflow/policy with review
rules (or a required workflow ruleset). Workflow YAML does not edit branch
protection. Until this migration is completed, do not interpret missing old/new
checks as acceptance. Weekly failures appear as failed Actions runs and a failed
exact-head status; inspect their artifacts before release.

Bootstrap: before this workflow reaches `main`, `pull_request_target` still uses
the old base workflow, which lacks this event. Validate this proposal with a
manual dispatch of its reviewed feature branch at minimum Level 3. That explicitly
selected run tests the new policy; it is not proof that the protected-base event
has already been deployed. Keep the PR open for independent review. After merge,
ordinary PRs cannot use their proposed classifier/workflow to downgrade checks.

## Change-aware coverage and reconciliation

Measurement keeps branch coverage enabled and the combined **90%** floor.
Overall historical line/branch/combined ratios remain blocking. Unchanged source
must preserve denominators, exclusions and every previously covered line/arc.
Coverage settings and pytest selection/coverage defaults must match the trusted
baseline; changing thresholds/exclusions cannot silently qualify.

Modified modules need a clean, committed source identity and a hash-verified
historical snapshot. Simple exact equal-line blocks preserve previously covered
unchanged lines. Every changed executable line and each branch originating on a
changed line must be covered. File line/branch/combined percentages may lose at
most **one percentage point**; new modules require 90% lines/combined and 80%
branches, plus all changed executable lines/branch origins. Larger regressions
fail and require investigation/review, not automatic baseline regeneration.
Removed code is reported. **Exact branch correspondence across edited source is
not established**; reports say so and use ratios/changed-origin checks. Equal-line
mapping is conservative evidence, not proof that unchanged text has unchanged
semantics. Integration/security tests still protect behavior.

Missing/corrupt/inconsistent counts, omitted modules, uncommitted source,
unavailable/mismatched historical source or altered exclusions fail safely.
New type-only exclusions also need explicit baseline reconciliation. The historical
floor is read from the protected base during PR acceptance: editing a candidate
floor cannot bypass current acceptance. Review proposed baseline edits as high
risk before merging them into the trusted base. At qualification/release
checkpoints reconcile changed modules individually against retained measurements,
with source/evidence provenance and reasons. Preserve unchanged entries and
historical totals. `source_snapshot_commit` identifies matching source bytes;
it does not relabel old measurements. Archive-only native runs require this
reconciliation because they have no Git history for changed-source comparison.

PR #27's [retained failure analysis](ci-test-audit.md#pr-27-coverage-baseline-failure)
records the narrow correction, improved coverage and skipped downstream gates.
The original failed run remains failed.

## Release qualification

Level 4 offline checks do not authorize publication. The existing release profile
also invokes genuine installed a2/current compatibility and records pending
scale/reproducibility, hosted and human review requirements. See the existing
[compatibility guide](version-compatibility-qualification.md) and
[alpha readiness checklist](alpha-release-readiness.md). For that native option:

```bash
REF=$(git rev-parse HEAD)
python3 ci/check.py --profile release --ref "$REF" \
  --baseline-python /external/a2-env/bin/python \
  --candidate-python /external/candidate-env/bin/python \
  --baseline-source-archive /external/a2-source.tar \
  --candidate-source-archive /external/candidate-source.tar \
  --release-evidence /external/new-release-evidence
```

Missing inputs fail. Its `release_ready: false` and exit 2 remain pending evidence,
not acceptance. Hosted comprehensive qualification can supply the platform
campaign without a duplicate local campaign; installed compatibility and the
independent scale/alpha gates still need their existing tools and review.

## Historical allocation study

The following retained experiment/recovery details describe optional complete-grid
operation. They do not override the risk table or require new benchmarking.
## Allocation budgets and experiments

The host has sixteen physical / thirty-two logical CPUs and 128 GB RAM. Windows shares six guest CPUs and 12 GiB across its Python lanes. macOS
shares four visible guest CPUs and 16 GiB. Its existing configuration requests
six cores, but Quickemu rounds down to four for macOS; both the boot topology
and in-guest telemetry confirm four. No guest setting is changed. Linux containers receive explicit per-container CPU-time
quotas and 16 GiB limits. Slots coordinate resource batches within each owned
allocation; no workstation-wide resource semaphore is imposed.

| Schedule | Linux workers × active configurations / CPU quota each | Windows workers × active configurations | macOS workers × active configurations | Maximum resource batches Linux / Windows / macOS |
|---|---|---|---|---|
| baseline (A) | 0 × 3 / 6 | 0 × 2 | 0 × 2 | 3 / 2 / 2 |
| parallel (B, tuned) | 6 × 3 / 6 | 4 × 2 | 2 × 2 | 3 / 2 / 2 |
| staged (C) | 6 × 1 / 6 | 4 × 1 | 4 × 1 | 1 / 1 / 1 |
| resource-aware (D) | 6 × 2 / 6 | 4 × 2 | 2 × 2 | 2 / 2 / 2 |
| isolated reference | 4 × 1 / 6 | 4 × 1 | 4 × 1 | One platform at a time |

Zero workers means sequential pytest. Guests retain their actual CPU/RAM
allocations under every schedule. Guest CPUs/RAM count once, not once per Python
lane. Grid admission checks at least two logical CPUs and 16 GiB host headroom;
quotas are caps, not reserved physical cores. Case receipts record actual affinity,
cgroup quota and visible/available memory. Guest sharing, disk/memory bandwidth
and non-CI host activity still require measurement.

Historical optimization study: the parallel schedule was the leading final candidate. Its measured four-worker
Windows allocation is retained; six workers per Windows lane have not been
shown to improve the complete grid. Two simultaneous lanes share the same six
guest CPUs, so the worker count is not an additional CPU reservation. Final
acceptance requires a fresh complete run after the SDK isolation change, not
the earlier failed parallel timing. The staged schedule is a demonstrated
passing alternative when reduced contention is more useful than total speed.

Use identical acceptance and source scope when comparing schedules:

```bash
uv run --offline --no-sync python ci/local/benchmark.py --ref "$REF" \
  --schedule parallel --output /mnt/VM_Fast/judgmentllm-ci-optimization-evidence/new-B
python3 ci/local/grid.py --ref "$REF" --platform linux --suite-stage ordinary \
  --schedule staged --benchmark-workers 0,2,4,6
python3 ci/local/grid.py --ref "$REF" --suite-stage resource --schedule isolated
python3 ci/local/grid.py --ref "$REF" --suite-stage resource --schedule parallel
```

Ordinary/resource-only modes and worker sweeps explicitly record incomplete
qualification. They cannot produce `qualification: true`; complete-grid qualification still
requires a full seven-configuration exact-head check when that grid is requested. The benchmark wrapper
retains total grid wall time, all host samples, CPU time/utilization, host RAM/swap,
disk counters, source/dependency/worker/budget records and every failed attempt.
Suite records add partition CPU/process-tree RSS and complete test outcomes.
Native resource fixtures retain their own authoritative fresh-process peaks,
queue/process counts and child deadlines. Sampling is labeled and never replaces
their measurements. The command sampler rejects descendant births before its
start window or in the future, allowing one second for OS timestamp precision.
Windows boot-clock resynchronization can otherwise combine stale parent IDs
with future process birth times and overstate a descendant tree. Original native
workload peaks and whole-host counters are independent of these estimates.
Historical pre-filter tree samples remain retained and must be interpreted as
approximate rather than exact per-command attribution.
Compare timing variability and repeat leading schedules
where needed; no claim of stable speedup follows from a single favorable sample.

An isolated nine-module, 709-case pilot at six CPUs / 16 GiB passed every case
with 0/2/4/6 workers. File distribution took 465.56 / 388.20 / 270.38 / 266.28s
including collection. Work stealing took 235.33s with four and 169.77s with six.
These are focused observations, not full qualification. The six-worker file and
work-stealing passes used 320.19 and 324.15 waited-child CPU seconds respectively.
Sampled tree RSS increased from about 879 to 988 MiB (shared pages can be counted
more than once). The full-grid trials validate whether that tradeoff remains
useful with other configurations active.

Linux starts another selected configuration as soon as any owned container
finishes. A slow coverage lane does not leave a completed sibling's slot idle.
Every lane still contributes its exit status; a failed peer never cancels the
remaining configurations. Deterministic scheduling tests cover both successful
and failed early completion without depending on favorable timing ratios.

Begin resource experiments with isolated measurements, then compare identical
workloads at bounded concurrency across separately allocated containers/guests.
The focused three-platform resource comparison uses Python 3.12 with one lane
per allocation: six Linux CPUs / 16 GiB, six Windows CPUs / 12 GiB, and four
visible macOS CPUs / 16 GiB. Complete-grid A–D measurements subsequently
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
