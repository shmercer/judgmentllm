# Assess synthetic responses with OpenAI

JudgmentLLM requires Python 3.11 or newer on Linux, macOS or Windows. Phase 5A
implements OpenAI Responses and Chat Completions. Serving-model qualification is
separate: this implementation does not claim that every model accepts every
parameter or native JSON Schema.

**Every new OpenAI execution requires explicit `schema_version: 3`, including
text-only runs.** Historical V1/V2 configurations keep their original offline
behavior and cannot execute through the production adapters. The sealed manifest
identifies the production protocol separately from the configuration version.

Install the optional provider SDK in your virtual environment:

```bash
pip install 'judgmentllm[openai]'
judgmentllm --help
```

The package has not yet been published to PyPI by this workstream. Until an alpha
is published, install the reviewed wheel with its `[openai]` extra. Installing,
importing and displaying help do not contact OpenAI.

Set `OPENAI_API_KEY` in the current process using your operating system's secure
credential tooling. On macOS/Linux, an interactive Bash prompt avoids putting the
key in shell history:

```bash
read -rsp 'OpenAI API key: ' OPENAI_API_KEY; export OPENAI_API_KEY; printf '\n'
```

For PowerShell, read a secure value and expose it only to this process:

```powershell
$secureKey = Read-Host 'OpenAI API key' -AsSecureString
$env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new('', $secureKey).Password
```

Keep keys out of YAML, source files and command arguments. JudgmentLLM reads only
its documented environment source; it does not load `.env`. `auth status` checks
configuration offline. Explicit `auth verify --provider openai` and
`providers models --provider openai` contact the protected models-list endpoint
without generating assessments. Authentication acceptance or model visibility
establishes neither generative endpoint eligibility nor native-schema support.

The repository includes complete synthetic examples in
[examples/openai](../examples/openai/). Copy this directory to your own working
folder. Replace `YOUR_EXACT_MODEL` in the configuration with the exact model you
intend to use. Review its endpoint, output-token and pricing documentation.
`endpoint: responses` selects Responses; `endpoint: chat.completions` selects Chat.
No automatic model or endpoint substitution occurs.

## Preview and score

These commands are offline and do not incur inference charges:

```bash
judgmentllm validate --config examples/openai/rubric.yaml --json
judgmentllm plan --config examples/openai/rubric.yaml --json
judgmentllm templates preview --config examples/openai/rubric.yaml --item-id 001 --trait 01
```

The example uses a three-category ordinal rubric with numeric values 1, 3 and 8.
A generated category is stored with its coded numeric value. It does not create
probabilities or an expected score. The pairwise example makes a forced choice
between the two original item IDs.

**The following run commands submit billable OpenAI requests.** Review the exact
prompt, model, requested output limit and account pricing before executing them:

```bash
judgmentllm run --config examples/openai/rubric.yaml --store ./openai-runs --json
judgmentllm run --config examples/openai/pairwise.yaml --store ./openai-runs --json
```

Record each returned run ID. Inspect and export without making provider calls:

```bash
judgmentllm runs status --run RUN_ID --store ./openai-runs --json
judgmentllm results export --run RUN_ID --store ./openai-runs --format jsonl --json
judgmentllm results export --run RUN_ID --store ./openai-runs --format csv --view r --json
```

The same services are available in Python:

```python
from judgmentllm import export_run_results, get_run, run_live

summary = run_live("examples/openai/rubric.yaml", store="./openai-runs")
status = get_run(summary.run_id, store="./openai-runs")
exported = export_run_results(summary.run_id, store="./openai-runs", format="jsonl")
```

Use the asynchronous `arun_live` counterpart inside an active event loop.

## Output modes, interruptions and privacy

`prompt.structured_output: text` supplies complete output instructions and performs
strict local assessment validation. `native` additionally requires sufficient
controlled evidence for the exact endpoint, model, SDK/API scope and features.
`auto` selects native only when eligible; unknown scopes follow the existing text
and strict-capability policy. Unknown is an explicit condition, and
`strict_capabilities: true` can reject unresolved controls. Credential presence,
model names and caller-supplied evidence never create native support. The initial
trusted registry contains no live-qualified models; ordinary native mode remains
unavailable until exact-scope records pass deliberate review. `auto` therefore
uses text for unqualified scopes when the policy permits it.

Output limits map to `max_output_tokens` on Responses and
`max_completion_tokens` on Chat. Omitted temperature/top-p controls stay omitted.
Unsupported requested settings fail rather than being silently removed. Each
assessment uses one nonstreaming SDK call with bounded timeouts and zero SDK
retries. Invalid JSON, duplicate keys, incorrect categories, refusals and truncated
outputs remain failed attempts. JudgmentLLM does not repair or automatically rescore
these completions.

After an interruption, use the original configuration and run ID:

```bash
judgmentllm resume --run RUN_ID --store ./openai-runs --config examples/openai/rubric.yaml --json
```

Resume can submit untouched prepared work, so it can incur charges. It never
replaces a submission whose acceptance is uncertain. Both endpoints initially use
`store=false`; do not expect server retrieval. Trustworthy retained response
evidence can be normalized after a restart. If that evidence is unavailable,
status remains unresolved. A handled worker failure preserves available responses
and known invocation evidence where storage remains writable, while retaining the
original failure. Preserve the original store and inspect the recorded status
before authorizing any explicit new attempt.

Keep the SQLite runtime store on a local disk. On Windows, choose a short store
and qualification directory, such as `C:\jllm`, to leave room for immutable
artifact paths under the platform path limit. Full capture retains prompts,
nonsecret request bodies and sanitized responses locally. Minimal capture omits
these bodies but retains normalized results, which may include requested
explanations. Provider reasoning requires separate authorization and is excluded
from ordinary capture. `store=false` controls the API storage option; consult
OpenAI's current account-specific data policies for its broader handling of data.
Usage is reported once per physical request. Cache and reasoning counters overlap
with their documented parent counters and must not be added again. Unknown usage
or costs remain unknown. Pairwise `cache_policy: off` is local intent; this adapter
cannot disable provider-imposed implicit caching and reports cache behavior unknown.

If an extra or key is missing, install `[openai]` or configure `OPENAI_API_KEY`.
For rejected parameters, review exact-model documentation and change the next
run's configuration deliberately. For native-admission errors, use text mode or
wait for reviewed exact-scope qualification. Preserve unresolved runs and their
evidence; a timeout does not prove that OpenAI rejected the request.

## Qualification is separately authorized

Ordinary tests and CI never call OpenAI. The repository harness
`scripts/qualify_openai.py` has three explicit stages: `plan`, `execute`, and
read-only `report`. Planning writes eight exact synthetic request commitments:
rubric and forced-choice pairwise, each in text/native mode on both endpoints.
It requires an explicit model, reviewed context/pricing profile, output limit,
request cap, overall token/spending caps and a dedicated new evidence directory.

The budget conservatively reserves the complete documented model context as
possible input plus each output bound. It assumes the cited ordinary input/output
rates without cache discounts. It is an estimated ceiling under those rates;
account terms, pricing changes and taxes can differ.

After exact-head offline qualification and PR review, the reviewer must separately
authorize the source commit and plan digest before the billable `execute` stage.
The shared ledger consumes each planned request at most once. Continuation never
adds replacement calls; reports cannot promote their own model-support status.
Reports distinguish accepted, rejected and unresolved requests from completion
status and local assessment validity. Missing acknowledgment never proves rejection.
A successful sample qualifies only its independently reviewed exact feature scope.
No live calls are authorized by this guide or by approval to implement the adapter.
