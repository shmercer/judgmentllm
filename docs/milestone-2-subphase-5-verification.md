# Milestone 2, Subphase 5 verification

Implemented on `codex/milestone-2-subphase-5-cli-lifecycle`, in the separate local
worktree `/tmp/judgmentllm-subphase-5`, from freshly fetched main
`01fa7e47b86f36370f8df017873cf41668b2f363`. PR #8 and PR #9 are merged and
post-merge GitHub run `41909583052` passed all five configurations.

Package version remains `0.1.0a2`. Registry, manifest, assessment and normalized
result schemas remain version 1; canonical and R export columns are unchanged.
No dependencies or lockfile contents changed. No push, PR, merge or release is
part of this work. Subphase 6 qualification remains deferred.

## Shared lifecycle and CLI

`lifecycle.py`, `lifecycle_models.py` and `_lifecycle_store.py` expose complete
offline validation, exact structural estimation, synchronous/asynchronous live
execution and resume, bounded run inspection/listing, explicit retry selection
and immutable result export. They delegate to existing preparation, executor,
retry and export services. The CLI calls these same public functions.

Public summaries distinguish planned/materialized logical work, current task
states, historical successful/unsuccessful results, physical attempts and
submission certainty. Provider usage/prices remain unknown. Inspection uses the
verified manifest without opening original assessment sources, short SQLite
snapshots, bounded export references and the existing OS lock. It never creates
a missing registry, repairs ownership or claims that an exited process continues.

Execution dispatch accepts only exact `mock` / `synthetic` identity. Both public
API and CLI regressions replace mock construction with a forbidden sentinel for
non-mock configurations, proving rejection before construction or submission.
Other valid configurations retain their established offline behavior.

Synchronous wrappers reject active event loops before I/O. Async preparation
and retry authorization drain their worker on cancellation; failures during that
drain do not replace cancellation. Existing executor cancellation retains its
provider-evidence distinction. A missing minimal response remains unresolved
without authoritative retrieval; neither recipes nor hashes manufacture results.

Narrow additive internal changes are a pure mock preflight/capability interface,
a read-only lock probe and `RetryAuthorizationError(IntegrityError)` for invalid
selection/permission/operation reuse. Existing low-level callers can still catch
`IntegrityError`. There is no schema or execution-scheduling change.

The seven CLI commands return the version-1 envelope and documented exits
0/1/2/3/130. JSON stdout is separate from stderr JSONL, including parser errors,
warnings and handled interruptions. JSON escaping preserves exact Unicode IDs
and paths through ASCII/locale-limited redirected streams; the installed-process
regression checks both streams and exit codes independently. Existing template
text behavior is retained. `run` and `resume` block within their own process.

## Independent processes and R

The six existing strict gates are unchanged: exports, persistence, generation,
source preparation, execution and grouped lifecycle. The seventh gate launches
the installed executable from base R, using separate stdout/stderr files and
independent process status assertions. Only base R and jsonlite are required.

Fixtures cover full/minimal independent/grouped rubric and pairwise execution,
leading-zero character IDs, original/reversed presentation, categorical versus
expected scores, nonuniform mappings, complete probabilities, unsuccessful
grouped observations, exact failed-member retries and immutable prior exports.
R joins results through attempts, memberships, submissions, observations and the
independent acceptance ledger. It checks complete normalized records and missing
provenance rather than inferring duplicate safety from result counts.

An accepted-but-unavailable fixture retrieves the original observation in a later
process. Real termination waits for one committed result and a later submitting
boundary; new processes inspect, safely resume and export without another
acceptance or invented retry authority. Supplemental Python subprocess tests
cover invalid/missing/duplicate members, refusal/error/confirmed cancellation,
completed-task permission and repeated operation IDs. Existing exhaustive core
fault and privacy tests remain required rather than being copied into R.

The strengthened R fixture took **0.26 s setup, 24.08 s lifecycle, 24.50 s total**
on the local Linux host. Adding evidence comparisons required no additional
processes. It never waits for the synthetic 60-second delay to finish and performs
no scale benchmark. The hosted job retains its 20-minute limit.

## Local checks and retained evidence

Host: Linux x86_64, Python 3.12.3, SQLite 3.45.1, Pydantic 2.14.2, Typer 0.27.3,
uv 0.13.6, R 4.6.2 and jsonlite 2.0.0. The locked environment includes all extras
for development checks; clean core installations verify optional SDKs are absent.

| Check | Result before final cross-platform qualification |
|---|---|
| Ruff lint/format | Passed |
| Strict Pyright | Passed, zero errors/warnings |
| Integrated pytest with branch coverage | 1,455 passed in 344.37 s; 95.07% |
| Offline lock validation | Passed; unchanged lock SHA-256 below |
| Offline isolated wheel/sdist builds | Passed |
| Strict Twine metadata | Passed for both distributions |
| Clean core wheel/sdist install and lifecycle smokes | Passed, 3.67 s / 3.92 s |
| Local CI infrastructure tests | 58 passed in 1.81 s |
| CLI focused tests including ASCII-stream regression | 52 passed in 2.34 s |
| Installed independent-process tests | 13 passed in 24.55 s concurrently with R |
| Public integrity edge tests | 8 passed in 1.90 s |
| All seven strict R/jsonlite gates | Passed |
| Linux/Windows/macOS readiness | Passed; readiness is not qualification |

Lock SHA-256:
`fb46854f753890cd9cc8006631e06345ad6e6417e150ed5d6843d16d905b77a3`.

The first integrated regression run found the old-high-water export convenience
path mismatch: selected bytes were immutable and correct, but an artifact alias
pointed to the newer export. The selected format/view alias now points to the
requested snapshot; a regression retains both snapshots and checks every returned
selected reference. That run's failure evidence is retained separately. Subsequent
focused tests and the subsequent integrated suite pass. The final ASCII-stream
regression was additionally verified in the 52-test CLI run. Exact final-head
full-suite counts and coverage for every native lane are recorded in the external
report described below.

The known restricted-sandbox asyncio/thread wakeup limitation reproduced during
initial testing. Execution tests completed outside that sandbox with unchanged
code, test assertions and timeouts. No timeout or resource gate was relaxed.

## Exact-head qualification and limits

This report is committed before qualification. The final report lives outside Git
at `/tmp/judgmentllm-subphase5-verification/final-report.json`, so writing exact
commit/results evidence cannot change the head being qualified. It records the
final SHA, test counts, coverage, elapsed times, source/archive/lock identities,
all six native summaries, failures and limitations. Detailed grid evidence is
retained under `/mnt/VM_Fast/judgmentllm-ci/grid/<run-id>/`.

Run `python3 ci/local/grid.py --ref FULL_HEAD_SHA --mode check` after the final
commit. Acceptance requires `qualification: true` and all six Linux/Windows/macOS
Python 3.11.18/3.12.16 lanes passing in parallel, including all seven strict R gates,
coverage, builds and clean-install smokes. Source bytes are verified before/after.
New source changes require a new final commit and a complete qualification run.

The 180-second execution fixture deadline, 64 completions, native RSS below
256 MiB, Python execution allocation below 32 MiB, export allocation below 8 MiB,
existing growth limits and worker/task/queue bounds are unchanged. No full
5,000-by-20 or 100,000-response qualification was added or claimed.

Local checks retain Linux network isolation, guest offline settings, short local
Windows scratch paths, shared VM locks and graceful shutdown. Local R versions
may differ from hosted R; jsonlite remains pinned to 2.0.0. Hard Windows termination
does not promise a graceful exit-130 envelope. POSIX signals and portable handler
tests cover that separate contract.

The final head is not published, so it has no hosted CI result yet. Required future
hosted qualification remains Ubuntu/macOS Python 3.11/3.15 plus Windows 3.12; the
full manual Windows 3.11 job is retained. Local 3.11/3.12 results do not establish
hosted Python 3.15 qualification. Real providers, batches, managed bulk-live,
actual caching, catalogs, rubricLLM, FastAPI and Subphase 6 remain outside scope.
