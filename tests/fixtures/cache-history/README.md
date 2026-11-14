# Genuine pre-cache-policy runs

`store.tar.gz.base64` contains four pending OpenAI runs produced from clean main
`34dd83f` using the retained producer and pinned dependencies: `auto`/`off`, each
with full/minimal capture. All inputs and outputs are synthetic; no SDK client,
credential lookup or network request was executed. Provenance records hashes.

The tests verify original manifest, receipt and request bytes. Full-capture runs
can be relocated using the existing explicit frozen-snapshot authorization.
Minimal-capture files retain their original filesystem identity requirements;
copying them does not authorize a source change or permit a new policy.

The in-place minimal-capture tests use `git archive` at the full pre-change
commit in `provenance.json` and run `in_place_producer.py.txt` against that source
in a fresh subprocess. This requires the existing full Git checkout, makes no
network requests and does not patch versions or verification. The original
archive and its provenance remain unchanged. Current code resumes the original
claim and allocates an explicitly authorized revision-1 retry; replacing even
identical input bytes still fails original-source verification.
