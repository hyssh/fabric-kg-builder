# SPEC-008: L5c RDF Semantic Interchange Boundary

**Status:** Contract foundation
**Version:** 1.0.0
**Date:** 2026-08-26
**Owner:** C0 Contract Owner
**Depends on:** SPEC-002, SPEC-003, SPEC-006, SPEC-007

## 1. Purpose

L5c may eventually publish a standardized RDF view for review and interchange.
The RDF dataset is always a deterministic derived view of canonical contracts
and the sealed Fabric Ontology, Graph, and Search projections. It never becomes
a competing semantic, identity, membership, evidence, access, or deployment
authority.

This specification defines only the C0.RDF interchange contract and the future
L5c adoption boundary. It adds no serializer, parser, validator, dependency,
CLI command, deployment behavior, or remote access.

## 2. Standards basis

The contract uses the following official standards as its rationale:

- [RDF 1.1 Concepts and Abstract Syntax](https://www.w3.org/TR/rdf11-concepts/)
  defines IRIs, literals, triples, graphs, and RDF datasets.
- [RDF 1.1 Turtle](https://www.w3.org/TR/turtle/) defines the human-review
  serialization.
- [RDF 1.1 XML Syntax](https://www.w3.org/TR/rdf-syntax-grammar/) defines the
  compatibility serialization.
- [JSON-LD 1.1](https://www.w3.org/TR/json-ld11/) defines the optional
  JSON-oriented serialization.
- [RDF Dataset Canonicalization](https://www.w3.org/TR/rdf-canon/) defines
  RDFC-1.0 canonicalization of RDF datasets represented as N-Quads. Canonical
  N-Quads, not sorted N-Triples, is the equivalence authority because graph
  names and blank-node canonicalization are dataset semantics.
- [OWL 2 Profiles](https://www.w3.org/TR/owl2-profiles/) motivates the
  conservative OWL 2 RL-compatible derived vocabulary.
- [SHACL](https://www.w3.org/TR/shacl/) defines projection validation.
- [PROV-O](https://www.w3.org/TR/prov-o/) defines provenance links without
  embedding detailed source quotes.

Third-party or community RDF samples can demonstrate syntax but are not
normative authorities and contribute no copied ontology terms, IRIs, examples,
defaults, or content to this contract.

## 3. Registered contract surface

| Contract | Version | Role |
|---|---:|---|
| `c0.rdf_projection_manifest` / `RdfProjectionManifest` | `1.0.0` | Declares exact source authority, governed IRIs, graph layers, vocabulary, and alignments |
| `c0.rdf_projection_acceptance_bundle` / `RdfProjectionAcceptanceBundle` | `1.0.0` | Self-contained accepted manifest/artifact/receipt proof |
| `c0.rdf_serialization_artifact` / `RdfSerializationArtifact` | `1.0.0` | Describes one serialized artifact and its canonical dataset equivalence |
| `c0.rdf_validation_receipt` / `RdfValidationReceipt` | `1.0.0` | Records SHACL and exact serialization round-trip outcomes |

All instances use strict fields, canonical JSON, deterministic SHA-256 seals,
and exact version negotiation. Unknown fields, unregistered versions, and
unknown major versions fail closed.

## 4. Authority conservation

Every projection manifest has `authority="derived"` and binds this exact source
tuple:

1. sealed L4 `SemanticServingProjection` ID/hash;
2. L5a projection manifest ID/hash;
3. `PublicationCrosswalk@1.1.0` ID/version/schema hash/crosswalk hash;
4. Ontology, Graph, and Search `ProjectionEquivalence` IDs/hashes covering
   expected, compiled, deployed, and read-back equality;
5. Domain contract ID/hash, hierarchy hash, identity-policy hash,
   relationship-policy hash, and K-policy hash;
6. `RequiredMemberManifest@1.1.0` ID/version/schema hash/manifest hash and
   `authoritative_collection_hash`; and
7. original L3 `ArtifactManifest` ID/hash.

The tuple references the existing `PublicationAuthorityReferences` primitive
for membership and original-artifact authority. It does not duplicate member
lists, canonical authority, access principals, or projection behavior.
The manifest's `authority_reference_set_hash` is computed from the exact sorted
reference tuples: stable reference name, ID, optional contract version,
optional schema hash, and content/policy hash. Artifacts, observations,
receipts, and the acceptance bundle must equal that manifest value; downstream
agreement is never authority.

## 5. IRI and identity policy

Production configuration must supply distinct, absolute, credential-free HTTPS
ontology and instance base IRIs under explicit governance. There is no default,
`example.org`, label-derived, local-path, query-bearing, or signed namespace.
The ontology IRI, version IRI, and semantic version are explicit, and the
version IRI contains that semantic version.
The namespace governance decision is bound by exact ID/hash.

Canonical IDs map deterministically under mapping version `1.0` to UTF-8
percent-encoded path segments with canonical uppercase escapes. Exact term
output is `ontology_base_iri + percent_encode(canonical_id)`. Class, property,
and relationship mappings are globally injective and reject external bases,
collisions, label-derived names, noncanonical escape case, and path traversal.
Instance/skolem mapping is separately versioned under the governed instance base.
Labels are annotations only and never define identity. Nodes without a
canonical ID use deterministic skolem IRIs; unstable blank-node identity is
forbidden in persisted artifacts and fails round-trip validation.

## 6. Dataset and graph layering

The dataset inventory has these named graph roles:

| Role | Required | Content |
|---|---:|---|
| `common_schema` | yes | Common derived RDF/RDFS/OWL vocabulary |
| `domain_schema` | yes | Domain classes, properties, hierarchy, endpoint and value semantics |
| `shacl_shapes` | yes | Projection validation shapes |
| `instances` | no | Canonical instance and relationship assertions |
| `provenance_authority` | yes | Authority, source, evidence, governed-asset IDs/hashes and PROV links |

Every mandatory role exists exactly once and declares `required=true`. An
instances graph is optional in the manifest, but when present it is required by
the represented dataset. Graph IDs and IRIs are unique. Every graph also
declares its expected graph hash and triple count.

Schema graphs contain schema triples only. Instance and provenance graphs
contain instance/evidence triples only and carry exact access-policy IDs/hashes.
The RDF dataset contains no full source quotes, secrets, ACL principals,
transient URLs, or signed URLs. Search remains the detailed quote surface.

## 7. Vocabulary and endpoint semantics

The manifest inventories exact classes, properties, explicit relationships,
parents, key properties, source/domain endpoint sets, target/range endpoint
sets, and literal value types. Every ID resolves within the inventory and every
exact ID set is sealed.

The declared vocabulary is a conservative OWL 2 RL-compatible derived
vocabulary. For one domain or range, a single RDFS term is permitted. Multiple
domains or ranges must not be represented by repeated `rdfs:domain` or
`rdfs:range`, because repeated statements mean intersection. Each source/domain and target/range side with more than one endpoint has a
separate deterministic union node IRI derived from the term ID, side name, and
sorted sealed endpoint-set hash. The source/domain and target/range nodes are
always side-distinct; swaps, reuse, missing, extra, or noncanonical nodes fail.
A side with one endpoint uses its direct class/value IRI and has no union node.
The selected encoding is either:

- a deterministic named `owl:unionOf` node; or
- a SHACL `sh:or` endpoint constraint.

The contract records the exact endpoint set and encoding. L5c may not infer,
broaden, narrow, or relabel it.

## 8. Serializations and equivalence

Required formats are Turtle, RDF/XML, and canonical N-Quads. JSON-LD is
optional. Each artifact records:

- exact media type and W3C syntax version;
- content SHA-256 and byte count;
- triple count, graph count, and exact named graph IDs;
- sealed graph ID/IRI/role/required/policy/triple-count bindings and exact
  graph-inventory hash;
- canonical ID-set hash;
- `canonical_dataset_hash_algorithm="RDFC-1.0"` and canonical dataset hash; and
- `blank_node_policy="none_after_deterministic_skolemization"`.

The future implementation must parse every emitted serialization, canonicalize
the complete dataset with RDFC-1.0, and compare the exact dataset hash, named
graphs, triple set/count, and authority-reference set. Missing or extra triples,
serialization drift, base-IRI drift, label identity, or unstable blank nodes
make the receipt non-equivalent.

Standalone artifact validation enforces exact role sets by exposure:
`public_schema` contains exactly common/domain/SHACL roles with no ACL policy;
`protected_dataset` adds mandatory provenance and optional instances, whose
policies equal the artifact policy. `validate_against_manifest` rejects any
missing, extra, relabeled, optionalized, or policy-shifted graph binding.

## 9. SHACL boundary

SHACL validates canonical identity, keys, cardinality, relationship endpoints,
and types in the projection. A receipt records the shapes hash, conforms flag,
violation/warning/info counts, report hash, and validator identity/version.
`conforms` is true exactly when violation count is zero.

Every observation seals the actual artifact contract hash, format/media type,
content and canonical-dataset hashes, graph inventory hash/IDs, and triple
count. Receipt formats equal observation formats.
`validate_against_manifest_and_artifacts` requires that set to equal the
manifest exactly, including JSON-LD when selected and no undeclared extra, and
binds the exact manifest, authority, and artifact set.
It derives the manifest's exact `shacl_shapes` graph ID/IRI/hash/triple count,
requires every serialization binding to equal it, and requires
`RdfShaclValidationSummary.shapes_hash` to equal that same graph hash. Missing,
extra, divergent, or coordinately resealed shapes metadata fails.

`RequiredMemberManifest@1.1.0` remains completeness authority. SHACL checks
that the RDF projection matches the sealed membership; it does not discover,
infer, default, or recompute membership.

## 9A. Acceptance boundary

`RdfProjectionAcceptanceBundle` is the only successful-publication acceptance
surface. It embeds the exact manifest, complete serialization artifact metadata
objects, and validation receipt, then validates all manifest/artifact/receipt
set, hash, format, authority, exposure, graph, dataset, round-trip, SHACL, and
identity invariants in one model-level validator. It requires conforming SHACL
and exact round-trip equivalence, carries the manifest
`authority_reference_set_hash`, and seals an accepted bundle hash.

L5c MUST emit and consume this bundle. Individual
`RdfProjectionManifest`, `RdfSerializationArtifact`, and
`RdfValidationReceipt.model_validate` calls establish syntax and local
integrity only; they MUST NOT be interpreted as accepted or successfully
published RDF.

## 10. External alignment

External alignment is opt-in governed metadata only. Each alignment records a
target IRI; an approved `rdfs:seeAlso`, exact-match, or equivalence relation;
immutable source artifact/version/hash; license; and approval ID/hash.

There is no default `owl:imports`, remote fetch, URL in implementation code, or
copied third-party ontology content. Approval of metadata does not authorize
network retrieval or semantic adoption.

Manifest, artifact, and receipt validation recursively rejects bearer tokens,
API keys, URI credentials, Azure SAS, AWS SigV4, Google signed URL, generic
signature/credential/password/passwd/pwd/auth/authentication/client-secret
query parameters after NFKC, case, and separator normalization, secret-looking
values, and repeatedly
percent-encoded variants from nested IDs, references, metadata, and alignment
values. Decoding is size-bounded and depth-bounded and fails closed if not
stable. Stable governed and W3C vocabulary IRIs remain permitted.
NFKC and case normalization are applied after every decode round and to the
final stable value. Absolute URLs parse query keys/values and fragments;
non-URL text requires a bearer/header pattern, an equals assignment, or a
colon-plus-whitespace assignment. Colon-delimited namespace IDs without
assignment context remain valid.
Every RDF-owned model inherits the RDF-local strict base configured to hide
inputs, preflights nested sensitive values, and returns sanitized structured
validation errors containing no rejected raw value.

## 11. Access boundary

Public schema artifacts may include only common, domain, and SHACL schema
graphs and carry no principal ACL policy. Protected dataset artifacts and
instance/provenance graphs reference existing `AccessPolicy` IDs/hashes.
Secrets and principal lists are never copied into public RDF.

## 12. Future L5c adoption plan

A later behavior PR may adopt these contracts in this order:

1. negotiate all source contract versions and validate the exact authority tuple;
2. load production-governed ontology and instance base IRIs with no fallback;
3. map canonical IDs to deterministic percent-encoded/skolem IRIs;
4. build the declared named graphs without quotes, secrets, or remote imports;
5. emit Turtle, RDF/XML, optional JSON-LD, and N-Quads;
6. canonicalize the dataset with RDFC-1.0 and construct one
   `RdfSerializationArtifact` per format;
7. run approved SHACL shapes without recomputing membership;
8. parse every serialization and compare exact dataset, graph, triple,
   authority, and base-IRI equality;
9. emit `RdfValidationReceipt`, assemble `RdfProjectionAcceptanceBundle`, and
   fail publication unless bundle validation proves conformance and exact
   round-trip equivalence;
10. consume only the accepted bundle as successful-publication proof; and
11. store public schema and protected dataset artifacts under their existing
    governed asset/access policies.

That PR must separately select and review an RDF library, add dependencies,
implement behavior, expose any CLI surface, and integrate deployment. None of
those decisions or behaviors are part of this contract foundation.

## 13. Compatibility

This change is additive. Existing schema files and contract bytes remain
identical; only the registry receives four new `1.0.0` entries and advances to
registry version `1.7.0`. Package version remains `0.2.3`. Schema 1 and every
existing contract remain unchanged.
