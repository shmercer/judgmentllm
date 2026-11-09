# Phase 5B implementation and coverage review

The starting remote main is `27101b232932d59f6a89e96db651955f4dae64e8`
(post PR #25). Work uses an independent clone and dedicated branch under
`/mnt/VM_Fast/judgmentllm-m3p5b`; the primary checkout and historical evidence
are unchanged. All qualification evidence is external under
`/mnt/VM_Fast/judgmentllm-m3p5b-evidence`. The open PR's verification report
records the final commit, local/hosted acceptance and immutable unexecuted plan.
This source document records design and coverage review, not final acceptance.

## Contract audit

The source of truth is the neutral RunSpecV3/output-contract2/renderer3 model,
provider-neutral execution and evidence services, immutable transport records and
exact-source acceptance rules in AGENTS.md and the existing design/data contracts.
Anthropic has distinct production/candidate/qualification context kinds and hash
domains, output/prepared request5, manifest5, transport3 and diagnostic2. No OpenAI
V4 or synthetic V3 record acquires new authority or interpretation. SQLite schema,
normalized result schemas, R exports, logical IDs and original/presented pair
orientation retain their contracts.

The original OpenAI adapter, production transport/registry/prompts, qualification
harness, dependency declarations/lock and every historical fixture were compared
byte-for-byte with the starting main: 69 files, no changes. The one existing
OpenAI CLI test's unsupported-provider case now uses still-unimplemented Gemini;
its test identity and OpenAI assertions remain. The CLI's two explicit provider
branches preserve existing default OpenAI Python record types and add separate
Anthropic records. There is no general request/header/URL extension mechanism.

## SDK, API and request review

The existing `anthropic==2.3.0` and `httpx2==2.16.0` support this integration without
a dependency change. `tests/fixtures/production_anthropic/sdk-profile.json` records
28 SDK source hashes and four HTTP source hashes, including credentials, omitted
parameters, Messages/model/count-token operations, output config, thinking, cache,
response blocks/stop reasons and usage. Tests verify actual installed bytes and
all five retained Phase4C projection source hashes. SDK support, endpoint
documentation, simulated HTTP acceptance and independent live-model acceptance
remain separate facts.

API2026-10-21 removes sampling controls and uses Bearer key authentication. The
protected async client uses only the shared explicit API-key resolver, fixed API
URL/header, zero retries, no redirects or environment routing, bounded connections
and timeouts. The forwarding boundary compares the entire actual SDK body to the
sealed body. SDK/HTTP loggers are filtered in the invocation context. Responses
echoing the selected credential, including escaped forms, are withheld before
retention or diagnostic reporting. Ordinary offline validation/status/preview
creates no SDK client or network operation.

Required max_tokens is either the unchanged requested integer or a separately
committed4096 adapter default. Tier/cache decisions are explicit policy fields.
Text JSON uses the complete rendered content; native output_config.format uses
the existing conservative projection. The production native registry stays empty.
Only the dedicated source/plan/membership-bound harness can probe unqualified
native cases. Neither tests nor reports populate serving qualification.

## Responses, privacy and recovery

Messages handling requires one nonblank text block and completed end_turn. It
keeps thinking separate from assessment explanations, never repairs or joins
ambiguous JSON, and preserves failed/grouped sibling outcomes and diagnostics.
Malformed/duplicate/invalid-category/ID/trait content remains unsuccessful.
Full versus minimal component retention reuses read-only evidence services.
Usage preserves uncached input, cache creation/read, output and known/missing
subdivisions without totals or duplicated grouped usage. Unknown cache accounting
prevents a fabricated precise cost.

Actual-SDK fault tests cover cancellation before/after forwarding, parsing and
durable capture. Separate child processes exit at physical forwarding, acknowledgment
receipt, evidence receipt and validation boundaries; independent synthetic
acceptance journals and fresh recovery processes verify no duplicate sends.
Wrong recovery identities cannot consume an original received response. No Messages
retrieval service is invented; unknown acceptance stays unknown and cannot be
blindly resent.

The existing execution R gate also consumes genuine normalized Anthropic outputs
from simulated SDK/HTTP calls. It checks leading-zero character IDs, multiple
traits, original/presented pairs, selected versus absent expected/distribution
scores, failed assessments/retries, observations, diagnostics and usage joins.
All seven mandatory gates remain; final local acceptance requires49 executions.
Four added resource cases retain existing64-call/5-trait workloads and original
180-second, memory, queue and concurrency bounds.

## Explicit coverage contract extension

The original overall floor totals and coverage settings remain literal, unchanged
acceptance criteria: lines12821/13533, branches4088/4656, combined92.96277970%,
and the independent90% minimum. Every unchanged module's original source hash,
denominators, exclusions and exact covered line/branch sets remain unchanged.
Only deliberately modified or new modules below have reviewed replacement entries.
There are no new coverage omissions or weakened test-contract requirements.

Development measurements are Linux/Python3.11 only. The initial working-tree
measurement exposed three missing-PATH installation checks and an already
corrected unsupported-provider test; it is retained as failed development
evidence. Edited new-module measurements were discarded and replaced by current
source-specific security/transport/CLI measurements. Existing-source coverage
remains intact. The external review records this reconciliation and hashes; it
does not substitute for the mandatory clean exact-commit grid.

| Deliberately modified or new module | Lines | Branches |
|---|---:|---:|
| `__init__.py` | 16/16 | 0/0 |
| `anthropic_assessment.py` | 115/120 | 43/48 |
| `anthropic_diagnostics.py` | 123/129 | 24/26 |
| `anthropic_prompts.py` | 30/31 | 1/2 |
| `anthropic_qualification.py` | 496/522 | 160/186 |
| `anthropic_registry.py` | 59/61 | 9/10 |
| `anthropic_transport.py` | 346/361 | 95/110 |
| `assessment_diagnostics.py` | 65/68 | 19/22 |
| `assessment_inspection.py` | 32/36 | 7/10 |
| `evidence/order.py` | 188/189 | 91/92 |
| `evidence/usage.py` | 195/197 | 60/62 |
| `execution/durable_evidence.py` | 169/197 | 59/86 |
| `execution/retry.py` | 190/203 | 66/78 |
| `execution/session.py` | 436/467 | 160/190 |
| `lifecycle.py` | 304/331 | 88/104 |
| `lifecycle_models.py` | 137/137 | 2/2 |
| `persistence/evidence.py` | 407/452 | 149/192 |
| `persistence/transport_receipts.py` | 68/78 | 14/18 |
| `preparation/__init__.py` | 305/330 | 111/134 |
| `preparation/manifest.py` | 282/308 | 46/64 |
| `preparation/native.py` | 111/120 | 33/42 |
| `preparation/session.py` | 352/383 | 124/156 |
| `preview.py` | 68/68 | 30/30 |
| `prompts.py` | 317/320 | 121/124 |
| `provider_diagnostics.py` | 16/16 | 0/0 |
| `providers/anthropic.py` | 336/344 | 98/104 |
| `providers/anthropic_cli.py` | 14/15 | 1/2 |
| `providers/anthropic_response.py` | 137/138 | 78/80 |
| `providers/evidence.py` | 129/134 | 43/48 |
| `providers/openai_cli.py` | 41/45 | 8/10 |
| `transport_dispatch.py` | 67/90 | 30/48 |

These entries correspond to explicit provider dispatch/type additions, independent
Anthropic records and services, or the narrow pure usage-helper extraction. Their
security, parsing, recovery, source mutation, cross-provider confusion and privacy
checks are located in `ci/verification-map.json`. The final grid must independently
reproduce every required covered set and meet the original overall ratios.

## Unexecuted live proposal

The [guide](anthropic.md) documents the separate four-case plan: exact candidate
claude-sonnet-4-6, SDK2.3.0/API2026-10-21, rubric/pairwise × text/native, no
retries, concurrency1, standard_only, cache off, full assessment capture and no
reasoning retention. Each complete body/schema is audited offline.8192 estimated
input plus512 requested output tokens per call reserves $0.129024 at reviewed
$3/$15 per million; the proposed estimated authorization ceiling is $0.15.
Tokenization and injected overhead remain unverified; this is not an enforced
input limit or guaranteed monetary cap. Exact bodies, identities, source/plan
digests and pricing assumptions must be independently reviewed before separate
billable consent. No Anthropic endpoint, credential retrieval, live execution,
registry promotion, package publication or merge is part of this work.

The first final-head Windows run exposed a test guard that denied asyncio's
private loopback socket pair before the Proactor loop could initialize. The
correction permits local control sockets and still denies every external IP/DNS
connection; an explicit socketpair/external-IP test protects that distinction.
No production transport, resource policy, test selection or acceptance requirement
changes. The failed receipts remain retained; the corrected final commit requires
fresh complete local and hosted qualification.

## Focused PR #26 correctness review

Review of `69de073a9087c2fa4c01d7f4bbc48962bbe5731e` requested two corrections.
The qualification harness now requires an exact planned/reported model match in
an accepted, completed, consistent durable observation before continuation,
assessment qualification success or matrix acceptance. No independently verified
canonicalization rule is available. Missing/mismatched identity produces a fixed
diagnostic and digest in report V2; local JSON validity remains a separate fact.
Ordinary production aliases and unknown model IDs retain their existing behavior.
SDK/HTTP fixtures exercise matching, missing and different identities, including
a mismatch on the final native case and read-only restart without replacement.

Messages completion validation now examines structured `stop_details`. The pinned
SDK's `RefusalStopDetails` and the official [Messages response example](https://platform.claude.com/docs/en/api/messages/create)
confirm its refusal discriminator and free-text explanation. Refusal takes
precedence over inconsistent `end_turn`; every other non-null stop-detail shape
is unsupported. Component diagnostics withhold assessment text and retain only
closed refusal/unsupported flags. Arbitrary explanations are excluded from saved
response derivatives. Deterministic response and SDK/HTTP lifecycle cases prove
that valid assessment JSON cannot override refusal/unsupported classification.

The preceding source-bound live plan is superseded and remains unexecuted. A new
plan must bind the corrected source, all four physical commitments and a fresh
digest. Previous receipts remain attributed to their original commits. The
corrected frozen source requires the complete seven-configuration local and
five-job hosted acceptance matrix, without changing the approved CI schedule or
repeating performance experiments. Final outcomes belong in the external
verification report and PR, avoiding source changes after qualification.

The focused review remeasured only the two changed production entries on
Linux/Python 3.11 using 377 deterministic Anthropic cases. Qualification coverage
is 496/522 lines and 160/186 branches; response coverage is 137/138 lines and 78/80
branches. Every other coverage entry, original overall total and measurement
setting remains literal. Mapped unchanged covered lines have no losses; one
former terminal-classification edge now passes through the exact-model guard,
with its entry and both outcomes covered. The external source-specific coverage
and explicit contract-update audit are development evidence; full final-head
coverage remains mandatory and is not replaced by this selection.


The complete local run on `494bb8ef1b79274fc935afe3368748a35f5c2acd` passed all
Linux/macOS configurations, all 49 strict R gates, applicable resources, quality
and packaging; its two Windows pytest steps failed. All five hosted jobs passed
on that source. The retained Windows failures identified excessive test-owned
qualification paths and a missing real Git executable. The minimal guest retains
its existing disabled long-path setting; qualification fixtures use short factory
prefixes and the source-authorization test uses the checksum-verified private
MinGit documented in the local-CI guide. The existing tiny-prefix regression
failed its unchanged 10 GiB free-disk reserve before executing any requests;
its code and workload dimensions remain unchanged. Task-owned completed scratch
is copied to externally retained archives, with per-file hashes independently
verified before removing only guest duplicates. Earlier-phase evidence and VM
allocation/registry settings remain untouched. There are no assertion, resource
threshold, CI schedule, source-authentication rule or production module changes
in this portability correction. The benchmark implementations and original measurement
evidence remain unchanged; only the failed tiny correctness fixture is rerun
during development. Its results are not performance qualification.

All failures and earlier source-bound plans remain retained. Any new test/doc
commit requires another complete exact-head acceptance run and a rebuilt,
independently audited, unexecuted live plan; earlier passes are not substituted.
