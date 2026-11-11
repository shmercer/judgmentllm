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

## Phase 5D2a: separate complete offline candidate plans

The Phase 5D1 proposal above and its version-1 command/records are preserved
unchanged. Its `us-central1` example is historical deferred intent, not evidence
that this exact model serves there. Phase 5D2a adds independent **plan version 2**,
`vertex-offline-executable-candidate-v2`. “Executable candidate” describes complete
request preparation; execution remains disabled and **authorized requests = 0**.
There is no Vertex qualification executor in this phase.

The initial candidate for **each of `api_key` and `adc`** is provider `vertex`,
endpoint `models.generate_content`, SDK `google-genai==2.34.0`, API `v1beta1`,
model `gemini-3.1-flash-lite`, location **`global`**, standard project-based route:

```text
https://aiplatform.googleapis.com/v1beta1/projects/[private-project]/locations/global/publishers/google/models/gemini-3.1-flash-lite:generateContent
```

The [model card](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-1-flash-lite)
documents GenerateContent, structured output and locations `global`, `us`, `eu`.
The adapter's broader regional host allowlist establishes transport syntax, not
exact-model availability. Neither `us` nor `eu` is added to that allowlist here.
The [versioned REST reference](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1beta1/projects.locations.publishers.models/generateContent)
retains the full project/location/publisher resource. Documentation and successful
offline serialization establish no project eligibility or successful access.

Each separately sealed plan contains exactly these cases:

| Case | Assessment | Output selection |
|---|---|---|
| `rubric-text` | Item `001`, trait `01`, categories low/middle/high coded 1/3/8 | Complete text JSON schema |
| `rubric-native` | Same independent single-trait rubric | `vertex.generate_content.response_json_schema` |
| `pairwise-text` | Items `001`/`002`, trait `01`, presented `002`/`001`, forced winner | Text JSON |
| `pairwise-native` | Same forced choice | `vertex.generate_content.response_json_schema` |

There are no explanations, grouped rubrics, ties, alternative schemas or models.
Canonical local validation remains mandatory. `response_schema` stays unqualified.
Native preparation uses the existing qualification-only context, never a production
grant. The Vertex production registry remains empty. Neither ordinary run/resume,
the Developer qualification executor, nor a rehashed production context can consume
these candidate commitments as authority.

## Construct the final immutable plans after merge

Use a clean checkout of the merged source, with the pinned SDK already installed.
`GOOGLE_CLOUD_PROJECT` must explicitly identify the intended standard-route project;
do not infer it from a key, ADC, `gcloud` or discovery. Keep private evidence outside
every source checkout and outside synced/public folders. Use two new directories,
one per mode, with private parent ACLs. On POSIX the generator creates mode 0700
directories and 0600 files; on Windows select equivalent private ACLs yourself.

```bash
# Select the actual intended project locally; never commit its value.
export GOOGLE_CLOUD_PROJECT='YOUR_EXPLICIT_PROJECT_ID'
SOURCE=$(git rev-parse HEAD)
python scripts/qualify_vertex.py candidate-plan --source-commit "$SOURCE" \
  --auth-mode api_key --location global --model gemini-3.1-flash-lite \
  --evidence-dir /external/private/new-vertex-key-plan
python scripts/qualify_vertex.py candidate-plan --source-commit "$SOURCE" \
  --auth-mode adc --location global --model gemini-3.1-flash-lite \
  --evidence-dir /external/private/new-vertex-adc-plan
```

Each command prints **only its plan SHA-256**. Preserve that digest independently
for review. No key, ADC credential, Google Cloud CLI, keyring access or network
is needed. Planning never reads `GOOGLE_API_KEY`, `GEMINI_API_KEY`,
`JUDGMENTLLM_VERTEX_API_KEY` or an ADC credential path. API-key mode records only
the credential-source name; ADC records `google_adc`. Existing credentials need
not be loaded into the environment for these commands.

Do not overwrite or edit a sealed directory. A source, project, mode, model,
route, generation setting or request change requires a fresh directory, complete
reconstruction, new digest and independent review. The physical run/attempt/
submission/correlation identities bind source and the mode/project/location route.
One mode's later success or consent cannot qualify or authorize the other. Each
plan has four intended requests; two plans do not create an eight-call grant.

The private `plan.json` retains the project and complete publisher resource,
original configuration-file hashes, configuration/dependency/schema/prompt/payload/
transport commitments, exact prompt/body bytes, SDK source profile and price
evidence hashes, requested controls and a hash inventory of all preparation
artifacts. The original synthetic inputs, configurations and immutable preparation
stores are retained alongside it. No credentials are retained.

## Independent read-only verification

Check out the plan's exact committed source with a clean working tree, including
untracked files. Supply the independently retained expected digest explicitly:

```bash
python scripts/qualify_vertex.py verify \
  --evidence-dir /external/private/new-vertex-key-plan \
  --plan-sha256 INDEPENDENTLY_RETAINED_FULL_SHA256
```

At Phase 5D2a, `report` was an alias for this read-only verification. Phase 5D2b
preserves `verify` and adds execution inspection to `report`, as described in the
[executor guide](vertex-qualification-executor.md). The verifier checks the
canonical plan bytes/digest, exact clean source, installed SDK version/source
hashes, complete artifact inventory, fixed scope, request limits and zero authority.
It independently reconstructs each dependency, physical identity, strict contract,
prompt and complete transport commitment. The installed SDK's converters and URL
builder then reproduce the complete HTTP body bytes and full global resource on
an inert route record. This does not construct any SDK client or call a request,
authentication or send method. Separate tests compare every case/mode to actual
SDK HTTP intercepted entirely in memory using synthetic credentials/projects.

Reporting opens no mutable preparation store and writes no evidence. Extra grant
files, changed artifacts, duplicate JSON keys, rehashed incompatible requests and
changed routes/identities are rejected. Tests compare evidence hashes and mtimes
before/after success and failure. Public JSON contains only hashes, fixed scope,
synthetic case names, redacted routes and cost assumptions. It omits the private
project, complete resource, evidence paths, prompts, bodies and credentials. Keep
the full seal private; hashes are commitments, not anonymization of guessable IDs.

## Vertex reservations and independently reviewed pricing

The [Vertex standard global price table](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing)
(currently redirected to [Model Garden pricing](https://cloud.google.com/model-garden/pricing))
was reviewed separately from Developer API pricing on 2026-11-10. Standard text
input is **$0.25/M tokens**; text output **including thinking/reasoning** is
**$1.50/M tokens**. The [dated evidence](vertex-qualification-price-evidence.json)
is hash-bound into each plan. Re-review before future billable authorization.

At 512 requested output tokens per case, the complete Vertex bodies give the
following reservations, independently for each authentication mode:

| Case | Complete body bytes | Additional native schema bytes | Estimated reserved input tokens | Reserved output tokens including thinking | Estimated USD |
|---|---:|---:|---:|---:|---:|
| rubric-text | 1,318 | 0 | 6,144 | 512 | 0.002304 |
| rubric-native | 1,489 | 486 | 6,144 | 512 | 0.002304 |
| pairwise-text | 950 | 0 | 5,120 | 512 | 0.002048 |
| pairwise-native | 1,088 | 184 | 6,144 | 512 | 0.002304 |
| **Per plan** | **4,845** | **670** | **23,552** | **2,048** | **0.008960** |

The allowance counts one estimated token per complete UTF-8 body byte, counts
native schema bytes again, adds 4,096 framing/injected-instruction tokens per
case and rounds up to 1,024. It has no copied Developer reservation or 8,192-token
floor. SDK body framing, schemas and requested controls all enter the calculation.
The total reservation is **25,600 tokens** per plan. Actual tokenization, provider
framing, usage and dollars remain unknown; these are not verified tokenizer
counts, enforced input limits or billing caps. No zero-thinking assumption, cached
token saving, free usage, batch/priority discount or promotional credit is used.

The **proposed estimated authorization ceiling is $0.02 per plan**, while the
actual authorized request count remains **zero**. If the computed conservative
reservation exceeds $0.02, preparation reports the needed revision and does not
seal a plan or increase the ceiling. Maximum intended requests are four, sequential,
one candidate, at most 512 requested output tokens each; SDK retries, automatic
continuation and automatic function calling are disabled. No thinking controls
or function/tool requests are added. The 512-token output reservation includes
thinking; it does not prove serving enforcement or actual billing behavior.

## Outstanding cloud setup and future authority

The operator now reports Google Cloud CLI installed, user ADC created with OAuth
token acquisition verified, a matching quota project, `aiplatform.googleapis.com`
enabled, `GOOGLE_CLOUD_LOCATION=global`, and `JUDGMENTLLM_VERTEX_AUTH_MODE=adc`.
The separate Vertex key remains in GNOME Keyring. These are operator-reported
setup facts, not independent verification by this work or prerequisites for
offline use. No installation or configuration is needed here. This PR never
inspects credential/keyring contents, runs `gcloud auth`, resolves ADC, refreshes
OAuth, queries metadata, lists projects/models or performs a live authentication
check. Setup does not establish billing, inference IAM or model eligibility.

Account eligibility and API-key type/project association stay **unverified** for
both modes. Before any later live work, the separately approved cloud-setup
process must establish the intended project, billing, enabled API, IAM permissions,
API restrictions, model access and the chosen credential's compatibility. For
API-key mode, independently establish that the key is a service-account-bound
authorization key for the standard project endpoint. **If it is an Express Mode
key, stop and report incompatibility.** Do not redirect to Express, fall back to
Developer, or change modes. ADC requires its own independently established
principal/permissions and separately sealed plan/consent.

A future Vertex executor needs a separate implementation, review and merge,
one-shot consumption controls, exact plan/source-bound billable authorization and
full synthetic assessment-capture consent. Qualification and eventual reviewed
production promotion remain distinct later reviews. Passing offline fixtures or
CI establishes neither live Vertex qualification nor eligibility. Historical
Developer, Anthropic, OpenAI and deferred Vertex evidence is never rewritten.

## Phase 5D2b: executor implementation

The [dedicated executor guide](vertex-qualification-executor.md) documents the
new source/plan/mode-bound consent command, separately reviewed reservations,
fixed sibling consumption ledger, final HTTP guard, conservative recovery and
read-only reports. Its implementation and adversarial tests are offline only.
Generate new plans from the final merged source; the original Phase 5D2a plans
remain immutable historical evidence and are not execution authorizations.
