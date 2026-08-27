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
5. The server issues a unique opaque `L6GraphExecutionReceipt` after validating
   the completed Graph result. The receipt binds request/result/scope,
   publication/ACL hashes, canonical IDs, assertion count, and typed accounting.
   A trusted atomic store validates all expected bindings and consumes the
   receipt once. Missing, forged, stale, replayed, or cross-scope receipts cause
   zero Search calls and cannot consume a valid receipt for another scope.
6. Reject empty, failed, overexecuted, stale, or out-of-scope Graph responses
   without Search fallback.
7. Execute one L5b route selected by the Runtime 1.1 request context. An
   authorized direct fallback must bind its exact originating context and
   budget; it does not follow a prior runtime retrieval call.
8. Validate the Runtime receipt plus one-to-one
   `SearchCitationEnvelope`/`CitationPresentation` hash links.
9. Emit complete, partial, or abstain as structured zero-synthesis JSON.

## Tool parity

| Tool | Purpose | Remote calls owned by L6 |
|---|---|---:|
| `fabric_kg_resolve_ontology_scope` | Resolve cached/local canonical authority | 0 |
| `fabric_kg_execute_bounded_graph_scope` | Execute one approved Graph path request | 1 maximum |
| `fabric_kg_retrieve_scoped_evidence` | Consume one trusted Graph receipt, then delegate one sealed L5b route | 0; L5b owns accounting |
| `fabric_kg_assemble_citation_presentation` | Return an exact immutable presentation collection for sorted unique envelope IDs | 0 |
| `fabric_kg_report_coverage_readiness` | Compute same-scope complete/partial/abstain from trusted Graph and Runtime receipts | 0 |

Fabric-kg makes zero synthesis calls. The emitted package declares a maximum of
one downstream synthesis call.

## Readiness

`complete` requires exact Graph required canonical IDs/assertions and complete
L5b coverage with no warnings, truncation, source errors, ACL gaps, stale
hashes, exhausted budgets, duplicate/missing citations, or unexpected IDs.
It also requires a non-empty `L6EvidenceToolOutput` and the exact
`L6CitationPresentationCollection` linked to that Graph receipt and Runtime
receipt. Readiness verifies one-to-one equality across citation IDs, stable
presentation source IDs, envelope hashes, source-response hashes, Search index,
publication, and required member authority. A complete Runtime receipt with no
verified citations is not synthesis-ready and fails closed; legitimate
zero-result cases abstain.
`partial` requires at least one verified Graph assertion and one verified
citation presentation, with typed failures and exact safe missing authority
IDs. All other outcomes abstain and expose no citations from a failed route.

All L6 result nesting is frozen and typed. Operation references are opaque
SHA-256 objects; Graph warnings/errors use a closed code vocabulary. Raw
provider URLs, queries, paths, principals, emails, secrets, control characters,
and Unicode confusables never enter agent-visible accounting.

Sealed L6 collections never embed C0 `CitationPresentation` objects because
their private transient authorized URL is intentionally mutable. L6 converts a
verified C0 presentation into `L6StableCitationPresentation`, an immutable DTO
containing only persisted policy-approved fields and exact citation envelope
ID/hash references. Conversion rejects any populated transient URL. Ephemeral
authorized URLs, if later required, belong only in an L7 UI adapter outside all
sealed L6 hashes and outputs.

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
