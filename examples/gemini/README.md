# Gemini Developer synthetic examples

See the [Gemini guide](../../docs/gemini.md). Install the Google extra and keep
keys outside configuration. These small files contain synthetic content only.

```bash
judgmentllm validate --config examples/gemini/rubric.yaml --json
judgmentllm plan --config examples/gemini/pairwise.yaml --json
judgmentllm templates preview --config examples/gemini/rubric.yaml --item-id 001 --trait 01
```

Replace `YOUR_EXACT_MODEL` before separately authorized live execution. These
examples select text mode. Native production mode requires independent reviewed
model/route qualification; the offline qualification proposal grants no execution
permission. All IDs, including `001` and `01`, are character strings.
