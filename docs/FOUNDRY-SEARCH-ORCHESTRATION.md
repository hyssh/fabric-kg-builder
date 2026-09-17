# Ontology keys and detailed source retrieval

This is integration guidance for the Foundry Agent/Copilot helper. It does not
claim that the CLI provisions a source index, performs multi-span adjudication,
or has verified a deployed agent's answers.

## Roles

| Surface | Responsibility |
|---|---|
| Ontology / Graph | Shared concepts, core business relationships, applicability, procedure structure and governed lookup keys |
| AI Search | Original passages, detailed descriptions, tables, warnings and citations, including detail not projected as graph properties |
| Lakehouse SQL | Declared counts, aggregates and trend analysis with explicit population/grain/time requirements |
| Foundry Agent | Select sources, expand detail, reconcile relevant evidence and explain uncertainty without inventing facts |
| CLI | Preserve working schemas, mappings, snapshots, provenance, approvals and the execution context needed by those consumers |

Not every paragraph or attribute must become an ontology property. Missing
optional detail can remain a retrieval need. However, a relationship or property
actually asserted in the ontology still needs the corresponding source proof.

## Suggested query loop

1. Read the question and retained execution context. Distinguish structural
   lookup, source-detail retrieval and SQL analysis; preserve mixed requirements.
2. Resolve the relevant core entities/relations in Ontology/Graph. Obtain
   canonical IDs plus useful exact source-facing keys, names and approved aliases.
3. Build a scoped Search request using those keys, document/source references,
   device/configuration, procedure and revision constraints where available.
4. Retrieve the detail and, when needed, its parent paragraph, table row/headers
   or referenced section. Keep original source coordinates and source version.
5. Confirm the returned evidence applies to the requested entity/task. If it
   conflicts, misses the required link or mixes revisions, retrieve narrower
   context or explain the uncertainty rather than fabricate an answer.
6. Cite the actual original text. Do not present a schema association, embedding
   similarity or a plausible model inference as the source statement.

If the graph cannot resolve a key, the orchestrator may use authorized document
scope and user-provided exact identifiers to search. It must label unresolved
identity rather than claim a verified graph traversal. No retrieved result is
automatically written back to an approved ontology.

## Owner and value details

A passage containing only a part number does not establish which part it
belongs to. Search can recover a table row, header, paragraph or cross-reference
that supplies that context. The orchestrator should preserve those source spans
separately when they are not contiguous; never join them into a fabricated quote.

If the source structure cannot establish the connection, keep the answer
unresolved. This runtime check need not force every such detail into a graph
property before an agent can be useful.

## Index prerequisites, not implemented by the window CLI

- Source content required for questions must actually be indexed. An index
  containing only already-asserted graph evidence cannot recover omitted detail
  unless a separate source-content path is available.
- Preserve document ID/version, page/section, parent chunk and table/row/header
  references where supported. Preserve both source-facing business keys and
  validated canonical links; their identities are not interchangeable.
- Distinguish original source text, candidate annotations and asserted-fact
  evidence. Being searchable does not give a candidate semantic authority.
- Apply source permissions and appropriate version/applicability restrictions.
  A vector similarity score does not replace these constraints.
- Exact identifiers such as SKUs benefit from exact/lexical retrieval together
  with semantic/vector retrieval; do not assume vectors alone preserve them.
- Retrieve parent/neighbor context only under its actual source coordinates.
  Do not attribute a neighboring page's statement to the primary chunk.
- Verify links and citations are usable by the asking user, not just by the
  identity that built the index.

Physical index count and implementation are deployment decisions. The distinction
between source content and asserted evidence must remain clear whether they live
in one index with explicit metadata or multiple indexes.

## Window schema evolution

The working schema is durable CLI state, not a conversation summary. Within a
batch, all workers consume the same snapshot. At its boundary, the CLI evaluates
proposed additions/mappings and commits a successor with a before/after log.
Copilot can coordinate and review; Foundry can propose changes. Neither model
silently changes published ontology meaning.

Use saved observations for alignment before re-extracting text. New aliases
affect later windows and final remapping, but source facts, candidate IDs and
original labels remain in the audit trail. Unknown and out-of-target concepts
stay visible instead of being forced into convenient categories.

## Acceptance questions for the deployed orchestrator

- Does a core graph result lead to the correct source document and revision?
- Can it expand a short hit into the necessary paragraph/table context?
- Are explicit facts distinguished from inference, absent information and conflict?
- Does it preserve task order/conditions instead of sorting search hits as steps?
- Can it decline an unproven owner/value association without hiding the useful
  source passage?
- Are SQL counts computed from complete, appropriately scoped records rather than
  the number of retrieved search hits?
- Are missing index content and permission restrictions reported explicitly?

References:

- [Azure AI Search document chunking](https://learn.microsoft.com/azure/search/vector-search-how-to-chunk-documents)
- [Parent-child index projections](https://learn.microsoft.com/azure/search/search-how-to-define-index-projections)
