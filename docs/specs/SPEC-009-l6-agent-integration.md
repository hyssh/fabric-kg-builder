# SPEC-009: L6 Evidence-First Agent Integration

**Status:** Implemented locally; live deployment deferred to L7  
**Date:** 2026-08-26  
**Package version:** 0.2.3

## Contract boundary

L6 consumes C0.Runtime 1.1 `QueryBudgetV1_1`,
`AgenticRetrievalRequestContextV1_1`, and
`AgenticRetrievalCoverageReceiptV1_1` unchanged. It also consumes
`ResolvedOntologyScope`, `ResolvedRetrievalScope`,
`SearchCitationEnvelope`, `CitationPresentation`, `AccessPolicy`,
`GovernedAssetReference`, and exact C0.Publish hashes.

No shared contract, schema registry, generated schema, or PRD text is modified.
RDF is an optional future output and is absent from L6 readiness. L6 does not
activate schema-2 CLI behavior or change schema-1 behavior.

## State machine

1. Resolve the supplied `OntologyScopeEnvelope` locally to exact Ontology and
   retrieval scopes.
2. Verify intact L5a and L5b persisted publication/read-back authority.
3. Verify exact access policy, principal scope hash, governed assets, project
   scope, Graph/Search ACL equality, and all serving/publication fingerprints.
4. Execute one canonical Graph request bounded by approved paths,
   relationships, K, record count, and RequiredMember authority.
5. Reject empty, failed, overexecuted, stale, or out-of-scope Graph responses
   without Search fallback.
6. Execute one L5b route selected by the Runtime 1.1 request context. An
   authorized direct fallback must bind its exact originating context and
   budget; it does not follow a prior runtime retrieval call.
7. Validate the Runtime receipt plus one-to-one
   `SearchCitationEnvelope`/`CitationPresentation` hash links.
8. Emit complete, partial, or abstain as structured zero-synthesis JSON.

## Tool parity

| Tool | Purpose | Remote calls owned by L6 |
|---|---|---:|
| `fabric_kg_resolve_ontology_scope` | Resolve cached/local canonical authority | 0 |
| `fabric_kg_execute_bounded_graph_scope` | Execute one approved Graph path request | 1 maximum |
| `fabric_kg_retrieve_scoped_evidence` | Delegate one selected sealed L5b retrieval route | 0; L5b owns accounting |
| `fabric_kg_assemble_citation_presentation` | Validate exact citation/presentation links | 0 |
| `fabric_kg_report_coverage_readiness` | Report exact complete/partial/abstain state | 0 |

Fabric-kg makes zero synthesis calls. The emitted package declares a maximum of
one downstream synthesis call.

## Readiness

`complete` requires exact Graph required canonical IDs/assertions and complete
L5b coverage with no warnings, truncation, source errors, ACL gaps, stale
hashes, exhausted budgets, duplicate/missing citations, or unexpected IDs.
`partial` requires at least one verified Graph assertion and one verified
citation presentation, with typed failures and exact safe missing authority
IDs. All other outcomes abstain and expose no citations from a failed route.

Top-k ranking, vector similarity, display-name matching, and document proximity
are never completeness or relationship proof.

## Persistence and deployment parity

`build_l6_agent_definition` creates deterministic instructions, five explicit
tool schemas, connection requirements, and call limits.
`persist_l6_agent_definition` verifies the definition hash, writes canonical
JSON, and reads back exact bytes and semantic content.

The definition requires:

- an existing Fabric Data Agent project connection;
- an existing Foundry RemoteTool project connection for the L6 endpoint;
- managed identity/RBAC configured outside the definition;
- no embedded token, key, signed URL, principal metadata, or provider secret.

No resource is deployed by L6. L7 owns endpoint publication, project connection
creation, live Data Agent/Foundry definition deployment, smoke tests, and
post-deploy acceptance.
