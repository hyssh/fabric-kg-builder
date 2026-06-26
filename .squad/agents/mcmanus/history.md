# McManus — History

## Core Context

- **Project:** A Python CLI tool that builds and deploys knowledge graphs and Fabric ontologies from documents/CSV using OpenAI enrichment and canonical Parquet.
- **Role:** KG / Ontology Dev
- **Joined:** 2026-06-24T17:38:25.161Z

## Summary

Sprint 1: Ontology compiler (Fabric tree generation), model.yaml design (35 entity types, 36 relationship types), ids.lock.json management. Sprint 2: Bridge validation (10 BRG gates: entity type definitions, relationship properties, search linkage binding, column mapping). Exit code 5 for bridge errors (same as model validation). Pure function design (compile-ontology owns severity policy).

**Key patterns:** Bridge validation runs after full compiler.compile() — tree written regardless, errors surface after. BRG gates cover: entity declarations, relationships, search index record bindings, document chunk linkage, image asset linkage. Warnings (BRG-009) for entities with no outbound bridge edges.

**Verification:** Real model.yaml compiles 0 errors, 9 warnings (expected—partial coverage).

**Tests:** 35 new bridge validation tests (10 gates + real model + CLI integration). **Total:** 529 unit tests passing.

Full history and details in history-archive.md.

---

## 2026-06-24T21:46:59.576-07:00 — DI Layout table approach spec update

**Requested by:** Hyunsuk Shin  
**Decision source:** `.squad/decisions/inbox/coordinator-tables-via-docintel.md`  
**Implementation verified:** `src/fabric_kg_builder/enrichment/docintel_tables.py`

### What changed

Updated three specs to reflect the verified Document Intelligence Layout table approach:

**SPEC-004 (§6.2 + §7.3 + §8.6):**
- §6.2 system prompt: added hard constraint banning `table_row`/`table_cell` emission by LLM.
- §7.3 Table Chunking: replaced old "LLM produces table_row" description with DI Layout pipeline. Added extraction pipeline diagram, per-source mapping table, MS Learn citations, validation proof (Surface PDF → 2 table_html chunks 2026-06-24).
- §8.6 (new section): Table Extraction via Document Intelligence Layout — full DI pipeline, division-of-labor table, MS Learn citations, reference implementations.

**SPEC-002 (§3.3 + §3.4):**
- §3.3 `document_elements`: updated `content_html` Key Notes to flag DI as source; updated `blob_url` note to include `table` type; added provenance callout: `element_type="table"` produced by DI only, `table_row`/`table_cell` schema-level only.
- §3.4 `chunks`: updated `chunk_type`, `content`, `content_html`, `embedding_text`, `blob_url` Key Notes to document DI provenance for `table_html`; added provenance callout.

**SPEC-003 (§12.10 new + §13 revision row):**
- §12.10 (new): Table nodes in the bridge — `evidenced_by` / `shown_in` linkage, graph_path examples, AI Search indexing of tables as independent docs, validation proof.

### Key learnings
- DI Layout `tables[]` cells carry `kind="columnHeader"` — used to split `<thead>`/`<tbody>` in HTML rendering.
- `analyze_result.content` (when `outputContentFormat=markdown`) = whole-document Markdown; tables appear as HTML `<table>` blocks — ideal for semantic chunking of non-table content.
- `table_row` type should remain in the schema for legacy/fallback but must not be produced by the live enrichment pipeline.
- The `canonicalize` drop of LLM `table_row` chunks is the safety net if a model ignores the system prompt constraint.
- Tables as independent AI Search docs (each `table_html` chunk) enables direct table retrieval without re-parsing the source document.

---

## 2026-06-25T00:05:13.466-07:00 — Real Fabric Ontology format + updateDefinition deploy

**Requested by:** Hyunsuk Shin  
**Decision source:** `.squad/decisions/inbox/coordinator-fabric-ontology-real-format.md`  
**Outcome:** 918 tests passing, Fabric graph now POPULATED via updateDefinition.

### Problem solved
The deployed Fabric Ontology item showed EMPTY (Nodes 0, Edges 0). Root cause: old deploy-ontology only created the item shell via POST /items but never called updateDefinition. The compiled build/ontology parts were in our own format (not the EXACT Fabric format), so they couldn't be pushed anyway.

### What was implemented

**1. `src/fabric_kg_builder/ontology/fabric_def.py` (NEW)**
- `build_ontology_parts(workspace_id, lakehouse_item_id, schema='dbo', ontology_name='kg_ontology') -> list[dict]`
- Produces exactly 6 REST parts in the REAL Fabric format decoded from on_finance:
  - `definition.json` → `{}`
  - `.platform` → Ontology metadata (type, displayName, version 2.0, logicalId all-zeros)
  - `EntityTypes/{entityTypeId}/definition.json` → KGEntity (4 props: entity_id/entity_type/display_name/canonical_key, all String)
  - `EntityTypes/{entityTypeId}/DataBindings/{guid}.json` → binds dbo.entities
  - `RelationshipTypes/{relTypeId}/definition.json` → related_to (source=KGEntity, target=KGEntity)
  - `RelationshipTypes/{relTypeId}/Contextualizations/{guid}.json` → binds dbo.relationships (source_entity_id→entity_id, target_entity_id→entity_id)
- BigInt IDs: SHA-256 → 8 bytes → mod 2^62 (stable, positive, unique)
- DataBinding/Contextualization IDs: deterministic UUIDv5

**2. `src/fabric_kg_builder/deploy/fabric_ontology.py` (MODIFIED)**
- Added `update_ontology_definition(workspace_id, ontology_item_id, parts, mock=False, token_provider=None) -> dict`
- Base64-encodes each part's `payload_json` dict → JSON string → base64
- POSTs `{"definition":{"parts":[{path, payload, payloadType:"InlineBase64"}]}}` to `/updateDefinition`
- Handles 200 (sync OK) + 202 (LRO), returns `{parts_count, status, note}`
- Updated `_NOTE_DEFINITION_API` to reflect the new updateDefinition approach

**3. `src/fabric_kg_builder/cli/deploy_cmd.py` (MODIFIED)**
- `deploy_ontology_cmd` completely rewritten:
  - No longer loads from `build_dir` (compile artifact is separate concern)
  - Uses `_read_fabric_env_config` to get workspace_id + lakehouse_item_id + schema_name
  - Calls `build_ontology_parts()` → `create_or_get_ontology_item()` → `update_ontology_definition()`
  - Mock mode: builds parts, logs all 6 paths, no network
  - Live mode: creates/gets item, resolves LRO placeholder if needed, then updateDefinition
  - Reports entity_type_names + rel_type_names in output

**4. `tests/unit/test_fabric_def.py` (NEW)**
- 55 tests covering: part structure (6 parts, correct paths), KGEntity definition (4 props, String type, entityIdParts, displayNamePropertyId), DataBinding (dbo.entities, LakehouseTable, 4 propertyBindings), RelationshipType (related_to, source/target=KGEntity), Contextualization (dbo.relationships, source_entity_id/target_entity_id), BigInt stability + distinctness, mock/live updateDefinition.

**5. `tests/unit/test_deploy_ontology_cmd.py` (MODIFIED)**
- Removed compile-to-dir dependency from mock mode tests
- Updated parts_count check to "6" (fixed from dynamic old compiler count)
- Added tests for KGEntity/related_to in output
- Fixed --no-mock tests to also patch `update_ontology_definition`
- Replaced `test_missing_build_dir_exits_one` (no longer valid) with `test_missing_env_config_exits_one`

### Key learnings
- The EXACT Fabric format uses `/item/ontology/` in schema URLs (not `/ontology/` alone). Getting this wrong → API rejects the definition.
- IDs: entity type IDs and property IDs are BigInt strings (large positive integers), NOT UUIDs. DataBinding/Contextualization IDs ARE UUIDs. Using wrong format crashes the API.
- The `logicalId` in `.platform` must be all-zeros UUID for new items; Fabric generates a real one on first deploy.
- `updateDefinition` body structure: `{"definition":{"parts":[...]}}` — the `definition` wrapper is required.
- Deploying to Fabric requires TWO calls: POST /items (create shell) + POST /updateDefinition (populate). The old code only did the first.
- The compile-ontology artifact (build/ontology/) is a separate concern from deploy format. The old format in compiler.py serves compile-ontology; fabric_def.py serves deploy-ontology. Both coexist.
- BigInt IDs must be distinct across entity_type_id, ALL property_ids, and rel_type_id. Verified via test.

