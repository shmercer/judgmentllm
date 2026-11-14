# Contributor testing guide

The [CI policy](ci-optimization.md) sets acceptance requirements. This guide explains
how to add tests without losing those requirements. Maintainers and contributors
update metadata with the behavior they change; the protected base decides which
checks a PR must pass. All examples use offline synthetic data.

## Taxonomy and inventory

`ci/test-profiles.json` is the reviewed selection/impact registry. Its `modules`
object has one entry per test module, with `domain`, `cost`, `platforms` and a
plain-language `reason`. Domains cover core contracts, provider SDKs, immutable
evidence/privacy, durable execution, process ownership, native transport, R,
resources and packaging. Cost is `quick`, `io`, `process`, `heavy` or `resource`.
Empty `platforms` means platform-neutral; OS-sensitive defaults list
`["linux", "win32", "darwin"]`. Neutral tests still run in complete local
qualification and Level 4.

The `smoke`, `mandatory_smoke`, `extended` and `essential` arrays contain exact
module/function selectors. A function selector includes every parameter; it is
not a substring match. These provide per-node overrides for heterogeneous
modules. `providers` supplies conservative component closures; `shared_sources`
lists reviewed source paths. Unknown source dependencies escalate to full
platform verification. New independent adapters require a reviewed closure;
sharing a directory never proves independence.

`ci/test-inventory.json` is generated, with one line per function. Keys beneath
each function preserve exact parameter suffixes and isolation markers. No one
hand-edits thousands of complete node IDs. Required historical cases and marker
semantics from the trusted policy must survive collection. The older
`ci/test-contract.json` replacement and platform-skip protections also remain.
No original case is deleted by this redesign.

| Marker | Execution rule |
|---|---|
| `smoke` | Existing short essential regressions; platform smoke is additionally defined by exact selectors, without relabeling expensive tests |
| `serial` | A single pytest process, after ordinary xdist work; real child/lock tests retain their children |
| `resource` | Bounded resource partition with a lease and original workloads, ceilings and deadlines; local execution only |
| `r_interop` | Actually runs R/jsonlite on Python outputs; strict local acceptance, explicit hosted deferral |

Markers combine where needed. The pinned Anthropic cold SDK probe is `serial`,
not a deferred measurement. The inventory records this parameter override.
There is no invented platform marker: platform relevance lives in the registry.
Existing justified `skipif` cases must match the trusted skip allowlist; do not
add platform skips to conceal defects. Strict pytest markers remain enabled.

## Worked additions

A platform-neutral assertion can extend `tests/test_models.py`:

```python
import pytest
from pydantic import ValidationError
from judgmentllm.models import Level


def test_numeric_label_is_canonical_but_boolean_is_rejected():
    assert Level(label=1).label == "1"
    with pytest.raises(ValidationError):
        Level(label=True)
```

Its existing module default is `core-contract`, `quick`, `platforms: []`.
Recollect and reconcile the added function. A changed test module is selected
whole on affected lanes; any newly collected case is mandatory on every lane
even before it enters the trusted inventory. A new module needs the same real
JSON entry shape:

```json
{
  "domain": "core-contract",
  "cost": "quick",
  "platforms": [],
  "reason": "Pure canonical label validation without OS or external state"
}
```

For SQLite/child ownership, follow
[the real process lock tests](../tests/test_persistence_ownership.py) and
[cancellation processes](../tests/test_execution_cancellation_processes.py).
The ownership module is `process-ownership`, `process`, with all three platforms.
Its `test_independent_process_cannot_take_live_lock` and
`test_process_death_releases_lock_and_recovers_stale_metadata` are mandatory smoke
selectors. Retain actual subprocesses and independent SQLite handles. In that
module, only fork inheritance requires `serial`; ordinary child tests may run in
xdist with independent stores. The heavier study-limit child cases use `serial`
and an explicit `extended` function selector so the original 120-second deadline
holds without competing workers. Affected modules classified `heavy` use work
stealing rather than file affinity; the Windows subset cap remains two. Add new
OS-sensitive selectors/defaults alongside new behavior, including a negative
lock/cancellation assertion.

For an offline SDK test, follow
[OpenAI SDK bytes](../tests/test_openai_sdk_adapter.py) and the four provider
`test_production_seal_execute_export_reopen` lifecycle functions in the registry.
These use actual pinned SDKs and synthetic HTTP, compare exact wire bytes,
persist evidence, export and reopen without another call. The module defaults
are `provider-sdk`, `io`, all platforms, and the provider's `providers` closure
includes it. Use existing offline credential/socket guards. Cold imports that
need isolation follow the `serial` parameter in
[the native SDK test](../tests/test_native_other_sdk.py), preserving its deadline.

For actual R consumption, follow
[generation interoperability](../tests/test_generation_r.py): the Python reader
stays ordinary, while the real consumer has `@pytest.mark.r_interop`. Heavy
fixtures also use `@pytest.mark.serial` where the existing deadline requires it,
as in [execution interoperability](../tests/test_execution_r.py). Add the module
as `r-interface`, update inventory and the affected named gate. R must consume
actual Python-generated files; validate character IDs, complete mappings,
missingness, attempts and original/presented ordering. Missing R/jsonlite fails.

For bounded resources, follow
[execution resources](../tests/test_execution_resources.py) with
`@pytest.mark.resource`. Classify it `resource-stress`, `resource`, retaining the
fresh child, measurement assertions, workload dimensions and 180-second deadline.
A mixed module such as [study limits](../tests/test_study_limits.py) keeps its
ordinary default; the per-case resource marker overrides cost/isolation. A
resource success never replaces functional, coverage or R qualification.

## Development and acceptance commands

1. Read the design and policy; classify the behavior, platform reach and isolation.
2. Add independent positive/negative assertions and reuse existing fixture helpers.
   Share immutable inputs; give each mutable repository, ledger and campaign its
   own path. Session study proposals are hash-guarded and execution copies stores.
3. Update module defaults, exact selectors, provider closures and inventory. A
   changed parameter ID is a case change. Review the inventory diff explicitly.
4. Run focused feedback and relevant `ci/local` selector tests.
5. Commit, inspect the automatic PR plan, then execute every required local gate.
6. Check protected-base hosted acceptance at the same literal head. Review
   executed, deferred-by-policy and not-required-for-this-lane separately.

Prepare the locked environment once:

```bash
uv sync --locked --all-extras --group dev --python 3.11
python3 ci/check.py --profile fast
python3 ci/check.py --profile fast --test tests/test_openai_sdk_adapter.py
python3 ci/check.py --profile fast --test ci/local
python3 ci/check.py --profile pr --plan-only --changed-since origin/main
python3 ci/check.py --profile pr --changed-since origin/main --workers 12
```

`--workers 12` is the measured workstation choice, not a hosted default. The
plan prints each hosted profile, worker cap, distribution, reasons and local
requirements. Fast outputs are always nonqualifying. Local Level 2/3 acceptance
uses the complete Linux/Python 3.11 suite and historical branch coverage;
selected coverage cannot substitute. Hosted Level 2 Linux is impacted regression
plus shared essential contracts; Windows/macOS run smoke and affected components.
OS-sensitive changes strengthen those lanes to extended. Level 3 uses complete
primary and extended Windows/macOS regression. Level 4 uses full suites everywhere.

To reconcile inventory, keep collection evidence outside Git:

```bash
uv run --offline --no-sync python -m pytest --no-cov --collect-only -q tests \
  -p ci.local.pytest_report --ci-report=/tmp/jllm-cases.json \
  -o cache_dir=/tmp/jllm-pytest-cache
python3 ci/selection.py --collection /tmp/jllm-cases.json
# After reviewing legitimate additions and updating classification:
python3 ci/selection.py --collection /tmp/jllm-cases.json \
  --write-inventory ci/test-inventory.json
uv run --offline --no-sync python -m pytest --no-cov -q ci/local \
  --basetemp=/tmp/jllm-policy-tests -o cache_dir=/tmp/jllm-policy-cache
```

The validator blocks unclassified/stale modules, duplicate metadata/selectors,
stale parameter IDs, changed isolation and lost mandatory smoke. Trusted-base
inventory checks still reject removal of a historical case even if candidate
metadata was rewritten. Replacement needs explicit independent review and
preservation of the existing replacement contract; never silently regenerate
inventory to hide a removed assertion.

Pushing a PR triggers read-only candidate jobs controlled by `pull_request_target`
from its base. Feature-branch manual dispatch verifies the proposed policy
separately and publishes `CI candidate policy`, not protected-base acceptance:

```bash
gh workflow run ci.yml --ref YOUR_BRANCH -f risk=3
# Full supported hosted OS/Python matrix; also scheduled weekly:
gh workflow run ci.yml --ref YOUR_BRANCH -f risk=4
REF=$(git rev-parse HEAD)
python3 ci/check.py --profile comprehensive --ref "$REF" --mode prepare
python3 ci/check.py --profile comprehensive --ref "$REF"
```

The native grid is explicit, with its existing locks and 49 R executions. Do not
duplicate it for ordinary PRs. Release checks additionally require genuine
installed baseline/current interpreters and source archives:

```bash
python3 ci/check.py --profile release --ref "$REF" \
  --baseline-python /tmp/baseline/bin/python \
  --candidate-python /tmp/candidate/bin/python \
  --baseline-source-archive /tmp/baseline.tar \
  --candidate-source-archive /tmp/candidate.tar \
  --release-evidence /tmp/jllm-release-evidence
```

Use real prepared paths. This command records remaining release-readiness,
scale/compatibility and separately authorized live-qualification requirements;
it never publishes or authorizes provider calls.

## Diagnose failures without lowering gates

- **Unclassified module / new parameters:** collect the full suite, compare exact
  IDs and review registry/inventory changes; new cases are never silently omitted.
- **Unexpected skip/xfail:** restore prerequisites or fix the behavior/test.
  Required local R cases cannot skip; existing platform exceptions are explicit.
- **Missing receipt / wrong digest:** inspect the first failed partition, worker
  crash and artifact upload. Re-run invalidated checks at the same clean source;
  never copy success from another SHA, run, interpreter or profile.
- **R failure:** install required Rscript/jsonlite during preparation, then run
  offline strict consumers and all applicable named gates. Hosted deferral is not
  a local pass.
- **Missing coverage baseline/source:** restore the trusted floor and historical
  Git objects. Preserve the 90% combined floor, covered lines/arcs, changed-origin
  requirements and exclusion checks; do not regenerate a floor to pass.
- **Slow fixture:** use retained timing receipts first. Move cheap pre-admission
  faults ahead of expensive sealing, preserve positive paths, hash-guard shared
  immutable artifacts and isolate mutable stores. Keep real lock/process tests.
- **Native Windows publication/path failure:** keep generated data in the native
  adapter's `C:\jllm` namespace. Its optional scratch-root override is limited to
  nine characters before exclusive run allocation. Do not truncate IDs or
  filenames, change Windows settings, or suppress durable-publication assertions.
  Use the runner commands above: their reporter allocates compact, independent
  Windows pytest directories while preserving complete node IDs in receipts.

Every test PR should include classification/impact updates, complete case
accounting, focused feedback, the automatic plan, exact-source local/hosted
evidence, and separate passed/failed/deferred results. CI metadata, fixture and
collection changes also require the selector/inventory selftests.
