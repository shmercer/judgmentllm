# Vertex live qualification acceptance — Phase 5D3

Evidence accepted on **2026-11-11**, following independent offline reconstruction
from two different historical clean source checkouts. This accepts eight already
completed requests. It authorizes no additional qualification, provider calls,
credential/keyring access, spending or package publication. The promotion PR
remains unmerged for independent review.

## Original sources and independent verification

The ADC qualification source is
`59a45ec31e53de7ed9eb833f5876c0000d982bca`; the successful API-key source is
`db98be97654d8e918516618399d57ec8c28e4ec6`. These remain the respective profiles'
`source_commit` values. The later accepting commit is identified by this document's
Git history and PR head; it must never replace the evidence-producing source.

Private evidence remains at `~/.local/share/judgmentllm/vertex-qualification/`.
The machine-readable [complete verification record](vertex-evidence-verification.json)
contains every case's physical/request/response/assessment/result/provenance
commitments, authorization hashes, SDK-source evidence, usage and authority digests.
It contains no project identifier, prompt, assessment text, credential or reasoning
content. Public CI never needs the private evidence.

| Identity | ADC | API key |
|---|---|---|
| Immutable plan SHA-256 | `ff2fd19488f11524ee0aed165e588a3a13581d12127cc7f25a9ca5f7836fa5aa` | `6707041fa6b0eb2945dc82593b7d5e975a019d0655e81aee7a25f4fba6b776c7` |
| Independently regenerated canonical report SHA-256 | `6843a1b81b0cca2bea08bcae6a5b1e5788fde8f557768f90272a8ecdfb3837ae` | `1a523c431b795e7e1d4b4ded26688f8d4cb9ea2746ed409f64b5331e5de84fff` |
| Retained CLI report file-byte SHA-256 | `95357999c6f1f2a9ef4c2dc3d98533c690aec706a8e55974821c70916bdcca55` | `7e43725853b9d4c03593c3257f5bddef763c1a3203327f08c1136505ae472f2d` |
| Separate authorization SHA-256 | `5487fc0fa997c8babe95b6085a556621bf56f5afe16ad9396e4830d7bbf982aa` | `48548b21de546822c5cd44c94b20c6ea4f8a2777ee516ecd07b4408028dfcc32` |
| Mode/project/global route SHA-256 | `079f2786c63e61bd84aacba6c6b11ba59c332101fb2e0827a5a685360cf611a4` | `b57981a23b79e46d517cca532d8fea81cc0a9bb662e9843fd401fcb4ffec49e1` |

Canonical hashes use `persistence.hashing.canonical_bytes`: sorted keys, compact
separators, UTF-8 and no newline. A report's `report_sha256` hashes the canonical
execution report **excluding that field**; a case's `provenance_sha256` similarly
excludes itself. These differ from hashes of complete canonical reports and
formatted CLI file bytes; the JSON record distinguishes all three.

Independent verification used separate clean detached clones of each original
commit, Python **3.11.18**, the unchanged dependency lock
`3952d60d4bf8d451aec42522705bcd8a2d9bbe361076e3be430ba5f4817e2526`,
and installed distributions checked against its pins. `google-genai==2.34.0`
and its reviewed source files matched the historical SDK profile. Original
`verify_plan()` rebuilt each physical URL/body through the production builders
and SDK's pure converters, without constructing a client. Original `report_plan()`
then inspected the existing execution stores read-only. Each regenerated report
matched both the independently supplied digest and retained CLI report content.

The audit hid provider environment variables, denied credential paths, keyring,
ADC discovery/refresh, SDK client construction, sockets/DNS and filesystem
mutations, and admitted only exact checkout `git rev-parse`/`status` subprocesses.
SQLite connections required read-only mode. The original evidence filesystem was
mounted **read-only with noatime**, preserving access times as well as other
metadata. Initial dependency-inspection and guard setup errors were corrected
before the successful verification; they established no evidence conclusion.

The before/after inventory covers **658 files**, including unsuccessful and older
proposal evidence, and verifies **402 content-addressed objects** against their
names. File bytes, membership, size, mode and **mtime/atime/ctime nanoseconds**, plus
directory metadata, were unchanged. The full file inventory hash is
`93bf96486a24e8045634cb81e9ff15310c3de8b86dd176eb4c3893c3d69638e3`;
metadata hash is
`dcc2478434959c5159f274dd3997b62b82d19e654bbe0a1403835e165f14e8f6`.
Inventory JSON maps evidence-relative POSIX file names to `[sha256, byte_length]`.
The separate metadata mapping includes directories and
`[mode,size,mtime_ns,atime_ns,ctime_ns]`.

For **each** mode, the immutable plan/inventory, separate matching authorization
anchor and copy, consumption ledger and independent forward intents establish:

- Four distinct physical submission/correlation/attempt identities, exactly one
  attempt per case, no retries, duplicates, unresolved or extra submissions.
- Four confirmed accepted HTTP responses, four valid assessments and four exact
  `modelVersion == gemini-3.1-flash-lite` matches, without an alias rule.
- Rubric-text, rubric-native, pairwise-text and pairwise-native each complete with
  `STOP`, one supported assessment component, no refusal or truncation.
- Exact request/prompt/transport/manifest/source commitments and response,
  evidence-receipt, assessment-component, normalized-result and case-provenance
  digests verify. Strict JSON/category/winner validation and result reconstruction
  preserve item `001`/`002`, trait `01` and reversed pair presentation.
- Retained response objects are assessment-only derivatives with exactly
  `protocol`, `observation_key`, `kind`, `assessment_text`. Their text matches the
  separate retained component hash/length. Credentials, reasoning text, signatures
  and unapproved envelope fields are absent from these retained derivatives.

These are integrity checks on retained local evidence. They do not supply provider
signatures or prove authenticity against replacement of every local artifact.

The original unsuccessful API-key execution at `59a45ec31e53-api_key` remains
separate and unchanged: one consumed allocation, unresolved acceptance, no
confirmed HTTP response or assessment, three unstarted cases. It is not accepted
qualification and its historical cause is not retrospectively inferred.

## Historical source compatibility

The exact source difference consists of four files: 43 documentation lines,
seven adapter lines, 118 executor-test lines, and 284 added/one removed SDK-test
lines. Dependencies, SDK profile, request builders, schemas, prompt rendering,
routing/authentication, successful-response parsing, usage, normalization,
qualification plan/report algorithms and authority definitions did not change.

Commit `6366753` adds the seven-line privacy-safe HTTP **403** correction inside
the already sealed HTTP guard. An actual 403 response is closed, without reading,
parsing, retaining, logging or hashing its body, then classified using the existing
fixed `permission_denied` rejection. A failed close cannot erase that status.
Other statuses retain their existing handling. Error prose or JSON codes cannot
establish a status. Tests exercise both modes, private body echoes, closing
failures, uncertainty, stop/replay behavior and retention exclusions.

Both accepted records establish successful HTTP-response and strict assessment
behavior compatible with the current production contract. The ADC source predates
the correction; the API-key source includes it. Successful handling is unchanged,
so the ADC success evidence remains applicable to its exact route. Its provenance
still points to the older source. Neither success qualifies historical 403
behavior; synthetic regression evidence establishes the corrected local behavior.
No old plan, manifest, authorization or unsuccessful record is rewritten.

## Narrow reviewed production authority

Exactly `vertex` / `models.generate_content` / `gemini-3.1-flash-lite` /
`v1beta1` / `google-genai==2.34.0` / `global`. Each authentication mode has its own
explicit reviewed profile and private-project route commitment. The native
mechanism is **`vertex.generate_content.response_json_schema`**, translator 2,
dialect `vertex.json-schema.v1`, with only:

- `rubric_scores`: one trait, scores only, per physical request.
- `pairwise_forced_choice`: winner only, without ties or explanations.

No authority transfers between modes, projects, regions, models, API/SDK versions,
mechanisms or providers. The route hash is an integrity commitment, not a claim
of project anonymity or account-wide authorization. A new project or region
retains unknown native capability. Runtime configuration and ordinary production
workflows select authority only through the reviewed registry; plans, reports,
credentials, environment and caller evidence cannot register a scope. There is
no public registration API. Production supplies no qualification submission guard
and consumes no historical qualification allowance.

Qualified explicit native and `auto` requests execute through the existing
production lifecycle and pinned SDK. Explicit text remains text. Unqualified
native fails before forwarding; nonstrict `auto` retains text fallback and strict
unknown fails. Selection freezes before rendering and cannot change after a
failed completion. Local canonical validation remains mandatory.

Grouped rubrics, explanations/layouts, ties, `response_schema`, other models,
batch/bulk, thinking controls and untested generation parameters remain
**unknown/unqualified**, not newly unsupported. Every live case requested
`max_output_tokens=512` and omitted sampling. `generation=None` in the authority
records and aggregate generation `live_qualified=False` preserve the existing
validation policy without broadening generation qualification.

## Provenance and authority digests

Both profiles retain the original-source byte hashes:

| Provenance | SHA-256 |
|---|---|
| `docs/vertex.md` at each original source | `9b423e04ed0c7d9c569c4fe73b2dd5b3bc13380856a2bc53f49760d622cec8b7` |
| `tests/vertex_fixture.py` at each original source | `918cd2571aef737985c658a7faa45d5b455d424119b2c1493711883c208f2fcb` |

The independent-live provenance is each separately regenerated canonical report
digest in the first table. Dates are 2026-11-11. Documentation hashes identify the
historical guide and reviewed references, not a later edit or mutable web download.
Reproduce source hashes using `git show ORIGINAL_SHA:PATH | sha256sum`.

| Reviewed identity | ADC SHA-256 | API-key SHA-256 |
|---|---|---|
| Profile authority (`vertex-reviewed-production-profile-v1`) | `9b0cc93d30233f67287115682da37e1d0ed9bc2483c5bcdde716da3aa435a3f4` | `4bfc3a920ff37fa4694bd0c3682aa04edcaca2d3ff848303ef00b2de315c01fa` |
| Ordinary production aggregate authority | `9153b409edc8661524cd61c58e0e4d152cd365f996d6387b78aa590731ac0413` | `6da20ebe9f425584c308146df6ef386032323b9f9350c8e127fa1e266f35911e` |
| Ordinary production context | `f29cf71404f83143df4c670e3ee4a0c20d8e1b29eb1e12d6f1da5336ca58f3df` | `88ecb86a147882c459e9be951619dd7bb7e86800d4915b2d314b6b3a9af79c74` |

## Frozen transport and resume compatibility

Adding the matching profile changes aggregate authority and context digests for
that exact model **and qualified route**, including explicit text and candidate
contexts. Output, manifest and transport commitments consequently change.
Explicit text body bytes remain the same; eligible fresh `auto` selects native.
Unqualified projects/regions/models retain the historical empty-profile authority
representation and their existing frozen identities. No public schema version,
normalized JSONL/CSV/R column or historical projection changes.

The JSON record preserves both historical and promoted production context hashes.
Historical stores/plans bind their original source/authority. New code rejects
reconstruction/resume/replay when those facts differ, including text requests;
there is no migration, compatibility exception, reselection or weakened check.
**Use each run's original clean source checkout and pinned dependencies to inspect
or recover historical evidence.** An original completed observation can finish
recovery without another call; unresolved work cannot gain replacement authority.
The promotion checkout deliberately cannot reconstruct the original live plans.

Synthetic regressions construct historical frozen and executed evidence with the
original registry, verify rejection after promotion, verify unchanged bytes, and
restore the original registry to prove exact reconstruction. New qualified
production runs test interruption, recovery, no replay and separate harness
identity using synthetic projects and credentials only. No external live evidence
is migrated or used as a test fixture.

## Usage and acceptance limits

ADC reports **1,089 input**, **97 candidate output**, **1,186 total** tokens.
API key reports **1,089 input**, **102 candidate output**, **1,191 total** tokens.
Reasoning/cache counters and estimated complete charges remain **null**. No zero
usage, inferred total, precise bill or partial-cost invoice is fabricated. Each
mode's original reservation is **$0.008960**, with the reviewed **$0.02** estimated
ceiling; this was not a provider-enforced monetary cap.

This establishes request/assessment/native behavior for the narrow historical
scope, not measurement validity, fairness, calibration, future availability or
all credentials/accounts. Credentials and inaccessible provider internals were
not inspected. Wider capability requires a separately authorized qualification
and reviewed promotion.

Ordinary Level 3 acceptance requires the exact-head protected-base **CI acceptance**:
Linux/Python 3.11 complete required suite and primary change-aware coverage,
representative Windows 3.12/macOS 3.11, all seven strict Linux R/jsonlite gates,
and affected cross-provider security/recovery/history regressions. The PR records
its automatic plan, exact-head hosted artifacts and separate passed/failed/deferred
results. Fast development checks establish no full acceptance. Comprehensive and
unaffected packaging checks remain deferred unless the protected plan requires
them; deferred work is never reported as passed.

### PR #36 resumption after CI repair PR #37

PR #37 merged into main as `cf694805ceeeef69ccd75c27ada168e9eafcfa93`.
PR #36 incorporates that main through a merge, retaining original promotion
commit `22f2b7cffb5bdd2a6a67ad94438d6c9de3f1b23f` and both original
qualification-source identities above. The two ADC/API-key instances of the
outdated empty-registry assertion now verify an unchanged reviewed registry and
an unmatched synthetic candidate route; all actual authority-denial checks remain.

The operator-authorized temporary Azure DNS exception requires three independently
reconciled results at one final PR head: complete local Ubuntu/Python 3.11 Level 3
including all resource cases and package validation, passing GitHub Windows3.12,
and passing GitHub macOS3.11. Hosted Linux and aggregate failures remain failed
infrastructure evidence; no replacement aggregate success is manufactured. The
operator retains responsibility for restoring required aggregate CI acceptance.
The agent makes no ruleset changes.

The [PR verification record](vertex-pr36-verification.md) locates the exact-head
external ledger, automatic and supplemental local plans, individual job links,
independent reconciliation and separate passed/failed/deferred results. This
resumption changes no reviewed profile, scope, source provenance, original audit
record or live evidence. PR #36 remains open and unmerged for independent review.
