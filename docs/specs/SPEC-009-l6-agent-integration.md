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
4. Derive `graph_request_id` as
   `grq-sha256:<sha256(canonical request payload)>`, where the payload excludes
   only the derived ID and self-hash. Claim an opaque
   `l6r-sha256:<64-hex>` run identity and one exact execution fingerprint at
   the trusted receipt authority. The fingerprint binds the canonical query,
   both resolved scope hashes, ACL/access policy, L5a/L5b publication,
   crosswalk, Graph model, read-back receipts, Runtime 1.1 budget
   ID/version/schema/hash, and RequiredMember authority. Then execute one Graph
   request bounded by approved paths, relationships, K, record count, and
   RequiredMember authority. The claim is atomic across tool instances.
   A byte-identical, authority-identical completed retry returns the persisted
   result and receipt without another provider call. A different request,
   scope, policy, publication, model, budget, or RequiredMember authority fails
   before Graph. Provider `BaseException` and result-validation failures
   atomically consume the run and wake all waiters before re-raising.
5. The server issues one opaque `L6GraphExecutionReceipt` after validating
   the completed Graph result. The receipt binds run/request/result/scope,
   publication/ACL hashes, canonical IDs, assertion count, and typed accounting.
   A trusted atomic store validates all expected bindings and consumes the
   receipt once. Missing, forged, stale, replayed, or cross-scope receipts cause
   zero Search calls and cannot consume a valid receipt for another scope.
   Receipt payloads carry an authority-instance HMAC. Model construction checks
   syntax and self-hashes only; trusted Graph, readiness, and synthesis
   acceptance operations verify authentication against an injected immutable
   keyring snapshot. Non-abstain packages bind every duplicated scope, request,
   result, and Runtime retrieval field to that authenticated receipt.
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

Every public DTO construction path recursively validates stable strings and the
URL-free locator view. HTTP/file/data and other URL schemes, signed/query
credentials, encoded secret assignments, principal/provider/email metadata,
control/format characters, and Latin/Cyrillic/Greek homoglyph mixing in
security-key prefixes fail with input-free errors. Safe NFC Unicode display
text remains supported.

Internationalized email/principal detection scans RFC-style atom and quoted
local parts and canonicalizes Unicode/IDN domains, including IDNA dot
separators and punycode. Ordinary prose using `@` without an address-shaped
domain remains valid. Mixed scripts are rejected only when their normalized
security skeleton matches a prohibited credential/provider/principal prefix;
ordinary multilingual headings remain valid.

After L5b validation and stable citation assembly, the authority issues
`L6EvidenceExecutionReceipt`. Its authenticated payload binds the Graph receipt
and authority, exact evidence output hash, Runtime coverage ID/hash, citation
IDs/hashes, source-response/Search-index/publication/required-member hashes, and
stable collection hash. Readiness verifies this chain without consuming it.
The downstream `validate_trusted` acceptance consumes it atomically, so the same
package cannot authorize a second synthesis call.

Authority state is explicit and lock-protected:
`issued -> consumed_for_retrieval -> evidence_receipt_issued ->
evidence_consumed`. Local request/context/scope validation occurs before the
Graph receipt claim, so malformed requests do not burn it. The claim hashes the
exact retrieval request, scope, context, and budget. Evidence issuance requires
that claim and stores exactly one Graph-to-evidence capability. A byte-identical
retry returns the same unconsumed receipt; different, duplicate-after-consume,
unclaimed, or concurrent competing issuance cannot mint another capability.

Evidence issuance captures one immutable keyring snapshot under the authority
lock and uses it for both Graph receipt verification and active-key signing.
Final synthesis acceptance captures one current snapshot, revalidates both the
bound Graph and evidence receipt signatures/windows/states, and then consumes
atomically. Rotation or Graph-key revocation between issuance and consume
therefore fails closed even when the evidence-signing key remains active.

The citation collection seals a source binding for each presentation:
`(presentation_id, source_envelope_id, source_envelope_hash,
stable_presentation_hash)`. Evidence output, collection assembly, readiness,
and synthesis rederive the canonical stable DTO from the exact immutable
`SearchCitationEnvelope` set and validate that set against Runtime citation
mappings. A collection without its authoritative citation objects is
candidate-only and cannot make an L6 package synthesis-ready.

Top-k ranking, vector similarity, display-name matching, and document proximity
are never completeness or relationship proof.

## Persistence and deployment parity

`build_l6_agent_definition` creates deterministic instructions, five explicit
tool schemas, connection requirements, and call limits. Agent display text is
NFC-safe under a display-name grammar and excludes controls, bidi formatting,
secrets, URLs, scheme-less endpoints/paths, traversal, query/fragment syntax,
principals, emails, and provider metadata. Descriptions/instructions use a
separate safe human-text policy. Connection references accept stable repo-defined
opaque IDs, UUIDs, Fabric workspace/item UUID pairs, and structurally valid
Azure ARM resource IDs only. A recursive pre-persistence scan revalidates every
string and the exact closed tool/connection names.
`persist_l6_agent_definition` verifies the definition hash, writes canonical
JSON, and reads back exact bytes and semantic content.

The definition requires:

- an existing Fabric Data Agent project connection;
- an existing Foundry RemoteTool project connection for the L6 endpoint;
- managed identity/RBAC configured outside the definition;
- no embedded token, key, signed URL, principal metadata, or provider secret.

No resource is deployed by L6. The L7 foundation consumes only
`L6CanonicalAgentDefinition`; arbitrary legacy agent dictionaries are not a
deployment authority. `AzureBlobL6GraphReceiptAuthority` provides the production
multi-process run/receipt boundary with Entra-authenticated Blob access, finite
leases, ETag compare-and-swap, crash recovery, one-time consumption, and an
opaque injected signer provider. Its production Graph path requires a
deadline-aware cancellable transport with bounded connect/read operations;
synchronous callbacks are test-only and rejected by production configuration.
Deadline expiry atomically fails the run and ignores late results. Signing keys
never enter Blob state, receipts, logs, or repository configuration.

L7 owns GET-only planning, endpoint publication prerequisites, project
connection creation/update, Foundry version deployment, exact readback,
attempt-owned rollback, and post-deploy acceptance. Fabric Data Agent mutation
is not claimed by this foundation: the adapter verifies configured Fabric item
IDs/types/definitions and reports an unsupported capability before any mutation
when an exact readback is unavailable. RDF remains optional and inactive.
