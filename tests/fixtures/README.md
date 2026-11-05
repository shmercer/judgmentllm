# Authentic a2 compatibility fixture

`a2-compatibility.tar.gz` is a small, deliberately retained synthetic exception
to the rule against committing runtime stores. It was produced by the unmodified,
installed `0.1.0a2` package from commit
`4b4563102d1bf2dbb7ede8e40b87a7734f9437dd`. Its archive checksum, every member's
length/hash, original run/manifest identities, installed package source hashes,
baseline source-archive checksum and producer-helper fingerprint are recorded in
`a2-compatibility.json`. `producer.json` inside the archive records the producer
verification independently. No saved version or manifest was relabeled.

The fixture contains one response with character ID `001`, three grouped traits,
nonuniform ordinal mappings, complete probabilities, two successful members and
one invalid member. Its independent acceptance ledger records one acceptance.
It includes immutable canonical JSONL/CSV and R CSV exports of all three rows.
Only synthetic material is present; no credentials or provider calls are used.

The regression extracts regular files only, checks the complete member inventory
and hashes, inspects the genuine old run, and executes a failed-member subset
retry through the candidate's public lifecycle. Original manifest bytes, old
attempt/submission identities, unsuccessful results and historical exports must
remain unchanged. Reusing the retry operation cannot add another acceptance.

Full capture permits portable recovery through explicit verified snapshots. The
producer uses a Python-origin specification with relative `Path` fields so their
canonical spelling is unchanged between POSIX and Windows. Original source
locations and filesystem generation evidence remain authentic and untouched.
Minimal-capture fixtures cannot be relocated this way: their original inode,
change-time and location commitments must still match. The separate comprehensive
compatibility qualification produces and consumes minimal stores in place.

## Reproduction

Prepare a local offline wheelhouse and build/install the baseline wheel from an
exact Git archive in an external workspace. Installation is preparation; fixture
production itself needs no network. Do not use the current package as producer.
For example, after creating `LOCAL` and the external baseline source directory:

```bash
git archive --format=tar 4b4563102d1bf2dbb7ede8e40b87a7734f9437dd --output "$LOCAL/a2-source.tar"
python3 -m tarfile --extract "$LOCAL/a2-source.tar" "$LOCAL/a2-source"
uv build --offline --wheel --directory "$LOCAL/a2-source" --out-dir "$LOCAL/wheels"
uv venv --python 3.11 "$LOCAL/a2-env"
uv pip install --python "$LOCAL/a2-env/bin/python" --offline --no-index --find-links "$LOCAL/wheelhouse" "$LOCAL/wheels/judgmentllm-0.1.0a2-py3-none-any.whl"
"$LOCAL/a2-env/bin/python" scripts/version_compatibility_fixture.py \
  --output "$LOCAL/new-a2-fixture" \
  --baseline-source-archive "$LOCAL/a2-source.tar" \
  --archive tests/fixtures/a2-compatibility.tar.gz \
  --checksums tests/fixtures/a2-compatibility.json
```

Use the corresponding installed interpreter path on Windows. The helper checks
the Git archive's baseline commit, actual package and distribution versions, and
every installed package resource against the archive before execution. It never
assigns a package version. Its archive writer dereferences existing artifact
aliases; the reader rejects links, traversal, duplicates and unexpected members.

Each fresh production has authentic new run IDs, source generations and timing
metadata, so it produces new expected hashes. Regeneration is deliberate and
reviewable; routine CI consumes the committed archive without fetching history,
building an older wheel or changing its bytes. Large compatibility matrices,
databases and profiler/export artifacts remain outside the repository.
