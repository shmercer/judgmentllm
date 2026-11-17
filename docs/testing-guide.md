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
Empty `platforms` means platform-neutral; OS-sensitive defaults list the relevant
subset of `["linux", "win32", "darwin"]`. `platform_overrides` supplies exact
function placements and reasons for mixed modules. Neutral tests still run in complete local
qualification and Level 4.

The `smoke`, `mandatory_smoke`, `extended` and `essential` arrays contain exact
module/function selectors. A function selector includes every parameter; it is
not a substring match. These provide per-node overrides for heterogeneous
modules. `providers` supplies conservative component closures; `shared_sources`
lists reviewed source paths. `provider_sources` binds each reviewed provider
source path to its nonempty provider test closure, including native schema,
dispatch and directly importing tests. A provider name prefix never establishes
a dependency closure. Unknown source dependencies escalate to full
platform verification. New independent adapters require a reviewed closure;
sharing a directory never proves independence. A Level 2 production source
without a verified nonempty closure escalates to full platform verification;
default fast feedback also expands to the suite. Add an exact source entry and
policy regressions when establishing a new narrow closure.

`ci/test-inventory.json` is generated, with one line per function. Keys beneath
each function preserve exact parameter suffixes and isolation markers. No one
hand-edits thousands of complete node IDs. Required historical cases and marker
semantics from the trusted policy must survive collection. The older
`ci/test-contract.json` replacement and platform-skip protections also remain.
No original case is deleted by this redesign.

| Marker | Execution rule |
|---|---|
| `smoke` | Existing short essential regressions; platform smoke is additionally defined by exact selectors, without relabeling expensive tests |
| `serial` | A single pytest process; Levels 3/4 use a separate hosted VM alongside ordinary xdist work; real child/lock tests retain their children |
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
whole on the primary lane; OS lanes select its platform-relevant cases.
Any newly collected case is mandatory on every logical lane
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

For a new provider source, merge exact source bindings and its complete test
closure into the existing registry. For example, an approved future offline
Together response parser could add the following entries. Preserve other
`shared_sources`, provider bindings and existing test-closure members:

```json
{
  "provider_sources": {
    "src/judgmentllm/providers/together_response.py": "together"
  },
  "providers": {
    "together": [
      "tests/test_native_other_providers.py",
      "tests/test_native_preseal_validation.py",
      "tests/test_native_schema_privacy.py",
      "tests/test_native_selection.py",
      "tests/test_native_v2_compatibility.py",
      "tests/test_together_response.py"
    ]
  },
  "shared_sources": [
    "src/judgmentllm/providers/together_response.py"
  ],
  "modules": {
    "tests/test_together_response.py": {
      "domain": "provider-sdk",
      "cost": "quick",
      "platforms": [],
      "reason": "Offline response-parser shapes and errors; transport has independent platform coverage"
    }
  }
}
```

Add the actual positive/negative tests, reconcile inventory and verify the
direct-import/selection regressions. A mapping permits narrow selection only
when the risk rules also allow it; credential, privacy, shared authority and
recovery changes still escalate. Editing the registry itself requires Level 3.

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
python3 ci/check.py --profile pr --changed-since origin/main
# Explicit conservative ordering remains available:
python3 ci/check.py --profile pr --changed-since origin/main --local-schedule conservative
```

For a parse-only edit to an already reviewed provider response module, a Level 2
plan can look like this (worker/scheduling details abbreviated):

```text
Risk 2; plan: /external/pr-check/plan.json
  linux-3.11: impacted
  windows-3.12: platform-smoke
  macos-3.11: platform-smoke
Local requirements: coverage=True, R=['exports', 'execution'], resources=False
```

Linux runs the affected closure plus essential contracts; the other platforms
retain their genuine smoke checks. Local Level 2 acceptance requires strict changed-code coverage and the applicable R gates. A Level 3 plan after editing executable CI policy, existing assertions or shared behavior is expected. For an unexpected escalation,
read `changes[].reason`, `unknown_production_dependencies`, `new_or_unclassified_tests`, `fallback_reasons`, `selection_reasons` and `impact`
in the saved plan. Check for an unreviewed source path or a sensitive boundary;
resolve the classification with evidence. Diff size and a lower `--risk` argument
cannot lower the automatic plan.

Omitting `--workers` selects 10 ordinary workers for approved Ubuntu concurrency
or 12 for conservative ordering, bounded by effective CPUs. Explicit worker caps
remain available; concurrent workers never exceed the reviewed allocation. The
plan prints each hosted profile, worker cap, distribution, reasons and local
requirements. Fast outputs are always nonqualifying. Local Level 2 acceptance uses impacted Linux tests and strict changed-code coverage.
Levels 3/4 use the complete Linux/Python 3.11 suite and historical branch coverage;
partial coverage cannot substitute. Hosted Level 2 Linux is impacted regression
plus shared essential contracts; Windows/macOS run smoke and affected components.
OS-sensitive changes strengthen those lanes to extended. Level 3 uses complete
primary and extended Windows/macOS regression. Level 4 uses full suites everywhere.
Generic serial semantics need complete primary coverage. Real process recovery,
locks, SDK identity and native path behavior retain their reviewed OS placements.
Full Windows runs are reserved for Level 4 and conservative unknown-dependency
fallback.

At Levels 3/4, `hosted_tasks` seals ordinary/serial tasks for each logical lane.
Both VMs collect the whole suite, execute disjoint cases and bind receipts to
the source, plan, workflow attempt, lane and partition. Acceptance requires both
receipts and matching collection universes. `covered-by-grid-partition` means
required on the other VM; it is never a passed case in this receipt. Local
coverage/R/resources still use the complete local lane. Levels 0–2 avoid another
VM setup for their small serial scope. The hosted job cap is 90 minutes; child,
fixture, resource and coverage limits are unchanged.

To reproduce one hosted task without measuring coverage or running R/resources:

```bash
# Use a committed head and the plan written by --profile pr --plan-only.
uv run --offline --no-sync python ci/acceptance.py lane --source . \
  --plan /external/plan.json --lane linux-3.11 --partition ordinary \
  --execution-scope hosted --run-id diagnostic:1 --output /external/ordinary
uv run --offline --no-sync python ci/acceptance.py lane --source . \
  --plan /external/plan.json --lane linux-3.11 --partition serial \
  --execution-scope hosted --run-id diagnostic:1 --output /external/serial
```

These are task diagnostics. Required hosted qualification comes from the complete
GitHub grid and trusted aggregation; a single task cannot qualify a PR.

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

## Independent additions and local concurrency

For Level 2 additive tests, preserve the complete existing module AST, including
assertions, decorators, fixtures, markers and parameter IDs. Add a unique test
function with literal parametrization; put any newly needed imports inside that
function. New modules may have ordinary imports, a docstring and independent
tests. Adding fixtures, helper implementations or collection hooks requires Level 3.
Reconcile collection and use strictly additive inventory/module metadata. New
metadata never narrows protected-base requirements.

See [Issue #59 operations](ci-issue59.md) for targeted coverage diagnostics, the
reviewed serial dependency hashes, resource applicability and scheduler evidence.
`--local-schedule auto` prefers the approved concurrent Ubuntu profile when
systemd ownership and CPU/RAM safeguards are available, otherwise diagnoses the
conservative fallback. No performance certificate is required. The retained
23.64% improvement applies to the 254-case/seven-R workload; it is not a measured
improvement for the entire 8,611-test suite. Latency observations remain recorded
and diagnostic; real deadline failures and all correctness, coverage, R, resource
and isolation requirements remain blocking. Shared
foreground allocation covers native grids and background main work; no permanent
main timer may be activated before independent review and merge.

The scheduling contract is checked at every local acceptance level, including
when the two reviewed overlap tests are absent from the selected lane. Inspect
the plan's `scheduling_contract.placements` for exact dependency differences and
effective fallback placement. A stale fingerprint is a blocking
`requalification-required` result, even if a diagnostic run passed with global
isolation. Do not refresh fingerprints or write an approval to make a check
green; prepare the source-bound report for independent review. Later CI,
classification, reporting and coverage changes must repeat this check.

An unrelated ordinary test must not invalidate overlap merely because the full
inventory JSON changed. Add independent tests using this scaffold, retaining
the established module AST and using private state:

```python
def test_new_independent_case(tmp_path):
    from judgmentllm.some_existing_module import existing_function

    result = existing_function(tmp_path / "private-state")
    assert result == expected_result
```

Replace the example import and assertion with the real contract. Keep ordinary
tests unmarked unless they need a documented isolation boundary. Classify the
module/function in `ci/test-profiles.json`, collect and reconcile the complete
inventory, and inspect the automatic plan. The scheduling closure follows only
the reviewed tests' helpers plus shared source/fixture/runtime controls; it
does not hash unrelated test bodies or the entire inventory into both placements.
An added fixture, helper implementation, plugin or collection hook remains
Level 3 and must be reviewed for its actual effect.

For new `serial` or `resource` cases, also propose an exact function/parameter
selector and nonblank reason in `ci/local-policy.json`'s `isolation_reasons`:

```json
{
  "tests/test_example.py::test_real_child_boundary":
    "Real child shares an OS lock; preserve its existing deadline and cleanup assertions"
}
```

The reason documents the proposed boundary; it grants no overlap authority.
Unknown serial cases retain global isolation. A missing reason blocks admission.
Review helpers transitively, parent package initializers, autouse fixtures,
applicable root/ancestor conftests, pytest plugins and dynamic dispatch. The
runner bounds conftest discovery to the candidate source with `--confcutdir`.
Repository-local plugins and helpers outside `tests/` are traversed too, including
their package initializers. Relative `import_module` calls with literal package
and module names and simple loader aliases are resolved. Variable packages,
escaped/rebound loaders and unsupported module names require independent review.
Nonliteral helper imports need a finite
explicit target list bound to the exact helper source hash in
`serial_dependency_scope.dynamic_imports`; an unknown or edited dispatch fails
closed. Keep all real source-inventory reads at their existing call sites.
Neither a setup optimization nor a diagnostic timing permits caching those reads,
removing assertions, reducing the 42-case pilot or relaxing recovery/deadlines.

An unplanned full suite remains diagnostic even when every case passes. Its
`qualification: false` result propagates through the native adapter; complete-grid
admission requires the exact reconstructed local scheduling contract as well as
the existing correctness, coverage, R and resource evidence. A planned local
suite checks admission before allocating evidence at every risk level. Synthetic
driver integration tests bind their private lane to the running host/interpreter;
the real lane mismatch checks and Linux/Python 3.11 primary policy remain intact.
