# Comparative-judgment sample prompt index

These are byte-verified, offline engine renderings from JudgmentLLM at `528b82088831b3280c02a1154a1a704a11a70f5a` (PR #48). Each `.txt` includes the complete `## Output Format` instructions and a canonical JSON schema using `essay_01` and `essay_02` only. Every CJ criterion is scored independently without explanations or ties.

- 6 models x 13 criteria = 78 forward samples.
- The same models and criteria reversed = 78 reversal samples.
- Total: **156 full prompt examples**.

Examples:

- [GPT-6 Luna: Introductory Sentence (forward)](gpt_6_luna__cj_13_independent_score_only__introductory_sentence__original.txt)
- [GPT-6 Luna: Introductory Sentence (reversed)](gpt_6_luna__cj_13_independent_score_only__introductory_sentence__reversed.txt)
- [GPT-6 Luna: Overall Writing Quality (forward)](gpt_6_luna__cj_13_independent_score_only__overall_writing_quality__original.txt)
- [GPT-6 Luna: Overall Writing Quality (reversed)](gpt_6_luna__cj_13_independent_score_only__overall_writing_quality__reversed.txt)

Under reversal, the first-presented essay is always `essay_01`, even if it had the other alias in the forward comparison. JudgmentLLM's contract validates only those aliases and resolves the winner internally to the original ID.

**Exact engine confirmation:** From the package root, run `bash scripts/preview_all.sh` with JudgmentLLM installed; this compares prompt bytes and schemas to the current package snapshots. It is offline and uses no credentials.
