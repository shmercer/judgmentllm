# Gemini Developer live qualification acceptance

Accepted evidence reviewed on **2026-11-10**. This record accepts four previously
authorized calls; the promotion work makes no provider requests and reads no
credentials or keyring. It establishes selected request/assessment capabilities,
not comprehensive model validity, scoring accuracy, fairness or calibration.

## Source and evidence identities

The original qualified source is
`a77e700f550e68b382c1734d270b8bfa9eb8b181`. A clean detached worktree at that
revision independently reconstructed the original plan and all four transport
commitments. The later commit introducing this record and the reviewed registry
entry **accepts** that evidence; it did not generate it. The profile's
`source_commit` must remain the original qualification revision. The promotion
commit is identified by this record's Git history and the PR head.

Sealed evidence remains outside Git at
`/mnt/VM_Fast/judgmentllm-gemini-live-a77e700f550e`.

| Retained identity | SHA-256 |
|---|---|
| Original immutable `plan.json` | `0ce5ad3f05f7d96def6db09df0c53b47bd3aea923342417e796feaa4e7a06487` |
| Uploaded/local `review-output.json` bytes | `ddd2a6e2f206d7b0f6f99db1a9328f65926abe8d8c660309fddad285afa36e31` |
| Independently regenerated report, canonical parsed JSON | `38106d92dd20ee9ef091f26e9a327751b2d682215582a568bab56e69f2370e68` |
| Complete retained file inventory | `a7ef2c3362a637e8b2643c99867ff5e6ab2a03cad5d7d10ef7735dd9d2ff80fa` |

The local uploaded report matches the reference byte digest. Independently
regenerated reports match its parsed content, without assuming identical output
formatting. Canonical JSON uses `persistence.hashing.canonical_bytes` (sorted
object keys, compact separators, UTF-8, no newline). The inventory digest hashes
canonical JSON mapping every evidence-relative POSIX file path to
`[sha256(file_bytes), byte_length]`; it covers **177 files**. All **112 retained
content-addressed object files** match their names. File membership, content,
lengths and modification times were unchanged across repeated read-only review.
Audit guards prohibited network/subprocess operations, provider environment reads,
authentication/client imports and filesystem writes during that verification.

The plan and separate authorization/consumption ledger agree on exactly four
distinct physical submission identities. All four allocations were consumed,
all four HTTP responses were confirmed, and each run has exactly one attempt,
no retry operation and no unresolved submission. No replacement or extra attempt
is present. Manifest/source/request/response/evidence receipts verify, and the
separately retained assessment components match their recorded hashes/lengths.

| Case | Outcome | Retained assessment component SHA-256 | Bytes |
|---|---|---|---|
| rubric-text | Qualified | `d2a421a405ac4968e51c3be09f2b001e18ae69d4a91a19276d9726d4d0bd7c06` | 54 |
| rubric-native | Qualified | `f7fe6a869e7668e0065cf2b44e36f5cf54f0b067757a8d7ed356c26939f9c3af` | 88 |
| pairwise-text | Qualified | `8b640b413ecdc1f020ab9bdfc24794032baccce771a5ce4f7d15f00b45581b76` | 19 |
| pairwise-native | Qualified | `cdd940e7216b7a46f33922e6605fed5f5b56bf8ebb9371279e45f763c6fd80f1` | 24 |

Every assessment is canonically valid, reports exactly
`gemini-3.1-flash-lite`, finishes with `STOP`, and has one extracted text component
without refusal, truncation, tool use or unsupported components. All durable
terminal results are complete. Outstanding, unsuccessful and unresolved case
lists are empty.

## Accepted scope

Exactly `gemini` / Gemini **Developer API** / `models.generate_content` /
`v1beta` / `google-genai==2.34.0` / `gemini-3.1-flash-lite`.
Text qualification covers one-trait ordinal scores without explanations and
forced-choice pairwise winners without ties or explanations. Native promotion
uses only `gemini.generate_content.response_json_schema`, translator version 2,
dialect `gemini.json-schema.v1`, with exactly:

- `rubric_scores` (one trait per physical request, scores only).
- `pairwise_forced_choice` (winner only).

The reviewed `_QualifiedProfile` records `supported` / `accepted` / verified
assessment and native-schema behavior. Its `unsupported_features` is empty.
`rubric_grouped`, `rubric_explanations_together`, `rubric_scores_first`,
`rubric_explanations_first`, pairwise ties/explanations, `response_schema`, all
other model identifiers, Vertex, batch/managed bulk, unrequested generation
controls and reasoning features remain **unknown/unqualified**, not unsupported.
Local projection/SDK fixture support for broader shapes does not change that.
Native requests outside the feature scope fail preflight; nonstrict `auto`
selects text for unknown scope, while strict `auto` rejects it. Failed responses
never trigger automatic text fallback or replacement calls.

All live cases requested `max_output_tokens=512`; temperature and top-p were
omitted. The existing generation descriptor has one aggregate live flag, so the
profile leaves `generation=None` and broader `live_qualified=False` unchanged.
Existing documented/fixture-tested generation mappings remain available under
their existing validation policy; they gain no live qualification from this PR.

## Provenance and reviewed authority

All provenance dates are 2026-11-10. These are plain SHA-256 byte identities:

| Profile provenance | Derivation | SHA-256 |
|---|---|---|
| documentation | `docs/gemini.md` at the original qualified source, containing the reviewed documentation references and exact proposal boundary | `67b3050f1727a23153a4f9eb5f8b27a85768e407f8f10c96e990db4eef7a1576` |
| offline_fixture | `tests/gemini_fixture.py` at that same source, used by the offline transport/SDK tests | `7e24ab54b72b4f99c8a35646bbf17f783381ac3bcbeddc08a39a9128d58d55f0` |
| independent_live | Local uploaded report bytes, accepted only after independent sealed-store verification above | `ddd2a6e2f206d7b0f6f99db1a9328f65926abe8d8c660309fddad285afa36e31` |

Reproduce source-file hashes with `git show ORIGINAL_SHA:PATH | sha256sum`.
The documentation digest identifies the historical guide, not this later edited
guide or a fresh download of mutable Google pages. The live digest alone is not
verification of the external store and never creates registry authority.

The profile's `authority_sha256` is
`88db527b54665d75f46e3f3554c00a6f2575000072953d0b8cc1591572c8de0a`, using
`semantic_digest("gemini-reviewed-production-profile-v1", profile)`.
For its ordinary production context, the aggregate authority digest is
`d95661e732cc119b3cff6b4c92e88f33cf9c39d4bc94d0e8975bd14c5ae475e9` and
context digest is
`90c5f2c261e55f150ef264dbc3d57c9e9ca721f809e70233a01b3c9045fd1074`.
Only a reviewed repository edit supplies production authority. Caller evidence,
reports, local plans, environment, discovery and authentication cannot add it.

## Historical compatibility and report interpretation

`gemini_transport._authority_sha256()` includes both mechanism profiles even for
text and qualification-candidate contexts. Adding this profile changes authority,
context, output and transport commitments for this exact model. Fresh explicit
text bodies preserve their existing behavior, but their authority receipts change;
fresh eligible `auto` requests now select native. Other model scopes are unchanged.

Existing manifests, selections, source commits and transport commitments are
never rewritten. New code rejects reconstruction/resume/replay of the affected
older frozen contexts. **Verify historical evidence and recover historical runs
using their original clean source and dependencies.** In particular, use the
original qualified checkout to run the existing read-only `report` command
against the external evidence at its original path. Newer promotion code cannot
reconstruct that plan and deliberately raises `QualificationError`. This is the
existing source-bound integrity policy; there is no historical digest exception,
automatic migration or authority bypass. Offline regressions exercise historical
synthetic executed evidence, frozen production manifests, original bytes and
restored original authority without altering the live evidence.

Report version 1 and plan version 1 remain unchanged. The inherited
`executor_status=deferred_requires_reviewed_source_bound_executor_and_human_authorization`
describes the immutable proposal stage. The separate authorization and durable
execution report establish the later `overall_qualification_result=qualified`,
four consumed requests and four responses. These historical stages are distinct;
the plan's zero-authority fields are not edited to resemble execution results.
A report never automatically promotes its conclusions into production.

## Usage, privacy and remaining acceptance

Provider-reported counters sum to **824 input**, **80 candidate output**, and
**904 total tokens**. Reasoning and cached-input tokens are unknown, not zero.
Estimated actual cost remains unknown under the complete-cost accounting policy;
no partial price calculation is represented as an invoice. Original estimated
reservation: **$0.011264 USD**; proposed estimated authorization ceiling:
**$0.02 USD**, without a provider-enforced monetary cap.

This record commits only nontext identities and conclusions. Raw prompts, exact
synthetic assessment outputs, complete provider responses, authentication
material and external SQLite stores are not copied into Git. Full synthetic
assessment capture was separately authorized; reasoning text/signatures remain
excluded. Retained derivatives cannot prove anything about inaccessible provider
internals. Independent re-verification requires access to the external sealed
store; public CI uses offline synthetic fixtures, not those live artifacts.

Production authority/recovery changes are **Level 3** under
[the verification policy](ci-optimization.md). The PR must pass exact-head
protected-base `CI acceptance`, including Linux primary coverage, representative
Windows/macOS and seven strict Linux R gates. Development checks and deferred
checks do not establish that acceptance. Broader features require separate,
explicitly authorized qualifications and reviewed promotion. No package
publication or additional provider operation is authorized here.
