# Assessment presentation revision 2 (issue #38)

New previews and preparations use concise Markdown. Rubrics show one `## Rubric`
heading, substantive shared guidance, readable trait names, exact trait IDs and
descending descriptors. Numbers come from the complete parent rubric's ordering,
including independent scoring. Standalone traits receive no invented ordinal.
Technical rubric IDs/names and coding arrows are omitted. Consecutive numeric
labels that equal their actual codes use `Score`; other scales use `Category`
with the exact label. Schema enums, internal ascending category order, numeric
codes, probabilities and expected scores are unchanged.

Descriptors remain verbatim, including whitespace and bullets. Alternative
conditions must be written explicitly in the rubric data (for example, `Any one
of the following:` in a descriptor). Multiline text never implies alternatives.
The default template supplies `## Student Essay`; its protected `response` block
contains only the complete original text. IDs written by an essay's author are
preserved. Templates remain sandboxed and cannot modify protected blocks.

CJ uses a protected `## Comparison Criterion` block with the actual name, ID and
description, without ordinal categories. The default template supplies `## Essays`.
`first_response`/`left_response` emit `essay_01:` plus the first presented text;
`second_response`/`right_response` emit `essay_02:` plus the second. Custom templates
must preserve that order. Adjacent redundant template headings for Rubric,
Comparison Criterion and Output Format are removed before inserting the complete
protected blocks; assessment text is never searched or rewritten.

The exact CJ wire enum is `essay_01`, `essay_02`, plus null for permitted ties.
Validation checks aliases before resolving once through that occurrence's
immutable `PairTask.presented_order`. New forced choice forbids `tie` and every
other extra field; explanations require their existing field. Ties-enabled
contracts still require `tie`, with null winner iff true. Unknown, malformed and
source-ID wire winners fail. If a source ID itself equals an alias, only the
committed revision decides its interpretation. Normalized results and R exports
retain original item IDs, occurrence IDs, attempts and orientation.

## Compatibility and qualification

`presentation_version` independently versions rendering and wire interpretation;
it is not an editable style or a new RunSpec setting. Revision 1 is historical;
revision 2 is selected for every newly prepared RunSpec 1/2/3 request. The existing
document, renderer-envelope, output-contract and provider-transport version
numbers retain their meanings. Their version plus this revision identifies the
complete protocol. Persisted omission means **1**, never the new default.

The manifest's template commitment seals the revision before submission. Output
contracts include it, native source snapshots retain it, and transport receipts
keep it under full and minimal capture. Exact output/prompt/body/receipt digests
and the sealed initial transport index bind those facts. Recovery and retries
use the original manifest/receipt revision without reselection. Legacy values are
omitted when serializing revision 1, preserving old bytes/digests. No store or
historical evidence is migrated, repaired or rehashed. Historical v1 requests
retain their explicit-tie parser; new v1 preparations use a committed generated
contract through the existing mock-v2 protocol. RunSpec parsing defaults remain
unchanged. Genuine main-produced fixtures cover pending v1/v2 and received v2
requests, including a source-ID/alias collision. Cross-platform replay uses an
additional Python-origin fixture with relative single-component input paths;
the original POSIX-path archive remains unchanged and independently hash-checked.
Scoped Git attributes preserve exact fixture and example bytes on checkout.

Native selection additionally requires the explicit
`assessment_presentation_v2` qualification feature. Historical reviewed profiles
and source-bound fixtures are unchanged and confer no new authority. Native
projection can represent the revised schemas offline; representation is not
qualification. Until separately qualified, new production `auto` chooses text,
explicit `native` fails, and strict unknown `auto` fails. Historical frozen
requests continue checking their original profiles/commitments. This PR makes no
provider calls and grants no qualification or publication authority.

## Offline illustrations

Run `python examples/rendering_revision2.py --output /tmp/rendering-examples`.
[Exact prompts and generated schemas](../examples/rendering-revision2/) include
one-trait and grouped scoring, explanations, forced-choice CJ and a reversal.
They use the supplied 15-minute Internet/travel writing task and synthetic essays;
their two-trait rubric is illustrative, not the future issue #39 study package.
Both forward `essay_01` and reversed `essay_02` normalize to original item `001`.
The existing strict export-to-R gate independently verifies those actual
Python-generated results, character IDs, missing probabilities and orientation.
