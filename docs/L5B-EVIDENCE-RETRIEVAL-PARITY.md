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
- C0.Runtime contracts are consumed unchanged to return citations and bounded
  structural coverage only; fabric-kg performs no answer synthesis;
- exact Graph-required canonical IDs, not display names or ranked top-k, define
  completeness; missing IDs, warnings, truncation, source failures, collisions,
  stale hashes, or ACL gaps yield partial/abstain behavior; and
- sealed response documents are quarantined before citation construction unless
  every applicable canonical scope, source/asset, publication, and ACL
  dimension is equal or narrower; quarantined quotes are never exposed;
- local reuse compares canonical compiled payloads and deterministic seals
  rather than deriving expected truth from mutable disk bytes, and source
  display filenames reject URLs, paths, connection strings, and credentials;
- schema 1, CLI/Data Agent activation, L6 synthesis, live Azure/Fabric
  deployment, and L7 validation remain unchanged or deferred.

## Official API assumptions

The adapter shapes follow Microsoft Learn for the Azure AI Search
`2026-04-01` stable data plane and explicitly gated `2026-05-01-preview`:

- [migration and version shape](https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-migrate);
- [retrieve references and activity](https://learn.microsoft.com/azure/search/agentic-retrieval-how-to-retrieve); and
- [search-index knowledge source and baseFilter/filterAddOn](https://learn.microsoft.com/azure/search/agentic-knowledge-source-how-to-search-index).

The preview knowledge base is persisted with `outputMode=extractiveData`, no
models, and minimal reasoning. Live capability validation and service-side
payload acceptance are intentionally deferred to L7.
