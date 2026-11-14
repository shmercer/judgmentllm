# Genuine pre-cache-policy runs

`store.tar.gz.base64` contains four pending OpenAI runs produced from clean main
`34dd83f` using the retained producer and pinned dependencies: `auto`/`off`, each
with full/minimal capture. All inputs and outputs are synthetic; no SDK client,
credential lookup or network request was executed. Provenance records hashes.

The tests verify original manifest, receipt and request bytes. Full-capture runs
can be relocated using the existing explicit frozen-snapshot authorization.
Minimal-capture files retain their original filesystem identity requirements;
copying them does not authorize a source change or permit a new policy.
