# fabric-kg 0.2.3 Isolated CLI and Live Deployment Smoke Test

Date: 2026-08-23  
Release: `fabric-kg` 0.2.3  
Tested revision: `b47dd7e123349e6afcacdb80588b718bed981ce1`  
Status: Partial live success with release-follow-up defects identified

## 1. Purpose

This report records the first clean, installed-CLI smoke test of 0.2.3 against
representative semi-structured source documents and existing Azure and
Microsoft Fabric resources.

The test intentionally did not use an editable repository Python environment.
The CLI was installed into an isolated local environment and treated as the
product under test.

The smoke test had four objectives:

1. verify the merged 0.2.3 package and command surface;
2. exercise approved-domain and sealed-semantic workflows on real PDFs;
3. perform a reviewed dry run before any live mutation;
4. validate nonempty Fabric and Azure AI Search serving surfaces.

No secrets, generated credentials, source documents, or local test artifacts
are committed with this report.

## 2. Release and Installation Evidence

The test worktree matched merged `main` at:

```text
b47dd7e Merge pull request #28 from hyssh/hyssh-ontology-pipeline-assessment
```

The isolated CLI reported:

```text
fabric-kg, version 0.2.3
```

Package metadata, entry point, version, and command discovery were verified
from the installed environment.

The installed-tree release gate completed:

```text
2694 passed, 4 deselected
```

## 3. Test Domain and Sources

The corpus consisted of 22 Microsoft Surface service-guide PDFs.

The approved domain was a Surface Technician Event Ontology:

> An ontology representing processual entities performed by Surface device
> technicians and support engineers, including diagnosis, repair, replacement,
> verification, safety, tools, parts, outcomes, and source evidence.

The domain contract was:

- deterministically validated;
- reviewed by the configured LLM;
- scored at 0.93;
- explicitly approved before enrichment.

The final sealed semantic contract contained:

- 20 entity types;
- 14 directed relationship types;
- exact endpoint and direction definitions;
- evidence policy `required_for_asserted`;
- stable semantic and Fabric identifiers.

Final semantic contract hash:

```text
sha256:8490be3d5e20772fdc6ba4fed03f00c224889d7c762f85276a448d28e954eb6d
```

## 4. Safety and Dry-Run Gate

Existing Azure and Fabric resources were discovered and checked before
deployment. The dry run proposed adoption of existing Azure services and
creation or update of only the named Fabric serving items.

No live Fabric mutation occurred until the dry-run plan was reviewed and
explicitly approved in the test session.

The final live stages were run using one stable processing run:

```text
f201a612-e7c2-4ff3-96f2-4e2ba613b57b
```

## 5. Enrichment and Compilation Results

The first enrichment pass was interrupted by service throttling. Checkpointed
resume preserved successful work and eventually completed all final-contract
work units:

```text
214 succeeded
0 failed
```

Final canonical compilation wrote 15 Parquet tables, including:

| Table | Rows |
|---|---:|
| `source_files` | 22 |
| `entities` | 1,354 |
| `relationships` | 1,723 |
| `semantic_entities` | 1,354 |
| `semantic_relationships` | 135 |
| `chunks` | 15,341 |
| `document_elements` | 15,320 |
| `evidence` | 16,277 |
| `claims` | 85 |
| `claim_evidence` | 85 |
| `visual_assets` | 1,999 |
| `visual_regions` | 1,999 |

Data-integrity gates passed. Semantic, Ontology, Graph, agent, and Search
artifacts compiled under the final semantic hash.

Ontology compilation produced:

```text
20 entity types
14 relationship types
70 definition parts
0 bridge errors
0 bridge warnings
```

Artifact validation passed, and the package included:

```text
ONTOLOGY_SEARCH_CONNECTION.md
```

## 6. Live Outcomes

### 6.1 Fabric Lakehouse

The live Lakehouse is populated, not an empty shell.

Thirteen lean graph/Ontology tables were written successfully. Text chunks
were intentionally excluded from the Lakehouse serving projection because
they are served through Azure AI Search.

Six optional tables not produced by this source pipeline were reported as
skipped rather than fabricated.

### 6.2 Azure AI Search

Three Search indexes were created and populated:

| Index | Live document count |
|---|---:|
| `surface-tech-kg-chunks` | 15,440 |
| `surface-tech-kg-document-elements` | 15,320 |
| `surface-tech-kg-visual-assets` | 3,998 |
| **Total** | **34,758** |

Live read-back confirmed chunk records include:

- `chunk_id`;
- `source_quote`;
- `source_quote_is_verbatim`;
- `source_file_id`.

A sampled live chunk contained all four fields and marked its source quote as
verbatim.

Local embedding generation was deferred after repeated Azure OpenAI S0
`429 RateLimitReached` responses. Non-vector Search schemas and documents were
still deployed successfully.

### 6.3 Fabric Ontology and Graph

The Ontology and Graph item shells existed, but their final definitions could
not be populated safely.

This is a partial deployment outcome. It must not be reported as a successful
Ontology or Graph deployment.

## 7. Root-Cause Findings

### 7.1 Candidate lifecycle and evidence

Aggregate relationship extraction produced:

```text
1,849 candidates
  137 asserted
1,373 rejected
  319 unresolved
   20 discovery-lane candidates
```

Normalized enriched records contained:

```text
asserted + evidence present      137
rejected + evidence missing      766
rejected + evidence present      588
unresolved + evidence missing    339
```

All asserted relationships had evidence. Missing `evidence_id` values were not
removed from valid asserted rows during later processing.

In representative rejected output, GPT produced relationship candidates but
returned:

```text
evidence_id_hint: null
evidence_id_hints: []
source_span_ids: []
```

The CLI correctly marked these candidates rejected or unresolved instead of
asserted.

### 7.2 Subtype compatibility

Some candidates were rejected with `source_type_mismatch` even though the
actual source type was a valid subtype.

Example hierarchy:

```text
ReplacementEvent <: RepairEvent <: SupportEvent
```

The approved `has_step` predicate accepts `SupportEvent` as its source.
A `ReplacementEvent` should therefore be compatible unless the contract marks
the endpoint exact-only.

The 0.2.3 validation path showed evidence of exact-type comparison rather than
transitive subtype compatibility.

### 7.3 Semantic quality versus publication scope

Semantic quality correctly reported full evidence coverage for the accepted
projection:

```text
relationship_evidence=1.000
endpoint_resolution=1.000
```

`compile-data` preserved raw candidates for audit and separately produced 135
semantic relationships. This separation is valuable.

The deployment defect occurred because Ontology materialization selected the
broader raw `relationships` table and marked `evidence_id` non-nullable for all
selected rows. It therefore reintroduced rejected and unresolved candidates
that compilation had excluded from the semantic serving projection.

The deployment failed with `BOUND_TABLE_REQUIRED_VALUE_NULL` across ten typed
relationship tables.

Correct invariant:

```text
raw relationships
  -> audit and lineage

asserted + evidence-backed semantic relationships
  -> Ontology and Graph publication
```

### 7.4 Graph dependency

The compiled Graph schema expected 20 node labels, 14 edge bindings, and 34
per-type source tables. Those typed tables are materialized during the blocked
Ontology semantic-projection step.

Graph deployment therefore failed with Fabric `ModelValidationError` because
the referenced typed tables did not exist. This was a valid downstream
consequence, not an independent absence of graph data.

### 7.5 Resume invalidation

After the semantic contract changed, `build-deploy --resume` retained
previously successful data stages from the old semantic hash.

`compile-search` correctly rejected the stale artifacts:

```text
authoritative entity bound to old contract hash; expected final contract hash
```

The rejection was a desirable provenance safety gate. The defect was that
resume did not invalidate semantic-dependent upstream stages automatically.

### 7.6 Identity remediation guidance

The initial Ontology identity error recommended adding an `entity_id` alias to
`DocumentChunk`. Mapping both semantic identity names to the same physical
column then caused duplicate crosswalk validation.

The valid correction was to map semantic `DocumentChunk.entity_id` directly
to physical `chunk_id`.

### 7.7 Existing-resource endpoint adoption

Initial existing-resource adoption synthesized incorrect service endpoints for
Document Intelligence and the Foundry/OpenAI path. The test configuration had
to be corrected with authoritative resource endpoints before enrichment could
complete.

### 7.8 Work-unit diagnostics

One enrichment work unit initially failed with only the exception type
`ValueError`. The checkpoint did not preserve a sanitized actionable message.
A later identity-bound resume succeeded.

Checkpoints should retain safe exception details, source identity, and retry
context.

## 8. Environment Interruption Distinguished from Product Defects

Direct Search upload initially stopped after 9,000 chunk documents because the
local macOS resolver temporarily failed to resolve a healthy public Search
endpoint.

Azure reported the service as running with public network access enabled.
After a local DNS-cache refresh, the same idempotent CLI command completed all
three indexes.

This interruption is not classified as a `fabric-kg` semantic defect, though
batch retry and resumability could improve operator experience.

## 9. Consolidated Defect Record

The related 0.2.3 recovery and publication defects were consolidated in GitHub
issue #29.

The issue covers:

- semantic-hash dependency invalidation on resume;
- misleading DocumentChunk identity remediation;
- `required_for_asserted` publication inconsistency;
- existing-resource endpoint synthesis;
- incomplete per-work-unit diagnostics.

## 10. Strategic Value of 0.2.3

0.2.3 is not discarded by these findings. It proved the following production
assets with real data:

- isolated install and command surface;
- approved domain and sealed semantic authority;
- PDF extraction and resumable enrichment;
- canonical Parquet and integrity gates;
- deterministic Ontology, Graph, agent, and Search compilation;
- semantic-hash provenance protection;
- populated Fabric Lakehouse tables;
- populated Azure AI Search indexes with source quotation and lineage.

The smoke test also established the product boundary for 0.2.4:

1. Copilot-assisted domain and competency-question intake;
2. bounded relationship vocabulary `N`;
3. bounded reasoning depth `K`;
4. exact source-span evidence for every asserted edge;
5. transitive subtype endpoint compatibility;
6. explicit asserted, unresolved, rejected, and discovery lifecycles;
7. audit retention separate from semantic publication;
8. Ontology and Graph deployment exclusively from the sealed semantic
   projection;
9. compile/deploy count and hash equivalence;
10. bounded Graph query plans using the same approved `K`.

These requirements are defined in:

```text
docs/PRD-0.2.4-COPILOT-DOMAIN-DESIGN.md
```

## 11. Final 0.2.3 Assessment

| Surface | Result |
|---|---|
| Isolated installation | Passed |
| Version and command discovery | Passed |
| Release tests | Passed |
| Domain review and approval | Passed |
| Final-contract enrichment | Passed, 214/214 |
| Canonical compilation | Passed |
| Semantic/Ontology/Graph/agent artifact compilation | Passed |
| Artifact validation and package | Passed |
| Fabric Lakehouse live data | Passed, 13 populated tables |
| Azure AI Search live data | Passed, 3 indexes and 34,758 documents |
| Vector embeddings | Deferred by service quota |
| Fabric Ontology definition | Blocked by publication-scope defect |
| Fabric Graph definition | Blocked by missing semantic typed tables |

0.2.3 should be retained as the validated foundation. 0.2.4 should address the
identified semantic lifecycle and domain-design gaps without regressing the
working extraction, lineage, compilation, Lakehouse, and Search capabilities.
