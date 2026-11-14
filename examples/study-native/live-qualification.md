# Launching the native pilot after review and merge

This procedure is for the operator **after this implementation is accepted and
merged**. Nothing in this guide authorizes a call. Development proposals and
synthetic test authorizations cannot authorize later live execution. Use only the
package's synthetic essays. Never repair or rescore an invalid returned assessment.

## Source and private workspace

From the final checkout, set `ACCEPTED_COMMIT` to the independently reviewed
**merged** 40-character commit. All subsequent commands use this checkout and its
pinned editable environment; the study module is repository tooling.

```bash
git switch main
git pull --ff-only
ACCEPTED_COMMIT='<REVIEWED_MERGED_COMMIT>'
test "$(git rev-parse HEAD)" = "$ACCEPTED_COMMIT"
test -z "$(git status --porcelain --untracked-files=all)"
uv sync --locked --all-extras --group dev --python 3.11
umask 077
LIVE=/mnt/VM_Fast/judgmentllm-live
install -d -m 700 "$LIVE" "$LIVE/proofs" "$LIVE/authorizations" "$LIVE/observations"
export LIVE
uv run --offline --no-sync python -m judgmentllm.study verify
```

Before sealing a proposal or starting a campaign, the **post-merge operator** must
exercise the source-checkout launcher against the real unlocked desktop service:

```bash
uv run --offline --no-sync python -m judgmentllm.keyring --provider openai -- \
  judgmentllm auth status --require openai
```

This offline configuration check verifies actual `gdbus` formatting and key lookup
without displaying keys or making provider requests. It is operator-only: the
implementation and automated tests use synthetic Secret Service fixtures. Existing
successful provider authentication/discovery need not be repeated.

Keep this persistent directory off Dropbox and outside the checkout. Keep account
proofs, credential fingerprints, proposals, consent, reports and exports private.
Retain the existing independent `~/.judgmentllm/study-campaign-bindings/` and
`study-campaign-checkpoints/` alongside it; restrict `~/.judgmentllm/` to its owner.
Do not remove, recreate, relocate or restore older campaign state to renew a budget.
The first authorized execution establishes the one campaign root and independent
checkpoints, and starts the suite's immutable clock before resolving a credential.
Readiness checks do neither. Suite deadlines include time spent inspecting a pause.

## What is already established, and what remains

The operator has already verified offline configuration for all six keyring
providers and successful **non-generative** authentication/model visibility for
OpenAI, Anthropic and Gemini. Preserve those private records; do not repeat
discovery merely to run this guide. Visibility is distinct from account native,
tier and control eligibility, SDK serialization and observed generative support.

| Provider / route | Exact models | Native mechanism |
|---|---|---|
| OpenAI / Responses | `gpt-6-luna`, `gpt-6.1-terra` | `openai.responses.text.format` |
| Anthropic / Messages | `claude-haiku-5-5`, `claude-sonnet-5-5` | `anthropic.messages.output_config.format` |
| Gemini Developer / GenerateContent | `gemini-3.5-flash-lite`, `gemini-3.8-flash` | `gemini.generate_content.response_json_schema` |

Vertex, Together and Typesafe are outside this six-model qualification. The main
pilot remains Flex for OpenAI/Gemini and Standard for Anthropic. Tier feasibility
retains Standard/Flex/Premium candidates only where eligible; Anthropic Flex and
Premium remain unsupported. Reasoning and temperature controls retain their
separate approved matrices. No family inference, route substitution or text fallback.

| Independent suite | Maximum handoffs | USD ceiling | Wall deadline |
|---|---:|---:|---:|
| `native_pilot` | 42 | 5 | 8 hours |
| `tier_feasibility` | 24 | 5 | 3 hours |
| `reasoning_comparison` | 24 | 5 | 5 hours |
| `temperature_acceptance` | 8 | 1 | 3 hours |

Keep the original 66-call/$10/39,600-active-second sub-cap and independently
consented 98-call/$16/68,400-active-second extension. Money, calls and time do not
transfer between suites. No lookup, readiness check or pause increases consent.

## Review evidence and bind the account

Generate an **unexecuted, blocked worksheet** at the final source:

```bash
uv run --offline --no-sync python -m judgmentllm.study proposal \
  --suite native_pilot --account-scope study-private-01 --output "$LIVE/native-worksheet"
```

For every exact selection in that proposal, an independent reviewer supplies
`Eligibility` and `Price` records (JSON arrays) using
[`records.py`](../../src/judgmentllm/study/records.py). Keep all unknowns blocked.
Review exact account, model, endpoint, SDK/API version, native mechanism, complete
feature set, selected controls, tier entitlement, 32,768-input bound and the full
8,192 **billable** output bound. Account authorization to attempt a qualification
does not assert an already successful generative observation. Offline SDK bytes
establish serialization only. The model must still demonstrate native acceptance.

Only a lightweight dated rate record is needed: USD input and all-billable-output
rates for **every possible billed served outcome**, exact model/route/account,
official URL or private account quote, recording date and reviewer. Flex must
cover Standard too; Premium must cover a Standard downgrade. Any additional
reviewed billable tier requires a complete rate. Include cache classes or other
charges in the conservative input/output bound; if those rates cannot upper-bound
the request, leave admission blocked. Do not reduce the committed generation caps.
Use the maximum complete cost over outcomes, without reclaiming estimated savings.
OpenAI reasoning is already included in output; Gemini reasoning is additive.
Anthropic input includes reported cache creation and read tokens.

Official rate references, checked 2026-11-13: [OpenAI](https://developers.openai.com/api/docs/pricing),
[Anthropic](https://platform.claude.com/docs/en/about-claude/pricing),
[Gemini Developer](https://ai.google.dev/gemini-api/docs/pricing).
These are reference documents, **not account-specific price or eligibility grants**.
Copy the relevant exact-model/tier rate and date into the private reviewed proof;
do not infer a rate from another model or a TTS, Vertex or Interactions route.

Store each proof's exact bytes as `proofs/<SHA256>.json`. Eligibility's
`evidence_sha256` identifies the private reviewed account evidence, and Price's
`evidence_sha256` identifies the dated billing proof. One provider's account proof
may cover multiple explicitly enumerated selections. Use opaque account/reviewer
aliases. No key, email, organization ID or billing detail belongs in public evidence.

`CredentialBinding` in [`execution.py`](../../src/judgmentllm/study/execution.py)
binds that independently reviewed account proof to the selected key. During the
post-merge operator session only, compute it in memory as follows (repeat for the
three study providers with their own reviewed account proof; **do not run this
during implementation**):

```python
import os
from datetime import UTC, datetime
from pathlib import Path
from judgmentllm._auth import _resolve_credentials
from judgmentllm.keyring import credentials
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.study.execution import CredentialBinding

root = Path(os.environ["LIVE"])
provider = "openai"  # or anthropic / gemini
account_proof = "<REVIEWED_ACCOUNT_PROOF_SHA256>"
assert (
    bytes_digest((root / "proofs" / (account_proof + ".json")).read_bytes())
    == account_proof
)
with credentials((provider,)):
    resolved = _resolve_credentials(provider)
    binding = CredentialBinding(
        provider=provider,
        account_scope="study-private-01",
        credential_source=resolved.source,
        credential_fingerprint_sha256=bytes_digest(
            canonical_bytes(
                ["study-account-binding-v1", "study-private-01", resolved._api_key()]
            )
        ),
        account_evidence_sha256=account_proof,
        verified_on=datetime.now(UTC),
        reviewed_by="<INDEPENDENT_REVIEWER_ALIAS>",
    )
raw = canonical_bytes(binding)
digest = bytes_digest(raw)
with (root / "proofs" / (digest + ".json")).open("xb") as stream:
    stream.write(raw)
print("Private binding proof:", digest)  # no key or fingerprint printed
```

Reference this binding digest from each matching Eligibility's `binding_sha256`.
Review and validate the complete records independently. A fingerprint links the
key to a reviewed proof; it does not independently establish account ownership.

## Seal, independently check, then explicitly authorize

```bash
uv run --offline --no-sync python -m judgmentllm.study proposal \
  --suite native_pilot --account-scope study-private-01 \
  --eligibilities "$LIVE/native-eligibilities.json" --prices "$LIVE/native-prices.json" \
  --proofs "$LIVE/proofs" --output "$LIVE/native-pilot"
```

Require `sealed`, 42 cases and zero blockers. Independently calculate SHA-256 of
the exact `proposal.json` bytes, compare `proposal.sha256`, source and ordered case
commitments with the worksheet and reviewed records. Changing evidence requires a
fresh proposal and consent; do not edit a sealed directory. The fixed first case
is grouped 12-trait scores-only rubric for `gpt-6-luna`, Flex, native schema,
output cap 4,096. Verify that selection in the proposal; never reorder to force it.

An independent authorizer must explicitly create a separate `Authorization` JSON
using the fields below, with the generated digest and accepted source. This is a
deliberate consent action; **never generate consent from lookup or readiness**:

```json
{
  "proposal_sha256": "<SEALED_PROPOSAL_SHA256>",
  "source_commit": "<REVIEWED_MERGED_COMMIT>",
  "suite": "native_pilot", "maximum_calls": 42, "ceiling_usd": "5",
  "elapsed_seconds": 28800,
  "campaign_calls": 66, "campaign_usd": "10", "campaign_seconds": 39600,
  "accept_billable_calls": true, "accept_synthetic_capture": true,
  "authorized_by": "<AUTHORIZER_ALIAS>", "authorized_at": "<UTC_ISO_TIMESTAMP>"
}
```

Save privately as `$LIVE/authorizations/native-pilot.json`. Each other suite needs
its own proposal and matching consent; extensions additionally require the exact
98/$16/68,400 declaration. Concurrency remains one and automatic retries zero.

```bash
DIGEST='<SEALED_PROPOSAL_SHA256>'
uv run --offline --no-sync python -m judgmentllm.study check-admission \
  --proposal "$LIVE/native-pilot" --proposal-sha256 "$DIGEST" \
  --authorization "$LIVE/authorizations/native-pilot.json" --proofs "$LIVE/proofs"
```

This validates source, request commitments, consent and proof hashes without
credentials, starting a suite clock or sending a request. A successful check is
document validation; live credential matching and budget admission still occur
under the campaign lock immediately before execution.

## GNOME Keyring launcher

From a normal unlocked Pop!_OS desktop terminal:

```bash
uv run --offline --no-sync python -m judgmentllm.keyring --provider openai -- \
  judgmentllm auth status --require openai
```

Repeat `--provider` for a selected set. Exact keyring names map to the existing
resolver: `openai` → `OPENAI_API_KEY`, `anthropic` → `ANTHROPIC_API_KEY`, `gemini` →
`GOOGLE_API_KEY`, `vertex` → `JUDGMENTLLM_VERTEX_API_KEY`, `together` →
`TOGETHER_API_KEY`, `typesafe` → `TYPESAFE_API_KEY`. Selecting Vertex defaults to
explicit `api_key` mode; `--vertex-auth-mode adc` reads no Vertex key and preserves
the existing ADC chain. Ordinary JudgmentLLM ADC behavior is unchanged. No Vertex
fallback to ADC or a Gemini key occurs in selected API-key mode.

The helper is available only in the editable development checkout; public wheels
and sdists contain neither its module nor a `judgmentllm-keyring` entry point.
The launcher uses metadata-only D-Bus `SearchItems` to reject missing, locked or
ambiguous entries, then exactly `secret-tool lookup provider NAME` with captured
output. `gdbus` and `secret-tool` are Pop!_OS prerequisites. Selected keys live only
in memory and the CLI process environment; inherited API-key variables are stripped.
Lookup subprocesses receive no provider keys. No shell, `.env`, secret argument,
key output or key file is used. The CLI replaces the launcher process and owns its
exit status and termination; transient lookup processes have bounded timeouts.
Process environments remain accessible to the same OS user/root; use a trusted
session, disable shell tracing/debug dumps and keep reports private.

Over SSH or in stale tmux sessions, reconnect from the unlocked desktop session
or deliberately use that session's existing `DBUS_SESSION_BUS_ADDRESS`. Never
start a separate empty session bus, export keys in shell history or print lookups
to diagnose this. Missing session service, locked entries, ambiguity and empty
keys fail with sanitized diagnostics. No desktop key is read by standard tests.
See [Secret Service](https://specifications.freedesktop.org/secret-service/latest/ref-dbus-api.html)
and [secret-tool](https://manpages.ubuntu.com/manpages/noble/man1/secret-tool.1.html).

## First scoring request — operator initiation required

**This command can incur API charges. Run it only after the preceding independent
review, merge, evidence and explicit authorization steps.** Reuse the same campaign
path for every later suite and restart. `study` is a special launcher route: it
forwards only provider names, and resolves only the current case's key **after**
source, consent, proofs, campaign checkpoints, deadlines and budget checks pass.

```bash
uv run --offline --no-sync python -m judgmentllm.keyring --provider openai -- study execute \
  --proposal "$LIVE/native-pilot" --proposal-sha256 '<SEALED_PROPOSAL_SHA256>' \
  --authorization "$LIVE/authorizations/native-pilot.json" --proofs "$LIVE/proofs" \
  --campaign "$LIVE/campaign" --max-new-cases 1
```

At most one new physical request is handed off. Inspect the returned `invocation`,
`suite_execution_state`, `incomplete_coverage`, campaign reservation and holds.
An expected normal pause is `case_limit_reached: true` and
`awaiting_explicit_execution`, with incomplete suite coverage. A completed suite
reports `complete`; a hold or uncertainty takes precedence. No pause journal event
or new authorization is created. Failed canonical assessments retain their evidence
and existing terminal-continuation policy; the limit never retries them.

Read-only status, report and exports require no keyring launcher:

```bash
uv run --offline --no-sync python -m judgmentllm.study status \
  --proposal "$LIVE/native-pilot" --proposal-sha256 "$DIGEST" --campaign "$LIVE/campaign"
uv run --offline --no-sync python -m judgmentllm.study report \
  --proposal "$LIVE/native-pilot" --proposal-sha256 "$DIGEST" --campaign "$LIVE/campaign" \
  > "$LIVE/observations/native-report.json"
uv run --offline --no-sync python -m judgmentllm.study export \
  --proposal "$LIVE/native-pilot" --proposal-sha256 "$DIGEST" \
  > "$LIVE/observations/native-exports.json"
```

Independently inspect the first report row and its immutable JSONL/CSV/R exports:
requested/returned exact model; requested/observed tier and downgrade/missing-tier
flags; native mechanism and committed schema/projection; canonical validity and
all 12 trait/category/coding mappings; input/output/reasoning/cache usage (unknowns
stay null); run/request/attempt/submission/correlation/observation identity;
diagnostic response outcome and termination metadata; evidence/receipt/artifact
hashes and provenance; one conservative budget reservation, never 12 charges.
The report verifies retained response commitments; use
`inspect_assessment_output(run_id, observation_id, store=PROPOSAL/preparation/CASE,
include_text=True)` for independently hash-verified assessment bytes. Text is
available only with full capture and is untrusted diagnostic output. No key is
needed. A missing served tier can preserve scores without qualifying the tier;
an expected Premium downgrade cannot qualify Premium. None of these observations
automatically promotes a model or control.

Continue only after independent inspection establishes safe, authorized
continuation and no campaign hold. Repeat the exact execution command for the
next case; later execution across providers selects `--provider openai --provider
anthropic --provider gemini`, still loading only the current case's key. Omit the
limit only when deliberately continuing the remaining suite. Completed cases are
never submitted again. If handoff is uncertain, retain state and use `study recover`
with the same proposal, digest, authorization, proofs and campaign; it reconciles
consumed identities only and cannot create replacement calls. A hold, cancellation,
damaged journal/checkpoint or expired deadline requires independent reconciliation,
not a fresh campaign or an automatic retry.
