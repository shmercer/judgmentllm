# Provider and endpoint compatibility (design target)

**As of 2026-11-02.** A design commitment is not the same as a qualified model/endpoint combination. Current per-model/region/account status belongs in generated `model-support.md` with evidence dates.

| Adapter | Live | Native provider batch | Managed bulk-live | Rubric | Pairwise | Notes |
|---|---|---|---|---|---|---|
| OpenAI Chat Completions | target | target | yes | generative | generative | Use official OpenAI SDK; model-dependent structured output and tiers |
| OpenAI Responses | target | target | yes | generative | generative | Use official OpenAI SDK; model-dependent structured output and tiers |
| OpenAI Decisions | target | not available | yes | typed score, 2-10 levels | typed choice | Currently GPT-6 Luna; no generated explanations; 1-400 questions/request; cache policy handled separately |
| Anthropic Messages | target | target | yes | generative | generative | Official SDK; token counting/batch/caching controls may vary |
| Gemini Developer | target | target | yes | generative | generative | `google-genai` SDK; batch via generateContent |
| Vertex AI Gemini | target | target (Google Cloud setup required) | yes | generative | generative | `google-genai` SDK; Gemini focus; GCS/BigQuery source and cloud IAM considerations |
| Together AI | target | target, subject to current API qualification | yes | generative | generative | Use official Together Python SDK v3 (`together>=3,<4`); endpoint/model feature coverage must be tested |
| Ollama local | target | no native provider batch | yes | generative | generative | Official Ollama SDK, local daemon; local model discovery; optional later cloud support separate |
| TypeSafe AI Jev | target | not available | yes | typed score, 2-10 levels | typed choice | Official `typesafe-sdk` confirmed; no generated explanations |

### Required capability checks

Each provider endpoint or model may differ in:

- Support for `scores_only` and `scores_with_explanations`.
- Number of levels, traits/questions per request and maximum context/output size.
- Structured JSON/schema generation (generative only) or native typed scoring output.
- Exact service tier names, eligibility and fallback behavior.
- Native batch availability, limits, input storage dependencies, polling and result ordering.
- Prompt-cache controls, token threshold, TTL, automatic/implicit behavior and usage reporting.
- Token-counting APIs and available accounting/cost data.
- Model listing and reported capability metadata.

### Evidence classifications

- `discovered`: appears in documented catalog or model list.
- `implemented`: compatible adapter present and local validation available.
- `fixture_tested`: provider-shaped synthetic fixtures pass.
- `live_qualified`: an opt-in real test passed with provider + endpoint + model + feature + date.

Unknown capabilities remain unknown, not false and not true. Avoid hardcoded allowlists blocking unknown but valid model IDs. Provider/model id alone does not prove support for a tier or batch mode.

### Qualification source links

- OpenAI Decisions: https://developers.openai.com/api/docs/guides/decisions
- OpenAI Batch: https://developers.openai.com/api/docs/guides/batch
- Anthropic Batches: https://platform.claude.com/docs/en/api/messages/batches/create
- Google Gen AI SDK: https://googleapis.github.io/python-genai/
- Gemini Batch: https://ai.google.dev/gemini-api/docs/batch-api
- Vertex Gemini batch: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/samples/generativeaionvertexai-batch-predict-gemini-createjob
- TypeSafe Python SDK: https://docs.typesafe.ai/sdk/python/
- Ollama local: https://github.com/ollama/ollama/blob/main/docs/api/introduction.mdx
- Together provider documentation: https://docs.together.ai/
- Together SDK v3 release notes: https://github.com/togethercomputer/together-py/releases/tag/v3.0.0
