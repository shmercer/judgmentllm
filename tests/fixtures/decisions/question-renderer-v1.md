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

The original archive contains absolute POSIX paths in typed configuration fields.
Its archive hash and every original file remain independently checked, without
reinterpreting those path bytes on Windows.

`question-renderer-v1-portable` is a separate genuine historical fixture produced
with the retained, locally qualified `23c855f` wheel. All 157 packaged resources
and its synthetic data helpers were verified against that commit before import.
The producer script, wheel, qualification receipt, source files and complete
archive contents have recorded SHA-256 identities in its metadata. The existing
private development-module packaging exclusions are preserved.

The portable producer uses Python-origin single-component relative Path fields,
following the existing presentation-history fixture pattern. Frozen source
locations still record their real original absolute paths as strings. The
all-platform tests explicitly reopen full capture using verified snapshots,
check the original sealed manifest/question/prompt/payload commitments, reject
resealing and prove renderer 1 cannot acquire the override. No stored paths,
hashes or sealed artifacts were rewritten. New full/minimal renderer-2 recovery
tests use fresh OS processes. Historical V1–V3 fixtures remain untouched.
