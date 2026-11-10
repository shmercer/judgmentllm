# Vertex live qualification proposal: deferred, unexecuted

This proposal namespace belongs solely to Vertex Phase 5D1. The production native
and model qualification registry remains empty. No live qualification was
attempted and no executor, execution authorization, request allocation or spending
is supplied by this phase.

The offline review establishes SDK serialization and routing, but cannot establish
an eligible project/account, key binding or ADC identity without crossing the
explicit no-credential/no-discovery boundary. Current documentation has changed
since the pinned SDK and does not prove this user's model eligibility. **No exact
serving model is selected.** Do not turn a documented model name or a Developer
API success into an executable Vertex scope.

After committing the final adapter source, create a separate immutable proposal:

```bash
python scripts/qualify_vertex.py plan --source-commit FULL_COMMITTED_SOURCE_SHA \
  --auth-mode adc --location us-central1 \
  --evidence-dir /external/new-vertex-offline-proposal
```

The script accepts only `plan`, reads local Git metadata, requires clean exact
source, uses explicit nonsecret options, and writes a new exclusive
`proposal.json` plus its SHA-256. It never constructs an SDK client, inspects a key
or ADC path, discovers resources, reads keyring, or calls a provider. Existing
proposal directories cannot be replaced. Select `--auth-mode api_key` separately
if that is the intended future mode; no approval transfers between proposals.

The version-1 proposal retains provider `vertex`, committed source, mode,
`v1beta1`, location, google-genai 2.34.0 and `models.generate_content`.
The desired four cases are one-trait ordinal rubric text/native and forced-choice
pairwise text/native over synthetic character IDs `001`/`002`, trait `01`.
The native candidate is only `vertex.generate_content.response_json_schema`;
`response_schema` remains a separate mechanism. Four requests at most, one
candidate, concurrency one, zero retries and at most 512 requested output tokens
per case describe the eventual proposed ceiling. **Zero calls are authorized.**

`serving_model`, input reservation and pricing/cost estimates remain null.
Physical requests and their hashes are empty, explicitly, because a complete
resource/model scope is unavailable. The proposal cannot be executed. Once
prerequisites are established, prepare a fresh exact-source plan with all four
complete physical bodies and hashes, rather than editing this deferred record.

Before any later authorization:

- Establish project/billing, explicit ADC or standard authorization-key mode,
  location, account eligibility, API restrictions, service-account binding and
  necessary IAM independently.
- Review current official exact-model availability, request limits, selected
  native mechanism and `v1beta1` compatibility for that mode/location. Pin the
  requested and acceptable reported identity; aliases require reviewed evidence.
- Audit complete request/schema bytes, conservative input/framing allowances,
  output/thinking limits, timeout/cancellation behavior and current standard
  input/output/thinking prices. Assume no cache or batch discount. Record unknown
  billing classes and no provider-enforced monetary cap.
- Obtain full synthetic assessment capture authorization with reasoning retention
  none. Independently review any future source/plan-bound one-shot executor,
  consumption ledger and billable authorization before issuing requests.

No Gemini Developer digest, plan, response, authorization, profile or live evidence
is reused. Implementation acceptance, this deferred proposal, later live execution
and eventual production capability promotion are separate reviews.
