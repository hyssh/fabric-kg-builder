# Windowed working-schema operations

Status: implementation specification, 2026-09-09.

## Goal and scope

Replace independent naming across discovery chunks with a common, evolving
working vocabulary and mapping registry. Carry its identity through the last
chunk, persist each batch transition locally, and reuse existing observations.

This change implements CLI state, alignment and handoff. Detailed source
retrieval, multi-span answer adjudication and Search index construction remain
Foundry Agent orchestration guidance, not new engines in this release.

An evolving working schema is not a published ontology. An accepted working
change is not an asserted source fact or final mapping/domain approval.

## Ordered operation

1. Load the exact discovery artifact and an explicit seed schema/reference.
2. Create a stable working schema with canonical concept IDs, definitions,
   aliases, owner/endpoint constraints and unresolved choices.
3. Plan deterministic windows over the declared chunk inventory.
4. Freeze schema version N for every worker in the current window.
5. Map existing observations and collect unknown expressions, conflicts and
   proposed additions. Preserve raw source and grounding context.
6. Evaluate combined proposals at a single window boundary; reject structurally
   invalid changes and retain ambiguous/breaking changes as pending.
7. Atomically record the decision, version N+1 and the completed window.
8. Apply N+1 to the next window. Reconcile affected earlier observations from
   cached data rather than repeating raw extraction.
9. Review the final mapping against its target domain before approved replay.

No worker updates the shared schema independently. Parallelism operates within
a frozen version; the version transition occurs only after batch collection.

## Persisted state

The exact public schemas are exported by the CLI. Their responsibilities are:

| Record | Required meaning |
|---|---|
| Run identity | Discovery/source/seed hashes, model and prompt versions, window policy and budgets |
| Schema snapshot | Version, parent snapshot hash, stable definitions/aliases/mappings, pending choices |
| Window input | Every chunk ID, input schema version/hash, bounded actual model context |
| Model proposal | Original request/response identity and candidate changes; no authority promotion |
| Evaluation | Accepted working changes, rejected/ambiguous choices and reasons |
| Window commit | Before/after schema hashes, completed chunk IDs and result/accounting hashes |
| Final mapping review | Exact final snapshot, target domain and discovery hashes, reviewer and rationale |

Keep all raw discovery candidates, original labels and evidence anchors immutable.
Schema IDs must not change merely because an alias or display name changes.
Do not automatically merge entities or silently redefine identity, direction,
property type or applicability.

Each stored transition must be sufficient to explain which source observations
caused a change. A narrative summary alone is not the authoritative state.
Final replay must use the final reviewed mappings, not stale per-window aliases.

## Bounded model context

The complete working schema remains local. Each request receives the relevant
definitions/mappings plus a consistent catalog and necessary pending context.
Bind the actual context to the request hash.

Do not silently truncate definitions or claim a whole window was handled if only
some observations were presented. Oversized requests require a visible bounded
plan/defer state or further sub-batching that preserves the same frozen version.
Completion counters must distinguish processing coverage, mapping coverage and
grounding quality.

Existing Foundry configuration supplies inference. Copilot can coordinate and
review through the public CLI; do not assume an undocumented Copilot inference
API or require two model calls for every observation. Additional review should
focus on uncertain/high-impact changes.

## Mapping and evidence boundaries

Formatting-equivalent names may be proposed under an explicit unique-match
policy. Synonyms need a recorded mapping decision; relationship mappings must
consider direction and source/target types. Property mappings are owner-specific.
Do not map a logical component to an orderable SKU solely because names resemble
one another.

Unknown source concepts may extend the working schema; they must not be forced
into the approved target domain. Until a new domain is reviewed and approved,
new concepts remain pending for approved extraction.

Final reviewed mappings can change classification/reference vocabulary but not
invent property values, widen source quotes, repair identities from labels,
promote quarantined observations, or assert an unsupported relation. L3 evidence
and identity verification still applies.

The window receives document/page/section and original owner/value/row context
where already available. It must not attribute neighboring source text to a
different primary chunk. Failure to structure an optional detail does not by
itself invalidate the conceptual ontology: preserve the detail for retrieval
and retain its unresolved status rather than fabricate a structured fact.

## Resume and inspection

Plan mode makes no model calls or state writes. Live runs use bounded calls and
concurrency; resource exhaustion leaves a partial run with an exact cursor.
Completed windows replay without model calls. Interrupted work does not advance
the committed schema/cursor, and received responses remain available for matching
resume without unsafe duplicate calls.

Validate source, seed, model, prompt and window-policy bindings before any new
dispatch. Reject tampered snapshots, reordered/missing log links, stale mapping
reviews and mismatched target domains. Create-only atomic files prevent partial
cache entries from becoming authority.

Provide read-only status/history inspection: current schema, completed/planned
windows and chunks, last committed cursor, pending changes and schema diffs.
Do not infer active execution from a task timer or a stale `in_progress` label.

## Ontology, Search and SQL responsibility split

- Ontology/Graph holds core concepts, meaningful relations, procedure structure,
  applicability and retrieval keys. Not every paragraph becomes a graph property.
- Search provides detailed original text and context. Document/version/section
  references and canonical or governed lookup keys guide retrieval.
- Foundry Agent orchestrates scoped Graph retrieval, Search expansion and
  evidence-aware answers. A retrieved hit is not automatically a verified fact.
- Lakehouse SQL handles routed numerical/count/trend questions using their
  preserved population/grain/filter/time/source requirements.
- Source-index coverage, parent/row retrieval, permissions and revision filters
  are prerequisites documented in the helper. This CLI change does not build
  a new index or guarantee those runtime capabilities.
- No Search answer is silently written back into a published ontology.

## Acceptance

1. All workers in one window use one frozen snapshot; the next window uses the
   committed successor, including at the last declared chunk.
2. A newly accepted alias is available to later windows and final remapping of
   earlier cached observations, without overwriting their originals.
3. Unknown/ambiguous/new concepts remain explicit; no forced synonym or identity
   merge and no fabricated facts.
4. Local snapshots and before/after logs reproduce each version transition.
5. Interrupted/budget-limited runs resume without repeating committed work.
6. Final mappings require explicit review and exact discovery/target bindings
   before approved candidate replay; absent mappings preserve legacy behavior.
7. Real L2/L3/L4 tests demonstrate valid mapped relationships/properties while
   invalid evidence remains rejected.
8. Public CLI inspection demonstrates actual coverage and change history.
9. Existing SQL routing, pending requirements and accepted coverage limitations
   survive unchanged.

Use the existing test runner; live acceptance invokes public CLI commands over
saved discovery artifacts, not project functions in temporary scripts or
hand-edited replacement candidates.
