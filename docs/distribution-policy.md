# Public distribution contents

The wheel and sdist ship the Python library, ordinary CLI, runtime templates,
typing markers and stubs, MIT license and README metadata. The sdist allowlist is
`src`, `README.md`, `LICENSE` and `pyproject.toml`, with the repository-only
`src/judgmentllm/study` subtree excluded from both artifacts. No global text,
JSON or YAML exclusion is used. Public dependencies and entry points are unchanged.
Hatchling also requires the VCS exclusion file in its sdist build metadata;
`.gitignore` is therefore explicitly retained and checked for reproducible rebuilds.

Study packages, verification/qualification tooling, tests, examples, developer
documentation, `AGENTS.md`, `uv.lock`, CI and benchmarks remain committed in Git.
Use a checkout and editable development environment for those workflows. The
original study's `sources/*/configs/*.yaml` are archival text-mode rendering
inputs; its `configs/study-config-*.json` files are the native study configurations.

Run the distribution audit from a clean committed checkout:

```bash
uv run --offline --no-sync python scripts/audit_distribution.py --output /tmp/judgmentllm-distribution-audit
```

The audit stages only committed source, uses the lock-pinned Hatchling, builds
both artifact types and a second wheel from the sdist, compares every runtime
member, checks metadata/license/templates/typing/entry points, and runs strict
Twine checks. Existing installation smokes then exercise both artifacts in
independent non-editable environments outside the checkout, with authentication
removed and offline dependencies. They cover core rubric/CJ preview, lifecycle,
durable exports and R-compatible views. Source code cannot mask missing assets.
Each audit environment first synchronizes core dependencies from the committed
lockfile's cached artifact URLs. Installing the local artifact then avoids a new
registry resolution; `uv pip check` still verifies its declared dependencies.
This works with the cache populated by locked CI setup on all three platforms.
The actual R/jsonlite campaigns remain separate required local gates.

`report.json` records the exact source/archive identities, complete member lists,
file counts, uncompressed totals, compressed bytes and artifact SHA-256 digests.
The serial distribution regression runs the same audit. Its size measurements
are evidence, not an arbitrary release size ceiling. Review source-checkout
coverage and the required R/resource/hosted evidence independently of packaging.

The retained pre-correction artifacts at draft source
`a11a74957bcbb4430717a0c6bfe8d5f81f40985d` measured:

| Artifact | Files | Compressed bytes | SHA-256 |
| --- | ---: | ---: | --- |
| Wheel | 156 | 531,421 | `f0100927a46097f31884c6640c398896c334deda533d77981480bb27487767d3` |
| Sdist | 674 | 2,671,755 | `391505023db8cfc258a903518119e11b724f134d0e245ee4285f2078334f4cf4` |

These are measured artifact sizes, rather than raw Git inventory estimates.
Final-source post-correction measurements belong in the external audit and PR
verification report. Artifact inspection checks for keys, local build paths,
cache/editable-install files and repository-only material. Runtime member bytes
must match the reviewed source allowlist. No package publication is authorized.
