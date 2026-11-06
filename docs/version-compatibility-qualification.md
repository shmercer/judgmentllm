# Offline a2 to a3 compatibility qualification

The a3 preparation reader accepts exactly the qualified a2 and a3 producers.
The a2 reader accepts a2 only. Unknown/future producer or consumer transitions
are rejected. This is an explicit one-way package compatibility policy, not a
SemVer range or permission to bypass registry, manifest, serializer, source,
configuration, payload, provider-evidence or immutable-export checks. No schema
migration or saved-manifest rewrite is involved.

Routine CI consumes the small genuine a2 archive described in
[the fixture provenance and reproduction guide](../tests/fixtures/README.md).
That test executes an actual failed-member retry and protects old manifest,
attempt/submission and export identities. It neither fetches Git history nor
builds an old release. The comprehensive matrix below is a separate required
Milestone 2 acceptance operation; it does not expand routine CI or the seven
strict R gates.

## Prepare the two installed environments

Use an external local directory, preferably under `/mnt/VM_Fast`, and a prepared
wheelhouse containing the locked core dependencies and build tools. Keep all
stores, source archives, wheels and reports outside synchronized folders. Build
the unmodified baseline from exact commit
`4b4563102d1bf2dbb7ede8e40b87a7734f9437dd` and build the a3 candidate from the
exact final commit. Retain the archives and wheel hashes. Installation and
dependency-cache preparation are distinct from qualification; qualification
itself never installs or fetches anything.

The [fixture guide](../tests/fixtures/README.md#reproduction) shows baseline archive
and offline wheel creation. Install each wheel into its own clean environment
with the same locked core dependency constraints. Generate source archives with
`git archive --format=tar COMMIT --output PATH`; the tool verifies their embedded
Git commit, installed metadata/version and every installed package resource
against the archived source. Editable candidate installations are rejected.
Record environment differences if separate dependency versions are deliberately
being tested; they do not replace the locked baseline/candidate comparison.

## Run the exact-commit matrix

After the final candidate is committed, installed as a3 and ready for offline
verification:

```bash
python scripts/qualify_version_compatibility.py \
  --baseline-python /local/compat/a2-env/bin/python \
  --candidate-python /local/compat/a3-env/bin/python \
  --baseline-source-archive /local/compat/a2-source.tar \
  --candidate-source-archive /local/compat/final-source.tar \
  --output /mnt/VM_Fast/judgmentllm-qualification/version-compatibility-FULL_HEAD_SHA
```

Use corresponding interpreter paths on Windows. The output directory must be
new. Missing interpreters/archives or an unqualified version fail; no test is
silently skipped. `PYTHONPATH` is removed from subprocesses, and each phase runs
outside the checkout. Each installed interpreter selects its own installed
console. No version field is assigned to manufacture an old fixture.

The authentic a2 process creates sixteen tiny stores. The primary eight are
full/minimal variants of completed, partial grouped, interrupted and uncertain
runs. The completed full
case is pairwise, including reversed presentations and character IDs `001`/`002`;
the remaining primary cases score grouped rubric traits with complete
probabilities and nonuniform coding. All primary stores have two original
physical requests. Rubric cases
have six logical tasks; the pairwise case has two. These are compatibility
fixtures, not performance or scale benchmarks.

Eight supplementary stores exercise full/minimal variants of sealed-only
preparation, a durable prepared attempt before submission, published response
receipt before its observation transaction, and committed observation before
normalization. Each supplement contains one response with three grouped traits:
three logical tasks and one physical request. The actual installed a2 child uses
the existing `prepare_run`/`execute_run` seams and exits at `after_preparation`,
`after_receipt` or `after_observation`; sealed-only preparation performs no
execution. Its parent owns and joins that child. Boundary markers, journal,
receipt/raw-artifact checks and independent ledger counts prove the claimed
windows before a3 consumes them. These helpers never relabel a saved version.

The a3 consumer completes sealed/prepared work with one attempt and acceptance.
For both full-capture response-loss windows, it must complete from exact retained
bytes with authoritative retrieval disabled. Each corresponding minimal case
must remain unresolved with zero results and unchanged acceptance while retrieval
is disabled, then recover that same accepted observation when retrieval is
restored. The original attempt/submission/payload/manifest commitments remain
unchanged throughout.

The a3 consumer performs public status, listing, absolute-directory resolution,
resume and all three export views. It retries only failed grouped members using
stable operation IDs, retains unsuccessful histories, and replays an operation
without another acceptance. Each original manifest's bytes/version/hash, prior
attempt/submission identities and original export bytes remain authoritative.
New results under an old run continue referencing its a2 manifest.

Accepted uncertainty remains blocked while authoritative evidence is disabled;
restoring evidence recovers the original observations without another
acceptance. Interruption uses the existing process-owning supervisor and waits
for confirmed exit before a3 opens the run. Its unaccepted submission boundary
correctly remains partial: disappearance never grants retry authority.
Synthetic long delays are interrupted at the durable boundary rather than
waited out. Minimal stores are consumed in place with their unchanged original
filesystem generations. Full snapshots do not grant an implicit minimal-mode
relocation exception.

The tool checks exact task/request/attempt/result/acceptance counts and preserves
old results, memberships, observations and immutable export hashes. Negative
checks cover unsupported package transitions, incompatible contexts, missing
ledgers, altered manifests, corrupt full snapshots and changed minimal originals.
The deliberately changed minimal original is retained as a failed-source
specimen after its positive checks; restoring bytes cannot restore its original
change-time commitment.

## Retained evidence and acceptance

`report.json` records qualification status, tool/source fingerprints, phase exits
and elapsed times. `identify-a2.stdout.json` and `identify-a3.stdout.json` retain
exact producer/consumer commits, package-resource hashes and Python/SQLite/
Pydantic environments. Each case retains its genuine a2 `before.json`, candidate
`after.json`, original artifacts, immutable exports, control stdout/stderr/exit
records, and interruption proof where applicable. `consumer-report.json` lists
the passing cases and negative checks. Failures and stderr remain in place.

Acceptance requires `qualified: true`, all sixteen cases and every negative check
passing on the exact final a3 source. A changed candidate commit requires fresh
qualification; an earlier producer-only or same-version helper exercise is not
cross-version acceptance. This command has its own bounded phase deadlines and
does not change the 180-second required execution-memory fixture deadline.
The historical Subphase 6 boundary qualified six local configurations. Current
publication independently requires the seven-configuration local grid and the
exact-head hosted matrix in [local CI](local-ci.md); retain the historical
six-lane evidence without treating it as the current publication matrix.

## Phase 4B registry revision

The consumer additionally creates internal registry schema 2 for new stores.
Genuine retained schema-1 alpha2/alpha3 stores remain verified in place with their
original checksums, source artifacts and receipts. Legacy execution/resume/retry
and exports remain supported without evidence backfill. Internal companion
readers report unavailable rather than zero usage or verified order. Evidence
writes to schema 1 are rejected safely; there is no implicit migration command.
