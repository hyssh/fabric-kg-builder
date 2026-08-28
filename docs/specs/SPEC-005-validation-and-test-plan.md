# SPEC-005 — Validation and Test Plan

**Status:** Draft  
**Date:** 2026-06-24T12:42:17.255-07:00  
**Author:** Hockney (Test Engineer)  
**References:** PRD §21 (Validation Requirements), §23 (MVP Acceptance Criteria), §24 (Sprint 1), §25 (Sprint 2)

### Revision history

| Rev | Date | Author | Summary |
|-----|------|--------|---------|
| 1 | 2026-06-24 | Hockney | Initial draft — VAL-001 through VAL-022, full test pyramid, Sprint 1 & 2 deliverables |
| 2 | 2026-06-24T11:46:10.517-07:00 | Hockney | Command rename `deploy-data` → `deploy-lakehouse` throughout; domain-intake tests with security assertion; graph→AI Search clue-chaining contract; Document Intelligence mocked tests; Foundry + .env config validation; VAL-023–VAL-028 added; mocking strategy updated to Microsoft Foundry SDK |
| 3 | 2026-06-24T12:42:17.255-07:00 | Hockney | Canonical-naming reconciliation (coordinator-canonical-naming.md): §12 graph→search fixture rewritten to `search.in()` + `vectorFilterMode: preFilter` + provenance select fields; BRG-001–BRG-010 (SPEC-003 §12 bridge gates) and D-31/D-32 (SPEC-002 §11 alias validations) registered in gate catalog and traceability; config keys updated to `AZURE_AI_FOUNDRY_ENDPOINT`/`AZURE_AI_FOUNDRY_API_KEY` (removing `AZURE_OPENAI_*`/`FOUNDRY_DEPLOYMENT_NAME`); commands aligned to `compile-search`, `deploy-lakehouse`, `enrich --domain-prompt`; VAL-023 reworded to chunk (text) and visual AI Search indexes only — no entity/relationship AI Search index exists |
| 4 | 2026-08-23 | Copilot | Added schema-2.0 DOM, EXT, SEM, DEP, and QRY gates and compatibility test requirements. |
| 5 | 2026-08-24 | Copilot | Added the C0.Core contract gate and corrected shared-contract ownership references to SPEC-006; no higher-layer feature gate is activated. |
| 6 | 2026-08-25 | Copilot | Activated the L1 domain-design/approval test gate from SPEC-007; schema-2 downstream behavior remains fail-closed. |
| 7 | 2026-08-25 | Copilot | Added the isolated L2 schema-constrained extraction test gate; L3 evidence validation, serving, publication, and schema-2 CLI activation remain excluded. |
| 8 | 2026-08-25 | Copilot | Added the isolated L3 evidence-validation test gate (§15.4), activated EXT-102/EXT-103, and registered EXT-109–EXT-112; serving, publication, and schema-2 CLI activation remain excluded. |
| 9 | 2026-08-25 | Copilot | Hardened the L3 gate for fail-closed carrier limits (direction, property owner/value, identity witness, design-evidence counts), leaf-checkpoint integrity, lifecycle cross-checking, collection-carrier partitioning, and fingerprint-scoped rerun; registered EXT-113 and EXT-114. |
| 10 | 2026-08-25 | Copilot | Required exact collection-policy binding to the sealed `CompletenessRequirementV2`, full leaf re-derivation for every candidate kind against interrupted-run forgery, and leaf fingerprints bound to the complete canonical `SourceUnit` artifact; registered EXT-115 and EXT-116. |
| 11 | 2026-08-26 | Copilot | Added the isolated local L4 audit and asserted-only serving projection gate (§15.5); L5 deployment/publication, runtime, and schema-2 CLI activation remain excluded. |
| 12 | 2026-08-25 | Copilot | Added the isolated L5a structured publication gate (§15.6) with persisted target definitions, bounded fake-client lifecycle, exact read-back equivalence, corruption-safe reuse, and no Search/L6/L7 activation. |
| 13 | 2026-08-26 | Copilot | Added the L5b Runtime 1.1 and L6 bounded agent-integration gates (§15.7–15.8), including complete/partial/abstain, exact citations, call bounds, config read-back, and RDF inactivity. |

---

## 1. Scope and Purpose

This specification defines the complete validation and test strategy for `fabric-kg-builder`. It is derived exclusively from `docs/PRD.md` and is the traceability backbone that links every acceptance criterion, validation rule, and pipeline stage to a concrete, automated test.

### What this covers

- Every validation rule from PRD §21, turned into a precise, testable gate
- The test pyramid (unit → contract → integration → smoke)
- Full traceability from each PRD §23 acceptance criterion to a test case
- Sprint 1 (PRD §24) and Sprint 2 (PRD §25) test deliverables
- An end-to-end trace test for entity → relationship → evidence (PRD §23.13)
- Fixture locations, mocking strategy, tooling layout, and CI gate policy
- Edge cases and failure mode coverage

### What this does not cover

- UI testing (no UI in first release — PRD §4.1)
- Production performance or load testing
- Azure AI Search deployment in CI (optional retrieval layer — PRD §6)

---

## 2. Validation Gate Catalog (PRD §21)

Every item in PRD §21 is mapped to a precise rule. Rules with severity `build-fail` block the pipeline. Rules with severity `warn` emit a warning but allow continuation.

| Rule ID | Description | Pipeline Stage | Severity | Exact Condition Checked | Error Message Contract |
|---------|-------------|----------------|----------|------------------------|------------------------|
| VAL-001 | Entity IDs must not be missing | `compile-data` (post-enrich) | build-fail | Any row in `entities.parquet` where `entity_id` is null or empty string | `[VAL-001] Entity at row {n} has missing entity_id` |
| VAL-002 | Entity IDs must not be duplicated | `compile-data` | build-fail | `entities.parquet` contains duplicate `entity_id` values | `[VAL-002] Duplicate entity_id: {id} appears {count} times` |
| VAL-003 | Relationship source entity ID must exist | `compile-data` | build-fail | Any `source_entity_id` in `relationships.parquet` not present in `entities.parquet`.`entity_id` | `[VAL-003] Relationship {rel_id}: source_entity_id {id} not found in entities` |
| VAL-004 | Relationship target entity ID must exist | `compile-data` | build-fail | Any `target_entity_id` in `relationships.parquet` not present in `entities.parquet`.`entity_id` | `[VAL-004] Relationship {rel_id}: target_entity_id {id} not found in entities` |
| VAL-005 | Evidence IDs must not be missing for extracted facts | `compile-data` | build-fail | Any row in `evidence.parquet` where `evidence_id` is null or empty | `[VAL-005] Evidence row {n} has missing evidence_id` |
| VAL-006 | Chunk IDs must not be missing | `compile-data` | build-fail | Any row in `chunks.parquet` where `chunk_id` is null or empty | `[VAL-006] Chunk at row {n} has missing chunk_id` |
| VAL-007 | Chunk IDs must not be duplicated | `compile-data` | build-fail | `chunks.parquet` contains duplicate `chunk_id` values | `[VAL-007] Duplicate chunk_id: {id} appears {count} times` |
| VAL-008 | Document elements referenced by chunks must exist | `compile-data` | build-fail | Any `document_element_id` in `chunks.parquet` not present in `document_elements.parquet`.`document_element_id` | `[VAL-008] Chunk {chunk_id}: document_element_id {id} not found in document_elements` |
| VAL-009 | Visual assets referenced by chunks/evidence must exist | `compile-data` | build-fail | Any `image_id` in `evidence.parquet` or `visual_regions.parquet` not present in `visual_assets.parquet`.`image_id` | `[VAL-009] Reference to missing visual asset: {image_id}` |
| VAL-010 | Visual assets must have blob_url after upload | `blob-upload` (post-upload) | build-fail | Any row in `visual_assets.parquet` where `blob_url` is null or empty after the blob upload stage | `[VAL-010] Visual asset {image_id} has no blob_url after upload` |
| VAL-011 | Visual regions must reference existing image IDs | `compile-data` | build-fail | Any `image_id` in `visual_regions.parquet` not present in `visual_assets.parquet`.`image_id` | `[VAL-011] VisualRegion {visual_region_id}: image_id {id} not found in visual_assets` |
| VAL-012 | Callouts must reference existing visual regions | `compile-data` | build-fail | Any `callout_id` in `evidence.parquet` not present in `visual_regions.parquet`.`visual_region_id` when `source_type = figure_callout` | `[VAL-012] Evidence {evidence_id}: callout_id {id} not found in visual_regions` |
| VAL-013 | AI Search documents must not reference missing blob URLs | `compile-search` | build-fail | Any AI Search index document where `blob_url` is non-null but the corresponding `visual_assets.parquet` row has null `blob_url` | `[VAL-013] Search document {id}: blob_url references asset {image_id} which has no uploaded URL` |
| VAL-014 | Ontology visual/image nodes must have blob_url property defined | `compile-ontology` | build-fail | Any ontology entity type in `ImageAsset`, `Figure`, or `VisualRegion` category that lacks a `blob_url` property in the compiled definition | `[VAL-014] Ontology entity type {type_name} is missing required blob_url property` |
| VAL-015 | Fabric ontology type IDs must not be missing | `compile-ontology` | build-fail | Any entity type or relationship type in `ids.lock.json` with a null or empty ID value | `[VAL-015] ids.lock.json: {type_name} has missing or empty ID` |
| VAL-016 | Fabric ontology type IDs must not be duplicated | `compile-ontology` | build-fail | `ids.lock.json` contains duplicate numeric ID values across `entityTypes` or `relationshipTypes` | `[VAL-016] ids.lock.json: ID {id} is assigned to both {name_a} and {name_b}` |
| VAL-017 | Relationship types must reference known entity types | `compile-ontology` | build-fail | Any relationship type in `ontology/model.yaml` whose `source_type` or `target_type` does not correspond to a known entity type in the same model | `[VAL-017] RelationshipType {rel_name}: {source_or_target}_type {type_name} is not defined` |
| VAL-018 | Parquet schema must match ontology binding expectations | `compile-data` | build-fail | For each Parquet table with a declared binding in `ontology/model.yaml`, every required column defined in the binding must be present in the actual Parquet schema | `[VAL-018] Parquet table {table}: missing column(s) {cols} required by ontology binding` |
| VAL-019 | AI Search schema must match generated index documents | `compile-search` | build-fail | For each field declared in the AI Search index schema, that field must exist in every generated index document; no document may contain a field not declared in the schema | `[VAL-019] Search index {index}: document {doc_id} has field mismatch — extra: {extra}, missing: {missing}` |
| VAL-020 | LLM output must conform to the declared JSON schema | `enrich` | build-fail | The LLM response JSON, when validated against the LLM output JSON schema (see §3 contract layer), produces zero schema violations | `[VAL-020] LLM output for {source_file}: schema violation at {path} — {message}` |
| VAL-021 | Required placeholder files must be present | `package` | build-fail | All placeholder files listed in PRD §18 that are implied by the current source types must exist under `build/` before packaging | `[VAL-021] Required placeholder missing: {path}` |
| VAL-022 | Dev/test/prod artifacts must not drift unexpectedly | `validate --env` | warn | Artifact file list and schema fingerprints produced in test env differ from those produced in dev env beyond allowed delta | `[VAL-022] Environment drift detected in {env}: {count} artifact(s) differ from dev baseline` |
| VAL-023 | `deploy-lakehouse` must not write structured Parquet rows into AI Search chunk or visual indexes | `deploy-lakehouse` | build-fail | After `deploy-lakehouse` completes, the chunk (text) AI Search index and the visual AI Search index must contain no documents derived from structured entity or relationship Parquet rows; those tables land in the Fabric Lakehouse only — there is no entity/relationship AI Search index | `[VAL-023] deploy-lakehouse: structured Parquet row detected in AI Search chunk/visual index {index} — entity and relationship tables must be deployed to the Lakehouse, not AI Search` |
| VAL-024 | Domain prompt text must be injected into LLM USER role only | `enrich` (domain-intake) | build-fail | In every LLM call that includes a domain brief, the domain text must appear in a `role: user` message and must never appear in any `role: system` message | `[VAL-024] Domain text detected in LLM system prompt at call {call_id} — must be in user role only` |
| VAL-025 | Required environment variables must be present at startup | CLI startup | build-fail | Each secret listed in the required-secrets catalog (`AZURE_AI_FOUNDRY_ENDPOINT`, `AZURE_AI_FOUNDRY_API_KEY`, `FABRIC_WORKSPACE_ID`, `AZURE_BLOB_CONNECTION_STRING`) must be non-empty before any pipeline stage runs | `[VAL-025] Required env var {var_name} is missing or empty — set it in .env or the environment before running` |
| VAL-026 | No secret values may appear in committed YAML/JSON config files | `validate` / pre-commit | build-fail | Scanning `fabric-kg.yaml` and `ontology/environments/*.json` must find zero string values matching known secret patterns (API keys, bearer tokens, connection strings) | `[VAL-026] Possible secret value in config file {path} at key {key} — use ${ENV_VAR} interpolation instead` |
| VAL-027 | Foundry chat deployment config must resolve | CLI startup | build-fail | The `enrichment.chat_deployment` value from `fabric-kg.yaml` (authenticated via `AZURE_AI_FOUNDRY_ENDPOINT` and `AZURE_AI_FOUNDRY_API_KEY`) must match an accessible deployment in the configured Foundry project; resolution is checked at startup via a lightweight list-deployments call (mocked in tests) | `[VAL-027] Foundry deployment '{name}' does not resolve — check enrichment.chat_deployment in fabric-kg.yaml and AZURE_AI_FOUNDRY_* credentials` |
| VAL-028 | Visual region polygon_json must be populated when Document Intelligence source is used | `compile-data` | build-fail | When a `visual_region` row carries `source_type = document_intelligence`, the `polygon_json` column must be non-null and must be valid JSON representing at least one polygon point | `[VAL-028] VisualRegion {visual_region_id}: polygon_json is null or invalid for Document Intelligence source` |

**Ontology Bridge Gates (SPEC-003 §12.9)** — enforced by `compile-ontology` and `fabric-kg validate`:

| Rule ID | Description | Pipeline Stage | Severity | Exact Condition Checked | Error Message Contract |
|---------|-------------|----------------|----------|------------------------|------------------------|
| BRG-001 | `DocumentChunk` ontology type declares all required bridge properties with correct types | `compile-ontology` | build-fail | `DocumentChunk` type definition is missing any of: `entity_id`, `chunk_id`, `related_entity_ids`, `entity_search_keys`; or any of these has wrong type | `[BRG-001] DocumentChunk missing required bridge property: {prop}` |
| BRG-002 | `SearchIndexRecord` declares `search_record_id`; `canonical_key` and `entity_search_keys` are compile-time derived (not raw column bindings) | `compile-ontology` | build-fail | `SearchIndexRecord` lacks `search_record_id`, or `canonical_key`/`entity_search_keys` are declared as raw Parquet column bindings instead of index-build-time derived fields | `[BRG-002] SearchIndexRecord: {prop} must be compile-time derived, not a raw column binding` |
| BRG-003 | Every entity type in the `support-domain` module declares `entity_id`, `canonical_key`, and `search_aliases` | `compile-ontology` | build-fail | Any support-domain entity type definition missing one or more of these three required properties | `[BRG-003] Support-domain entity type {type}: missing required property {prop}` |
| BRG-004 | `ImageAsset` and `Figure` declare `blob_url` property with format `uri` | `compile-ontology` | build-fail | `ImageAsset` or `Figure` type definition lacks a `blob_url` property or the property is missing `format: uri` | `[BRG-004] Ontology type {type}: missing required blob_url property (format uri)` |
| BRG-005 | `evidenced_by`, `shown_in`, `indexed_as` relationship types exist in `model.yaml` with `inversePolicy` set | `compile-ontology` | build-fail | Any of the three bridge relationship types is absent from `model.yaml` or lacks an `inversePolicy` declaration | `[BRG-005] Bridge relationship type {rel}: missing from model.yaml or lacks inversePolicy` |
| BRG-006 | All `indexed_as` edges resolve: every referenced `SearchIndexRecord` node exists with `search_record_id` populated | `compile-ontology` / `validate` | build-fail | Any `indexed_as` edge references a `SearchIndexRecord` node that does not exist or has null `search_record_id` | `[BRG-006] indexed_as edge: SearchIndexRecord {id} not found or missing search_record_id` |
| BRG-007 | All `evidenced_by` edges resolve: every referenced `DocumentChunk` node exists with `chunk_id` populated | `compile-ontology` / `validate` | build-fail | Any `evidenced_by` edge references a `DocumentChunk` node that does not exist or has null `chunk_id` | `[BRG-007] evidenced_by edge: DocumentChunk {id} not found or missing chunk_id` |
| BRG-008 | All `shown_in` edges resolve: every referenced `Figure`/`ImageAsset` node has a non-empty `blob_url` | `compile-ontology` / `validate` | build-fail | Any `shown_in` edge references a `Figure` or `ImageAsset` node with null or empty `blob_url` | `[BRG-008] shown_in edge: {type} {id} has empty or null blob_url` |
| BRG-009 | Support-domain entities with no outbound `evidenced_by` or `shown_in` edges | `compile-ontology` / `validate` | warn | Any support-domain entity node has zero outbound `evidenced_by` edges and zero outbound `shown_in` edges — provenance may be incomplete | `[BRG-009] WARN: entity {id} ({type}) has no evidenced_by or shown_in edges — provenance may be incomplete` |
| BRG-010 | `entity_id` on any support-domain node is not empty and not duplicated within its entity type | `compile-ontology` / `validate` | build-fail | Any support-domain node has null/empty `entity_id`, or two nodes of the same entity type share the same `entity_id` | `[BRG-010] entity_id empty or duplicate for {type} node: {entity_id}` |

**Graph-to-Search Alias Gates (SPEC-002 §11.10)** — enforced by `compile-data`:

| Rule ID | Description | Pipeline Stage | Severity | Exact Condition Checked | Error Message Contract |
|---------|-------------|----------------|----------|------------------------|------------------------|
| D-31 | Chunks linked to entities must have `entity_search_keys` populated | `compile-data` | warn | Any row in `chunks.parquet` where `related_entity_ids` is non-null but `entity_search_keys` is null — alias keyword boost will be degraded in AI Search | `[D-31] WARN: chunk {chunk_id} has related_entity_ids but null entity_search_keys — alias keyword boost will be degraded` |
| D-32 | Non-placeholder entities must have `search_aliases` populated | `compile-data` | warn | Any row in `entities.parquet` where `is_placeholder = False` but `search_aliases` is null — AI Search alias coverage will be incomplete | `[D-32] WARN: entity {entity_id} has is_placeholder=False but null search_aliases — AI Search alias coverage will be incomplete` |

---

## 3. Test Pyramid

### Layer definitions

```
                    ┌───────────────────┐
                    │    SMOKE TESTS    │  post-deploy, live env
                    ├───────────────────┤
                    │ INTEGRATION TESTS │  full pipeline, local
                    ├───────────────────┤
                    │  CONTRACT TESTS   │  schema conformance
                    ├───────────────────┤
                    │    UNIT TESTS     │  functions, pure logic
                    └───────────────────┘
```

### Unit tests

These test pure functions with no I/O. They must run in milliseconds with no network, no Blob Storage, no OpenAI, and no Fabric connections.

| Scope | What is tested | Location |
|-------|----------------|----------|
| Schema definitions | Each canonical Parquet schema has all required columns, correct types, and no unexpected nullability | `tests/unit/test_schemas.py` |
| ID generation | `ids.lock.json` loader produces correct integer IDs, raises on missing keys, raises on duplicate IDs | `tests/unit/test_ids.py` |
| Validators (VAL-001 through VAL-022) | Each validator function accepts valid input without error and raises the correct exception with the correct message on invalid input | `tests/unit/test_validators.py` |
| Ontology compiler (parts) | Each ontology model YAML is parsed into correct in-memory structure; entity types, relationship types, and properties are correctly populated | `tests/unit/test_ontology_compiler.py` |
| CSV loader | CSV rows are loaded into the expected intermediate dict structure; column mapping is applied; empty values produce None not empty string | `tests/unit/test_csv_loader.py` |
| Chunk ID generation | Chunk IDs are deterministic for the same (source_file_id, element_id, index) input and differ for distinct inputs | `tests/unit/test_chunk_ids.py` |
| Content hash | `content_hash` is stable for the same content and differs on any byte change | `tests/unit/test_content_hash.py` |
| Placeholder generator | Each expected placeholder path is produced for a minimal source list | `tests/unit/test_placeholders.py` |

### Contract tests

These verify that data produced by one component satisfies the schema expected by the next consumer. They use fixture data only — no live services.

| Contract | What is verified | Location |
|----------|-----------------|----------|
| LLM output JSON schema | A sample LLM response fixture validates successfully against the JSON schema; a broken fixture (missing `entities`, wrong type, extra fields) fails validation with the correct error | `tests/contract/test_llm_output_schema.py` |
| Parquet schema vs ontology binding | For each Parquet table fixture, all columns declared in the ontology binding are present with the correct dtype; the binding validator accepts the table and rejects a table missing a required column | `tests/contract/test_parquet_binding.py` |
| AI Search schema vs index documents | For each generated search index document fixture, all fields exist in the declared index schema and no undeclared fields are present | `tests/contract/test_search_index_schema.py` |
| Blob URL presence in cross-table references | `visual_assets`, `evidence`, `chunks`, and `document_elements` fixtures that reference visual content all carry non-null `blob_url` | `tests/contract/test_blob_url_references.py` |

### Integration tests

These run the full pipeline locally against fixture inputs, writing real Parquet output to a temp directory. No live cloud services. OpenAI is mocked. Blob upload is mocked.

| Scenario | What is verified | Location |
|----------|-----------------|----------|
| CSV → enriched → Parquet | A sample CSV flows through `inspect-source`, `enrich` (mocked Foundry LLM), `compile-data`, producing all eight Parquet tables with correct schemas and row counts | `tests/integration/test_csv_pipeline.py` |
| Document → chunks → Parquet (Sprint 2) | A tiny sample DOCX/HTML flows through document extraction, chunk creation, visual asset extraction (with mocked blob upload and mocked Document Intelligence), producing `document_elements.parquet`, `chunks.parquet`, `visual_assets.parquet`, `visual_regions.parquet` | `tests/integration/test_document_pipeline.py` |
| Ontology compiler end-to-end | `compile-ontology` runs against `ontology/model.yaml` and `ids.lock.json` and produces a structurally valid ontology package directory matching the Fabric definition structure | `tests/integration/test_ontology_compile.py` |
| Validation gate end-to-end | Feeding a deliberately broken fixture (duplicate entity ID, missing blob_url, dangling FK) to `compile-data` causes the CLI to exit non-zero and emit each expected VAL-XXX error message | `tests/integration/test_validation_gates.py` |
| Lakehouse vs AI Search separation | `deploy-lakehouse` (mocked Fabric REST) writes Parquet tables to the Lakehouse mock and the AI Search mock (chunk and visual indexes only) receives zero structured entity/relationship Parquet rows (VAL-023) | `tests/integration/test_deploy_lakehouse.py` |

### Smoke tests

These run post-deploy against a real dev environment. They are not part of the merge-blocking CI suite; they run in the deploy pipeline only.

| Check | What is verified | Command |
|-------|-----------------|---------|
| Ontology exists in Fabric | The Fabric Ontology item is reachable via REST and returns HTTP 200 | `fabric-kg validate --env dev --check ontology-exists` |
| Data bindings resolve | Each ontology binding in the dev workspace returns a non-empty result set for its Parquet table | `fabric-kg validate --env dev --check bindings` |
| Blob URLs reachable | A sample of `blob_url` values from `visual_assets.parquet` return HTTP 200 (anonymous or with SAS token) | `fabric-kg validate --env dev --check blob-urls` |
| Sample query returns results | A KQL or Fabric query for at least one entity type returns at least one row | `fabric-kg validate --env dev --check sample-query` |
| Lakehouse data present in OneLake | After `fabric-kg deploy-lakehouse --env dev`, each of the eight Parquet tables is readable from the Fabric Lakehouse via OneLake REST; row counts are non-zero | `fabric-kg validate --env dev --check lakehouse-data` |
| Structured data absent from AI Search | After `fabric-kg deploy-lakehouse --env dev`, the chunk (text) and visual AI Search indexes contain no documents whose `_schema_source` equals `parquet` (i.e., no structured entity/relationship Parquet rows leaked into AI Search — there is no entity/relationship AI Search index) | `fabric-kg validate --env dev --check search-no-parquet` |

---

## 4. MVP Acceptance Test Matrix (PRD §23)

Each of the 13 PRD §23 acceptance criteria maps to a concrete test case. The command is the CLI invocation that exercises it. The pass condition is observable and automated.

| AC# | PRD §23 Criterion | Test Case ID | Given | When | Then | Command | Pass Condition |
|-----|-------------------|--------------|-------|------|------|---------|----------------|
| AC-01 | User can run `fabric-kg init` | TC-AC-01 | Clean project directory with no existing `build/` or `dist/` | `fabric-kg init` is run | Exit code 0; `ontology/model.yaml`, `ontology/ids.lock.json`, and `ontology/environments/dev.json` exist | `fabric-kg init` | Exit 0, all three config files created |
| AC-02 | Sample CSV can be inspected | TC-AC-02 | `examples/csv/sample.csv` exists with at least two data rows | `fabric-kg inspect-source --input examples/csv/sample.csv` | Exit code 0; printed schema profile lists all column names and inferred types; no error in output | `fabric-kg inspect-source --input examples/csv/sample.csv` | Exit 0, output contains column names from sample CSV |
| AC-03 | CSV can be enriched into canonical JSON | TC-AC-03 | `examples/csv/sample.csv` exists; OPENAI_API_KEY is set to a mocked value; LLM client is patched to return the deterministic fixture `tests/fixtures/llm/sample_enrichment.json` | `fabric-kg enrich --input examples/csv/sample.csv --out build/enriched` | Exit code 0; `build/enriched/entities.json`, `build/enriched/relationships.json`, and `build/enriched/evidence.json` exist and are valid against the LLM output JSON schema | `fabric-kg enrich --input examples/csv/sample.csv --out build/enriched` | Exit 0, three JSON files exist, each validates against LLM schema |
| AC-04 | Sample document produces text, chunk, table, and visual asset records | TC-AC-04 | `examples/docs/sample.docx` (or HTML) exists with at least one table and one inline image; blob upload is mocked | `fabric-kg enrich --input examples/docs/sample.docx --out build/enriched` | Exit code 0; enriched output contains non-empty `document_elements`, `chunks` (including a `table_html` chunk), and `visual_assets` lists | `fabric-kg enrich --input examples/docs/sample.docx --out build/enriched` | Exit 0, each list has ≥1 item, table_html chunk present |
| AC-05 | Extracted images/figures are uploaded to Blob Storage | TC-AC-05 | Document with inline image processed; blob upload client is mocked with a fake URL generator | After `enrich` stage completes | `visual_assets.parquet` rows for image-type assets have non-null `blob_url` matching the mocked URL pattern | `fabric-kg compile-data --input build/enriched --out build/parquet` | All `visual_assets` rows with `asset_type in [figure, inline_image, diagram]` have non-null `blob_url` |
| AC-06 | Blob URLs appear in all required locations | TC-AC-06 | Parquet tables produced by TC-AC-05 | Read all six targets defined in PRD §23.6 | `blob_url` is non-null in `visual_assets.parquet`, `document_elements.parquet` (image rows), `chunks.parquet` (image_description chunks), `evidence.parquet` (visual evidence), AI Search documents (if generated), and ontology image node property definitions | Assertion over Parquet files + optional search document JSON | All six locations contain the same blob URL for the same image asset |
| AC-07 | Canonical JSON written to all eight Parquet tables | TC-AC-07 | Valid enriched JSON from TC-AC-03 or TC-AC-04 | `fabric-kg compile-data --input build/enriched --out build/parquet` | All eight Parquet files exist; each validates against its schema; row counts match enriched JSON counts | `fabric-kg compile-data --input build/enriched --out build/parquet` | Exit 0; eight files present; zero schema violations from VAL-018 |
| AC-08 | Minimal Fabric Ontology definition compiled from model.yaml | TC-AC-08 | `ontology/model.yaml` with at least one entity type and one relationship type; `ontology/ids.lock.json` with corresponding IDs | `fabric-kg compile-ontology --out build/ontology` | Exit code 0; `build/ontology/definition.json` exists; at least one `EntityTypes/{ID}/definition.json` exists; structure matches Fabric definition format from PRD §16 | `fabric-kg compile-ontology --out build/ontology` | Exit 0, required files present, JSON is valid |
| AC-09 | Ontology package uses deterministic IDs from ids.lock.json | TC-AC-09 | Same `ontology/model.yaml` and `ids.lock.json`; run `compile-ontology` twice | Compare the two output packages | The numeric IDs in each `EntityTypes/{ID}/` directory name and each `definition.json` are byte-for-byte identical between the two runs | Run `fabric-kg compile-ontology` twice; diff outputs | Zero diff between the two output trees |
| AC-10 | AI Search index documents generated for chunks, table HTML, image descriptions | TC-AC-10 | `build/parquet/chunks.parquet` with rows of types `table_html` and `image_description`; AI Search feature enabled | `fabric-kg compile-search --out build/search` | Exit code 0; `build/search/kg-chunks/documents/` contains JSON documents for each chunk; `table_html` documents have `content_html` populated; `image_description` documents have `blob_url` populated | `fabric-kg compile-search --out build/search` | Exit 0; documents exist; table/image fields populated; schema validates (VAL-019) |
| AC-11 | Package is deployable to Fabric dev workspace | TC-AC-11 | All eight Parquet tables present; ontology package compiled; Fabric client mocked or real dev workspace available | `fabric-kg deploy-lakehouse --env dev && fabric-kg deploy-ontology --env dev` | Exit code 0 for both commands; no error from Fabric REST mock/client; Lakehouse tables verified present (VAL-023 passes — no structured rows in AI Search) | `fabric-kg deploy-lakehouse --env dev` then `fabric-kg deploy-ontology --env dev` | Both exit 0 (mocked in unit/integration; real in smoke); VAL-023 check passes |
| AC-12 | CLI can validate generated data, search artifacts, and ontology package | TC-AC-12 | All Parquet tables, ontology package, and optional search artifacts present | `fabric-kg validate --env dev` | Exit code 0; no VAL-XXX errors emitted; output lists each passing rule | `fabric-kg validate --env dev` | Exit 0; output contains "PASS" for each applicable VAL rule |
| AC-13 | At least one entity → relationship → evidence end-to-end trace | TC-AC-13 | Fixture: single entity row, single relationship row pointing to that entity, single evidence row pointing to that relationship, with matching IDs | Assert that joining the three Parquet tables on their FK columns produces a single complete row | The trace row contains: `entity_id`, `display_name`, `relationship_type`, `evidence_id`, `source_type`, `text` (or `blob_url` for visual evidence) | Integration assertion in `tests/integration/test_e2e_trace.py` | Trace produces exactly one complete row; no null FK values |

---

## 5. Sprint 1 Test Deliverables (PRD §24)

Sprint 1 establishes the CSV-first pipeline foundation. The following tests must exist and pass before Sprint 1 is considered done.

| Deliverable | Test File | What Is Tested | Pass Condition |
|-------------|-----------|----------------|----------------|
| Repo skeleton | `tests/unit/test_project_structure.py` | All required top-level directories (`src/`, `tests/`, `ontology/`, `examples/csv/`, `build/` gitignored) and required files (`ontology/model.yaml`, `ontology/ids.lock.json`) exist | All path assertions pass |
| CLI entry point | `tests/unit/test_cli_entry.py` | `fabric-kg --help` exits 0 and prints each command name; `fabric-kg unknown-command` exits non-zero | Exit codes and output strings match |
| CSV loader | `tests/unit/test_csv_loader.py` | Valid CSV produces list of dicts with correct keys; malformed CSV raises `CsvLoadError`; empty file raises `CsvLoadError`; UTF-8 BOM handled; trailing whitespace stripped from values | All assertions pass |
| Canonical schemas | `tests/unit/test_schemas.py` | Each of the eight Parquet schemas has the exact columns defined in PRD §12; no extra or missing columns; dtypes match spec | Zero schema deviations |
| Placeholder schemas (document_elements, chunks, visual_assets, visual_regions) | `tests/unit/test_placeholder_schemas.py` | Placeholder Parquet files can be written and read back; schema matches spec; row count is 0 | Write + read round-trip succeeds; 0 rows; correct schema |
| Parquet writer | `tests/unit/test_parquet_writer.py` | Writing a list of dicts to a named Parquet table produces a file readable by pandas/pyarrow; column types match schema; re-read row count matches input count | Round-trip produces identical values |
| Ontology compiler skeleton | `tests/unit/test_ontology_compiler.py` | `compile-ontology` with minimal `model.yaml` produces `definition.json` and at least one `EntityTypes/` directory; output is valid JSON; IDs match `ids.lock.json` | Files exist; JSON valid; IDs match lock |
| ids.lock.json validation | `tests/unit/test_ids.py` | Valid `ids.lock.json` loads without error; missing key raises `IdsLockError`; duplicate numeric ID value raises `IdsLockError`; IDs that contain non-digit characters raise `IdsLockError` | All error cases raise correct exceptions |
| Validation gates (Sprint 1 rules) | `tests/unit/test_validators.py` | VAL-001 through VAL-007, VAL-015, VAL-016, VAL-017, VAL-018, VAL-021, VAL-025, VAL-026, VAL-027 each raise the correct exception with the correct message on the minimal failing fixture; each passes on the minimal valid fixture | 28 validator tests pass (one per rule, valid + invalid case) |

---

## 6. Sprint 2 Test Deliverables (PRD §25)

Sprint 2 adds document/chunk/image extraction. The following tests must exist and pass before Sprint 2 is considered done.

| Deliverable | Test File | What Is Tested | Pass Condition |
|-------------|-----------|----------------|----------------|
| Section extraction from DOCX/HTML | `tests/unit/test_document_extractor.py` | A tiny sample DOCX with two headings and two paragraphs produces two `section` document element records; `parent_element_id` chain is correct; `section_path` is populated | Correct element count and structure |
| Traditional text chunk creation | `tests/unit/test_chunker.py` | A paragraph of known length produces chunks of expected count with correct `chunk_type = section_text`; `document_element_id` FK is set on each chunk | Chunk count and FK correctness |
| Structured table extraction | `tests/unit/test_table_extractor.py` | A DOCX table with 2 rows × 3 columns produces 6 `TableCell`-equivalent records; column headers are detected; `table_id` FK is consistent | 6 records; headers present; FK set |
| Table → HTML chunk | `tests/unit/test_table_chunker.py` | A structured table produces a `table_html` chunk whose `content_html` is valid HTML containing a `<table>` element with correct `<th>` and `<td>` values | Valid HTML; values match source table |
| Blob upload (mocked) | `tests/unit/test_blob_uploader.py` | Mocked Azure Blob client receives the correct container name, blob name, and binary content; returned URL is stored in `blob_url`; duplicate upload for same `image_hash` is skipped | Mock called with correct args; URL returned; dedup honored |
| visual_assets generation | `tests/unit/test_visual_assets.py` | Processing a DOCX with one inline image produces one `visual_asset` record with `asset_type = inline_image`; `blob_url` is populated from mocked uploader; `image_hash` is non-null | 1 record; all required fields populated |
| visual_regions generation | `tests/unit/test_visual_regions.py` | An image enrichment fixture containing one callout produces one `visual_region` record with `region_type = callout`; `image_id` FK matches parent `visual_asset` | 1 record; FK correct |
| Evidence linking (text) | `tests/unit/test_evidence_linking.py` | An enriched entity with `evidence_id` referencing a chunk produces one `evidence` row with `source_type = chunk`; `chunk_id` FK is set and matches `chunks.parquet` | 1 evidence row; FK resolves |
| Evidence linking (visual) | `tests/unit/test_evidence_linking.py` | An enriched relationship with `evidence_id` referencing a figure callout produces one `evidence` row with `source_type = figure_callout`; `blob_url` is non-null; `visual_region_id` FK is set | 1 evidence row; blob_url present; FK resolves |
| VAL-008 through VAL-014, VAL-023, VAL-024, VAL-028 (Sprint 2 rules) | `tests/unit/test_validators.py` (extended) | VAL-008, VAL-009, VAL-010, VAL-011, VAL-012, VAL-013, VAL-014, VAL-023, VAL-024, VAL-028 each raise the correct exception with the correct message on the minimal failing fixture | 10 additional validator tests pass |

---

## 7. End-to-End Traceability Test (PRD §23.13)

This test verifies that a single entity, its relationship to another entity, and the visual evidence that grounds the relationship can be traced end-to-end through the Parquet tables.

### Fixture definition

**File:** `tests/fixtures/e2e_trace/`

```
tests/fixtures/e2e_trace/
  entities.json           # two entity rows
  relationships.json      # one relationship row
  visual_assets.json      # one visual asset row
  visual_regions.json     # one visual region row (callout)
  evidence.json           # one evidence row linking relationship to callout
  llm_response.json       # deterministic LLM response producing the above
```

**Fixture data (minimal, deterministic):**

```json
// entities.json
[
  {
    "entity_id": "e2e:device:surface-laptop-5",
    "entity_type": "Device",
    "display_name": "Surface Laptop 5",
    "canonical_key": "surface-laptop-5",
    "aliases": "[]",
    "description": "15-inch laptop",
    "source_file_id": "sf:e2e:sample.csv",
    "confidence": 1.0
  },
  {
    "entity_id": "e2e:component:battery",
    "entity_type": "Component",
    "display_name": "Battery",
    "canonical_key": "surface-laptop-5:battery",
    "aliases": "[\"Battery pack\"]",
    "description": "Rechargeable internal battery",
    "source_file_id": "sf:e2e:sample.csv",
    "confidence": 0.91
  }
]

// relationships.json
[
  {
    "relationship_id": "e2e:rel:has_component:battery",
    "relationship_type": "has_component",
    "source_entity_id": "e2e:device:surface-laptop-5",
    "target_entity_id": "e2e:component:battery",
    "evidence_id": "e2e:ev:figure12:calloutB",
    "confidence": 0.88
  }
]

// visual_assets.json
[
  {
    "image_id": "e2e:va:figure12",
    "source_file_id": "sf:e2e:sample.docx",
    "document_element_id": "de:e2e:figure12",
    "asset_type": "diagram",
    "page_number": 42,
    "caption": "Battery connector location",
    "blob_url": "https://fake.blob.core.windows.net/kg-assets/e2e/figure12.png",
    "image_hash": "abc123",
    "description": "Diagram showing the battery connector and surrounding screws.",
    "confidence": 0.95
  }
]

// visual_regions.json
[
  {
    "visual_region_id": "e2e:vr:figure12:calloutB",
    "image_id": "e2e:va:figure12",
    "region_type": "callout",
    "label": "B",
    "text": "Battery connector",
    "identified_entity_id": "e2e:component:battery",
    "blob_url": "https://fake.blob.core.windows.net/kg-assets/e2e/figure12.png",
    "confidence": 0.90
  }
]

// evidence.json
[
  {
    "evidence_id": "e2e:ev:figure12:calloutB",
    "source_file_id": "sf:e2e:sample.docx",
    "source_type": "figure_callout",
    "document_element_id": "de:e2e:figure12",
    "chunk_id": null,
    "page_number": 42,
    "figure_id": "e2e:va:figure12",
    "visual_region_id": "e2e:vr:figure12:calloutB",
    "callout_id": "e2e:vr:figure12:calloutB",
    "blob_url": "https://fake.blob.core.windows.net/kg-assets/e2e/figure12.png",
    "text": "Callout B identifies the battery connector."
  }
]
```

### Test assertions

**File:** `tests/integration/test_e2e_trace.py`

```python
def test_entity_relationship_visual_evidence_trace(parquet_tables):
    """
    Trace: Surface Laptop 5 --has_component--> Battery
           evidenced by figure12 callout B (visual)
    """
    entities = parquet_tables["entities"]
    relationships = parquet_tables["relationships"]
    visual_assets = parquet_tables["visual_assets"]
    visual_regions = parquet_tables["visual_regions"]
    evidence = parquet_tables["evidence"]

    # Step 1: find the relationship
    rel = relationships[
        relationships["relationship_type"] == "has_component"
    ]
    assert len(rel) == 1, "Expected exactly one has_component relationship"

    # Step 2: confirm both endpoints exist in entities
    source_id = rel.iloc[0]["source_entity_id"]
    target_id = rel.iloc[0]["target_entity_id"]
    assert source_id in entities["entity_id"].values
    assert target_id in entities["entity_id"].values

    # Step 3: follow evidence_id to evidence table
    ev_id = rel.iloc[0]["evidence_id"]
    ev = evidence[evidence["evidence_id"] == ev_id]
    assert len(ev) == 1, "Expected exactly one evidence row"
    assert ev.iloc[0]["source_type"] == "figure_callout"
    assert ev.iloc[0]["blob_url"] is not None

    # Step 4: follow visual_region_id to visual_regions
    vr_id = ev.iloc[0]["visual_region_id"]
    vr = visual_regions[visual_regions["visual_region_id"] == vr_id]
    assert len(vr) == 1
    assert vr.iloc[0]["region_type"] == "callout"
    assert vr.iloc[0]["identified_entity_id"] == target_id

    # Step 5: follow image_id to visual_assets
    img_id = vr.iloc[0]["image_id"]
    va = visual_assets[visual_assets["image_id"] == img_id]
    assert len(va) == 1
    assert va.iloc[0]["blob_url"] is not None
    assert va.iloc[0]["blob_url"] == ev.iloc[0]["blob_url"]
```

---

## 8. Test Data and Fixtures

### Fixture directory layout

```
tests/
  fixtures/
    csv/
      sample.csv              # minimal valid CSV: 3 rows, Device + Component columns
      malformed.csv           # intentionally broken: missing header, mixed delimiters
      duplicate_ids.csv       # two rows that produce duplicate entity_id after normalization
    docs/
      sample.docx             # tiny Word doc: 2 headings, 1 table (2×3), 1 inline image
      sample.html             # tiny HTML: 1 section, 1 <table>, 1 <img>
    llm/
      sample_enrichment.json  # deterministic Foundry response for sample.csv (all valid)
      broken_schema.json      # LLM response with missing required field (triggers VAL-020)
      empty_entities.json     # LLM response with entities: [] (edge case)
      vision_enrichment.json  # deterministic Foundry vision response for region semantics (§13)
    domain/
      domain_rephrase_response.json  # deterministic Foundry response for domain rephrase pass (§11)
    document_intelligence/
      analyze_result.json     # deterministic AnalyzeResult fixture: polygons, figures, OCR words (§13)
    graph_search_linkage/
      graph_result.json       # simulated graph query output: entity + related chunk IDs (§12)
      expected_search_query.json  # expected AI Search filter expression (§12)
      expected_chunks.json    # expected grounding chunks (§12)
      search_response.json    # mocked AI Search response payload (§12)
    parquet/
      valid/                  # pre-built Parquet fixtures with correct schemas
        entities.parquet
        relationships.parquet
        evidence.parquet
        chunks.parquet
        visual_assets.parquet
        visual_regions.parquet
        document_elements.parquet
        source_files.parquet
      broken/
        entities_dup_id.parquet       # duplicate entity_id (triggers VAL-002)
        entities_missing_id.parquet   # null entity_id (triggers VAL-001)
        relationships_dangling_fk.parquet  # FK to non-existent entity (VAL-003/VAL-004)
        chunks_missing_blob.parquet   # image chunk with null blob_url (triggers VAL-010)
        visual_regions_null_polygon.parquet  # DI region with null polygon_json (triggers VAL-028)
    e2e_trace/
      entities.json
      relationships.json
      visual_assets.json
      visual_regions.json
      evidence.json
    ids_lock/
      valid.json              # correct ids.lock.json
      duplicate_ids.json      # two types share the same numeric ID (triggers VAL-016)
      missing_id.json         # one type has empty string ID (triggers VAL-015)
```

### Sample CSV fixture

```csv
source_file_id,device_name,component_name,part_number,quantity
sf:e2e:sample.csv,Surface Laptop 5,Battery,M1287099-003,1
sf:e2e:sample.csv,Surface Laptop 5,Keyboard Assembly,M1234567-001,1
sf:e2e:sample.csv,Surface Laptop 5,Display Cable,M9876543-002,1
```

### Deterministic LLM response fixture

```json
{
  "entities": [
    {
      "id_hint": "surface-laptop-5",
      "type": "Device",
      "label": "Surface Laptop 5",
      "aliases": [],
      "description": "15-inch laptop device.",
      "confidence": 1.0
    },
    {
      "id_hint": "surface-laptop-5:battery",
      "type": "Component",
      "label": "Battery",
      "aliases": ["Battery pack"],
      "description": "Rechargeable internal battery assembly.",
      "confidence": 0.91
    }
  ],
  "relationships": [
    {
      "source_id_hint": "surface-laptop-5",
      "relation": "has_component",
      "target_id_hint": "surface-laptop-5:battery",
      "evidence_id": "ev:sf:e2e:sample.csv:row:1",
      "confidence": 0.95
    }
  ],
  "chunks": [],
  "visual_assets": [],
  "visual_regions": [],
  "evidence": [
    {
      "evidence_id": "ev:sf:e2e:sample.csv:row:1",
      "source_type": "csv_row",
      "row_index": 1,
      "text": "Surface Laptop 5, Battery, M1287099-003"
    }
  ]
}
```

### Fake blob URLs

All test fixtures use the pattern:

```
https://fake.blob.core.windows.net/kg-assets/{test-run-id}/{asset-id}.{ext}
```

The `BlobUploader` is mocked to return URLs in this pattern without any network call. The `{test-run-id}` is fixed to `"e2e"` in integration fixtures for determinism.

---

## 9. Tooling

### pytest layout

```
tests/
  conftest.py             # shared fixtures: parquet_tables, mock_foundry_client, mock_blob_uploader, mock_document_intelligence
  unit/
    test_schemas.py
    test_ids.py
    test_validators.py
    test_csv_loader.py
    test_chunker.py
    test_chunk_ids.py
    test_content_hash.py
    test_placeholders.py
    test_ontology_compiler.py
    test_document_extractor.py    # Sprint 2
    test_table_extractor.py       # Sprint 2
    test_table_chunker.py         # Sprint 2
    test_blob_uploader.py         # Sprint 2
    test_visual_assets.py         # Sprint 2
    test_visual_regions.py        # Sprint 2
    test_evidence_linking.py      # Sprint 2
    test_cli_entry.py
    test_project_structure.py
    test_parquet_writer.py
    test_placeholder_schemas.py
    test_domain_intake.py         # §11 domain-intake + security tests
    test_config_secrets.py        # §14 Foundry + .env config validation
  contract/
    test_llm_output_schema.py
    test_parquet_binding.py
    test_search_index_schema.py
    test_blob_url_references.py
    test_graph_search_linkage.py  # §12 graph→AI Search clue-chaining
    test_document_intelligence.py # §13 Document Intelligence mocked contract
  integration/
    test_csv_pipeline.py
    test_document_pipeline.py     # Sprint 2
    test_ontology_compile.py
    test_validation_gates.py
    test_e2e_trace.py
    test_deploy_lakehouse.py      # VAL-023 Lakehouse vs AI Search separation
```

### Shared conftest.py fixtures

```python
# tests/conftest.py

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
import pandas as pd

@pytest.fixture
def mock_foundry_client():
    """
    Returns a patched Microsoft Foundry SDK client that always returns the
    deterministic sample_enrichment.json fixture. No network calls.

    Targets: azure.ai.projects.AIProjectClient (or the project's Foundry SDK
    wrapper — whichever constructor is used in src/fabric_kg_builder/enrichment/).
    Patch at the constructor level so any import path resolves to the mock.
    """
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/llm/sample_enrichment.json").read_text()
    )
    client = MagicMock()
    # Foundry SDK chat completion path
    client.inference.get_chat_completions_client.return_value.complete.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=json.dumps(fixture)))]
    )
    return client

@pytest.fixture
def mock_blob_uploader():
    """
    Returns a patched BlobUploader that returns deterministic fake URLs
    and records every upload call for assertion.
    """
    uploader = MagicMock()
    uploader.upload.side_effect = lambda asset_id, data, ext: (
        f"https://fake.blob.core.windows.net/kg-assets/e2e/{asset_id}.{ext}"
    )
    return uploader

@pytest.fixture
def mock_document_intelligence_client():
    """
    Returns a patched Azure AI Document Intelligence AnalyzeDocumentClient.
    Returns a deterministic fixture response with polygon data so visual_region
    extraction is fully deterministic without any live OCR calls.

    Fixture file: tests/fixtures/document_intelligence/analyze_result.json
    """
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/document_intelligence/analyze_result.json").read_text()
    )
    client = MagicMock()
    poller = MagicMock()
    poller.result.return_value = MagicMock(**fixture)
    client.begin_analyze_document.return_value = poller
    return client

@pytest.fixture
def parquet_tables(tmp_path):
    """
    Loads all eight Parquet fixture files from tests/fixtures/parquet/valid/
    into a dict of DataFrames.
    """
    fixture_dir = Path(__file__).parent / "fixtures/parquet/valid"
    tables = {}
    for parquet_file in fixture_dir.glob("*.parquet"):
        tables[parquet_file.stem] = pd.read_parquet(parquet_file)
    return tables
```

### Mocking strategy

| Dependency | Mocking approach | Scope |
|------------|-----------------|-------|
| Microsoft Foundry SDK (`azure.ai.projects.AIProjectClient`) | `unittest.mock.patch` on the client constructor; `inference.get_chat_completions_client().complete()` returns a `MagicMock` wrapping a fixture JSON string; patched at constructor level, not function level | Unit and contract tests |
| Azure AI Document Intelligence (`azure.ai.documentintelligence.DocumentIntelligenceClient`) | `unittest.mock.patch` on the client constructor; `begin_analyze_document().result()` returns a deterministic `AnalyzeResult` fixture from `tests/fixtures/document_intelligence/analyze_result.json` | Unit and contract tests (Sprint 2) |
| Azure Blob Storage (`azure.storage.blob.BlobServiceClient`) | `unittest.mock.patch` on the client; `upload_blob` records calls and returns fake URL | Unit and integration tests |
| Fabric REST API | `unittest.mock.patch` on the HTTP client; returns canned 200 responses | Integration tests |
| File system (Parquet write) | Real file writes to `tmp_path` (pytest built-in) — no mocking needed | Integration tests |

**Rule:** No live API call may be made in unit, contract, or integration tests. Any test that makes a real network call fails CI.

### Coverage targets

| Layer | Target | Enforcement |
|-------|--------|-------------|
| Unit | 90% line coverage for `src/fabric_kg_builder/` | `pytest --cov=src --cov-fail-under=90` |
| Contract | 100% of VAL rules have at least one passing and one failing fixture | Verified by test count assertion in `test_validators.py` |
| Integration | All 13 AC test cases pass | TC-AC-01 through TC-AC-13 |
| Smoke | N/A for merge gate | Deploy pipeline only |

### CI gate policy

| Test suite | When it runs | Blocks merge? |
|------------|-------------|---------------|
| Unit tests | Every PR push | Yes |
| Contract tests | Every PR push | Yes |
| Integration tests | Every PR push | Yes |
| Coverage gate (90%) | Every PR push | Yes |
| Smoke tests | Post-deploy to dev | No (alerts only) |

All merge-blocking tests run with:

```bash
pytest tests/unit tests/contract tests/integration \
  --cov=src --cov-fail-under=90 \
  -v --tb=short
```

---

## 10. Edge Cases and Failure Modes

| Failure Mode | Trigger | Expected Behavior | Test Location |
|-------------|---------|-------------------|---------------|
| Malformed CSV | CSV with inconsistent column count per row | `CsvLoadError` raised; error message includes row number; pipeline exits non-zero | `tests/unit/test_csv_loader.py::test_malformed_csv` |
| Duplicate entity IDs | Two enriched entities with the same `id_hint` after normalization | VAL-002 raised before Parquet write; message includes duplicate ID and count | `tests/unit/test_validators.py::test_val_002_duplicate_entity_id` |
| Dangling FK in relationships | `source_entity_id` references entity not in `entities.parquet` | VAL-003 raised; message includes `rel_id` and missing `entity_id` | `tests/unit/test_validators.py::test_val_003_dangling_source_entity` |
| LLM schema violation | LLM returns JSON with missing `entities` key or wrong type for `confidence` | VAL-020 raised; schema path and message included in error | `tests/contract/test_llm_output_schema.py::test_broken_schema_raises_val020` |
| Missing blob_url after upload | Visual asset record remains with null `blob_url` after blob upload stage | VAL-010 raised; asset ID included in message; pipeline fails before Parquet write | `tests/unit/test_validators.py::test_val_010_missing_blob_url` |
| Oversized LLM input | Source file that, when chunked for LLM context, exceeds the model's token limit | `LlmContextOverflowError` raised; recommendation to reduce chunk size logged; pipeline does not silently truncate | `tests/unit/test_csv_loader.py::test_oversized_input_raises_error` |
| Environment drift | `visual_assets.parquet` schema in test env has a column absent from dev env output | VAL-022 emitted as warning; specific column name and env pair included | `tests/integration/test_validation_gates.py::test_val_022_env_drift` |
| Empty LLM entities list | LLM returns `{"entities": [], ...}` for a non-empty source | Pipeline continues but emits a `WARN: No entities extracted from {source_file}`; zero rows written to `entities.parquet` for that source; overall build does not fail | `tests/contract/test_llm_output_schema.py::test_empty_entities_is_warning_not_error` |
| Duplicate ids.lock IDs | Two type names share the same numeric ID in `ids.lock.json` | VAL-016 raised at startup; both conflicting type names listed in error message | `tests/unit/test_ids.py::test_val_016_duplicate_ids_lock_id` |
| Missing required placeholder | `compile-data` completes but a required placeholder file is absent before `package` | VAL-021 raised; missing path listed in error message | `tests/unit/test_validators.py::test_val_021_missing_placeholder` |
| Missing required env var | Pipeline started without `AZURE_AI_FOUNDRY_API_KEY` or `AZURE_AI_FOUNDRY_ENDPOINT` set | VAL-025 raised immediately at startup before any stage runs; message names the missing variable | `tests/unit/test_config_secrets.py::test_val_025_missing_env_var` |
| Secret in YAML config | `fabric-kg.yaml` contains a raw API key value instead of `${ENV_VAR}` placeholder | VAL-026 raised; file path and key name included in message | `tests/unit/test_config_secrets.py::test_val_026_secret_in_yaml` |
| Domain text in system prompt | Domain brief injected into `role: system` message instead of `role: user` | VAL-024 raised; call ID included in message; pipeline exits non-zero | `tests/unit/test_domain_intake.py::test_val_024_domain_in_system_prompt` |
| Document Intelligence polygon_json null | `visual_region` row with `source_type = document_intelligence` has null `polygon_json` | VAL-028 raised; `visual_region_id` included in message | `tests/unit/test_validators.py::test_val_028_null_polygon_json` |

---

## 11. Domain-Intake Tests

The domain-intake feature lets users supply a business-domain prompt before enrichment. The prompt is normalized into a domain brief and injected into LLM calls. The security contract is hard: user-supplied text must never appear in the LLM system role.

### 11.1 Persistence test

**File:** `tests/unit/test_domain_intake.py`

| Case | Given | When | Then |
|------|-------|------|------|
| CLI accepts domain prompt | `fabric-kg enrich --domain-prompt "Surface device repair guides" ...` | Command parses the `--domain-prompt` flag | Exit 0; `build/enriched/domain.json` exists and `domain_text` field equals the supplied string |
| Domain JSON structure | Any accepted `--domain-prompt` value | `domain.json` is written | JSON contains at least `domain_text` (string), `created_at` (ISO timestamp), `normalized_brief` (string) |
| Empty domain is rejected | `--domain-prompt ""` | Command parses the flag | Exit non-zero; message says domain prompt must not be empty |

```python
def test_domain_prompt_persisted(tmp_path, cli_runner):
    result = cli_runner.invoke(
        cli,
        ["enrich", "--domain-prompt", "Surface device repair guides",
         "--input", "tests/fixtures/csv/sample.csv",
         "--out", str(tmp_path / "enriched")]
    )
    assert result.exit_code == 0
    domain_file = tmp_path / "enriched" / "domain.json"
    assert domain_file.exists()
    data = json.loads(domain_file.read_text())
    assert data["domain_text"] == "Surface device repair guides"
    assert "normalized_brief" in data
```

### 11.2 Rephrase pass test

The enrichment pipeline runs a normalization/rephrase pass on the raw domain text before injecting it into LLM prompts. The rephrase result must be deterministic for a given input when the Foundry mock is used.

| Case | Given | When | Then |
|------|-------|------|------|
| Rephrase produces normalized brief | Raw domain text; Foundry client mocked to return fixture brief | `_rephrase_domain(domain_text)` is called | Returns a non-empty string ≠ raw input; stored in `domain.json` as `normalized_brief` |
| Rephrase is idempotent with fixed mock | Same raw text; same mock fixture | Called twice | Returns identical normalized brief both times |

### 11.3 Security test — domain text in USER role only

> **Priority: HIGH — This is a hard security contract.**

User-supplied text must never appear in the LLM system prompt. This prevents prompt-injection attacks where malicious domain text could hijack system instructions.

**File:** `tests/unit/test_domain_intake.py`

```python
def test_domain_text_never_in_system_prompt(mock_foundry_client):
    """
    VAL-024: Assert that the domain brief appears ONLY in role='user' messages
    and NEVER in role='system' messages across all LLM calls made during enrichment.
    """
    domain_text = "INJECT_MARKER_XYZ"  # unique sentinel; easy to find in any message

    with patch("fabric_kg_builder.enrichment.llm_client", mock_foundry_client):
        run_enrichment(
            source="tests/fixtures/csv/sample.csv",
            domain_text=domain_text,
        )

    # Collect all message lists from every call to the Foundry client
    all_calls = mock_foundry_client.inference.get_chat_completions_client().complete.call_args_list
    assert len(all_calls) > 0, "Expected at least one LLM call"

    for call in all_calls:
        messages = call.kwargs.get("messages") or call.args[0]
        for msg in messages:
            role = msg.get("role") if isinstance(msg, dict) else msg.role
            content = msg.get("content") if isinstance(msg, dict) else msg.content
            if role == "system":
                assert domain_text not in (content or ""), (
                    f"[VAL-024] Domain sentinel found in system prompt: {content!r}"
                )
            if role == "user":
                # At least one user message should carry the domain text
                pass  # checked by test_domain_text_present_in_user_message

def test_domain_text_present_in_user_message(mock_foundry_client):
    """
    Companion to VAL-024: confirm the domain text IS injected into at least one
    user-role message so we know the injection is happening at all.
    """
    domain_text = "INJECT_MARKER_XYZ"
    with patch("fabric_kg_builder.enrichment.llm_client", mock_foundry_client):
        run_enrichment(
            source="tests/fixtures/csv/sample.csv",
            domain_text=domain_text,
        )
    all_calls = mock_foundry_client.inference.get_chat_completions_client().complete.call_args_list
    found_in_user = any(
        domain_text in ((msg.get("content") or "") if isinstance(msg, dict) else (msg.content or ""))
        for call in all_calls
        for msg in (call.kwargs.get("messages") or call.args[0])
        if (msg.get("role") if isinstance(msg, dict) else msg.role) == "user"
    )
    assert found_in_user, "Domain text was never injected into any user-role message"
```

**Fixture:** `tests/fixtures/domain/domain_rephrase_response.json` — deterministic Foundry response for the rephrase pass; mocked separately from the main enrichment fixture.

---

## 12. Graph → AI Search Clue-Chaining Contract

This section defines the contract test for the agent flow that converts graph-derived entity identifiers into AI Search queries and retrieves grounding chunks. The linkage must be deterministic and bidirectional.

### 12.1 Fixture definitions

**Directory:** `tests/fixtures/graph_search_linkage/`

```
tests/fixtures/graph_search_linkage/
  graph_result.json         # simulated graph query output: entity + related chunk IDs
  expected_search_query.json  # expected AI Search filter expression derived from graph result
  expected_chunks.json      # grounding chunks that AI Search should return
  search_response.json      # mocked AI Search response payload (matches expected_chunks)
```

**`graph_result.json`** (minimal deterministic fixture):

```json
{
  "entity_id": "e2e:component:battery",
  "canonical_key": "surface-laptop-5:battery",
  "entity_type": "Component",
  "entity_aliases": ["Battery pack"],
  "related_chunk_ids": [
    "chunk:sf:e2e:sample.csv:section:1:0",
    "chunk:sf:e2e:sample.docx:figure12:desc"
  ]
}
```

**`expected_search_query.json`**:

```json
{
  "filter": "search.in(entity_ids, 'e2e:component:battery', ',')",
  "search": "surface-laptop-5:battery Battery pack",
  "select": "chunk_id,source_path,blob_url,entity_ids,canonical_key,entity_aliases,graph_path",
  "vectorFilterMode": "preFilter"
}
```

**`expected_chunks.json`**:

```json
[
  {
    "chunk_id": "chunk:sf:e2e:sample.csv:section:1:0",
    "source_path": "examples/csv/sample.csv",
    "blob_url": null,
    "entity_ids": ["e2e:component:battery"],
    "canonical_key": "surface-laptop-5:battery",
    "entity_aliases": ["Battery pack"],
    "graph_path": "Component/surface-laptop-5:battery"
  },
  {
    "chunk_id": "chunk:sf:e2e:sample.docx:figure12:desc",
    "source_path": "examples/docs/sample.docx",
    "blob_url": "https://fake.blob.core.windows.net/kg-assets/e2e/figure12.png",
    "entity_ids": ["e2e:component:battery"],
    "canonical_key": "surface-laptop-5:battery",
    "entity_aliases": ["Battery pack"],
    "graph_path": "Component/surface-laptop-5:battery"
  }
]
```

### 12.2 Contract test — entity → chunks

**File:** `tests/contract/test_graph_search_linkage.py`

```python
def test_entity_to_chunks_query_is_correct(mock_search_client):
    """
    Given a graph result with entity_id, canonical_key, and entity_aliases,
    assert the AI Search query is built using search.in() on stable IDs (filter),
    aliases appended to the keyword search param, vectorFilterMode=preFilter,
    and provenance fields in select.

    Anti-pattern guard: filter must NOT use entity_ids/any(id: id eq ...) OData chains.
    """
    graph_result = load_fixture("graph_search_linkage/graph_result.json")
    expected_query = load_fixture("graph_search_linkage/expected_search_query.json")

    actual_query = build_search_query_from_entity(
        entity_id=graph_result["entity_id"],
        canonical_key=graph_result["canonical_key"],
        entity_aliases=graph_result.get("entity_aliases", []),
    )

    # filter: must use search.in() — never entity_ids/any(id: id eq ...) chains
    assert actual_query["filter"] == expected_query["filter"], (
        "Filter must use search.in(entity_ids, '...', ',') — "
        "entity_ids/any(id: id eq ...) is the forbidden anti-pattern"
    )
    # aliases go into the keyword search param, not the filter
    assert actual_query["search"] == expected_query["search"]
    # provenance fields: chunk_id, source_path, blob_url, entity_ids, canonical_key, entity_aliases, graph_path
    assert actual_query["select"] == expected_query["select"]
    # preFilter mode: filter applied before vector scoring, not post
    assert actual_query["vectorFilterMode"] == expected_query["vectorFilterMode"]
    assert actual_query["vectorFilterMode"] == "preFilter"


def test_search_returns_expected_chunks(mock_search_client):
    """
    Given the mocked AI Search response (search_response.json),
    assert retrieved chunks carry the full provenance select fields:
    chunk_id, source_path, blob_url, entity_ids, canonical_key, entity_aliases, graph_path.
    """
    mock_search_client.search.return_value = load_fixture(
        "graph_search_linkage/search_response.json"
    )
    expected = load_fixture("graph_search_linkage/expected_chunks.json")

    result = retrieve_grounding_chunks(
        entity_id="e2e:component:battery",
        canonical_key="surface-laptop-5:battery",
        entity_aliases=["Battery pack"],
        search_client=mock_search_client,
    )

    assert len(result) == len(expected)
    for actual_chunk, expected_chunk in zip(
        sorted(result, key=lambda c: c["chunk_id"]),
        sorted(expected, key=lambda c: c["chunk_id"]),
    ):
        assert actual_chunk["chunk_id"] == expected_chunk["chunk_id"]
        assert actual_chunk["blob_url"] == expected_chunk["blob_url"]
        assert actual_chunk["canonical_key"] == expected_chunk["canonical_key"]
        assert actual_chunk["entity_aliases"] == expected_chunk["entity_aliases"]
        assert actual_chunk["graph_path"] == expected_chunk["graph_path"]
        assert actual_chunk["source_path"] == expected_chunk["source_path"]
```

### 12.3 Bidirectional linkage assertions

Both directions must be deterministic. The following table defines the assertions:

| Direction | Input | Expected output | Assertion |
|-----------|-------|-----------------|-----------|
| Entity → chunks | `entity_id = "e2e:component:battery"` | 2 chunks from `expected_chunks.json`; each carrying `canonical_key`, `entity_aliases`, `graph_path`, `source_path` | `len(chunks) == 2`; each chunk carries `entity_id` in its `entity_ids` array; provenance fields non-null |
| Chunk → entities | `chunk_id = "chunk:sf:e2e:sample.csv:section:1:0"` | Entity `e2e:component:battery` | Entity is in Parquet `entities` table; `canonical_key` matches; `search_aliases` non-null |
| Chunk → entities (visual) | `chunk_id = "chunk:sf:e2e:sample.docx:figure12:desc"` | Entity `e2e:component:battery` + `blob_url` is non-null + `graph_path` is non-null | `blob_url` and `graph_path` match fixture values |

**File:** `tests/contract/test_graph_search_linkage.py::test_bidirectional_linkage_deterministic`

```python
def test_bidirectional_linkage_deterministic(parquet_tables, mock_search_client):
    """
    Asserts both directions of entity<->chunk linkage produce deterministic, consistent results.
    """
    chunks = parquet_tables["chunks"]
    entities = parquet_tables["entities"]

    # Forward: entity → chunks
    entity_chunks = chunks[chunks["entity_ids"].apply(
        lambda ids: "e2e:component:battery" in (ids or [])
    )]
    assert len(entity_chunks) >= 1

    # Reverse: chunk → entity
    for _, chunk_row in entity_chunks.iterrows():
        for eid in chunk_row["entity_ids"]:
            assert eid in entities["entity_id"].values, (
                f"Chunk {chunk_row['chunk_id']} references entity {eid} not in entities table"
            )
```

---

## 13. Document Intelligence Tests (Mocked)

`visual_regions` extraction now depends on Azure AI Document Intelligence (for OCR and bounding polygons) and the vision LLM (for semantic region classification). Both are mocked in all non-smoke tests.

### 13.1 Mocked Document Intelligence fixture

**File:** `tests/fixtures/document_intelligence/analyze_result.json`

This fixture represents a deterministic `AnalyzeResult` payload from the Document Intelligence `prebuilt-layout` model. It must contain:

- At least one `page` with `polygons` populated
- At least one `figure` with a bounding region
- At least one `table` cell with polygon data

```json
{
  "pages": [
    {
      "page_number": 1,
      "width": 8.5,
      "height": 11.0,
      "words": [
        {"content": "Battery", "polygon": [0.5, 1.0, 1.5, 1.0, 1.5, 1.2, 0.5, 1.2], "confidence": 0.99}
      ]
    }
  ],
  "figures": [
    {
      "figure_id": "fig:1",
      "bounding_regions": [
        {"page_number": 1, "polygon": [2.0, 3.0, 5.0, 3.0, 5.0, 6.0, 2.0, 6.0]}
      ],
      "caption": {"content": "Battery connector location"}
    }
  ]
}
```

### 13.2 Contract tests

**File:** `tests/contract/test_document_intelligence.py`

| Case | Given | When | Then |
|------|-------|------|------|
| polygon_json populated | Mocked `AnalyzeResult` with one figure and polygon | `extract_visual_regions(doc, di_client=mock_di)` | Returned `visual_region` has non-null `polygon_json`; JSON parses to a list of coordinate pairs |
| text populated | Mocked result with word-level OCR for a figure region | `extract_visual_regions(...)` | `text` field on the region equals the OCR text from the fixture |
| region_type populated | Figure from Document Intelligence | `extract_visual_regions(...)` | `region_type == "figure"` |
| provenance recorded | Any region extracted from Document Intelligence | `extract_visual_regions(...)` | `source_type == "document_intelligence"` on the returned region |
| vision LLM enrichment mocked separately | DI result ready; vision LLM patched to return `{"label": "Battery callout", "region_type": "callout"}` | `enrich_visual_regions(regions, llm_client=mock_foundry)` | `region_type` updated to `"callout"`; `label` set; DI fixture unchanged |

```python
def test_polygon_json_populated_from_document_intelligence(mock_document_intelligence_client):
    """VAL-028: polygon_json must be non-null and valid JSON for DI-sourced regions."""
    regions = extract_visual_regions(
        document_path="tests/fixtures/docs/sample.docx",
        di_client=mock_document_intelligence_client,
    )
    assert len(regions) >= 1
    for region in regions:
        assert region["source_type"] == "document_intelligence"
        assert region["polygon_json"] is not None
        polygon = json.loads(region["polygon_json"])
        assert isinstance(polygon, list)
        assert len(polygon) >= 4, "Polygon must have at least 4 coordinate values"

def test_vision_llm_enrichment_is_independent_of_di_fixture(
    mock_document_intelligence_client, mock_foundry_client
):
    """
    Vision LLM enrichment is a separate pass. DI gives geometry; LLM gives semantics.
    Mock both independently and assert neither bleeds into the other's fields.
    """
    regions = extract_visual_regions(
        document_path="tests/fixtures/docs/sample.docx",
        di_client=mock_document_intelligence_client,
    )
    # polygon_json comes from DI — must be set before LLM pass
    assert all(r["polygon_json"] is not None for r in regions)

    enriched = enrich_visual_regions(regions, llm_client=mock_foundry_client)
    # LLM pass adds label and semantic region_type — polygon_json must be unchanged
    for original, enriched_region in zip(regions, enriched):
        assert enriched_region["polygon_json"] == original["polygon_json"]
```

### 13.3 Fixture additions for Document Intelligence

Add to `tests/fixtures/`:

```
tests/fixtures/
  document_intelligence/
    analyze_result.json       # deterministic AnalyzeResult payload (see §13.1)
  llm/
    vision_enrichment.json    # deterministic Foundry vision response for region semantics
```

---

## 14. Foundry + .env Config Validation Tests

These tests protect against misconfiguration and secret leakage. They reference the test-discipline and secret-handling skills.

### 14.1 Required secrets fail fast

**File:** `tests/unit/test_config_secrets.py`

| Case | Given | When | Then |
|------|-------|------|------|
| All secrets present | All required env vars set to non-empty test values | `validate_required_secrets()` | Returns without error |
| Missing AZURE_AI_FOUNDRY_ENDPOINT | `AZURE_AI_FOUNDRY_ENDPOINT` unset | `validate_required_secrets()` | Raises `ConfigError` with `[VAL-025]` prefix; message names the missing variable |
| Missing AZURE_AI_FOUNDRY_API_KEY | `AZURE_AI_FOUNDRY_API_KEY` unset | `validate_required_secrets()` | Raises `ConfigError` with `[VAL-025]` prefix; message names the missing variable |
| Missing FABRIC_WORKSPACE_ID | `FABRIC_WORKSPACE_ID` unset | `validate_required_secrets()` | Raises `ConfigError` naming `FABRIC_WORKSPACE_ID` |
| Missing AZURE_BLOB_CONNECTION_STRING | Env var absent | `validate_required_secrets()` | Raises `ConfigError` naming the missing var |
| Empty string treated as missing | `AZURE_AI_FOUNDRY_API_KEY=""` | `validate_required_secrets()` | Raises `ConfigError` (empty string is not acceptable) |

```python
import os
import pytest
from fabric_kg_builder.config import validate_required_secrets, ConfigError

REQUIRED_VARS = [
    "AZURE_AI_FOUNDRY_ENDPOINT",
    "AZURE_AI_FOUNDRY_API_KEY",
    "FABRIC_WORKSPACE_ID",
    "AZURE_BLOB_CONNECTION_STRING",
]

@pytest.mark.parametrize("missing_var", REQUIRED_VARS)
def test_missing_env_var_raises_config_error(monkeypatch, missing_var):
    """VAL-025: absent required env var must raise ConfigError before any pipeline stage."""
    for var in REQUIRED_VARS:
        monkeypatch.setenv(var, "test-value")
    monkeypatch.delenv(missing_var)

    with pytest.raises(ConfigError) as exc_info:
        validate_required_secrets()
    assert "[VAL-025]" in str(exc_info.value)
    assert missing_var in str(exc_info.value)

def test_empty_string_treated_as_missing(monkeypatch):
    """VAL-025: empty string must be treated the same as absent."""
    for var in REQUIRED_VARS:
        monkeypatch.setenv(var, "test-value")
    monkeypatch.setenv("AZURE_AI_FOUNDRY_API_KEY", "")

    with pytest.raises(ConfigError) as exc_info:
        validate_required_secrets()
    assert "AZURE_AI_FOUNDRY_API_KEY" in str(exc_info.value)
```

### 14.2 Guard test — no secret values in committed files

**File:** `tests/unit/test_config_secrets.py`

This test scans the committed config files for patterns that look like real secrets. It is a static analysis guard, not a runtime check.

```python
import re
from pathlib import Path

# Patterns that indicate a raw secret value (not a ${ENV_VAR} placeholder)
SECRET_PATTERNS = [
    re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"),      # base64-like (API keys, tokens)
    re.compile(r"sk-[A-Za-z0-9]{32,}"),             # OpenAI-style key
    re.compile(r"AccountKey=[A-Za-z0-9+/=]{40,}"),  # Azure storage connection string
]

CONFIG_FILES_TO_SCAN = [
    "fabric-kg.yaml",
    *Path("ontology/environments").glob("*.json"),
]

def test_no_secrets_in_config_files():
    """VAL-026: committed config files must contain no raw secret values."""
    violations = []
    for config_path in CONFIG_FILES_TO_SCAN:
        path = Path(config_path)
        if not path.exists():
            continue
        text = path.read_text()
        for pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                violations.append(
                    f"[VAL-026] Possible secret in {config_path}: '{match.group()[:12]}...'"
                )
    assert not violations, "\n".join(violations)
```

### 14.3 Foundry deployment-name resolves

**File:** `tests/unit/test_config_secrets.py`

```python
def test_foundry_deployment_name_resolves(monkeypatch, mock_foundry_client):
    """VAL-027: enrichment.chat_deployment in fabric-kg.yaml must resolve to a known deployment,
    authenticated via AZURE_AI_FOUNDRY_ENDPOINT and AZURE_AI_FOUNDRY_API_KEY."""
    monkeypatch.setenv("AZURE_AI_FOUNDRY_ENDPOINT", "https://fake.foundry.azure.com")
    monkeypatch.setenv("AZURE_AI_FOUNDRY_API_KEY", "fake-key-for-testing")

    # Mock the list-deployments call to return a known deployment
    mock_foundry_client.deployments.list.return_value = [
        MagicMock(name="gpt-4.1-dev"),
        MagicMock(name="gpt-4.1-mini-dev"),
    ]

    with patch("fabric_kg_builder.config.foundry_client", mock_foundry_client):
        # Should not raise
        resolve_foundry_deployment()

def test_unknown_foundry_deployment_raises(monkeypatch, mock_foundry_client):
    """VAL-027: unresolvable enrichment.chat_deployment must raise ConfigError."""
    monkeypatch.setenv("AZURE_AI_FOUNDRY_ENDPOINT", "https://fake.foundry.azure.com")
    monkeypatch.setenv("AZURE_AI_FOUNDRY_API_KEY", "fake-key-for-testing")
    mock_foundry_client.deployments.list.return_value = [
        MagicMock(name="gpt-4.1-dev"),
    ]

    with patch("fabric_kg_builder.config.foundry_client", mock_foundry_client):
        with pytest.raises(ConfigError) as exc_info:
            resolve_foundry_deployment()
    assert "[VAL-027]" in str(exc_info.value)
    assert "nonexistent-model" in str(exc_info.value)
```

### 14.4 Updated LLM mocking target

All test files that previously patched `openai.OpenAI` must instead patch the Microsoft Foundry SDK constructor. The patch target follows the import path used in `src/fabric_kg_builder/enrichment/`:

```python
# Before (deprecated — do not use)
with patch("openai.OpenAI") as mock_openai:
    ...

# After (correct — Foundry SDK)
with patch("fabric_kg_builder.enrichment.foundry_client") as mock_foundry:
    mock_foundry.inference.get_chat_completions_client().complete.return_value = ...
```

This change applies to: `test_csv_pipeline.py`, `test_document_pipeline.py`, `test_e2e_trace.py`, `test_ontology_compile.py`, and all contract tests that invoke the enrichment pipeline.

### 14.5 Acceptance matrix additions (Foundry + config)

| AC# | Criterion | Test Case ID | Pass Condition |
|-----|-----------|--------------|----------------|
| AC-F1 | CLI fails fast on missing secrets | TC-AC-F1 | `fabric-kg enrich` with `AZURE_AI_FOUNDRY_API_KEY` unset exits non-zero with `[VAL-025]` in stderr before any file is written |
| AC-F2 | No secret in any committed config file | TC-AC-F2 | `test_no_secrets_in_config_files` passes on every PR; zero violations reported |
| AC-F3 | Foundry deployment resolves before enrichment starts | TC-AC-F3 | With valid `AZURE_AI_FOUNDRY_ENDPOINT`, `AZURE_AI_FOUNDRY_API_KEY`, and mocked Foundry client, `fabric-kg enrich` begins without `[VAL-027]` error |

## 15. Schema-2.0 Validation Gates

These gates apply only to schema-2.0 projects. Schema-1.0 tests and behavior
remain unchanged.

| Gate | Stage | Failure condition |
|---|---|---|
| DOM-101 | domain validation | Fewer than five or more than ten competency questions |
| DOM-102 | domain validation | An extracted entity lacks proposal evidence without `business_defined=true`, or a relationship lacks both proposal evidence and an explicit governance rule/business justification |
| DOM-103 | domain validation | N is outside 1-24, or N 21-24 lacks rationale; N below 8 is advisory and must not be padded |
| DOM-104 | domain validation | A required question has neither a valid path nor an explicit unsupported result; unsupported critical questions block approval |
| DOM-105 | domain validation | K differs from the maximum shortest covered path on the question-scoped relationship graph, exceeds 4, or K=4 lacks cited rationale |
| DOM-106 | domain validation | Type IDs, predicates, endpoint signatures, per-hop endpoints, or traversal direction are ambiguous or invalid |
| EXT-101 | enrichment | A canonical type or predicate is outside the approved closed vocabulary |
| EXT-102 | L3 validation | An asserted classification, relationship, or property observation lacks a locally minted, verified C0 evidence span, or an asserted relationship lacks grounded endpoints |
| EXT-103 | L3 validation | Endpoint validation is not transitive, deterministic, or compliant with exact-only policy |
| EXT-104 | enrichment | Work-unit overflow truncates candidates instead of deterministic splitting |
| EXT-105 | enrichment | L2 consumes a missing, non-succeeded, stale, or hash-inconsistent L1 handoff |
| EXT-106 | enrichment | Any complete-corpus entry lacks a materialized, explicitly-empty, excluded, or blocked disposition |
| EXT-107 | enrichment | An input candidate lacks exactly one retained/deduplicated accounting disposition |
| EXT-108 | enrichment | L2 mints verified evidence, emits an asserted state, or activates L3/L4 output |
| EXT-109 | L3 validation | L3 consumes a non-succeeded/skipped, stale, or hash-inconsistent L2 handoff, or L2 already minted an L3-owned contract |
| EXT-110 | L3 validation | A retained candidate lacks exactly one appended current lifecycle state, or a terminal record is mutated |
| EXT-111 | L3 validation | A required-member-set proposal lacks exactly one L3 outcome, or an incomplete collection is sealed as a complete `RequiredMemberManifest` |
| EXT-112 | L3 validation | L3 consumes a remote resource, emits `AuditProjection`/`SemanticServingProjection`, or activates schema-2 CLI/serving behavior |
| EXT-113 | L3 validation | A candidate is asserted without local proof: an unproven relationship direction, an unattributed or unvalued property, an unrecomputed entity identity, or a count proven by L1 design-sample evidence |
| EXT-114 | L3 validation | A reused leaf checkpoint is trusted without recomputing its canonical payload hash, a reported current state diverges from its sealed appended transition or from `classify_state`, or any published leaf is not byte-identical to a full re-derivation from the sealed L2 proposal, verified SourceUnits, and compiled Domain authority |
| EXT-115 | L3 validation | A required-member-set proposal is accepted on self-consistency instead of exact equality with the sealed `CompletenessRequirementV2` membership relationship, ordering policy, cardinality bounds, and required roles |
| EXT-116 | L3 validation | A leaf fingerprint binds less than the complete canonical `SourceUnit` artifact, allowing a same-text unit with a changed `unit_kind`, `offset_unit`, `ordinal`, or locator to reuse a stale leaf |
| SEM-100 | compile-data | `semantic_entities` contains a non-asserted, unapproved, stale-hash, or unevidenced extracted row |
| SEM-101 | compile-data | `semantic_relationships` contains a state other than asserted |
| SEM-102 | compile-data | A semantic relationship lacks a valid evidence FK |
| SEM-103 | compile-data | A semantic relationship endpoint is absent from `semantic_entities` |
| SEM-104 | compile-data | Candidate lifecycle counts do not reconcile exactly |
| DEP-101 | compile/deploy | Ontology or Graph selects a raw relationship source instead of the sealed semantic projection |
| DEP-102 | deploy/read-back | Compile, prepared deploy, or persisted count/hash evidence differs |
| QRY-101 | query planning | A generated path exceeds approved K or uses unbounded traversal |

### 15.1 Foundation test matrix

The schema foundation merge gate covers:

- strict version discrimination and unknown-key rejection;
- strict schema-2.0 nested sections, nonblank required text, and rejection of
  schema-1.0 scalar/null list coercion;
- unchanged schema-1.0 defaults, loading, hashing, review, approval, and
  enrichment guards;
- schema-2.0 YAML/JSON round-trip and generated JSON Schema;
- N advisory/no-padding behavior, rationale threshold, and hard maximum;
- derived K, justified K=4, rejection above four, and per-hop direction;
- question-scoped relationship authorization and shortest-path computation;
- proposal-evidence requirements for entities and relationships; and
- canonical publication excluded-state ordering and hash equivalence;
- deterministic contract hashing excluding approval metadata; and
- explicit non-activation of schema-2.0 enrichment until proposal approval is
  implemented.

### 15.2 C0.Core contract gate

SPEC-006 C0.Core adds offline contract tests for strict frozen models, registered
kind/version negotiation, unknown-major rejection, NFC canonical JSON and golden
hashes, immutable secret-free locators, Unicode exact-span verification, every
allowed/forbidden lifecycle transition, mutually exclusive disposition and
current-state accounting, asserted-only serving membership, manifest totals,
receipt skip preconditions, resource-metric binding, and equality adapters to
the existing domain/lineage/model/checkpoint/semantic authorities.

These tests validate contract shapes and invariants only. Later layers add
subtype-validation integration, materialization, query execution, isolated
installation, approved numeric performance policies, and live-smoke evidence
without weakening the C0 gate.

### 15.3 L2 schema-constrained extraction gate

The L2 merge gate runs offline and covers:

- intact succeeded L1 receipt and exact domain, hierarchy, identity-policy,
  completeness, complete-corpus, SourceUnit-manifest, prompt, model, extractor,
  and C0 carrier bindings;
- complete corpus rather than bounded design-sample extraction, with one
  disposition for every corpus entry and every eligible SourceUnit assigned;
- strict whole-response parsing, closed approved vocabulary, unknown-observation
  audit/rereview behavior, and no authority mutation;
- type-independent entity identity, classification versions, predicate/endpoint
  relationship identity, and deterministic cross-leaf deduplication;
- one mutually exclusive accounting disposition per input, separate non-additive
  audit reasons, and initial proposed-only lifecycle records;
- deterministic structural splitting, atomic overflow failure, bounded
  batch/concurrency scheduling, immutable leaf checkpoint reuse, and corrupt-leaf
  rerun without repeated successful remote work;
- manufacturing, clinical, and logistics structured fact-set fixtures proving
  domain-neutral use of sealed completeness roles/order/cardinality;
- exact `RequiredMemberSetProposal@1.1.0` policy and nullable cardinality
  preservation, roleless unordered canonicalization, role-bearing ordered
  positions, structural-violation observability, and prohibition of sentinel
  roles or fabricated order;
- proposal schema/hash, receipt, skip-key, and accepted-version binding to C0
  carrier 1.1, including invalidation of legacy 1.0 resume fingerprints;
- proposed anchors without EvidenceSpan minting, asserted state, endpoint/subtype
  terminal validation, required-member sealing, serving, publication, or Graph
  activation;
- dry-run behavior with zero remote calls and writes, no schema-2 product CLI
  activation, and unchanged schema-1 tests.

No numeric latency, throughput, memory, retry, or cache threshold is introduced
by this gate. The approved maximum relationship-candidate count is semantic
policy rather than a performance target.

### 15.4 L3 evidence-validation gate

The L3 merge gate runs offline, makes no remote call of any kind, and covers:

- intact succeeded or skipped L2 receipt, output/input manifests, resource
  metrics, exact L2 accepted-contract versions, and the complete L1 authority
  chain, with `L3_INPUT_RECEIPT_INVALID`, `L3_INPUT_MANIFEST_INVALID`,
  `L3_CONTRACT_VERSION_UNSUPPORTED`, and `L3_SOURCE_UNIT_MISSING` fail-closed
  entry paths;
- complete L2 accounting before validation: one disposition per input, one
  sequence-zero proposed event per retained candidate, retained candidates equal
  to proposal partitions, resolvable dedup targets, and every required-member
  proposal bound to its exact merged batch, requirement, and requirement hash;
- every required-member collection policy re-bound to its sealed
  `CompletenessRequirementV2` rather than accepted on proposal self-consistency:
  membership semantic relationship, ordering policy (including the ordered
  carrier's `member_order_encoding`), expected/minimum/maximum cardinality, and
  required roles must equal the approved authority exactly, with
  `L3_COMPLETENESS_HASH_MISMATCH` covering Domain-ordered vs proposal-unordered,
  Domain `expected=3` vs an omitted proposal bound, a membership-predicate
  mismatch, and a required-role mismatch;
- rejection of any handoff in which L2 already minted `c0.evidence_span` or
  `c0.required_member_manifest`;
- exhaustive Unicode code-point evidence verification across Latin, accented,
  CJK, combining-mark, astral-plane, and emoji text, including deterministic
  replay of the same C0 evidence ID and refusal to mint on invalid ranges, empty
  quotes, quote mismatch, or source-hash drift;
- every L3-minted span is `c0.evidence_span@1.1.0` with
  `purpose=extraction_assertion`, the accepted purpose-policy version, and the
  L3 extraction verifier identity; changing verifier implementation version or
  purpose-policy version changes the evidence ID deterministically;
- L1 `domain_design` spans, whether read as 1.0 or adapted to 1.1 through the C0
  trusted-context adapter, can never assert an extraction candidate, and no 1.0
  artifact can adapt to `extraction_assertion`;
- model-authored evidence IDs are ignored as identity, recorded as
  `MODEL_EVIDENCE_ID_IGNORED`, and never enter the verified evidence set;
- endpoint grounding inside the relationship span, unique exact label matching,
  exact occurrence anchors for repeated names, and refusal to ground two
  endpoints to one occurrence;
- case-folded local endpoint references that resolve to exactly one retained
  entity candidate, and `ENDPOINT_UNRESOLVED` when they do not;
- forward-only direction, rejection of reverse/unknown proposals without silent
  endpoint swapping, and `SOURCE_TYPE_MISMATCH`/`TARGET_TYPE_MISMATCH` through
  the sealed ancestor closure only;
- direction proof treated as a separate obligation from endpoint compatibility:
  because the frozen L2 carrier never persists the model-proposed direction
  token, no relationship candidate may reach `asserted`, forward/reverse/unknown
  proposals all transition to `unsupported` with
  `EVIDENCE_MODALITY_UNSUPPORTED`, and a more precise blocking reason
  (`DIRECTION_MISMATCH`, `ENDPOINT_UNRESOLVED`, `ENDPOINT_EVIDENCE_UNGROUNDED`,
  `EVIDENCE_MISSING`) keeps its own state instead of being masked;
- property observations that never claim inherited-property validity or value
  conformance while owner attribution and value are unpersisted, transitioning
  to `unsupported` rather than `asserted`;
- direct and multi-level subtype compatibility with recorded deterministic
  inheritance paths, exact-only policy rejecting child endpoints, and no
  word/label similarity anywhere;
- generated acyclic hierarchies returning insertion-order-independent
  inheritance paths, generated cyclic hierarchies terminating with
  `SUBTYPE_HIERARCHY_CYCLE`, and unknown parents failing closed with
  `HIERARCHY_CONCEPT_MISSING`;
- abstract types that can never be instantiated, root-to-leaf inherited
  property/constraint conjunctions, incompatible descendant overrides, and
  properties that remain unresolved under an unresolved sibling classification;
- competing sibling classifications that stay unresolved on one stable entity ID
  in every permutation, and a uniquely most-specific concrete classification that
  asserts without changing entity identity;
- type-independent entity identity recomputation through the sealed root policy
  and C0 ID primitive, business-key normalization, prohibition of any identity
  seed carrying classification/ancestry/depth, and relationship identity that is
  invariant under endpoint reclassification;
- non-assertable identity witnesses: an unpersisted witness is `unresolved` with
  `IDENTITY_WITNESS_UNAVAILABLE`, a witness that does not reproduce the persisted
  stable ID is `rejected` with `IDENTITY_POLICY_VIOLATION` — including the
  regression where one model-authored `stable_source_identity` collapses two
  distinct local references onto one stable ID — and reconciliation refuses any
  asserted entity whose identity was not recomputed;
- deterministic reason-code to state mapping with the fixed precedence
  rejected > unsupported > discovery > unresolved > asserted, rejection of
  unstable reason codes, exactly one appended transition per retained candidate,
  refusal to mutate a terminal record, and refusal to assert without evidence;
- hierarchy depth reported independently of K, and receipts exposing the exact
  domain, hierarchy, root identity-policy, completeness-requirement, and approved
  external-reference-decision hashes;
- authority drift blocking the stage while observation drift emits deterministic
  discovery plus `DOMAIN_REREVIEW_REQUESTED` without mutating any sealed
  authority or serving schema;
- generic structured-collection validation parameterized across unrelated
  manufacturing, clinical, and logistics domains covering unordered/roleless,
  unordered/role-bearing, and ordered/role-bearing policies;
- missing members, missing required roles, member type/role mismatch, missing
  membership evidence, duplicate/missing ordinals, ordinal policy violations,
  broken/duplicate/extra/branching/cyclic adjacency edges, unsupported count
  claims, count-bound violations, and collection-hash mismatch all keeping the
  collection `unresolved`;
- adjacency obligations that are never invented from ordinal order, and counts
  that are never inferred when the Domain contract does not specify them;
- `source_evidence` cardinality proven only by locally minted
  `extraction_assertion` spans, with bounded L1 design-sample span IDs passed as
  an explicitly prohibited set so a design-evidenced count keeps the collection
  `unresolved` with `CARDINALITY_EVIDENCE_INVALID`;
- exactly one deterministic L3 outcome per required-member-set proposal, with a
  sealed `c0.required_member_manifest@1.1.0` only for complete collections and an
  audit-addressable unresolved outcome naming its missing obligations otherwise;
- the sealed-manifest and unresolved-outcome carriers forming a strict partition
  of the proposal set — every proposal appears exactly once across the two, with
  no duplicate, no drop, no proposal in both, and no manifest naming a foreign
  proposal — recorded as an explicit frozen-contract constraint because
  `c0.required_member_manifest@1.1.0` cannot represent an incomplete collection;
- member permutations producing identical member identity sets, order, and
  collection hashes;
- manifest reconciliation covering one current state per retained candidate,
  resolvable evidence references, asserted relationships with grounded endpoints,
  asserted classifications/property observations with approved IDs, evidence, and
  the exact sealed hashes, unique appended lifecycle record IDs, and no duplicate
  stable node or edge ID;
- lifecycle cross-checking of every reported current state against the sealed
  appended transition and against an independent `classify_state` re-derivation,
  so a structurally valid, resealed leaf checkpoint that upgrades a candidate to
  `asserted` — with or without matching reason codes — fails closed with
  `L3_LIFECYCLE_CHAIN_INVALID`;
- full re-derivation of every published leaf from the sealed L2 proposal
  partition, the verified `SourceUnit` artifacts, and the compiled Domain
  authority, requiring byte-exact equality for the complete reason set and state
  of every candidate kind plus its classifications, property observations, minted
  evidence, appended transitions, and leaf fingerprint;
- interrupted-run forgery rejected for every candidate kind: a checkpoint whose
  candidate result, lifecycle transition hash, and leaf payload hash are all
  resealed to upgrade a relationship or property from `unsupported` to
  `asserted`, with nothing published to collide against, fails closed with
  `L3_VALIDATION_RESULT_INCOMPLETE` and the rerun recomputes the correct
  `unsupported` outcome instead of publishing the forgery;
- leaf checkpoints that carry a canonical payload hash recomputed before reuse
  and are content-addressed by their leaf fingerprint, so a corrupt or stale
  checkpoint is discarded at the cache gate and a resealed checkpoint is caught
  by reconciliation rather than trusted;
- leaf fingerprints that bind the complete canonical `SourceUnit` artifact rather
  than its ID and text hash, so a same-text unit whose `unit_kind`,
  `offset_unit`, `ordinal`, locator, or identity changed yields a different
  fingerprint, cannot address the earlier checkpoint, and cannot republish the
  stale states it would have implied;
- deterministic replay producing an identical receipt hash and output-manifest
  hash, intact leaf reuse, corrupt-leaf rerun that leaves intact leaves
  untouched, and receipt recovery after a lost receipt;
- fingerprint-scoped run directories for mutable per-run stage artifacts, so a
  changed validator, source, or candidate set reruns into its own directory
  without an immutable-artifact collision and without reusing a stale leaf, while
  the immutable content-addressed collision rule stays enforced within a run;
- every remote resource dimension remaining exactly zero, no `AuditProjection` or
  `SemanticServingProjection`, no schema-2 product CLI activation, and no import
  of CLI, Graph, serving, compile, deploy, runtime, canonical/Arrow, or legacy
  entity-ID modules from either L3 module.

No numeric latency, throughput, memory, retry, token, byte, or cache threshold is
introduced by this gate. Hierarchy depth is a reported dimension and is never
compared to or derived from K.

L3 validation command:

```
PYTHONPATH=src uv run --offline --no-project pytest -q \
  tests/contract/test_c0_core_contracts.py \
  tests/contract/test_c0_extraction_contracts.py \
  tests/unit/test_c0_core_adapters.py \
  tests/unit/test_schema2_sources.py \
  tests/unit/test_schema2_extraction.py \
  tests/unit/test_schema2_work_units.py \
  tests/unit/test_schema2_evidence.py \
  tests/unit/test_schema2_validation_stage.py \
  tests/unit/test_graph_occurrence.py \
  tests/unit/test_lineage_common.py \
  tests/unit/test_schemas.py
```

### 15.5 L4 audit and serving projection gate

The L4 merge gate is local-only and covers:

- intact succeeded L3 receipt, output manifest, resource metrics, exact accepted
  versions, leaf artifacts, lifecycle transitions, evidence spans, sealed
  hierarchy/identity/completeness hashes, and every
  `RequiredMemberManifest@1.1.0`;
- complete input-to-retained-or-deduplicated mapping and exact reconciliation of
  `input = retained + deduplicated` and `retained = sum(lifecycle states)`,
  including asserted, discovery, unresolved, rejected, and unsupported states;
- non-additive reason-code metrics that do not alter the accounting partition;
- asserted-only serving membership, with discovery, unresolved, rejected,
  unsupported, and raw canonical fallback excluded;
- stable type-independent entity and relationship IDs, one entity row across
  reclassification, and explicit most-specific plus ancestor type assertions;
- verified extraction evidence, recomputed identity witnesses, approved
  governance policy, resolved served endpoints, deterministic inheritance
  paths, and exact hierarchy, identity-policy, Domain, and semantic hashes;
- exact physical projection of required members by copying the C0 manifest
  fields, with mutation tests for member IDs/types, tuple order, role, ordinal,
  candidate reference, member hash, cardinality, required roles, collection
  hash, and source-manifest hashes, plus a manifest-authority row that preserves
  valid zero-member collections;
- deterministic Arrow schemas and Parquet bytes, artifact counts, canonical ID
  set hashes, row fingerprints, local Parquet `ProjectionEquivalence`, and no
  success with a missing, corrupt, partial, or extra file;
- fingerprinted reuse bound to the L3 receipt, authority hashes, accepted
  contract versions, projection-code version, and physical schema hashes;
- per-fingerprint interprocess locking across resume validation and atomic
  publication, including concurrent same-fingerprint writer coverage;
- zero remote, model, Fabric, Graph, Search, or deployment calls, unchanged
  schema-1 source resolution, no schema-2 CLI activation, and fail-closed
  product readiness until an L5 persisted publication receipt exists.

### 15.6 L5a structured publication gate

The L5a gate uses only a real persisted `SealedL4ServingSource` and fake target
clients. It covers:

- exact per-manifest C0.Publish authority and anchored L3 artifact-manifest
  equality;
- deterministic typed Parquet and Semantic Model, Ontology, and Graph
  definitions with explicit hierarchy flattening and approved endpoints;
- persisted definition inputs before publication and exact post-update
  resource, version, policy, schema, count, ID-set, row, and definition
  read-back;
- manifest-specific equivalence for empty, non-empty, distinct, misassigned, and
  swapped required-member collections using actual persisted carried rows;
- uniform accounting for inspect/read-back/mutation calls, including positive
  bytes, retries/waits, globally unique operation references, and remote errors;
- an enforced, derived 20-call worst-case target state machine with rollback
  contained inside the same budget and no N+1 table operations;
- idempotent reuse after local integrity and current target read-back;
- corruption, stale target version, authority/policy drift, partial failure,
  cleanup, and failure receipt/resource metrics;
- unchanged schema-1 behavior and no Search, L6 runtime, Data Agent, synthesis,
  CLI, or live Fabric activation.

L5a validation command:

```text
PYTHONPATH=src pytest -q \
  tests/unit/test_l5a_structured_publication.py \
  tests/unit/test_schema2_projection_stage.py \
  tests/contract/test_c0_publish_contracts.py
```

### 15.7 L5b Runtime 1.1 evidence gate

The L5b gate remains the authoritative Search evidence-plane test surface. It
must cover intact L5a/L5b publication authority, agentic and exact authorized
direct retrieval, all `QueryBudgetV1_1` dimensions, candidate and operation
accounting, spoofed documents, ACL and publication drift, truncation, warnings,
source failures, Unicode-safe exact quotes, citation collisions, and exact
`AgenticRetrievalCoverageReceiptV1_1` validation.

```text
PYTHONPATH=src python3 -m pytest -q \
  tests/unit/test_l5b_evidence_retrieval.py \
  tests/contract/test_c0_runtime_contracts.py
```

### 15.8 L6 bounded agent-integration gate

The L6 gate uses fake scope, Graph, and sealed-L5b hosts. No live deployment,
model call, Graph call, Search call, embedding call, vector call, or RDF
projection occurs. Required cases are:

- exact complete Graph plus complete RequiredMember/citation coverage;
- partial Graph coverage with exact safe missing authority IDs;
- Graph empty, source error, out-of-scope return, warning, truncation, stale
  scope/hash, policy mismatch/collision, and unauthorized principal;
- Search spoof, ACL mismatch, truncation, warning, source failure, duplicate or
  misassigned citation, and stale envelope/presentation hash;
- Runtime 1.1 budget exhaustion and Graph/Search overexecution;
- one authorized direct fallback route with exact origin binding;
- multiple manifests/scopes, multi-endpoint relationships, K bounds, and
  Unicode-safe citation/question handling;
- explicit five-tool schemas, zero-synthesis output assertions, deterministic
  instructions, local definition hash/read-back equivalence, and config drift;
- at most one Graph request, at most one selected L5b retrieval request, zero
  fabric-kg synthesis calls, unchanged schema-1 behavior, and RDF inactivity.

```text
PYTHONPATH=src python3 -m pytest -q \
  tests/unit/test_l6_agent_integration.py

PYTHONPATH=src python3 -m pytest -q -m integration \
  tests/integration/test_l6_agent_integration.py
```

### 15.9 L7 production deployment adapter foundation gate

The L7 gate is offline and fake-backed. It must prove that dry-run performs only
identity/GET probes; plan JSON is canonical, immutable, hashed, sanitized, and
expires; live mode requires `--approve-live` equal to the exact persisted plan
hash; and tenant, principal, config, audience, L5a/L5b/L6 hash, definition, or
ETag drift fails before the first mutation. Resume accepts only the same plan
and state.

It also covers Blob lease/CAS concurrency, concurrent run claims, expired claim
recovery, one-time receipt consumption, absent/rotated opaque signers,
deadline-aware/cancellable Graph transport and ignored late results, RemoteTool
auth-first bounded streaming/framing/encoding/disconnect/schema/deadline and
authenticated readiness/OpenAPI behavior, bearer header use without token
disclosure, signed durable CustomKeys ownership, transaction-journal rollback
under network/parser/interrupt failures, mandatory canonical Fabric definition
bytes plus POST/LRO readback, Foundry version readback, accounting, redaction,
and unchanged RDF inactivity.

```text
PYTHONPATH=src python3 -m pytest -q \
  tests/unit/test_l6_blob_authority.py \
  tests/unit/test_l7_deployment.py \
  tests/unit/test_l7_adapters.py \
  tests/unit/test_l7_ownership.py \
  tests/unit/test_l7_remote_tool.py \
  tests/unit/test_project_connections_l7.py \
  tests/unit/test_l7_cli.py \
  tests/unit/test_l6_agent_integration.py
```

L4 validation command:

```text
PYTHONPATH=src pytest -q \
  tests/unit/test_schema2_projection_stage.py \
  tests/unit/test_schema2_validation_stage.py \
  tests/contract/test_c0_core_contracts.py \
  tests/contract/test_c0_extraction_contracts.py
```
