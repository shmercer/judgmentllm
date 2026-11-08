# First PyPI alpha release: readiness and user experience plan

**Status:** Planned; not a claim of feature completion, live qualification, PyPI-name ownership, or
publication. This is a separate release-readiness workstream linked from the [implementation
plan](implementation-plan.md). Implementation may begin alongside the remaining adapters;
**publication is gated on completion and live qualification of all intended provider/endpoint families
in Milestones 3 and 4**. A development version such as the current `0.1.0a3` is not itself a published
or accepted alpha.

**Release boundary:** First public, installable **Python library and command-line application** for
actual live rubric scoring and comparative judgments. This is an explicitly early **PEP 440 alpha**:
document unstable APIs/limits and do not advertise unimplemented features. Milestone 5 (native batch
and managed bulk-live), Milestone 6 (automated model catalog and thin R package), and Milestone 7
(FastAPI) remain committed *later* deliverables, **not alpha blockers**. Existing R-compatible exports
and required interoperability tests remain alpha blockers.

## A. Definition of ready and supported scope

- [ ]  Complete the applicable Milestone 3 live-adapter, evidence/export, diagnosis, and qualification
      acceptance for **OpenAI Responses and Chat, Anthropic Messages, Gemini Developer, Vertex Gemini,
      Together, and Ollama**; complete Milestone 4 acceptance for **OpenAI Decisions and TypeSafe
      Jev**. Account/region/model access and SDK releases may differ; record the precise successfully
      qualified combinations rather than implying universal model support.
- [ ]  Maintain a *human-readable, dated* alpha support matrix: provider, endpoint family,
      authentication mode, installation extra, tested model/version/region (where relevant), rubric
      scoring, forced-choice pairwise, explanation availability, selected score versus probability
      distributions, structured output, usage/provenance, supported controls, and explicitly
      unsupported features. Use separately scoped `implemented`, `fixture_tested`, and
      `live_qualified` statuses. The generated catalog remains Milestone 6.
- [ ]  For **each advertised live provider/endpoint**, demonstrate documented rubric and pairwise
      workflows to the extent supported by its actual contract; for the required first-release
      workflows, qualify a real run through the normal installed CLI and shared Python services,
      including output/export and later-process status/resume where applicable. Never invent
      probability distributions, hidden reasoning, native features, or batch support. An
      endpoint-specific limitation must be visible before billable submission.
- [ ]  Separate (a) general backend support, (b) a specific model/endpoint/feature live qualification,
      and (c) account-specific access. Neither successful `auth status`/`auth verify` nor a passing
      SDK mock proves live model support. All paid/manual smoke tests require explicit authorization,
      synthetic inputs, bounded requests/output, nonsecret evidence, date/version, observed results,
      and reported/unknown costs.
- [ ]  Treat live **single-run** execution as the alpha user experience. Identify native batch,
      managed bulk-live, caching modes, auto-discovery/catalog automation, R wrapper, and HTTP/API as
      *not yet included* unless independently implemented and qualified before publication. Do not
      make them hidden requirements, and do not list optional extras as functional integrations merely
      because their SDKs install.
- [ ]  Preserve existing frozen source/payload identities, versioned contracts, historical-store
      compatibility, audit exports, conservative retries/recovery, and mandatory R gates. Any
      incompatible public change needs an explicit version/compatibility note rather than an invisible
      migration.

**Go/no-go:** Every intended live provider family has evidence for the **advertised** minimum real
scoring and comparison workflow, all mandatory offline/hosted/local gates pass at the release commit,
a novice can follow the first-run guide from an isolated install, and release artifacts pass
independent installation and provenance checks. If a required family is not live-qualified, defer the
alpha; changing this all-provider criterion needs a recorded, explicit scope decision, not a silent
downgrade.

## B. Beginner-first documentation and navigation

The GitHub README and PyPI project description are **front doors, not development logs**. Keep deep
contract/engineering records accessible under a clearly separated contributor/developer section.

- [ ]  Rewrite the top of `README.md` around **what JudgmentLLM does**, who it is for
      (researchers/assessors), what it does *not* do (no validated fairness/accuracy, no BT fitting),
      current alpha limitations, a working installation command, a safe five-to-ten-minute **synthetic
      no-cost first run**, a separately labeled **real provider** example, and a clear table of
      contents/links. Move milestone status and qualification details below or to developer documents.
      State that alpha is prerelease and show the correct explicit `--pre`/version-pinned installation
      instruction.
- [ ]  Create a clearly named **Start here** documentation hub (e.g., `docs/user-guide/README.md`),
      with a short route for beginners and a separate advanced route. Plan and link these pages:
  -  `installation.md`: supported Python versions and OS, pip/virtual-environment and uv alternatives,
    supported provider extras, Windows PowerShell and macOS/Linux terminal commands, verifying
    `judgmentllm --help`, updates and clean uninstall. **No source checkout, uv, R, Docker, or FastAPI
    is required** for a normal installed CLI user.
  -  `first-run.md`: a complete, copy/paste **offline synthetic** example using shipped/downloadable
    example files, with annotated input, commands, expected console output, where output files appear,
    and how to inspect them. Explain "rubric", "pairwise judgment", "provider", "trait", "run", and
    "expected score" in plain language; label steps that do/do not contact an API.
  -  `rubric-scoring.md` and `pairwise-judgment.md`: two complete task-oriented workflows from a tiny
    CSV/example rubric or pair list through validation, preview, (authorized) run, status, results
    export, and interpretation, including ties/failures/reversed presentation and the meaning of score
    versus expected score where relevant.
  -  `providers.md` plus short provider-specific sections/pages: which extra to install, where to
    obtain/configure credentials **without putting keys in YAML**, model/endpoint selection, Vertex
    versus Gemini Developer, Decisions versus generative OpenAI, TypeSafe Jev, local Ollama
    prerequisites, supported/unknown features, explicit billable-action warnings and links to the
    dated support matrix. Avoid undocumented credentials, assumptions about free access, and stale
    provider pricing.
  -  `outputs.md`: explain the run ID, output directory, canonical JSONL versus simpler CSV/R views,
    which rows represent attempts versus completed outcomes, missing values, probability-weighted
    scores, usage and separately authorized reasoning, and how to avoid presenting model-generated
    scores as calibrated grades.
  -  `cost-privacy-safety.md`: what makes network/paid requests, preview versus run, token/cost
    uncertainty, full versus minimal capture and residual metadata, storing sensitive student
    responses, provider-side data handling to verify, local SQLite directories (not synced/network
    folders), retries, cleanup/retention and avoiding secret disclosure.
  -  `troubleshooting.md` and `faq.md`: missing Python/executable/extra/key, wrong model/endpoint,
    permissions/Vertex auth, local Ollama daemon, malformed rubric/input, no network, rate limits,
    partial submissions, safe resume/export, "where did my files go?", and how to report issues with
    redacted diagnostics.
- [ ]  Provide a compact **documentation index** separating *Get started*, *How-to guides*,
      *Concepts*, *Reference* (CLI/Python API/configuration/output schemas), *Provider support*, and
      *Development/contributing*. Keep the current detailed `docs/cli-spec.md`, `docs/python-api.md`,
      R design and verification material as reference/engineering sources rather than requiring
      beginners to read them.
- [ ]  Make every tutorial executable and clearly labeled **offline/mock** or **live/billable**. Ship
      tiny **synthetic, non-sensitive** sample inputs, rubrics, configs, expected outputs and a "start
      here" example path usable from outside a Git checkout; do not require copying real student work,
      choosing arbitrary models, or creating a template to get started.
- [ ]  Use plain-language explanations before specialized terminology, numbered instructions that
      state **expected result and recovery step**, tested OS-specific shell syntax, consistent command
      names/file layout, linkable headings, and links that resolve on GitHub and in the PyPI long
      description. If a docs site is added, keep Markdown navigable in GitHub and avoid a site
      framework as a release blocker.
- [ ]  Document the public Python API with at least one installed-package end-to-end example, typed
      signatures, error handling and import patterns; distinguish Python API from CLI and show when to
      use each. Preserve a separate short R-export guide for existing R users without claiming the
      future `rubricLLM` wrapper is released.
- [ ]  Recruit at least one reviewer **not familiar with the codebase** to follow the offline guide
      and the provider-setup instructions in a clean environment. Record confusion/failed steps; the
      minimum benchmark is an independent first run without editing code, requiring hidden repo files,
      or asking the author to explain core terms.

**Documentation acceptance:** The README's first screen tells a new user what they can do *today*.
Every advertised command, filename, link, output sample, and installation extra is validated against
an **installed wheel**. Automated offline tutorial smoke checks (or equivalent scripted command
assertions) cover starter workflows; authorized manual live examples are independently checked per
provider and marked by model/date. No missing or prospective step masquerades as implemented.

## C. Installed-package experience and package layout

- [ ]  Verify `pip install --pre judgmentllm` (and a specific version), `pip install
      "judgmentllm[PROVIDER]"`, `uv tool install ...` when applicable, the installed `judgmentllm`
      executable, and `import judgmentllm` from **outside** the repo. Document isolated environments,
      optional dependencies and the meaning of prereleases. Core installation must not pull in all
      provider SDKs, R, FastAPI or PyArrow.
- [ ]  Check package name normalization and availability on PyPI **before publishing**; a GitHub name
      or a *pending* Trusted Publisher does not reserve a PyPI name. Confirm packaging metadata
      (`project.description`, long README, author/license/classifiers, Python versions, keywords,
      project URLs including Documentation/Changelog/Issues as appropriate, entry point,
      dependencies/extras, and `py.typed` marker) accurately reflect the supported alpha.
- [ ]  Inventory `src/judgmentllm` public imports, CLI command groups, defaults and examples:
      consistent naming for provider/endpoint/mode, safe and actionable errors, `--help`
      discoverability, deterministic config validation, a no-cost `validate`/`plan`/`preview` path,
      and explicit confirmation for potential billable operations. Make real provider failures explain
      **what went wrong and how to fix it** without exposing secrets or raw sensitive responses.
- [ ]  Keep package resources (built-in prompt templates, schemas and any installed examples) readable
      through package-relative/resource APIs, not repository-relative paths or an editable install.
      Ensure the **wheel**, **sdist**, and published artifact contain precisely the needed runtime
      files, valid license and typing metadata; exclude secrets, development stores, private fixtures,
      large benchmarks, platform-specific paths and unnecessary artifacts. Developer-only
      scripts/fixtures may remain in the sdist when justified.
- [ ]  Exercise core and each advertised extra in clean Python 3.11+ environments on Linux, macOS and
      Windows; verify resolver conflicts, package import, CLI help, offline examples and appropriate
      error behavior when an optional provider SDK is absent. State tested combinations instead of
      promising unqualified runtimes.
- [ ]  Preserve machine-readable CLI stdout/stderr/exit-code contracts for R/external automation while
      improving human-readable help. Never turn errors into silent successes, default to an
      unannounced model/endpoint, or make live calls during install/import/help/status.
- [ ]  Define how users report bugs and request features (GitHub Issues, issue template with
      versions/OS/steps/expected vs actual behavior and a **redaction warning**), where they find
      supported-version and changelog information, and the non-guaranteed alpha support/stability
      policy.

**Experience acceptance:** A clean user installation works without development dependencies and
without the source checkout; optional extras are truly optional. First-run and error-path tests
exercise the **installed artifact**, not merely `uv run` in the repository.

## D. Release integrity, publishing, and operational controls

- [ ]  Establish PyPI and separate TestPyPI accounts with verified email and two-factor
      authentication; confirm ownership of the normalized `judgmentllm` distribution name, maintainers
      and recovery controls. Document how a name conflict would be handled without publishing a
      misleading package.
- [ ]  Add an isolated **GitHub Actions Trusted Publishing** release workflow with a guarded `pypi`
      environment, least-privilege permissions, OIDC `id-token: write` only for the publish job,
      pinned/reviewed actions, and no long-lived PyPI API token. If using TestPyPI, set its distinct
      trusted publisher/environment/index correctly. No production upload on a PR or unreviewed source
      branch.
- [ ]  Treat building and publishing as separate jobs: build **once** from a protected approved
      release commit/tag, verify the build and tag versus `judgmentllm.__version__` and
      `pyproject.toml` metadata, produce wheel/sdist, run strict `twine check`, clean wheel/sdist
      installs, artifact contents/secret scan and sha256 record, then publish those same approved
      files. Preserve artifact hashes, source commit, tool versions and provenance/attestations where
      available.
- [ ]  Define the **PEP 440** version/tag policy (`0.1.0aN` and `v0.1.0aN`, or another explicit
      choice), do not infer release acceptance from the present development version, and ensure
      published versions/filenames are unique; never plan to overwrite a PyPI distribution. Document
      how to fix a bad release by shipping a new version or, if appropriate, yanking it.
- [ ]  Use TestPyPI or a controlled dry run to test publishing authorization and metadata before the
      actual first release. Remember TestPyPI uses a separate index/account and might not contain
      dependencies; validate installation of the **exact wheel/sdist** independently rather than
      silently resolving everything from a mixed index.
- [ ]  Require a human approval gate for the first production publish. Release notes/GitHub release
      must state: alpha stability and breaking-change expectations, providers and **exactly
      qualified** models/features, installation and first example, known limits, security/privacy
      considerations, R interoperability/export support, and changes from the previous release (if
      any).
- [ ]  After publishing, verify PyPI long description and project links, distribution hashes and
      provenance, installation by a new user with prerelease opt-in, installed CLI/import, offline
      example, and the release's documented real-provider sample where explicitly authorized. Record
      the actual PyPI version, Git tag, commit and verification evidence. Never publish private
      grading data or real credentials.
- [ ]  Document repeatable alpha patch/pre-release cadence, compatibility/migration guidance for
      config/schema/store upgrades, dependency vulnerability handling, minimal triage/reproducibility
      rules and who can approve/publish releases. Do not commit API keys or create indefinite
      external-access permissions.

**Release acceptance:** Production publication is an **explicit last step**, not a side effect of
merging documentation or of green CI. A reviewed release checklist references exact passing artifacts,
verified package contents, a tested trusted-publisher path, nonsecret/manual live qualifications, docs
review, and an approved release tag.

## E. Verification evidence and implementation sequence

Keep these tasks in **small PRs**, independent of live adapter development. Each relevant PR retains
all existing exact-head CI, strict R/jsonlite and historical compatibility requirements; do not lower
test thresholds or replace genuine provider qualification with mocks.

1.  **User experience and documentation IA (can begin now):** README structure, user guide and docs
   index, novice first-run mock walkthrough, example inputs/expected outputs, missing-optional-SDK
   help and actionable errors. Test commands against installed wheel.
2.  **Packaging and pre-publish controls (can begin now):** metadata/license/typing/resources audit,
   optional-extras clean-install matrix, version/tag check, release workflow, TestPyPI dry run and
   no-production-on-PR safeguards. Keep production publishing disabled until approved.
3.  **Provider guides and live usability (parallel with M3/M4 completion):** per-provider credential
   and endpoint directions, real rubric/pairwise examples, dated capability matrix, sanitized
   troubleshooting and explicit billable manual smoke records. Each guide lands with its provider
   rather than relying on a late documentation-only sweep.
4.  **Alpha candidate qualification (after all intended live providers qualify):** final independent
   docs walk-through; full local/hosted quality gates and applicable resource/R gates; historical
   store/read compatibility; wheel/sdist/CLI checks on supported OS/Python combinations;
   privacy/security audit; release notes; release artifact freeze.
5.  **Approved alpha publication:** create protected version tag, approve guarded trusted-publisher
   run, verify artifacts on PyPI, reproduce clean install and first workflow, and record
   version/commit/known limitations for the next alpha.

### Required alpha evidence checklist

- [ ]  A provider-by-provider **advertised versus actually qualified** matrix exists, with no missing
      mandatory live families.
- [ ]  Real rubrics and pairwise evaluations complete on representative synthetic input through the
      installed public interfaces, not internal fixtures only; endpoint-limited cases are documented
      honestly.
- [ ]  Zero-credential/mock onboarding and external-wheel installs succeed on each supported OS; an
      independent beginner can find output and interpret a failure without developer help.
- [ ]  Tested help/README/tutorial commands, relative file links, package metadata and included
      resources match the artifact users install.
- [ ]  Clean tests, provider fixtures, strict R gates, resource checks and previously required
      historical compatibility pass at the **exact release commit**, with offline CI remaining
      credential-free.
- [ ]  Security/privacy, no-key-in-config and capture/retention guidance are accurate; no secrets,
      private work, or synthetic-as-real claims appear in wheel/sdist/README/logs.
- [ ]  Build provenance and SHA-256 hashes are retained; tag/version match; TestPyPI or equivalent dry
      run and approved PyPI Trusted Publishing are verified.
- [ ]  GitHub release notes, known limitations, issue-reporting route, version/migration policy and
      post-upload installation check are complete.

### Official packaging references (recheck when implementing)

- [Python Packaging User Guide: packaging projects](https://packaging.python.org/en/latest/tutorials/packaging-projects/)
- [Python Packaging User Guide: versioning](https://packaging.python.org/en/latest/discussions/versioning/)
- [PyPI: Trusted Publishing](https://docs.pypi.org/trusted-publishers/using-a-publisher/)
- [PyPI: creating a project through a pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)


## Phase 5A alpha integration

The OpenAI user guide documents Python>=3.11, the `[openai]` extra, explicit V3
configuration, credentials, offline preview/validation, both generative endpoints,
run/resume/export, and storage/recovery limits. Native production eligibility
requires separately reviewed exact-model feature evidence. Model visibility,
installation and successful authentication are not qualification. No PyPI
publication is authorized by Phase 5A; exact-head offline/hosted acceptance and
separately authorized synthetic live qualification remain required before merge.
