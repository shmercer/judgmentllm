# Issue #59: faster verification with targeted coverage and safe local concurrency

The [issue update](https://github.com/shmercer/judgmentllm/issues/59#issuecomment-6885679158)
replaces the original issue's full-history Level 2 and sequential-local constraints.
All changes belong to one PR. The [risk policy](ci-optimization.md) remains authoritative.
No production background timer is enabled by implementation or qualification.

## Planning and selection

`ci/risk.py` takes exact base/head commits and aggregates the maximum per-file risk.
Plans include per-file reasons, escalation/fallback reasons, unknown production
paths separately from new tests, trusted dependency closures, each OS profile,
local/hosted gates and immutable policy/metadata hashes. Locally, taxonomy and
inventory are read from the protected base Git objects; hosted planning uses the
separate protected checkout. Candidate metadata can add requirements, never remove them.

`ci/additions.py` preserves every existing module statement's AST and relative
order. Independent additions cannot shadow names, install fixtures/collection
controls, mutate global bindings or execute calls in annotations/defaults/decorators.
Only literal parametrization is allowed. New modules contain imports, a docstring
and independent tests. Existing fixtures, helpers, assertions, parameters, markers
and hooks remain protected. Actual complete collection must match candidate
inventory exactly and preserve protected historical inventory and isolation.
Strictly additive module taxonomy/inventory may be Level 2; executable policy is Level 3.

| Level | Local Linux | Hosted platforms | Coverage | Named R | Resources |
|---|---|---|---|---|---|
| 0 | Documentation checks | Primary only | None | None | None |
| 1 | Focused regressions and contracts | Primary only | None | None | None |
| 2 | Impacted components, every new case, essential contracts, mandatory smoke | Linux impacted; Windows/macOS real smoke plus affected cases, extended for OS sensitivity | Strict changed executable lines and relevant branch outcomes | Affected gates | Reviewed affected workloads; unknown dependency means complete fallback |
| 3 | Complete required suite | Six ordinary/serial jobs across representative Linux/Windows/macOS | Unchanged full historical comparison and 90% combined floor | All seven | Affected plus conservative full fallback |
| 4 | Complete required suite and supported matrix | Sixteen ordinary/serial jobs | Unchanged full historical comparison and 90% combined floor | All seven | Complete |

Packaging is affected-only at Levels 2/3 and complete at Level 4. Actual R and
resource measurements remain local; hosted deferral is never local success.
CI-policy changes, including this PR, require Level 3 and separate validation of
candidate policy alongside protected-base acceptance.

## Targeted coverage

At Level 2, trusted source bindings select affected tests and shared essential
contracts; all new tests remain mandatory on every planned platform. Only changed
production modules are instrumented. Coverage's parser supplies executable lines
and possible branches; exact committed source and old/new line correspondence
identify changed executable lines, changed predicate origins and new branch arcs.
Every changed executable line and relevant outcome must execute. Missing lines
and arcs are listed by file. There is no lowered percentage or partial-suite
historical denominator. A test-only addition explicitly reports no changed
production code and historical coverage `not-assessed`.

Exclusions and measurement settings must match protected requirements. A new or
changed exclusion requires high-risk review under the existing policy; an
intrinsically unreachable changed branch does not receive an automatic exception.
Unknown dependencies escalate to Level 3. Unassessable coverage fails admission;
inspect the diagnostic and replan at Level 3 when reliable correspondence cannot
be established. Missing/malformed evidence never admits a change. Levels 3/4 retain
all historical covered-line/arc, exclusion, metric and changed-source protections.

To fix a Level 2 failure, read `coverage-comparison.json`, reproduce the listed
lines/arcs with the affected test selection, and add positive/negative assertions.
Use `--profile pr --plan-only` to inspect the trusted closure. A fast focused run
is feedback; repeat the targeted PR acceptance at the final source afterward.
Do not regenerate `ci/coverage-floor.json` or choose an easier threshold.
The realistic adapter and defensive-parser integration cases in
`ci/local/test_issue59.py` exercise real Git history, collection, two xdist workers,
new-test selection on all three OS profiles, and covered/uncovered admission.

## Local scheduling and evidence

`ci/local/orchestrator.py` is a bounded dependency graph, not a general workflow
engine. `conservative` preserves ordinary → serial → resources → named R ordering.
`concurrent` permits ordinary workers, one reviewed safe serial group and at most
two R gates; exclusive serial groups and host resource work have barriers.
`auto` requires a current same-policy, same-machine whole-bundle certificate
before selecting concurrency, otherwise uses the conservative fallback. Certificates
bind every operational Git object, dependency version, CPU affinity and host/kernel
fingerprint. Documentation-only source correspondence may preserve a performance
hint; its original measured SHA remains explicit and every acceptance gate runs
fresh at the current exact head. Performance evidence never becomes qualification
evidence for another commit.
`resource-isolated` falls back to authoritative exclusive host resources unless
an independently qualified VM backend exists. Hosted worker caps remain separate.
Recommended standalone local ordinary allocation is 12, bounded by actual CPUs,
with `worksteal` for full Linux suites. Concurrent allocations are measured as a
whole workload, rather than selected from isolated ordinary timing.

`ci/local-policy.json` holds reviewed serial placements with explanations and
source/fixture hashes. Unreviewed or invalidated entries are globally exclusive.
Safe serial cases have private stores/process ownership and can overlap ordinary
pytest. Host-exclusive cases require no other host pytest process; globally
exclusive cases require the entire relevant host/guest environment uncontended.
The conservative VM barrier remains until physical-core reservation, memory
pressure, measurement equivalence and contention tests qualify its relaxation.
Changing a test, fixture or any recorded production dependency invalidates placement.
Each serial group still uses one pytest process, genuine subprocesses and original deadlines.

Each group gets private temp/cache/store directories, `COVERAGE_FILE`, output and
owned subprocess tree. Linux ownership uses a subreaper and parent PID/birth
monitor; native concurrency additionally requires transient user systemd control
groups. Exit, interruption, leaks and missing ownership receipts cannot pass.
Cancellation stops admission, terminates owned children, reaps them and preserves
failed/incomplete receipts. A killed worker never inherits a peer's success.
Execution manifests record allocations, dependencies, times, waiting, outcomes and
critical path; process receipts retain CPU/RAM/swap observations and cleanup.
Independent admission rechecks barriers, allocation budgets, complete case accounting,
all seven R identities where required and every raw partition outcome.

Named R gates retain original fixture generation, persist/export phases, actual
R/jsonlite commands and assertions. Their directories and receipts are unique.
Package libraries are read-only during execution; no installation occurs in gates.
R `setwd` and synthetic environment changes occur only in the gate's process.
No similar output from another gate can replace a missing receipt. Logs, exit codes,
source identity and all artifacts are independently checked.

Coverage uses normal pytest-cov xdist combination inside each execution group.
Each invocation has a fresh attempt identity. A private copy of the unchanged
coverage configuration adds a supported static context hashing source, environment,
run, attempt and execution group. Admission verifies that configuration and rejects
foreign namespaces inside the data itself, including instrumented subprocesses.
The reporter records contributor completion/context metadata through public hooks;
it does not snapshot worker data or use pytest-cov internals. A group is sealed
only after passing and normal combination. The pinned pytest-cov startup erase race is prevented with supported `--cov-append`
inside each newly created private group directory. That directory must not exist
before execution; there is no cross-group append or reuse. Two- and twelve-worker
integration cases protect this compatibility requirement. The final dataset is built once from
explicitly enumerated authorized files, with source/run/group/attempt provenance,
checksums, branch mode and source hashes. Independent admission rebuilds that exact
union and reruns the coverage comparison. No shared append file or directory glob
is used across concurrent groups. Tests cover unique lines/arcs on separate workers,
explicitly instrumented subprocesses, missing data, corruption, stale identities,
failed stages and duplicate groups. The project's existing child instrumentation
settings remain unchanged; group isolation does not invent child coverage.

## Resources and VM decision

The reviewed source-to-resource map is independent of functional selection.
Levels 2/3 select affected workloads and all new resource cases. Unresolved shared
source, measurement or orchestration dependencies use the full partition;
Level 4 always selects all workloads. No workload dimensions, limits, assertions
or deadlines change.

`functional-coverage-comparison.json` independently assesses ordinary/serial
coverage before resources. `resource-coverage-assessment.json` records Path A
(independently sufficient) or Path B (host resource coverage still needed), exact
resource-derived lines/arcs and contributing case contexts. Resource measurement
receipts never supply functional coverage. VM adoption requires both passing
historical coverage and original measurement thresholds. Path B retains the
verified host coverage configuration unless a separate functional pass is proven.
A different source, guest, run or attempt is never an authorized coverage contributor.

`ci/local/resource_vm.py` inventories KVM/QEMU, physical siblings, available RAM,
delegated controllers and actual mounted storage before any experiment. A disk
alone does not isolate CPU/RAM. A viable experiment needs reserved physical cores
including SMT siblings, separate emulator cores, sufficient host and guest RAM,
no pressure/swap distortion, locked offline Python/R/SDK dependencies, exact guest
Git/source identity, private mutable stores and graceful owned lifecycle recovery.
Use `/mnt/VM_Fast`, `/mnt/vm` or `/mnt/ellipse_archive` only after inspecting mounts,
capacity and I/O; host paths are external configuration. Never repartition, format,
alter encryption, delete existing images or change unrelated VM settings.

Authoritative adoption additionally needs a bounded same-case host-versus-VM
comparison with host qualification concurrent: original resource thresholds and
stable equivalent measurements, startup/recovery costs included, and shorter total
critical path beyond variability. If any condition fails, retain exclusive host
resources and record a no-go decision. No threshold is adjusted for virtualization.
The runtime record below and PR evidence report the actual decision; feasibility
alone never establishes an authoritative resource VM.

## Automatic main verification and foreground priority

Every push to main runs complete Linux ordinary/serial functional tests with full
historical inventory accounting on GitHub Actions. Main runs have unique concurrency
groups and are not cancelled by newer merges. Weekly/manual Level 4 verification
retains the complete hosted matrix. Hosted failures are visible in the main commit
check and Actions run; repair the offending behavior and requalify the corrective
source. These jobs provide no actual R/resource or local historical coverage claim.

`ci/local/main_worker.py` supplies the actual optional local mechanism. It polls
the fixed public repository into a private mirror, verifies fast-forward history,
records every observed first-parent commit and waits for a configurable quiet
period (120 seconds by default). Expensive local historical coverage, seven R gates
and full resources prioritize the latest unverified tip. Earlier pending tips
become `superseded`, never `passed`. `--strict-per-commit` retains each queued source.
Statuses `pending`, `running`, `passed`, `failed`, `superseded`, `unavailable` are
separate. Interrupted attempts retain logs and require fresh execution. A failed
tip remains visible until corrective latest-source qualification passes. Retry is
explicit; a newer result is never attributed to an earlier SHA.

`ci/local/allocation.py` coordinates foreground PR acceptance, the existing native
grid and background main work. Foreground requests reserve priority before waiting
for the shared allocation. Background schedulers stop admitting stages at safe
boundaries; a five-second grace period then interrupts owned work if necessary. Background work refuses admission and interrupts its
owned attempt when foreground capacity is needed; it never suspends/resumes a timed
case as a pass. Native guest locks and ResourceLease remain additional safeguards.
Restart recovery marks abandoned attempts interrupted, preserves logs and queues
the correct source again. The worker's nonzero service status, durable queue and
journal expose failures; production activation is required for these local checks
to run automatically. No self-hosted runner or automatic post-merge R pass is assumed.

After independent review and merge, prepare a dedicated checkout/locked environment,
R/jsonlite and offline assets with adequate free capacity. Then:

```bash
.venv/bin/python -B ci/local/main_worker.py install
systemctl --user daemon-reload
.venv/bin/python -B ci/local/main_worker.py activate
systemctl --user status judgmentllm-main.timer judgmentllm-main.service
journalctl --user -u judgmentllm-main.service
.venv/bin/python -B ci/local/main_worker.py status
.venv/bin/python -B ci/local/main_worker.py recover
.venv/bin/python -B ci/local/main_worker.py retry
```

`install` writes definitions only. `activate` verifies reviewed merged CI files
before enabling the production timer. Never activate before merge. For rollback:

```bash
.venv/bin/python -B ci/local/main_worker.py rollback
systemctl --user stop judgmentllm-main.service
```

Keep the queue and failed attempts for diagnosis. A stopped running attempt is
interrupted and cannot qualify. Use `install --unit-dir /tmp/... --state-root /tmp/...`
and isolated transient services for pre-merge validation; this does not enable a
permanent timer. The worker needs a lingering user systemd session for unattended
runs, fixed public Git access and dependency preparation; test execution stays offline.

## Verification and runtime record

The refreshed base is `8eae31c5d508a6ed9c968a8aaea328bebf023001`.
It collects 8,611 cases: 8,573 ordinary, 18 serial and 20 resource, including 14
actual R pytest cases. No production source or historical test assertion changes
are needed for this CI optimization.

Retained passed evidence includes ordinary 697.10s, serial 573.43s and named R
86.07s in one scope, and resource 613.10s in another. These different campaigns
are not additive benchmarks. Failed historical attempts remain failures. The isolated exact-base assessment passed all 8,591 ordinary/serial cases without
resources and passed the unchanged historical comparison at 95.2624% combined
coverage. Ordinary took 1,248.34s, serial 1,656.08s and the complete assessment
2,919.34s. This establishes resource coverage Path A at that source; it is not a
final-head acceptance or a controlled scheduler benchmark. The bounded scheduler
comparison and VM decision are recorded externally and summarized in the final PR. Expected targeted Level 2
savings come from avoiding unrelated historical execution; concurrency/VM savings
are measured separately and never promised from isolated stage timings.

Use `ci/local/profile_bundle.py --output EXTERNAL_PATH` for the fixed bounded
ordinary/serial/resource/all-seven-R workload. It alternates conservative and
concurrent trials, records coverage/results, process CPU, memory/swap, allocations,
startup/teardown and cleanup. Only equivalent, reliable whole-run improvement beyond
observed variability and a 5% materiality margin permits automatic promotion.
A negligible or unreliable result leaves the conservative default. Profiling
receipts explicitly do not establish PR qualification. Final local/hosted evidence
must use the exact final source and protected-base admission; candidate-policy
workflow results remain a separate check.

Automatic promotion also preserves at least 16 GiB of available host memory and
rejects swap or OS pressure above the repeated controls' maximum plus observed
spread. Each test and R phase must remain within that control envelope plus 10%
(or one second for short-process startup noise). This conservative latency margin
guards timeout risk without changing any test deadline. A faster but contended
profile remains an explicit diagnostic option and does not become the default.

The committed implementation's real Gemini response scenario (synthetic source
`aa6ef9fb437140d6370c08aa4562541cf6d513b8`, implementation base `40b3dcf`)
passed independent Level 2 local admission: 1,955 selected cases, zero skips or
failures, exports/execution R gates, and complete changed-line/branch coverage.
Functional/R execution took 89.75s; quality checks also included 561 infrastructure
cases in 141.77s. Its full collection retained the historical inventory and added
two independent tests. Historical coverage was explicitly not assessed. This is
one bounded scenario, not a full-suite speedup benchmark or final-PR acceptance.

Four isolated transient systemd candidate executions validated unit syntax,
duplicate public-main polling, durable restart recovery and foreground priority.
The production timer was absent and remains unactivated before review and merge.

Local admission can be replayed without rerunning tests using
`ci/acceptance.py verify-local` with independently supplied `--base`, `--head`,
`--run-id`, the saved `--plan`, `--receipts` primary directory and external
`--output`. It reconstructs the plan, validates every case/R/ownership receipt,
rebuilds the exact coverage union and repeats the applicable coverage comparison.
Missing reports, foreign attempt namespaces and partial stages fail admission.
