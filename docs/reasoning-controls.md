# Typed reasoning controls

RunSpec V3 accepts optional `generation.reasoning.effort` and
`generation.reasoning.mode`. `ReasoningSpec` is frozen and rejects unknown keys,
values and scalar conversions. Absent, null and empty reasoning objects mean no
requested control. The string `none` is an explicit effort; it is not null.

```yaml
generation:
  max_output_tokens: 8192
  reasoning:
    effort: none
```

The existing `temperature`, `top_p` and `max_output_tokens` fields keep their
numeric types and omission rules. New reasoning controls require schema version
3. V1/V2 configurations, omitted-control serialized bytes and historical sealed
requests keep their original identities. Every explicit control enters the
generation digest, logical request context, configuration and exact physical
payload. Changing a control cannot resume or retry the original sealed request.

## Provider controls and evidence

| Provider/route | Typed effort to wire | Mode to wire |
|---|---|---|
| OpenAI Responses, openai 3.42.1 | `reasoning.effort` | `reasoning.mode` |
| Anthropic Messages API 2026-10-21, anthropic 2.3.0 | `output_config.effort` | `thinking.type` |
| Gemini Developer GenerateContent v1beta, google-genai 2.34.0 | `thinking_config.thinking_level` to `generationConfig.thinkingConfig.thinking_level` | Unsupported |

The canonical Gemini REST spelling is `thinkingLevel`. The pinned SDK's typed
`ThinkingConfig` retains the original proto spelling `thinking_level` inside
`thinkingConfig`. The HTTP guard commits the actual SDK bytes, without rewriting
the request or injecting provider JSON. Both names are recorded in control
evidence. [ProtoJSON accepts original and lower-camel field names](https://protobuf.dev/programming-guides/json/#field-names-as-json-keys);
this representation and SDK-byte evidence do not establish serving acceptance.

The implementation records exact documented/fixture-tested control facts for
the six Issue #39 models. Facts bind provider/model/route, pinned SDK/API,
mechanism, declared account and requested tier. They do not authenticate the
account, establish entitlement or promote production native qualification.
Unknown model IDs remain permitted; explicit reasoning with unknown support
fails preflight. OpenAI Chat, Vertex and other routes inherit no reasoning
support from these records.

| Exact model | Documented omitted default | Study contrast |
|---|---|---|
| `gpt-6-luna` | medium / standard | effort `none`, mode omitted |
| `gpt-6.1-terra` | medium / standard | effort `none`, mode omitted |
| `claude-haiku-5-5` | medium / adaptive | effort `low`, mode omitted |
| `claude-sonnet-5-5` | high / adaptive | effort `low`, mode omitted |
| `gemini-3.5-flash-lite` | minimal | effort `high` (SDK `HIGH`) |
| `gemini-3.8-flash` | medium | effort `low` (SDK `LOW`) |

Defaults are dated documentation metadata, never inserted into omitted requests.
The effort labels are not a cross-provider measurement scale. OpenAI Pro mode,
Anthropic disabled thinking and between-tools thinking are separate mode controls.
Between-tools and Gemini minimal/low thinking do not mean full reasoning off.
Haiku disabled and Sonnet between-tools require effort high or below.

On the two OpenAI models, explicit sampling requires effort `none`; omitted
effort uses the documented medium default and cannot enable custom sampling.
Anthropic's current API rejects explicit sampling at every thinking mode. Gemini
retains its numeric bounds; its guidance discourages changing default temperature.
The separate temperature proposal uses only the eligible OpenAI models, explicit
effort `none`, and omitted versus `temperature: 0.2`, with top-p omitted.

References reviewed 2026-11-13:
[OpenAI model defaults](https://developers.openai.com/api/docs/models/gpt-6-luna),
[Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra),
[reasoning](https://developers.openai.com/api/docs/guides/reasoning),
[sampling restrictions](https://developers.openai.com/api/docs/guides/latest-model),
[Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort),
[thinking modes](https://platform.claude.com/docs/en/build-with-claude/thinking),
[Messages](https://platform.claude.com/docs/en/api/messages/create),
[Gemini GenerateContent thinking](https://ai.google.dev/gemini-api/docs/thinking?generate-content=true).

## Commitments, privacy and qualification

An optional `ReasoningPolicyV1` freezes requested intent and exact control facts
in the existing output commitment; its digest is already bound by the physical
transport receipt. Omission serializes without this record. V1 interpretation
and SDK mapping are frozen; a future interpretation needs a new policy version.
Anthropic effort merges with its native schema format. Gemini receives a typed
SDK thinking configuration. Final HTTP bytes must equal the sealed payload.

Production native profiles need a separately reviewed reasoning scope binding
effort/mode, sampling omission or exact values, account, tier and demonstrated
assessment features. Historical native profiles authorize no new explicit effort
or temperature combination. This implementation installs no reasoning scopes.
Offline source-bound candidate preparation grants no execution authority.

Private reasoning text, summaries and signatures are not retained. Reported
reasoning/thought token counters remain usage metadata, with missing values
unknown. Assessment explanations remain ordinary rubric outputs. Normalized
result/CSV/R columns do not change; generation/provenance records retain safe
requested-control metadata. The existing strict R generation gate consumes
separately persisted Python-produced omission/effort/temperature records.

PR2-A is high risk: exact-source local coverage/historical, seven strict R gates,
applicable resource/durability checks, and protected-base Linux/Windows/macOS
functional acceptance are required. Offline fixtures use synthetic keys and
mock HTTP; no live call or qualification is authorized.
