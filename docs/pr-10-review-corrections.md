# PR #10 review corrections

Review baseline: `8049bb1a9fb57e34e04d936e8794475706301b4a`, on
`codex/milestone-2-subphase-5-cli-lifecycle`. The reviewed head passed all five
hosted jobs in run `41934273214`. These corrections retain package/schema
versions, public lifecycle signatures, CLI envelope fields, provider behavior,
and the existing resource and CI timeouts.

## Non-mutating registry inspection (Finding A)

Checking only that `registry.sqlite3` existed allowed subsequent inspection to
use the write-capable repository constructor. A zero-byte SQLite file could
therefore acquire an initialized schema during a status/list operation.

Inspection now uses `ReadOnlyRepository`, opening an existing URI with `mode=ro`
and `uri=True`; `query_only=ON` adds a second guard. Schema version, migration
checksum and actual schema-object digest are checked in one read transaction by
the shared verifier. Writer creation still uses its original initialization path,
unchanged migration statements/checksum, WAL durability and ownership rules.
There is no migration, repair, checkpoint, immutable reader or lock bypass.

Public status/listing, all run-directory/ID resolution and resume's identity
prelookup use this reader. Manifest and retained-artifact verification also use
existing-only artifact stores, preventing an incidental getter from recreating a
deleted run directory or object tree. Hash-only artifact identities require no
filesystem store. Sources, provider calls and recovery remain outside inspection.

The approved clarification permits SQLite-owned WAL/SHM coordination files.
Inspection must not write application state or create JudgmentLLM artifacts;
it does not promise that SQLite's shared coordination files remain absent or
byte-identical. Existing malformed/empty registries are tested in isolation for
exact byte preservation. Valid WAL stores are tested with committed data that
has not reached the main database, while another process holds executor ownership.
Application snapshots, ownership and event history remain unchanged. Missing
stores/runs do not create application directories, registries or locks.

Regression coverage includes disabling `query_only` and still being denied a
write, checksum/structural damage, public explicit/default/directory resolution,
export/resume identity validation, allowed SQLite coordination, original missing
run IDs in control envelopes, and compatibility with existing cursors/status.

Focused integration verification: 226 tests passed in 8.08 seconds, including
64 new read-only cases. The independent reader runs with a 15-second process
deadline while the parent owns the writer and executor lock. Locked-file identity,
size and modification time are checked without reading Windows' mandatory-locked
byte. The test neither terminates an executor child nor guesses process lifetime.

All seven strict R/jsonlite gates passed on the final production source:

| Gate | Seconds, including fixture generation |
|---|---:|
| Exports | 0.78 |
| Persistence | 0.84 |
| Generation | 0.89 |
| Source preparation | 1.14 |
| Execution | 1.59 |
| Grouped lifecycle | 1.44 |
| Installed CLI lifecycle | 25.20 |

Ruff lint/format, strict Pyright (zero errors/warnings), offline lock validation,
offline wheel/sdist builds, strict Twine metadata, clean wheel/source installation
smokes (3.72/3.92 seconds), and all 58 local CI infrastructure tests passed.
The full suite passed **1,532 tests in 354.64 seconds**, with **95.41% branch-aware
coverage**, retaining the 90% gate and warnings-as-errors. It includes the existing
privacy, durability, recovery, ownership, export and resource regressions.
Evidence is retained under `/tmp/judgmentllm-pr10-review-verification`, separately
from the original implementation qualification. The final exact-head grid and
hosted results are retained there after the correction commit is frozen.

## Process-instance-safe interruption (Finding B)

The old interruption fixture selected an operating-system signal target using
only `runs.owner_pid`. A stale or substituted row could therefore select another
process. The fixture now launches the actual installed console through a small
test-only supervisor and retains operating-system ownership of that process tree.
The journal PID is used only to verify identity, never to choose a signal target.

- Linux/macOS: a new session isolates the CLI process group. The supervisor
  retains its direct child unreaped, anchoring the PID/group until termination.
  The journal owner must equal that child. Nondefault SIGCHLD handling is rejected
  before launch because automatic reaping would invalidate the anchor. Signaling
  is disabled before an ambiguous wait; cleanup never signals a reaped group.
- Windows: `CreateProcessW` creates a suspended installed-console launcher. A
  fresh private Job Object receives it before execution resumes, closing the
  launcher/child-assignment race. Inherited handles are restricted to standard
  input/output/error. The actual interpreter handle is checked with
  `IsProcessInJob` and retained. Termination uses the private job handle and waits
  for owner and launcher handles plus an empty job, including launcher/interpreter
  PID differences. Failure cleanup touches only handles/tree owned by this test.

The supervisor also binds the durable run to its launched console's prepared
progress record and requires a fresh fixture store/ledger. Missing, conflicting,
out-of-range, stale or foreign identities fail safely. Ordinary R lifecycle calls
still invoke the installed CLI directly; the hard-termination case invokes that
same executable through the test supervisor. No internal execution service is
substituted and no production ownership/recovery behavior changes.

The R fixture waits for the supervisor's actual-exit proof before independent
status/resume/export processes. It no longer polls status to guess whether the
old process died. The first result and independent ledger acceptance remain
unchanged; authoritative nonacceptance still does not manufacture another outcome
or retry permission. Hard termination does not promise a final JSON envelope or
exit 130.

Focused verification before integration:

- Linux: 25 safety/installed-CLI tests passed in 24.41 seconds; strict R lifecycle
  passed in 24.25 seconds (0.25 setup, 23.84 lifecycle). Ruff/Pyright passed.
- Native Windows 3.11.18 and 3.12.16: 10 safety/interruption tests passed on each,
  with three POSIX-only skips, in 3.73/4.24 seconds. Additional actual-console
  probes verified distinct launcher/owner PIDs, foreign-process survival,
  acceptance/result counts of one and inactive status after confirmed exit.
- Native probe evidence:
  `/mnt/VM_Fast/pairwiseLLM-checks/judgmentllm-ci/diagnostics/process-safety-20261104T235348Z-62549/summary.json`.
  This targeted probe reused the reviewed installed package and is explicitly
  not final-head qualification. The previous diagnostic's unsupported pytest
  option was corrected; its evidence is retained separately.

No additional lifecycle invocations or heavy fault/scale tests were added to R.
The representative host R timing is comparable to the earlier 24.50-second
measurement; it is not a controlled performance benchmark. The required final
six-configuration grid and hosted workflow provide integration qualification.

## Cross-platform integration correction

The first correction grid, on `c128a84ba8edbef89300ae1c2c0826e180aa71fd`,
passed Linux/macOS completely. Windows passed 1,558 runtime tests (four expected
platform skips), 95.24% coverage, all seven R gates and packaging, but strict
Pyright reported eight references to POSIX-only signal/process names. This run
is retained as failed qualification, not counted as an accepted final head.

The test supervisor now explicitly refuses POSIX supervision on Windows before
those platform-specific references and declares its process-state field types.
This small Finding B integration correction adds no ignores, dependencies or
behavioral change to the owned POSIX process or Windows Job Object paths. Strict
Pyright passes with Linux, Windows and Darwin stubs. The complete host checks and
all seven R gates passed again before a fresh six-configuration exact-head grid.
