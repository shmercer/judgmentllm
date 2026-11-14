# PR2-C bounded test-efficiency audit

This audit follows the [Issue #39 addendum](https://github.com/shmercer/judgmentllm/issues/39#issuecomment-6865503213).
Functional source `e7a0813d3d981d576bbaddf9874a3913e1f560cf` was committed before
the audit. No provider calls, credentials, spending or qualification are authorized.
The original 50 configurations and 30 main selections remain unchanged.

## Sources and comparison scope

PR2-B merged as `195741224fa4a06637eb578519d7e82d7927e618`. Its tree is exactly
`f64c9187b560b256a17709bd42c58516e1612937`, also the tree of accepted PR head
`b27fc77c3b8c5224e1dec4b4b1a815fd78eb1b47`. Its passing local receipt is
`/tmp/pr2b-local-b27fc77-1/primary/receipt.json`; the protected-base
[hosted run](https://github.com/shmercer/judgmentllm/actions/runs/43506732734)
passed Linux 3.11, Windows 3.12, macOS 3.11 and aggregate acceptance. Earlier
failed/interrupted PR2-B attempts retain their original status.

The feature-complete baseline uses an isolated committed checkout at
`/tmp/pr2c-feature-source`, with receipts at `/tmp/pr2c-before-optimization`.
Final-head evidence and the complete machine-readable comparison are attached to
the draft PR. Results stay outside Git so recording them does not change the
qualified source. All comparisons identify their source, lock, Python, worker
count, actual case inventory and coverage hashes.

| Accepted PR2-B local measurement | Value |
|---|---:|
| Python tests, including resource cases | 8,385 passed, zero skips |
| Ordinary / serial / resource wall seconds | 1,160.07 / 458.21 / 613.10 |
| Complete suite / complete lane seconds | 2,244.06 / 2,411.22 |
| Workers / physical-logical host CPUs | 6 / 16-32 |
| Host memory | 128 GB installed; 134,972,350,464 bytes reported |
| Line coverage | 20,537 / 21,303 = 96.404262% |
| Branch coverage | 6,621 / 7,232 = 91.551438% |
| Combined coverage | 95.174347% |
| Strict R gates | Seven passed |
| Collection wall seconds | 7.12 |
| Sampled ordinary process-tree peak RSS | 2,908,237,824 bytes |

The new functional collection contains **8,479 cases: 94 added, zero removed**.
The complete feature-baseline lane passed 8,461 cases with zero skips; the 18
resource cases remain separate. Its ordinary/serial wall times were
1,041.07/611.69 seconds, and the complete lane took 1,847.36 seconds including
all seven strict R gates. Coverage was **96.414361% lines, 91.586340% branches,
95.189520% combined**, with the same 766 missing lines, 611 missing branch arcs
and 141 excluded lines as PR2-B. All 60 added executable lines and 30 added
branches were covered. The 131 byte-identical production modules lost zero
executed lines/arcs; historical reconciliation passed. Per-module results and
complete case IDs are in `/tmp/pr2c-baseline-to-features-comparison.json`.

The audit reporter adds three negative-control self-checks. Ordinary plus serial
time is the shared full-suite comparison: resource applicability is reported
separately. The PR2-C automatic Risk 3 plan selects local coverage/history and
all seven R gates plus the three hosted platforms. Its automatic packaging and
resource flags are false for these repository-only changes. The existing serial
distribution audit still rebuilds wheel/sdist and independently installs both;
the requested 18 resource tests additionally run at final head through the
existing bounded resource partition. None of those supplemental measurements
is substituted for complete local or hosted acceptance.

## Cost and setup audit

Times below come from accepted PR2-B's actual per-phase receipts. Worker-seconds
sum overlapping workers and must not be read as module wall time.

| Group | Aggregate worker-seconds | Assessment |
|---|---:|---|
| Vertex qualification execution | 1,599.92 | Many distinct authorization, integrity, privacy and recovery outcomes; retain all |
| Study native matrix | 1,036.30, including 533.40 setup | Existing worker/session proposal reuse retained; real native SDK bodies and full source verification remain |
| Gemini qualification execution | 571.12 | Same safety boundaries under a separate provider protocol; retain all |
| Study workflow | 514.20 | Positive sealing and complete 42-case execution/recovery are expensive, distinct integration checks |
| Study failures | 410.63 | Refusals, truncation, missing evidence, uncertainty and cancellation remain independent cases |

A focused synthetic Vertex four-case plan profile took 1.79 seconds with
instrumentation. SQLite methods accounted for 0.140 seconds of self time,
fsync for 0.119 and file-reading methods for 0.016. JSON snapshots, strict model
validation and native request commitments remain substantial. These categories
are measured self time, not mutually exclusive cumulative call-tree totals.
The profile is `/tmp/pr2c-setup-profile/vertex-plan.prof` and its full function
table is `profile.json`. This diagnostic ran during baseline acceptance and is
not an isolated speed comparison.

Five warm native-preview archive decodes took 0.0311–0.0373 seconds each;
original-preview decodes took 0.0049–0.0051 seconds. Caching the repeated native
decodes would save roughly two aggregate worker-seconds across the matrix.
That does not justify another shared mutable fixture or a new freezing layer.

The slowest serial integration exercises the complete source-bound pilot,
single-owner campaign, reservations and recovery. The R checks and cold SDK
probe also retain their isolation and existing deadlines. Child process launch,
independent installed packages and fresh SQLite repositories are part of the
asserted contracts; they cannot be replaced with in-process shortcuts.

## Bounded worker comparison

Use the existing ordinary-only runner on the **same 38 cases** from
`test_assessment_identity_retention_and_usage`, with work stealing and explicit
6/12 workers. Run AB then BA in fresh directories, after the complete feature
baseline finishes. Every case and assertion must pass in all four trials;
the runner retains collection, per-case reports, source/dependency provenance,
wall/CPU time and sampled peak RSS. Benchmark-only receipts never qualify a PR.

From the isolated feature checkout, the reproducible Python driver is:

```python
import subprocess
import sys

sys.path.insert(0, "ci")
from acceptance import clean_environment

for name, workers in (("ab", "6,12"), ("ba", "12,6")):
    subprocess.run(
        [
            sys.executable,
            "ci/local/suite.py",
            "--source",
            ".",
            "--output",
            f"/tmp/pr2c-workers-{name}",
            "--stage",
            "ordinary",
            "--no-coverage",
            "--benchmark-workers",
            workers,
            "tests/test_vertex_qualification_execution.py::test_assessment_identity_retention_and_usage",
        ],
        env=clean_environment(),
        check=True,
    )
```

The draft PR records all four raw times, worker counts, observed variability and
the resulting local worker choice. The measured ordinary partition was:

| Trial order | Workers | Cases passed | Ordinary seconds | Sampled peak tree RSS |
|---|---:|---:|---:|---:|
| AB-1 | 6 | 38 | 31.8504 | 1,184,763,904 bytes |
| AB-2 | 12 | 38 | 21.7184 | 2,039,586,816 bytes |
| BA-1 | 12 | 38 | 21.6376 | 2,040,930,304 bytes |
| BA-2 | 6 | 38 | 31.0928 | 1,185,787,904 bytes |

Mean ordinary time fell from **31.4716 to 21.6780 seconds (31.1%)**. The observed
ranges were 0.7576 seconds at six workers and 0.0808 at twelve. Including separate
collection, mean trial time was 32.6422 versus 22.8269 seconds. Waited-child CPU
rose from approximately 153.0 to 177.2 seconds. RSS includes shared-page duplication;
it is separate from authoritative fresh-process resource peaks. This is evidence
for a local **12-worker** acceptance invocation, not a general full-suite speed
guarantee. Final complete-suite measurements and coverage determine its acceptance.
Raw receipts are `/tmp/pr2c-workers-ab` and `/tmp/pr2c-workers-ba`, with 152 total
case passes, no failures and no skips. Each trial used the same source and case IDs,
fresh mutable stores and pytest cache; OS/dependency caches were warm. No other
qualification workload ran during the four worker trials.

The host has sixteen physical cores, thirty-two
logical CPUs and 128 GB RAM. No protected CI worker default, platform, timeout,
resource limit, skip list or coverage policy changes. Hosted Linux/Windows/macOS
timings remain separately labeled observations from different machines.

## Coverage, inventory and rejected changes

Generate the final comparison from actual receipts:

```bash
uv run --offline --no-sync python -m scripts.audit_test_efficiency \
  --source . \
  --baseline /tmp/pr2b-local-b27fc77-1/primary/suite \
  --before /tmp/pr2c-before-optimization/primary/suite \
  --after /tmp/pr2c-final-head-2/primary/suite \
  --output /tmp/pr2c-coverage-runtime-comparison.json
```

The report includes overall and every module's line/branch/combined percentages,
complete added/retained/removed case IDs, largest setup/call costs, process/resource
measurements and evidence hashes. Exact Git blob equality identifies unchanged
production modules. Their executed lines, branch arcs, denominators and exclusions
are compared directly; any loss is flagged. Branch identity across edited modules
is explicitly unestablished; existing historical/change-aware reconciliation
remains mandatory. Three negative-control self-checks prove the reporter detects
lost lines, lost arcs and changed exclusions even when aggregate coverage looks
healthy. No floor, baseline, exclusion or measurement setting changes.

No existing test is removed or consolidated, so the deletion-to-survivor mapping
is empty. Original test IDs and assertions survive. New control failure cases
reuse the existing failure, cancellation and process-death assertion functions
with new explicit-control inputs; they retain their own repositories, campaign
ledgers, clocks, HTTP transports and artifacts.

Rejected changes:

- Sharing Vertex/Gemini execution plans across mutable tests: seals contain
  directory-bound configurations and physical commitments, while tests also alter
  plan/evidence files. Safe reuse would require a separately reviewed redesign.
- Sharing mutable repositories, ledgers, HTTP mocks or clock state: breaks
  independent interruption, privacy and rollback detection.
- Deleting similar provider/parameter cases: endpoint/protocol/native/capture
  differences are distinct safety boundaries; shared lines do not prove redundancy.
- Parallelizing the complete pilot or loosening child deadlines: the assertions
  require single-owner ordering or bounded fresh processes.
- Removing R, distribution/installed compatibility, historical or resource checks:
  these are required guarantees, regardless of their runtime.

No broad test-framework refactoring is included. Further setup reuse should be a
separate proposal if measurements justify changing directory-bound test fixtures.
