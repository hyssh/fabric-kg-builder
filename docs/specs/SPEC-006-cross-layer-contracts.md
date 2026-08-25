# SPEC-006: Cross-Layer Contracts (C0.Core, C0.Extraction, and C0.Publish)

**Status:** Approved foundation
**Version:** 1.3.0
**Date:** 2026-08-24
**Owner:** C0 Contract Owner
**Depends on:** Bootstrap PR #30, SPEC-001 through SPEC-005

## 1. Scope

C0.Core, the additive C0.Extraction carriers, and behavior-free C0.Publish
proof/reference contracts are the shared contract, specification, schema,
fixture, and hashing foundation used across L1-L5.
They contain no proposal UX,
LLM request, extraction activation, evidence pipeline integration, canonical
Parquet rewrite, publication/deployment operation, runtime query, citation,
answer, or live Fabric behavior.

Stable layer vocabulary:

| ID | Stable name |
|---|---|
| Bootstrap | PR #30 Domain schema foundation |
| C0 | Shared Contract Foundation |
| L1 | Domain Design/Approval |
| L2 | Schema-Constrained Extraction |
| L3 | Evidence Validation |
| L4 | Audit/Serving Projection |
| L5 | Publication |
| L6 | Runtime |
| L7 | Acceptance |

C0.Publish registers only crosswalk, equivalence, governed-asset reference, and
access-policy contracts. C0.Runtime query/citation/answer contracts remain an
explicitly deferred additive slice.

## 2. Existing authorities and ownership

`src/fabric_kg_builder/contracts/` is the single schema owner for shared
cross-layer primitives. It does not replace mature field authorities:

| Concern | Existing authority retained | C0 relationship |
|---|---|---|
| Domain schema and N/K | `domain/models.py` | Reference only |
| Domain hash | `domain/service.py::compute_contract_hash` | Equality adapter |
| Canonical tables | `model/schemas.py::TABLE_MODELS` | Typed reference adapters |
| Canonical IDs/hashes | `model/ids.py` | Byte-compatible ID/hash seeds |
| Lineage fields | `model/schemas.py::CommonLineageRow` | Equality adapter |
| Source locator | `lineage/common.py::build_source_locator` | Typed immutable adapter |
| Checkpoint fingerprint | `sources/checkpoint.py` | Equality adapter |
| Semantic projection | `serving/semantic_projection.py` | Header ID adapter |
| Semantic contracts | `semantic/models.py`, `semantic/schemas.py` | Referenced, not duplicated |
| Runtime traces | `runtime/semantic_reliability.py` | Unchanged; C0.Runtime deferred |

Contract instances are strict and frozen. Unknown fields, wrong types, wrong
registered versions, and unknown contract majors fail closed. Enrichment means
creating a new immutable version or event, never mutating an earlier contract.

## 3. Canonical serialization and IDs

Canonical serialization is UTF-8 JSON with:

- Unicode NFC for every stored string;
- sorted object keys and compact separators;
- finite numbers only;
- explicit nulls only where permitted by schema;
- sorted, deduplicated set-like arrays;
- preserved order for semantic arrays where order carries meaning; and
- lowercase 64-character SHA-256 values.

`canonical_json`, `canonical_sha256`, and generated golden fixtures are the
normative byte representation. Semantic and evidence IDs are deterministic and
use the existing `prefix:sha256(canonical_seed)[:32]` authority. Operational
run/attempt IDs may remain UUIDs. Timestamps are UTC and are excluded from
semantic hashes where the contract defines time as operational evidence.

Source file hashes remain hashes of original bytes. `SourceUnit.text` is stored
in NFC and `text_content_hash` hashes that exact UTF-8 text. All evidence offsets
are Unicode code-point offsets against that exact text.

## 4. Registered C0.Core contracts

All registered artifacts retain contract version `1.0.0`. Additive
`c0.evidence_span@1.1.0`, `c0.required_member_set_proposal@1.1.0`, and
`c0.required_member_manifest@1.1.0` readers coexist with exact `1.0.0`
readers. `c0.extraction_candidate_batch` remains `1.0.0`.

| `contract_kind` | Model | Contract-specific authority |
|---|---|---|
| `c0.identity` | `CanonicalIdentityEnvelope` | Cross-layer references; no generic `record_id` |
| nested primitive | `ImmutableSourceLocator` | Immutable typed locator, version `1.0` |
| `c0.source_unit` | `SourceUnit` | Exact source text and partition identity |
| `c0.evidence_span@1.0.0` | `EvidenceSpan` | Legacy local verifier-minted exact span |
| `c0.evidence_span@1.1.0` | `EvidenceSpanV1_1` | Purpose-bound local verifier-minted exact span |
| `c0.extraction_candidate_batch` | `ExtractionCandidateBatch` | L2 candidate and C0.Core accounting carrier |
| `c0.required_member_set_proposal@1.0.0` | `RequiredMemberSetProposal` | Legacy L2 ordered, role-bearing scope-membership carrier |
| `c0.required_member_set_proposal@1.1.0` | `RequiredMemberSetProposalV1_1` | Policy-faithful L2 scope-membership carrier |
| `c0.required_member_manifest@1.0.0` | `RequiredMemberManifest` | Legacy L3 deterministic scope-membership seal |
| `c0.required_member_manifest@1.1.0` | `RequiredMemberManifestV1_1` | Policy-faithful L3 deterministic scope-membership seal |
| `c0.candidate_lifecycle_record` | `CandidateLifecycleRecord` | Append-only state event |
| `c0.candidate_accounting_disposition` | `CandidateAccountingDisposition` | One input disposition |
| `c0.canonical_entity_assertion` | `CanonicalEntityAssertion` | Typed `EntityRow` reference |
| `c0.canonical_relationship_assertion` | `CanonicalRelationshipAssertion` | Typed `RelationshipRow` reference |
| `c0.canonical_property_assertion` | `CanonicalPropertyAssertion` | Typed `PropertyObservationRow` reference |
| `c0.audit_projection` | `AuditProjection` | Complete accounting header |
| `c0.semantic_serving_projection` | `SemanticServingProjection` | Exact asserted subset header |
| `c0.publication_crosswalk` | `PublicationCrosswalk` | Canonical-to-physical mapping proof |
| `c0.projection_equivalence` | `ProjectionEquivalence` | Expected/compiled/deployed/read-back equality proof |
| `c0.governed_asset_reference` | `GovernedAssetReference` | Generic immutable delivery-asset reference |
| `c0.access_policy` | `AccessPolicy` | Credential-free authorization and retention policy |
| `c0.artifact_manifest` | `ArtifactManifest` | Sorted artifact entries and totals |
| `c0.stage_receipt` | `StageReceipt` | Immutable stage outcome |
| `c0.stage_resource_metrics` | `StageResourceMetrics` | Future-measurement counters |

Generated schemas and their registry are under
`src/fabric_kg_builder/contracts/schemas/`. JSON/YAML examples, invalid
fixtures, and canonical JSON/hash goldens are under
`tests/fixtures/contracts/`.

## 5. Identity and locator invariants

`CanonicalIdentityEnvelope` carries project, run, source, asset/version, domain,
canonical schema, prompt, model, extractor, parent, and immutable locator
references. It deliberately has no generic `record_id`; each containing
contract's specific ID is authoritative.

When a contract repeats an identity field, equality is mandatory. In
particular:

- `SourceUnit.source_unit_id == identity.source_unit_id`;
- `SourceUnit.source_file_id == identity.source_file_id`;
- `EvidenceSpan` repeats source unit, source file, asset version, and locator by
  equality; and
- sealed domain/semantic hashes equal the projection identity hashes.

A partially populated source identity is invalid. Prompt, model, and extractor
name/version/hash pairs are all-or-none.

`ImmutableSourceLocator` uses exactly the vocabulary accepted by
`build_source_locator`. At least one immutable coordinate is required.
Character bounds are paired and ordered. Local paths, `file:` URIs, SAS query
parameters, bearer tokens, account keys, client secrets, and durable signed
URLs are forbidden.

## 6. SourceUnit and EvidenceSpan

`SourceUnit` records the exact text, UTF-8 byte count, Unicode code-point count,
content hash, source file, ordinal, optional parent unit, and immutable locator.
Its deterministic ID seed is asset version, source file, locator hash, and
ordinal.

Only `EvidenceSpan.mint_verified` may mint an evidence span. Before minting it
proves:

1. `0 <= span_start < span_end <= len(SourceUnit.text)`;
2. `quote == SourceUnit.text[span_start:span_end]`;
3. quote and source text hashes match;
4. source unit, source file, and asset version identities match; and
5. locator character offsets match the span.

Model-authored evidence IDs are never accepted as verified evidence.

EvidenceSpan `1.1.0` adds required `purpose` with the exact values
`domain_design|extraction_assertion` and required
`verifier_purpose_version`. Both fields participate in canonical JSON,
canonical hashes, and the deterministic evidence ID seed. All exact quote,
Unicode-codepoint, source-unit, source-file, asset-version, source-text hash,
and immutable-locator invariants remain unchanged.

The only `1.0.0 -> 1.1.0` adapter accepts an explicit trusted, intact
`l1.design_sample_manifest@1.0.0` context and the exact source unit. It emits
only `domain_design` when the legacy span is manifest-listed and uses the
standard verifier name
`fabric-kg.local-evidence-verifier/domain_design`. Missing or ambiguous proof
fails with `C0_EVIDENCE_PURPOSE_AMBIGUOUS`; adaptation to
`extraction_assertion` is prohibited. Existing `1.0.0` artifacts are read
without reinterpretation or hash changes, and no bulk migration is defined.

## 6A. C0.Extraction carriers

C0.Extraction registers one strict `1.0.0` candidate batch and both `1.0.0`
and `1.1.0` versions of its member proposal and manifest.
`CompletenessRequirementV2`, hierarchy and ancestor closure, abstract and
identity-root rules, key policy, relationship policy, N/K, and approval remain
exclusively owned by L1 `DomainContractV2`. C0 does not define or register a
`CompletenessRequirement` or `HierarchyIdentity` contract.

`ExtractionCandidateBatch` is emitted by L2. It binds the source-corpus and
source-unit manifest IDs/hashes to the sealed domain contract, completeness
requirement, hierarchy, and identity-policy hashes. Candidate references retain
the C0.Core candidate ID/version/kind, semantic type, lifecycle record, and
`EvidenceSpan` IDs. Its embedded `CandidateAccountingDisposition` values retain
C0.Core's mutually exclusive retained/deduplicated accounting. Candidate counts,
the retained ID-set hash, and the batch hash reconcile deterministically.

Legacy `RequiredMemberSetProposal@1.0.0` remains registered for exact reads and
explicit fail-closed migration. It carries the batch ID/hash,
sealed authority references, aggregate/scope canonical ID, membership semantic
relationship ID, and ordered members. Each member carries only a canonical ID,
semantic type ID, domain-authored role ID, order, cardinality, originating
candidate ID, and supporting C0 `EvidenceSpan` IDs. Cardinality is carried
losslessly as a non-negative minimum and an optional maximum; C0 does not
select those bounds. Production schemas contain
no domain names, predicates, or fixed member counts.

That legacy shape is readable without reinterpretation, and its canonical
JSON and hashes remain unchanged. It is not suitable for roleless or unordered
Domain policies because it requires a role and ordinal per member and repeats
collection cardinality on every member.

`RequiredMemberSetProposal@1.1.0` corrects the carrier additively:

- every member retains only canonical member ID, semantic type ID, originating
  candidate ID, supporting evidence-span IDs, optional approved role ID,
  optional carrier order, and a deterministic `member_hash`;
- `ordering_policy` repeats the sealed Domain ordering mode and exact ordinal
  property, integer value type, direction, uniqueness, and contiguity metadata;
- unordered collections declare no ordinal metadata, every `member_order` is
  null, and members canonicalize by stable `member_canonical_id`;
- ordered collections use the strict domain-neutral
  `zero_based_contiguous` carrier encoding. The sealed Domain policy must state
  unique and contiguous ordinals. Every member has one unique position and the
  stored positions are exactly `0..n-1`; missing, duplicate, or gapped positions
  fail closed;
- `required_role_ids` is the sorted set copied from Domain authority. An empty
  set requires null member roles. A non-empty set requires every member role to
  be an approved ID and every required role to be represented. Sentinel roles
  such as `role:unspecified` are prohibited;
- optional `expected_cardinality`, `minimum_cardinality`, and
  `maximum_cardinality` carry exact collection-level Domain values. Null means
  Domain declared no value; C0 never defaults or infers one. Minimum cannot
  exceed maximum, and expected must lie inside declared bounds; and
- `member_set_hash`, optional `ordered_member_tuple_hash`, and
  `authoritative_collection_hash` are deterministic. The collection hash
  includes source/domain/completeness/hierarchy/identity authority, scope,
  membership relationship, ordering policy, cardinality, approved role IDs,
  and canonical members.

Ordering and roles are independent policy dimensions: an ordered collection
may be roleless, and an unordered collection may carry approved roles. Presence
of each member field must agree with its corresponding collection policy.

`RequiredMemberManifest@1.1.0` is sealed locally and deterministically by L3. It
must repeat the proposal ID/hash, batch, authority, scope, relationship,
ordering policy, cardinality, required roles, members, and all collection hashes
exactly. The local validator also requires the sealed member count to satisfy
every declared exact/minimum/maximum bound. It cannot add, remove, reorder, or
reinterpret proposal content. Diagnostics and unresolved reasons remain
external lifecycle/validation records; the carrier does not invent an
unresolved status. It is the sole cross-layer completeness/scope-membership
artifact for a negotiated `1.1.0` path.

Legacy `RequiredMemberManifest@1.0.0` is sealed locally and deterministically by L3. It must
repeat the proposal's batch, authority, scope, relationship, and ordered member
content exactly. Its `authoritative_collection_hash` hashes that content with
all sealed authority references; its semantic manifest hash excludes only the
operational seal timestamp. It is the sole cross-layer
completeness/scope-membership artifact for a negotiated `1.0.0` path.

These contracts prove reference and hash equality only. They cannot broaden,
narrow, infer, or reinterpret L1 policy, and they do not activate extraction or
validation feature behavior.

The explicit `1.0.0 -> 1.1.0` adapter is fail-closed and is never invoked by
parsing or version negotiation. It requires trusted policy context tied to the
same domain, completeness requirement, hierarchy, and identity-policy hashes.
Only an ordered, role-bearing legacy proposal with approved non-sentinel roles,
contiguous zero-based order, identical repeated bounds, and no discarded
expected-count information can adapt. Roleless, unordered, sentinel, defaulted,
gapped, inconsistent, or otherwise ambiguous legacy content raises
`C0_REQUIRED_MEMBER_1_0_AMBIGUOUS`. A legacy manifest adapts only after proving
that it exactly sealed the safely adapted legacy proposal.

L2 adoption of `1.1.0` requires it to:

1. negotiate and emit `c0.required_member_set_proposal@1.1.0` explicitly;
2. copy the sealed `CompletenessRequirementV2` ID/hash, hierarchy hash,
   identity-policy hash, ordering metadata, role IDs, and exact optional
   cardinality without defaults;
3. emit null role/order for unsupported policy dimensions, never
   `role:unspecified` or fabricated ordinals;
4. accept observed ordered positions only when Domain authority declares unique,
   contiguous integer ordinals and the positions are already exact zero-based
   contiguous values, and canonicalize unordered members by stable ID;
5. compute every member, tuple, proposal, and collection hash through the C0
   model factories; and
6. keep incomplete/unresolved diagnostics in L2 audit/view or lifecycle records
   rather than encoding them in the proposal.

No L2, L3, feature, enrichment, semantic, deployment, publication, or runtime
behavior is activated by this C0 contract registration.

## 7. Candidate lifecycle and accounting

The canonical state enum is:

`proposed`, `discovery`, `unresolved`, `rejected`, `unsupported`, `asserted`.

Existing `unverified` input maps explicitly to `unresolved`; it never becomes
asserted. The allowed append-only transitions are:

- `null -> proposed`;
- `proposed -> discovery|unresolved|rejected|unsupported|asserted`;
- `discovery -> unresolved|rejected|unsupported|asserted`; and
- `unresolved -> rejected|unsupported|asserted`.

`rejected`, `unsupported`, and `asserted` are terminal for a candidate version.
Reprocessing creates a new candidate version rather than mutating a terminal
event. Sequence zero is only the initial proposed event; every later event
names its prior lifecycle record. The transition hash excludes occurrence time.

Every input candidate has exactly one disposition:

```text
input_candidate_count
  = retained_candidate_count
  + deduplicated_input_count
```

Each retained candidate has exactly one current state:

```text
retained_candidate_count
  = proposed + discovery + unresolved + rejected + unsupported + asserted
```

A deduplicated input has no lifecycle state and maps to exactly one retained
candidate. Reason-code counts may overlap and are non-additive diagnostics.

## 8. Canonical assertion references

Entity, relationship, and property assertion models are typed adapters over the
existing canonical rows. They preserve contract-specific IDs, canonical keys,
endpoints, property/value identity, and canonical content hashes. They add
shared lifecycle and exact evidence references; they do not define replacement
tables or alter Arrow schemas.

`assertion_state`, legacy `assertion_status`, and `processing_status` consumers
must use the shared state adapter rather than declaring disconnected literal
enums.

## 9. Audit and serving projections

`AuditProjection` validates:

- one disposition for every unique input candidate;
- retained and deduplicated accounting;
- dedup targets resolve to retained candidates;
- one current state per retained candidate;
- lifecycle state counts sum to retained count;
- assertion ID sets and canonical ID/row hashes; and
- a canonical projection hash.

`SemanticServingProjection` permits only `included_states == ["asserted"]`.
Validation requires each serving entity, relationship, and property ID set to
equal exactly the asserted subset declared from the audit projection. A raw
canonical table name or count-only comparison is not a serving fallback.

C0.Core defines and tests these headers but does not rewrite canonical Parquet
or activate L4 projection behavior.

## 10. C0.Publish contracts

C0.Publish registers exactly four strict, frozen `1.0.0` contracts. They are
proof and reference schemas only; they do not compile, deploy, read back, sign,
authorize, retrieve, or log a remote resource.

`PublicationCrosswalk` maps upstream-owned canonical semantic type, property,
relationship, hierarchy, and instance-key IDs to physical table/column IDs,
Ontology BigInt IDs, Graph labels/aliases/properties, Search
index/filter/vector fields, and Data Agent selected-property IDs. It seals the
stable-ID lock, hierarchy, identity-policy, semantic-contract, and source
projection hashes. Canonical IDs and physical namespace IDs are unique, and
relationship endpoint key mappings must equal the canonical instance keys of
their referenced types. The contract rejects physical ID reuse or collision;
it never creates hierarchy, identity, or projection authority.

The crosswalk references, without copying or recomputing membership:

- `RequiredMemberManifest@1.1.0` ID, exact contract version/schema hash,
  manifest hash, and `authoritative_collection_hash`; and
- source `ArtifactManifest` ID/hash.

`RequiredMemberManifest@1.1.0` remains the sole completeness and collection
membership artifact. C0.Publish defines no structured-scope manifest,
completeness requirement, member list, member inference, or competing
blob/table/visual-specific publication schema.

`ProjectionEquivalence` supports exactly `parquet`, `semantic_model`,
`ontology`, `graph`, and `search`. It records expected, compiled, deployed, and
read-back counts, canonical ID-set hashes, and the applicable row, definition,
or index fingerprint, together with missing/extra canonical IDs and exact
source projection, crosswalk, member-manifest, authoritative-collection, and
source-artifact hashes. `equivalent=true` is valid only when all four
observations are exactly equal and both difference sets are empty. The schema
is proof evidence and performs no remote operation.

`GovernedAssetReference` is one generic, domain-neutral delivery-asset
reference with approved kinds `original`, `visual`, `table`, `derived`, and
`other`. It binds immutable source-file, asset, asset-version, locator,
content-hash, versioned credential-free storage coordinates, an `AccessPolicy`
ID/hash, and either no URL access or authorized on-demand short-lived URL
issuance. Text citations remain valid without a governed asset reference.

`AccessPolicy` carries principal ACL scopes; allowed `metadata`, `content`, and
`short_lived_url` operations; sensitivity; retention; legal-hold state; and an
authorization-resource ID. Its policy hash is deterministic. SAS parameters,
bearer tokens, secrets, credentials, and durable signed URLs are forbidden in
every C0.Publish contract and therefore never enter canonical hashes or
contract-derived logs.

## 11. Manifests, receipts, and resource evidence

`ArtifactManifest.entries` are sorted by artifact ID and unique. Total row and
byte counts reconcile with entries, then seal into `manifest_hash`.

`StageReceipt` uses the stable IDs C0 and L1-L7. Succeeded/skipped receipts
require paired output manifest ID/hash and no errors. Failed/blocked receipts
require error codes. Remote operation references are opaque and secret-free.

A skip is valid only when:

1. the prior receipt succeeded;
2. skip key and input manifest hash are unchanged;
3. output manifest ID/hash equal the prior result; and
4. the referenced output manifest and artifacts remain intact.

`StageResourceMetrics` records non-negative wall/CPU/RSS/storage/network,
source-unit, service-call, token, row/document, retry, cache, and concurrency
counters for future measurement. A succeeded receipt cannot name an exceeded
declared hard dimension.

No numeric cache TTL, p50/p95, RSS, call, token, network, retry, or regression
threshold is selected or enforced in 0.2.4 C0.Core. Numeric policies remain
deferred pending baseline capture and approval. Confirmed integrity ceilings
such as evidence completeness, no lifecycle loss, no N+1 design, and count/hash
identity may be named as hard dimensions.

## 12. Registry and compatibility

The registry maps each kind to one model and exact supported versions. Readers
reject unknown kinds, unknown majors, and unregistered minor/patch versions.
Schema 1.x domain behavior remains unchanged. Domain schema 2.0 is
new-project-only. There is no implicit migration, dual write, automatic
feature activation, or reuse of schema-1 approval artifacts.

C0.Runtime contracts may register later under the same owner. A required field,
changed meaning/type, changed ID/hash seed, tightened accepted value set,
lifecycle transition change, or serving enum addition requires a new major.

## 13. Contract gate

The C0.Core gate includes:

- strict unknown field/type/version rejection;
- canonical JSON and golden SHA verification;
- JSON/YAML round trips and generated schema registry hashes;
- exact C0.Extraction registry/version and sealed authority equality;
- C0.Core lifecycle, accounting, evidence, manifest, and receipt compatibility;
- deterministic candidate ID-set and authoritative member-collection hashes;
- duplicate/missing member rejection and semantic order/cardinality retention;
- multiple-domain fixtures with domain-neutral production schemas;
- all allowed and forbidden lifecycle transitions;
- exhaustive Unicode code-point span/quote/hash ranges;
- mutually exclusive candidate accounting;
- asserted-only serving set equality;
- publication key/physical-ID collision and reuse rejection;
- exact crosswalk hierarchy, identity-policy, stable-ID-lock, and projection
  hash authority;
- exact `RequiredMemberManifest@1.1.0` ID/version/schema-hash/manifest-hash and
  authoritative-collection references without membership recomputation;
- projection count, canonical ID-set, row/definition/index fingerprint, and
  missing/extra equivalence proof;
- generic multi-kind asset references, policy authorization, and text citations
  without asset references;
- deterministic access-policy/asset/crosswalk/equivalence hashes and
  credential, SAS, bearer-token, secret, and durable-URL rejection;
- manifest totals and receipt skip preconditions;
- secret/token/path rejection;
- domain hash, `CommonLineageRow`, source locator, canonical row ID/hash,
  checkpoint, and semantic projection-header adapter equality; and
- the existing unit plus contract test gate.

No test in this gate performs a remote request or live Fabric mutation.

## 13. Deferred decisions and exclusions

Deferred:

- numeric cache TTLs;
- numeric latency, RSS, service-call, token, byte, and retry thresholds;
- publication crosswalk/equivalence/governed asset contracts; and
- runtime query, citation, claim, and answer contracts.

Excluded from C0.Core:

- L1 proposal/intake/approval UX;
- L2 extraction behavior or activation;
- L3 validation pipeline integration;
- canonical Parquet or Arrow rewrites;
- L4 projection execution;
- L5 compile/deploy/read-back behavior;
- L6 Graph/Search/synthesis execution;
- L7 live acceptance;
- remote requests, deployment, and live Fabric changes.
