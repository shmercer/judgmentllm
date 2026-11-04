# Local cross-platform verification

This development harness runs the committed JudgmentLLM checkout on six local
configurations before publication: Ubuntu, Windows and macOS, each with CPython
3.11.18 and 3.12.16. It is separate from the installed library/CLI and does not
implement Milestone 2 Subphase 5. The infrastructure branch is based on proposed
performance commit `5a333b077b8a8a27ba2a80d85474b6eb802e206d`.

## Existing infrastructure and isolation

| Platform | Reused environment | JudgmentLLM state |
|---|---|---|
| Linux | Ubuntu 24.04.4 `pairwisellm-linux-ci:release` base image, R 4.6.1 | `/mnt/VM_Fast/judgmentllm-ci/linux` |
| Windows | Existing Docker-hosted `pairwise-windows-ci`, Server 2025, six CPUs, 12 GiB RAM | Host `/mnt/VM_Fast/pairwiseLLM-checks/judgmentllm-ci`; guest `C:\JudgmentLLM-ci` |
| macOS | Existing Quickemu `macos-sequoia`, macOS 15.8.1 x86_64, six CPUs, 16 GiB RAM | Host `/mnt/VM_Fast/judgmentllm-ci/macos`; guest `~/judgmentllm-ci` |

The Linux adapter creates a namespaced derivative image, preserving the existing
R project's image and tags. Containers have six CPUs and 16 GiB RAM. The Windows
adapter uses `docker start` on the existing container; it never invokes a VM
installer, recreates the container, changes Compose, or changes its disk/share
configuration. The macOS adapter invokes the existing Quickemu configuration
with `--display none --viewer none`. No replacement VM is created.

Windows and macOS adapters hold the existing pairwise runner's lock for their
entire operation, including result collection and graceful shutdown:

```text
/mnt/VM_Fast/pairwiseLLM-checks/windows-ci/.local-ci-windows.lock
/mnt/VM_Fast/pairwiseLLM-checks/macos-ci/.ci.lock
```

Never run an adapter while another project holds the corresponding lock. A
preexisting running VM is left running. A VM started by the adapter is shut down
gracefully; no VM process kill, disk deletion, removal/recreation of the existing
VM container, or forced VM stop is used. Keep an unattended invocation alive
until collection/shutdown completes.
Linux containers have a dedicated init process and per-run CID/ownership labels.
Interruption cleanup sends normal signals only to that run's disposable
containers, including when the Docker client exits before its container.
The first interactive inventory's macOS process disappeared when its tool
session ended; tool descendant cleanup is a possible cause. The adapter keeps
boot, work and shutdown in one process rather than relying on an interactive
session's daemon lifetime.

SSH uses the existing identities without copying or logging key contents:

```text
Windows: pairwiseci@127.0.0.1:2222, ~/.ssh/pairwise_windows_ci
macOS:   pairwiseci@127.0.0.1:22230, ~/.ssh/pairwise_macos_ci
```

Host-key checking and noninteractive authentication are required. `/mnt/VM_Fast`
must be mounted. The host needs Python >=3.11, Git, Docker, Bash, SSH, `flock`,
and the existing Quickemu installation. Effective `/dev/kvm` access is required
for macOS. The host's `ignore_msrs` setting is not changed; inventory successfully
booted this guest with its current value `N`.

## Prerequisites and offline boundary

Initial inventory found no uv/Python 3.11/3.12 in either guest. Windows had R
4.6.1 and jsonlite 2.0.0 in its system library. macOS had Python 3.9.6 and R
4.6.1, with jsonlite 2.0.0 in the existing pairwise library. Linux's existing
image had Python 3.12.3 and R 4.6.1 but no uv or jsonlite.

Preparation installs only project-specific tools and caches:

- uv 0.13.6 archives and executable bytes are pinned/verified. Official archive
  checksums are in `ci/local/grid.py`; macOS/Windows adapters also verify their
  cached executable bytes.
- Managed CPython 3.11.18/3.12.16 installs use their own directory, `--no-bin`
  and `--no-registry`; system Python, PATH startup files, and registry defaults
  are not replaced.
- `uv sync --locked --all-extras --group dev` populates a JudgmentLLM cache.
  Each check gets a fresh source directory and `.venv`; editable installs never
  refer to a previous run's checkout.
- Linux installs the same commit-pinned jsonlite 2.0.0 source used by GitHub CI
  into its derivative image. Windows reads the existing system library. macOS
  copies only jsonlite into its own library and verifies the original/copy
  bytes; it does not write to the pairwise library.
- Preparation warms isolated build and clean-install caches. Checks use
  `UV_OFFLINE=1`, `UV_PYTHON_DOWNLOADS=never`, locked offline sync, and the
  smoke installer's explicit `--offline` mode. Pyright uses its locked bundled
  implementation and Node wheel, with update/version downloads disabled.

Linux check containers additionally use `--network none`. SSH guests retain
their existing networking for transport; offline subprocess settings and the
credential-free synthetic tests govern execution. No provider credentials are
forwarded from the host. Preparation is the only dependency-download phase.
See [uv cache behavior](https://docs.astral.sh/uv/concepts/cache/) and
[managed Python configuration](https://docs.astral.sh/uv/concepts/python-versions/).

## Commands

Commit the proposed runner/test/documentation changes before qualifying them.
Use a full commit SHA. Uncommitted and untracked files are excluded from source
archives; the grid rejects an edited host harness that differs from that SHA.

```bash
REF=$(git rev-parse HEAD)
python3 ci/local/grid.py --ref "$REF" --mode doctor
python3 ci/local/grid.py --ref "$REF" --mode prepare
python3 ci/local/grid.py --ref "$REF" --mode check
```

The grid starts Linux, macOS and Windows concurrently, and each adapter runs
both Python versions concurrently: six independent configurations in parallel.
Shared tool bootstrap and archive verification precede a platform's Python lanes.
Each lane has its own checkout, virtual environment, scratch and results; cached
dependencies are shared only through the tool's locking. Existing VM locks remain
held for both lanes through collection and graceful shutdown. An individual lane
failure does not cancel its peer or the other platforms. Interrupting the grid
requests adapter cleanup and waits; it never sends a process-group signal that
could terminate Quickemu. Resource thresholds are unchanged, and parallel gate
times are qualification timings rather than isolated performance benchmarks.
Windows cancellation joins a private Job Object containing only the newly
launched lane process trees; the VM and SSH service are outside that object.
A separate SSH control call writes a source- and run-bound cancellation request
to the guest's private local directory; a shared-file request is only a fallback.
The native adapter-only TERM probe failed to stop workers with the shared-file
request alone. Cancellation now also uses guest-local control, avoiding reliance
on a newly created SMB file becoming visible.
Linux cleanup can remove a stopped disposable CI container after verifying its
full ID and exact run label, without force; mounted source/results are retained.
The two Linux containers retain six CPUs/16 GiB each; both Python lanes in a VM
share that VM's existing six CPUs and memory allocation. No test threshold is
adjusted for concurrent execution.
A platform can be inspected/prepared/checked independently:

```bash
python3 ci/local/grid.py --ref "$REF" --platform windows --mode check
bash ci/local/windows.sh --mode doctor
bash ci/local/macos.sh --mode doctor
bash ci/local/linux.sh --mode doctor
```

Standalone preparation adapters accept `--uv-archive PATH --uv-sha256 HASH`.
Linux additionally requires `--jsonlite-archive PATH --jsonlite-sha256 HASH`.
The grid downloads and verifies these assets in its own namespace. Cached assets
with mismatching bytes fail rather than being overwritten.

Only one adapter may operate on a guest at a time. The launchers normally handle
shutdown. For a VM deliberately started outside the launcher, acquire its shared
lock before requesting shutdown; do not interrupt another project's run.
Windows uses a direct ACPI `system_powerdown` request through its existing
`/run/shm/monitor.sock` and observes guest exit. Do not substitute `docker stop`:
the installed Windows image has its own approximately 105-second shutdown
escalation, independent of Docker's stop timeout. Earlier qualification recorded
container exit after `docker stop --timeout=-1`; that alone did not establish
graceful guest shutdown. macOS uses SSH `sudo -n /sbin/shutdown -h now`; the only
monitor fallback is ACPI `system_powerdown`. If graceful shutdown fails,
preserve the guest and report the environment limitation rather than
force-killing it.

Windows diagnosis can use its existing console at `http://127.0.0.1:8007`.
During native failure probes, SSH accepted TCP connections without sending a
banner although the desktop and OpenSSH service were running. A console
`Restart-Service sshd` under the shared lock restored access immediately; no
service startup settings or VM configuration changed. Treat readiness timeouts
as environment failures, inspect the console, and never force a reboot to
recover SSH. The runner does not silently restart services or waive readiness.

## Gates and evidence

Every check runs Ruff lint/format, strict Pyright, locked offline synchronization,
the full Python suite with branch coverage and the existing 90% gate, all six
strict R/jsonlite gates, wheel/sdist builds, strict Twine metadata and clean core
installation/CLI/execution smokes. Windows also runs its existing durability,
cancellation, privacy, lock and signature prechecks. ResourceWarning and
PytestUnraisableExceptionWarning remain errors. The 180-second execution resource
fixture timeout, 64-completion samples, memory limits, worker/task/queue bounds,
source scales, and all integrity/durability assertions are preserved.

The guest runner also runs its portable selftests. Source-admission/grid tests
are Linux-host tests, run with:

```bash
uv run --offline --no-sync pytest --no-cov ci/local
```

`source.py` creates `git archive` plus a manifest containing the exact commit,
archive SHA-256, lockfile SHA-256 and every tracked file's hash. Guests verify
archive bytes before extraction and tracked source bytes before/after gates.
Every output directory is fresh; existing runs are never replaced.

Machine-readable `summary.json` records Python/R/jsonlite/uv/OS versions,
dependency versions, source/lock identities, commands, exit codes, test/coverage
summaries, elapsed times, and retained log/artifact paths. Failures remain visible;
environment setup errors and timeouts are distinguished from check failures.
Partial suites, missing results, or successful preparation never qualify a lane.
The grid requires two fresh successful Python results per OS at the requested
commit. A six-configuration qualification requires all three OS adapters.

Grid evidence is under `/mnt/VM_Fast/judgmentllm-ci/grid/<run-id>/`; native details
remain in the per-platform roots above. Do not publish raw logs automatically.

## Resource tests and Windows scratch paths

The execution scaling tests now share one immutable numeric measurement of their
common `200×20` baseline. Each distinct workload still runs in a fresh child
process with its own inputs/database/executor. Only the returned numeric evidence
is shared; no child, prepared session, source cache or database is reused. The
two large independent workloads and all three grouped workloads remain. This
removes one redundant baseline subprocess (six distinct cases instead of seven),
without reducing the 64 completions, continuous allocation tracing, concurrency,
scales, native/Python memory bounds, or 180-second deadline for any child.
It reduces total work; it does not guarantee that a slow hosted machine will
finish the large child before its deadline.

Before that redesign, the original `072c1f69d85dca893bbac56ec91fe1753570d67f`
resource tests were run unchanged on local Windows/Python 3.11.18. The standalone
`200×200` case passed in 63.36 seconds wall. The actual resource files passed all
three tests/all seven original subprocess cases in 200.32 seconds. Fixture
elapsed times ranged from 12.84 to 62.88 seconds; all 64-completion, memory,
queue, task and scaling assertions passed. All 169 tracked file hashes matched
before and after. Evidence is retained under:

```text
/mnt/VM_Fast/pairwiseLLM-checks/judgmentllm-ci/diagnostics/
  resource-shorttmp-ready-3.11-20261104T155404Z/evidence/
```

An earlier diagnostic used a deeply nested test scratch directory and failed
artifact publication immediately at approximately 272-character object paths.
It was not a memory-fixture timeout. A fresh short scratch directory resolved
that setup failure. The runner uses separate short Windows scratch directories
for pytest, R fixture data and child temporary files, retaining their locations
in metadata while logs, coverage and distribution artifacts stay in the ordinary
results tree. It never changes the VM's global long-path registry configuration
or deletes another run's scratch directory. The probe attempts and failures are
kept alongside their successful retry; these diagnostic results do not replace
the required exact-head full six-configuration qualification.

## Parity differences and proposed hosted workflow

The existing hosted matrix is Ubuntu/Windows/macOS with Python 3.11/3.15.
The requested local matrix deliberately uses 3.11/3.12. Existing local guests
have R 4.6.1, while hosted Ubuntu/Python 3.11 installs R 4.6.2. All local lanes
require jsonlite 2.0.0 and all six actual R round trips, which is stricter than
the hosted lanes where R is optional except Ubuntu/Python 3.11. macOS is an
x86_64 Sequoia guest on an AMD host; it is not native Apple hardware. Local disks,
antivirus, CPU allocation and operating-system images differ from hosted runners.
These differences must appear alongside qualification results.

After repeatable local Windows/Python 3.11 qualification demonstrates the same
required assertions, the proposed routine hosted matrix retains Ubuntu and macOS
3.11/3.15 plus Windows 3.12 (five configurations). Hosted Python 3.15 qualification
remains required; the local 3.11/3.12 grid supplements that broader compatibility
coverage rather than replacing it. Windows 3.11 should remain
available through manual workflow dispatch. The GitHub matrix is not reduced
merely because the existing hosted job timed out, and no infrastructure branch
will be published before the local results and matrix proposal are reviewed.

The previous performance PR's hosted run `41848730145` passed five configurations.
Windows 3.11's first attempt reached its 20-minute job budget after a Python
fixture timeout and standalone R probe timeout. Its second attempt failed the
known 180-second `200×200` execution memory fixture; 1,367 tests passed and eight
optional R tests skipped. All 79 new integrity cases passed. That is evidence of
a timing-sensitive hosted test, not six-job acceptance or proof that infrastructure
alone caused the failures. No thresholds were changed to obtain a pass.

## Intel macOS native build prerequisites

The approved lock records only arm64 macOS wheels for `cryptography==50.0.2`,
which is required by the Google SDK extra. The existing Intel guest therefore
builds this exact dependency from source; no SDK extra or locked version is
removed. Its [official installation guide](https://cryptography.io/en/50.0.2/installation/#building-cryptography-on-macos)
requires a C compiler, Rust and OpenSSL, and supports static linkage. The
[openssl-sys manual configuration](https://docs.rs/openssl/0.10.81/openssl/#manual)
allows explicit library/header paths, avoiding a global pkg-config installation.

`macos-toolchain.sh` uses OpenSSL 3.5.9, the current
[3.5 LTS release](https://openssl-library.org/source/), and Rust 1.99.1
[standalone components](https://forge.rust-lang.org/infra/other-installation-methods.html#standalone-installers).
Exact official archive URLs and SHA-256 pins are in the helper. Every reuse
checks those fixed archive hashes, compares installed Rust payloads directly
with the pinned archives, and checks locally built OpenSSL files against their
recorded build/source/compiler provenance. OpenSSL is built with two compiler
jobs into the private prefix; the Apple command-line compiler is reused.

Native tools, downloads, temporary builds, Cargo registry/target caches and
Rustup state remain under `~/judgmentllm-ci/`. No Homebrew or system installation
is used. `MATURIN_NO_INSTALL_RUST=1` disables backend toolchain downloads; the
pinned Rust executables must exist before dependency synchronization. Check mode
also sets `CARGO_NET_OFFLINE=true`. The first failed prepare attempt downloaded
Rust through the dependency backend into `~/Library/Caches/puccinialin` before
this isolation was added. That initial cache is left untouched; subsequent runs
neither use nor modify it.

## Qualification record and publication gate

All six prerequisite preparations passed: Linux at `2b59f4601b28`, and both
Windows and Intel macOS at `a75b7fc9e16b`. Preparation includes locked dependency
installation, R/jsonlite, builds, metadata and clean-install smokes; it is not a
substitute for full qualification. The Windows multiline R probe failure and
Intel macOS native-build prerequisite failure were retained and corrected as
described above, without changing production code or the dependency lock.

The first complete Windows/Python 3.11 qualification passed on exact commit
`a75b7fc9e16bf3566da581f78d198d84e7e6f675`: **1,405 tests passed, one existing
Unix-only fork-inheritance test skipped, 95.97% coverage**. Its 33 steps passed,
including strict Pyright, Windows prechecks, all six strict R/jsonlite gates,
build/Twine and both clean-install smokes. Full gate time was 660.52 seconds;
the pytest step took 593.38 seconds. Source bytes matched before/after. This
joins the separate repeated large-resource checks above as evidence supporting
routine hosted Windows 3.11 removal; neither a partial suite nor preparation
alone was used for that decision.

The companion Windows 3.12 run exposed an existing mock-provider test that used a
30 ms sleep to assume completion order despite worker-thread and journal timing.
The test now uses an event handshake, still requiring the configured `0.03`
delay path, real submissions, unchanged membership and two acceptances. All 26
mock-provider tests passed on Linux and native Windows 3.11/3.12 after that
correction. No production behavior or timeout was changed. The attempted
`b15d1e7da0d0` grid was explicitly interrupted to incorporate the known fix; its
partial results do not qualify any configuration.

That interruption also exposed a Linux launcher cleanup gap: a Bash container
init could outlive its interrupted client. The launcher now uses Docker init,
process-group signal forwarding, `exec`, and guarded CID/ownership-label cleanup.
Six tiny lifecycle probes passed (normal exit, adapter INT/TERM, client INT,
whole-group INT and partial CID creation), with no owned container remaining.
The existing Windows VM and other projects' containers were never cleanup
targets. The corrected source requires a new full exact-head qualification.

```text
Source archive SHA-256:
5501c0d479b95fa82c24fbae5e1dd0d190f589428a304a19700d0e923c0d0164
Dependency lock SHA-256:
fb46854f753890cd9cc8006631e06345ad6e6417e150ed5d6843d16d905b77a3
First Windows full-gate evidence:
/mnt/VM_Fast/pairwiseLLM-checks/judgmentllm-ci/results/
  windows-20261104T160646Z-408309-15804/python-3.11/summary.json
```

The proposed routine workflow retains Ubuntu/macOS 3.11/3.15 and Windows 3.12.
Manual dispatch with boolean `windows_311_only=true` runs only Windows 3.11
through the same complete job; the default dispatch runs the routine five.
Steps, action pins, required R conditions, coverage gates and timeouts are shared
and unchanged. Actionlint and matrix-selection regressions validate the proposal.

Before publication, run `grid.py --mode check --ref FULL_HEAD_SHA` for all three
OS adapters and require `qualification: true` in the fresh grid summary. The
reported commit must still equal `git rev-parse HEAD`; later source, test,
workflow or harness changes require a new exact-head qualification. Final results
are generated outside Git under the per-commit grid ledger, avoiding a results
commit that changes the revision being qualified. The execution report for
review links that exact ledger, all six native results and any failures. The
infrastructure branch remains unpublished until that review. No merge, release,
or Subphase 5 work is authorized.
