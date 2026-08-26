# L5b Evidence Retrieval Publication Parity

L5b supersedes only the Azure AI Search evidence-publication and retrieval
experiments from older frozen Search work. It does not cherry-pick or reactivate
those branches. The successor is rebuilt on merged C0.Runtime, C0.Publish, sealed
L4, and hardened L5a authority.

## Preserved behavior

- deterministic Search index artifacts and canonical document identities;
- Azure identity/credential injection at adapter boundaries, with no persisted
  secrets or signed URLs;
- bounded batch publication and direct filtered hybrid/vector retrieval; and
- exact source quotation and canonical Graph-to-Search linkage.

## Successor changes

- only an intact successful L5a read-back plus its exact
  `SealedL4ServingSource` and anchored L3 `ArtifactManifest` authorize
  publication;
- every indexed nontrivial assertion resolves through
  `EvidenceSpanV1_1 -> SourceUnit -> source file/asset version -> immutable
  locator/hash -> exact quote or governed asset`;
- the supplied governed-asset set exactly equals sealed L5a authority, and each
  applicable evidence source resolves one exact asset or publication fails;
- exact filterable canonical entity, relationship, property, type, assertion,
  member-manifest, source, evidence, lifecycle, ACL, and authority keys are
  persisted without recomputing membership;
- index, knowledge-source, knowledge-base, document, vector-state, policy, and
  authority hashes are materialized and read back exactly;
- publication uses strict uniform accounting, deterministic call bounds,
  hash-keyed reuse, compare-and-swap, ownership tokens, and conditional
  cleanup/restore;
- preview retrieval is explicitly pinned and gated to
  `2026-05-01-preview`; persisted `baseFilter` and request `filterAddOn` combine
  only by `AND`;
- preview reasoning uses the official `{ "kind": ... }` shape, and provider
  timeout conversion floors without broadening or rejects subsecond budgets
  before any call;
- stable direct fallback uses the same canonical scope with
  `vectorFilterMode=preFilter`; unavailable vectors can degrade only to the
  same filtered keyword/semantic path and must be reported;
- schema-2 L5b exclusively emits the registered C0.Runtime successors
  `c0.query_budget@1.1.0`
  (`2d744838296209d78da2e2c8b7df7ab5f030af400d45a3d04d62b7d763f92b52`),
  `c0.agentic_retrieval_request_context@1.1.0`
  (`dfed8fe3449b824cffa1570c278d3e476712987cb8d2e8cb2c903ac480bd8868`),
  and `c0.agentic_retrieval_coverage_receipt@1.1.0`
  (`92d39c05d33a360bd542386af022a382ba18788efe4a1fe5b0728c42b5aec652`);
  mixed 1.0/1.1 request and budget pairs are rejected before provider calls,
  while non-schema-2 compatibility remains unchanged;
- the sealed QueryBudget 1.1 carries all 17 ceilings: Graph scope requests and
  admitted canonical records; agentic invocations, subqueries, and source
  calls; direct requests; output documents, tokens, bytes, and runtime; Search
  candidates and verified returned records; vector requests; embedding calls
  and items; and retry count and wait;
- coverage records exact observations without clamping: the upstream resolved
  scope establishes one Graph resolver request and its exact admitted canonical
  ID count, Search matched candidates remain distinct from verified returned
  documents, direct and agentic source-call accounting remains mode-specific,
  and fallback preserves separate origin and direct budgets;
- agentic Search candidate observations are the checked sum of unique validated
  source-call activity counts and must exactly equal adapter accounting; direct
  `@odata.count`, when returned, must likewise equal adapter accounting.
  Missing, negative, contradictory, or signed-int32-overflowing candidate
  accounting fails closed before a receipt or citation can be returned;
- every counted agentic Search activity and every reference `activitySource`
  requires the signed-int32 identity defined by the pinned preview schema;
  booleans, strings, floats, nulls, and under/overflow are rejected before
  aggregation or binding. Numeric IDs are canonicalized for duplicate
  detection and bound opaquely into source-call and subquery receipt IDs;
- exhausted dimensions are derived exactly when observation exceeds ceiling;
  provider overexecution produces one typed `retrieval_budget_exhausted`
  failure with `partial` or `abstain` coverage instead of validation failure or
  false completeness;
- zero optional vector, embedding, source-call, subquery, and retry paths do
  not widen or clamp a request; unrepresentable provider integers and disabled
  requested paths fail before the provider call;
- C0.Runtime citations and bounded structural coverage remain answer-free;
  fabric-kg performs no answer synthesis;
- exact Graph-required canonical IDs, not display names or ranked top-k, define
  completeness; missing IDs, warnings, truncation, source failures, collisions,
  stale hashes, or ACL gaps yield partial/abstain behavior; and
- sealed response documents are quarantined before citation construction unless
  every applicable canonical scope, source/asset, publication, and ACL
  dimension is equal or narrower; quarantined quotes are never exposed;
- local reuse compares canonical compiled payloads and deterministic seals
  rather than deriving expected truth from mutable disk bytes, and source
  display filenames reject URLs, paths, connection strings, and credentials;
- succeeded and skipped checkpoints use an opaque signer/verifier injected from
  outside the mutable state tree; fabric-kg receives only algorithm/key
  ID/version plus sign/verify operations, never raw key material, and missing
  signers disable reuse;
- persisted display names additionally reject Unicode controls/formats/bidi,
  noncharacters, encoded URL/secret forms, and whitespace-tolerant credential
  assignments (including embedded credential stems) while preserving safe NFC
  Unicode and spaces; citation section-path labels use the same validator;
- credential assignment keys are checked through a normalized alphanumeric
  skeleton across raw and bounded-decoded forms, so prefixed/suffixed stems
  cannot bypass policy while non-credential words remain valid;
- credential skeletons use NFKC plus case folding for detection only, blocking
  fullwidth/mathematical confusables while persisted display values remain NFC;
- every Search result must reproduce the exact compiled field set, canonical
  payload, recomputed document hash, and immutable document ID/hash authority
  before any quote, locator, or display field is inspected; duplicate
  references/documents and strict locator-schema drift are quarantined;
- pre-verification reference/document/provider IDs and warnings are never
  echoed; opaque local or hashed identifiers are used, and only document IDs
  already resolved to immutable compiled authority may appear in failures;
- schema 1, CLI/Data Agent activation, L6 synthesis, live Azure/Fabric
  deployment, and L7 validation remain unchanged or deferred.

## Official API assumptions

The adapter shapes follow Microsoft Learn for the Azure AI Search
`2026-04-01` stable data plane and explicitly gated `2026-05-01-preview`:

- [migration and version shape](https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-migrate);
- [retrieve references and activity](https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-retrieve); and
- [search-index knowledge source and baseFilter/filterAddOn](https://learn.microsoft.com/azure/search/agentic-knowledge-source-how-to-search-index);
- [`KnowledgeBaseActivityRecord.Id` signed-int32 identity](https://learn.microsoft.com/dotnet/api/azure.search.documents.knowledgebases.models.knowledgebaseactivityrecord.id); and
- [`KnowledgeBaseReference.ActivitySource` signed-int32 binding](https://learn.microsoft.com/dotnet/api/azure.search.documents.knowledgebases.models.knowledgebasereference.activitysource).

The preview knowledge base is persisted with `outputMode=extractiveData`, no
models, and minimal reasoning. Live capability validation and service-side
payload acceptance are intentionally deferred to L7.
