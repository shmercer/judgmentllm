# Issue #54 implementation and verification record

Issue: [complete CI optimization](https://github.com/shmercer/judgmentllm/issues/54).
Base: `07a6d73410395ebe21bc67bc5194e14ca5917c0c`.
Branch: `feat/54-ci-tiered-verification`. The final PR and external receipts
identify the literal qualified head; changing source invalidates prior receipts.
The final source-bound outcomes, run links and runtime ledger are maintained in
[PR #55](https://github.com/shmercer/judgmentllm/pull/55). This tracked record also
preserves the earlier unsuccessful attempts; they do not qualify its current head.

## A — inventory and retained timing evidence

Full offline collection at the base contains **8,549 cases, 1,822 functions,
189 modules**, with all parameter IDs preserved. The generated compact inventory
adds no test executions or assertion replacements. Existing replacement/skip
contracts, genuine historical fixture bytes and coverage floor are unchanged.
The identifier, scalar-subclass and runtime-snapshot regressions add three
cases/functions: **8,552 cases and 1,825 functions**, with every original case
retained.
The module taxonomy records domain, cost/isolation and platform rationale;
function selectors cover heterogeneous modules. Unclassified additions are
mandatory on every lane and unknown dependencies strengthen platform depth.
Historical case removal/rename and marker drift fail before execution.

Retained [PR2-C timing evidence](pr2c-test-efficiency-audit.md) identifies Vertex
qualification execution (1,599.92 aggregate worker seconds), native study matrix
(1,036.30, including 533.40 setup), Gemini execution (571.12), study workflow
(514.20) and failures (410.63). These are overlapping worker times, not elapsed
module times. The accepted local ordinary/serial reference at 12 workers took
697.10/573.43 seconds; complete suite 1,278.57 seconds, with resource applicability
reported separately. No historical performance campaign was repeated.

The successful [hosted reference](https://github.com/shmercer/judgmentllm/actions/runs/43532744276)
took about 41.5 minutes overall, with Linux 18.5, macOS 26.9 and Windows 39.6
minutes. These are observations on different hosted machines, not distributions.
Its actual receipts collect 8,482 cases and execute 8,451 hosted cases per lane,
including allowed platform skips. The current base adds 67 cases and removes
zero. Comparable full-suite observations must disclose that scope difference;
shorter selected-platform execution is not an equal-case speed benchmark.
The later [pre-change PR #53 run](https://github.com/shmercer/judgmentllm/actions/runs/43546038698)
failed Windows and aggregate acceptance; its failure remains failed evidence.

## B — selection and trust

| Level | Local primary | Hosted Linux | Hosted Windows/macOS |
|---|---|---|---|
| 0 | Documentation | Documentation | None |
| 1 | Focused preliminary contracts | Focused contracts | None |
| 2 | Complete suite and historical coverage, affected R/resources | Impacted plus essential contracts | Real smoke plus affected components; extended if OS-sensitive |
| 3 | Complete suite/history, seven R gates, affected resources/packages | Complete functional suite | Extended OS-sensitive/high-consequence and affected cases |
| 4 | Existing comprehensive requirements | Complete supported versions | Complete supported versions |

The final registry selects **108** hosted smoke cases or **2,422** extended cases
before change-specific impact additions. Both explicitly defer **34** local-only
R/resource cases. Full hosted selection retains **8,518** cases. All counts are
derived from complete collection and exact selectors, not acceptance thresholds.
The final plan may add whole changed modules and new cases. Each lane preserves
ordinary/serial/resource partitioning and the genuine cold SDK correctness probe.

Plans bind impact closures, reasons, per-lane depth/platform/scheduling and policy
digest. Every lane validates the complete universe first. Selection manifests
and executed/deferred/not-required accounting include exact node IDs, platform
relevance and reasons. Protected aggregation recomputes selection and validates
raw phase receipts, source/run/lock/platform/interpreter, uniqueness, hashes,
counts, omissions and skip/xfail behavior. Candidate code never runs with the
status-writing token. A manual candidate-policy status cannot replace the
protected-base status. Full eight-lane Level 4 availability remains intact.

## C — execution changes

- Full authoritative collection happens once per lane; partition workers import
  only selected modules. This removes unnecessary repeated broad collection in
  smoke/extended lanes without changing their case receipts.
- Selected profiles use file affinity, switching to work stealing when affected
  modules are classified heavy. Windows subsets keep two workers. Complete
  suites retain work stealing/four hosted workers, avoiding a long single-file tail.
  The 12-worker local choice remains explicit, supported by retained evidence.
- Six invalid study-limit cases use typed synthetic consent and absent paths and
  assert admission/credential code is unreachable, avoiding repeated 42-case
  proof sealing/store copies. Positive execution still uses fresh sealed copies,
  actual pinned SDK HTTP, ledger, child processes and original assertions.
- Shared study artifacts/models gain final byte/model guards. Mutable stores,
  HTTP mocks, clocks and campaign ledgers remain separate. Directory-bound
  Vertex/Gemini plans are deliberately rebuilt; unsafe sharing was rejected.
- Quality runs once; build/cache/evidence outputs are external. Genuine
  distribution reconstruction and independent installs retain their assertions.
- The two bounded study subprocess cases run serially with their original
  120-second deadline. This removes xdist CPU contention from process-death and
  fresh-process recovery verification; both remain actual subprocess tests and
  mandatory extended-platform cases.

No coverage baseline, supported version, resource ceiling, workload size,
deadline, historical fixture or skip allowance is changed. Production adjustments
are limited to the equivalent identifier scan and scalar snapshot branch ordering
described below; public validation and serialized outputs retain their behavior.

The six invalid-limit cases took **49.02 seconds before / 1.30 seconds after**
in bounded single-process runs (97.3% lower elapsed time). The original test and
fixture source was retained externally; both runs passed all six cases. This is
a focused setup comparison, not a full-suite or hosted speedup estimate.
The pre-change Windows failure was the Linux-only launcher's test forcing a
POSIX exec path under Windows. The test now retains genuine CLI execution via a
portable Windows child and asserts the actual JSON result as well as exit status;
POSIX still uses real process replacement. No launcher production behavior changes.

A focused profile of one real study-limit case records 1.98 billion function
calls in 438.14 instrumented seconds. Identifier validation accounts for 84.58
cumulative seconds, including 255 million Python character-generator iterations.
The validator now scans the same ASCII control range with a compiled character
class, retaining the whitespace checks and exact error text. It adds no cache or
dependency. Comparison with the committed original on 2,228,224 strings spanning
every Unicode code point found identical acceptance, values and errors. The new
regression covers every ASCII boundary, Unicode whitespace and leading-zero IDs;
it is mandatory platform smoke. For the same 500,000 synthetic valid IDs, the
predicate took 0.9133 seconds before and 0.1651 after. This narrow production
performance correction addresses the measured hosted-budget pressure without
reducing validation; its microbenchmark is not an end-to-end speedup claim.

The same profile records 39 million export snapshot calls, 33 million canonical
snapshot calls and 23 million output-contract snapshot calls. Exact built-in
scalars now take their existing return path before model/container type checks.
Canonical floats still require finiteness; subclasses retain their previous
path, and every model is still deeply revalidated. Comparison against committed
source on 1,531 deterministic scalar/container/model/mutation/root/error inputs
per boundary found identical values and failures. Five bounded measurements of
the same nested workload show median times of 0.0938/0.0702 seconds for export
snapshots, 0.0967/0.0753 for contract snapshots and 0.1656/0.1436 for canonical
bytes (before/after). These are isolated observations, not full-suite estimates.
A mandatory smoke regression rejects scalar subclasses at the canonical boundary.

## D — qualification and contributor scaffolding

`AGENTS.md` and the canonical [testing guide](testing-guide.md) cover real examples,
metadata maintenance, fast/PR/comprehensive/release commands, local R/coverage,
platform rationale, troubleshooting and adding tests. Executable selftests cover
new modules/parameters, deletion/rename, duplicate/stale metadata, isolation drift,
lost mandatory smoke, all levels/platform reach, missing/wrong-scope/tampered
receipts, ordinary/serial failures and guide/interface consistency.

Development validation passed 417 CI-policy selftests (46.73 seconds), Ruff,
formatting, strict Pyright, offline links/examples and unchanged-inventory
validation. Three isolated in-memory production mutations were detected by
independent assertions: accepting an invalid schema, claiming retention of
hash-only private evidence, and issuing twelve physical synthetic acceptances
where six were required. They produced test-call failures, with no setup errors.
Five real negative-fixture cases additionally verified corrupt recovery,
owned-child cancellation and unexpected Windows-lock contents. Seven selector/
receipt fault selftests ensure those probes remain selected and failed calls or
missing platform evidence cannot be reported as success. Mutants and raw logs
are external and never enter normal execution or tracked production code.

Final receipt review added explicit rejection of unevaluated outcome/phase tokens,
non-boolean xfail fields, boolean exit codes, duplicate JSON keys, changed hosted
scheduling, collection-time execution and boolean/unknown taxonomy controls.
The preliminary `7b421b377b0cc00e0a0edc373a84fa977c826b0b` local campaign was
gracefully interrupted after 8,452 ordinary passes (596.79 seconds) to apply these
controls before expensive downstream work. It is **not qualified**: no complete
coverage, resource or named R-gate pass is credited to that attempt. Final
qualification uses a new clean commit and independently source-bound receipts.

The first [candidate-policy dispatch](https://github.com/shmercer/judgmentllm/actions/runs/43560873475)
reached artifact upload, then failed because a recursive quality upload included
the deliberately invalid `drive:stream` filename from an infrastructure test.
Uploads now enumerate only plan/receipt JSON and diagnostic logs, preserving
the negative fixture and keeping scratch trees external. That failed dispatch
and the interrupted source-3928301 local campaign remain unqualified; final
source-bound checks are rerun after the upload correction.

Retained real Windows receipts also expose Git's CRLF checkout form of `uv.lock`.
Plans derive that exact alternative byte hash from the committed lock, while
receipts retain their actual bytes' hash. Only Windows admits LF or that committed
CRLF form; changed content and foreign hashes still fail. This avoids treating a
valid checkout normalization as a dependency substitution.

The source-`37c7fae` [candidate run](https://github.com/shmercer/judgmentllm/actions/runs/43562912436)
passed macOS (2,444 cases, zero skips, 1,458.60 suite seconds), but Linux and
Windows reached the unchanged 50-minute job limit. Linux's ordinary receipt
records 8,503 passes and a genuine 120-second child timeout under parallel load;
Windows remained in the heavy file-affinity tail. Actual macOS reports attribute
1,012.60 of 1,133.71 ordinary seconds to the changed study-limit module. These
observations justify serial child isolation and work stealing for heavy impact;
they do not qualify that failed run. Its local campaign was interrupted after
8,513 ordinary passes (1,588.29 seconds) and twelve serial passes to apply the
correction. No complete coverage/resource/named-R result is credited to it.

Source `59153f7` subsequently passed complete local qualification: 8,550 cases,
95.2297% combined historical/change-aware coverage, all seven named strict R
gates, twenty resource cases and isolated package checks. Its candidate Level 3
[run](https://github.com/shmercer/judgmentllm/actions/runs/43569816010) passed all
three profiles and strict aggregation. Its protected-base full Windows lane
timed out twice at the unchanged fifty-minute job limit; other required lanes
passed. The actual eight-lane Level 4
[run](https://github.com/shmercer/judgmentllm/actions/runs/43573597532) passed
Linux 3.11/3.12 and macOS 3.11, while five jobs reached that limit. Python 3.15
also exposed four real Google equivalence failures: a built-in unhashable-set
TypeError gained contextual wording, while all other response fields matched.
The test now normalizes only those exact list/dict runtime messages and preserves
the accepted historical fixture bytes. All 321 equivalence cases pass on both
Python 3.11 and 3.15; 667 focused correction cases pass on Python 3.15. These
corrections require fresh exact-head qualification; the earlier source's local
pass and candidate pass cannot qualify the final source. Full Windows budget
pressure remains unresolved until fresh actual receipts establish otherwise.

Raw evidence is retained outside Git under `/tmp/judgmentllm-issue54/`, with
source/run/plan identities. The final PR reports actual checks and runtime by
scope, deliberate fault detection, hosted bootstrap and Level 4 evidence. A
focused success or old-source result never qualifies a new head. Required local
and hosted outcomes will be recorded separately; absent evidence remains
unqualified. This record must be read with the final PR's exact-head ledger.

Independent review should inspect conservative dependency closures and platform
selectors, trusted aggregation and bootstrap separation, fixture equivalence,
coverage/R/resource evidence and whether the contributor guide alone suffices
to add a test correctly. Publication, merging and live providers are excluded.
