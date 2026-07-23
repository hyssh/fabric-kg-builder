# McManus — History

## Core Context

- **Project:** A Python CLI tool that builds and deploys knowledge graphs and Fabric ontologies from documents/CSV using OpenAI enrichment and canonical Parquet.
- **Role:** KG / Ontology Dev
- **Joined:** 2026-06-24T17:38:25.161Z

## 2026-07-23 — scope/agent-capability: Formal Blocker Revision (Issues #12, #13, #14)

**Branch:** `scope/agent-capability`
**Context:** Verbal was locked out of this revision. McManus independently owns all changes.
**Prior state:** Verbal implemented the scaffolding (184 tests passing, 2 RED). Hockney's review surfaced 2 RED tests and 4 formal blockers requiring this revision.

### Formal Blockers Fixed

**Blocker 1 — `DataAgentRequiredExampleEmpty` escapes uncaught from both CLI paths:**
- Root cause: `graph_few_shots_from_competency_contract` is called inside the grounding `try` block; the `except` clause only caught `(AgentPublicationError, OSError, ValueError)`. The `DataAgentRequiredExampleEmpty` import was deferred AFTER the try/except, making it unreferenceable in the except clause.
- Fix: Added early deferred import of `DataAgentRequiredExampleEmpty` from `knowledge.validation` BEFORE the grounding try block in both `deploy_cmd.py` and `build_deploy_cmd.py`. Added dedicated `except DataAgentRequiredExampleEmpty` clause surfacing `ClickException` (deploy path) / `BuildDeployError` (build-deploy path). Removed the duplicate import from the second deferred block.

**Blocker 2 — `_capability_availability` / `_bd_availability` not wired into graph builders:**
- Both CLI paths built the availability dict but then called `build_graph_source_instructions(semantic_context)` and `build_graph_source_description(semantic_context)` WITHOUT passing `availability=`. The functions accepted the kwarg but received None.
- Fix: Added `availability=_capability_availability or None` / `availability=_bd_availability or None` to both call sites in both CLI files.

**Blocker 3 — Count-only property selection hashes:**
- `compiled_property_selection_hash` and `published_property_selection_hash` were computed as `_canonical_hash({"property_child_count": N})` — a count-only hash that cannot distinguish equal-size different selections.
- Fix: Added `selected_property_ids: list[str]` property to `DataAgentStageSnapshot` in `data_agent.py` — returns sorted canonical property child IDs across all selected elements. Changed hash computation in `agent_validation.py` to `_canonical_hash({"property_ids": snap.selected_property_ids})`.

**Blocker 4 — Compiled property count not validated before draft/published:**
- The three-way property check only compared draft and published against `grounding.expected_property_child_count`. A compiled omission would pass uncaught until draft/publish time.
- Fix: Added compiled property count check in `build_agent_publication_receipt` BEFORE the existing draft/published checks. Raises `AgentPublicationError("DATA_AGENT_PROPERTY_OMITTED", ...)` at the earliest possible boundary.

### Dependency File Determination
`pyproject.toml` and `uv.lock` had accidental `[dependency-groups]` additions from Verbal's implementation (duplicating existing `[project.optional-dependencies]` dev deps). Reverted both to baseline — tests run correctly without them.

### Test Results
- Five-file targeted suite: **202 passed** (184 existing + 18 new regression tests)
- Full suite: **2409 passed, 4 deselected, 5 warnings** (SwigPy deprecation — pre-existing, unrelated)
- Zero new failures introduced.

### New Regression Tests (18 tests in `test_agent_contract_validation.py`)
- `TestPropertySelectionHashContentBased` (5): selected_property_ids sorted, empty, equal-count-different hashes differ, same IDs → same hash, valid sha256 format
- `TestCompiledPropertyOmissionBlocks` (2): compiled omission raises DATA_AGENT_PROPERTY_OMITTED, equal count passes check
- `TestRequiredExampleEmptyBoundaryClassification` (4): importable, not OSError, not ValueError, actionable str repr, structured attrs
- `TestGraphSourceAvailabilityWiring` (6): instructions/description accept kwarg, unavailable rel named in instructions, available rel named in description, no-availability generic, only-unavailable notes no data

### Key Learnings
- Deferred imports inside CLI functions must be ordered so the earliest `except` clause referencing a type appears AFTER that type's import, even within the same function scope. The pattern "import at function top, use in except" is safer than "import after try block".
- `DataAgentStageSnapshot` is a frozen dataclass with `sources: tuple[dict, ...]` — the `_selected_elements` and `_selected_children` module-private functions are in `data_agent.py` and can be used within the class's own property methods. Adding `selected_property_ids` to the class is the correct extension point for content-based hashing.
- `DataAvailability` schema has strict status-observedRows consistency rules: `unavailable`/`not_observed` requires `observed_rows=None`; `insufficient` requires a set value `< required_rows`; `sufficient` requires a set value `>= required_rows`.
- `build_agent_publication_receipt` is tested indirectly (no direct test exists) — testing it requires constructing fully consistent `DataAgentStageSnapshot` objects with matching instruction hashes, sidecar hashes, and source selection hashes. Using `stage_snapshot_from_spec` + the same spec for all three stages is the cleanest way to get past the earlier checks.
- Always verify `pyproject.toml`/`uv.lock` changes are intentional — accidental dependency-groups additions from uv scaffolding can sneak into diffs.


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


---

## 2026-07-22 — Issues #7 + #8: Ontology Integrity (scope/ontology-integrity)

**Branch:** `scope/ontology-integrity`

### What was implemented

**Issue #7 — Relationship Key Validation (OKV-001)**
- Added `resolve_entity_identity_columns()`, `validate_relationship_keys()`, `get_identity_mappings()` to `compiler.py` — same-table FK mismatch raises `OntologyCompilerError` at compile time.
- Added `read_graph_counts()` to `fabric_ontology.py` — post-deployment node/edge count readback via OneLakeDeltaClient.
- Updated `deploy_cmd.py` to print identity mappings in dry-run, call graph count readback in live path, and detect zero-edge relationship types.
- Updated `compile_ontology_cmd.py` to emit entity + relationship identity mappings in SUMMARY.
- Created `src/fabric_kg_builder/ontology/identity_validation.py` — full OKV-001/OKV-002 public API per Hockney's acceptance test contract.

**Issue #8 — Partial Date Handling (OKV-002)**
- `validate_date_types()` in `compiler.py` — model-level timestamp + datePrecision check.
- `identity_validation.detect_date_precision()` — coarsest-wins classification (YEAR < YEAR_MONTH < FULL_DATE < TIMESTAMP).
- `identity_validation._check_okv002()` — flags `timestamp`-typed properties whose name contains "date".
- `_validate_parquet_date_precision()` in `deploy_cmd.py` — Parquet scan for partial date strings.

**model.yaml changes**
- Added `entity_id` property (type: string, required: true) + `additionalColumns` alias to 14 entity types: Document, Section, Table, TableRow, TableColumn, TableCell, Figure, Caption, Callout, VisualRegion, OCRText, Chunk, ChunkEmbedding, SearchDocument.
- No new Parquet columns; entity_id reads from each entity's existing physical identity column.

### Key decisions
- OKV-001 compatibility: exact FK match OR implied-domain match OR explicit entity_id property.
- 14 entities needed entity_id alias to satisfy OKV-001 (they participate in source_entity_id/target_entity_id FK relationships but lacked entity_id property).
- OKV-002 trigger: type=timestamp + name contains "date". Does not affect created_at/updated_at.
- validate_post_deploy_definition is structural (definition parts), not data row counts.
- identity_validation never raises; returns list[IdentityViolation].

### Tests
- 70 new identity_validation acceptance tests (all pass, Hockney contract).
- 112 existing targeted tests (compiler, deploy_ontology_cmd, bridge_validation, compile_ontology_cmd) continue passing.
