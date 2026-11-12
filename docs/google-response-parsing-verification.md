# Shared Google response interpretation

This extraction preserves the interpretation implemented by accepted main
`6e2a8d204f4ae33e3f74d25e7421674ef3d28c96`. It shares only Google candidate
selection, part classification, completion rules and their constants. Gemini
Developer and Vertex retain their existing entry points, assessment component
types, diagnostics, usage mappings, evidence sources, API scopes and error wording.
There is no new provider capability or public configuration.

The shared completion function receives exactly two typed validators: each
provider's unchanged `_mapping()` and `_blocked_ratings()`. They preserve safe
messages and short-circuit evaluation at the original validation sites. Part
classification accepts an already validated mapping; its provider wrapper still
validates arbitrary input. Candidate selection uses casts only after its existing
dictionary checks. No parser class, registry or provider dispatch is introduced.

## Deterministic equivalence evidence

[The matrix](../tests/test_google_response_equivalence.py) was run against the
original source before production edits. Its
[golden fixture](../tests/fixtures/google_response/baseline.json) records the base
commit, original provider-module SHA-256 values, each input digest and separate
Gemini/Vertex snapshot digests. Expected values were not captured from the
extracted implementation.

There are 154 response cases per provider, five malformed raw transport cases per
provider and two exact provider-error tests: 320 tests. All 320 passed against the
original source archive. The response snapshots compare candidate/part selection,
completion tuples and exceptions, exact assessment bytes, component metadata and
canonical diagnostics, usage, envelope eligibility, raw and safe transport bytes,
reasoning metadata/text absence and adapter parsing. Inputs must remain unchanged.
The existing SDK/lifecycle/privacy suites additionally exercise durable full and
minimal capture, invalid UTF-8 HTTP bodies, recovery and provider-specific errors.

| Boundary | Matrix cases |
|---|---|
| Missing/multiple candidates | Absent, null, non-list, empty, non-mapping candidate and two candidates |
| Identity/content structure | Omitted/invalid indices, booleans/floats/strings, absent/malformed content, wrong role, unknown fields and malformed parts |
| Completion | All 19 recognized finish reasons, absent/null/unknown/malformed reasons |
| Safety | Every recognized prompt block reason; prompt and candidate ratings with missing/false/true/null/invalid flags and malformed entries |
| Precedence | Prompt refusal before missing/multiple candidates; safety before malformed ratings; ratings before limits/tools/continuation; limit before continuation |
| Incomplete/limits | Missing/unspecified/continued finish, non-null continuation tokens including false/zero/empty, MAX_TOKENS |
| Tools | Function call/response, executable code/result, before/after text and with thought metadata |
| Reasoning | Thought-only, thought before/after assessment, signatures, false/null/invalid thought fields |
| Assessment text | Multiple components, empty/blank, Unicode, invalid JSON, duplicate keys, wrong root and oversized text |
| UTF-8/transport | High/low unpaired surrogates, invalid UTF-8 bytes, malformed/duplicate JSON and non-object transport roots |
| Provider separation | Provider-specific component/usage/reasoning sources, diagnostics and exact safe errors |

The detailed before/after response-snapshot streams are byte-identical:
308 records, 12,308,988 bytes, each SHA-256
`76b960052eeb03db7e14444c907ac84a40419e9b666cd030647fe7c96769a7f9`.
The streams and comparison receipt are retained under
`/tmp/judgmentllm-google-response/`; the committed golden fixture makes every
comparison part of ordinary offline tests without committing large raw streams.
These deterministic synthetic fixtures establish equivalence for the matrix;
they do not establish new serving behavior or live qualification.

Reproduce the baseline independently using the original archived Python modules
and this PR's tests in the locked environment:

```bash
mkdir -p /tmp/google-response-original
git archive 6e2a8d204f4ae33e3f74d25e7421674ef3d28c96 | tar -xf - -C /tmp/google-response-original
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/tmp/google-response-original/src:tests \
  .venv/bin/python -m pytest --no-cov -q tests/test_google_response_equivalence.py
.venv/bin/python -m pytest --no-cov -q tests/test_google_response_equivalence.py
```

## Code counts and verification scope

Physical production lines, including imports, constants, docstrings and blanks:

| Module | Before | After |
|---|---:|---:|
| `gemini_response.py` | 342 | 228 |
| `vertex_response.py` | 342 | 228 |
| `google_response.py` | 0 | 147 |
| Total | 684 | 603 |

The net reduction is 81 production lines. All retained provider functions other
than `_part_kind()` and `_generate_content_output()` are AST-identical to the
base; those two become wrappers and `_parts` is re-exported from the shared
module. In particular `_mapping()`, `_blocked_ratings()`, `_usage()`, diagnostic
extraction and envelope retention are unchanged. Existing adapter callers and
historical fixtures are unchanged. The added matrix/golden data are verification
code and are not included in the production counts.

This is Level 3 because refusal and reasoning/privacy safeguards are shared.
The PR records the automatic `ci/check.py --profile pr --plan-only` selection,
exact-source local primary coverage, all seven strict R/jsonlite gates, applicable
security/compatibility/resource/package checks and protected-base hosted results.
Passed, failed and deferred checks are reported separately in the PR; a focused
development pass or hosted pass alone is not complete acceptance. No CI policy,
coverage baseline, dependency, provider authority, schema or historical evidence
is changed. See the [authoritative risk policy](ci-optimization.md).
