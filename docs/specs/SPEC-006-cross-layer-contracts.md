# SPEC-006: Cross-Layer Contracts (C0.Core, C0.Extraction, C0.Publish, C0.RDF, and C0.Runtime)

**Status:** Approved foundation
**Version:** 1.7.0
**Date:** 2026-08-26
**Owner:** C0 Contract Owner
**Depends on:** Bootstrap PR #30, SPEC-001 through SPEC-005

## 1. Scope

C0.Core, the additive C0.Extraction carriers, and behavior-free C0.Publish,
C0.RDF, and C0.Runtime proof/reference contracts are the shared contract, specification,
schema, fixture, and hashing foundation used across L1-L6.
They contain no proposal UX,
LLM request, extraction activation, evidence pipeline integration, canonical
Parquet rewrite, publication/deployment operation, runtime transport, retrieval,
synthesis, answer, or live Fabric behavior.

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
access-policy contracts. C0.RDF registers only derived RDF manifest,
serialization-artifact, and validation-receipt contracts. C0.Runtime registers only canonical scope, bounded
request configuration, structural coverage, and citation-data contracts.
Downstream Agent intent, orchestration, retry, synthesis, final-answer, claim,
and answer-evidence UX are not fabric-kg contract behavior.

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
| Runtime traces | `runtime/semantic_reliability.py` | Unchanged; C0.Runtime contracts are additive only |

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
| `c0.publication_crosswalk@1.0.0` | `PublicationCrosswalk` | Legacy canonical-to-physical mapping proof |
| `c0.publication_crosswalk@1.1.0` | `PublicationCrosswalkV1_1` | Ownership/reference/materialization-separated mapping proof |
| `c0.projection_equivalence` | `ProjectionEquivalence` | Expected/compiled/deployed/read-back equality proof |
| `c0.governed_asset_reference` | `GovernedAssetReference` | Generic immutable delivery-asset reference |
| `c0.access_policy` | `AccessPolicy` | Credential-free authorization and retention policy |
| `c0.rdf_projection_manifest` | `RdfProjectionManifest` | Exact authority, namespace, graph, vocabulary, and alignment declaration |
| `c0.rdf_projection_acceptance_bundle` | `RdfProjectionAcceptanceBundle` | Self-contained manifest/artifact/receipt acceptance proof |
| `c0.rdf_serialization_artifact` | `RdfSerializationArtifact` | One format artifact bound to the canonical RDF dataset |
| `c0.rdf_validation_receipt` | `RdfValidationReceipt` | SHACL and exact cross-serialization round-trip proof |
| `c0.query_budget` | `QueryBudget` | Agent-selected request ceilings with separate hierarchy depth and relationship K |
| `c0.ontology_scope_envelope` | `OntologyScopeEnvelope` | Agent-requested canonical scope proposal |
| `c0.resolved_ontology_scope` | `ResolvedOntologyScope` | Structured authoritative Ontology/Graph scope response |
| `c0.resolved_retrieval_scope` | `ResolvedRetrievalScope` | Locally validated canonical retrieval scope |
| `c0.agentic_retrieval_request_context` | `AgenticRetrievalRequestContext` | Capability-gated safe Search request configuration |
| `c0.agentic_retrieval_coverage_receipt` | `AgenticRetrievalCoverageReceipt` | Bounded maximal structural coverage receipt |
| `c0.search_citation_envelope` | `SearchCitationEnvelope` | Exact authorized Search grounding and canonical lineage |
| `c0.citation_presentation` | `CitationPresentation` | User-displayable exact citation with transient URL exclusion |
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

C0.Publish registers four strict, frozen contract kinds. Publication crosswalk
readers `1.0.0` and `1.1.0` coexist; the other three contracts remain `1.0.0`.
All are proof and reference schemas only; they do not compile, deploy, read
back, sign, authorize, retrieve, or log a remote resource.

Legacy `PublicationCrosswalk@1.0.0` retains its exact schema, canonical bytes,
hashes, acceptance, and rejection behavior. It maps upstream-owned canonical
semantic type, property, relationship, hierarchy, and instance-key IDs to
physical namespaces, and requires every type key to resolve to that type's
local property mappings. It is not reinterpreted or migrated implicitly.

`PublicationCrosswalkV1_1@1.1.0` additively separates:

- one global `SemanticPropertyOwnershipMappingV1_1` for every canonical
  property, including its sole owner type, data type, value-semantics ID, and
  semantic Ontology/Graph/Data Agent mapping authority;
- each type's locally owned canonical property IDs and explicit
  `InheritedPropertyReferenceV1_1` values. An inherited reference repeats the
  owner, data type, and value-semantics ID and must equal the global authority;
- exact type-local `PhysicalPropertyBindingV1_1` materializations for every
  effective local or inherited canonical property. A binding may repeat
  physical presence in another type but cannot change canonical ID, owner,
  data type, or value semantics;
- explicit `PhysicalSurrogateKeyBindingV1_1` values, which are non-semantic,
  cannot use canonical property IDs, and cannot enter canonical property or
  instance-key sets; and
- relationship endpoint canonical key sets separately from relationship-local
  physical endpoint bindings. Canonical endpoint sets must equal the selected
  type mappings' exact canonical instance keys, and each local endpoint
  binding must resolve exactly one of those keys.

Every canonical property has exactly one ownership mapping and exactly one
local owner claim. Unknown owners, orphan properties, duplicate ownership,
cross-type ownership shadows, self-inheritance, contradictory inherited
metadata, missing/extra/duplicate physical bindings, physical-column
collisions, surrogate/canonical collisions, unknown parents, hierarchy cycles,
endpoint key mismatches, and coordinated hash reseals fail closed. Physical
columns are unique within their containing type or relationship; the same
canonical property may be physically materialized in multiple type tables
without creating additional semantic ownership.

The successor seals the same stable-ID lock, hierarchy, identity-policy,
semantic-contract, source-projection, membership-manifest, and source-artifact
authorities. It performs no hierarchy inference and does not decide whether an
owner is a valid ancestor. L5a must compare the explicitly selected owner and
parent references against the sealed `DomainContract` authority before
publication. The contract remains provider- and domain-neutral.

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

## 10A. C0.RDF contracts

C0.RDF registers four strict, frozen `1.0.0` contracts. They are semantic
interchange metadata only. They do not serialize, parse, canonicalize, validate,
fetch, import, publish, deploy, or read back RDF. The normative L5c behavior
boundary and adoption sequence are specified in
[SPEC-008](SPEC-008-l5c-rdf-semantic-interchange.md).

`RdfProjectionManifest` declares:

- `authority="derived"` and the exact sealed L4 serving projection, L5a
  projection manifest, `PublicationCrosswalk@1.1.0`, Ontology/Graph/Search
  `ProjectionEquivalence` read-back proofs, Domain contract, hierarchy,
  identity, relationship, K, `RequiredMemberManifest@1.1.0`, authoritative
  collection, and original L3 `ArtifactManifest` IDs, versions, schema hashes,
  and content hashes. The manifest seals a deterministic
  `authority_reference_set_hash` over the exact sorted reference tuples;
- governed absolute HTTPS ontology and instance base IRIs with exact namespace
  governance ID/hash, ontology IRI, version IRI, semantic version, UTF-8
  percent-encoded canonical-ID mapping,
  deterministic skolem policy, and the invariant that labels never define
  identity;
- exact common-schema, domain-schema, SHACL-shapes, optional-instance, and
  provenance/authority named graphs, with schema triples separated from
  instance/evidence triples and access-policy IDs/hashes on protected graphs.
  Every graph carries an exact canonical-ID-set hash/count: common is the empty
  canonical set, domain and SHACL derive from exact vocabulary inventories, and
  instances/provenance copy sealed upstream commitments;
- exact class, property, relationship, hierarchy, key, endpoint, range, and
  value-type inventory under a conservative OWL 2 RL-compatible derived
  vocabulary. Every term IRI is the injective canonical uppercase
  percent-encoding of its canonical ID under the governed ontology base;
- explicit endpoint-set encoding. Multiple domains or ranges cannot be emitted
  as repeated `rdfs:domain` or `rdfs:range` statements because that denotes an
  intersection. Each multi-endpoint side has its own deterministic union node
  IRI derived from term ID, side, and sorted endpoint-set hash; single sides
  use the direct class/value IRI and have no union node; and
- opt-in external alignment metadata with target IRI, approved relation,
  immutable source artifact/version/hash, license, and approval proof.
  Alignment is metadata-only: no default `owl:imports`, remote fetch, embedded
  URL, or copied third-party ontology content is authorized.

Full source quotes and transient or signed URLs are forbidden. RDF may carry
only evidence/source/governed-asset IDs and hashes plus PROV links. Search
remains the detailed authorized quote surface. `RequiredMemberManifest`
remains completeness authority; neither RDF nor SHACL recomputes membership.

`RdfSerializationArtifact` records one exact Turtle, RDF/XML, optional JSON-LD,
or canonical N-Quads artifact. It fixes media type and W3C syntax version,
content hash, byte/triple/graph counts, named graph IDs, sealed graph
ID/IRI/role/required/policy/count/canonical-ID commitments and inventory hash,
RDFC-1.0 canonical dataset hash, and deterministic no-unstable-blank-node
policy. Public artifacts contain exactly common/domain/SHACL roles and no
policy; protected artifacts contain mandatory provenance and optional
instances with exact policy. `validate_against_manifest` rejects any missing,
extra, relabeled, optionalized, or policy-shifted graph. Turtle is the human-review form, RDF/XML is the compatibility form,
JSON-LD is optional, and canonical N-Quads is the equivalence form. Sorted
N-Triples is not a dataset-canonicalization substitute. Public schema
artifacts cannot carry ACL principal policy; protected dataset artifacts must
reference an `AccessPolicy` by exact ID/hash.
The artifact's `canonical_id_binding_hash` is not a claimed raw union hash; it
hashes exact sorted graph ID, role, per-graph canonical-ID-set hash, and count
tuples. Artifact bindings must equal manifest graph commitments. Observations,
receipts, and acceptance bundles copy and validate that same
manifest-derived binding, so coordinated downstream reseals cannot substitute
`ffff` commitments or swap/subset public and protected graph semantics.

`RdfValidationReceipt` records SHACL shapes/report hashes, conformance and
severity counts, validator identity/version, and observations sealed to the
actual artifact contract hash, format/media type, content/dataset hash, graph
inventory hash/IDs, and triple count. Its format set exactly equals the
manifest, including selected JSON-LD and no extras. The cross-object acceptance
hook binds the exact manifest, authority, and artifact set. Exact equivalence requires the same
RDFC-1.0 graph hash, named graph set, triple count/set, and authority-reference
set. Missing/extra triples, serialization or base-IRI drift, label-derived
identity, unstable blank nodes, or SHACL violations force
`exact_round_trip_equivalent=false`.

The manifest declares every graph's expected content hash and triple count.
Every serialization's `shacl_shapes` graph binding must equal that manifest
hash/IRI/count, and `RdfShaclValidationSummary.shapes_hash` must equal it.
Coordinated artifact/receipt reseals cannot replace the manifest shapes
authority. Artifact, observation, receipt, and acceptance-bundle
`authority_reference_set_hash` values likewise must equal the manifest value;
downstream observations cannot establish authority by agreeing with each other.

Every top-level RDF contract recursively rejects secrets, bearer/API-key
material, password/passwd/pwd/auth/client-secret credentials, URI credentials,
AWS SigV4, Google signed URL, signed/SAS query keys, and percent-encoded variants
in nested identifiers, references, metadata, and alignments while allowing
stable governed and W3C vocabulary IRIs.
NFKC and case normalization run after every bounded decode round and again on
the stable value. URL queries/fragments use normalized credential-key parsing;
their normalized raw text is also scanned for header assignments across
ampersand/semicolon segments and duplicate credential keys fail.
free text requires a bearer/header pattern, `=` assignment, or colon followed
by assignment whitespace. Stable namespace IDs such as
`authorization:policy` and `credential:approval` remain valid.
Raw, initially normalized, every decoded-and-normalized, and final stable values
are each limited to 64 KiB. URL parsing and lazy hostname/port/user-info access
are wrapped in constant input-free errors. Raw and decoded/NFKC URL authority
scheme/canonical-host/port/user-info semantics must remain identical, so encoded
authority delimiters fail. Hosts use the declared
`c0.rdf.nfc-idna2003-strict-a-label-v1` profile: NFC, NFKC-stability,
letter/mark/number/hyphen/dot code points, standard-library IDNA A-label
round-trip, lowercase comparison, label/hostname length limits, and normalized
IP literals. Canonically equivalent decomposed/composed hosts compare equally;
NFKC-only compatibility, joiner, symbol, invalid A-label, and bidi hosts fail.
Compatibility characters in valid IRI path/query/fragment text
are not rejected merely because NFKC introduces a delimiter there.
Every RDF-owned top-level and nested model uses the RDF-local strict base with
hidden inputs and sanitized `ValidationError.errors()` details; rejected values
are never included in exception text or structured error input.
All collection before-validators type-check model or mapping entries before
sorting. Scalars, nulls, lists, and wrong objects defer to strict element
validation or raise constant sanitized errors; no attribute/type exception
escapes.

`RdfProjectionAcceptanceBundle` embeds one exact manifest, the complete
serialization artifact metadata set, and one validation receipt. Its model
validator invokes every graph, manifest, authority, format, artifact-hash,
dataset, round-trip, and SHACL cross-invariant and seals the accepted bundle.
L5c MUST emit and consume this registered bundle for successful publication.
Validating an individual manifest, artifact, or receipt proves syntax and local
integrity only and MUST NOT be treated as successful publication.

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

## 12. C0.Runtime contracts

C0.Runtime is exclusively owned by the C0 Contract Owner. Its original
contracts remain strict, frozen `1.0.0` schemas. The additive
`QueryBudget`, `AgenticRetrievalRequestContext`, and
`AgenticRetrievalCoverageReceipt` successors are registered at `1.1.0`; every
other C0.Runtime carrier remains `1.0.0`. These contracts define data-plane
boundaries only; they perform no Ontology/Graph request, Search request,
agentic decomposition, retry, synthesis, deployment, or live acceptance.

`OntologyScopeEnvelope` uses only approved canonical semantic type, entity,
relationship, role, path, and policy identities. Its modes are `exact_type`,
`descendants`, `ancestors_context`, and `explicit_member_set`. A relative scope
change is `exact`, `narrow`, or `expand`, with parent ID/hash identity required
for narrow or expand. Narrowing cannot add members, types, roles, paths,
includes, hierarchy depth, or relationship K, remove exclusions, or replace
sealed authority; expansion applies the inverse constraints. Display names,
natural-language resolver output, raw GQL,
raw OData, untrusted filters, ontology definitions, and schema deltas have no
authority. Hierarchy expansion policy/depth and relationship traversal K are
distinct fields and cannot substitute for each other. Normal relationship
K is at most 3; K=4 requires a reviewed justification.

`ResolvedOntologyScope` is the structured authoritative resolver response. It
carries canonical aggregate, collection, member, semantic type, relationship,
assertion, evidence, group, ordering, adjacency, and type-assertion identities;
sealed hierarchy/closure identity and deterministic expansion trace; exact
projection, crosswalk, Graph, publication, ACL, and receipt hashes; and a
deterministic canonical key-set hash. Canonical entity IDs remain stable across
subtype reclassification while type-assertion versions and affected scope hashes
change. Its acceptance hook binds every authority-bearing envelope dimension,
including includes/exclusions, requested types/roles/paths, project scope, and
agent policy, requires expanded types to equal the exact traced hierarchy
closure, constrains every relationship to approved authority, validates
adjacency endpoints/assertions/evidence, and rejects duplicate current type
assertions. Natural-language-only, label-only, omitted-key, stale-hash,
ambiguous, collision, unauthorized, or orphan results fail closed.

`ResolvedRetrievalScope` records local validation of the resolver response and
the safe canonical-ID Graph filter. It references
`RequiredMemberManifest@1.1.0` by exact ID, version, schema hash, manifest hash,
and authoritative collection hash. That L3 manifest remains the sole
completeness and membership authority; C0.Runtime does not define or infer a
competing member manifest. Acceptance validates the referenced manifest object
itself and requires exact scope, relationship, ordering, cardinality, unique
member count, roles, canonical members, semantic types, member order, supporting
evidence, manifest hash, and authoritative collection hash.

`QueryBudget` records Agent-selected request ceilings for one bounded
Ontology/Graph scope request and one mutually exclusive Search retrieval mode.
Agentic modes permit one Agent-owned retrieval invocation and zero direct Search
requests; the stable fallback permits one direct Search request and zero
agentic invocations. Internal subquery/source-call, document, token, byte, time,
Graph/Search request, and Graph/Search result fields are request ceilings and
observed-budget dimensions, not approved performance thresholds. Coverage binds
every declared ceiling to its exact budget and reconciles observed invocation,
subquery, source-call, direct-request, document, and Search-result counts. The
contract contains no synthesis or hidden retry field.

`QueryBudget@1.1.0` retains every `1.0.0` ceiling and adds the bounded
observations already present in L5b accounting or C0 resource metrics: Search
candidate records (distinct from returned Search result records and output
documents), vector Search requests, embedding calls and embedding items, retry
count, and retry wait milliseconds. Zero disables an optional vector,
embedding, or retry path. Embedding calls and items are enabled or disabled
together. Agentic modes cannot declare client vector or embedding request
ceilings; direct mode may enable them. These are provider-neutral request
ceilings and observations, not performance targets or permission to retry.
If retry count is zero, retry wait must also be zero. A positive retry count
with zero retry wait permits bounded immediate retries while disabling waits;
a positive wait ceiling therefore always requires a positive retry-count
ceiling. Observed wait is zero when no retry occurred, while an observed retry
may have zero wait.

`AgenticRetrievalRequestContext` seals knowledge-base, knowledge-source, Search
index, capability, static base-policy, ACL, publication, hierarchy, scope,
filter, and budget identities. Preview mode requires API
`2026-05-01-preview`, an explicit feature gate, available references/activity,
and a structured canonical-ID-only narrowing proof for:

```text
baseFilter AND filterAddOn
```

The add-on may narrow but cannot replace or broaden the static project, ACL, and
asserted-publication boundary. The stable fallback is
`direct_hybrid_prefilter` with the exact canonical Graph-ID scope and
`vectorFilterMode=preFilter`. A fallback execution context hash-references its
originating agentic context, preserves the exact scope and authority dimensions,
and binds its own direct-mode budget. Capability loss selects only the declared
fallback or a typed fail-closed result; a scope filter is never silently removed.
Search document ID is delivery identity only and never substitutes for canonical
keys.

`AgenticRetrievalRequestContext@1.1.0` binds the exact
`QueryBudget@1.1.0` ID, contract version, model-schema hash, budget hash, and
retrieval mode. A `1.0.0` context accepts only a `1.0.0` budget, and a `1.1.0`
context accepts only a `1.1.0` budget. Fallback origin and execution contexts
must use the same contract version; no implicit cross-version projection is
defined.

Every cross-contract acceptance hook conserves canonical identity authority,
including project, asset/version, run, source, content, Domain, Semantic,
canonical-schema, and locator identity. Child artifacts cannot retain a parent
ID/hash while changing those authority dimensions.

`AgenticRetrievalCoverageReceipt` records required, returned, missing,
unexpected, duplicate, and orphan canonical IDs; groups, roles, ordering,
adjacency, counts, collection hashes, subqueries, activity, references, source
calls, warnings, truncation, budget observations, citations, and typed
remediation. Status is `complete`, `partial`, or `invalid`. `complete` means
exact structural coverage inside the declared canonical scope and request
budget. Required group, sequence, and adjacency hashes are copied through the
validated retrieval scope and request context; equal self-attested receipt hashes
cannot establish completeness. Returned member/type/role/group/order records and
adjacency edges deterministically produce the returned hashes, source successes
carry response hashes, and every citation mapping includes the exact citation
envelope hash. Citation acceptance resolves those hashes against the actual
`SearchCitationEnvelope` artifacts. It never means exhaustive discovery of every
fact in a corpus. Missing
members or roles, duplicates, collection mismatch, warning, truncation, source
failure, missing reference, unsupported capability, or budget exhaustion cannot
produce `complete`.

`AgenticRetrievalCoverageReceipt@1.1.0` records every `QueryBudget@1.1.0`
ceiling and its exact observed counterpart. Its sorted
`budget_exhausted_dimensions` is derived exactly: a dimension is present if and
only if observed use exceeds its ceiling. Missing, extra, duplicate, or
undeclared names fail validation. Provider over-execution is preserved as
observed and is never clamped to the request ceiling. Any exhausted dimension
requires `partial` or `abstain`, exactly one typed
`retrieval_budget_exhausted` failure, and never `complete`; that failure is
forbidden when no dimension is exhausted. Failure records and source-call IDs
are unique. Mode-inapplicable observations are zero. Agentic source-call and
direct Search request observations each equal the exact number of source-call
records in their applicable mode, including over-execution beyond the ceiling.
Search candidate
records equal matched candidates, while Search result records and output
documents equal returned documents; these quantities are not interchangeable.
The same exact rule covers Ontology/Graph scope requests and result records,
agentic invocations/subqueries/source calls, direct requests, vector/embedding
operations, output tokens/bytes/documents, runtime, retries, and retry waits.

L5b adoption is explicit and behavior-preserving: construct and enforce a
`QueryBudget@1.1.0` before provider calls; emit a
`AgenticRetrievalRequestContext@1.1.0` with the exact budget schema/hash
binding; preserve provider counters without normalization; emit one source-call
record per observed source call and one planned-subquery record per observed
subquery; derive exhaustion from exact observed-versus-ceiling comparisons; and
select `partial` or `abstain` with typed failure when any dimension is
exhausted. Existing `1.0.0` artifacts remain readable without reinterpretation,
rewriting, or hash changes.

`SearchCitationEnvelope` and `CitationPresentation` preserve original document
name, source/file/unit/chunk/evidence IDs, canonical entity/relationship/assertion
IDs, exact caller-authorized quote, page/section, immutable locator, and
quote/content/asset hashes. An optional governed asset is referenced by exact
ID/hash. Duplicated source-file, source-unit, content-hash, locator, page, and
section lineage must equal the canonical identity envelope exactly. A protected
asset URL is response-only and HTTPS-only. It is a private out-of-band response
value that is absent from the registered persisted schema as well as
serialization, canonical hashes, caches, logs, metrics, fixtures, and durable
receipts. Secrets, tokens, durable access URLs, unauthorized quotes, stale
authority, and missing exact evidence fail closed.

## 13. Registry and compatibility

The registry maps each kind to one model and exact supported versions. Readers
reject unknown kinds, unknown majors, and unregistered minor/patch versions.
Schema 1.x domain behavior remains unchanged. Domain schema 2.0 is
new-project-only. There is no implicit migration, dual write, automatic
feature activation, or reuse of schema-1 approval artifacts.

C0.Runtime contracts are registered under the same exclusive C0 owner. A
required field, changed meaning/type, changed ID/hash seed, tightened accepted
value set, lifecycle transition change, or serving enum addition requires a new
major.

## 14. Contract gate

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
- exact C0.RDF authority tuples, governed HTTPS namespaces, named graph
  layering, conservative vocabulary inventories, endpoint-set encoding, format
  profiles, RDFC-1.0 equivalence hashes, SHACL summaries, protected-graph
  policy references, and metadata-only external alignments;
- RDF round-trip failure on missing/extra triples, graph/authority/base-IRI
  drift, label identity, blank-node instability, or SHACL violations;
- exact/narrow/expand and all four hierarchy scope modes over canonical IDs;
- stable entity identity across subtype reclassification with revised
  type-assertion and scope hashes;
- strict hierarchy expansion depth versus relationship K separation;
- preview `baseFilter AND filterAddOn` narrowing and direct `preFilter` fallback;
- bounded complete/partial/invalid coverage with generic member sets,
  truncation, warnings, missing IDs, collisions, and orphan rejection;
- exact authorized citation lineage and transient protected URL exclusion;
- exact C0.Runtime registry negotiation with no synthesis, answer, or retry
  behavior;
- manifest totals and receipt skip preconditions;
- secret/token/path rejection;
- domain hash, `CommonLineageRow`, source locator, canonical row ID/hash,
  checkpoint, and semantic projection-header adapter equality; and
- the existing unit plus contract test gate.

No test in this gate performs a remote request or live Fabric mutation.

## 15. Deferred decisions and exclusions

Deferred:

- numeric cache TTLs;
- numeric latency, RSS, service-call, token, byte, and retry thresholds;
- downstream Agent claim/answer evidence and final-answer UX; and
- L6 transport, retrieval, orchestration, retry, synthesis, and acceptance
  behavior.

Excluded from C0.Core:

- L1 proposal/intake/approval UX;
- L2 extraction behavior or activation;
- L3 validation pipeline integration;
- canonical Parquet or Arrow rewrites;
- L4 projection execution;
- L5 compile/deploy/read-back behavior;
- L5c RDF serialization, parsing, canonicalization, SHACL execution, storage,
  publication, remote import, or external ontology fetch;
- L6 Graph/Search/synthesis execution;
- L7 live acceptance;
- remote requests, deployment, and live Fabric changes.
