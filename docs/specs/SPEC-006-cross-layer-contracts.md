# SPEC-006: Cross-Layer Contracts (C0.Core)

**Status:** Approved foundation
**Version:** 1.0.0
**Date:** 2026-08-24
**Owner:** C0 Contract Owner
**Depends on:** Bootstrap PR #30, SPEC-001 through SPEC-005

## 1. Scope

C0.Core is the shared contract, specification, schema, fixture, and hashing
foundation required before L1-L3. It is additive and contains no proposal UX,
LLM request, extraction activation, evidence pipeline integration, canonical
Parquet rewrite, publication, deployment, runtime query, citation, answer, or
live Fabric behavior.

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

C0.Publish crosswalk/equivalence/governed-asset details and C0.Runtime
query/citation/answer contracts are explicitly deferred additive slices. They
are not registered by C0.Core.

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

All registered artifacts use contract version `1.0.0`.

| `contract_kind` | Model | Contract-specific authority |
|---|---|---|
| `c0.identity` | `CanonicalIdentityEnvelope` | Cross-layer references; no generic `record_id` |
| nested primitive | `ImmutableSourceLocator` | Immutable typed locator, version `1.0` |
| `c0.source_unit` | `SourceUnit` | Exact source text and partition identity |
| `c0.evidence_span` | `EvidenceSpan` | Local verifier-minted exact span |
| `c0.candidate_lifecycle_record` | `CandidateLifecycleRecord` | Append-only state event |
| `c0.candidate_accounting_disposition` | `CandidateAccountingDisposition` | One input disposition |
| `c0.canonical_entity_assertion` | `CanonicalEntityAssertion` | Typed `EntityRow` reference |
| `c0.canonical_relationship_assertion` | `CanonicalRelationshipAssertion` | Typed `RelationshipRow` reference |
| `c0.canonical_property_assertion` | `CanonicalPropertyAssertion` | Typed `PropertyObservationRow` reference |
| `c0.audit_projection` | `AuditProjection` | Complete accounting header |
| `c0.semantic_serving_projection` | `SemanticServingProjection` | Exact asserted subset header |
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

## 10. Manifests, receipts, and resource evidence

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

## 11. Registry and compatibility

The registry maps each kind to one model and exact supported versions. Readers
reject unknown kinds, unknown majors, and unregistered minor/patch versions.
Schema 1.x domain behavior remains unchanged. Domain schema 2.0 is
new-project-only. There is no implicit migration, dual write, automatic
feature activation, or reuse of schema-1 approval artifacts.

Compatible additive C0.Publish and C0.Runtime contracts will register later
under the same owner. A required field, changed meaning/type, changed ID/hash
seed, tightened accepted value set, lifecycle transition change, or serving enum
addition requires a new major.

## 12. Contract gate

The C0.Core gate includes:

- strict unknown field/type/version rejection;
- canonical JSON and golden SHA verification;
- JSON/YAML round trips and generated schema registry hashes;
- all allowed and forbidden lifecycle transitions;
- exhaustive Unicode code-point span/quote/hash ranges;
- mutually exclusive candidate accounting;
- asserted-only serving set equality;
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
