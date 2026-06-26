# Squad Decisions

## Active Decisions

### Lakehouse Lean + Visual Extraction Feedback (2026-06-24T22:30:00-07:00) — Hyunsuk + Squad

#### Coordinator: Lakehouse scope + visual extraction feedback
**Date:** 2026-06-24T22:30:00-07:00  
**By:** Hyunsuk Shin (via Copilot)

**Feedback 1 — Lakehouse should NOT store document text content:**
- AI Search is the home for text search (chunk content, document_elements text, table HTML).
- The Fabric Lakehouse (datalake) is for ONTOLOGY + GRAPH MODEL data — the structured graph (entities, relationships, evidence/provenance keys), NOT heavy text bodies.
- Therefore deploy-lakehouse should write LEAN, graph-centric tables: drop/trim large text columns (chunks.content/content_html/embedding_text, document_elements.content/content_html) from the Lakehouse — those belong in AI Search only.
- Also many sparse/empty columns (col_index, row_index, etc.) are table-HTML concerns that live in AI Search, not the Lakehouse — don't bloat the datalake with empty columns.

**Feedback 2 — visual_assets and visual_regions are EMPTY:**
- The enrich path never actually extracted images/figures → both visual tables are 0 rows.
- Reference implementation to follow: `C:\Users\hyssh\workspace\starbuck-siot-kb` — they extract images/figures as image files and UPLOAD them to Blob for visibility.
- Need to wire real image/figure extraction from PDFs → upload to Blob → populate visual_assets (blob_url) + visual_regions (Doc Intelligence polygons/OCR), so the visual evidence is real.

**Why:** Clean separation (datalake=graph/ontology, AI Search=text/visual retrieval); real visual evidence with blob URLs.

---

#### Fenster: Lakehouse Lean Projection — Implemented
**Date:** 2026-06-24T22:52:55-07:00  
**By:** Fenster (Data Engineer)  
**Requested by:** Hyunsuk Shin  
**Status:** ✅ Implemented

**Decision:** The Fabric Lakehouse receives **graph/ontology data only**. Azure AI Search owns all text retrieval (chunks content, document_elements text/HTML). Deploy-lakehouse must use a lean projection — no bulk text columns, no sparse table-HTML columns.

**Scope:**

| Table | Lakehouse? | Notes |
|---|---|---|
| source_files | ✓ All columns | File provenance — graph root |
| document_elements | ✓ Lean (12 cols) | Structural/graph cols only — content/content_html/row_index/col_index dropped |
| chunks | ✗ **Excluded** | Pure retrieval text → AI Search kg-chunks index |
| entities | ✓ All columns | Graph nodes + ontology bindings |
| relationships | ✓ All columns | Graph edges |
| evidence | ✓ All columns | Provenance links |
| visual_assets | ✓ All columns | Visual ontology assets |
| visual_regions | ✓ All columns | Visual ontology regions |

**document_elements — kept columns:**
`document_element_id`, `source_file_id`, `element_type`, `parent_element_id`, `page_number`, `section_path`, `sort_order`, `table_id`, `figure_id`, `image_id`, `blob_url`, `content_hash`

**Dropped:** `content`, `content_html` (heavy text → AI Search), `row_index`, `col_index` (sparse table-HTML → AI Search)

**Implementation:**
- `LAKEHOUSE_TABLE_PROJECTION` dict constant in `onelake_writer.py` — single source of truth.
- `LAKEHOUSE_TABLES` list exported from same module — derived from projection keys.
- `deploy_parquet_to_onelake(projection=...)` param — defensive column select, skips excluded tables.
- `deploy_cmd.py` imports constants from `onelake_writer`; mock mode reports lean scope; live mode passes projection.

**Test result:** 30 new tests in `tests/unit/test_deploy_lakehouse_projection.py`. All 832 suite tests pass.

**Rationale:** Clean system-of-record separation: Lakehouse = structured/queryable graph model optimized for Spark/SQL analytics on entities, relationships, evidence, ontology. AI Search = full-text + vector retrieval on chunk content and document element text. Lean projection keeps tables small and purpose-clear.

---

#### Verbal: Real visual extraction — visual_assets and visual_regions populated
**Date:** 2026-06-24T22:52:55-07:00  
**By:** Verbal (AI Integration Dev)  
**Requested by:** Hyunsuk Shin  
**Status:** ✅ Implemented (832 unit tests pass)

**Problem:** `visual_assets` and `visual_regions` tables were always 0 rows. The enrich path never physically extracted figure images from PDFs; pdfplumber-based path only handles embedded image streams, not DI-detected figure regions.

**Solution:**

**1. `src/fabric_kg_builder/enrichment/image_extractor.py` — DI figure extraction:**
- `_polygon_to_rect(polygon)` — DI polygon (inches) → `fitz.Rect` (points = in × 72)
- `_render_figure_crop(fdoc, page_number, polygon)` — Render PNG crop at 200 DPI; returns `(bytes, width, height)`
- `_extract_caption(fig)` — Pull caption text from DI figure dict/object
- `extract_figures_from_di(pdf_path, di_analyze_result, source_file_id, *, _fitz_open)` — Iterate DI `.figures`, render crops, dedup by SHA-256 hash → `list[VisualAssetCandidate]`
- `make_visual_regions_for_figure(image_id, candidate, di_analyze_result, *, blob_url, now)` — One `VisualRegionRow` per figure: `region_type="figure_region"`, `polygon_json`, `normalized_polygon_json`, `image_id` FK

Added `polygon: list[float]` field to `VisualAssetCandidate` (backward-compatible default).

**Key design points:**
- DI polygon is in **inches**; multiply ×72 for PyMuPDF points (per reference `starbuck-siot-kb/ingestion/images.py`).
- Zoom = 200/72 for legible crops.
- `_fitz_open` kwarg allows clean unit test injection without module-level patching.
- Dedup by hash: identical crops from the same document produce one asset.

**2. `src/fabric_kg_builder/cli/enrich_cmd.py` — Wire blob uploader + figure extraction:**
- Added `_build_blob_uploader(ctx_obj)` — returns `BlobUploader` or `None` (graceful when `blob.account_name` empty).
- Refactored DI block: `di_analyze_result` assigned outside `try` so it can be reused for figure extraction without a second DI call.
- Added figure extraction block (after table extraction, before LLM enrichment).
- Added `visual_assets` + `visual_regions` keys to canonical JSON output.
- Added `_blob_uploader` injection from `ctx.obj` (mirrors `_di_layout_client` pattern).

**3. Tests — `tests/unit/test_enrich_cmd_visual.py` (20 new tests):**
- Unit tests for `extract_figures_from_di`: produces candidate, carries polygon, empty for no figures, skips bad polygon, deduplicates identical crops, skips out-of-range page.
- Unit tests for `make_visual_regions_for_figure`: one row produced, FK matches, polygon_json present, values in [0,1], caption as text, blob_url inherited.
- Integration tests via CliRunner: canonical JSON has `visual_assets` + `visual_regions`, `image_id` FK resolves, `blob_url` non-empty, caption matches fixture.
- Fallback tests: no blob → empty visual_assets (exit 0); no DI → empty visual_assets (exit 0).

**Decisions:**
- **Single DI call per document:** the `di_analyze_result` from the table extraction step is reused for figure extraction. No second DI call.
- **PDF only:** figure extraction is gated on `src_file.suffix.lower() == ".pdf"`. DOCX handling is deferred.
- **Graceful additive:** both DI and Blob must be configured for visual extraction. Missing either = silent skip, no crash.
- **description field (vision LLM):** left `None` for now; the slot exists in `VisualAssetRow`; a Foundry vision pass can fill it later.
- **Figure region type:** `"figure_region"` — distinct from `"ocr_text"` (paragraph OCR) to allow filtered queries.

**Verification:** 13 figures cropped+uploaded to blob, lean lakehouse deployed (7 tables, chunks excluded, doc text dropped), visual_assets=13 in lakehouse. **832 unit tests passing** (0 failures).

---

### Live Deployment Session (2026-06-24T22:03:53.693-07:00) — Hyunsuk + Squad

#### Coordinator: Live Deployment Results — Schema-Enabled Lakehouse + AI Search
**Date:** 2026-06-24T22:03:53.693-07:00  
**By:** Hyunsuk Shin (via Copilot) — executed by Coordinator

**Lakehouse recreated WITH schema (the user's check):**
- Old flat `kg_lakehouse` (c1a44e9d..., no schema) DELETED.
- New **schema-enabled** `kg_lakehouse` = id **22222222-2222-2222-2222-222222222222**, `defaultSchema=dbo`, created via Fabric REST `enableSchemas=true`. dev.json updated (lakehouse_item_id, onelake paths, sql_endpoint 9b1f7482..., schemas_enabled=true, schema_name=dbo).

**LIVE deployments executed (real cloud, DefaultAzureCredential):**
1. **deploy-lakehouse → OneLake Delta tables (VERIFIED):** all 8 canonical tables written as Delta under `Tables/dbo/` of kg_lakehouse. Read-back confirms: dbo.entities=92, dbo.relationships=33, dbo.chunks=127, dbo.evidence=201 (+ document_elements, source_files, visual_assets, visual_regions). Impl: deltalake write_deltalake, abfss OneLake path, storage.azure.com token, use_fabric_endpoint=true.
2. **deploy-search → Azure AI Search (VERIFIED):** 2 indexes created on example-search (kg-dev-kg-chunks, kg-dev-kg-document-elements), 253 docs uploaded (127+126). Live query "kickstand" returns real Surface kickstand procedure chunks. Impl: REST 2024-07-01 PUT index + batch upload; `_sanitize_for_rest` (semantic prioritizedContentFields/titleField, drop incomplete azureOpenAI vectorizer); `allowUnsafeKeys=true` for ':' in chunk_id keys.

**RBAC assigned (signed-in user):** Cognitive Services User on example-docintell; Search Service Contributor + Search Index Data Contributor on example-search. (OneLake/Fabric access already present.)

**deploy-ontology — finding (NOT yet wired live):** Fabric workspace SUPPORTS a real **Ontology** item type (existing examples: on_retail_dev, on_finance, on_manufacturing, demo_ontology) — creating an Ontology auto-provisions a paired Lakehouse + GraphModel. deploy-ontology currently compiles valid InlineBase64 definition parts and mock-publishes; real Fabric Ontology item creation (preview item API, bind to kg_lakehouse) is the documented next step. Avoided half-building it to prevent orphan items.

**Why:** Schema-enabled Lakehouse for governed table namespace; live KG data + retrieval index in the dev workspace, queryable end-to-end.

---

#### Fenster: Live Deploy Modules — Implemented
**Date:** 2026-06-24  
**By:** Fenster (Data Engineer)  
**Requested by:** Hyunsuk Shin  
**Status:** Implemented, ready for coordinator to trigger live run

**What was decided:** Replace the mock deploy stubs in `deploy-lakehouse` and `deploy-search` with real cloud calls using the verified patterns from the coordinator's live testing.

**Modules created:**
| File | Purpose |
|------|---------|
| `src/fabric_kg_builder/deploy/onelake_writer.py` | Writes Parquet tables as Delta to OneLake via abfss:// + deltalake |
| `src/fabric_kg_builder/deploy/search_deployer.py` | PUTs AI Search index schema + batch-uploads docs via REST 2024-07-01 |

**Flags changed:** Both `deploy-lakehouse` and `deploy-search` now default to **`--no-mock` (LIVE)**.  Pass `--mock` explicitly for dry-run / CI smoke checks.

**Config additions:**
- `FabricConfig.schema_name: str = "dbo"` — read from `fabric.schema_name` in env JSON
- `AiSearchConfig.endpoint: str = ""` — read from `ai_search.endpoint` in env JSON

**Auth pattern:** Both deployers use `DefaultAzureCredential`:
- OneLake: scope `https://storage.azure.com/.default`
- AI Search: scope `https://search.azure.com/.default`

**Live deploy targets (dev):**
| Target | IDs |
|--------|-----|
| Workspace | `11111111-1111-1111-1111-111111111111` |
| Lakehouse | `22222222-2222-2222-2222-222222222222` (schema-enabled, schema=dbo) |
| AI Search endpoint | `https://example-search.search.windows.net` |
| Index prefix | `kg-dev-` |

**Tests:** 34 new unit tests added (all mock — no live calls in tests):
- `tests/unit/test_onelake_writer.py` (12 tests)
- `tests/unit/test_search_deployer.py` (22 tests)

**Total: 779 passing** after all changes.

---

#### Fenster: Azure AI Search Schema REST Sanitization — Implemented
**Date:** 2026-06-24  
**By:** Fenster (Data Engineer)  
**Requested by:** Hyunsuk Shin  
**Status:** Implemented

**Problem:** Live PUT to `example-search` (api-version 2024-07-01) failed with two errors:
1. `"Cannot find nested property 'contentFields' on the resource type 'PrioritizedFields'."`  
   — `prioritizedFields` used the wrong property names (`contentFields`, `keywordsFields`).
2. Incomplete `vectorSearch.vectorizers` entry (kind=azureOpenAI, no parameters) → 400.

**Decision:** Fix both at source (generator) and add a `_sanitize_for_rest` layer in the deployer.

**Changes:**
| File | Change |
|------|--------|
| `src/fabric_kg_builder/cli/compile_search_cmd.py` | `contentFields` → `prioritizedContentFields`, `keywordsFields` → `prioritizedKeywordsFields` in both schema builders; removed `vectorizers` block from kg-chunks |
| `build/search/kg-chunks/index.schema.json` | Same property name fixes; `vectorizers` array removed |
| `build/search/kg-document-elements/index.schema.json` | Property name fixes |
| `src/fabric_kg_builder/deploy/search_deployer.py` | Added `_sanitize_for_rest(schema)` function; wired into `deploy_index` after strip+ensure_vector_search |
| `tests/unit/test_search_deployer.py` | 13 new `TestSanitizeForRest` tests |

**Outcome:** 789 unit tests passing. Sanitized `semantic` section has correct `prioritizedContentFields` / `prioritizedKeywordsFields`. `vectorSearch` has only `algorithms` + `profiles` — ready for live PUT.

---

### Document Intelligence Tables & Enrichment Hardening (Session 2026-06-24) — Hyunsuk + Squad

#### Coordinator: Tables via Document Intelligence Layout (not LLM transcription)
**Date:** 2026-06-24T20:56:07.600-07:00  
**By:** Hyunsuk Shin (via Copilot) — verified against Microsoft Learn (DI v4.0 GA)

**What:**
- STOP asking the LLM to emit `table_row` / `table_cell` records (observed wasteful: null rows/cols, poor fidelity).
- USE Azure AI Document Intelligence **Layout** model as the source of truth for tables:
  - Run Layout with `outputContentFormat=markdown` → document markdown for semantic chunking + structured `tables[]` (cells with row/col index, columnHeader, bounding polygons).
  - Save each table as an INDEPENDENT artifact: `table_{n}.html` (+ optional `.md`), upload to Blob → `blob_url`.
  - Index each table as its own AI Search doc: `chunk_type="table_html"`, `content_html`, `blob_url`, entity-linkage fields.
  - Graph: a `Table`/document-element node carrying `content_html` + `blob_url`, linked via `evidenced_by` / `shown_in` to entities (e.g. a PartNumber cell → Part). Bidirectional graph↔table integration.
- LLM role on tables = SEMANTICS only (table summary, entity linking over the HTML), never structural transcription. Matches SPEC-002 provenance split (DI = geometry/OCR/structure; LLM = semantics).

**Verified (Microsoft Learn, DI v4.0 GA):**
- Layout extracts tables as structured cells (row/col index, columnHeader, bounding polygon). [prebuilt/layout]
- Layout outputs Markdown via `outputContentFormat=markdown`; tables render as HTML `<table>` in markdown. [prebuilt/layout]
- Markdown output is the recommended input for semantic chunking in RAG. [concept/retrieval-augmented-generation]

**Reference repos:** microsoft/Document-Knowledge-Mining-Solution-Accelerator; Azure-Samples/document-intelligence-code-samples; LeDat98/NexusRAG (vector+KG+table captioning).

**Spec impact:** SPEC-004 §6/§8 (chunking + table handling), SPEC-002 §3.2/§3.4 (document_elements/chunks table provenance), SPEC-003 §12 (Table node bridge). INFRA-001 DI already required.

**Why:** Higher-fidelity tables, less LLM waste, tables become independent indexable+graph-linkable artifacts.

---

#### Fenster: DI Layout Table Extraction — Implemented
**Date:** 2026-06-24T21:27:00.000-07:00  
**By:** Fenster (Data Engineer) — requested by Hyunsuk Shin

**Status:** ✅ Complete — 47 new tests, 731 → 745 total passing

**What was implemented:**

**New: `src/fabric_kg_builder/enrichment/docintel_tables.py`**
- `DocIntelTableResult` dataclass: `document_elements`, `chunks`, `html_artifacts` (name→html), `markdown`.
- `table_to_html(table) -> str` — Builds `<table>` HTML from a DI Layout table object/dict.
  - Cells with `kind="columnHeader"` → `<thead>/<th>` rows.
  - All other cells → `<tbody>/<td>` rows organized by `row_index`/`column_index`.
  - Falls back to deriving `column_count` from max cell index when the field is 0/missing.
- `extract_tables(analyze_result, source_file_id, *, section_path, now, sort_order_start) -> DocIntelTableResult`
  - Per table: one `DocumentElementRow` (element_type="table", content_html, blob_url=None) + one `ChunkRow` (chunk_type="table_html", embedding_text).
  - Deterministic IDs via `make_document_element_id` / `make_chunk_id` (SHA-256 of HTML).
  - `blob_url=None` — uploader/Verbal sets this post-upload.
- `write_table_artifacts(html_artifacts, out_dir, source_file_id) -> list[Path]`
  - Writes `{out_dir}/tables/{safe_id}/table_N.html` + companion `table_N.md`.
  - **Windows safety:** colons in `source_file_id` replaced with `_` for valid directory names.
- `get_document_markdown(analyze_result) -> str` — returns `analyze_result.content` (whole-doc Markdown for semantic chunking; Verbal wires chunking pipeline).

**Extended: `src/fabric_kg_builder/enrichment/docintel.py`**
- `DocIntelClient.analyze_document_bytes` + `analyze_document_url` gain optional `output_content_format: str | None` kwarg.
- When set, forwarded to `begin_analyze_document(output_content_format=...)`.
- When `None`, kwarg is omitted entirely (preserving existing SDK call shape).
- SDK note in docstring: verify `output_content_format` parameter name against current azure-ai-documentintelligence SDK version (≥ 1.0.0 GA).

**New fixture: `tests/fixtures/document_intelligence/analyze_result_tables.json`**
- 3-row parts table: header (Part | Part Number | Quantity) + data rows (Battery | M1287099-003 | 1) + (Display | M1234567-001 | 1).
- All cells on page 1 with bounding_regions.

**Extended: `tests/conftest.py`**
- Added `make_document_intelligence_client_with_tables()` factory + `mock_document_intelligence_client_with_tables` fixture.
- Exported in `__all__`.

**New: `tests/unit/test_docintel_tables.py`**
- 47 unit tests, 100% mocked DI — zero live calls.
- Covers: HTML structure, thead/tbody separation, cell values, deterministic IDs, FK alignment, page number, section_path, write artifacts (html+md), Windows-safe dir names, empty tables, multiple tables, markdown passthrough, output_content_format kwarg forwarding/omission, conftest fixture integration.

**Design alignment:**
- SPEC-002 §3.3/§3.4: DocumentElementRow element_type="table" + ChunkRow chunk_type="table_html".
- SPEC-004 §8: DI = structure/OCR; LLM = semantics only (no table_row/table_cell from LLM).
- coordinator-tables-via-docintel: tables as independent indexable+graph-linkable artifacts.
- IDs follow SPEC-002 §5.3/§5.4 (SHA-256, deterministic, prefixed).

**Windows gotcha documented:** `source_file_id` contains `:` → sanitize to `_` for `mkdir` (WinError 267 on colons in directory names).

---

#### Verbal: DI Table Wire into Document Enrich Path
**Date:** 2026-06-24T21:35:00.000-07:00  
**By:** Verbal (AI Integration Dev) — requested by Hyunsuk Shin

**What was done:**

1. **Stop LLM table transcription** (`orchestrator.py`):
   - `_ENRICH_SYSTEM_PROMPT` updated to explicitly instruct: do NOT emit `table_row`/`table_cell` chunks; table structure comes from Document Intelligence, not LLM transcription. LLM may still summarize a table as `section_text` or link entities to it.
   - `canonicalize_llm_output` now drops any LLM-emitted chunk whose `effective_chunk_type == "table_row"` (with a `_log.warning`). `table_html` from the LLM is not explicitly blocked (edge case: LLM may legitimately summarize as HTML), but DI's `table_html` chunks are the authoritative ones injected outside canonicalize.

2. **Wire DI tables into document enrich path** (`docintel.py` + `enrich_cmd.py`):
   - Added `DocIntelClient.layout_analyze_raw(data: bytes) -> Any` — calls `begin_analyze_document` with `output_content_format="markdown"` and returns the raw `AnalyzeResult` (not mapped to VisualRegionRow). Feed directly to `extract_tables()`.
   - Added `_build_di_layout_client(ctx_obj)` in `enrich_cmd.py` — returns a `DocIntelClient` when `document_intelligence.endpoint` is configured, else `None` (never raises).
   - `_enrich_document_file` now accepts `di_layout_client=None`. When provided: reads PDF bytes, calls `layout_analyze_raw`, calls `extract_tables(analyze_result, source_file_id)`, merges `di_result.document_elements` + `di_result.chunks` into the canonical JSON output alongside text chunks. When `None`: silently skipped (graceful fallback).
   - `enrich_cmd` resolves `di_layout_client` from `ctx.obj["_di_layout_client"]` (test injection) or builds it from config; passes it to `_enrich_document_file`.

3. **DI table chunks** (`chunk_type="table_html"`, `content_html` set) are included in the `chunks` array of `_canonical.json` and are thus eligible for AI Search compile-search path via the existing chunks table.

4. **Tests** (`tests/unit/test_enrich_cmd_di_tables.py`, 6 new tests):
   - `test_system_prompt_forbids_table_row_transcription` — asserts `_ENRICH_SYSTEM_PROMPT` mentions `table_row` + `Document Intelligence`.
   - `test_canonicalize_drops_llm_table_row_chunk` — LLM `table_row` dropped; `section_text` survives.
   - `test_di_table_wire_produces_table_html_chunk` — mock DI → `table_html` chunk in canonical JSON.
   - `test_di_table_wire_produces_table_document_element` — mock DI → table `document_element` in canonical JSON.
   - `test_di_table_chunk_has_content_html` — `content_html` contains `<table>`.
   - `test_di_not_configured_pipeline_still_works` — `di_layout_client=None` → exit 0, no `table_html` chunks.
   - `test_orchestrator.py::test_chunk_row_produced` — updated from "table_row chunk survives" to "table_row chunk is dropped" (old behavior was wrong per new spec).

**Files changed:**
- `src/fabric_kg_builder/enrichment/orchestrator.py` — prompt update + table_row drop
- `src/fabric_kg_builder/enrichment/docintel.py` — `layout_analyze_raw()` method added
- `src/fabric_kg_builder/cli/enrich_cmd.py` — `_build_di_layout_client`, `_enrich_document_file` DI wire, `enrich_cmd` DI client injection
- `tests/unit/test_enrich_cmd_di_tables.py` — new test file (6 tests)
- `tests/unit/test_orchestrator.py` — updated `test_chunk_row_produced` to assert new drop behavior

**pytest result:** 737 passed, 4 deselected (was 731 + 6 new = 737).

**Design decisions:**
- DI client injected via `ctx.obj["_di_layout_client"]` (mirrors existing `_foundry_client` pattern for clean test injection without monkeypatching).
- `layout_analyze_raw` does NOT call `map_di_result_to_visual_regions` — that mapping is for OCR/polygon work, not table extraction. Keeping them separate respects the SPEC-002 §3.2/§3.4 provenance split.
- `table_html` from the LLM is not explicitly blocked (the prompt says "don't do this", and DI tables are injected outside canonicalize as authoritative). Low-trust LLM table_html would be a duplicate at worst.
- Markdown semantic chunking (SPEC point 3) left as future opt-in: `di_result.markdown` is available on `DocIntelTableResult` but not yet passed to the Chunker. This keeps the PR minimal and non-breaking.

---

#### McManus: Spec updates — DI Layout table approach applied to SPEC-002/003/004
**Date:** 2026-06-24T21:46:59.576-07:00  
**By:** McManus (KG/Ontology Dev)  
**Requested by:** Hyunsuk Shin

**What was updated**

| Spec | Sections | Change |
|---|---|---|
| SPEC-004 | §6.2, §7.3, §8.6 (new) | DI Layout = source of truth for tables; LLM bans table_row/cell; full extraction pipeline documented |
| SPEC-002 | §3.3, §3.4 | Provenance notes: DI = structure+HTML, LLM = semantics only; table_row/cell schema-level only |
| SPEC-003 | §12.10 (new), §13 | Table nodes in bridge: evidenced_by/shown_in, independent AI Search docs, validation proof |

**Summary of changes**

**SPEC-004 §6.2 — System prompt constraint added**
```
Do not emit table_row or table_cell records — table structure and HTML are extracted by
Azure AI Document Intelligence Layout before the LLM pass. Your role on tables is
SEMANTICS ONLY: summarize, extract entities, and link evidence from the provided HTML.
```

**SPEC-004 §7.3 — Table Chunking rewritten (DI Layout pipeline)**
- Tables are extracted by DI Layout (`outputContentFormat=markdown`), not the LLM.
- DI produces `tables[]` (cells: row_index, col_index, kind=columnHeader, bounding_polygon) + whole-doc Markdown.
- `docintel_tables.extract_tables()` produces: `document_element` (element_type="table", content_html) + `chunk` (chunk_type="table_html") + artifact `table_{n}.html`.
- LLM: P7 summary + P2/P5 entity linking over HTML only.
- `canonicalize` drops any LLM-emitted `table_row` chunks.
- MS Learn cited: prebuilt/layout (Markdown + tables[]) + concept/retrieval-augmented-generation (semantic chunking).

**SPEC-004 §8.6 — New section: Table Extraction via DI Layout**
Full pipeline diagram, division-of-labor table, validation proof, reference repos.

**SPEC-002 §3.3 / §3.4 — Provenance notes**
- `element_type="table"`: DI = content_html + blob_url; LLM = semantics only.
- `chunk_type="table_html"`: DI = structure + HTML; LLM = embedding_text (P7) + related_entity_ids (P2/P5).
- `table_row` / `table_cell`: schema-level only, not produced by pipeline.

**SPEC-003 §12.10 — Table nodes in the bridge**
- `evidenced_by`: entity → `DocumentChunk` (table_html) → indexed as independent AI Search doc.
- `shown_in`: entity → `Table` document-element node (content_html, blob_url) → direct artifact retrieval.
- Each table = independent, retrievable, graph-linkable artifact.

**Validation proof**
Real DI Layout on a Surface PDF yielded **2 tables → 2 `table_html` chunks** (2026-06-24).  
Reference implementations: `microsoft/Document-Knowledge-Mining-Solution-Accelerator`, `Azure-Samples/document-intelligence-code-samples`.

**MS Learn references**
- [Azure AI Document Intelligence — Layout model](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/layout): `outputContentFormat=markdown`; `tables[]` with `row_index`, `col_index`, `columnHeader`, `bounding_regions`.
- [Document Intelligence for RAG](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/retrieval-augmented-generation): Markdown output recommended for semantic chunking in RAG pipelines.

---

#### Hockney: Test Tiers — Fast by Default + Integration Opt-In
**Date:** 2026-06-24  
**By:** Hockney (Test Engineer)  
**Requested by:** Hyunsuk Shin  
**Status:** Accepted

**Context**
The unit suite (~745 tests post-table-work) runs in ~18–20s. Integration tests that parse real Surface PDFs (multi-MB) are slow and were included in the default `pytest` run and in the CI merge gate. As the `sample_data/` corpus grows this will make both local dev and CI painful.

**Decision**
Three test tiers with explicit marker contracts:

| Tier | Markers | Default? | CI gate? |
|------|---------|----------|----------|
| Fast unit | `unit`, `contract` (no marker = fast) | **Yes** | **Yes** |
| Integration / real PDF | `integration` + `slow` | No (opt-in) | No (separate job) |
| Smoke | `smoke` | No | No |

**Key choices**
1. **`addopts = "-m 'not slow and not integration'"` in `pyproject.toml`** — bare `pytest` always runs fast. No per-developer config needed.
2. **Both `@pytest.mark.integration` AND `@pytest.mark.slow` on real-PDF tests** — users can opt in by either; they are complementary.
3. **Golden fixture** (`tests/fixtures/golden/surface_mini_canonical.json`) — a hand-crafted trimmed canonical record (2 entities, 1 rel, 1 chunk, 1 evidence) keeps the compile-data + data-gates code path covered in ~2s without live PDFs.
4. **CI merge gate is fast-only** — `pytest tests/unit tests/contract -m "not slow and not integration"`. Integration job is `workflow_dispatch` only, `continue-on-error: true`.
5. **`pytest.skip()` in test body** — real-PDF tests skip gracefully when `sample_data/` is absent (fresh clone, CI). Never fail with FileNotFoundError.

**Alternatives Considered**
- **Keep integration in merge gate** — rejected; will get progressively slower as the corpus grows.
- **Separate test directory for slow tests** — rejected; would require moving existing tests and breaking existing file organisation. Marker-based filtering is less disruptive.

**Files Changed**
| File | Change |
|------|--------|
| `pyproject.toml` | Added `slow` marker; updated `addopts` to exclude `slow` + `integration` |
| `tests/unit/test_extractors.py` | Added `@pytest.mark.slow` to real-PDF integration test |
| `tests/unit/test_inspect_cmd_pdf.py` | Added `@pytest.mark.slow` to real-PDF integration tests |
| `tests/fixtures/golden/surface_mini_canonical.json` | New golden fixture |
| `tests/unit/test_golden_canonical.py` | New 10-test golden fixture test file |
| `.github/workflows/ci.yml` | Fast-only merge gate; separate integration job |
| `docs/TEST-STRATEGY.md` | New — documents the three tiers |

**Verification**
```
python -m pytest -q   # 745 passed, 4 deselected, ~18–20s
python -m pytest -m integration --collect-only  # 4 tests collected
python -m pytest tests/unit/test_golden_canonical.py  # 10 passed, 1.7s
```

---

### Architecture & CLI (SPEC-001) — Keyser

**Date:** 2026-06-24T12:42:17.255-07:00  
**Spec:** `docs/specs/SPEC-001-architecture-and-cli.md` (v4)  
**Infra doc:** `docs/infra/INFRA-001-azure-resources.md`

**Locked Choices:**

1. **CLI framework:** Click 8.x (mature, no typing-extensions conflicts, better for complex option groups).
2. **Pipeline stages (11):** domain-intake → inspect-source → enrich → compile-data → compile-ontology → compile-search → package → deploy-lakehouse → deploy-ontology → deploy-search → validate.
3. **Package layout:** 10 modules under `src/fabric_kg_builder/` — cli, config, sources, enrichment, model, parquet, ontology, search, deploy, validate.
4. **Entity/relationship ID generation:** Content-addressed SHA-256 hash (deterministic, collision-resistant). Ontology type IDs from `ids.lock.json`.
5. **Checkpoint strategy:** Per-source-file JSON checkpoints for LLM enrichment. `--resume` continues; `--force` restarts.
6. **Configuration split:** `fabric-kg.yaml` (non-secret, env-interpolated) + `.env` (secrets, gitignored). Precedence: CLI flag > env var > yaml > default.
7. **Environment isolation:** Per-env JSON in `ontology/environments/{dev,test,prod}.json`. Only workspace/lakehouse/blob/search IDs vary; model and ontology definitions are stable.
8. **AI Search:** Optional and disabled by default. Entire search pipeline is no-op when disabled.
9. **Auth:** DefaultAzureCredential for all Azure services except OpenAI/Foundry (API key).
10. **LLM SDK:** Microsoft Foundry SDK (`azure-ai-projects`) replaces raw OpenAI SDK. Foundry endpoint + project in yaml; keys in .env.
11. **Document Intelligence:** REQUIRED dependency (not deferred). Azure AI Document Intelligence handles OCR text extraction and bounding polygon/callout detection for visual_regions.

**CLI Commands (Canonical):**

```
set-domain --prompt "..."                    # persist domain brief
enrich --domain-prompt "..." | --domain-file <path>  # supply inline or from file
compile-search                               # NOT compile-search-index
deploy-lakehouse                             # NOT deploy-data
```

**Config Format (Canonical):**

```yaml
# fabric-kg.yaml (non-secret, env-interpolated):
foundry:
  endpoint: ${AZURE_AI_FOUNDRY_ENDPOINT}
  project: example-project
enrichment:
  chat_deployment: chat
  embedding_deployment: embedding
  embedding_dimensions: 1536
  vision_deployment: chat  # default: multimodal chat; alternative: example-vision/gpt-4o
```

```bash
# .env (secrets, gitignored):
AZURE_AI_FOUNDRY_ENDPOINT=https://...
AZURE_AI_FOUNDRY_API_KEY=...
AZURE_DOCINTEL_ENDPOINT=...
AZURE_DOCINTEL_API_KEY=...
AZURE_SEARCH_API_KEY=...
AZURE_BLOB_*=...
```

**Domain Intake Security Requirement:**

User-supplied domain text is injected ONLY into the LLM **user prompt** (message role = `user`), NEVER into the system prompt. This prevents prompt injection from user-supplied text reaching the system instruction layer.

---

### Canonical Data Model (SPEC-002) — Fenster

**Date:** 2026-06-24T12:42:17.255-07:00  
**Spec:** `docs/specs/SPEC-002-canonical-data-model.md` (v1.2)  
**Research basis:** `docs/research/RESEARCH-001-kg-aisearch-grounding.md`

**Decision 1: All Data Row IDs Are Deterministic SHA-256 Hashes**

Data row IDs (`entity_id`, `chunk_id`, `relationship_id`, `evidence_id`, `image_id`, `visual_region_id`, `document_element_id`, `source_file_id`) are derived as `{prefix}:{sha256(canonical_string)[:32]}`. Guarantees cross-environment reproducibility, natural dedup, and incremental re-run detection.

**Decision 2: Entity Canonical Key Normalization is Pinned**

The `canonical_key` rule: lowercase → strip → collapse whitespace → remove non-alphanumeric except `-` → replace spaces with `-` → prepend `entity_type.lower():`. This rule is stable; changes require a spec version bump. Any normalization change invalidates existing entity_ids and requires full re-extraction.

**Decision 3: list<string> Columns Are Native Parquet Arrays**

`aliases` (entities) and `related_entity_ids` (chunks) are written as native pyarrow `pa.list_(pa.string())`, not JSON-encoded strings. Enables Fabric Lakehouse and Spark to read directly as `ArrayType(StringType)`.

**Decision 4: Placeholder Parquet Uses Subdirectory Layout**

Placeholder files → `build/parquet/{table}/_placeholder.parquet`. Real data → `build/parquet/{table}.parquet` (single file). The two layouts are mutually exclusive.

**Decision 5: Validation Severity Split — 22 Fail / 8 Warn**

FK violations, duplicate primary keys, schema mismatches, missing blob_urls (post-upload) = **fail**. Orphan nodes, empty evidence, missing cross-links = **warn**.

**Decision 6: Graph-to-Search Bridge — Denormalized Fields**

Two new columns support agent-orchestrated graph→AI Search path:

| Column | Table | Type | Purpose |
|---|---|---|---|
| `search_aliases` | `entities` | `pa.list_(pa.string())` | Flattened list: `[canonical_key, display_name.lower(), *aliases_lowercased]` |
| `entity_search_keys` | `chunks` | `pa.list_(pa.string())` | Flattened `search_aliases` of all entities in `related_entity_ids` |

**Linkage guarantee:**
- Entity → Chunks (Lakehouse): `array_contains(chunks.related_entity_ids, entity_id)`
- Entity → AI Search: GQL returns `canonical_keys`/`search_aliases` → AI Search OData filter on `chunks.entity_search_keys/any(k: k eq '...')`
- Chunk → Entity: Unnest `chunks.related_entity_ids` → join `entities.entity_id`
- Visual evidence: `visual_regions.identified_entity_id` → `evidence.visual_region_id` → `chunks.chunk_id`

**Decision 7: Structured Tables Are Lakehouse-Only**

Canonical structured tables (`entities`, `relationships`, `evidence`, `source_files`, `document_elements`, `visual_regions`) land **exclusively** in Fabric Lakehouse. Never pushed to Azure AI Search. Prevents dual authoritative stores that drift.

**AI Search-eligible content:**
- `chunks`: `content`, `embedding_text`, `related_entity_ids`, `entity_search_keys`, `blob_url`
- `visual_assets`: `description`, `caption`, `blob_url`
- `document_elements`: `content`/`content_html` for table type only

**Decision 8: Document Intelligence Required for Visual Regions**

Azure AI Document Intelligence is a **required** (not optional) infrastructure dependency. Provides:
- `polygon_json`: bounding polygons via Layout/Read API
- `text`: OCR text from `content` field
- `region_type` = `ocr_text`, `table_region`: structural analysis

Vision LLMs provide semantic classification (`label`, `identified_entity_id`, confidence) but cannot reliably produce pixel-accurate bounding polygons.

**Open Questions:**

1. Should `entity_search_keys` be updated incrementally or always full-rebuild?
2. Should `search_aliases` normalization strip punctuation for broader fuzzy matching?
3. For visual_regions: separate `ocr_confidence` + `llm_confidence` or single merged `confidence`? (Verbal/SPEC-004 to decide)

---

### Ontology & Deployment (SPEC-003) — McManus

**Date:** 2026-06-24T12:42:17.255-07:00  
**Spec:** `docs/specs/SPEC-003-ontology-and-deployment.md` (v2)  
**Bridge basis:** `docs/research/RESEARCH-001-kg-aisearch-grounding.md`

**Decision 1: blob_url is a node property, not a relationship edge**

The `blob_url` is a property of type `blob_url` (format `uri`) on `ImageAsset`, `Figure`, and `VisualRegion` — not a separate relationship type edge. Properties are the correct mechanism for URI-typed scalar values.

**Decision 2: Inverse relationships are explicit and enumerated**

Every relationship type in `model.yaml` declares `inversePolicy` as one of `none | materialize | alias`. Compiler fails if absent. Prevents silent loss of traversal direction.

**Decision 3: Environment config injected at compile time**

The `compile-ontology --env {env}` command bakes the Lakehouse ID from `ontology/environments/{env}.json` into data binding JSON files. `deploy-ontology` deploys pre-baked artifacts without patching. Self-contained per-environment artifacts are easier to inspect, diff, and version.

**Decision 4: ID ranges are prefix-based**

Entity type IDs start with `1`, relationship type IDs start with `2`. Hundreds column as loose module grouping (1…99 = support-domain, 100…199 = document-evidence, 200…299 = retrieval), but convention not enforced.

**Decision 5: Graph-to-Search Bridge — Canonical Columns (SPEC-002 Binding)**

**Source columns (SPEC-002):**
- `chunks.related_entity_ids`, `chunks.entity_search_keys`
- `entities.canonical_key`, `entities.search_aliases`

**AI Search index fields (derived at build time):**
- `entity_ids` (filterable)
- `canonical_key` (filterable)
- `entity_aliases` (searchable)
- `graph_path` (searchable)
- `blob_url` (filterable)

**Filter rule:** `search.in(entity_ids, '...', ',')` on stable IDs; aliases → keyword `search` param; `vectorFilterMode: preFilter`. Never `or`-chains or `any(... eq ...)` for ID lists.

**Open Questions:**

1. Should compiler support `--dry-run` flag?
2. Should `build/ontology/` be committed or treated as generated-only?
3. Should Fabric sensitivity label GUID be stored in `env.json` or resolved at deploy time?

---

### Environment Config — Dev/Test/Prod (McManus)

**Date:** 2026-06-24T13:14:20-07:00  
**Status:** Implemented (dev), Placeholder (test/prod)  
**Location:** `ontology/environments/{dev,test,prod}.json`

**Created Files:**

| File | Purpose | Status |
|---|---|---|
| `ontology/environments/dev.json` | Concrete dev config — verified INFRA-001 resources | ✅ Implemented |
| `ontology/environments/test.json` | Template — all keys reserved, values TBD | 🔲 Placeholder |
| `ontology/environments/prod.json` | Template — all keys reserved, values TBD | 🔲 Placeholder |

**Schema Decisions:**

1. **snake_case** field names (not camelCase) — consistent with Python conventions and SPEC-001
2. **Nested sections** per service (fabric, blob_storage, ai_search, foundry, document_intelligence, vision, key_vault) — keeps related fields together
3. **Lakehouse item ID** is `"<dev-lakehouse-item-id>"` in dev.json (not yet provisioned; INFRA action item)
4. **Foundry endpoint format:** `https://<account>.services.ai.azure.com/api/projects/<project>` — must verify in Azure AI Foundry portal
5. **embedding_dimensions** (1536) lives here alongside `embedding_deployment` — coupling constant to AI Search vector field width
6. **Sensitivity label** stored as display name (not GUID) — resolved at deploy time via Fabric admin API
7. **No secrets** in these files — API keys, connection strings, SAS tokens come from `.env`

**Open Items:**

| # | Item | Owner |
|---|---|---|
| 1 | Populate `fabric.lakehouse_item_id` in `dev.json` | Infra / Hyunsuk |
| 2 | Verify Foundry endpoint URL (`https://example-aiservices.services.ai.azure.com/api/projects/example-project`) | McManus / Hyunsuk |
| 3 | Verify exact endpoints for `example-docintell` and `example-vision` | McManus / Hyunsuk |
| 4 | Confirm `kg-assets` is the correct container name in `examplestorageacct` | Hyunsuk |
| 5 | Set `fabric.sensitivity_label` in `dev.json` once org policy confirmed | McManus |
| 6 | Populate placeholders in `test.json` and `prod.json` | Future sprint |

---

### LLM Enrichment (SPEC-004) — Verbal

**Date:** 2026-06-24T12:42:17.255-07:00  
**Spec:** `docs/specs/SPEC-004-llm-enrichment.md` (v2)  
**Research basis:** `docs/research/RESEARCH-001-kg-aisearch-grounding.md`

**Decision D1: LLM output is intermediate JSON only**

LLM output never writes Parquet, AI Search, or ontology definitions directly. All LLM output → structured intermediate JSON in `build/enriched/`. Canonicalize step (not LLM) resolves id_hints to stable IDs, applies confidence thresholds.

**Decision D2: id_hint → stable ID hashing owned by Fenster**

Exact stable ID hashing algorithm (SHA256 prefix, UUID5, sequential lock) is Fenster's decision (SPEC-002). SPEC-004 defines handoff contract: canonicalize receives id_hint + source_file_id + type, returns stable string ID.

**Decision D3: Blob URLs are runner-injected only**

LLM must never generate, invent, or modify Blob URLs. Runner uploads images/figures to Blob Storage before calling LLM, then injects URL into prompt context. LLM echoes it back unchanged.

**Decision D4: Confidence thresholds (configurable defaults)**

- Include in active Parquet: >= 0.70
- Include as flagged/low-confidence: 0.50–0.69
- Drop silently: < 0.50

All configurable via `config/enrich.yaml`.

**Decision D5: LLM SDK = Microsoft Foundry SDK**

Raw OpenAI SDK replaced by **Microsoft Foundry SDK** (`azure-ai-projects` / `azure-ai-inference`). Client initialization uses `AIProjectClient` with Foundry project endpoint from `fabric-kg.yaml`.

**Auth options supported:**
- API key (env var: `AZURE_AI_FOUNDRY_API_KEY`)
- DefaultAzureCredential (dev: `az login`, prod: Service Principal)

**Decision D6: Model defaults locked**

- **Chat/enrichment:** GPT-5.5-mini (target) / gpt-4.1 (interim dev)
- **Embedding:** text-embedding-3-large @ dimensions=1536 (fallback: small@1536)
- **Vision:** Default = chat deployment (multimodal; gpt-4.1 interim / GPT-5.5-mini target); Alternative = `example-vision` / gpt-4o

All configurable via `fabric-kg.yaml` + `.env`.

**Decision D7: CLI contract coordination with Keyser**

The `fabric-kg enrich` command signature in SPEC-004 is a proposal. Keyser owns CLI contract. If SPEC-001 contradicts any flag or output path here, SPEC-001 takes precedence.

**Decision D8: JSON Schema validation file required**

`config/schemas/llm-intermediate.schema.json` must exist before first `enrich` command implementation is merged.

**Open Questions from PRD §26:**

| Q# | Question | Blocked Work |
|---|---|---|
| Q6 | Text enrichment model? | §8.1 default (locked: GPT-5.5-mini target / gpt-4.1 interim) |
| Q7 | Vision model? | §8.1 default (locked: chat deployment multimodal) |
| Q11 | Checkpoint design for large jobs? | §9.4 (per-file atomic writes) |

---

### Test Plan & Validation Gates (SPEC-005) — Hockney

**Date:** 2026-06-24T12:42:17.255-07:00  
**Spec:** `docs/specs/SPEC-005-validation-and-test-plan.md` (v3)  
**Status:** Final (79,845 bytes)

**Decision 1: VAL rule numbering is stable**

Rule IDs are never reused or renumbered. Retired rules marked deprecated (not deleted) for traceability in old log files.

**Decision 2: Severity split — 21 Fail / 1 Warn**

- 21 rules: `build-fail` (schema, FK, dedup, blobs, confidence, etc.)
- 1 rule (VAL-022 env drift): `warn` (informational, blocks nothing)

**Decision 3: No live API calls in unit, contract, or integration tests**

OpenAI, Azure Blob Storage, Fabric REST always mocked. Hard rule, not preference.

**Decision 4: Fixture locations are fixed**

All fixtures live under `tests/fixtures/`. No test may create ad-hoc data inline that is not committed as named fixture.

**Decision 5: 90% coverage gate**

90% line coverage floor is a merge gate. Applies to `src/fabric_kg_builder/` only. Test code excluded.

**Decision 6: Smoke tests are not merge gates**

Smoke tests run post-deploy to dev. Failing smoke test generates alert but does not roll back.

**Decision 7: Graph-to-Search contract fixture — canonical form**

Graph-to-Search validation tests use `search.in(entity_ids, 'e2e:component:battery', ',')` as the filter. The `entity_ids/any(id: id eq ...)` OData chain is **explicitly prohibited** (CRITICAL finding #4).

**Open Questions:**

1. Should VAL-022 (env drift) ever be promoted to `build-fail`? Define allowed delta threshold explicitly.
2. What is the exact token limit for LLM context overflow check? (Needed from AI engineer's model selection)
3. Fabric REST mock in integration tests needs canned response shape. (Whoever writes deploy module supplies fixture format.)

---

### Infrastructure & Azure Resources (INFRA-001) — Keyser / Hyunsuk

**Date:** 2026-06-24T12:39:10.117-07:00  
**Status:** Verified (Dev/Test), Action Items (Prod)  
**Doc:** `docs/infra/INFRA-001-azure-resources.md` (new)

**Dev/Test Inventory (Verified — Example-Subscription):**

**Subscription:** `00000000-0000-0000-0000-000000000000` (Example-Subscription)  
**Resource group:** `example-rg`  
**Fabric workspace:** `11111111-1111-1111-1111-111111111111`

| Need | Resource | Type | Region | Status |
|---|---|---|---|---|
| Microsoft Foundry (LLM+embeddings) | `example-aiservices` + project `example-project` | CognitiveServices | eastus2 | ✅ present |
| Blob (visual assets) | `examplestorageacct` | Storage | eastus2 | ✅ present |
| Azure AI Search (text/visual) | `example-search` | Search/searchServices | swedencentral | ✅ present |
| Document Intelligence (REQUIRED) | `example-docintell` | CognitiveServices | westus3 | ✅ present |
| Vision | `example-vision` | CognitiveServices | swedencentral | ✅ present |
| Secrets store | `example-kv` | KeyVault | eastus2 | ✅ present (use .env for app secrets) |
| API gateway (optional) | `exp-demo-apim` | ApiManagement | westus2 | ✅ optional |
| Knowledge/bot agent | `fsi-iq-knowledge-agent90830` | BotService | global | ✅ existing demo |

**Foundry Model Deployments on `example-aiservices`:**

| Deployment | Model | Version | SKU |
|---|---|---|---|
| `chat` | gpt-4.1 | 2025-04-14 | Standard |
| `embedding` | **text-embedding-3-large** | 1 | GlobalStandard |
| `model-router` | model-router | 2025-11-18 | GlobalStandard |
| `gpt-4o` | gpt-4o | 2024-11-20 | GlobalStandard |

**Gaps vs Locked Decisions:**

- ✅ Embedding default (text-embedding-3-large) already deployed. Set `dimensions=1536` at call time.
- ⚠️ **GPT-5.5-mini NOT deployed.** Available chat models: gpt-4.1, gpt-4o, model-router. **Action:** Deploy `gpt-5.5-mini` to `example-aiservices` OR use `gpt-4.1` as interim dev default until 5.5-mini added.

**Model Defaults Locked:**

| # | Decision | Rationale |
|---|---|---|
| 1 | Chat/enrichment: GPT-5.5-mini (target); interim dev: gpt-4.1 | 5.5-mini not yet deployed; gpt-4.1 suitable for dev/test |
| 2 | Embedding: text-embedding-3-large @ 1536 dimensions | Balances quality & cost; fallback text-embedding-3-small@1536 if needed |
| 3 | Embedding dimension (1536) couples to AI Search `chunk_vector` field | Changing requires full reindex; coupling documented in SPEC-001, INFRA-001, RESEARCH-001 §4, SPEC-002 |
| 4 | Non-secret config in `fabric-kg.yaml`; secrets in `.env` | Consistent with existing secrets model |
| 5 | Dev auth: DefaultAzureCredential (`az login`); CI/prod: Service Principal | Simplest dev experience; secure for automation |
| 6 | Dev environment verified — all resources active except GPT-5.5-mini | See resource table above |
| 7 | Sample data reserved: `sample_data\Surface_Troubleshootings\*.pdf` | Reserved for Sprint 2+ tests; NOT processed in Sprint 1 MVP |

**Action Items:**

| # | Action | Owner |
|---|---|---|
| 1 | Deploy GPT-5.5-mini to `example-aiservices` Foundry | Infra / Hyunsuk |
| 2 | Populate `ontology/environments/dev.json` with verified workspace ID | Keyser / Hyunsuk |
| 3 | Create Lakehouse item in dev workspace (ID for env.json) | Keyser |

---

### Canonical Naming Reconciliation (Post-Consistency Review)

**Date:** 2026-06-24T12:42:17.255-07:00  
**Status:** Resolved  
**Findings:** 5 CRITICAL + 4 MEDIUM + 2 MINOR, resolved before PLAN-001

**What was unified:**

**CLI commands (CANONICAL):**

```
set-domain --prompt "..."                    # persist domain brief
enrich --domain-prompt "..." | --domain-file <path>
compile-search                               # NOT compile-search-index
deploy-lakehouse                             # NOT deploy-data
```

**Stage order (CANONICAL):**

```
domain-intake → inspect-source → enrich → compile-data →
compile-ontology → compile-search → package →
deploy-lakehouse → deploy-ontology → deploy-search → validate
```

**Config keys (CANONICAL):**

```yaml
.env:
  AZURE_AI_FOUNDRY_ENDPOINT
  AZURE_AI_FOUNDRY_API_KEY
  AZURE_DOCINTEL_ENDPOINT
  AZURE_DOCINTEL_API_KEY
  AZURE_SEARCH_API_KEY
  AZURE_BLOB_*

fabric-kg.yaml:
  foundry.endpoint, foundry.project
  enrichment.chat_deployment, enrichment.embedding_deployment,
  enrichment.embedding_dimensions (=1536),
  enrichment.vision_deployment
```

**Models (CANONICAL):**

- Chat/enrichment: GPT-5.5-mini (target) / gpt-4.1 (interim dev)
- Embedding: text-embedding-3-large @ 1536 dimensions
- Vision: chat deployment (multimodal; gpt-4.1 interim / GPT-5.5-mini target); `example-vision`/gpt-4o alternative

**Graph-to-Search columns (CANONICAL, per SPEC-002):**

- Source: `chunks.related_entity_ids`, `chunks.entity_search_keys`; `entities.canonical_key`, `entities.search_aliases`
- Derived AI Search fields: `entity_ids` (filterable), `canonical_key` (filterable), `entity_aliases` (searchable), `graph_path`, `blob_url`
- Filter rule: `search.in(entity_ids, '...', ',')` on stable IDs

**PRD handling:** PRD.md stays verbatim; add banner noting SPEC-001..005 supersede for SDK, command names, model choices.

**Validation gates to add to SPEC-005:** BRG-001..BRG-010 (bridge), D-31/D-32 (graph-search alias), domain-intake security, Document Intelligence, .env/Foundry config gates. Reword VAL-023.

---

### Implementation Plan (PLAN-001) — Keyser

**Date:** 2026-06-24T12:42:17.255-07:00  
**Document:** `docs/PLAN-001-implementation-plan.md`  
**Status:** Proposed

**Milestone Structure (M0–M5):**

| Milestone | Focus | Acceptance Criteria | Sprint |
|-----------|-------|-------|--------|
| M0 | Skeleton (pyproject, CLI structure, test fixtures) | Foundation (no direct AC) | S1 |
| M1 | Canonical Data Model + CSV | AC-01, AC-02 (schema, CSV round-trip) | S1 |
| M2 | LLM Enrichment + Parquet | AC-03, AC-07, AC-13 (enrichment, JSON→Parquet) | S1 |
| M3 | Ontology Compiler + Deploy | AC-08, AC-09, AC-11, AC-12 (compile, deploy, query) | S1 |
| M4 | Doc/Chunk/Image + AI Search | AC-04, AC-05, AC-06, AC-10 (doc extraction, visual, search) | S2 |
| M5 | Query Agent (DESIGN-001) | Post-MVP prototype (Foundry Agent Service) | Future |

**Critical Path:**

```
pyproject-setup → cli-entrypoint → cli-stubs → 
enrich-cmd → compile-data-cmd → compile-ontology-cmd → package-cmd
```

All schema, ontology model authoring, LLM integration can proceed in parallel.

**MVP Definition of Done:**

All 13 PRD §23 acceptance criteria must pass automated tests. No criterion deferred or weakened. AI Search (AC-10) in Sprint 2 scope, `search.enabled=false` default (opt-in).

**Sprint Sizing:**

- **Sprint 1:** 37 tasks across 4 epics (Skeleton, Data Model, Enrichment, Ontology)
- **Sprint 2:** 22 tasks across 4 epics (Doc Extraction, Visual Assets, Enrichment E2E, AI Search)

**Open Items:**

1. ✅ RESOLVED (2026-06-24): Deploy gpt-5-4-mini (GlobalStandard, 200K TPM) to example-aiservices. Replaces earlier GPT-5.5-mini (does not exist).
2. ✅ RESOLVED (2026-06-24): Create Lakehouse `kg_lakehouse` (item ID 44444444-4444-4444-4444-444444444444) in workspace 11111111-1111-1111-1111-111111111111.
3. Select first sample document for Sprint 2 (Fenster)
4. ✅ RESOLVED (2026-06-24): fabric-cicd is REQUIRED PRIMARY for all deployments. REST API is fallback only.

---

### Demo Provisioning & Deployment — Completed 2026-06-24

**By:** Hyunsuk Shin, Verbal (AI Integration Dev), McManus (KG/Ontology Dev)

**What:**

**1. Foundry Model Deployment (Coordinator, executed 2026-06-24T15:41:07.842-07:00)**
- Deployed `gpt-5.4-mini` (deployment name `gpt-5-4-mini`, GlobalStandard capacity 200 = **200K TPM**) on `example-aiservices` in eastus2.
- ⚠️ Note: GPT-5.5-mini does not exist in catalog. gpt-5.4-mini is the latest mini variant and chosen as enrichment default.
- Fallback: `chat` (gpt-4.1).
- **Requirement (carry forward):** CLI and specs (SPEC-001, SPEC-004, INFRA-001) must document **≥200K TPM** as minimum for high-volume enrichment stage.
- Config updated: `ontology/environments/dev.json`: `chat_deployment=gpt-5-4-mini`.

**2. Fabric Lakehouse Renamed & Configured (Coordinator, executed 2026-06-24T15:41:07.842-07:00)**
- Renamed `fabrickg_lakehouse` → **`kg_lakehouse`** (item ID 44444444-4444-4444-4444-444444444444 unchanged).
- Workspace: `11111111-1111-1111-1111-111111111111`.
- OneLake Tables/Files paths + SQL endpoint captured.
- Dev config updated: `ontology/environments/dev.json`.

**3. fabric-cicd is REQUIRED PRIMARY (McManus/Verbal, updated SPEC-003)**
- **Decision:** fabric-cicd is the PRIMARY deployment mechanism for all three deploy commands (deploy-lakehouse, deploy-ontology, deploy-search).
- Fabric REST API is FALLBACK ONLY (for item-level granularity when fabric-cicd cannot perform the operation).
- `FabricDeployer` defaults to fabric-cicd.
- **Carry forward:** CLI prerequisite (`pip install fabric-cicd`) in docs/REQUIREMENTS-001-cli-prerequisites.md; SPEC-003 deployment table and §9.1.1 updated.
- Why: Governed, reproducible Git-driven deployments per PRD CI/CD goals.

**4. AI Search Status (Verbal → SPEC-001/SPEC-004/INFRA-001 updated)**
- **Decision:** AI Search is **IN MVP scope** (not optional).
- Config default: `ai_search.enabled=true` in dev.json (dev); engineers can opt out per environment.
- All docs corrected: heading changed from "Optional" to "IN MVP."

**5. CLI Prerequisites Documented (Verbal, created docs/REQUIREMENTS-001-cli-prerequisites.md)**
- Seven required tools: Azure CLI, Python 3.10+, fabric-cicd, Fabric workspace (kg_lakehouse), Foundry ≥200K TPM, AI Search, Document Intelligence.
- Complete onboarding guide: install steps, `az login` / DefaultAzureCredential, .env setup, RBAC table, verify checklist, demo command sequence.
- 17,859 bytes.

**Specs Updated:**
- **SPEC-001:** §5.1 yaml updated (chat_deployment=gpt-5-4-mini, 200K TPM note); §5.3 flag example; §10 decisions rows 4, 5, 8 corrected.
- **SPEC-004:** §9.2 deployment corrected; model defaults table revised; Appendix B Q6/Q7 updated.
- **INFRA-001:** §1a corrected (gpt-5.4-mini, 200K TPM ⚠️); §1b AI Search heading; resource mapping table updated; reference to REQUIREMENTS-001 added.
- **SPEC-003:** §9.1 deployment table updated; §9.1.1 fabric-cicd declared REQUIRED PRIMARY; kg_lakehouse IDs captured; §9.8 CI/CD pipeline updated (deploy-search gate removed, fabric-cicd step added); §1.1 Boundaries corrected.

**No code changed** — all changes are documentation/configuration.

**Secrets:** None committed. `.env` references use placeholders only.

---

### Retrieval Agent Architecture (DESIGN-001) — Keyser

**Date:** 2026-06-24T12:42:17.255-07:00  
**Design Doc:** `docs/design/DESIGN-001-retrieval-agent-orchestration.md`  
**Status:** Proposed (M5, post-MVP)

**Decision:** Use **Foundry Agent Service + deterministic `retrieve_grounding` function tool** (Option A).

**Key Points:**

1. **Foundry orchestrates when to call the tool.** Deterministic code builds `search.in()` OData filter from validated graph entity IDs. LLM never touches OData.
2. **Effort is small:** ~400–600 LOC + config. No new dependencies.

---

### Coordinator: Lakehouse scope + visual extraction feedback

**Date:** 2026-06-24T22:30:00-07:00  
**By:** Hyunsuk Shin (via Copilot)

**Feedback 1 — Lakehouse should NOT store document text content:**
- AI Search is the home for text search (chunk content, document_elements text, table HTML).
- The Fabric Lakehouse (datalake) is for ONTOLOGY + GRAPH MODEL data — the structured graph (entities, relationships, evidence/provenance keys), NOT heavy text bodies.
- Therefore deploy-lakehouse should write LEAN, graph-centric tables: drop/trim large text columns (chunks.content/content_html/embedding_text, document_elements.content/content_html) from the Lakehouse — those belong in AI Search only.
- Also many sparse/empty columns (col_index, row_index, etc.) are table-HTML concerns that live in AI Search, not the Lakehouse — don't bloat the datalake with empty columns.

**Feedback 2 — visual_assets and visual_regions are EMPTY:**
- The enrich path never actually extracted images/figures → both visual tables are 0 rows.
- Reference implementation to follow: `C:\Users\hyssh\workspace\starbuck-siot-kb` — they extract images/figures as image files and UPLOAD them to Blob for visibility.
- Need to wire real image/figure extraction from PDFs → upload to Blob → populate visual_assets (blob_url) + visual_regions (Doc Intelligence polygons/OCR), so the visual evidence is real.

**Why:** Clean separation (datalake=graph/ontology, AI Search=text/visual retrieval); real visual evidence with blob URLs.

---

### Fenster: Lakehouse Lean Projection — Implemented

**Date:** 2026-06-24T22:52:55-07:00  
**By:** Fenster (Data Engineer)  
**Requested by:** Hyunsuk Shin  
**Status:** ✅ Implemented

**Decision:** The Fabric Lakehouse receives **graph/ontology data only**. Azure AI Search owns all text retrieval (chunks content, document_elements text/HTML). Deploy-lakehouse must use a lean projection — no bulk text columns, no sparse table-HTML columns.

**Scope:**

| Table | Lakehouse? | Notes |
|---|---|---|
| source_files | ✓ All columns | File provenance — graph root |
| document_elements | ✓ Lean (12 cols) | Structural/graph cols only — content/content_html/row_index/col_index dropped |
| chunks | ✗ **Excluded** | Pure retrieval text → AI Search kg-chunks index |
| entities | ✓ All columns | Graph nodes + ontology bindings |
| relationships | ✓ All columns | Graph edges |
| evidence | ✓ All columns | Provenance links |
| visual_assets | ✓ All columns | Visual ontology assets |
| visual_regions | ✓ All columns | Visual ontology regions |

**document_elements — kept columns:**
`document_element_id`, `source_file_id`, `element_type`, `parent_element_id`, `page_number`, `section_path`, `sort_order`, `table_id`, `figure_id`, `image_id`, `blob_url`, `content_hash`

**Dropped:** `content`, `content_html` (heavy text → AI Search), `row_index`, `col_index` (sparse table-HTML → AI Search)

**Implementation:**
- `LAKEHOUSE_TABLE_PROJECTION` dict constant in `onelake_writer.py` — single source of truth.
- `LAKEHOUSE_TABLES` list exported from same module — derived from projection keys.
- `deploy_parquet_to_onelake(projection=...)` param — defensive column select, skips excluded tables.
- `deploy_cmd.py` imports constants from `onelake_writer`; mock mode reports lean scope; live mode passes projection.

**Test result:** 30 new tests in `tests/unit/test_deploy_lakehouse_projection.py`. All 832 suite tests pass.

**Rationale:** Clean system-of-record separation: Lakehouse = structured/queryable graph model optimized for Spark/SQL analytics on entities, relationships, evidence, ontology. AI Search = full-text + vector retrieval on chunk content and document element text. Lean projection keeps tables small and purpose-clear.

---

### Keyser: Templates-First is the Documented Onboarding Path (INBOX MERGE)

**Date:** 2026-06-25  
**Author:** Keyser (Lead / Architect)  
**Status:** Accepted

## Decision

The **domain-template approach** is now the primary documented onboarding path for fabric-kg-builder. Users are coached — from `fabric-kg --help`, `fabric-kg set-domain --help`, and the README — to start by defining a domain template (industry + entity types + relationships + sample questions) before running any pipeline stage.

## Context

We built the Surface hardware-troubleshooting knowledge graph end-to-end and learned that generic, schema-less extraction yields a sparse, poorly-connected graph that the Fabric Data Agent cannot query effectively. Specifically:

- Per-section extraction leaves device models disconnected from their own parts.
- 79% of symptoms were isolated (no Cause or Resolution edges) before densify.
- The Data Agent returned 0 rows when using exact-match on DeviceModel names.

The remedies — `densify`, `deploy-ontology --multitype`, and Data Agent grounding — are only effective when the extraction itself is guided by a typed schema.

## What Changed (documentation + feature)

1. **CLI:** `set-domain --industry <ind> --business-domain <dom>` now accepted; domain stored in `DomainBrief/{industry}/{business_domain}/domain.json`.
2. **README:** New section "## Domain Template Playbook" with Surface template table, full step-by-step build, 8 sample questions, data-agent-grounding pointer, and iteration loop.
3. **Specs:** SPEC-001, SPEC-004 updated to emphasize schema-first intake.
4. **Densify link_procedure_steps():** Reconstructs has_step edges by document reading order (page+sort_order). Coverage 2%→27% of procedures. CLI: `densify --link-steps` (default on).
5. **Reproducibility scripts:** `scripts/reproduce-surface-kg.ps1` + `.sh` (one-command end-to-end recipe).

## Rationale

Users who skip domain-template definition end up with a generic KGEntity-only ontology and a sparse graph that retrieves poorly. Surfacing these concepts in the CLI help text (which every new user sees first) and in the README (the primary onboarding document) ensures the right mental model is established before any commands are run.

---

### Keyser: fabric-kg CLI is now fully self-documenting

**Date:** 2026-06-25  
**Author:** Keyser (Lead / Architect)  
**Status:** Done

**Context**

The `fabric-kg` CLI was previously usable only if you already knew the commands. Help text was thin: some options had no help strings, defaults were not shown, and there was no example invocation or contact information anywhere in help output. This made the tool hard to drive from GitHub Copilot CLI, which relies on help to understand commands.

**Decision**

Enhanced all CLI help text in a purely additive, non-breaking way:

1. **Top-level group** (`fabric-kg --help`):
   - Expanded docstring: describes the end-to-end transformation pipeline.
   - Epilog: numbered 12-stage pipeline overview, Surface-sample Windows-path example sequence, and contact line `Questions? Contact hyssh@microsoft.com`.
   - Added `context_settings={"max_content_width": 120, "help_option_names": ["-h", "--help"]}`.

2. **All 12 subcommands**: Each has an epilog with:
   - A realistic Example block using `sample_data\Surface_Troubleshootings` Windows paths where relevant.
   - A `Questions? hyssh@microsoft.com` contact line.
   - Improved help strings on every option (input types, defaults, behavior).
   - `show_default=True` added where it was missing.

3. **No functional changes**: option names, defaults, and runtime behavior are completely unchanged. Only help text, docstrings, and epilogs were added.

**Verification**

- `fabric-kg --help`, `fabric-kg enrich --help`, `fabric-kg deploy-lakehouse --help` all render correctly with pipeline overview, example, and contact line.
- `pytest -q -m "not slow and not integration"` → **918 passed, 0 failures**.

**Contact**

Questions? hyssh@microsoft.com

---

### Keyser: README.md as Canonical Onboarding Document

**Author:** Keyser (Lead / Architect)  
**Status:** Accepted  

**Context**

The repository had no top-level README. New contributors and operators had no single entry point to understand what the project does, how to install it, or how to run the pipeline.

**Decision**

`README.md` at the repo root is the **canonical onboarding document** for `fabric-kg-builder`. It is the first file any contributor, operator, or evaluator should read.

**What the README Documents**

1. **Goal / what the project does** — pipeline from raw documents to deployed Fabric KG.  
2. **Prerequisites** — Python ≥ 3.10, Azure subscription, Fabric workspace, Foundry, AI Search, Document Intelligence, Blob Storage, `az` CLI.  
3. **Installation** — `pip install -e .` / `pip install -e .[dev]`.  
4. **Configuration** — three-layer model: `.env` (secrets), `fabric-kg.yaml` (non-secret), `ontology/environments/{env}.json` (per-env resource IDs).  
5. **Full pipeline command order** (canonical, confirmed against SPEC-001 and `build_deploy_cmd.py` docstring):

   ```
   az login
   set-domain → enrich → compile-data → compile-ontology → compile-search
   → package → deploy-lakehouse → deploy-ontology → deploy-search → validate
   ```

6. **Command reference table** — all 13 subcommands with one-line descriptions.  
7. **Project layout** — `src/`, `tests/`, `ontology/environments/`, `sample_data/`.  
8. **Testing** — pytest marker scheme (unit / contract / integration / slow / smoke).  
9. **Notes & limitations** — `build-deploy` not yet fully implemented; locked embedding dimensions; schema-enabled Lakehouse required.

**Trade-offs**

| Choice | Alternative | Rationale |
|---|---|---|
| README as single doc | Separate INSTALL.md + USAGE.md | Lower friction; one file to discover |
| Windows-style paths in examples | POSIX-only | Primary dev environment is Windows |
| Mermaid-style text diagram | Separate diagram tool | Zero tooling dependency; renders in GitHub |

**Consequences**

- The README must be updated whenever commands are added, renamed, or their options change.  
- Real resource IDs, tenant IDs, and secrets must **never** appear in the README.  
- The pipeline stage order in the README is authoritative for documentation purposes; the implementation source of truth remains SPEC-001.

---

### McManus: Real Fabric Ontology Format + updateDefinition Deploy

**Date:** 2026-06-25T00:05:13.466-07:00  
**Agent:** McManus (KG/Ontology Dev)  
**Requested by:** Hyunsuk Shin  
**Status:** ✅ IMPLEMENTED + VERIFIED (918 tests passing)

**Problem**

The deployed Fabric Ontology item showed **EMPTY (Nodes 0, Edges 0)**. Two root causes:

1. `deploy-ontology` only called `POST /items` to create the shell — never called `updateDefinition` to populate it.
2. The compiled parts from `compiler.py` were in the wrong format (our own schema, not the EXACT Fabric format).

**Solution**

### New module: `src/fabric_kg_builder/ontology/fabric_def.py`

`build_ontology_parts(workspace_id, lakehouse_item_id, schema='dbo', ontology_name='kg_ontology') -> list[dict]`

Produces **6 parts** in the exact decoded Fabric format:

| Path | Content |
|------|---------|
| `definition.json` | Root manifest, always empty |
| `.platform` | Ontology metadata (type, displayName, version 2.0, logicalId all-zeros) |
| `EntityTypes/124494482930080181/definition.json` | KGEntity (4 String props: entity_id/entity_type/display_name/canonical_key) |
| `EntityTypes/124494482930080181/DataBindings/{guid}.json` | Binds → `dbo.entities` |
| `RelationshipTypes/212385435028070257/definition.json` | related_to (source=KGEntity, target=KGEntity) |
| `RelationshipTypes/212385435028070257/Contextualizations/{guid}.json` | Binds → `dbo.relationships` (source_entity_id→entity_id, target_entity_id→entity_id) |

**ID generation:** SHA-256 of seed string → first 8 bytes → unsigned int → mod 2^62 → positive BigInt string. Stable across runs (idempotent updateDefinition).

### Updated: `src/fabric_kg_builder/deploy/fabric_ontology.py`

Added `update_ontology_definition(workspace_id, ontology_item_id, parts, mock=False, token_provider=None) -> dict`:
- Base64-encodes each payload_json dict
- POSTs `{"definition":{"parts":[{path, payload, payloadType:"InlineBase64"}]}}` to POST /updateDefinition
- Handles 200 (sync) + 202 (LRO)
- `mock=True` returns summary without network call

### Updated: `src/fabric_kg_builder/cli/deploy_cmd.py`

`deploy_ontology_cmd` rewritten:
- Reads `workspace_id` + `lakehouse_item_id` + `schema_name` from `ontology/environments/{env}.json`
- Calls `build_ontology_parts()` → `create_or_get_ontology_item()` → `update_ontology_definition()`
- Mock mode: builds 6 parts, logs all paths + entity/rel type names, no network
- Live mode: creates/gets item, resolves LRO placeholder if needed, calls updateDefinition
- Old `--dist` flag retained for compat (informational; compile artifact is separate)

**Verification**

```
python -m pytest -q
918 passed, 4 deselected in 97.87s
```

**Key Technical Notes**

1. **Schema URL path:** must be `/item/ontology/entityType/...` (not `/ontology/entityType/...`) — matches working on_finance ontology.
2. **ID types:** entity/property/rel IDs are **BigInt strings** (not UUIDs). DataBinding/Contextualization IDs are **UUIDs**. Mixing these crashes the Fabric API.
3. **logicalId:** must be `00000000-0000-0000-0000-000000000000` for new items.
4. **Two-call deploy:** POST /items (create shell) + POST /updateDefinition (populate). Previously only the first call was made.
5. **old compiler.py stays:** compile-ontology artifact (build/ontology/) is separate from the Fabric deploy format. Both coexist.

**Files Changed**

| File | Status |
|------|--------|
| `src/fabric_kg_builder/ontology/fabric_def.py` | NEW |
| `src/fabric_kg_builder/deploy/fabric_ontology.py` | MODIFIED (+`update_ontology_definition`) |
| `src/fabric_kg_builder/cli/deploy_cmd.py` | MODIFIED (deploy_ontology_cmd rewritten) |
| `tests/unit/test_fabric_def.py` | NEW (55 tests) |
| `tests/unit/test_deploy_ontology_cmd.py` | MODIFIED (updated for new behavior) |
3. **Eliminates injection risk** — LLM never generates filter syntax; malformed filters impossible.
4. **MCP is a graduation path**, not starting point. Start in-process; wrap as MCP if second consumer appears.
5. **Post-MVP** — query-time grounding consumes data MVP pipeline produces. No existing specs change.
6. **Prototype slice:** CLI `fabric-kg query` command against dev resources (example-aiservices, example-search).

**Alternatives Considered:**

- **B. AI Search Agentic Retrieval** — good Phase-2 engine but doesn't traverse graph. Layer later.
- **C. MCP server** — more infra; warranted only for multi-agent reuse.
- **D. Custom orchestration (LangChain etc.)** — most code; rejected while on Foundry.

**Action Required:**

- [ ] Team review of DESIGN-001
- [ ] Hyunsuk: confirm Option A direction
- [ ] Prioritize prototype in sprint backlog (post-MVP)

---

## Governance

- All meaningful changes require team consensus
- Document architectural decisions here
- Keep history focused on work, decisions focused on direction


## Sprint 1+2 Implementation Decisions


### fenster

# Decision: compile-data Implementation

**ID:** fenster-compile-data  
**Date:** 2026-06-24T17:26:19-07:00  
**Author:** Fenster (Data Engineer)  
**Status:** Implemented  
**Sprint:** 1  

---

## Context

`fabric-kg compile-data` was a stub that printed "not implemented yet." Sprint 1 requires it to read canonical intermediate JSON from `build/enriched/` (produced by the `enrich` stage) and write all 8 canonical Parquet tables to `build/parquet/` via the existing `parquet.writer` module.

---

## Decision

### What was implemented

1. **`src/fabric_kg_builder/validate/data_gates.py`** — new module, VAL-001..VAL-007:
   - VAL-001..VAL-004: duplicate ID checks (entity_id, relationship_id, chunk_id, evidence_id)
   - VAL-005..VAL-006: dangling source/target entity FKs on relationships
   - VAL-007: dangling evidence_id FK on relationships
   - Returns `list[Violation]` (pure function, no CLI dependency)

2. **`src/fabric_kg_builder/cli/compile_data_cmd.py`** — full implementation:
   - Reads all `*.json` files from `--input` dir (skips `.checkpoint.json`, `domain.json`)
   - Coerces ISO datetime strings back to aware `datetime` objects for PyArrow compatibility
   - Runs VAL-001..007 on merged data; exits 5 with human-readable violation messages on failure
   - Calls `write_all_tables(table_rows, out_dir)` for all 8 tables (empty rows for tables not in enriched JSON)
   - Prints per-table row count summary on success; exits 0

3. **`tests/unit/test_compile_data_cmd.py`** — 17 new unit tests:
   - Happy path: exits 0, writes 8 Parquet files, readable by PyArrow, multi-file merge
   - VAL-001: duplicate entity_id → exits 5, reports violation, no Parquet written
   - VAL-005/006: dangling relationship FK → exits 5, reports violation
   - Edge cases: missing input dir, empty dir, checkpoint skipping, nested output dir creation

### Key design choices

| Choice | Rationale |
|---|---|
| Always run VAL gates | Data integrity is non-negotiable; not gated behind `--validate` flag |
| Gates on raw merged data (before dedup) | Catches corruption; normal pipelines rely on orchestrator's per-batch dedup |
| 8 tables always written (empty for unpopulated) | Downstream consumers expect all 8 files; empty Parquet is valid |
| `sys.exit(5)` for gate failure | Matches SPEC-001 §7 exit code contract exactly |
| `_SKIP_NAMES` set for checkpoint/domain files | Prevents parser errors on non-batch JSON in enriched dir |
| ASCII-only CLI output | Avoids Windows cp1252 encoding errors with Unicode arrows/dashes |

### Exit codes implemented

| Code | Meaning |
|---|---|
| 0 | Success — all 8 tables written |
| 1 | I/O or unexpected error (click.ClickException) |
| 5 | VAL-001..007 gate failure |

---

## Files changed

- `src/fabric_kg_builder/cli/compile_data_cmd.py` — replaced stub
- `src/fabric_kg_builder/validate/data_gates.py` — new
- `tests/unit/test_compile_data_cmd.py` — new

---

## Verification

```
fabric-kg compile-data --input build/enriched --out build/parquet
# entities=2, relationships=1, chunks=1, evidence=1 — exit 0
# 8 parquet files written

pytest tests/unit -q --no-cov
# 346 passed (274 pre-existing + 17 new compile-data + pre-existing failures now resolved)
```


# Decision: fenster-sprint1-ingest — CSV Loader, Parquet Writer, and Placeholders

**Date:** 2026-06-24T17:01:06-07:00  
**Author:** Fenster (Data Engineer)  
**Sprint:** 1  
**Status:** Implemented & Tested

---

## Context

Sprint 1 required implementing the ingestion and persistence layer for the canonical data model defined in SPEC-002. Three components were needed:

1. **CSV Loader** — read CSV/TSV/XLSX files and produce `SourceFileRow` + `DocumentElementRow` records plus a `schema-profile.json`.
2. **Parquet Writer** — write validated canonical rows for any of the 8 tables using the declared PyArrow schemas with correct types, null handling, and list/JSON encoding.
3. **Placeholder Writer** — emit empty-but-typed `_placeholder.parquet` files for all 8 tables into per-table subdirectories.

---

## Decisions Made

### 1. CSV encoding: `utf-8-sig` as the default

**Decision:** Use Python's `utf-8-sig` codec (not `utf-8`) for all CSV/TSV reads.  
**Rationale:** `utf-8-sig` transparently strips the UTF-8 BOM that Excel, Power BI, and many Windows tools emit. The alternative (manual `.lstrip('\ufeff')`) requires two code paths and is error-prone. `utf-8-sig` is a strict superset — it handles BOM-less files identically to `utf-8`.

### 2. Delimiter detection via `csv.Sniffer` with fallback

**Decision:** Auto-detect delimiter using `csv.Sniffer` on the first 4 KB of text; fall back to comma on `csv.Error`. Override with explicit `\t` for `.tsv` files.  
**Rationale:** Sprint-1 scope is Surface domain CSV files. `csv.Sniffer` covers the common cases (comma, tab, pipe, semicolon). Hard-coding comma would break TSV passthrough.

### 3. PyArrow write path: `pa.Table.from_pylist(rows, schema=schema)`

**Decision:** Use `from_pylist` (row-oriented) not `from_pydict` (column-oriented) for the canonical Parquet write.  
**Rationale:** Row-oriented input is natural for pipeline data (list of dicts from Pydantic models or CSV parsing). `from_pylist` with an explicit `schema` performs strict type coercion at conversion time, surfacing errors with useful messages. It handles `list<string>` columns (`aliases`, `related_entity_ids`) natively without special wrapping.

### 4. NOT NULL validation before Arrow conversion

**Decision:** Explicitly scan rows for NOT NULL violations _before_ calling `from_pylist`, with column name and row index in the error message.  
**Rationale:** Arrow's own error messages for null violations can be cryptic. An explicit pre-check at the application layer gives pipeline operators a clear, actionable error pointing to the exact column and row.

### 5. Placeholder timestamp sentinel: `datetime(2000, 1, 1, tzinfo=UTC)`

**Decision:** Use a fixed year-2000 UTC timestamp as the sentinel for all `*_at` NOT NULL timestamp columns in placeholder rows.  
**Rationale:** Using `datetime.now()` in placeholders introduces non-determinism in build artifacts and makes placeholder detection unreliable. The year-2000 value is visually obvious in any data inspector, pre-dates all real pipeline data, and is constant across all environments.

### 6. Placeholder file layout: `build/parquet/<table>/_placeholder.parquet`

**Decision:** Write placeholders into per-table subdirectories, not as flat files.  
**Rationale:** SPEC-002 §8.2 is explicit: the subdirectory structure reserves the flat `<table>.parquet` name exclusively for real data. This prevents ambiguity at CI artifact validation and allows the ontology compiler to detect "placeholder-only" vs "real data" state by checking whether a flat file or a subdirectory exists.

### 7. `examples/csv/sample.csv` vs `tests/fixtures/csv/sample.csv`

**Decision:** Maintain two separate CSV fixtures with different row counts but identical column schema.  
**Rationale:** `examples/csv/` is the public demo fixture (6 rows, full Surface dataset). `tests/fixtures/csv/` is the minimal test stub used by the `sample_csv_path` conftest fixture (3 rows). They share column names but differ in row count; the conftest's `assert _CSV_FIXTURE.exists()` guard enforces the fixture is present.

---

## Files Created / Modified

| File | Type | Purpose |
|---|---|---|
| `src/fabric_kg_builder/sources/csv_loader.py` | New | CSV/TSV/XLSX loader — SPEC-002 §6 |
| `src/fabric_kg_builder/parquet/writer.py` | New | Parquet writer + placeholder writer — SPEC-002 §7–8 |
| `examples/csv/sample.csv` | New | Public demo CSV fixture (6 Surface rows) |
| `tests/unit/test_csv_loader.py` | New | 22 unit tests for csv_loader |
| `tests/unit/test_parquet_writer.py` | New | 23 unit tests for parquet writer + placeholders |
| `.squad/agents/fenster/history.md` | Updated | Sprint 1 learnings appended |

---

## Test Results

```
tests/unit/test_csv_loader.py    — 22 passed
tests/unit/test_parquet_writer.py — 23 passed
Full suite tests/unit/           — 156 passed, 1 pre-existing failure (test_foundry_client, unrelated)
```

Pre-existing failure confirmed: `test_complete_json_uses_temperature_zero_and_seed` in `test_foundry_client.py` fails on the baseline commit before any Sprint 1 changes.

---

## Open Items

- XLSX multi-sheet support is implemented but the schema-profile only covers the first sheet's columns. A future sprint should produce per-sheet profiles when multi-sheet XLSX is common.
- `llm_suggested_entity_type` and `llm_mapping_notes` fields in SPEC-002 §6.2 schema-profile are reserved but not populated (LLM enrichment is Sprint 2 scope).
- `write_table` raises `ValueError` on schema mismatch but does not yet attempt type coercion. Post-MVP: consider a `coerce=True` flag for developer convenience.


# Fenster Sprint 2 Extractors

## Context

Sprint 2 adds native PDF, DOCX, and HTML extraction plus chunking and table normalization to the `sources` package.

## Decisions

1. **Use local-library parsing, not cloud OCR, for Sprint 2.**
   - PDF text comes from `pdfplumber`.
   - DOCX structure comes from `python-docx`.
   - HTML parsing comes from `BeautifulSoup` with `lxml`.

2. **Keep extraction and normalization separate.**
   - First-pass extractors produce `document_elements`.
   - `TableExtractor` performs second-pass table cell normalization from `content_html`.
   - `Chunker` converts normalized elements into `chunks`.

3. **Prefer deterministic, content-derived IDs everywhere.**
   - `document_element_id` uses `make_document_element_id(...)`.
   - `chunk_id` uses `make_chunk_id(...)`.
   - `content_hash` is always derived from the canonical content string for the row/chunk.

4. **PDF heading detection is heuristic by design.**
   - Signals: font-size ratio, numbered headings, ALL CAPS short lines, colon-suffixed labels.
   - This is intentionally lightweight and local so it works on service guides without external dependencies.

## Consequences

- The pipeline can now ingest common document formats into canonical `document_elements`.
- Structured table and chunk generation can be tested independently of the file-format extractors.
- Future OCR/image extraction can plug in later without replacing these deterministic text extractors.


# Decision Record — Sprint 2 Search & Inspect Pipeline

**Date:** 2026-06-24  
**Author:** Fenster (Data Engineer)  
**Status:** Implemented  
**Requested by:** Hyunsuk Shin

---

## Context

Sprint 2 required wiring PDFs end-to-end from raw file → inspect-source inventory → full 8-table Parquet write → AI Search chunk/document push. Four implementation gaps were identified and closed.

---

## Decisions Made

### 1. inspect-source now routes PDF / DOCX / HTML / .md

**Decision:** Extend `inspect_source_cmd` to accept any supported source type (not just CSV), dispatching via `sources/router.py`. Directories are fully scanned; per-file summaries (pages, element-type counts, content hash, file size) are printed, followed by a combined inventory line.

**Rationale:** The 22 Surface PDFs must flow through the toolchain. Making `inspect-source` the first visible confirmation that a file is readable and parseable reduces silent failures later in `extract → enrich → compile`.

**Impact:** `_SUPPORTED_EXTS` now covers `.csv`, `.pdf`, `.docx`, `.html`, `.htm`, `.md`. Exit code 3 is returned for unsupported extensions.

---

### 2. compile-data reads all 8 tables from enriched JSON

**Decision:** `_load_enriched_json` now loads `source_files`, `document_elements`, `visual_assets`, `visual_regions` in addition to the original four. All 8 keys are optional in each JSON batch (absent → empty list). Gate message updated to VAL-001..VAL-012.

**Rationale:** The writer already accepted all 8 tables; the loader was the gap. Closing it means `compile-data` is now a true end-to-end table writer, not just an entity/relationship writer.

---

### 3. VAL-008..012 added for visual/evidence integrity

**Decision:** Five new gates added to `data_gates.py`:
- **VAL-008** — duplicate `image_id` in `visual_assets`  
- **VAL-009** — duplicate `visual_region_id` in `visual_regions`  
- **VAL-010** — `visual_regions.image_id` FK → `visual_assets.image_id`  
- **VAL-011** — `evidence.image_id` FK → `visual_assets.image_id`  
- **VAL-012** — `evidence.visual_region_id` FK → `visual_regions.visual_region_id`  

**Rationale:** SPEC-002 §9 D-05/D-06 defines referential integrity rules for the visual sub-graph. Without these gates a missing FK is only discovered at query time in Fabric.

**Run order:** Dup checks (VAL-008, 009) before FK checks (VAL-010..012) so a missing FK target that is itself a dup produces a single, clear error message.

---

### 4. search/linkage.py — filter-on-IDs / search-on-aliases split enforced

**Decision:** `derive_chunk_search_docs` produces two separate AI Search columns:
- `entity_ids: list[str]` — SHA-256 IDs from `chunks.related_entity_ids`; tagged `filterable=True`, `searchable=False`
- `entity_aliases: list[str]` — human-readable names from `chunks.entity_search_keys`; tagged `searchable=True`, `filterable=False`

**Rationale:** SPEC-002 §11.4 is explicit: opaque IDs must never be BM25-indexed (they would inflate recall for random substrings), and aliases must never be used as filter keys (text normalization breaks exact ID equality). Both columns are required; conflating them is a correctness bug.

**Compile-time population:** `entity_search_keys` on the chunk record is populated at `compile-data` time (when the entity lookup is in scope), not re-derived at push time. Push simply reads the pre-populated field.

---

### 5. search/push.py — change detection via content_hash

**Decision:** `push_chunk_docs` accepts `existing_hashes: dict[str, str]` (chunk_id → SHA-256). Any doc whose `content_hash` matches the stored hash is skipped. `PushResult` gains a `skipped: int = 0` field.

**Rationale:** AI Search charges per upsert API call. Skipping unchanged chunks on re-runs (e.g., daily pipeline reruns where only a subset of PDFs changed) reduces cost and latency without sacrificing correctness.

**Scope constraint (Lakehouse-only for Parquet):** Parquet files are written to Fabric Lakehouse tables only — they are NOT pushed to AI Search. Only chunk/text/visual search documents are pushed. This matches §2.1.

---

## Outstanding / Future Work

- **Async/batch PDF extraction** — `inspect-source` on 22 PDFs (~137 MB) takes ~13 min sequentially. A `--workers N` flag + `concurrent.futures.ThreadPoolExecutor` would reduce this to ~2–3 min.
- **Markdown extractor** — `.md` files currently route through `html_extractor`, producing minimal element counts. A dedicated Markdown parser would yield heading/paragraph/code-block structure.
- **Push index creation** — `push.py` upserts documents but does not create the AI Search index schema. A `create-index` subcommand (separate from `push`) is needed before the first push.
- **`entity_search_keys` back-fill** — chunks enriched before Sprint 2 will not have `entity_search_keys`. A migration pass in `compile-data --backfill` or a `search/backfill.py` utility should be added before production push.



### hockney

# Decision: Sprint 1 CI Scaffold

**Agent:** Hockney (Test Engineer)  
**Date:** 2026-06-24T16:09:30-07:00  
**Status:** Accepted  
**Refs:** SPEC-005 §3 §8 §9, SPEC-001 §7

---

## What was built

| File | Purpose |
|------|---------|
| `tests/conftest.py` | Shared pytest fixtures: `tmp_build_dir`, `sample_csv_path`, `mock_foundry_client`, `mock_blob_uploader`, `mock_search_client`, `mock_document_intelligence_client`, `parquet_tables` + factory functions |
| `tests/fixtures/csv/sample.csv` | Tiny 3-row Surface hardware CSV for use across tests |
| `tests/fixtures/llm/sample_enrichment.json` | Deterministic Foundry LLM response fixture (2 entities, 1 relationship, 1 evidence) |
| `tests/fixtures/document_intelligence/analyze_result.json` | DI AnalyzeResult stub with polygon data for Sprint 2 tests |
| `tests/fixtures/parquet/valid/` | Empty dir; Sprint 1 unit agents populate when they write Parquet tests |
| `tests/unit/test_cli_smoke.py` | 31 CliRunner tests: help exits 0, all 13 canonical commands listed, each command has --help, unknown command exits non-zero, --version works |
| `pyproject.toml` | Added `markers` for unit/contract/integration/smoke; coverage stays reportable but `--cov-fail-under` NOT in addopts (dev-friendly) |
| `.github/workflows/ci.yml` | Push + PR gate: Python 3.10/3.11/3.12 matrix; `pip install -e .[dev]`; `pytest tests/unit tests/contract tests/integration --cov-fail-under=80` |

---

## Key decisions

### 1. Factory functions alongside fixtures

`conftest.py` exposes both `@pytest.fixture` wrappers (zero-arg, default JSON) and bare `make_*()` factory functions. This lets any test module import and call a factory with custom JSON without having to use `request.getfixturevalue()` or create an inner fixture.

### 2. `--cov-fail-under` belongs in CI only

Sprint 1 coverage starts at ~31% (CLI stubs + scaffolding only). Enforcing 80% in `addopts` would break every `pytest` run until all unit agents have delivered their tests. The gate is enforced exclusively in `ci.yml`.

### 3. `mock_search_client` added beyond SPEC-005 §9 spec

SPEC-005 §9 listed Foundry, Blob, and Document Intelligence as the three mocked services. `azure.search.documents.SearchClient` is equally needed for `compile-search` and `deploy-search` tests; it was added to the shared conftest now rather than forcing every agent to write their own.

### 4. Contract and integration test dirs scaffolded empty

`tests/contract/__init__.py` and `tests/integration/__init__.py` exist so pytest collection never errors when other sprint agents drop files there. The CI workflow already lists both paths in the pytest invocation.

---

## Pytest result (2026-06-24T16:09 local)

```
31 passed in 0.58s
```

All 13 canonical commands verified in CliRunner:  
`set-domain`, `inspect-source`, `enrich`, `compile-data`, `compile-ontology`, `compile-search`, `package`, `deploy-lakehouse`, `deploy-ontology`, `deploy-search`, `validate`, `build-deploy`, `init`


# Decision: Validation Suite Architecture — Sprint 1+2

**ID:** hockney-validation-e2e  
**Date:** 2026-06-24  
**Author:** Hockney (Test Engineer)  
**Status:** Accepted  
**References:** SPEC-005 §2 (gate catalog), §7 (e2e trace), PRD §21, §23.13

---

## Context

Sprint 1+2 required completing the validation gate catalog (VAL-008..028), wiring up the `validate` CLI command, and implementing the SPEC-005 §7 end-to-end traceability test. Fenster had already implemented `data_gates.py` (VAL-001..012 in internal D-XX numbering) and McManus had BRG-001..010 in `bridge_validation.py`. The question was how to compose these into a single `validate_all()` callable with SPEC-005 rule IDs.

---

## Decision 1: Keep two Violation types; adapt at the suite boundary

**Chosen:** `suite.py` defines a new `ValidationViolation(rule_id, severity, message)` and adapts both `data_gates.Violation` and `BridgeViolation` via thin adapter functions (`_adapt_data_gate`, `_adapt_brg`).

**Rejected:** Retrofitting the existing Violation types with `rule_id`/`severity` fields.

**Rationale:** The existing types are already tested with their current field layout. Changing them would break existing tests and require updating all callers. An adapter layer is a one-line conversion and keeps the modules decoupled.

---

## Decision 2: `validate_all(skip_env_check=True)` flag for tests

**Chosen:** Add `skip_env_check: bool = False` parameter to `validate_all()`. When True, VAL-025 (required env vars) and VAL-026 (secret scan) are skipped.

**Rationale:** VAL-025 fires in every CI environment where secrets are not set. Including it by default would make `pytest` fail for any test that calls `validate_all()` on a temp build directory — which is the correct behavior for a production run, but wrong for unit/integration tests. The flag cleanly separates structural validation (Parquet FK, ontology) from credential checks (startup only).

---

## Decision 3: E2E trace test uses plain dict fixtures, not Parquet

**Chosen:** `tests/fixtures/e2e_trace/*.json` + a minimal `_find()` helper in `test_e2e_trace.py`. No Parquet, no pandas in the trace logic.

**Rejected:** Writing Parquet fixtures and using the conftest `parquet_tables` fixture.

**Rationale:** The SPEC-005 §7 trace is testing FK join integrity and blob_url consistency — pure data logic. Parquet I/O adds ~100ms per test and a pyarrow dependency with no benefit. JSON + dict lookups run in <10ms and are more readable. The Parquet round-trip is already tested in `test_parquet_writer.py`.

---

## Decision 4: `chat_deployment` lives under `enrichment` in fabric-kg.yaml

**Observed:** The `Config` Pydantic schema places `chat_deployment` under `foundry`, but the real `fabric-kg.yaml` has `enrichment.chat_deployment`. VAL-027 must check both sections.

**Fix:** Updated `_val027_foundry_config()` to look for `enrichment.chat_deployment` first, then fall back to `foundry.chat_deployment`. This allows validate to pass with the shipped YAML without false-positive failures.

**Impact:** Any future code that builds a config dict for programmatic calls to `validate_all` should use the `foundry.chat_deployment` key for simplicity; the YAML parser path uses `enrichment.chat_deployment`.

---

## Decision 5: VAL-023 uses structural field intersection, not schema awareness

**Chosen:** Check `_STRUCTURED_FIELDS & set(doc.keys())` where `_STRUCTURED_FIELDS = frozenset({"entity_id", "entity_type", "relationship_id", "relationship_type", "source_entity_id", "target_entity_id"})`.

**Rationale:** This catches the most common leakage pattern (copying the full entity/relationship row into the search doc) without needing to load the Parquet schemas. The check is O(1) per document. False positives are possible if a chunk document legitimately contains one of these field names, but that would be a schema violation in its own right.

---

## New files created

| File | Purpose |
|------|---------|
| `src/fabric_kg_builder/validate/suite.py` | Unified validation suite: `validate_all()` + all new gate implementations |
| `src/fabric_kg_builder/cli/validate_cmd.py` | Updated `validate` command with `--build`, `--skip-env-check`, grouped report, exit 8 on FAIL |
| `tests/unit/test_validators.py` | Unit tests for VAL-008..028, D-31/D-32 (53 tests) |
| `tests/integration/test_e2e_trace.py` | E2E trace test: entity→relationship→evidence→visual chain (8 tests) |
| `tests/fixtures/e2e_trace/*.json` | Deterministic fixture data for 5 canonical tables |

---

## Acceptance verification

- `python -m pytest tests -q -m "not integration"` → **652 passed** (was 591; +61 new tests)
- `python -m pytest tests/integration/test_e2e_trace.py -v` → **8 passed**
- `fabric-kg validate --env dev --build build --skip-env-check` → **exit 0**, 0 FAIL, 9 WARN (BRG-009 bridge-traversal warnings for entities without `evidenced_by` edges — expected on a dev config without bridge wiring)
- `fabric-kg validate --env dev --build build` (without skip-env-check on a machine without env vars) → **exit 8**, VAL-025 fires



### keyser

# Decision: inspect-source Implementation

**ID:** keyser-inspect-source  
**Date:** 2026-06-25T00:13:26Z  
**Author:** Keyser (Lead / Architect)  
**Status:** Accepted  
**Requested by:** Hyunsuk Shin  

---

## Context

The `inspect-source` CLI command was a stub (`[inspect-source] not implemented yet`). Sprint 1 requires it to load a CSV/TSV/XLSX file via the existing `csv_loader.load_csv()` and print a human-readable schema profile, with optional JSON file output via `--out`.

---

## Decision

Replaced the stub in `src/fabric_kg_builder/cli/inspect_cmd.py` with a full implementation that:

1. **Calls `load_csv()`** — does not reimplement parsing; delegates entirely to the existing loader.
2. **`--input PATH`** — required; accepts a single file or a directory (auto-discovers all `.csv`/`.tsv`/`.xlsx` files in dir).
3. **`--format {table|json}`** — table (default) prints a human-readable columnar summary; json emits the raw schema-profile dict.
4. **`--out DIR`** — when given, writes `schema-profile.json` to that directory (created if absent). For multiple files, writes a JSON array.
5. **Exit codes per SPEC-001 §7:** 0 = success, 1 = error/not-found, 3 = unsupported file type.

### Flag naming

The existing stub had `--output <file>` (write path). The task spec says `--out <dir>` (write directory + fixed filename `schema-profile.json`). Chose `--out` + directory to match the task spec and align with `--out` conventions used by other commands (`compile-data`, `compile-ontology`, `compile-search`).

---

## Bug Found and Fixed

`_collect_files()` initially returned any file unconditionally. This caused `.txt` → `load_csv()` → `CsvLoaderError` → exit 1, instead of the correct exit 3. Fixed by checking the extension at the single-file return point.

---

## Tests Added

`tests/unit/test_inspect_cmd.py` — 12 CliRunner tests:
- Exit 0, column names, row count, source_type present in output
- `--out` creates `schema-profile.json`, valid JSON, correct `row_count`, has `columns`
- Bad path → exit non-zero + error message
- Unsupported extension → exit 3
- `--format json` → parseable JSON with expected keys

**Result: 237 unit tests passed, 0 failures.**

---

## Tradeoffs

| Option | Chosen | Reason |
|---|---|---|
| `--output <file>` (spec stub) | No | Task spec says `--out <dir>` + fixed filename; aligns with other commands |
| Reimplement CSV parsing | No | csv_loader already battle-tested with full unit coverage |
| Write multi-file dirs as array vs multiple files | Array | Single schema-profile.json is simpler; array preserves all profiles |


# Decision: Package, Deploy-Lakehouse, Compile-Search Sprint 1 Implementation

**Author:** Keyser (Lead/Architect)  
**Date:** 2026-06-25  
**Sprint:** 1  
**Status:** Implemented

---

## Context

Sprint 1 required replacing three CLI stubs with functional implementations and writing unit tests for each. This decision record captures the design choices made during implementation.

---

## Decisions

### 1. `package` — dist layout

**Decision:** `dist/fabric-kg-package/` directory (not a zip) containing `parquet/`, `ontology/`, optional `search/`, and `manifest.json`.

**Rationale:**
- SPEC-001 §8 specifies `dist/` with `parquet/`, `ontology/`, `search/` subdirs and `manifest.json`. A named subdirectory (`fabric-kg-package`) provides clean namespacing and makes re-runs idempotent (directory is deleted and recreated).
- Manifest `schema_version`, `created_at`, per-artifact `file_count`/`total_bytes`/`files[]` gives operators enough info to audit what was bundled without opening every file.
- `build/parquet` and `build/ontology` are **required** (exit 1 if absent). `build/search` is optional via `--include-search`.

**Alternatives considered:** Zip archive — rejected because it adds a decompression step before inspection and complicates incremental re-deploys.

---

### 2. `deploy-lakehouse` — offline mock, env JSON direct read

**Decision:** Sprint 1 mock reads `ontology/environments/{env}.json` directly (no `load_config`) to get `workspace_id` and `lakehouse_item_id`. Reports what WOULD be uploaded; exits 0. Leaves a `# TODO Sprint 2` seam for fabric-cicd.

**Rationale:**
- `load_config` requires `AZURE_AI_FOUNDRY_ENDPOINT` (fail-fast EnvironmentError). A mock deploy command should work fully offline without any credential setup.
- Reading only the `fabric` section of the env JSON is safe and sufficient — no secrets needed to know the target workspace/lakehouse.
- fabric-cicd is the primary path per SPEC-003 §9.1.1; the seam is a single `# TODO Sprint 2` block that replaces the mock log block.

**Seam location:** `deploy_cmd.py` → `deploy_lakehouse_cmd()` → comment block after the mock log section.

---

### 3. `compile-search` — placeholder schemas only, no documents

**Decision:** Sprint 1 emits `build/search/kg-chunks/index.schema.json` and `build/search/kg-document-elements/index.schema.json` only. No Parquet reads, no embeddings, no AI Search calls.

**Rationale:**
- Full document generation requires embedding + Parquet reads, which is Sprint 2 scope.
- Emitting the schema now unblocks: (a) deploy-search smoke testing, (b) AI Search index creation can be automated from the schema, (c) field contracts are locked for Sprint 2 implementation.
- Sprint 2 will read from `build/parquet/`, call `text-embedding-3-large@1536`, and write `documents/batch-*.json` alongside the existing schemas.

**Schema design choices (SPEC-002 §11.3/§11.4 compliant):**
- `entity_ids`: `Collection(Edm.String)`, filterable only — never searchable (label-vs-ID anti-pattern prevention)
- `entity_aliases`: `Collection(Edm.String)`, searchable only — never filterable
- `canonical_key`: `Edm.String`, filterable — stable exact-match
- `chunk_vector` / `element_vector`: `Collection(Edm.Single)`, dimensions=1536, LOCKED — matches `text-embedding-3-large` (SPEC-002 §11.7; changing requires full reindex)
- `graph_path`: `Edm.String`, retrievable only — injected at push time, not stored in Parquet
- `blob_url`, `source_path`, `last_modified`, `content_type`: filterable + retrievable per SPEC-002 §11.3 mapping

---

### 4. `dev.json` verification — PASSED

`load_config('dev')` returns:
- `fabric.workspace_id`: `11111111-1111-1111-1111-111111111111` ✓
- `fabric.lakehouse_item_id`: `44444444-4444-4444-4444-444444444444` ✓

Both values match SPEC-003 §9.1.1 (Lakehouse `kg_lakehouse`, provisioned 2026-06-24). No missing fields. Task marked complete.

---

## Test Results

- **New tests:** 38 (in `tests/unit/test_package_deploy_search.py`)
- **Total passing:** 346 / 346
- **Coverage highlights:** `package_cmd.py` 100%, `compile_search_cmd.py` 98%, `deploy_cmd.py` 92%

---

## Open Items / Sprint 2 TODOs

1. `deploy_lakehouse_cmd`: replace mock with `fabric-cicd` primary path + REST fallback (SPEC-003 §9.1.1)
2. `compile_search_cmd`: read Parquet tables, generate embedding-based document batches per SPEC-001 §7
3. `deploy_search_cmd`: implement AI Search index create/update + document upload
4. `package_cmd`: consider adding SHA-256 checksums per file in the manifest for integrity verification


# Decision: compile-search Sprint 2 + deploy-search Implementation

**Date:** 2026-06-24T18:04:21.340-07:00  
**Author:** Keyser  
**Sprint:** 2  
**Status:** Implemented

---

## Context

Sprint 1 `compile-search` emitted placeholder index schemas only. Sprint 2 required:
1. Full document generation from canonical Parquet → AI Search docs
2. `deploy-search` with real env-config reading and mock upsert

---

## Decisions Made

### D1 — Compose search/linkage.py, don't duplicate

`compile_search_cmd.py` imports `derive_chunk_doc`, `derive_document_element_doc`, `build_entity_lookup` from `search.linkage` rather than re-implementing field mapping inline. This keeps field logic in one place (Fenster's module) and the CLI as a thin orchestrator.

### D2 — Mock = default for push.py

`search.push.push_documents` / `push_index` default to `mock=True` and check `AZURE_SEARCH_ENDPOINT`. No live call happens in tests or when the endpoint env var is absent. This is correct: AI Search uses its own SDK (`azure-search-documents`), not `fabric-cicd` (which is Fabric items only).

### D3 — embed=False default; live embed auto-disabled when endpoint absent

`search.embeddings.attach_vectors` checks `AZURE_AI_FOUNDRY_ENDPOINT`. When absent (all offline/test runs), it fills zero vectors. This means `--embed` flag is safe to pass in CI without credentials — it degrades to zero-fill rather than erroring.

### D4 — Schema always written; docs.json only when Parquet found

`compile-search` writes `index.schema.json` unconditionally (correct — schema must exist for downstream `deploy-search`). `docs.json` is written only when the source Parquet table has rows. Empty-input runs exit 0 with a NOTE line.

### D5 — deploy-search reads ai_search section directly (no full load_config)

Same pattern as `deploy-lakehouse` using `_read_fabric_env_config`: read the env JSON directly without requiring `AZURE_AI_FOUNDRY_ENDPOINT`. This keeps deploy-search fully offline/mock without credential dependencies.

### D6 — enabled flag short-circuits

When `ai_search.enabled = false` in the env JSON, `deploy-search` logs the skip reason and exits 0 immediately. No index or doc push is attempted.

---

## Test Coverage

- `tests/unit/test_compile_deploy_search_sprint2.py` — 38 tests:
  - `TestLinkageDerivation` (11 tests): field presence, entity_ids, entity_aliases, canonical_key, entity_types, graph_path=None
  - `TestSearchPush` (5 tests): PushResult, push_from_build_dir mock/missing schema/no-docs
  - `TestCompileSearchSprint2` (11 tests): docs.json written, field values, schema 1536 dims, no-parquet case, no-network
  - `TestDeploySearchCmd` (11 tests): exits 0, service name, index names, enabled=false, doc count, no-network, missing env, SUCCESS message, read_search_env_config, real dev.json

---

## AI Search vs fabric-cicd

**Clarification (important for future agents):** `fabric-cicd` is exclusively for Fabric workspace items (Lakehouses, notebooks, semantic models, data pipelines). AI Search index creation and document upload use the `azure-search-documents` Python SDK (`SearchIndexClient`, `SearchClient`). Never route AI Search operations through fabric-cicd.

---

## Open Items

- Live `push_index`: the current live path creates a minimal `SearchIndex(name=...)` without field mappings. A full implementation must convert `schema["fields"]` dicts to `SearchField` objects — Sprint 3 work.
- `--embed` live mode: tested only via zero-fill mock. Live test requires `AZURE_AI_FOUNDRY_ENDPOINT` to be set in CI.


# Decision Note: Sprint 1 Scaffold

**Author:** Keyser  
**Date:** 2026-06-24T16:02:24.189-07:00  
**Tasks completed:** repo-skeleton, pyproject-setup, cli-entrypoint, cli-stubs  
**Status:** Implemented and verified

---

## What was decided / implemented

### 1. Package layout
10-module package under `src/fabric_kg_builder/`:
`cli`, `config`, `sources`, `enrichment`, `model`, `parquet`, `ontology`, `search`, `deploy`, `validate`
Each has an `__init__.py` with a module docstring. Follows SPEC-001 §3 exactly.

### 2. CLI framework
**Click 8.x** — confirmed per SPEC-001 §4 and decisions.md decision 1. NOT Typer.
Entry point: `fabric-kg = fabric_kg_builder.cli:main`

The `cli/` module is a package (directory). Entry point resolves via:
`cli/__init__.py` → re-exports `main` from `cli/main.py`

### 3. Foundry SDK package
**`azure-ai-projects>=1.0`** — the Microsoft Foundry SDK package, per SPEC-001 §4 and decisions.md decision 10.
A `# TODO: verify Foundry SDK pkg` comment is included in `pyproject.toml` as requested.

### 4. Dependencies added to pyproject.toml
- `azure-ai-projects>=1.0` (Foundry SDK)
- `fabric-cicd>=0.1` (Fabric deployment — required per SPEC-001)
- All other deps per SPEC-001 §4: click, pyarrow, pydantic, pyyaml, python-dotenv, azure-identity, azure-search-documents, azure-storage-blob, azure-ai-documentintelligence
- Dev: pytest>=7.4, pytest-cov>=4.0

### 5. CLI stub commands
13 stub commands registered, each parsing their canonical flags from SPEC-001 §7.2:
`init`, `set-domain`, `inspect-source`, `enrich`, `compile-data`, `compile-ontology`,
`compile-search`, `package`, `deploy-lakehouse`, `deploy-ontology`, `deploy-search`,
`validate`, `build-deploy`

### 6. .gitignore
Extended to exclude: `build/`, `dist/`, `__pycache__/`, `*.egg-info/`, `.env`, `.env.*` (not `.env.example`), `.venv/`, `build/**/*.parquet`, `.pytest_cache/`, `.coverage`

---

## Verification results

```
pip install -e .  →  Successfully installed fabric-kg-builder-0.1.0 (+ fabric-cicd-1.1.0)

fabric-kg --help  →  Lists all 13 commands:
  build-deploy, compile-data, compile-ontology, compile-search,
  deploy-lakehouse, deploy-ontology, deploy-search, enrich, init,
  inspect-source, package, set-domain, validate
```

---

## Open item

The `azure-ai-projects` package version / API surface should be re-verified once
SPEC-004 (LLM enrichment spec, owned by Verbal) is finalized and the Foundry SDK
call patterns are confirmed. The TODO comment in `pyproject.toml` marks this.



### mcmanus

# Decision: compile-ontology + deploy-ontology (Mock) Implementation

**Date:** 2026-06-24T17:26:00-07:00
**Author:** McManus (KG/Ontology Dev)
**Sprint:** 1
**Status:** Implemented and tested

---

## Context

Sprint 1 requires two CLI commands to be wired:
1. `compile-ontology` — run `OntologyCompiler` on `ontology/model.yaml` + `ontology/ids.lock.json`, emit Fabric definition parts to `build/ontology/`.
2. `deploy-ontology` — mock-deploy the compiled parts offline; structure for fabric-cicd as the real primary path.

The `OntologyCompiler` class in `src/fabric_kg_builder/ontology/compiler.py` was already fully implemented in a previous session. Both CLI stubs existed but emitted `"not implemented yet"` messages.

---

## Decisions Made

### 1. Compile-ontology reuses OntologyCompiler directly — no reimplementation

The compiler's `compile(out_dir)` method already handles all file writing. The CLI command resolves default paths (`ontology/model.yaml`, `ontology/ids.lock.json`), reads the lakehouse ID from the env JSON, constructs the compiler, calls `compile()`, then prints a summary. Exit codes: 0 = success, 1 = I/O or unknown error, 5 = `OntologyCompilerError` (validation failure).

### 2. `--env` added to compile-ontology for lakehouse ID resolution

The compiler needs `lakehouse_id` to embed the correct Lakehouse ID in `DataBinding` and `Contextualization` files. Rather than require a separate `--lakehouse-id` flag, `--env dev` is used to read `fabric.lakehouse_item_id` from `ontology/environments/{env}.json` via the new `load_fabric_ids()` helper. This keeps the compile command self-consistent with the deploy command's env targeting.

### 3. `load_fabric_ids()` bypasses the full config loader

`load_config()` requires `AZURE_AI_FOUNDRY_ENDPOINT` and fails without it. Compile and deploy only need `workspace_id` and `lakehouse_item_id`. A new `load_fabric_ids(env, environments_dir)` function in `config/loader.py` reads only the env JSON's `fabric` section — no secret env vars required. This keeps offline/CI workflows unblocked.

### 4. FabricDeployer abstraction as the deploy seam

`src/fabric_kg_builder/deploy/fabric_deployer.py` provides:
- `FabricDeployer(workspace_id, parts, mock=True)` — constructor
- `deploy()` — dispatches to `_deploy_mock()` or raises `NotImplementedError` for live path
- `from_build_dir(build_dir, workspace_id, mock)` — class method that reads `definition.json`

The `deploy()` body has a clear `# TODO (SPEC-003 §9.1): fabric-cicd primary path` comment. When fabric-cicd is wired, only that method body changes — no interface or test changes needed.

### 5. deploy-ontology defaults to mock=True via `--mock/--no-mock`

Per SPEC-003 §8/§9, the deploy command must be testable offline. `--mock/--no-mock` with `default=True` means running `deploy-ontology --env dev` without any flags runs in mock mode. The seam for live deployment is explicit but requires `--no-mock` to activate — preventing accidental live calls.

### 6. `--dist` default aligned to `build/ontology` (not `dist`)

The compile step writes to `build/ontology/`; deploy reads from the same dir. Changing `deploy-ontology --dist` default from `dist` to `build/ontology` means the default pipeline (`compile-ontology` → `deploy-ontology --env dev`) works without any extra flags.

---

## Files Changed

| File | Change |
|------|--------|
| `src/fabric_kg_builder/cli/compile_ontology_cmd.py` | Full implementation replacing stub |
| `src/fabric_kg_builder/cli/deploy_cmd.py` | `deploy_ontology_cmd` implemented; imports added |
| `src/fabric_kg_builder/deploy/fabric_deployer.py` | New file: FabricDeployer class |
| `src/fabric_kg_builder/config/loader.py` | Added `load_fabric_ids()` helper |
| `tests/unit/test_compile_ontology_cmd.py` | New: 11 unit tests |
| `tests/unit/test_deploy_ontology_cmd.py` | New: 6 unit tests |

---

## Test Results

- **17 new tests added** (11 compile-ontology, 6 deploy-ontology) — all pass
- **Total unit suite: 346 passed** (up from 274 baseline; the +72 gain includes the 17 new tests plus previously-blocked CLI smoke tests unlocked by the stale pyc fix)
- No network calls in any test — all offline/deterministic

---

## Open TODOs (fabric-cicd integration)

- `FabricDeployer.deploy()` — wire `from fabric_cicd import FabricWorkspace, publish_all_items` per SPEC-003 §9.1
- `deploy-ontology --no-mock` path — currently exits 1 with "live deploy not yet implemented"; Sprint 2 scope
- Sensitivity label GUID resolution (SPEC-003 §9.6) — not in scope for Sprint 1


# Decision: mcmanus-sprint1-compiler

**Date:** 2026-06-24T17:01:06-07:00  
**Author:** McManus (KG/Ontology Dev)  
**Status:** Implemented  
**Requested by:** Hyunsuk Shin  

---

## Context

Sprint 1 required a working `ontology-compiler` that reads `ontology/model.yaml` +
`ontology/ids.lock.json` and emits the full Fabric Ontology definition part tree per
SPEC-003 §6–§8.

---

## Decision

Implemented `src/fabric_kg_builder/ontology/compiler.py` with:

### Architecture

- **`OntologyCompiler` class** — validates at construction; exposes two public methods:
  - `compile(out_dir)` — writes all files to disk (`.platform`, `definition.json`, per-type dirs)
  - `get_rest_parts()` — returns the InlineBase64 REST payload list without touching disk
- **`_iter_parts()` internal** — single source of truth for all part content; both public methods delegate here; no duplication
- **`_validate()` function** — four gate checks at construction time: entity IDs present, relationship IDs present, no duplicate IDs, source/target types valid; raises `OntologyCompilerError` immediately

### Output structure (SPEC-003 §6.2)

```
build/ontology/
  .platform                                        ← Fabric item metadata
  definition.json                                  ← manifest; all parts with Base64 payloads
  EntityTypes/{typeId}/definition.json
  EntityTypes/{typeId}/DataBindings/{guid}.json
  RelationshipTypes/{typeId}/definition.json
  RelationshipTypes/{typeId}/Contextualizations/{guid}.json
```

### ID/GUID strategy

- **typeIds** read directly from `ids.lock.json` — never regenerated
- **binding/contextualization GUIDs** are deterministic UUIDv5:
  `uuid5(uuid5(NAMESPACE_DNS, "FabricKG"), f"{type_name}:{table}")` — stable across environments and re-runs
- **logicalId** (in `.platform`) derived as `uuid5(_ONTOLOGY_NS, "ontology:logicalId")`

### Key property rules

- `blob_url` type in model.yaml → `"type": "String", "format": "uri"` in Fabric definition JSON
- `inverseTypeId` emitted only when `inversePolicy` is `materialize` or `alias`; looked up via `inverseName` in ids.lock.json
- `typeFilterColumn` + `typeFilterValue` both included or both omitted per binding

---

## Tests (44 new in `tests/unit/test_compiler.py`)

Coverage areas:
- Directory structure (`.platform`, `definition.json`, EntityTypes/RelationshipTypes dirs)
- Locked IDs in definition files match ids.lock.json
- `blob_url` properties have `format: uri` on Figure, ImageAsset, VisualRegion
- `inverseTypeId` present for materialize/alias policies; absent for none
- REST InlineBase64 parts decode to valid JSON
- Top-level `definition.json` manifest lists all emitted parts
- Deterministic GUID derivation (stable, distinct by type/table)
- Validation errors: missing entity ID, missing relationship ID, unknown source/target, duplicate ID

**Result:** 225 tests pass (44 new + 181 pre-existing). Compiler module: 100% coverage.

---

## Trade-offs / Rejected Alternatives

- **Env config injection at compile time** — lakehouse_id is an optional constructor param
  (empty string default). Tests run without an env file. Production invocations pass the
  value from `ontology/environments/{env}.json`. This keeps the compiler pure and testable
  without mocking the filesystem config loader.
- **No inline model validation for `blob_url` gate** — SPEC-003 §7 requires the compiler to
  fail if visual types are missing `blob_url` properties. The current implementation does NOT
  add that gate to `_validate()` — that gate belongs in the separate `validate` module
  (BRG-003 equivalent) which runs as a pre-deploy check, not during compilation. The tests
  verify correct `format: uri` emission for types that DO have it; the gate is deferred.
- **`definition.json` not included in its own parts list** — the manifest file lists all
  other parts as InlineBase64 entries but does not include itself. This matches the Fabric
  REST API contract where the definition.json is the request body, not a listed part.


# Decision: Sprint 2 Bridge Validation — compile-ontology-e2e

**ID:** mcmanus-sprint2-bridge  
**Date:** 2026-06-24T18:04:21-07:00  
**Author:** McManus (KG/Ontology Dev)  
**Status:** Implemented  
**Requested by:** Hyunsuk Shin  

---

## Context

Sprint 2 hardened the ontology compile path against the full canonical data set (8 Parquet tables including document/visual). The `compile-ontology` command previously had no validation of the graph-to-search bridge relationships. Any mis-binding between `evidenced_by`/`shown_in`/`indexed_as` and the canonical columns (`chunks.related_entity_ids`, `chunks.entity_search_keys`, `visual_assets.blob_url`, etc.) would silently produce a broken ontology that Phase 1 traversal could not use.

---

## Decision

### 1. New module: `src/fabric_kg_builder/ontology/bridge_validation.py`

Implements all 10 BRG validation gates (SPEC-003 §12.9) as a pure function:

```python
def validate_bridge(model: dict) -> list[BridgeViolation]
```

Returns a flat list of `BridgeViolation(gate_id, severity, message)` objects. No I/O. No side effects. Re-usable by future `fabric-kg validate` command and CI scripts.

**Gates implemented:**

| Gate | Check | Severity |
|---|---|---|
| BRG-001 | `DocumentChunk` declares `entity_id`, `chunk_id`, `related_entity_ids`, `entity_search_keys` AND binds them to canonical `chunks` columns | Error |
| BRG-002 | `SearchIndexRecord` declares `search_record_id` AND data binding includes `chunk_id` column | Error |
| BRG-003 | Every `support-domain` entity type declares `entity_id`, `canonical_key`, `search_aliases` | Error |
| BRG-004 | `ImageAsset` and `Figure` declare `blob_url` property (type `blob_url`) AND bind the column | Error |
| BRG-005 | `evidenced_by`, `shown_in`, `indexed_as` all exist in model with `inversePolicy` key present | Error |
| BRG-006 | `indexed_as` target (`SearchIndexRecord`) has `search_record_id` property and `chunk_id` column bound | Error |
| BRG-007 | `evidenced_by` target (`DocumentChunk`) has `chunk_id` property and column bound | Error |
| BRG-008 | `shown_in` target (`Figure`/`ImageAsset`) has `blob_url` property and column bound | Error |
| BRG-009 | `support-domain` entities with no outbound bridge edge | **Warning only** |
| BRG-010 | `entity_id` column binding on `support-domain` nodes is non-empty | Error |

### 2. Wired into `compile-ontology` CLI

Bridge validation runs **after** `compiler.compile()` (the full Fabric tree is written to disk regardless). Error violations cause `sys.exit(5)` (same exit code as model validation failures). Warnings are logged but do not block. The summary includes `Bridge validation: N errors, M warning(s)`.

### 3. Tests: 35 new unit tests in `tests/unit/test_bridge_validation.py`

- `TestRealModelBridgeValidation`: shipped `model.yaml` produces **0 errors**
- `TestBRG001` through `TestBRG010`: one class per gate, covering property violations + column binding violations + valid-no-error cases
- `TestCompileOntologyBridgeValidationCLI`: CLI integration tests — exit 0 for real model, exit 5 for broken model, summary line present, full tree still produced

---

## Result

```
fabric-kg compile-ontology --out build\ontology

[compile-ontology] Bridge validation: OK (0 errors, 9 warning(s))

SUMMARY
  Entity types      : 35
  Relationship types: 36
  Parts written     : 144
  Bridge validation : 0 errors, 9 warning(s)
  Output directory  : ...build\ontology
```

The 9 BRG-009 warnings are expected: the shipped model only declares one instance each of `evidenced_by` (sourceType: Part) and `shown_in` (sourceType: Step). The remaining 9 support-domain entity types have no outbound bridge edge and fall back to pure AI Search — this is by design for Sprint 2. Future sprint can add bridge edges for other entity types.

**pytest: 529 passed** (440 pre-existing + 35 new bridge validation + 54 others in cumulative suite).

---

## Tradeoffs Considered

| Option | Verdict |
|---|---|
| Run bridge validation at construction time (inside `OntologyCompiler.__init__`) | Rejected — compiler is a general-purpose tool; keeping bridge validation separate preserves composability and lets `compile-ontology` decide severity policy independently |
| Exit 1 for bridge errors (instead of 5) | Rejected — exit 5 is already established for model validation failures; bridge errors are a class of model validation failure and should use the same code |
| Fail on BRG-009 warnings | Rejected — SPEC-003 explicitly marks BRG-009 as Warning severity; blocking on missing bridge edges would prevent partial models from compiling during development |
| Check actual Parquet data for BRG-006/007/008 resolution | Out of scope for ontology compilation — those gates check the MODEL structure (entity type definitions), not runtime data. Runtime data checks belong in `fabric-kg validate` post-deploy. |



### verbal

# verbal-enrich-resilience

**Date:** 2026-06-24T19:58:00-07:00  
**Author:** Verbal (AI Integration Dev)  
**Requested by:** Hyunsuk Shin  
**Status:** Implemented ✅

---

## Problem

Live run on a Surface PDF (gpt-5-4-mini, real output) returned valid entities and relationships but evidence items came back as `{"text": "...", "confidence": 0.98}` — WITHOUT `id_hint` or `source_type`. The `Evidence` Pydantic model marked both as required `str`, so `validate()` raised `ValidationError` and `enrich` exited 4, aborting the whole file. Zero usable output was written.

---

## Root Cause

`output_schema.py` declared `Evidence.id_hint: str` and `Evidence.source_type: str` (required). Per SPEC-004 §1.1, the LLM provides **hints** and the canonicalize step mints stable IDs — requiring perfect evidence IDs from the model is architecturally wrong and brittle.

---

## Decision: Make evidence hints optional; synthesize deterministically; never hard-fail a batch

### 1. Schema relaxation

- `Evidence.id_hint` → `Optional[str] = None`
- `Evidence.source_type` → `Optional[str] = None`
- `Chunk.id_hint` → `Optional[str] = None`

This is a **backwards-compatible** schema change: existing callers that provide these fields see no change. Payloads omitting them now parse successfully.

### 2. Canonicalize synthesis

`canonicalize_llm_output()` gains `default_source_type: str = "document_span"` parameter.

- Missing `ev.source_type` → filled from `default_source_type`
- Missing `chunk.id_hint` → synthesized via `make_chunk_id()` from content hash (deterministic)
- All synthesis is deterministic: same input → same ID every run

Context flows from caller:
- `enrich_documents()` passes `default_source_type="document_span"`
- CSV path in `enrich_cmd.py` passes `default_source_type="csv_row"`

### 3. Per-item resilience

Every `for item in output.X` loop in `canonicalize_llm_output()` is wrapped in `try/except`. Unsalvageable items are dropped with `logging.warning()`; the rest are kept. A single bad entity/chunk/evidence row cannot blow up the whole batch.

### 4. Batch-level resilience in `enrich_batch()`

`validate()` failures attempt a light coercion pass: inject missing `source_file_id` / `pass` envelope fields, then retry. If the second attempt also fails, the pass is skipped (logged at ERROR) and remaining passes continue. The file is never aborted because of one bad pass. The checkpoint is written after all passes complete, preserving resume support.

### 5. System prompt strengthened (best-effort guidance)

`_ENRICH_SYSTEM_PROMPT` now explicitly asks the model for `id_hint` and `source_type` on evidence items and `id_hint` on chunks as best-effort. The security invariant is preserved: domain/user text is USER-only, the system prompt remains a fixed trusted literal (SPEC-004 §2.3).

---

## Files Changed

| File | Change |
|------|--------|
| `src/fabric_kg_builder/enrichment/output_schema.py` | `Evidence.id_hint`, `Evidence.source_type`, `Chunk.id_hint` → Optional |
| `src/fabric_kg_builder/enrichment/orchestrator.py` | Strengthened system prompt; `canonicalize_llm_output` synthesis + per-item try/except; `enrich_batch` coerce-then-retry + resilient pass skipping; `default_source_type` parameter throughout |
| `src/fabric_kg_builder/cli/enrich_cmd.py` | CSV path passes `default_source_type="csv_row"` |
| `tests/unit/test_output_schema.py` | Updated `test_validate_rejects_evidence_missing_source_type` → now tests that source_type defaults to None (no longer a required field); added two more optional-field tests |
| `tests/unit/test_enrich_resilience.py` | **New file** — 19 tests covering the Surface PDF pattern end-to-end |

---

## Test Result

```
671 passed, 4 deselected   (was 652 before this fix)
```

All 19 new tests pass. All 652 existing tests remain green.

---

## Invariants Preserved

- **Security:** Domain/user text stays in USER message only. System prompt is a fixed literal. ✅
- **Determinism:** All synthesized IDs are SHA-256 content-hash based — same input → same ID. ✅
- **Checkpoint/resume:** `enrich_batch` still writes `.checkpoint.json` after all passes. ✅
- **response_format:** `json_object` mode unchanged (proven working with gpt-5-4-mini). ✅
- **Exit semantics:** Only exit non-zero if the process itself errors (file not found, client build failure). A batch that produces usable output exits 0. ✅


# Decision: verbal-enrich-utf8-entities

**Date:** 2026-06-24T20:07:00-07:00  
**Author:** Verbal (AI Integration Dev)  
**Status:** Implemented  
**Requested by:** Hyunsuk Shin  

## Context

A live run of `fabric-kg enrich` on a real Surface Pro PDF (126 document elements, 422KB output) produced a canonical JSON with `entities=0`, `relationships=0`, and exited with code 4 (`UnicodeEncodeError` on `→`). Three root causes were identified.

---

## Bug 1 — UnicodeEncodeError on Windows cp1252 console

**Symptom:** Exit 4; `'charmap' codec can't encode character '\u2192'`.

**Root cause:** `click.echo(f"[enrich] enriched {name} → {out_dir}")` calls `sys.stdout.write()` directly. On Windows, the default console encoding is cp1252, which cannot encode → (U+2192). The `UnicodeEncodeError` is caught by the per-file `try/except`, increments `errors`, and triggers `ctx.exit(4)`.

**Decision:** Add `_configure_utf8_console()` to `cli/main.py`, called at the very start of `main()` before `cli()` runs. Reconfigures `sys.stdout` and `sys.stderr` to `encoding='utf-8', errors='replace'` on `sys.platform == 'win32'`, guarded by `hasattr(stream, 'reconfigure')` for safety. `errors='replace'` ensures any remaining unencodable characters become `?` rather than crashing.

**Files changed:** `src/fabric_kg_builder/cli/main.py`

---

## Bug 2 — entities=0 / relationships=0 in canonical JSON

**Symptom:** 422KB canonical JSON written successfully but with `entities=0` and `relationships=0` despite the LLM returning entities like `Surface Pro 7` and part numbers.

**Root cause:** `enrich_documents` formerly concatenated ALL 126 document elements into a single LLM call. The LLM's response included `chunks` items missing `chunk_type` and/or `content` (required pydantic fields). This caused `ValidationError` in `validate()`, and after coercion also failed, the pass was skipped entirely via `continue`. Entities and relationships from that pass were never added to `all_records`.

**Decision — section batching in `enrich_documents`:**  
- Group elements by `section_path` using `defaultdict(list)`, keyed by `section_path or "__root__"`.
- Call `enrich_batch` once per section with a section-specific `batch_key = f"{source_file_id}:section:{section_key}"`.
- Each section call is wrapped in `try/except`; an exception logs an error and skips that section (`continue`), preserving results from all other sections.
- After all sections, write `source_file_id` to the checkpoint for document-level resume.

**Decision — `batch_key` parameter on `enrich_batch`:**  
Add `batch_key: str | None = None`. When set, it drives the checkpoint entry and intermediate JSON filename (`effective_key = batch_key or source_file_id`). `source_file_id` continues to drive canonical record FKs (entity provenance etc.). Fully backward compatible — callers without `batch_key` are unchanged.

**Files changed:** `src/fabric_kg_builder/enrichment/orchestrator.py`

---

## Bug 3 — chunks missing chunk_type/content abort the pass

**Symptom:** Same as Bug 2; often happens together.

**Root cause:** `Chunk.chunk_type: str` and `Chunk.content: str` were required non-optional pydantic fields. A LLM response that omitted either field triggered `ValidationError`, aborting the entire pass.

**Decision:**  
- Make both `Optional[str] = None` in `output_schema.py`.
- In `canonicalize_llm_output`, add an early check: if `not chunk.content`, increment `dropped_chunks`, log a warning, and `continue`. This avoids `content_hash(None)` and silently drops incomplete LLM-supplied chunks.
- LLM-supplied chunks are supplementary; the authoritative chunks come from the `Chunker`. Dropping incomplete LLM chunks is safe and correct.

**Files changed:** `src/fabric_kg_builder/enrichment/output_schema.py`, `src/fabric_kg_builder/enrichment/orchestrator.py`

---

## Tests Added

**`tests/unit/test_utf8_unicode_enrich.py`** — 12 new unit tests:

| Class | Count | Covers |
|---|---|---|
| `TestUtf8ConsoleReconfiguration` | 5 | `_configure_utf8_console()` reconfigures on win32, no-ops on Linux, silences exceptions, handles streams without `reconfigure`, enrich echo exits 0 with `→` |
| `TestMultiSectionEntityCapture` | 4 | Two sections aggregate entities+rels; bad section doesn't abort others; section+doc checkpoint keys written; document-level resume skips all LLM calls |
| `TestChunkLeniency` | 3 | Chunk with `content=None` dropped; entities survive all-malformed chunks; `enrich_batch` end-to-end captures entities despite malformed chunks |

**Result:** 683 passed (was 671), 4 deselected (integration). No regressions.

---

## Invariants preserved

- Security: domain text still only in USER message (no change to prompt building).
- `enrich_batch` called directly (CSV path) is unchanged; no breaking API change.
- Checkpoint files written with `encoding="utf-8"` throughout.
- Canonical JSON `entities`/`relationships` arrays now always populated from all sections that the LLM returns data for.


# Decision: Foundry SDK — Replace AIProjectClient with Verified AzureOpenAI Path

**Date:** 2026-06-24  
**Author:** Verbal (AI Integration Dev)  
**Requested by:** Hyunsuk Shin  
**Status:** Implemented & verified (live smoke + 652 unit tests green)

---

## Context

`FoundryClient._build_sdk_client` previously used `azure.ai.projects.AIProjectClient`
with a call chain that was explicitly marked TODO and was never live-tested:

```python
# OLD (unverified, TODO-marked)
client.inference.get_chat_completions_client().complete(model=..., messages=...)
client.inference.get_embeddings_client().embed(input=..., model=..., dimensions=...)
```

Hyunsuk Shin confirmed via live testing that the correct path is `openai.AzureOpenAI`
targeting `https://example-aiservices.openai.azure.com/` (distinct from the Foundry project
URL `services.ai.azure.com/api/projects/...`).

---

## Decision

Replace `AIProjectClient` with `openai.AzureOpenAI` across the entire SDK integration layer.

### Verified call pattern

```python
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

tp = get_bearer_token_provider(
    DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
)
client = AzureOpenAI(
    azure_endpoint="https://example-aiservices.openai.azure.com/",
    azure_ad_token_provider=tp,
    api_version="2024-12-01-preview",
)

# Chat (json_object mode — confirmed working with gpt-5-4-mini):
r = client.chat.completions.create(
    model="gpt-5-4-mini",
    messages=[{"role": "system", ...}, {"role": "user", ...}],
    response_format={"type": "json_object"},
    temperature=0.0, seed=42,
)
content = r.choices[0].message.content   # JSON string

# Embeddings (1536 dims confirmed):
e = client.embeddings.create(model="embedding", input=[...], dimensions=1536)
vecs = [d.embedding for d in e.data]
```

### Auth

- Default: `get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")`
- Override: if `AZURE_AI_FOUNDRY_API_KEY` or `AZURE_OPENAI_API_KEY` is set, use `api_key=` instead

---

## Changes Made

| File | Change |
|------|--------|
| `src/fabric_kg_builder/enrichment/foundry_client.py` | Full rewrite of `_build_sdk_client`, `complete_json`, `embed` to use `openai.AzureOpenAI` |
| `src/fabric_kg_builder/config/schema.py` | Added `FoundryConfig.openai_endpoint` (default `""`) and `api_version` (default `"2024-12-01-preview"`) |
| `src/fabric_kg_builder/config/loader.py` | Added resolution of `openai_endpoint` and `api_version` from env/yaml/json |
| `src/fabric_kg_builder/search/embeddings.py` | Updated live path in `attach_vectors()` from `AIProjectClient` to `AzureOpenAI` |
| `ontology/environments/dev.json` | Added `foundry.openai_endpoint` and `api_version` |
| `fabric-kg.yaml` | Added `foundry.openai_endpoint: ${AZURE_OPENAI_ENDPOINT}` |
| `.env.example` | Added `AZURE_OPENAI_ENDPOINT` variable |
| `tests/conftest.py` | Updated `make_foundry_client` to `client.chat.completions.create` pattern |
| `tests/unit/test_foundry_client.py` | Updated all mock assertions to new call chain |
| `tests/unit/test_domain.py` | Updated `_make_client` + assertions |
| `tests/unit/test_enrich_cmd.py` | Updated `_make_client` + assertions + side_effect |
| `tests/unit/test_enrich_cmd_pdf.py` | Updated `_make_client` + side_effects |
| `tests/unit/test_enrich_documents.py` | Updated `_make_client` + side_effects |
| `tests/unit/test_orchestrator.py` | Updated `_make_client` + side_effect |

---

## Endpoint Notes

Two URLs exist for one Azure AI service account (`example-aiservices`):

| Purpose | URL | Variable |
|---------|-----|----------|
| Foundry project (AIProjectClient etc.) | `https://example-aiservices.services.ai.azure.com/api/projects/example-project` | `AZURE_AI_FOUNDRY_ENDPOINT` |
| Azure OpenAI SDK (`openai.AzureOpenAI`) | `https://example-aiservices.openai.azure.com/` | `AZURE_OPENAI_ENDPOINT` ← **NEW** |

Do not confuse them. `FoundryConfig.endpoint` keeps the Foundry project URL.
`FoundryConfig.openai_endpoint` is the new field for the OpenAI SDK.

---

## Verification

```
pytest tests -q -m "not integration"   → 652 passed, 4 deselected (identical to pre-change baseline)

Live smoke:
  FoundryClient(load_config('dev').foundry).embed(["test"]) → dims: 1536  ✅
```


# Decision Record: Sprint 1 Enrichment Implementation

**Author:** Verbal (AI Integration Dev)  
**Date:** 2026-06-24T17:15:00-07:00  
**Requested by:** Hyunsuk Shin  
**Status:** Implemented and tested

---

## Summary

Sprint 1 enrichment is fully implemented: domain intake (`set-domain`), enrichment orchestrator, CLI wiring (`enrich`), and 39 new unit tests. All 237 pre-existing tests continue to pass; total is now **274 passed**.

---

## Files Created / Modified

| File | Action | Purpose |
|------|--------|---------|
| `src/fabric_kg_builder/enrichment/domain.py` | Created | DomainBrief model, `rephrase_domain()`, `save/load_domain_brief()` |
| `src/fabric_kg_builder/enrichment/orchestrator.py` | Created | `canonicalize_llm_output()`, `enrich_batch()`, checkpoint/resume |
| `src/fabric_kg_builder/cli/set_domain_cmd.py` | Replaced | Wires `set-domain` CLI to `domain.py`; `ctx.obj` client injection |
| `src/fabric_kg_builder/cli/enrich_cmd.py` | Replaced | Wires `enrich` CLI to orchestrator; domain brief resolution |
| `tests/unit/test_domain.py` | Created | 15 tests: rephrase, save/load, security assertions |
| `tests/unit/test_orchestrator.py` | Created | 16 tests: canonicalize, dedup, confidence threshold, checkpoint/resume |
| `tests/unit/test_enrich_cmd.py` | Created | 8 tests: set-domain writes domain.json, enrich exits 0 with mock |

---

## Key Architectural Decisions

### 1. Security: `_DOMAIN_SYSTEM_PROMPT` and `_ENRICH_SYSTEM_PROMPT` are module-level literal constants

Both system prompts are bare `str` literals assigned at module scope. There are no f-strings, no variables, no concatenation paths that could let user text leak in. The security tests verify this via a `CapturingClient` subclass pattern.

### 2. `ctx.obj["_foundry_client"]` injection for CLI testability

CLI commands check `ctx.obj.get("_foundry_client")` before calling `_build_foundry_client()`. Tests pass the mock via `CliRunner.invoke(..., obj={"_foundry_client": mock_client})`. No `unittest.mock.patch` needed. Click's `ensure_object(dict)` leaves an existing dict obj intact, so extra keys survive the group command's setup.

### 3. Canonicalization flow: id_hint → entity_id via `ids.make_entity_id`

1. Validate `LLMOutput` with `output_schema.validate()`.
2. Drop entities/relationships below `CONFIDENCE_THRESHOLD` (0.50).
3. Build `hint_to_entity_id` map: `hint → make_entity_id(type, label)`.
4. Dedup by `normalize_canonical_key(type, label)` — merge aliases, keep higher confidence.
5. Resolve relationship source/target hints → stable entity IDs; drop if either is missing.
6. Produce `EntityRow`, `RelationshipRow`, `ChunkRow`, `EvidenceRow` records.

### 4. Checkpoint / resume

- Checkpoint at `{output_dir}/.checkpoint.json` with `{"completed": ["src:...", ...]}`.
- On `resume=True`, skip source_file_id immediately (return empty `CanonicalRecords`) without calling LLM.
- Each completed batch is marked after all passes succeed.

### 5. Domain brief injection in user message only

`build_user_message()` prepends the domain brief block delimited by `--- DOMAIN CONTEXT ---` / `--- END DOMAIN CONTEXT ---`. This appears only in the `user` argument to `FoundryClient.complete_json`. The system prompt never receives domain content.

---

## Test Coverage

| Module | Coverage |
|--------|----------|
| `enrichment/domain.py` | 100% |
| `enrichment/orchestrator.py` | 96% |
| `enrichment/output_schema.py` | 100% |
| `model/ids.py` | 100% |

---

## Open Items / Next Sprint

- Orchestrator currently runs only pass `p2` (entity extraction). Remaining passes (P1, P3–P8) need implementation as per SPEC-004 §3.
- `enrich_batch` batches at 50 rows; proper chunking strategy for large CSVs (SPEC-004 §5) not yet implemented.
- Embedding pass (P7) and visual description pass (P6) need `FoundryClient.embed()` integration.
- Parquet writer integration (Fenster's domain) is out of scope for this sprint.


# verbal-sprint1-foundry — Sprint 1 Foundry Client + Output Schema

**Date:** 2026-06-24T17:01:06-07:00  
**Author:** Verbal (AI Integration Dev)  
**Sprint:** 1 — LLM Integration Layer  
**Requested by:** Hyunsuk Shin  
**Status:** Implemented and verified (181 tests pass)

---

## What Was Built

### 1. `src/fabric_kg_builder/enrichment/foundry_client.py`

Thin wrapper around the Microsoft AI Foundry SDK with two public methods:

| Method | Purpose |
|--------|---------|
| `complete_json(system, user, json_schema) -> dict` | Structured JSON chat completion |
| `embed(texts) -> list[list[float]]` | Batch embeddings at configured dimensions |

**Key design decisions:**

- **`_sdk_client` injection** — constructor accepts `_sdk_client=` kwarg. Tests pass a `MagicMock`; production calls `_build_sdk_client()` which constructs `AIProjectClient`. No `unittest.mock.patch` needed anywhere.
- **`_build_sdk_client()` is the only SDK touch-point** — marked `TODO: verify against current azure-ai-projects / azure-ai-inference SDK`. All other logic is SDK-agnostic.
- **Auth:** `DefaultAzureCredential` by default; `AzureKeyCredential` if `AZURE_AI_FOUNDRY_API_KEY` env var is present. Keys come from `.env` only — never hardcoded.
- **Determinism:** `temperature=0.0`, `seed=42` forwarded on every chat call (SPEC-004 §6.7).
- **Security:** docstring explicitly prohibits placing user-supplied domain text in the `system` argument. Enforcement is by convention (documented) + test that checks role separation.

### 2. `src/fabric_kg_builder/enrichment/output_schema.py`

Pydantic v2 models for the full SPEC-004 §4 intermediate JSON contract:

| Model | Required fields |
|-------|----------------|
| `LLMOutput` | `source_file_id`, `pass` |
| `Entity` | `id_hint`, `type`, `label`, `confidence` |
| `Relationship` | `id_hint`, `source_id_hint`, `relation`, `target_id_hint`, `confidence` |
| `Chunk` | `id_hint`, `chunk_type`, `content` |
| `VisualAsset` | `id_hint`, `asset_type`, `blob_url`, `confidence` |
| `VisualRegion` | `id_hint`, `image_id_hint`, `region_type`, `confidence` |
| `Evidence` | `id_hint`, `source_type` |
| `PlaceholderSuggestion` | `concept`, `reason`, `confidence` |

- `validate(payload: dict) -> LLMOutput` — raises `pydantic.ValidationError` on any schema violation.
- `LLM_OUTPUT_JSON_SCHEMA` — module-level constant (JSON Schema dict) ready for injection into LLM prompts and `complete_json(json_schema=...)`.
- `"pass"` Python-keyword workaround: `Field(alias="pass")` + `ConfigDict(populate_by_name=True)`; accessible in Python as `.pass_`.

### 3. Tests

| File | New tests |
|------|-----------|
| `tests/unit/test_foundry_client.py` | 8 tests — construction, `complete_json`, `embed`, dimensions=1536 assertion |
| `tests/unit/test_output_schema.py` | 30 tests — valid payloads, missing required fields, bad confidence, optional fields |

---

## Foundry SDK Package Decision

**Package:** `azure-ai-projects` (already in `pyproject.toml`; confirmed by decision 10 in `.squad/decisions.md`)

**Mock call chain alignment:**

```python
# Chat (matches conftest make_foundry_client):
client.inference.get_chat_completions_client().complete(model=..., messages=..., ...)
    -> completion.choices[0].message.content  # JSON string

# Embeddings (new; same pattern):
client.inference.get_embeddings_client().embed(input=..., model=..., dimensions=...)
    -> response.data[i].embedding  # list[float]
```

Both paths have `TODO: verify against current Foundry SDK` comments. When the exact SDK version is confirmed, update only `_build_sdk_client()` — the rest of the class is stable.

---

## Open Items

- `_build_sdk_client()` marked `TODO` — exact `AIProjectClient` constructor signature and embedding method path need verification against the installed SDK version.
- `foundry_client.py` coverage is 67% (lines 94–114 = `_build_sdk_client` branch not exercised in unit tests — correctly excluded since unit tests use the mock). Integration tests will cover the live path.
- `sample_enrichment.json` fixture uses older field names (`evidence_id` instead of `id_hint` on evidence items). The fixture is not consumed by `output_schema.validate()` — it is the mock LLM response payload. No action needed now; flag for Fenster when canonicalize is implemented.


# Decision: verbal-sprint2-enrich-embed

**Date:** 2026-06-24T18:30:00-07:00  
**Author:** Verbal (AI Integration Dev)  
**Sprint:** 2  
**Status:** DONE — 456 tests pass  

---

## Context

Hyunsuk Shin requested that Surface PDFs flow through the `enrich` command
end-to-end. Two deliverables were scoped:

1. **PDF/document routing in `enrich`** — extend `--input` to accept PDF, DOCX,
   HTML, and MD files (or directories containing them) and route through
   `sources/router.py → extractors → chunker → orchestrator.enrich_documents`
   with evidence linking and canonical intermediate JSON output.

2. **`search-embeddings`** — add `generate_embeddings()` to
   `search/embeddings.py` that calls `FoundryClient.embed()` in batches,
   caches by `content_hash`, and attaches `chunk_vector` to AI Search docs
   produced by Fenster's `linkage.derive_chunk_doc()`.

---

## Decisions Made

### D1 — Dispatch by extension at the CLI seam

**Decided:** `enrich_cmd.py` contains a `_CSV_EXTENSIONS` / `_DOC_EXTENSIONS`
frozenset pair and a `_enrich_document_file()` helper. The orchestrator and
chunker are called identically regardless of where they are invoked from.

**Rationale:** Keeps `orchestrator.enrich_documents()` pure (no file I/O
beyond what it already does); all new file-type routing logic lives in one
function that is easy to test via `CliRunner`.

### D2 — Write `{safe_id}_canonical.json` per document file

**Decided:** One comprehensive JSON file per input document, containing all
six sections: `source_file`, `document_elements`, `chunks` (Chunker structural
chunks), `entities`, `relationships`, `evidence` (merged LLM + text-linked).
The per-pass LLM output files from `enrich_batch` are still written alongside.

**Rationale:** The spec requires the canonical intermediate to include
`document_elements` and `chunks`. The existing `enrich_batch` output omits
document elements. Rather than modifying the orchestrator's write path (which
would change existing behavior for CSV), we write an additional file at the CLI
level for documents.

### D3 — `generate_embeddings()` composes `linkage.derive_chunk_doc()`

**Decided:** `generate_embeddings()` in `search/embeddings.py` accepts chunk
row dicts, calls `linkage.derive_chunk_doc()` via a local import, then embeds
the `embedding_text` field via `client.embed()`. It does NOT replicate field
mapping from `linkage.py`.

**Rationale:** Fenster owns the field-mapping contract. A local import (not
module-level) avoids any future circular-import risk and keeps the dependency
explicit.

### D4 — Cache key = `content_hash`

**Decided:** The embedding cache maps `content_hash → list[float]`. Callers
pass a shared `cache` dict across multiple `generate_embeddings()` invocations
to persist cache across calls.

**Rationale:** `chunk_id` can change between pipeline runs if file content
changes (IDs are content-derived). `content_hash` is the stable dedup key and
matches how the Parquet pipeline deduplicates rows.

---

## Files Changed

| File | Change |
|------|--------|
| `src/fabric_kg_builder/cli/enrich_cmd.py` | Extended for document routing; `_enrich_document_file()` added |
| `src/fabric_kg_builder/search/embeddings.py` | Added `generate_embeddings()` with FoundryClient + hash cache |
| `tests/unit/test_enrich_cmd_pdf.py` | 6 new unit tests (PDF routing, canonical JSON, security assertion, CSV path still works, checkpoint/resume) |
| `tests/unit/test_search_embeddings.py` | 10 new unit tests (dims, caching, batching, empty input, linkage fields, custom vector field) |

---

## Test Results

```
456 passed in 40.49s
```

- 440 original tests: all pass (no regressions)
- 16 new tests: all pass (6 PDF enrich + 10 search embeddings)
- No live API calls: all tests use mock FoundryClient
- Security invariant confirmed: domain text in USER message only (SPEC-004 §2.3)

---

## Notes

- The `TestInspectSourceSurfaceDir` integration tests (Fenster, added concurrently)
  process 22 large Surface PDFs (up to 14MB) and are slow. They are not part of the
  "existing 440" baseline and should be gated by `@pytest.mark.integration` and
  a CI marker filter.
- Domain brief security test uses `UNIQUE_DOMAIN_TOKEN` sentinel captured via
  `complete.side_effect` → asserts the token never appears in `role="system"` messages.


# Decision: verbal-sprint2-visual

**Date:** 2026-06-24T17:49:23-07:00  
**Author:** Verbal (AI Integration Dev)  
**Requested by:** Hyunsuk Shin  
**Sprint:** 2 — Visual Pipeline Implementation  
**Status:** Implemented and verified (440 tests passing)

---

## Summary

Sprint 2 implements the visual asset pipeline, document enrichment bridge, and evidence linking layer as real, tested Python code. All external services (Blob Storage, Document Intelligence, Foundry LLM) are mock-injectable per SPEC-004 §2.3 and the conftest contract — no live calls in tests.

---

## Files Created

| File | Purpose |
|---|---|
| `src/fabric_kg_builder/enrichment/image_extractor.py` | Extract embedded images from PDFs via pdfplumber; produce `VisualAssetCandidate` records; assemble `VisualAssetRow` after Blob upload |
| `src/fabric_kg_builder/deploy/blob_uploader.py` | Upload image bytes to Azure Blob Storage; idempotent dedup by asset_id; `DefaultAzureCredential` auth |
| `src/fabric_kg_builder/enrichment/docintel.py` | Map Azure Document Intelligence Layout/Read response to `VisualRegionRow` records; pure mapping function + `DocIntelClient` wrapper |
| `tests/unit/test_image_extractor.py` | 17 tests for image extraction, hash dedup, and VisualAssetRow assembly |
| `tests/unit/test_blob_uploader.py` | 9 tests for BlobUploader upload, dedup, path prefix, and conftest mock |
| `tests/unit/test_docintel.py` | 18 tests for polygon helpers, DI mapping, FK checks, and DocIntelClient delegation |
| `tests/unit/test_visual_assets_regions.py` | 8 tests for visual_assets + visual_regions FK chain and assembly pipeline |
| `tests/unit/test_enrich_documents.py` | 9 tests for enrich_documents (section_path prefix, empty-content skip, checkpoint, resume) |
| `tests/unit/test_evidence_linking.py` | 25 tests for link_text_evidence and link_visual_evidence (all source_types, all FKs, stability) |

## Files Modified

| File | Change |
|---|---|
| `src/fabric_kg_builder/enrichment/orchestrator.py` | Added `DocumentElementRow` import; added `enrich_documents`, `link_text_evidence`, `link_visual_evidence` functions |

---

## Architecture Decisions

### 1. Document Intelligence split (SPEC-004 §8)

Document Intelligence handles OCR text + bounding polygons. Vision LLM handles semantic labels, entity linking, and non-structural region_type classification. This maps directly to SPEC-002 §3.9.1:

| Column | Source |
|---|---|
| `polygon_json`, `normalized_polygon_json`, `text` | Document Intelligence |
| `label`, `identified_entity_id` | Vision LLM (left None by docintel.py) |
| `blob_url` | Pipeline runner (injected after upload) |
| `confidence` | DI (for ocr_text rows), LLM (for semantic rows) |

### 2. Pure mapping function in docintel.py

`map_di_result_to_visual_regions(di_result, image_id)` is a pure function with no SDK import dependency. It accepts any object with `.pages`, `.paragraphs`, `.content` attributes — real SDK object or MagicMock. The `DocIntelClient` wrapper isolates the SDK call chain. Tests can test the mapping without constructing a client.

### 3. BlobUploader dedup strategy

Dedup is by `asset_id` (the stable `image_id` from `make_image_id`). If `get_blob_properties()` succeeds, the blob exists and we return its URL without re-uploading. This makes the upload step idempotent across pipeline re-runs. The `make_blob_uploader()` conftest mock matches `upload(asset_id, data, ext) -> str` exactly.

### 4. `enrich_documents` as a thin bridge

`enrich_documents` assembles `source_content` from `DocumentElementRow` objects and delegates to `enrich_batch`. No new checkpoint, retry, or JSON-writing logic is needed — `enrich_batch` handles all of that. The bridge adds `section_path` as a context prefix (`[paragraph|Battery Replacement]`) so the LLM has structural context.

### 5. Evidence linking: source_type computed from args

`link_text_evidence`: `source_type = "chunk"` if `chunk_id` is provided, else `"document_span"`.  
`link_visual_evidence`: `source_type = "figure_callout"` if `callout_id` is provided, else `"image_region"`.  
Callers never need to remember the string literals. Evidence IDs are deterministic (stable across re-runs) via `make_evidence_id`.

---

## Test Results

```
440 passed in 82.30s (0:01:22)
```

- **Baseline:** 354 passing (Sprint 1)
- **New:** +86 tests (Sprint 2)
- **Total:** 440 passing
- **Live API calls in tests:** 0

---

## Security Invariants Maintained

- `_ENRICH_SYSTEM_PROMPT` is a fixed literal — domain text never enters it (Sprint 1 invariant preserved).
- `enrich_documents` delegates to `build_user_message` which places domain text in USER message only.
- No secrets in code: `BlobUploader` reads `AZURE_STORAGE_KEY` from env; `DocIntelClient` reads `AZURE_DOCINTEL_API_KEY` from env. Both default to `DefaultAzureCredential`.
- Blob URLs are runner-injected (never LLM-generated) per SPEC-004 §4.5/§4.6 rule.



### Multi-type Fabric ontology (deploy-ontology --multitype)

**Decision:** The Fabric Ontology Explorer is a *type/schema* view — it draws one box per entity **type**, not per instance row. The original deploy modelled all entities as a single generic `KGEntity` type, so the graph showed one box even though `dbo.entities` held 11k+ instances across 92 real types. This looked empty to users.

**What we built:** `deploy-ontology --multitype --parquet-dir <dir>` now models a rich graph:
- One Fabric `EntityType` per real domain type, bound to its own per-type Lakehouse table (`entities_<type>`).
- One typed `RelationshipType` per `(source_type → target_type)` pair, bound to a per-pair edge table (`rel_<src>_<tgt>`).
- Relationship verbs are **collapsed by endpoint pair** (hundreds of near-synonym verbs like `HAS_STEP`/`has_step`/`includes_step` → one `has_step` edge named after the dominant verb), with a `--min-pair-count` threshold and a cap to keep the graph legible.

**Why per-type tables:** Fabric data bindings have **no row filter** — a binding points at a whole table. So splitting one `dbo.entities` into many typed boxes requires materializing per-type tables in the Lakehouse first (verified: Microsoft's digital-twin/ontology model maps each entity type from its own source). The `onelake_multitype.materialize_multitype_tables()` helper writes these slices as Delta before `updateDefinition`.

**Verified live (dev):** 12 entity types (Device, DeviceModel, Component, Part, PartNumber, Procedure, Step, Tool, Symptom, Cause, Resolution, Section) + 40 typed relationships = 106 ontology parts. updateDefinition returned 202 LRO → polled to **Succeeded**; getDefinition read-back confirmed 12 types live. 52 per-type/per-pair Delta tables materialized to OneLake `dbo`.

**Files:** `ontology/multitype_plan.py` (planner), `ontology/fabric_def.py` (`build_multitype_ontology_parts`), `deploy/onelake_multitype.py` (table materialization), `cli/deploy_cmd.py` (`--multitype`/`--parquet-dir`/`--min-pair-count`). 7 unit tests; 925 fast tests pass. The single-type path is unchanged (default), so existing behaviour is preserved.

---

### Densify Hub Linker + Cause-Symptom-Resolution Chain (Session 2026-06-25)

**Date:** 2026-06-25T20:12:00Z  
**By:** Scribe (Session Coordinator)  
**Status:** Implemented & live-deployed  
**Commits:** feature (densify hub), 210596f (SCR linker), data-agent-grounding doc

#### What Was Shipped

**1. `fabric-kg densify` — Document Hub Linking**

**Module:** `src/fabric_kg_builder/enrichment/densify.py` (new), `src/fabric_kg_builder/cli/densify_cmd.py` (new)

**Purpose:** Link each document's source domain (e.g., Surface Pro 10 troubleshooting guide) to the ontology's DeviceModel hub, auto-populating component/part/procedure/symptom edges.

**Design:**
- Read `build/parquet/source_files.parquet`, `document_elements.parquet`, `entities.parquet`
- Extract device model name from `source_file.source_name` via regex ("Surface Pro 10" → `DeviceModel:surface-pro-10`)
- Query Lakehouse GQL: find the DeviceModel entity + all its canonical `has_component` / `has_part` / `has_procedure` / `exhibits_symptom` edges
- Link each document element to matching entities via `shown_in` / `indexed_in` edges (deterministic, confidence=0.95)
- Write new `relationships.parquet` batch with +27,436 hub edges

**Result:** `relationships` rows 3,715 → 29,251; Surface Laptop 5 components 0 → 400.

**2. Cause-Symptom-Resolution (S/C/R) Transitive Linker**

**Added to:** `densify.py` — `link_symptom_cause_resolution()`

**Design:**
- Deterministic keyword overlap (token-level match, TF × IDF scoring ≥ 0.45 confidence threshold)
- Transitive: Cause → resolves → Resolution (edge `addressed_by`); Symptom → caused_by → Cause
- Document-scoped: only links within same source file + procedure (no cross-document leakage)
- Validation: both endpoints must exist in `entities.parquet` with `entity_type ∈ {Cause, Symptom, Resolution}`

**Result:** +3,555 S/C/R edges; isolated symptoms 327 → 8.

**3. Data-Agent Grounding Documentation**

**File:** `docs/data-agent-grounding.md` (new)

**Contents:**
- Agent instructions (how to reason about the KG, name/type mismatch patterns)
- Per-type field descriptions (Surface Pro = DeviceModel w/ variants, not exact device name match)
- Example queries: "Find Surface Pro troubleshooting procedures" + GQL template
- Debugging: "No data found" pattern diagnosis (sparse graph vs name mismatch)

**Why:** Coordinator discovered agents were failing with "name/type mismatch" (Surface Pro 10 device vs DeviceModel hub). Doc explains this foundational concept for all future agent work.

#### Technical Decisions

| Decision | Rationale |
|----------|-----------|
| Keyword threshold confidence = 0.45 (not 0.70) | S/C/R matching is looser than entity extraction; 45% captures valid relationships without hard edges like 70% would impose |
| Document-scoped S/C/R only | Cross-document matching would produce false positives (same symptom description in unrelated manuals) |
| Deterministic (TF×IDF) not LLM | Fully offline, reproducible, no model variance, completes in seconds on 32K edges |
| `addressed_by` as transitive edge | Allows graph queries: "procedures that address this symptom" via multi-hop traversal |
| Hub linking via `shown_in` edges | Matches SPEC-003 bridge semantics; agent can use consistent edge types across all knowledge extraction |

#### Live Deployment Verification

- Lakehouse (dev) redeployed 2026-06-25: 12 entity types, ~40 edge types (incl. causes/resolved_by/addressed_by), 32,118 total relationships, 935 unit tests pass
- GQL: "Find all procedures for Surface Laptop 5" returns procedure + step + tool/part chains; agent can now ground queries correctly
- "List symptoms caused by battery degradation" chains via caused_by → Cause → Symptom; no orphan nodes

#### Confidence & Gate

- **Confidence:** 0.45 (keyword overlap S/C/R); 0.95 (hub document linking)
- **Gate:** VAL-001..028 + BRG-001..010 validation suite green; no structural violations
- **Open:** Future session can refine threshold based on production query patterns

---

### Domain Template + Questions-File Intake (2026-06-25T21:20:00Z) — Coordinator & Keyser

#### Coordinator: Domain-Template Playbook Feature — Implemented

**Date:** 2026-06-25T21:20:00Z  
**By:** Keyser & Hyunsuk Shin (via Copilot)  
**Status:** ✅ Shipped (942 tests pass)

**Decision:** `set-domain` now accepts `--industry` and `--business-domain` flags (required) and stores domain input in a structured directory hierarchy under `DomainBrief/` for reusability. Supports optional `--questions-file` for injecting domain competency questions.

**What was implemented:**

1. **CLI flags:**
   - `--industry TEXT` (required) — e.g., "automotive" 
   - `--business-domain TEXT` (required) — e.g., "field-service-operations"
   - `--prompt TEXT` (optional) — inline domain brief text
   - `--questions-file PATH` (optional) — file path to competency questions

2. **Directory structure:**
   ```
   DomainBrief/
     {industry}/
       {business_domain}/
         domain.json      (persisted brief text)
         questions.json   (optional competency questions)
   ```

3. **Backward compatibility:** Old flat `domain.json` still supported; auto-upgraded to new structure on first `set-domain --industry ... --business-domain ...` run.

**Why:** Enables templates for common (industry, business_domain) pairs; teams can share domain briefs across projects; questions-file drives downstream data-agent-instructions generation.

**Tests:** 942 passing (18 new domain-template tests added).

---

#### Coordinator: Auto-Generated Data-Agent Instructions

**Date:** 2026-06-25T21:20:00Z  
**By:** Verbal & Coordinator  
**Status:** ✅ Shipped

**Decision:** `deploy-ontology --multitype --create-data-agent-instruction` (default on) auto-writes `data-agent-instructions.md` from the LIVE Lakehouse graph + sample competency questions.

**What was implemented:**

1. **New module:** `src/fabric_kg_builder/deploy/agent_instructions.py`
   - `generate_agent_instructions()` — queries live Lakehouse (GQL), extracts entity type counts, relationships, sample competency questions from questions.json
   - Produces markdown: entity type reference, relationship patterns, example queries, debugging tips

2. **CLI wiring:** `deploy-ontology --create-data-agent-instruction` (default True, `--skip-agent-instructions` to disable)
   - Reads deployed ontology from Lakehouse
   - Embeds sample questions from `DomainBrief/{industry}/{business_domain}/questions.json`
   - Writes `data-agent-instructions.md` to build directory

3. **Output format (markdown):**
   ```
   # Data Agent Instructions for {industry} / {business_domain}
   
   ## Entity Types (from Lakehouse)
   - Device (92 instances)
   - Component (412 instances)
   - ...
   
   ## Sample Competency Questions
   - How many Surface Pro devices are in the database?
   - Which procedures fix the blue screen error?
   
   ## Example GQL Patterns
   [patterns auto-derived from live relationships]
   
   ## Debugging
   [common agent error patterns with solutions]
   ```

**Why:** Data agents (future Foundry Agent Service + MCP tools) need grounding in the actual deployed schema. Auto-generation keeps instructions in sync with live data and avoids manual doc drift.

**Tests:** 942 tests pass (integrated into deploy flow).

---

### GitHub Pages Landing Site (2026-06-25T21:20:00Z) — Coordinator

**Date:** 2026-06-25T21:20:00Z  
**By:** Coordinator  
**Status:** ✅ Deployed to production  
**URL:** https://hyssh.github.io/fabric-kg-builder/  
**Commits:** site/pages (2026-06-25)

**Decision:** Built a static GitHub Pages site under `/site` to document the project, pipeline, installation, and best practices. Deployed via GitHub Actions on every push to main.

**What was built:**

1. **Files in `/site`:**
   - `index.html` — landing page (responsive, dark theme)
   - `styles.css` — styling (CSS Grid, flexbox)
   - `app.js` — interactive pipeline diagram + navigation
   - `.nojekyll` — disable Jekyll processing
   - `README.md` — site source notes

2. **Page sections:**
   - **Background/Problem:** Why KG + why fabric-kg-builder
   - **What It Does:** End-to-end pipeline (domain → deploy → query)
   - **Pipeline:** Visual 12-stage flow with descriptions
   - **Install:** Prerequisites, pip install, quick start
   - **GitHub Copilot CLI Usage:** How to use this repo with GitHub Copilot
   - **Best Practices:** Templates, industry/business-domain patterns, questions-file, densify, iterate without re-enriching
   - **Proven Surface Results:** Real data on Surface troubleshooting graphs (device models, procedures, components, symptoms)
   - **Architecture:** Fabric Lakehouse + AI Search + Ontology + Agent grounding
   - **Security:** Secrets model, auth, prompt injection prevention
   - **FAQ:** Common issues + solutions

3. **GitHub Actions workflow:**
   - File: `.github/workflows/pages.yml`
   - Trigger: `push` to `main` branch
   - Deploy: `/site` directory to GitHub Pages
   - Automatic rollout on every commit

**Why:** 
- Attracts new users (landing page instead of just README)
- Documents the full end-to-end story
- Best practices are centralized + discoverable
- Proof-of-concept results are visible
- Live site keeps docs in sync with codebase (Git-driven)

**Tests:** Manual verification — site loads, links work, no 404s.

**Live URL:** https://hyssh.github.io/fabric-kg-builder/

---

### RCA Diagnostic-Path Linker (2026-06-25T23:05:00Z) — Coordinator

**Date:** 2026-06-25T23:05:00Z  
**By:** Coordinator  
**Status:** ✅ Deployed to dev  
**Commits:** 4c6c282 (RCA linker feat), d1af045 (RCA docs/grounding)

**Decision:** Implement `link_rca_paths()` in densify to build Root Cause Analysis chains from Symptom entities to both diagnostic procedures and remediation procedures. Addresses reviewer feedback: "Repair KG not RCA KG."

**What was built:**

1. **New relationship edges:**
   - `Symptom` →`diagnosed_by`→ `diagnostic Procedure` (SDT/check/inspect/validate/verify/status — real diagnostic entities from corpus)
   - `Symptom` →`remediated_by`→ `repair Procedure` (which has `has_step` edges to concrete repair steps)

2. **Linking algorithm:**
   - Keyword-gated per document (match symptom text against procedure titles/descriptions)
   - Confidence score = 0.4 (additive, non-destructive; documents are already data-grounded)
   - New helper: `is_diagnostic_procedure()` — identifies SDT classification from ontology

3. **Full RCA chain now traversable:**
   - Symptom (e.g., "Battery expansion") → 28 causes (Cause→Symptom edges) 
   - Symptom → 1 diagnostic test (diagnosed_by)
   - Symptom → 19 remediation procedures (remediated_by)
   - Procedures → 35 reachable steps (has_step edges)
   - Steps → 28 resolutions (resolve_symptom)
   - **Total path:** Cause → Symptom → DiagnosticTest + Procedure → Steps + Resolution

4. **Deployment:**
   - Lakehouse: dbo.rel_symptom_procedure = 972 live in OneLake
   - densify (+1,429 RCA edges), compile (additivity guard OK, rels 35,445), deploy (LRO Succeeded)
   - Remediated_by now top relationship type by volume

5. **Documentation:**
   - agent_instructions generator emits one-query RCA template
   - README densify section expanded: 4 passes with RCA chain + Battery expansion example
   - lessons-learned marks RCA "partially addressed" (data-limited for Observation/FailureMode in repair manuals)

**Data boundaries:**
- Observation/FailureMode: data-limited (repair manuals not yet ingested)
- All Symptom, Cause, Procedure, Step, and Resolution entities are data-grounded (extracted from documents, not synthesized)

**Verification:**
- Full RCA chain end-to-end traversable ✓
- 951 unit tests pass ✓
- No regression in compile or deploy ✓

**Why:** Users can now follow a complete diagnostic + repair flow from symptom detection through root cause to specific remediation steps. The chain is discoverable and queryable.



