# Project Assessment and 0.2.3 Improvement Plan

Date: 2026-08-23  
Release target: 0.2.3

## Executive assessment

`fabric-kg-builder` already provides a substantial pipeline for converting
structured, semi-structured, and unstructured sources into canonical Parquet,
Fabric Ontology and Graph artifacts, and Azure AI Search indexes. Its strongest
qualities are lineage, deterministic semantic contracts, deployment receipts,
and evidence-aware validation.

The project is not yet complete against the next product goal:

1. Domain inference is based mainly on filenames and tabular headers. It does
   not yet synthesize representative document content, example prompts, and
   competency questions into a cited domain inference report.
2. External ontology discovery, selection, mapping, and immutable provenance are
   absent. Local semantic contracts are authoritative for a build, but they are
   not linked to recognized external standards.
3. Graph, Ontology, Search, and Data Agent components exist, but the production
   retrieval path does not yet enforce one ontology/graph-first, graph-filtered
   hybrid search loop.
4. Open issues #25, #26, and #27 expose release-blocking deployment reliability
   problems. These belong in 0.2.3 before broader ontology automation.

## Goal fit

| Goal | Current state | Assessment |
|---|---|---|
| Accelerate semi/unstructured data transformation to ontology | Broad source adapters, extraction, enrichment, canonicalization, semantic compilation, and Fabric deployment exist | Strong foundation |
| Infer domain from data, prompts, and documents and apply it throughout the pipeline | Approved domain contracts and hashes exist; inference is shallow and not connected to external ontology selection | Partial |
| Find and retain the most relevant external ontology | No provider abstraction, ranking, immutable snapshot, or term mapping contract | Missing |
| Use Ontology first and AI Search for supporting detail | Graph and Search executors, routing, citations, and validation exist; orchestration is not enforced as one bounded retrieval loop | Partial |

## Existing strengths

### Transformation and evidence

- Source routing covers CSV, TSV, XLS/XLSX, PDF, DOCX, PPTX, HTML,
  Markdown, Parquet, and images.
- Document tables, figures, images, OCR, spans, and chunks remain traceable.
- LLM output is intermediate and schema validated; canonical Parquet remains
  the durable data contract.
- Canonical rows carry asset, asset-version, processing-run, source locator,
  schema-version, and domain-hash context.
- Semantic contracts and manifests seal entity, relationship, property,
  evidence, and deployment expectations.

### Domain governance

- `domain.yaml` captures business context, terminology, candidate entities and
  relationships, constraints, examples, competency questions, and approval.
- Enrichment rejects missing, unapproved, or stale domain contracts.
- Domain and semantic hashes flow into processing and search artifacts.

### Retrieval and release validation

- Direct Graph execution validates semantic query plans.
- Azure AI Search supports semantic, keyword, vector, and exact-ID retrieval.
- Data Agent packaging validates selected ontology/graph elements, properties,
  instructions, examples, and persisted read-back.
- Runtime validation links graph results, search evidence, citations, and
  contradiction checks.

## Principal opportunities

### 1. Evidence-based domain inference

Add a versioned `DomainInferenceReport` generated from:

- observed source metadata and schemas;
- bounded samples of headings, text, tables, and image descriptions;
- user-provided prompts and positive/negative examples;
- competency questions;
- candidate domains and subdomains with confidence, alternatives, and cited
  source spans.

The report must remain a proposal until reviewed. An approval step should
produce the domain contract used by enrichment, semantic design, retrieval, and
deployment.

**Reason:** filename/header heuristics are fast but insufficient for legal,
clinical, engineering, and other ambiguous corpora. Cited evidence and explicit
alternatives make inference reviewable instead of silently authoritative.

### 2. External ontology discovery and IP-safe context

There is no universal registry that proves which ontology is authoritative for
every domain. Discovery should rank candidates and allow the reviewer to select
multiple modular ontologies or reject all candidates.

External ontology use must be opt-in. The project must not:

- hard-code a third-party ontology, registry, or provider URL in runtime code;
- bundle or redistribute third-party ontology content;
- copy third-party terms into generated definitions by default;
- fetch a live ontology during a reproducible build;
- imply that technical availability grants reuse rights.

Candidate discovery should produce a review report only. An operator must supply
or approve the source, license, permitted use, attribution requirements, and
immutable artifact before any mapping is enabled. Provider locations belong in
environment-specific, non-default governance records rather than source code or
compiled Fabric definitions.

Recommended discovery order:

1. Regulator, standards body, or recognized domain consortium.
2. Domain registry, such as OBO Foundry/OLS/BioPortal for life sciences.
3. Cross-domain catalogues such as BARTOC, FAIRsharing, or LOV.
4. General web discovery as a fallback.

Rank candidates by:

- publisher and locus of authority;
- scope and competency-question coverage;
- persistent term and version identifiers;
- release and maintenance history;
- license compatibility;
- machine-readable serialization and API access;
- term definitions, relation reuse, adoption, and mappings.

If legal and license review permits use, an approved source should be represented
by an `OntologyReference` stored outside the generated ontology definition:

```yaml
reference_id: ontology-ref:<approved-source>
ontology_iri: <operator-supplied-iri>
version_iri: <operator-supplied-version-iri>
publisher_version: <version>
publisher: <publisher>
license_iri: <approved-license>
landing_page: <governance-reference-only>
retrieval_url: <approved-immutable-location>
repository: <approved-repository>
commit: <immutable-git-commit>
artifact_sha256: sha256:<digest>
retrieved_at_utc: <timestamp>
selection_score: <0..1>
selection_rationale: <reviewed explanation>
approval:
  status: approved
  reviewer: <identity>
  legal_review_id: <approval-record>
  permitted_uses: [mapping-reference]
```

Each local type/property/relationship mapping should carry:

- internal semantic ID;
- external term IRI;
- ontology reference ID;
- SKOS mapping predicate (`exactMatch`, `closeMatch`, `broadMatch`,
  `narrowMatch`, or `relatedMatch`);
- method, confidence, evidence IDs, state, and reviewer.

Do not use `owl:sameAs`, `owl:equivalentClass`, or `skos:exactMatch` unless the
strong equivalence semantics are justified.

### 3. Legal-domain ontology example and boundary

FOLIO/Open Legal Standard is a relevant discovery example for a legal corpus,
but it must not be a built-in pipeline dependency or default. The user-supplied
explorer URL may be consulted during a human assessment, but it must not be
persisted into runtime code, default configuration, generated ontology
definitions, or deployment artifacts.

Before any use, the operator must obtain legal approval for the intended use,
confirm the applicable license and attribution obligations, and decide whether
remote reference, local mapping, or import is permitted.

If approved, acquisition must be explicit and immutable:

1. keep the provider reference in an operator-controlled governance record;
2. acquire only the specifically approved artifact;
3. record the exact version/commit, retrieval time, license decision, required
   attribution, and SHA-256;
4. restrict storage and redistribution according to the approval;
5. make builds consume only the approved snapshot or mapping record;
6. support revocation without changing application code.

This retains a consistent legal vocabulary through ingestion, extraction,
enrichment, ontology design, deployment, and runtime citations without making
the project itself a distributor or implicit licensor of third-party content.

### 4. Ontology-first layered retrieval

Implement one deterministic `retrieve_grounding` coordinator:

1. Infer intent and resolve terms against the approved domain and ontology
   mappings.
2. Query Fabric Ontology/Graph first for canonical IDs, aliases, constraints,
   and bounded paths.
3. Validate and cap returned identifiers in code.
4. Build a deterministic `search.in(...)` filter; never accept raw
   model-authored OData filters.
5. Run one Azure AI Search hybrid keyword/vector/semantic query with
   `vectorFilterMode=preFilter`.
6. If graph resolution yields no IDs, perform one unfiltered Search fallback.
7. Evaluate coverage, freshness, and contradictions; issue only bounded
   follow-ups.
8. Return claim-level graph and document citations or abstain.

Stop when evidence is sufficient, no new evidence is found, or iteration,
token, and time budgets are reached.

**Reason:** the graph supplies meaning and bounded identity; Search supplies
verbatim detail and source evidence. Running them independently loses the graph
constraint and increases irrelevant retrieval.

### 5. Layered Ontology and generated connection guide

The sealed semantic contract projects three Fabric Ontology modules:

- `common-entities`: reusable nouns representing things and concepts;
- `common-relationships`: reusable directed verbs between common entities;
- `domain`: domain-specific nouns and directed verbs.

Each packaged pipeline output includes
`ONTOLOGY_SEARCH_CONNECTION.md`. The generated guide records the layered
Ontology, noun-verb-noun relationships, canonical crosswalk to Fabric Graph and
Search, source-quotation coverage, and the required query sequence:

`Fabric Ontology -> Fabric Graph -> ID-filtered AI Search -> cited answer`.

Text Search records retain both searchable `content` and an explicit
`source_quote`, plus `source_quote_is_verbatim`, source IDs, locators, page and
section context, evidence IDs, and semantic IDs. Search provides detailed
definitions, descriptions, and source passages; it does not establish
structured relationships without a corresponding Graph result.

## Open issue assessment

| Issue | Root cause | 0.2.3 action |
|---|---|---|
| #25 Data Agent `UnknownError`, empty/duplicate items | Failed LRO polling discarded operation URL, headers, request ID, and full body; failed creates could leave shells | Preserve structured LRO diagnostics and clean newly created failed targets |
| #26 Lakehouse ARRAY columns fail SQL endpoint sync | `None` projections retained native list columns even when scalar JSON alternatives existed | Use explicit scalar projections and reject nested Arrow fields before writes |
| #27 Graph validation errors masked; paths ambiguous; no compiled-artifact recovery | Readiness check ran before deployment errors were surfaced; path conventions were not validated; `deploy-graph` rebuilt only from Parquet | Surface original errors first, validate relative Lakehouse paths, and accept compiled graph artifacts |

## 0.2.3 release scope

### Required

- Resolve #25, #26, and #27 with regression tests.
- Publish this assessment and make it the roadmap authority for the next
  architecture increment.
- Align package, plugin, and API default versions at `0.2.3`.
- Reconcile README language with the approved-domain and sealed-semantic
  workflow.
- Pass the existing unit/contract suite and package build.
- Run a Fabric deployment dry run and inspect its generated plan. A live deploy
  requires explicit operator confirmation and valid environment credentials.

### Deferred after 0.2.3

- Provider-neutral discovery adapters enabled only by operator configuration
  after legal and license approval.
- RDF/OWL/SKOS import/export and SHACL validation.
- Domain-inference and ontology-reference schemas propagated through every
  canonical table and receipt.
- Production `retrieve_grounding` orchestration and unified citation schema.
- Ontology drift monitoring and reviewed migration planning.

## Release gates

1. No SQL-facing serving projection contains Arrow list, map, struct, or union
   fields.
2. Graph and Data Agent failures expose operation and request diagnostics.
3. Failed create/replace operations do not leave newly created Data Agent shells.
4. Graph definitions cannot combine a Lakehouse `referenceName` with an
   absolute `abfss://` path.
5. `deploy-graph` accepts the same compiled Graph definition used by
   `deploy-serving`.
6. Offline tests cover failure paths; live smoke evidence is recorded when a
   configured Fabric environment is available.
7. No third-party ontology URL or content is embedded in runtime code, default
   configuration, generated definitions, or release artifacts.

## Standards and primary references

- OWL 2 ontology and version IRIs:
  <https://www.w3.org/TR/owl2-syntax/#Ontology_IRI_and_Version_IRI>
- SKOS mapping properties:
  <https://www.w3.org/TR/skos-reference/#mapping>
- PROV-O:
  <https://www.w3.org/TR/prov-o/>
- DCAT 3:
  <https://www.w3.org/TR/vocab-dcat-3/>
- SHACL:
  <https://www.w3.org/TR/shacl/>
- Web Annotation Data Model:
  <https://www.w3.org/TR/annotation-model/>
- Microsoft GraphRAG local and DRIFT search:
  <https://microsoft.github.io/graphrag/query/local_search/>,
  <https://microsoft.github.io/graphrag/query/drift_search/>
- Azure AI Search agentic, hybrid, vector-filter, and `search.in` guidance:
  <https://learn.microsoft.com/azure/search/agentic-retrieval-overview>,
  <https://learn.microsoft.com/azure/search/hybrid-search-overview>,
  <https://learn.microsoft.com/azure/search/vector-search-filters>,
  <https://learn.microsoft.com/azure/search/search-query-odata-search-in-function>
