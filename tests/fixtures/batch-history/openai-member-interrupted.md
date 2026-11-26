# Interrupted OpenAI Batch member fixture

The protected `d6071de62c139122c2c4ebd9874e15b16807d72e` writer generated
this fixture using the pinned SDK and the repository's offline synthetic service.
The producer loaded `src`, `tests`, and the service script from that commit's
verified tree. It used the base fixture setup for one Chat Completions member in
full capture and ran the real prepare, submit, poll, and collect entry points.
The Python specification used the single-component relative input path
`responses.jsonl` before preparation. The writer sealed that specification
directly.

Before collection, the synthetic row used the harmless
`offline-envelope-placeholder` string from the decoded-envelope regression in its
optional fingerprint. The writer ran with its production screens active. The
fault callback interrupted collection at `after_batch_member_receipt`, after
the named exact receipt had been published and before result, usage, or cursor
publication. The metadata records the manifest, specification, original wire,
retained object, named receipt, and archive digests.

The archive contains regular-file copies of the synthetic store and service
ledger. SQLite integrity was checked and WALs were checkpointed before packing.
Locks, transient SQLite sidecars, and the original input file were excluded.
The candidate restores the run using its sealed frozen inputs.

The populated typed paths are `input.path` in the manifest and `input.path`
and `output_path` in the specification. Linux verification substituted
Windows path objects into those fields and reproduced the sealed manifest and
specification bytes and both configuration and assessment digests. Original
source locations and artifact references are sealed strings. This check covered
path serialization. Native Windows replay remains to be verified.
