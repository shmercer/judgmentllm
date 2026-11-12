# Portable genuine pre-change preparation fixture

This additional fixture was produced with unchanged main commit
`38bda981a46330b73fec6e80a9b1c0c7a3c1c61a`, using the exact retained producer.
The Python-origin specifications deliberately retain single-component relative
input paths. Their serialized path bytes therefore have the same interpretation
on POSIX and Windows; no historical manifest or commitment is rewritten to move
between path syntaxes. All originals are unavailable during replay, which
explicitly authorizes the verified frozen snapshots.

The three synthetic stores preserve the same old source-ID/alias collision,
pending RunSpec 1/2 requests and received RunSpec 2 response as the parent fixture.
The parent archive and its producer remain byte-identical and independently
hash-checked. This archive has its own immutable producer/archive hashes in
`provenance.json`. Base64 is only a transport encoding. Scoped Git attributes
preserve exact producer and rendering bytes on Windows without normalizing any
historical evidence or weakening byte comparisons. No provider is contacted.
