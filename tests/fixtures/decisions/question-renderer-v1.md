# Historical V4 question renderer 1

This synthetic full-capture rubric/pairwise store was produced from clean commit
`23c855f60f0cbb61700793e764340c7651719b72`, before adding the V4 instruction
override or changing defaults. Both runs are sealed and unsubmitted. Their
original specification, manifest, question, prompt, payload and membership bytes
are preserved; no provider, credential or network operation was used.

`question-renderer-v1.json` records the producer source-file hashes, archive hash,
every archived file's hash/length, and exact original request/question identities.
The base64 archive uses regular files so the existing strict compatibility
extractor can reject links and traversal. Repacking the producer's hard links
changed only archive representation; all original file hashes stayed unchanged.
The original linked archive's hash is retained in the metadata.

The original input paths no longer exist. Tests explicitly reopen full capture
using verified frozen snapshots, check all original commitments, and prove that
renderer 1 cannot acquire the new override. New full/minimal renderer-2 recovery
tests use fresh OS processes. Historical V1–V3 fixtures remain untouched.
