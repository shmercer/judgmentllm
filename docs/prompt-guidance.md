# Prompt templates: simple by default, customizable when needed

## Recommended UX

Most users should need no template file at all: supply a rubric and choose `rubric_scores_only`, `rubric_scores_with_explanations`, or `pairwise_forced_choice`. The package builds a correct prompt and structured response contract.

Users wanting wording control can copy an ordinary text template:

```text
You are an expert writing assessor. Score the response using these criteria.

{{ rubric }}

Examples:
{{ exemplars }}

Student response:
{{ response }}

{{ output_instructions }}
```

`{{ rubric }}` and `{{ exemplars }}` are pre-rendered human-readable blocks. They do not require users to write loops. `{{ output_instructions }}` contains helpful requested-output language, but **the actual expected output is separately validated by the package**.

For independent trait runs, `{{ trait }}` can contain the complete name/description/levels block. For grouped tasks, `{{ rubric }}` contains the configured trait group, not necessarily every trait stored in the rubric.

## Beginner-friendly customizations

- Supply a `.txt` or `.j2` template file, or choose built-in `preset`.
- `judgmentllm templates init --preset rubric_scores_only --output rubric_prompt.txt` copies a starting point.
- `judgmentllm templates preview --config run.yaml --item-id essay_001 --trait organization` prints one rendered prompt offline (redact or use a separate private output file if text is sensitive).
- Provide clear errors for missing/unknown placeholder names. Use stable replacement data structures, not access to live Python objects.

## Advanced customizations

Allow documented Jinja2 `{% for %}`, `{% if %}` functionality for cases needing custom layout of traits/examples. Use a `SandboxedEnvironment`, `StrictUndefined`, limited safe filters and plain structured input. Disable templates from accessing files, network, Python classes, attributes/methods beyond the documented context. Put a cap on render effort/size suitable for local use.

Custom prompt text changes assessment instructions, but cannot override required output validation and adapter restrictions. For OpenAI Decisions and TypeSafe Jev, prompts populate typed question instructions; the provider wire schema is built by the adapter, never by a user JSON file.

## Prompt caching layout

For reusable rubric scoring, built-in templates arrange invariant instructions + trait criteria + fixed exemplars before the changing student response when API semantics permit. This increases opportunities for prefix caching. Custom templates may choose a different order; `plan` can explain whether a reusable prefix was detected.

Default rubric `cache_policy: auto`, pairwise `cache_policy: off`. Provider-side implicit caching might be uncontrollable. The report must distinguish library cache settings from observed provider token savings.

## Exemplars and context

No automatic exemplar selection: print a helpful error with the request ID, estimated or verified input tokens, known provider limit and steps to reduce the load. If limits are unknown, show an honest warning; strict mode may refuse to send. Never claim exact token counts from a heuristic estimator.
