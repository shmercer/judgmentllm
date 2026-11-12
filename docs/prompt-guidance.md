# Offline prompt templates

Milestone 1 supplies three presets, custom text rendering, and offline preview. These functions never submit requests, require credentials, or construct provider API payloads.

| Preset | Assessment output |
|---|---|
| `rubric_scores_only` | One category per selected trait |
| `rubric_scores_with_explanations` | One category and explanation per selected trait |
| `pairwise_forced_choice` | Exactly one winning item, with an explanation if requested |

Rubric presets must agree with `execution.explanations`. Forced choice rejects `allow_ties: true`; a custom pairwise template can request ties using the separately validated tie contract. Endpoint compatibility remains a preflight concern: selecting an explanation preset cannot enable explanations on a score-only endpoint.

For OpenAI Decisions score questions, preflight enforces the documented 2–10 ordered-level range. This endpoint rule cannot be widened by a caller-supplied capability profile; a stricter supplied limit still applies. The provider-neutral rubric schema remains unrestricted above its two-level minimum. Other endpoints do not inherit this rule. Independently checked TypeSafe documentation and the distinction between documented facts and implemented endpoint checks are recorded in the [provider matrix](provider-matrix.md); supply verified capability facts rather than assuming identical limits. The [official OpenAI Decisions guide](https://developers.openai.com/api/docs/guides/decisions#score-against-a-rubric) was checked on 2026-11-02; this local check is not a provider implementation or live qualification claim.

## Python and CLI interfaces

```python
from judgmentllm.config import load_run_spec
from judgmentllm.preview import preview_prompt
from judgmentllm.prompts import get_template, init_template, list_templates

presets = list_templates()  # list[TemplateInfo], with preset and description
source = get_template("rubric_scores_only")  # installed UTF-8 text resource
init_template("rubric_scores_only", "my_prompt.txt")  # refuses overwrite

spec = load_run_spec("examples/run.yaml")
preview = preview_prompt(spec, item_id="essay_001", trait_id="organization")
print(preview.text)
print(preview.exemplar_ids)
print(preview.output_schema)

pair_preview = preview_prompt(
    "examples/pairwise.yaml", pair_uid="comparison_001_original"
)
```

`preview_prompt` accepts a resolved `RunSpec` or configuration path. A trait selects its complete planned request group. Independent runs with multiple traits therefore require `trait_id`. Pairwise preview requires `pair_uid`. Relative file paths resolve against their containing configuration, independent of the current directory.

For direct use, `render_prompt` accepts a `response`, a `rubric` or explicit `traits` sequence, and optional `exemplars`. Supply `traits` with the full rubric to preview a specific group while validating exemplar annotations against all stored traits. Pairwise rendering instead accepts `pair`, `left`, `right`, and `criterion`, with item IDs matching the original pair orientation. `preset` and custom `template` text are mutually exclusive. `RenderedPrompt` carries `schema_version: 1`, exact `text`, ordered `exemplar_ids`, and a separate `output_schema`.

```bash
judgmentllm templates list
judgmentllm templates init --preset rubric_scores_only --output my_prompt.txt
judgmentllm templates preview --config examples/run.yaml --item-id essay_001 --trait organization
judgmentllm templates preview --config examples/pairwise.yaml --pair-uid comparison_001_original
judgmentllm templates preview --config examples/advanced.yaml --item-id essay_001 --trait evidence
```

Success exits 0. Configuration, file, selection, and render errors exit 2 with a diagnostic. Preview writes prompt text to stdout and preflight warnings to stderr. The Python preview returns text without printing warnings; inspect `validate_run(spec)` for the structured validation report. Milestone 1 implements no execution CLI commands.

## Simple custom text

Use a text file containing only ordinary text and `{{ name }}` substitutions:

```text
You are a careful assessor. Apply every supplied criterion.

{{ rubric }}

Reference exemplars:
{{ exemplars }}

{{ output_instructions }}

Student response:
{{ response }}
```

Configure `prompt: {file: my_prompt.txt}`. A `.j2` extension does not itself enable advanced mode. Placeholder names are case sensitive; unknown names fail, including those in conditional branches that would not run.

| Placeholder | Content |
|---|---|
| `rubric`, `trait`, `criterion` | Aliases for the complete selected criteria group; includes shared rubric guidance when supplied |
| `response` | Identified rubric response and its complete original text |
| `exemplars` | All supplied exemplars, with IDs, full text, scores, rationales, and metadata, in selected order |
| `output_instructions` | Required output instructions and the separately enforced JSON schema |
| `first_response` | First **presented** pairwise response; preferred position name |
| `second_response` | Second **presented** pairwise response; preferred position name |
| `left_response`, `right_response` | Compatibility aliases for `first_response`, `second_response`, respectively; these do **not** mean original left/right orientation |

The built-in pairwise template and new custom templates use `first_response` and `second_response` to name presentation positions. Existing `left_response`/`right_response` templates retain their original behavior, following presentation order even after reversal. The identified blocks retain actual item IDs. Python render arguments `left` and `right` always refer to original item orientation, matching `PairTask.left_item_id` and `right_item_id`; `PairTask.presented_order` chooses which text is first. For original `[A, B]` presented as `[B, A]`, `first_response` (and legacy `left_response`) contains B, while `second_response` contains A. No response placeholders are available for the other assessment kind.

```text
{{ criterion }}
{{ exemplars }}
{{ output_instructions }}
{{ first_response }}
{{ second_response }}
```

Every response, selected criterion, selected exemplar, and output-instructions block must appear exactly once and remain unchanged. Thus, a rubric template needs `response`, one criteria alias, and `output_instructions`, plus `exemplars` when any are supplied. Pairwise templates need both presented responses, criteria, and output instructions. Missing or repeated blocks fail. Filters, statements, expressions, and loops require explicit advanced mode.

## Advanced Jinja layout

Set `prompt.advanced: true` together with `prompt.file`. Rendering uses Jinja2 `SandboxedEnvironment` and `StrictUndefined`, with a small syntax allowlist rather than a separate interpreter. See `examples/advanced.yaml` and its template.

```jinja2
{% if explanations %}Explain each category choice.{% endif %}
{% for entry in traits %}
{{ entry['block'] }}
{% endfor %}
{% for entry in exemplar_items %}
Reference {{ entry['exemplar_id'] }}:
{{ entry['block'] }}
{% endfor %}
{{ output_instructions }}
{{ response }}
```

Besides the simple placeholders, advanced context provides:

- `traits`: ordered dictionaries with `trait_id` and `block`; for pairwise tasks this contains the one criterion. Shared rubric guidance prefixes the first trait block.
- `exemplar_items`: ordered dictionaries with `exemplar_id` and `block`.
- `explanations` and `allow_ties`: boolean configuration values.

All context values are plain strings, booleans, lists, and dictionaries. Loops must iterate directly over `traits` or `exemplar_items`, use a new variable name, and cannot be nested or recursive. Literal subscriptions such as `entry['block']` are supported; dot attributes and dynamic subscript expressions are rejected. Conditions support basic comparisons and `and`, `or`, and `not`. Available filters are `length`, `lower`, `upper`, and `trim`; applying a filter that changes a protected block fails content validation. These filters are useful for surrounding headings and identifiers.

Calls, Python attributes/methods, arithmetic, assignments, macros, imports, includes, inheritance, Jinja globals, filesystem loaders, and `loop`/`self`/private names are disabled. Unknown filters fail. Student and exemplar text is substituted once and never reparsed as template source.

## Inclusion guarantees and safeguards

The renderer wraps assessment content in internal identity markers, renders through Jinja, then parses those markers. It verifies each expected block exactly once, compares its content verbatim, and checks the complete exemplar ID sequence. This handles identical or empty exemplar text and does not rely on independent substring searches. Internal markers are removed before returning the prompt. New preparations hide item IDs in generated labels and retain exemplar IDs; historical revision 1 retains its original item labels. Marker namespaces avoid collisions with source content and template text.

Advanced customization changes the surrounding layout and conditional wording. It does not rewrite, escape, summarize, reorder, select, or omit protected exemplar content. Exemplar annotations for other stored rubric traits remain present even when only one trait is being assessed.

Default safeguards are configurable through `prompt.limits` or `render_prompt(limits=RenderLimits(...))`:

| Setting | Default |
|---|---:|
| `max_source_bytes` | 262,144 UTF-8 bytes |
| `max_ast_nodes` | 10,000 |
| `max_ast_depth` | 16 |
| `max_loop_iterations` | 100,000 conservative loop-work units |
| `max_output_bytes` | 16,777,216 UTF-8 bytes |

The loop budget sums each loop's list length multiplied by its AST size; it bounds work even when a loop produces no text. These are rendering safeguards, not limits on stored trait or exemplar counts. Exceeding a safeguard raises `PromptError`; nothing is truncated. Increase a documented limit explicitly when appropriate. Syntax, undefined names, incompatible arguments, invalid exemplar assignments, and inclusion failures also raise `PromptError`, a `ValueError` subclass.

Immutable source preparation validates every source and record before sealing,
and compiles each template/assessment/grouping/rendering context with the same
syntax, AST, unknown-variable, sandbox, and compilation checks, even with empty
inputs. Bounded full render checks use the first verified actual response for
each rubric group or first actual indexed pair per represented criterion. Actual
response IDs and original/presented orientation are preserved. Empty/unrepresented
contexts receive static checks; hypothetical content branches are not evaluated.
Normal preparation avoids rendering the full response-by-trait product.

Actual selected requests must pass `PreparedRun.render(dependency)` before any
future provider submission. It checks their full verified texts and protected
blocks with these same safeguards. A content-dependent output-size or template
condition failure can therefore first appear at this request boundary; dependency
iteration alone does not establish rendering success. Internal
`prepare_run(..., exhaustive_rendering=True)` and
`validate_inputs(..., exhaustive_rendering=True)` provide an explicit full render
scan when wanted. The option changes validation work without changing manifest
compatibility, request identities, or generation commitments, and does not retain
prompts under minimal capture.

## Output validation and caching layout

Prompt text and `output_schema` are separate. Rubric generative output is a `scores` list containing `trait_id`, `category_label`, and an explanation when requested. Local `validate_categorical_output` additionally rejects missing, duplicate, or unknown traits and invalid categories. Pairwise output contains `winner_id`, `tie`, and an explanation when requested; a tie requires a null winner. Editing wording cannot disable those checks, invent probabilities, or provide an arbitrary provider request body.

Built-in rubric prompts place shared guidance, criteria, exemplars, and output instructions before the variable response. This preserves reusable prefixes. Milestone 1 does not implement provider caching or token estimates. Unknown capabilities remain explicit in preflight; a preview makes no claim about exact context size, endpoint availability, or provider behavior.

## Proposed Milestone 3 extensions (not implemented)

The authoritative [assessment output design](assessment-output-design.md) extends
these existing contracts. The [six complete template and rendered-prompt
examples](assessment-prompt-examples.md) cover scores only, the existing together
shape, scores first, explanations first, winner-only forced choice, and explicit
ties. They show the proposed generated instructions, complete output contracts,
and representative synthetic model responses; they are design snapshots, not
installed presets or live qualifications.

Proposed `prompt.output_order` values are `together` (default), `scores_first`, and
`explanations_first`. Layout is independent of trait grouping and always uses one
call per planned group. Separated arrays join by `trait_id`, never by position,
and normalize into the existing per-trait results. Nondefault order requires
rubric scoring with explanations; incompatible settings fail preflight. Keep
the current scores-only shape when explanations are disabled.

Proposed `prompt.structured_output` is `auto` (default), `native`, or `text`.
Provider-native enforcement, where evidenced compatible, carries the generated
schema separately. Use concise `output_instructions` without repeating the
complete schema where the endpoint's documented practice permits; retain the
schema copy where recommended or required. Local validation remains mandatory.
Explicit native enforcement fails preflight when unavailable; automatic selection reports any text fallback
before immutable request preparation. No fallback follows a rejected submission.
Neither a prompt nor schema property order proves a model's reasoning order;
serialized key-order guarantees require endpoint-specific verification from
original emitted text. Observe both top-level keys and score-object keys to
assess `together`'s category-before-explanation order. Full capture can retain
authorized original evidence for later review; minimal capture retains only
nontext order metadata and hashes after in-memory inspection.
Persist these root/member key sequences in one linked evidence record per
provider observation, rather than copying them into grouped result rows.

Built-in templates will omit the reference-example section entirely for zero
exemplars. Custom templates may already omit `{{ exemplars }}` when none are
supplied. The documented advanced `exemplar_items` conditional needs no new
placeholder. All supplied exemplars retain exact inclusion, identity, and order
checks. Assessor role, scoring procedure, evidence requirements, explanation
style, and section arrangement remain editable around protected blocks.

The proposed generative forced-choice response requires only `winner_id`;
normalization retains `tie: false`. Historical responses with explicit
`tie: false` remain supported through compatibility validation. Ties-enabled
responses retain explicit `winner_id` and `tie`. Assessment explanations remain
distinct from optional provider-exposed reasoning evidence. None of these
changes enables unsupported explanation generation on native decision endpoints.

## Current default presentation

[Assessment revision 2](renderer-revision-2.md) applies to every new preparation
and preview. `response` contains only exact student text; default templates add
`## Student Essay`. Rubric/criterion/output blocks own their Markdown headings.
CJ first/second and legacy left/right placeholders emit positional aliases and
must appear in that order. Custom templates cannot reveal source IDs through
these standard placeholders. CJ output uses `essay_01` or `essay_02`, mapped to
original IDs internally. Historical sealed requests keep their original revision.
Alternative-descriptor instructions belong explicitly in rubric data.
