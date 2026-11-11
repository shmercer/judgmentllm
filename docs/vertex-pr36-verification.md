# Phase 5D3 PR #36 verification and temporary Linux substitution

This record defines the final-source acceptance campaign for
[PR #36](https://github.com/shmercer/judgmentllm/pull/36), resumed after
[CI repair PR #37](https://github.com/shmercer/judgmentllm/pull/37) merged into main
at `cf694805ceeeef69ccd75c27ada168e9eafcfa93`. The original promotion commit
`22f2b7cffb5bdd2a6a67ad94438d6c9de3f1b23f` remains an ancestor. The PR stays
open and unmerged for independent review.

## Exact source and results

The PR description identifies the final literal commit and individual CI job
links. Its authoritative machine-readable results are retained outside Git at:

`/mnt/VM_Fast/judgmentllm-ci/qualification/<literal-final-PR-head>/phase5d3-resumption-20261111/acceptance-report.json`

The adjacent evidence inventory records SHA-256 digests of retained files. Keeping
results outside Git avoids a result-only commit changing the source being tested.
A later source, test, documentation or harness change requires fresh final-head
acceptance. The report distinguishes completed checks, failures and deferrals;
this document alone is not evidence that a gate passed.

Retain the protected-base automatic plan unchanged. If it defers resources or
packaging, a separately identified supplemental local plan adds the checks requested
by the operator, preserving risk3, coverage, all seven R gates, complete test
selection, thresholds and the representative hosted matrix. Its derivation and
hash must be independently checked against the original automatic plan. It must
never replace the hosted automatic plan or reduce a requirement.

## Three independent required results

1. Complete local Ubuntu24.04/Python3.11 acceptance from a clean Git checkout,
   using the immutable CI policy from merged main. Run with six CPUs, 16GiB,
   disabled networking, no host credential mounts, the established Linux lock,
   fresh result paths and graceful cleanup. Verify source and protected-policy
   bytes before and after. Include the full ordinary/serial/resource suite,
   line/branch coverage, the90% combined floor, unchanged historical/change-aware
   reconciliation, CI selftests, security/privacy/recovery regressions, all
   original resource bounds/deadlines, wheel/sdist builds, strict metadata and
   isolated installation smokes. Dependency preparation is separate from offline
   acceptance and supplies no provider authority.
2. Passing GitHub `windows-3.12` full required job at that exact candidate commit.
3. Passing GitHub `macos-3.11` full required job at that exact candidate commit.

All seven strict R/jsonlite gates must pass locally: exports, persistence,
generation, source-preparation, execution, grouped-lifecycle and installed-CLI
lifecycle. Missing Rscript/jsonlite fails. The execution gate includes both
promoted synthetic Vertex modes and original qualification-source/report joins.

Reconstruct the automatic plan with protected-base code and compare the hosted
plan artifact. Verify GitHub event/base/candidate identities, each job's completed
steps, receipt source/plan/run identities, artifact ZIP digests, full collection
and every ordinary/serial/resource case outcome. Reconcile coverage and required
steps independently of runner summaries. Hosted nonprimary coverage remains
disabled; existing policy-allowed skips and deferred resource/R measurements
stay explicit. They do not replace required local measurements or strict R gates.

## Failure evidence and temporary exception

The original [PR #36 run](https://github.com/shmercer/judgmentllm/actions/runs/43046300016)
remains unsuccessful. Its Windows/macOS assertion failures and cancelled Linux
R setup are preserved; none become passing evidence for the revised head.
The two failing parameterizations were ADC/API-key instances of the same stale
empty-registry assertion. They now require the two reviewed modes, an unchanged
registry and no profile for the synthetic candidate route. All existing forged
context, provider-integrity, no-client/no-forwarding and cross-provider rejection
checks remain. Exact-profile positives and unknown/unsupported-scope negatives
continue in the production, transport and historical-compatibility suites.

PR #37's [hosted Linux R setup](https://github.com/shmercer/judgmentllm/actions/runs/43055611527/job/130644872060)
also failed DNS on Ubuntu24.04. The image pin is not a complete DNS fix. Under
the operator's temporary exception, preserve revised-head hosted Linux and
aggregate failures as failed infrastructure checks, with unrun tests/R gates
explicitly unpassed. Retain all unsuccessful local attempts separately. Do not
waive tests, publish a synthetic aggregate success or edit the temporary ruleset.

After Azure repository DNS/setup recovers, the operator must obtain complete
successful exact-head protected-base Level3 hosted acceptance and restore required
`CI acceptance` from GitHub Actions integration15368 in ruleset26950502, replacing
the temporary Windows/macOS checks while preserving every other protection.
Retain both the restored configuration and earlier failed infrastructure evidence.

The first revised head `3deb20f647e3bbce40ffb7786d397ef8214dbd5e` has a separate
[hosted run](https://github.com/shmercer/judgmentllm/actions/runs/43077701605):
Linux and macOS passed, Windows failed, and the aggregate failed. Local Linux
and hosted Windows both exceeded the execution-R pytest fixture's unchanged
60-second persist deadline under parallel execution. These are retained fixture
setup failures, distinct from the earlier Azure DNS failures. The isolated
unchanged fixture subsequently passed both strict tests with 16.03-second setup;
that development result does not qualify a later head. Its full local failed
attempt and digest-verified hosted artifacts remain under the external ledger
for that earlier commit. The serial scheduling repair requires fresh acceptance.

## Unchanged live authority

Following the first revised-head run, the operator also authorized local CI
setup repairs. Local planning avoids bytecode writes; installation acceptance
passes `--offline`; and the existing execution-to-R pytest module uses the serial
partition with its original 60-second deadline and all assertions. These CI/test
changes require a new committed head and a fresh complete Level3 campaign. The
earlier fixture-timeout and package-cache failures remain unsuccessful evidence.
No runtime provider module, profile, historical audit or qualification source
changes as part of these repairs.

The original [qualification acceptance](vertex-live-qualification-acceptance.md)
and [independent evidence audit](vertex-evidence-verification.json) retain their
original evidence-producing ADC and API-key sources, route/profile/report hashes
and limitations. The registry and all production implementation bytes remain
unchanged from the original promotion commit. Check their Git blob identities
before and after this campaign. Private historical evidence is not needed or
accessed for these synthetic checks.

Only the existing exact global/v1beta1/SDK2.34.0/gemini-3.1-flash-lite profiles
and their separate route/mode commitments authorize one-trait scores and
winner-only forced choice through `response_json_schema`. No live provider calls,
credential/keyring access, additional spending, evidence rewrites, capability
expansion, package publication, merge or ruleset changes belong to this campaign.
