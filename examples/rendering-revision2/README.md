# Exact offline writing examples

These UTF-8 prompts and schemas come directly from JudgmentLLM's renderer and
output contracts. All essays are synthetic; the illustrative two-trait rubric
is not the issue #39 study package. Descriptors include explicit alternative
wording in the rubric data.

| Files | Assessment |
|---|---|
| `one-trait.*` | Introductory Sentence, scores only |
| `grouped.*` | Both illustrative traits, scores only |
| `grouped-explanations.*` | Both traits with explanations |
| `cj-forced.*` | Forced-choice comparison |
| `cj-explanations.*` | Comparison with one-sentence justification |
| `cj-reversed.*` | Reversed essay presentation |

Each `.txt` includes the exact generated instructions and schema. The matching
`.schema.json` is the generated ordered schema, without a user-authored substitute.
The forward synthetic choice `essay_01` and reversed choice `essay_02` both resolve
to original item `001`. JSONL and R-compatible CSV retain original IDs and order.

Regenerate from the repository root:

```bash
python examples/rendering_revision2.py --output /tmp/rendering-examples
```

No provider, credential or network operation occurs. The strict export/R gate
also regenerates these examples and checks original identities and reversals.
