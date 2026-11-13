# Engine-verified JudgmentLLM study prompts (PR #48)

**JudgmentLLM source commit:** `528b82088831b3280c02a1154a1a704a11a70f5a`  
**Renderer:** current production V3 / presentation revision 2  
**Contents:** 6 CJ configurations, 156 prompt samples (including 78 reversals)  
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
judgmentllm templates preview --config configs/gpt_6_luna__cj_13_independent_score_only.yaml --pair-uid demo_overall_writing_quality_original
```

The package's configuration format is `schema_version: 3` and its assessment
presentation is revision 2. The archive name `v3` identifies this **study
package update**, not a change to the output contract or schema version.
Future changes to the JudgmentLLM renderer may legitimately cause strict
preview comparisons to fail; investigate differences before updating samples.

---

# JudgmentLLM independent comparative-judgment study package

**Current renderer:** Presentation revision 2 (issue #38) with output-instruction improvements from PR #48; locally rendered at JudgmentLLM source `528b82088831b3280c02a1154a1a704a11a70f5a`.

The study uses **13 independent forced-choice criteria**: the original 12 trait names (each criterion description reproduces the original *Score 2* / full-attainment rubric descriptor verbatim), plus `overall_writing_quality` with the supplied complete holistic guidance. This concise, criterion-based CJ version does **not** repeat all the alternative score-1 rubric descriptors; it is not identical to fully rubric-informed CJ. This difference should be documented when comparing direct scoring and CJ. No explanation condition, no ties, and no ordinal score assignment.

There are 13 one-criterion comparisons per pair per model; **78 across six models**, excluding reversals/repetitions/retries. The 13th holistic criterion emphasizes deep structure, not surface errors, and is not a checklist. The Task requests a criterion-based comparison for the first 12 and a holistic impression for the 13th.

The common Role and Context is unchanged. Only the approved English Internet/travel writing task is included, with the 15-minute limit (not the German instructions or page minimum).

The template uses supported placeholders `{{ criterion }}`, `{{ output_instructions }}`, `{{ first_response }}`, and `{{ second_response }}` exactly once. The revision-2 renderer expands `{{ criterion }}` to `## Comparison Criterion`, criterion heading/ID, and original description. The template adds `## Essays` and the renderer provides `essay_01:` (first presented) and `essay_02:` (second presented), **regardless of which source ID corresponds to either position**. New wire outputs must use exactly `{"winner_id":"essay_01"}` or `{"winner_id":"essay_02"}` and no other keys. The library maps the alias to the actual source ID for the exact occurrence before normalizing or exporting. In the reversal example, the original first essay becomes second-presented and therefore `essay_02`.

`pairs_demo.jsonl` has 13 original comparisons, one per criterion. The separate `pairs_reversal_demo.jsonl` is for illustration/testing only and is not included in the default configs; for a real reversal audit combine appropriate original and reversed occurrences in a properly planned input. Both response texts are synthetic and their original IDs are intentionally opaque to test privacy.

All six model configurations use `schema_version: 3`, `prompt.structured_output: text`, and no explanations. No credentials or provider calls were used to create this package.

To inspect using the installed JudgmentLLM renderer (offline), run:

```bash
python3 scripts/verify_package.py
bash scripts/preview_all.sh
judgmentllm templates preview --config configs/gpt_6_luna__cj_13_independent_score_only.yaml --pair-uid demo_overall_writing_quality_original
```

Verify model-agnostic aliases in text AND output instructions/schema; ensure original IDs never occur in the model-visible prompt; and verify exact orientation and result normalization under reversal, retry and resume before any billable pilot. For study interpretation, decide whether concise CJ descriptors are appropriate for its validity comparisons, or whether to include additional rubric descriptors in CJ criterion guidance.
