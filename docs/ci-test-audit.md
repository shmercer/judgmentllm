# CI baseline and test-suite audit

The starting source is `1298bedf5e408d48ef1fd6e113677a0ece27aa24`, the merge of
Phase 5A PR #24. Its tree equals qualified `b300a3e5f7fbd59d6ca2d3761d79a5660b9c2633`.
The final Phase 5A acceptance comment records eight live successes before merge;
the earlier PR-body pending status is historical. No active qualification was
interrupted. The primary checkout remains untouched.

The pre-change audit, frozen source, test AST inventory, per-test/fixture timings,
coverage database/XML/JSON, complete uncovered lines/arcs, source hashes and
original qualification reports are retained under
`/mnt/VM_Fast/judgmentllm-ci-optimization-evidence/baseline*`.
Raw experiments remain outside the worktree. The tracked coverage floor and case
contract are executable acceptance contracts, not substitutes for raw evidence.
`ci/verification-map.json` maps original requirements to retained tests/gates.

## Baseline

| Measurement | Retained baseline |
|---|---:|
| Canonical collected cases, including parameters | 4,987 |
| Native main-suite cases, including portable CI tests | 5,043 per configuration |
| Main-suite outcomes over seven configurations | 35,269 passed; 32 platform skips; zero failures |
| Additional repeated Windows prechecks | 69 per Windows configuration |
| Production line coverage | 12,821 / 13,533 = 94.73878667% |
| Production branch coverage | 4,088 / 4,656 = 87.80068729% |
| Branch-enabled combined coverage.py metric | 92.96277970%; existing blocking floor 90% |
| Existing excluded lines | 118; configuration and exclusions preserved |
| Complete local grid | 4,249.327 seconds (70.82 minutes) |
| Required R assertions | 49 / 49; seven in every configuration |
| Retained numeric resource records | 112; sixteen per configuration |

The historical phrase “90% branch coverage” denotes the branch-enabled combined
coverage.py gate configured by this repository. It was not a 90% branch-only
ratio. This change preserves that gate and additionally blocks any overall line
or branch decrease and any loss from the exact covered sets of unchanged source.
The production source remains byte-identical. Coverage settings/omissions and
the historical fixture contracts are unchanged.

| Configuration | Main pytest seconds |
|---|---:|
| Linux 3.11 | 4,036.58 |
| Linux 3.12 | 3,571.85 |
| Linux 3.15 | 3,554.43 |
| macOS 3.11 | 1,353.84 |
| macOS 3.12 | 1,420.00 |
| Windows 3.11 | 3,856.14 |
| Windows 3.12 | 3,907.06 |

Retained hosted run [42509063214](https://github.com/shmercer/judgmentllm/actions/runs/42509063214)
passed all five routine jobs. Ubuntu 3.11: 2,416s job / 2,184s pytest;
Ubuntu 3.15: 1,405 / 1,343; macOS 3.11: 1,153 / 1,097;
macOS 3.15: 1,046 / 968; Windows 3.12: 2,745 / 2,621.
These are recorded single-run observations, not a runtime distribution.
Local Ruff took about 0.1s, Pyright 13s, build 1s, and clean install smokes 12s each.
The installed CLI R gate cost about 100s hosted; the other R gates cost 1–7s.
Pytest dominates, so cheap quality/platform installation gates remain intact.

The workstation inventory confirms sixteen physical / thirty-two logical CPUs
and 134,972,321,792 bytes RAM. Windows exposes six CPUs / 12 GiB. macOS exposes four CPUs / 16 GiB: Quickemu
rounds its configured six cores down to four, confirmed by boot and guest
telemetry. Earlier six-CPU budget metadata reserved two unused CPUs conservatively;
raw records remain unchanged. Disposable Linux limits are explicit per schedule.
Native/Python workload peaks remain independent fresh-process measurements;
sampled aggregate RSS may double-count shared pages and miss transient processes.
Host utilization includes all host activity. These measurement scopes are labeled.

## Necessity and implementation decisions

Every original function/parameter selection has an inventory record and a retained
case identity. Domain classification is conservative; low unique coverage alone
does not prove redundancy. Review used assertions, calls, fixture cost, actual
failure boundaries, historical contracts and selected deliberate faults. No
obsolete contract was demonstrated and no critical Cartesian matrix was reduced.

| Original tests/work | Independent guarantee | Decision/replacement | Equivalence and risks |
|---|---|---|---|
| Two tests each in execution/generation/grouped-lifecycle/source-preparation R modules | Independent reopened Python evidence plus real R assertions | Eight persist/export pipelines become four immutable module fixtures per worker; all eight test functions remain | Both readers consume actual outputs; complete artifact tree hashes must remain unchanged. Writable persistence R tests keep fresh fixtures. `loadfile` keeps a module on one worker; work stealing may create independent fixture copies. Both modes preserve all readers and artifact guards. |
| Six cases across four OpenAI qualification functions | Exact offline eight-entry plan, source/digest/consent authorization, private executor token, duplicate physical identity rejection | Six full plan builds become one verified immutable plan per worker; mutating/execution cases remain fresh | All eight entries remain; file hashes and canonical model bytes are checked after every reader. No SDK boundary is mocked in ordinary tests. |
| Windows durability/export/cancellation/privacy/lock prechecks repeated in main pytest | Early platform failure detection plus complete case execution | Retain early prechecks, reuse complete passing per-case receipts in main suite | 69 identical cases execute once per Windows lane. Missing/failed/duplicate receipts fail; resource cases cannot be reused; coverage lanes cannot reuse unmeasured preflights. |
| Native policy × scope × capability × strictness matrices | Selection authority, endpoint incompatibility and transport identity | Retain exhaustive interactions | Similar call paths protect different authority boundaries. No pairwise reduction. |
| Capture × layout × grouping / diagnostic malformed-output cases | Privacy, durable recovery, canonical salvage and exact evidence retention | Retain | Shared functions do not imply equivalent failure modes. |
| Resource fixtures, memory-accounting probes and tiny allocation diagnostics | Scale, queue/process ceilings, native transient peaks, 90/180-second deadlines | Retain workloads/limits; mark resource cases and bound shared-allocation concurrency | Neither sizes nor thresholds changed. Compare isolation with independently budgeted concurrency before selecting a schedule. |
| Large schema limits/cache-eviction/many-trait tests | Exact boundary, no hidden schema caps, compiler cache bounds | Retain | Scale is the property here; shrinking to a few records would remove protection. |
| Crash helpers sleeping 30/60 seconds; short polling loops | Real owned child liveness/termination and durable-state synchronization | Retain | Long sleeps are deliberately interrupted, not normally awaited; polls already use bounded state checks. No evidence of costly unconditional sleeps. |
| Fork-inheritance check and two small benchmark CLI diagnostics | Real fork ownership, fixture replacement refusal and genuine focused execution reports | Retain all cases in a serial partition | Fork must avoid execnet's threaded workers. The two CLI diagnostics failed at the added Windows worker path depth in both versions. Original inputs, assertions and deadlines remain. |
| Streaming 100-row examples, small query diagnostic | Incremental iteration, SQLite VM-step behavior | Retain | Negligible measured cost; deletion/reduction has no justified benefit. |
| Repeated Ruff/Pyright/build/install/R gates | Platform tool/install/runtime acceptance | Retain current frequency | Static reuse is plausible but current savings are small; no acceptance-policy reduction is bundled. |

The focused fixture comparison retains fourteen original cases. Two alternating
uninstrumented pairs measured 40.949→18.636s and 38.340→15.848s; waited-child CPU
21.18→11.26s and 19.92→9.81s. The coverage-enabled pair measured 49.484→27.332s,
with no lost covered production line or branch. Preparation was active during
these focused comparisons, so they establish a setup improvement, not full-grid
speedup or isolated resource variability. The external comparison records all
commands/dependencies/source IDs. Full-grid measurements establish total impact.

Targeted deliberate in-memory defects were detected by both suites: missing
source/digest/consent authorization failures (three cases), missing private-token
check, accepted duplicate physical identities, and corrupt generation IDs checked
independently in Python and R (two cases). Seven of seven cases failed under each
faulted suite, with zero setup errors. This is narrow sensitivity evidence, not
a claim of general mutation completeness. Faults never touched production files
or made provider calls.

The fresh diagnostic baseline profiler initially had a missing installed-console
PATH and coverage-context interaction with an intentional sqlite-connect fault.
Its failed attempt is retained and excluded from qualification. The remaining
895 cases were rerun successfully with corrected PATH and ordinary coverage;
the retained Phase 5A full baseline remains the passing reference. Context
instrumentation is not added to qualification or used to alter fault tests.

## Per-module baseline coverage

Exact covered line/branch sets, excluded lines, denominators and measurement settings are in `ci/coverage-floor.json`. The external
baseline JSON additionally retains every uncovered/excluded line and branch.
An optimized qualifying run writes `coverage-comparison.json`, requiring every
one of these unchanged-module covered sets to remain present. Percentages alone
cannot admit a different set with the same size.

| Module under `src/judgmentllm` | Covered lines / statements | Covered branches / branches |
|---|---:|---:|
| `__init__.py` | 15 / 15 | 0 / 0 |
| `_auth.py` | 91 / 93 | 24 / 24 |
| `_lifecycle_store.py` | 117 / 128 | 37 / 48 |
| `_verification.py` | 39 / 39 | 18 / 18 |
| `assessment_diagnostics.py` | 65 / 68 | 19 / 22 |
| `assessment_errors.py` | 45 / 47 | 30 / 32 |
| `assessment_inspection.py` | 31 / 35 | 7 / 10 |
| `auth.py` | 82 / 82 | 14 / 14 |
| `cli.py` | 116 / 117 | 22 / 24 |
| `cli_controls.py` | 226 / 249 | 50 / 60 |
| `config.py` | 328 / 338 | 118 / 126 |
| `evidence/__init__.py` | 6 / 6 | 0 / 0 |
| `evidence/_base.py` | 80 / 80 | 6 / 6 |
| `evidence/assessment.py` | 55 / 60 | 21 / 26 |
| `evidence/order.py` | 185 / 186 | 89 / 90 |
| `evidence/reasoning.py` | 80 / 80 | 20 / 20 |
| `evidence/scope.py` | 58 / 58 | 18 / 18 |
| `evidence/usage.py` | 194 / 196 | 60 / 62 |
| `execution/__init__.py` | 2 / 2 | 0 / 0 |
| `execution/cancellation.py` | 42 / 42 | 26 / 26 |
| `execution/core.py` | 181 / 189 | 53 / 60 |
| `execution/durable_evidence.py` | 164 / 192 | 59 / 86 |
| `execution/evidence.py` | 35 / 42 | 9 / 14 |
| `execution/exports.py` | 111 / 120 | 45 / 54 |
| `execution/processing.py` | 156 / 159 | 67 / 70 |
| `execution/retry.py` | 190 / 203 | 66 / 78 |
| `execution/session.py` | 434 / 465 | 160 / 190 |
| `generation.py` | 33 / 33 | 6 / 6 |
| `io.py` | 224 / 224 | 98 / 98 |
| `lifecycle.py` | 286 / 309 | 78 / 92 |
| `lifecycle_models.py` | 137 / 137 | 2 / 2 |
| `models.py` | 179 / 179 | 66 / 66 |
| `native_prompts.py` | 101 / 107 | 28 / 34 |
| `native_schema.py` | 161 / 162 | 29 / 30 |
| `native_schema_v2.py` | 178 / 190 | 58 / 70 |
| `native_selection.py` | 196 / 199 | 55 / 58 |
| `native_selection_v2.py` | 196 / 211 | 57 / 68 |
| `openai_diagnostics.py` | 117 / 123 | 20 / 22 |
| `output_commitments.py` | 114 / 121 | 31 / 36 |
| `output_contracts.py` | 177 / 179 | 64 / 66 |
| `persistence/__init__.py` | 0 / 0 | 0 / 0 |
| `persistence/artifacts.py` | 228 / 238 | 69 / 74 |
| `persistence/evidence.py` | 406 / 451 | 149 / 192 |
| `persistence/generation.py` | 57 / 57 | 12 / 12 |
| `persistence/hashing.py` | 38 / 38 | 18 / 18 |
| `persistence/models.py` | 168 / 168 | 18 / 18 |
| `persistence/ownership.py` | 134 / 160 | 25 / 32 |
| `persistence/receipts.py` | 99 / 103 | 14 / 14 |
| `persistence/repository.py` | 601 / 614 | 179 / 196 |
| `persistence/schema.py` | 75 / 75 | 16 / 16 |
| `persistence/sources.py` | 74 / 75 | 11 / 12 |
| `persistence/transport_receipts.py` | 65 / 75 | 12 / 16 |
| `planning.py` | 252 / 253 | 128 / 130 |
| `preparation/__init__.py` | 305 / 330 | 111 / 134 |
| `preparation/capture.py` | 210 / 217 | 75 / 82 |
| `preparation/freezing.py` | 97 / 103 | 22 / 26 |
| `preparation/index.py` | 178 / 181 | 54 / 54 |
| `preparation/manifest.py` | 238 / 256 | 38 / 52 |
| `preparation/native.py` | 111 / 120 | 33 / 42 |
| `preparation/records.py` | 30 / 31 | 7 / 8 |
| `preparation/session.py` | 350 / 379 | 123 / 154 |
| `preview.py` | 62 / 62 | 28 / 28 |
| `production_prompts.py` | 30 / 31 | 1 / 2 |
| `production_registry.py` | 58 / 59 | 10 / 10 |
| `production_transport.py` | 331 / 348 | 95 / 112 |
| `prompts.py` | 317 / 320 | 121 / 124 |
| `providers/__init__.py` | 4 / 4 | 0 / 0 |
| `providers/_assessment_schema.py` | 120 / 120 | 56 / 56 |
| `providers/anthropic_schema.py` | 31 / 32 | 11 / 12 |
| `providers/evidence.py` | 128 / 133 | 43 / 48 |
| `providers/google_schema.py` | 156 / 169 | 57 / 70 |
| `providers/ledger.py` | 118 / 121 | 19 / 20 |
| `providers/mock.py` | 425 / 439 | 178 / 192 |
| `providers/ollama_schema.py` | 15 / 16 | 1 / 2 |
| `providers/openai.py` | 279 / 306 | 70 / 88 |
| `providers/openai_cli.py` | 33 / 35 | 4 / 6 |
| `providers/openai_response.py` | 228 / 250 | 134 / 152 |
| `providers/openai_schema.py` | 196 / 202 | 88 / 94 |
| `providers/protocol.py` | 95 / 101 | 10 / 16 |
| `providers/together_schema.py` | 16 / 17 | 1 / 2 |
| `qualification.py` | 425 / 494 | 141 / 178 |
| `r_exports.py` | 68 / 68 | 22 / 22 |
| `request_rendering.py` | 24 / 24 | 2 / 2 |
| `results.py` | 205 / 205 | 80 / 80 |
| `transport.py` | 464 / 523 | 153 / 200 |
| `transport_dispatch.py` | 50 / 65 | 19 / 32 |

## Module inventory and retention

The counts below are function definitions and parametrized functions, not collected
cases. Exact parameter IDs remain in the executable case contract. Category names
indicate the primary protection domain; overlapping security/recovery guarantees
remain required. All modules are retained; only the documented setup/execution
consolidations above change their work.

| Module | Functions | Parametrized functions | Primary audit domains |
|---|---:|---:|---|
| `ci/local/test_gates.py` | 18 | 8 | ci-infrastructure |
| `ci/local/test_grid.py` | 14 | 6 | ci-infrastructure |
| `ci/local/test_linux.py` | 2 | 1 | ci-infrastructure |
| `ci/local/test_source.py` | 3 | 0 | ci-infrastructure |
| `ci/local/test_workflow.py` | 5 | 2 | ci-infrastructure |
| `tests/test_assessment_capture.py` | 4 | 2 | behavioral-contract, security-privacy |
| `tests/test_assessment_diagnostics.py` | 7 | 6 | behavioral-contract |
| `tests/test_auth.py` | 17 | 6 | security-privacy |
| `tests/test_auth_cli.py` | 12 | 6 | security-privacy |
| `tests/test_auth_cli_processes.py` | 2 | 1 | security-privacy |
| `tests/test_auth_privacy.py` | 7 | 1 | security-privacy |
| `tests/test_cli.py` | 8 | 2 | behavioral-contract |
| `tests/test_cli_lifecycle.py` | 15 | 4 | behavioral-contract, persistence-recovery, resource-scale, security-privacy |
| `tests/test_cli_process_safety.py` | 7 | 1 | security-privacy |
| `tests/test_cli_processes.py` | 6 | 2 | behavioral-contract, persistence-recovery, security-privacy |
| `tests/test_compatibility_qualification.py` | 5 | 2 | historical-compatibility |
| `tests/test_config.py` | 12 | 2 | behavioral-contract, security-privacy |
| `tests/test_durable_evidence_lifecycle.py` | 12 | 11 | persistence-recovery, security-privacy |
| `tests/test_durable_native_preparation.py` | 16 | 10 | persistence-recovery, security-privacy |
| `tests/test_evidence_failure_retry.py` | 4 | 4 | persistence-recovery, security-privacy |
| `tests/test_evidence_offline.py` | 2 | 0 | behavioral-contract |
| `tests/test_evidence_order.py` | 20 | 11 | behavioral-contract, security-privacy |
| `tests/test_evidence_persistence.py` | 20 | 8 | persistence-recovery, security-privacy |
| `tests/test_evidence_reasoning_scope.py` | 15 | 6 | behavioral-contract, security-privacy |
| `tests/test_evidence_receipt_omission.py` | 4 | 4 | behavioral-contract |
| `tests/test_evidence_recovery_integrity.py` | 1 | 1 | security-privacy |
| `tests/test_evidence_usage.py` | 22 | 8 | behavioral-contract, security-privacy |
| `tests/test_evidence_validation_cache.py` | 7 | 2 | security-privacy |
| `tests/test_execution.py` | 12 | 7 | behavioral-contract, persistence-recovery, security-privacy |
| `tests/test_execution_automatic_retry.py` | 6 | 3 | persistence-recovery, security-privacy |
| `tests/test_execution_cancellation.py` | 5 | 1 | persistence-recovery |
| `tests/test_execution_cancellation_processes.py` | 5 | 4 | persistence-recovery |
| `tests/test_execution_exports.py` | 10 | 6 | behavioral-contract, security-privacy |
| `tests/test_execution_performance_integrity.py` | 18 | 11 | security-privacy |
| `tests/test_execution_persistence.py` | 14 | 5 | persistence-recovery |
| `tests/test_execution_privacy.py` | 7 | 2 | security-privacy |
| `tests/test_execution_processes.py` | 6 | 4 | behavioral-contract, persistence-recovery, security-privacy |
| `tests/test_execution_r.py` | 2 | 0 | R-interoperability |
| `tests/test_execution_resources.py` | 2 | 0 | resource-scale |
| `tests/test_execution_retry.py` | 9 | 2 | persistence-recovery, security-privacy |
| `tests/test_execution_safety.py` | 7 | 6 | security-privacy |
| `tests/test_export_durability.py` | 2 | 2 | behavioral-contract, persistence-recovery |
| `tests/test_export_validation.py` | 9 | 3 | security-privacy |
| `tests/test_generation.py` | 12 | 6 | behavioral-contract, security-privacy |
| `tests/test_generation_commitment.py` | 9 | 3 | behavioral-contract, historical-compatibility |
| `tests/test_generation_r.py` | 2 | 0 | R-interoperability |
| `tests/test_google_schema.py` | 8 | 5 | behavioral-contract, security-privacy |
| `tests/test_grouped_execution.py` | 9 | 7 | behavioral-contract, persistence-recovery, security-privacy |
| `tests/test_grouped_lifecycle_r.py` | 2 | 0 | R-interoperability |
| `tests/test_grouped_resources.py` | 1 | 0 | resource-scale |
| `tests/test_initial_usage_commitment.py` | 3 | 1 | behavioral-contract |
| `tests/test_io.py` | 14 | 4 | behavioral-contract |
| `tests/test_mock_provider.py` | 19 | 4 | behavioral-contract, persistence-recovery, security-privacy |
| `tests/test_models.py` | 20 | 11 | behavioral-contract |
| `tests/test_native_memory_accounting.py` | 3 | 1 | resource-scale |
| `tests/test_native_other_providers.py` | 6 | 4 | behavioral-contract |
| `tests/test_native_other_sdk.py` | 1 | 1 | behavioral-contract |
| `tests/test_native_preparation_reuse.py` | 9 | 5 | behavioral-contract, security-privacy |
| `tests/test_native_preseal_memory.py` | 1 | 0 | resource-scale |
| `tests/test_native_preseal_validation.py` | 12 | 8 | security-privacy |
| `tests/test_native_retry_integrity.py` | 3 | 3 | security-privacy |
| `tests/test_native_schema.py` | 31 | 17 | behavioral-contract, historical-compatibility, security-privacy |
| `tests/test_native_schema_privacy.py` | 1 | 0 | security-privacy |
| `tests/test_native_selection.py` | 29 | 14 | behavioral-contract, historical-compatibility, security-privacy |
| `tests/test_native_transport_integration.py` | 19 | 18 | behavioral-contract, persistence-recovery, security-privacy |
| `tests/test_native_transport_integrity.py` | 6 | 6 | security-privacy |
| `tests/test_native_transport_processes.py` | 2 | 1 | persistence-recovery, security-privacy |
| `tests/test_native_transport_r.py` | 1 | 0 | R-interoperability |
| `tests/test_native_transport_rendering.py` | 5 | 4 | behavioral-contract |
| `tests/test_native_v2.py` | 25 | 16 | behavioral-contract, security-privacy |
| `tests/test_native_v2_compatibility.py` | 3 | 0 | historical-compatibility |
| `tests/test_native_v2_privacy.py` | 1 | 0 | security-privacy |
| `tests/test_observation_lookup_benchmark.py` | 2 | 1 | resource-scale |
| `tests/test_openai_cancellation.py` | 6 | 6 | persistence-recovery |
| `tests/test_openai_capabilities.py` | 5 | 2 | behavioral-contract, security-privacy |
| `tests/test_openai_diagnostics.py` | 6 | 2 | behavioral-contract, security-privacy |
| `tests/test_openai_invalid_utf8.py` | 1 | 1 | behavioral-contract |
| `tests/test_openai_lifecycle.py` | 9 | 2 | behavioral-contract, persistence-recovery, security-privacy |
| `tests/test_openai_qualification.py` | 27 | 10 | behavioral-contract, historical-compatibility, persistence-recovery, security-privacy |
| `tests/test_openai_resources.py` | 1 | 1 | resource-scale |
| `tests/test_openai_responses_parsing.py` | 7 | 5 | behavioral-contract, persistence-recovery |
| `tests/test_openai_sdk_adapter.py` | 11 | 3 | behavioral-contract |
| `tests/test_openai_uncertain_usage.py` | 3 | 1 | behavioral-contract, persistence-recovery |
| `tests/test_openai_validation.py` | 3 | 1 | security-privacy |
| `tests/test_output_contract_compatibility.py` | 10 | 5 | historical-compatibility |
| `tests/test_output_contract_interpretation.py` | 18 | 14 | behavioral-contract, historical-compatibility |
| `tests/test_output_contracts.py` | 30 | 21 | behavioral-contract, historical-compatibility, persistence-recovery, security-privacy |
| `tests/test_package.py` | 7 | 1 | behavioral-contract |
| `tests/test_persistence_artifacts.py` | 26 | 2 | persistence-recovery |
| `tests/test_persistence_hashing.py` | 10 | 3 | persistence-recovery |
| `tests/test_persistence_models.py` | 17 | 9 | persistence-recovery |
| `tests/test_persistence_ownership.py` | 14 | 0 | persistence-recovery |
| `tests/test_persistence_r.py` | 2 | 0 | persistence-recovery |
| `tests/test_persistence_repository.py` | 29 | 3 | persistence-recovery, security-privacy |
| `tests/test_persistence_schema.py` | 31 | 9 | persistence-recovery |
| `tests/test_persistence_sources.py` | 5 | 1 | persistence-recovery |
| `tests/test_planning.py` | 17 | 7 | behavioral-contract |
| `tests/test_preparation_attempt_rendering.py` | 5 | 3 | behavioral-contract, persistence-recovery, security-privacy |
| `tests/test_preparation_index.py` | 12 | 5 | behavioral-contract |
| `tests/test_preparation_integrity.py` | 23 | 12 | security-privacy |
| `tests/test_preparation_manifest.py` | 5 | 2 | behavioral-contract, security-privacy |
| `tests/test_preparation_recovery.py` | 10 | 5 | persistence-recovery, security-privacy |
| `tests/test_preparation_rendering.py` | 10 | 6 | behavioral-contract, security-privacy |
| `tests/test_preparation_resources.py` | 5 | 0 | persistence-recovery, resource-scale |
| `tests/test_preview.py` | 6 | 2 | behavioral-contract |
| `tests/test_production_transport.py` | 19 | 6 | behavioral-contract, security-privacy |
| `tests/test_prompts.py` | 21 | 5 | behavioral-contract, historical-compatibility, security-privacy |
| `tests/test_provider_evidence_boundary.py` | 10 | 5 | behavioral-contract, security-privacy |
| `tests/test_public_lifecycle.py` | 14 | 3 | behavioral-contract, persistence-recovery, security-privacy |
| `tests/test_public_lifecycle_integrity.py` | 7 | 1 | security-privacy |
| `tests/test_r_exports.py` | 13 | 5 | R-interoperability |
| `tests/test_readonly_diagnostic_repository.py` | 9 | 4 | security-privacy |
| `tests/test_readonly_inspection.py` | 12 | 5 | security-privacy |
| `tests/test_results.py` | 28 | 12 | behavioral-contract, security-privacy |
| `tests/test_scale_benchmarks.py` | 11 | 3 | historical-compatibility, resource-scale |
| `tests/test_scale_diagnostics.py` | 3 | 1 | resource-scale, security-privacy |
| `tests/test_scale_storage.py` | 3 | 0 | resource-scale |
| `tests/test_source_preparation_r.py` | 2 | 0 | R-interoperability |
| `tests/test_source_signatures.py` | 12 | 3 | behavioral-contract, historical-compatibility |
| `tests/test_transport_rendering.py` | 6 | 5 | behavioral-contract, persistence-recovery |
| `tests/test_v2_durable_contracts.py` | 14 | 10 | persistence-recovery |
| `tests/test_v2_public_lifecycle.py` | 6 | 3 | behavioral-contract, persistence-recovery, security-privacy |
| `tests/test_v2_rendering.py` | 22 | 15 | behavioral-contract, resource-scale, security-privacy |
| `tests/test_version_compatibility.py` | 6 | 2 | historical-compatibility, security-privacy |
