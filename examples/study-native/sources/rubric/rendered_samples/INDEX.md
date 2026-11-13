# Rubric sample prompt index

These are byte-verified, offline engine renderings from JudgmentLLM at `528b82088831b3280c02a1154a1a704a11a70f5a` (PR #48). Each `.txt` contains the complete prompt including the **standardized text-mode output-instruction wording** and the compact generated JSON schema. Each `.schema.json` is a readable copy of that same schema.

- 6 model configurations x 2 grouped variants = 12 grouped prompt samples.
- 6 model configurations x 2 independent variants x 12 traits = 144 independent prompt samples.
- Total: **156 full prompt examples**; no explanation text is fabricated for model output.

Examples:

- [GPT-6 Luna: all 12 traits, scores only](gpt_6_luna__all_12_score_only.txt)
- [GPT-6 Luna: all 12 traits, explanations](gpt_6_luna__all_12_score_explanation.txt)
- [GPT-6 Luna: trait 9, scores only](gpt_6_luna__one_trait_score_only__rephrasing_of_the_thesis_statement.txt)
- [GPT-6 Luna: trait 9, explanations](gpt_6_luna__one_trait_score_explanation__rephrasing_of_the_thesis_statement.txt)
- [Claude Haiku 5.5: all 12 traits, explanations](claude_haiku_5_5__all_12_score_explanation.txt)

**Exact engine confirmation:** From the package root, run `bash scripts/preview_all.sh` with JudgmentLLM installed. This compares complete rendered prompt bytes and schemas against these snapshots without provider calls. Byte-exact engine confirmation is performed by the command above.
