# Engine-verified JudgmentLLM study prompts (PR #48)

**JudgmentLLM source commit:** `528b82088831b3280c02a1154a1a704a11a70f5a`  
**Renderer:** current production V3 / presentation revision 2  
**Contents:** 24 rubric configurations, 156 prompt samples  
**Provider calls:** zero; these are entirely offline rendering examples.

The `rendered_samples/` prompt text was regenerated with the **actual installed
JudgmentLLM** `preview_prompt()` engine using the source commit above. Every
original-v2-to-v3 change was checked: only the generated `## Output Format`
section changed; the complete rubric/criterion/essay content and CJ orientation
remain byte-identical, and canonical JSON Schema lines and sidecars are unchanged.
The original ZIP archives have not been edited. These new files include the
standardized text-only JSON instructions and spacing introduced by PR #48.

## Verify and preview locally

Activate the virtual environment for the recorded JudgmentLLM checkout, then
from this package's root run:

```bash
python3 scripts/verify_package.py
bash scripts/preview_all.sh
```

`preview_all.sh` renders all 156 cases using the installed engine, writes
`actual_engine_previews/`, and verifies exact prompt bytes and schema structure
against `rendered_samples/`. This process makes no provider requests. The
integrity checker ignores the generated preview directory. To inspect one prompt:

```bash
judgmentllm templates preview --config configs/gpt_6_luna__all_12_score_explanation.yaml --item-id essay_demo_001 --trait introductory_sentence
```

The package's configuration format is `schema_version: 3` and its assessment
presentation is revision 2. The archive name `v3` identifies this **study
package update**, not a change to the output contract or schema version.
Future changes to the JudgmentLLM renderer may legitimately cause strict
preview comparisons to fail; investigate differences before updating samples.

---

# JudgmentLLM rubric-scoring study package

**Current renderer:** Presentation revision 2 (issue #38) with output-instruction improvements from PR #48; locally rendered at JudgmentLLM source `528b82088831b3280c02a1154a1a704a11a70f5a`.

This package contains 12 original analytic rubric traits, four prompt conditions, and **24 configs (six models × four conditions)**. Per model, for each essay: grouped scores-only (1 request), grouped with explanations (1), independent scores-only (12), independent with explanations (12): **26 requests/model, 156 across six**. No real provider calls were made.

The rubric retains the exact original descriptors for score 2, all score-1 alternatives, and the common score-0 description. Alternative score-1 descriptions are represented as `Any one of the following:` and bullets in a **single** level description. Technical maximum-score metadata was removed from the rubric's shared description. The old role sentence and explanation sentence are verbatim. The Task instruction states: `Assign a score of 0, 1, or 2 using the rubric descriptors. When several descriptions are listed for score 1, any one of them is sufficient. Do not introduce additional scoring criteria.`. The English Internet/travel task is verbatim except the approved removal of `(minimum 1 page)` and German instructions; the 15-minute time limit is noted.

Each prompt uses the supported Jinja blocks `{{ rubric }}`, `{{ output_instructions }}`, and `{{ response }}` once; the revision-2 renderer supplies `## Rubric`, numbered stable trait names, score headings and the bare student essay body. The file itself supplies `## Student Essay` and does not repeat the rubric heading. No extra JSON fields are requested. Output continues to use `scores` with `trait_id` / `category_label`, plus `explanation` only when configured. The 0/1/2 category coding remains unchanged.

All model configs use `schema_version: 3`, `structured_output: text`, `output_order: together`, with generation parameters left unspecified. Exact model access and provider settings must be validated locally. The six model IDs are in `configs/`.

To inspect using the installed JudgmentLLM renderer (offline):

```bash
python3 scripts/verify_package.py
bash scripts/preview_all.sh
judgmentllm templates preview --config configs/gpt_6_luna__one_trait_score_only.yaml --item-id essay_demo_001 --trait rephrasing_of_the_thesis_statement
```

`rendered_samples/` contains actual-engine-rendered revision-2 samples, with actual JudgmentLLM v3 text-mode output-instruction wording and matching canonical JSON schemas; these are not model-scored essays. The merged renderer preserves immutable historical/frozen runs under presentation revision 1. Replace synthetic response data before any authorized live scoring.
