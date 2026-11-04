# PR #10 review corrections

Review baseline: `8049bb1a9fb57e34e04d936e8794475706301b4a`, on
`codex/milestone-2-subphase-5-cli-lifecycle`. The reviewed head passed all five
hosted jobs in run `41934273214`. These corrections retain package/schema
versions, public lifecycle signatures, CLI envelope fields, provider behavior,
and the existing resource and CI timeouts.

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
