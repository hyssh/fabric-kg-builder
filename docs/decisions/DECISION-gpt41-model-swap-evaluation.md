# Decision record: gpt-4.1 model swap for the query-answering Foundry Agent

**Status: GATED — NOT MERGEABLE.** This document records an evaluation result and a
decision to *not* ship the change it describes, at least not standalone. Do not
merge/deploy the swap on the strength of this record alone.

## Background

Production query agent `fabric-kg-024-grounded-agent` (v12) runs on `gpt-4o`.
Two `no_tools_at_all` / routing-failure issues (#137, #138) motivated evaluating
whether a different chat-completion model reduces the routing/tool-selection
failure rate independent of prompt-instruction changes (v1.8/v1.9/v1.10 had
already been tried and did not close the gap).

## A/B test result

Two experiment agents were deployed from the same instructions/tools/connections,
differing only in `model.deploymentName`:

- `fabric-kg-024-model-ab-gpt41` (`gpt-4.1`)
- `fabric-kg-024-model-ab-gpt4o` (`gpt-4o`, matching production)

12-query battery, 10 serial calls each per model per query, 240 total calls,
zero exceptions:

| Model | "ok" (routing + format compliant) | Failure rate |
|---|---|---|
| gpt-4.1 | 116/120 (96.7%) | 3.3% |
| gpt-4o | 29/120 (24.2%) | 75.8% |

`no_tools_at_all` did not reproduce at all under gpt-4.1 in 120 calls. Separating
gpt-4o's failures into true routing failures vs. output-format noncompliance:
true routing-failure rate is closer to **~57%**, not the headline 76% — format
noncompliance (not routing) accounts for the remaining ~19 points. This more
honest split still leaves gpt-4.1 the clearly more reliable tool-caller.

Full table and methodology: [#138](https://github.com/hyssh/fabric-kg-builder/issues/138#issuecomment-5497543395).

## Why this is gated: the grounding audit

"ok" in the A/B measured **routing and output-format compliance**, not whether the
answer's content is true. A follow-up grounding audit sampled gpt-4.1's "ok"
answers on q1/q6/q8/q12 (2 runs each) and inspected the raw answer text and
citations directly, not just the pass/fail verdict:

- **q6 (list components, Surface Pro 10 Business) — confirmed fabrication, both
  runs.** The model produced a fluent 8-item component list (Battery, Camera,
  Display, Keyboard, Motherboard, SSD, Speaker, Touchpad), citing
  `source_type=ontology source_id=entity:surface-pro-10-business`. **Surface Pro
  10 has zero real `device_has_component` edges in the graph.** The citation is
  well-formed and looks legitimate; the content behind it does not exist.
- **q12 (C-Cover) — split.** One run cited real, verifiable search chunk IDs. The
  other reproduced the unfilled citation-template-placeholder defect tracked in
  [#139](https://github.com/hyssh/fabric-kg-builder/issues/139) — literal
  `source_id=entity:<C-Cover-entity-id>` angle brackets, and an invented
  `chunk_id=warning-c-cover-removal` slug — confirming **#139 is model-independent**,
  not gpt-4o-specific.
- q1, q8: no fabrication observed; q8 correctly reported "not found."

**Conclusion: 96.7% "ok" is not 96.7% grounded.** gpt-4.1 is a materially more
reliable *tool-caller*, but on at least one sampled query it is a more willing
*fabricator*, producing a confident, correctly-routed, correctly-formatted answer
whose central content and ontology citation are both invented. A model that fails
loudly (gpt-4o, ~57–76% of the time) is safer than one that fails silently and
convincingly (gpt-4.1). Shipping the swap alone would trade a visible failure
mode for a more dangerous, harder-to-detect one — this is the same failure class
tracked in [#137](https://github.com/hyssh/fabric-kg-builder/issues/137).

## Causal chain (why the graph, not the agent, is the real trigger)

q6 fabricates *because* the ontology genuinely returns empty for Surface Pro 10 —
and it returns empty because of the entity-fragmentation defect described in
[#135](https://github.com/hyssh/fabric-kg-builder/issues/135) (Surface Pro 10
fragments into ~12 relationship-starved near-duplicate nodes instead of one
canonical node). No amount of model selection or prompt wording changes that the
underlying graph fact does not exist; it only changes *how the model behaves*
when it doesn't find one. This makes the extraction/fragmentation fixes in #135
higher-leverage than any further agent-side change, and it means the swap's true
value cannot be fairly judged until the graph itself is repaired.

## Decision

1. **Do not merge/deploy this swap standalone.** Both experiment agents
   (`fabric-kg-024-model-ab-gpt41`, `fabric-kg-024-model-ab-gpt4o`) remain as-is,
   untouched, for reference. Production (`fabric-kg-024-grounded-agent`, v12)
   stays on `gpt-4o`.
2. Gate any future swap on both of:
   - An **ontology-null-safety instruction guard**: if a tool call returns no
     rows/edges, the agent must say so explicitly rather than filling the gap
     with plausible-sounding content.
   - The **#139 citation-contract fix**: citations must be built from actual
     tool-call results, never from an unfilled template, and validated before
     being included in a response.
3. Re-evaluate the swap's real benefit only after the #135 extraction/fragmentation
   fixes land, since a large share of observed fabrication is downstream of a
   graph gap the model cannot be expected to compensate for.

## Note on repo scope

There is no single, clean code location to "make" this swap today: the query
agent's `model.deploymentName` is intentionally not committed (it lives in a
local, uncommitted `agent-metadata.yaml` passed to `deploy-agent --metadata`
alongside connection IDs and other environment-specific values). This record
exists to preserve the evaluation and the decision, independent of where/whether
a future config change eventually lands.
