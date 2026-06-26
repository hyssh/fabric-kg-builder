# SPEC-002: Canonical Data Model

**Status:** Draft  
**Date:** 2026-06-24T11:46:10.517-07:00  
**Author:** Fenster (Data Engineer)  
**PRD references:** §7 (Source Types), §12 (Canonical Parquet Tables), §17 (Deterministic ID Strategy), §21 (Validation Requirements)

### Revision History

| Version | Date | Author | Summary |
|---|---|---|---|
| 1.0 | 2026-06-24 | Fenster | Initial draft — 8-table schema, ID strategy, CSV ingestion, validation rules |
| 1.1 | 2026-06-24T11:46:10.517-07:00 | Fenster | Added §11 Graph-to-Search Clue Chaining; added §2.1 Lakehouse vs AI Search boundary; updated §3.9 visual_regions with Document Intelligence provenance; added `entities.search_aliases` and `chunks.entity_search_keys` columns; updated Appendix A schemas accordingly |
| 1.2 | 2026-06-24T12:42:17.255-07:00 | Fenster | §11 enriched with RESEARCH-001 production findings: added §11.3 canonical→AI Search field mapping table; §11.4 filter-on-IDs/search-on-aliases split and `search.in()` requirement; §11.5 `preFilter` entity_ids coverage requirement; §11.6 KG↔index sync push pipeline (Parquet not readable by OneLake indexer); §11.7 embedding coupling (1536-dim `text-embedding-3-large`); renumbered prior §11.3–§11.6 to §11.8–§11.11; §11.9 worked example enriched with `search.in()` query pattern; updated §11.10 required conditions and §11.11 design constraints |
| 1.3 | 2026-06-24T12:42:17.255-07:00 | Fenster | §3.4 `entity_search_keys` and §3.5 `search_aliases` notes corrected: `entity_search_keys` feeds AI Search `entity_aliases` SEARCHABLE field (keyword/alias matching only); filtering is done on `entity_ids` / `canonical_key` (stable IDs). Stale "filter on entity_search_keys" and "filterable/searchable" wording removed to match §11.4 filter-on-IDs/search-on-aliases rule. |
| 1.4 | 2026-06-24T21:46:59.576-07:00 | McManus | §3.3 document_elements + §3.4 chunks: Document Intelligence Layout is the authoritative source for table structure and `content_html`. Added provenance notes: `element_type="table"` and `chunk_type="table_html"` are produced by DI Layout (`docintel_tables.py`), not the LLM. LLM role = semantics only (summary, entity linking over HTML). `table_row` / `table_cell` element and chunk types are schema-level only — no longer produced by the enrichment pipeline. |

---

## 1. Scope and Purpose

This specification defines the canonical data model for fabric-kg-builder: the eight Parquet tables that form the durable data contract between extraction/enrichment and all downstream consumers (Fabric Lakehouse, Fabric Ontology, Azure AI Search).

The model covers:

- Full schema definitions (column names, pyarrow types, nullability, descriptions, key notes)
- Primary keys and foreign-key relationships between tables
- Referential integrity matrix
- Deterministic ID strategy for data rows
- CSV/TSV/XLSX ingestion mapping conventions and schema-profile output
- Parquet writer layout and encoding rules
- Placeholder Parquet generation (empty-but-typed)
- Data-level validation checklist with severity ratings
- Sample rows for key tables

### 1.1 PRD Alignment

| PRD Section | What this spec covers |
|---|---|
| §7 Source Types | CSV/TSV/XLSX ingestion path into source_files + raw records |
| §12 Canonical Parquet Tables | All 8 tables with precise type-level schemas |
| §17 Deterministic ID Strategy | Data row ID hashing, canonical_key normalization, content_hash dedup |
| §21 Validation Requirements | Data integrity checks, severity ratings, FK validation |

---

## 2. Canonical Data Contract Principle

```
LLM output is intermediate.
Canonical Parquet is the data contract.
```

The pipeline has three data layers:

| Layer | Format | Role |
|---|---|---|
| Raw source | CSV / TSV / XLSX / PDF / DOCX / HTML / images | Input material |
| LLM intermediate | JSON (in-memory or enriched/*.json) | Extraction scaffolding — unstable, not source of truth |
| Canonical data | Parquet (build/parquet/*.parquet) | Durable contract — all downstream work starts here |

Downstream consumers — Fabric Lakehouse, Fabric Ontology compiler, Azure AI Search indexer — read only from canonical Parquet. LLM JSON is never committed to Fabric or read by consumers directly.

### 2.1 Lakehouse vs AI Search: Storage Boundary

**Structured canonical tables live exclusively in the Fabric Lakehouse (OneLake). They are NOT pushed to Azure AI Search.**

| Table | Storage | AI Search eligible? | Notes |
|---|---|---|---|
| `source_files` | Lakehouse only | No | Metadata/provenance — no retrieval value |
| `document_elements` | Lakehouse only | Text/HTML content only (see below) | Raw structural units; `content` and `content_html` may feed chunk indexing |
| `chunks` | Lakehouse + AI Search index | Yes — text/visual chunks | The primary AI Search index documents. Chunks carry entity linkage fields for graph-guided retrieval |
| `entities` | Lakehouse only | No | Graph nodes live in Lakehouse and Fabric Ontology — NOT in AI Search. Entity metadata (canonical_keys, aliases) is denormalized onto chunks instead |
| `relationships` | Lakehouse only | No | Graph edges stay in Lakehouse and Fabric Ontology |
| `evidence` | Lakehouse only | No | Provenance/lineage store — not a retrieval document |
| `visual_assets` | Lakehouse + AI Search index | Yes — image descriptions, captions | `description`, `caption`, `blob_url` eligible; raw pixel data is in Blob Storage |
| `visual_regions` | Lakehouse only | No | Sub-region bounding boxes and OCR blocks; not indexed directly — relevant text flows into parent chunk |

**What feeds AI Search:** Only content suitable for vector search and hybrid retrieval — specifically `chunks.content` / `chunks.embedding_text`, `visual_assets.description`, `document_elements.content_html` (for table HTML chunks), and the entity linkage fields denormalized onto those records (`related_entity_ids`, `entity_search_keys`).

**What stays Lakehouse-only:** CSV-derived rows, all entity/relationship graph data, all evidence/provenance metadata, visual region bounding boxes. These are queryable as Parquet/Delta tables in Spark or via Fabric Ontology GQL — never via AI Search.

> **Infra note:** The AI Search compile stage (`fabric-kg compile-search`) reads from Lakehouse Parquet and builds index documents. It is an optional pipeline stage. Disabling it does not affect Lakehouse data integrity.

---

## 3. Schema Definitions

### 3.1 Conventions

| Convention | Rule |
|---|---|
| `entity_id`, `relationship_id`, etc. | UTF-8 string, max 512 bytes, content-hash-derived (see §5) |
| `timestamp[us, UTC]` | Microsecond-precision UTC timestamp; pyarrow type `pa.timestamp("us", tz="UTC")` |
| `list<string>` | pyarrow type `pa.list_(pa.string())`; serialized as JSON array in CSV contexts |
| `string` | pyarrow `pa.string()` (UTF-8 variable length) |
| `float64` | pyarrow `pa.float64()` |
| `int32` | pyarrow `pa.int32()` |
| `bool` | pyarrow `pa.bool_()` |
| NOT NULL | Column must be populated; writer raises on null |
| NULLABLE | Column may be null/None |

---

### 3.2 `source_files` Table

**Purpose:** Tracks every ingested source file with hash and metadata. Root of all provenance chains.

| Column | pyarrow Type | Null? | Description | Key Notes |
|---|---|---|---|---|
| `source_file_id` | `pa.string()` | NOT NULL | Stable source file ID | **Primary key.** SHA-256 of canonical path + content_hash. See §5.1 |
| `path` | `pa.string()` | NOT NULL | Original source file path as supplied to the CLI | Relative to project root when possible |
| `filename` | `pa.string()` | NOT NULL | Basename of the source file | Derived from `path` |
| `source_type` | `pa.string()` | NOT NULL | Source format: `csv`, `tsv`, `xlsx`, `pdf`, `docx`, `html`, `markdown`, `image`, `parquet` | From §7 |
| `content_hash` | `pa.string()` | NOT NULL | SHA-256 hex digest of file bytes at ingestion time | Used for change detection and dedup |
| `byte_size` | `pa.int64()` | NULLABLE | File size in bytes | |
| `ingested_at` | `pa.timestamp("us", tz="UTC")` | NOT NULL | UTC timestamp when the file was ingested | |
| `schema_profile_path` | `pa.string()` | NULLABLE | Path to schema-profile.json produced by inspect-source | For CSV/XLSX sources (see §6.2) |
| `row_count` | `pa.int64()` | NULLABLE | Number of data rows for tabular sources | CSV/XLSX only; null for documents |
| `notes` | `pa.string()` | NULLABLE | Free-text notes or warnings from the ingestion pass | |

**Primary key:** `source_file_id`

---

### 3.3 `document_elements` Table

**Purpose:** Stores every structural unit extracted from a source document — sections, tables, figures, images, steps, pages, callouts. First-class evidence for chunks and entities.

| Column | pyarrow Type | Null? | Description | Key Notes |
|---|---|---|---|---|
| `document_element_id` | `pa.string()` | NOT NULL | Stable element ID | **Primary key.** See §5.3 |
| `source_file_id` | `pa.string()` | NOT NULL | Source file that produced this element | **FK → source_files.source_file_id** |
| `element_type` | `pa.string()` | NOT NULL | `section`, `page`, `paragraph`, `table`, `table_row`, `table_cell`, `figure`, `image`, `caption`, `callout`, `procedure`, `step`, `warning`, `note`, `toc_entry`, `header`, `footer` | |
| `parent_element_id` | `pa.string()` | NULLABLE | Parent element in document hierarchy | Self-referential **FK → document_elements.document_element_id** |
| `title` | `pa.string()` | NULLABLE | Section heading, figure title, or table caption | |
| `content` | `pa.string()` | NULLABLE | Plain-text content of this element | |
| `content_html` | `pa.string()` | NULLABLE | HTML representation; required for `table` type | **Table provenance:** set by DI Layout (`docintel_tables.table_to_html()`). LLM must not set this field — it is populated before the LLM pass. |
| `blob_url` | `pa.string()` | NULLABLE | Azure Blob Storage URL; required for `figure`, `image`, `caption`, `table` types after upload | For tables: URL of the uploaded `table_{n}.html` artifact |
| `page_number` | `pa.int32()` | NULLABLE | Source page number (1-indexed) | |
| `section_path` | `pa.string()` | NULLABLE | Forward-slash-joined heading path, e.g. `Introduction/Overview` | |
| `sort_order` | `pa.int32()` | NULLABLE | Reading order index within the parent element | |
| `row_index` | `pa.int32()` | NULLABLE | Row index for `table_row` and `table_cell` types | |
| `col_index` | `pa.int32()` | NULLABLE | Column index for `table_cell` type | |
| `content_hash` | `pa.string()` | NOT NULL | SHA-256 of content + content_html | Used for dedup and change detection |
| `extracted_at` | `pa.timestamp("us", tz="UTC")` | NOT NULL | UTC timestamp of extraction | |

**Primary key:** `document_element_id`  
**Foreign keys:** `source_file_id` → `source_files`, `parent_element_id` → `document_elements` (self)

> **Table provenance:** `element_type="table"` rows are produced exclusively by **Azure AI Document Intelligence Layout** (`docintel_tables.extract_tables()`), never by the LLM. `content_html` = DI-rendered `<table>` HTML; `content` = tab-delimited plain text; `blob_url` = URL of the uploaded `table_{n}.html` artifact (set after Blob upload). LLM (P2/P5/P7) provides semantic enrichment over the HTML only and must not write `element_type="table"` rows. `table_row` and `table_cell` element types are schema-level only — they are no longer produced by the enrichment pipeline.

---

### 3.4 `chunks` Table

**Purpose:** Stores traditional document chunks for vector search, hybrid retrieval, source grounding, and LLM context. Every chunk links to a document element and source file.

| Column | pyarrow Type | Null? | Description | Key Notes |
|---|---|---|---|---|
| `chunk_id` | `pa.string()` | NOT NULL | Stable chunk ID | **Primary key.** See §5.4 |
| `source_file_id` | `pa.string()` | NOT NULL | Source file | **FK → source_files.source_file_id** |
| `document_element_id` | `pa.string()` | NULLABLE | Parent document element | **FK → document_elements.document_element_id** |
| `chunk_type` | `pa.string()` | NOT NULL | `section_text`, `procedure_step`, `table_html`, `table_row`, `figure_caption`, `image_description`, `ocr_text`, `warning`, `note`, `raw_page_text` | From §10.1. **`table_html` is produced by DI Layout (`docintel_tables.py`), not the LLM.** `table_row` remains in the type list for schema compatibility but is not produced by the enrichment pipeline. |
| `content` | `pa.string()` | NOT NULL | Text used for embedding and retrieval | For `table_html`: tab-delimited plain text of cells (DI-derived) |
| `content_html` | `pa.string()` | NULLABLE | HTML content for table chunks | For `table_html`: DI-rendered `<table>` HTML set before LLM pass; LLM must not overwrite |
| `embedding_text` | `pa.string()` | NULLABLE | Cleaned text prepared for embedding (may differ from `content`) | For `table_html`: `"{P7 summary}\n\n{pipe-delimited cells}"` populated after P7 |
| `blob_url` | `pa.string()` | NULLABLE | Blob URL for image/figure/table HTML artifact | Required for `image_description`, `figure_caption`; set for `table_html` after `table_{n}.html` upload |
| `page_number` | `pa.int32()` | NULLABLE | Source page number | |
| `section_path` | `pa.string()` | NULLABLE | Heading path for retrieval context | |
| `table_id` | `pa.string()` | NULLABLE | Document element ID of parent table | For `table_html` and `table_row` chunk types |
| `figure_id` | `pa.string()` | NULLABLE | Document element ID of parent figure | |
| `image_id` | `pa.string()` | NULLABLE | Visual asset ID if chunk describes an image | **FK → visual_assets.image_id** |
| `related_entity_ids` | `pa.list_(pa.string())` | NULLABLE | Entity IDs linked to this chunk | Populated by enrichment; written as JSON array in CSV contexts |
| `entity_search_keys` | `pa.list_(pa.string())` | NULLABLE | Denormalized entity canonical_keys, display_names, and aliases for all entities in `related_entity_ids` | **AI Search linkage field.** Populated at compile-data time by joining `related_entity_ids` → `entities.search_aliases`. Feeds the AI Search `entity_aliases` SEARCHABLE field for keyword/alias matching (BM25) — not used for filtering. Entity identity filtering is done via `entity_ids` / `canonical_key` (stable IDs). The graph-to-search path: GQL returns canonical_keys → `search.in(entity_ids, ...)` filter; aliases go into the `search` text param → BM25 on `entity_aliases`. See §11. |
| `content_hash` | `pa.string()` | NOT NULL | SHA-256 of `content` | Change detection and dedup |
| `created_at` | `pa.timestamp("us", tz="UTC")` | NOT NULL | UTC timestamp | |

**Primary key:** `chunk_id`  
**Foreign keys:** `source_file_id` → `source_files`, `document_element_id` → `document_elements`, `image_id` → `visual_assets`

> **Table provenance:** `chunk_type="table_html"` rows are produced by **Azure AI Document Intelligence Layout** (`docintel_tables.extract_tables()`) — DI = table structure + `content_html`; LLM = table semantics only (`embedding_text` after P7, `related_entity_ids` after P2/P5). The LLM must not write `chunk_type="table_html"` or `"table_row"` rows; the `canonicalize` step drops any LLM-emitted table_row chunks.

---

### 3.5 `entities` Table

**Purpose:** Stores all canonical knowledge graph entities. Every row is a stable, deduplicated, enriched graph node.

| Column | pyarrow Type | Null? | Description | Key Notes |
|---|---|---|---|---|
| `entity_id` | `pa.string()` | NOT NULL | Stable canonical entity ID | **Primary key.** SHA-256-derived. See §5.2 |
| `entity_type` | `pa.string()` | NOT NULL | Ontology entity type label (e.g. `Device`, `Component`, `Part`, `PartNumber`, `Procedure`, `Step`) | Must match a type in `ontology/model.yaml` |
| `display_name` | `pa.string()` | NOT NULL | Human-readable label for the entity | |
| `canonical_key` | `pa.string()` | NOT NULL | Normalized identity key used for ID derivation and dedup | Lowercase, stripped, `type:normalized_name` format. See §5.2 |
| `aliases` | `pa.list_(pa.string())` | NULLABLE | Alternate names and synonyms | Collected across sources during enrichment |
| `search_aliases` | `pa.list_(pa.string())` | NULLABLE | Flattened, normalized search keys for this entity | **AI Search linkage field.** Populated at compile-data time as `[canonical_key] + [display_name.lower()] + [a.lower() for a in (aliases or [])]`. Written to AI Search index documents for keyword/alias matching only (feeds the `entity_aliases` SEARCHABLE field — not filterable). Filtering on entity identity is done on `entity_ids` / `canonical_key`. See §11 for usage in clue-chaining. Do not confuse with `aliases` (human-readable synonyms). |
| `description` | `pa.string()` | NULLABLE | LLM-generated or source description | |
| `properties_json` | `pa.string()` | NULLABLE | JSON object of additional typed properties | For properties not modeled as dedicated columns |
| `source_file_id` | `pa.string()` | NULLABLE | Source file where entity was first observed | **FK → source_files.source_file_id** |
| `confidence` | `pa.float64()` | NULLABLE | Extraction confidence score [0.0, 1.0] | |
| `is_placeholder` | `pa.bool_()` | NOT NULL | True if row was generated as a model-driven placeholder | Default false |
| `content_hash` | `pa.string()` | NOT NULL | SHA-256 of `canonical_key + entity_type + display_name` | Used to detect enrichment changes across runs |
| `created_at` | `pa.timestamp("us", tz="UTC")` | NOT NULL | UTC timestamp | |
| `updated_at` | `pa.timestamp("us", tz="UTC")` | NOT NULL | UTC timestamp of last update | |

**Primary key:** `entity_id`  
**Unique constraint:** `canonical_key` (enforced at write time; duplicates are merged, not duplicated)

---

### 3.6 `relationships` Table

**Purpose:** Stores all canonical directed edges between entities in the knowledge graph.

| Column | pyarrow Type | Null? | Description | Key Notes |
|---|---|---|---|---|
| `relationship_id` | `pa.string()` | NOT NULL | Stable relationship ID | **Primary key.** See §5.5 |
| `relationship_type` | `pa.string()` | NOT NULL | Ontology relationship type (e.g. `has_component`, `evidenced_by`, `visually_depicts`) | Must match a type in `ontology/model.yaml` |
| `source_entity_id` | `pa.string()` | NOT NULL | ID of the source entity (the "from" node) | **FK → entities.entity_id** |
| `target_entity_id` | `pa.string()` | NOT NULL | ID of the target entity (the "to" node) | **FK → entities.entity_id** |
| `evidence_id` | `pa.string()` | NULLABLE | Primary evidence record for this relationship | **FK → evidence.evidence_id** |
| `properties_json` | `pa.string()` | NULLABLE | JSON object of edge-level typed properties | |
| `confidence` | `pa.float64()` | NULLABLE | Extraction confidence score [0.0, 1.0] | |
| `is_placeholder` | `pa.bool_()` | NOT NULL | True if generated as placeholder | |
| `content_hash` | `pa.string()` | NOT NULL | SHA-256 of `relationship_type + source_entity_id + target_entity_id` | Dedup key |
| `created_at` | `pa.timestamp("us", tz="UTC")` | NOT NULL | UTC timestamp | |

**Primary key:** `relationship_id`  
**Foreign keys:** `source_entity_id` → `entities`, `target_entity_id` → `entities`, `evidence_id` → `evidence`

---

### 3.7 `evidence` Table

**Purpose:** Stores structured provenance for every extracted fact. Every entity and relationship should be traceable to at least one evidence record.

| Column | pyarrow Type | Null? | Description | Key Notes |
|---|---|---|---|---|
| `evidence_id` | `pa.string()` | NOT NULL | Stable evidence ID | **Primary key.** See §5.6 |
| `source_file_id` | `pa.string()` | NOT NULL | Source file | **FK → source_files.source_file_id** |
| `source_type` | `pa.string()` | NOT NULL | `csv_row`, `document_span`, `table_cell`, `figure_callout`, `image_region`, `ocr_text`, `chunk` | |
| `document_element_id` | `pa.string()` | NULLABLE | Related document element | **FK → document_elements.document_element_id** |
| `chunk_id` | `pa.string()` | NULLABLE | Related chunk | **FK → chunks.chunk_id** |
| `page_number` | `pa.int32()` | NULLABLE | Source page number | |
| `section_path` | `pa.string()` | NULLABLE | Heading path for provenance context | |
| `table_id` | `pa.string()` | NULLABLE | Document element ID of the parent table | |
| `row_index` | `pa.int32()` | NULLABLE | Row index within the table | |
| `col_index` | `pa.int32()` | NULLABLE | Column index within the table | |
| `figure_id` | `pa.string()` | NULLABLE | Document element ID of the parent figure | |
| `image_id` | `pa.string()` | NULLABLE | Visual asset ID | **FK → visual_assets.image_id** |
| `callout_id` | `pa.string()` | NULLABLE | Visual region ID of the callout | **FK → visual_regions.visual_region_id** |
| `visual_region_id` | `pa.string()` | NULLABLE | Visual region this evidence is anchored to | **FK → visual_regions.visual_region_id** |
| `blob_url` | `pa.string()` | NULLABLE | Blob URL when evidence is visual (image, figure, callout) | Required for `figure_callout`, `image_region` source types |
| `text` | `pa.string()` | NULLABLE | Supporting text, extracted value, or OCR content | |
| `content_hash` | `pa.string()` | NOT NULL | SHA-256 of `source_file_id + source_type + text + row/col/page context` | Dedup and change detection |
| `created_at` | `pa.timestamp("us", tz="UTC")` | NOT NULL | UTC timestamp | |

**Primary key:** `evidence_id`  
**Foreign keys:** `source_file_id` → `source_files`, `document_element_id` → `document_elements`, `chunk_id` → `chunks`, `image_id` → `visual_assets`, `callout_id` / `visual_region_id` → `visual_regions`

---

### 3.8 `visual_assets` Table

**Purpose:** Stores every extracted image, figure, diagram, screenshot, chart, or table-image artifact with its Blob URL and metadata.

| Column | pyarrow Type | Null? | Description | Key Notes |
|---|---|---|---|---|
| `image_id` | `pa.string()` | NOT NULL | Stable visual asset ID | **Primary key.** See §5.7 |
| `source_file_id` | `pa.string()` | NOT NULL | Source document or image file | **FK → source_files.source_file_id** |
| `document_element_id` | `pa.string()` | NULLABLE | Linked document element (figure, image, caption) | **FK → document_elements.document_element_id** |
| `asset_type` | `pa.string()` | NOT NULL | `figure`, `inline_image`, `screenshot`, `diagram`, `photo`, `chart`, `table_image` | |
| `page_number` | `pa.int32()` | NULLABLE | Source page if applicable | |
| `section_path` | `pa.string()` | NULLABLE | Nearby section heading context | |
| `caption` | `pa.string()` | NULLABLE | Figure caption or nearby title | |
| `alt_text` | `pa.string()` | NULLABLE | HTML/Office alt text if available | |
| `blob_url` | `pa.string()` | NULLABLE | Azure Blob Storage URL for the stored asset | Required after upload; validation fails if missing post-upload |
| `image_path` | `pa.string()` | NULLABLE | Local build artifact path before upload | |
| `image_hash` | `pa.string()` | NOT NULL | SHA-256 of image bytes | Used for dedup; identifies identical images across sources |
| `width` | `pa.int32()` | NULLABLE | Pixel width | |
| `height` | `pa.int32()` | NULLABLE | Pixel height | |
| `description` | `pa.string()` | NULLABLE | LLM-generated visual description | |
| `confidence` | `pa.float64()` | NULLABLE | Extraction/enrichment confidence [0.0, 1.0] | |
| `is_placeholder` | `pa.bool_()` | NOT NULL | True if generated as placeholder before real asset exists | |
| `created_at` | `pa.timestamp("us", tz="UTC")` | NOT NULL | UTC timestamp | |

**Primary key:** `image_id`  
**Unique constraint:** `image_hash` (per source_file_id; identical images in different sources may share a hash but differ in image_id)

---

### 3.9 `visual_regions` Table

**Purpose:** Stores sub-regions of visual assets — callouts, OCR text blocks, component regions, bounding boxes, and detected labels.

| Column | pyarrow Type | Null? | Description | Key Notes |
|---|---|---|---|---|
| `visual_region_id` | `pa.string()` | NOT NULL | Stable region ID | **Primary key.** See §5.8 |
| `image_id` | `pa.string()` | NOT NULL | Parent visual asset | **FK → visual_assets.image_id** |
| `region_type` | `pa.string()` | NOT NULL | `callout`, `ocr_text`, `component_region`, `connector_region`, `warning_region`, `table_region`, `detected_label` | |
| `label` | `pa.string()` | NULLABLE | Callout label, detected object label, or OCR block header | |
| `text` | `pa.string()` | NULLABLE | OCR text or region annotation | |
| `polygon_json` | `pa.string()` | NULLABLE | Region polygon or bounding box as JSON array of `[x, y]` pixel coordinates | |
| `normalized_polygon_json` | `pa.string()` | NULLABLE | Normalized polygon coordinates [0.0–1.0] relative to image dimensions | |
| `identified_entity_id` | `pa.string()` | NULLABLE | Entity this region has been linked to | **FK → entities.entity_id** |
| `blob_url` | `pa.string()` | NULLABLE | Blob URL for cropped region artifact or parent image | |
| `confidence` | `pa.float64()` | NULLABLE | Detection/extraction confidence [0.0, 1.0] | |
| `created_at` | `pa.timestamp("us", tz="UTC")` | NOT NULL | UTC timestamp | |

**Primary key:** `visual_region_id`  
**Foreign keys:** `image_id` → `visual_assets`, `identified_entity_id` → `entities`

#### 3.9.1 Column Provenance: Document Intelligence vs Vision LLM

> **Decision (team, 2026-06-24T11:46:10.517-07:00):** Azure AI Document Intelligence is **required** to populate `visual_regions`. It is an infrastructure dependency. Extraction behavior is specified in SPEC-004 (Verbal). Infrastructure setup is in `docs/infra` (Keyser). This section specifies schema and column-level provenance only.

| Column | Primary Source | Source Detail |
|---|---|---|
| `polygon_json` | **Document Intelligence** | Bounding polygon from Layout/Read API response (`boundingPolygon` per word/line/region). Pixel coordinates derived from page dimensions × normalized values. |
| `normalized_polygon_json` | **Document Intelligence** | `boundingPolygon` values normalized to [0.0–1.0] by dividing pixel coordinates by `width`/`height`. Written directly from the API response. |
| `text` | **Document Intelligence** | OCR output from Layout/Read API (`content` field on words, lines, or paragraphs). For `ocr_text` region_type rows, this is the authoritative source. |
| `region_type` | **Document Intelligence** (structural) + **Vision LLM** (semantic) | `ocr_text` and `table_region` are assigned by Document Intelligence analysis. `callout`, `component_region`, `connector_region`, `warning_region`, `detected_label` are assigned by vision LLM classification of the region content. |
| `label` | **Vision LLM** | Semantic label generated by the vision LLM (e.g., "Battery Connector", "Callout B"). Document Intelligence does not generate semantic labels. |
| `identified_entity_id` | **Vision LLM** | Entity linking performed by the vision LLM, mapping the region to a canonical entity. Document Intelligence provides no entity links. |
| `confidence` | **Document Intelligence** or **Vision LLM** | For `ocr_text` rows: confidence from Document Intelligence word/line confidence. For vision-LLM-classified rows: the LLM extraction confidence score. When both contribute, use the lower of the two values. |
| `blob_url` | **Pipeline runner** | Injected by the runner after Blob Storage upload. Neither Document Intelligence nor vision LLM generates this. |

> **Architecture note:** Document Intelligence produces the structural skeleton (OCR text, bounding boxes, polygon coordinates). The vision LLM adds semantic meaning (labels, entity links, region classification). The Parquet row merges both. SPEC-004 defines the enrichment pipeline that orchestrates this merge.

---

## 4. Primary Keys, Foreign Keys, and Referential Integrity Matrix

### 4.1 Primary Keys Summary

| Table | Primary Key Column |
|---|---|
| `source_files` | `source_file_id` |
| `document_elements` | `document_element_id` |
| `chunks` | `chunk_id` |
| `entities` | `entity_id` |
| `relationships` | `relationship_id` |
| `evidence` | `evidence_id` |
| `visual_assets` | `image_id` |
| `visual_regions` | `visual_region_id` |

### 4.2 Referential Integrity Matrix

Each cell shows which column in the child table references the parent table.

| Child Table → Column | References Table | Referenced Column | Null Allowed? |
|---|---|---|---|
| `document_elements.source_file_id` | `source_files` | `source_file_id` | No |
| `document_elements.parent_element_id` | `document_elements` | `document_element_id` | Yes |
| `chunks.source_file_id` | `source_files` | `source_file_id` | No |
| `chunks.document_element_id` | `document_elements` | `document_element_id` | Yes |
| `chunks.image_id` | `visual_assets` | `image_id` | Yes |
| `entities.source_file_id` | `source_files` | `source_file_id` | Yes |
| `relationships.source_entity_id` | `entities` | `entity_id` | No |
| `relationships.target_entity_id` | `entities` | `entity_id` | No |
| `relationships.evidence_id` | `evidence` | `evidence_id` | Yes |
| `evidence.source_file_id` | `source_files` | `source_file_id` | No |
| `evidence.document_element_id` | `document_elements` | `document_element_id` | Yes |
| `evidence.chunk_id` | `chunks` | `chunk_id` | Yes |
| `evidence.image_id` | `visual_assets` | `image_id` | Yes |
| `evidence.callout_id` | `visual_regions` | `visual_region_id` | Yes |
| `evidence.visual_region_id` | `visual_regions` | `visual_region_id` | Yes |
| `visual_assets.source_file_id` | `source_files` | `source_file_id` | No |
| `visual_assets.document_element_id` | `document_elements` | `document_element_id` | Yes |
| `visual_regions.image_id` | `visual_assets` | `image_id` | No |
| `visual_regions.identified_entity_id` | `entities` | `entity_id` | Yes |

### 4.3 Entity Relationship Diagram (text form)

```
source_files (1)
  ├── (N) document_elements  [source_file_id]
  │         └── (N) document_elements [parent_element_id, self-join]
  ├── (N) chunks             [source_file_id]
  ├── (N) entities           [source_file_id]
  ├── (N) evidence           [source_file_id]
  └── (N) visual_assets      [source_file_id]

document_elements (1)
  ├── (N) chunks             [document_element_id]
  ├── (N) evidence           [document_element_id]
  └── (N) visual_assets      [document_element_id]

visual_assets (1)
  ├── (N) visual_regions     [image_id]
  ├── (N) chunks             [image_id]
  └── (N) evidence           [image_id]

visual_regions (1)
  ├── (N) evidence           [callout_id / visual_region_id]
  └── (N) entities           [identified_entity_id, reverse]

entities (1)
  ├── (N) relationships      [source_entity_id]
  ├── (N) relationships      [target_entity_id]
  └── (N) visual_regions     [identified_entity_id]

evidence (1)
  └── (N) relationships      [evidence_id]
```

---

## 5. Deterministic ID Strategy for Data Rows

> **Scope note:** This section covers only **data row IDs** stored in Parquet tables. Fabric ontology type IDs (numeric IDs in `ontology/ids.lock.json`) are McManus's domain — see PRD §17. Do not use or redefine those IDs here.

### 5.1 General Hashing Scheme

All data row IDs are derived using SHA-256 over a deterministic canonical string. This guarantees:

- Same input → same ID across environments, runs, and machines
- No UUID generation required
- Natural deduplication via `content_hash`

```python
import hashlib

def make_id(prefix: str, canonical_string: str) -> str:
    digest = hashlib.sha256(canonical_string.encode("utf-8")).hexdigest()
    return f"{prefix}:{digest[:32]}"
```

The `prefix` encodes the table/entity type so IDs are visually scoped and debuggable.

### 5.2 `entity_id` — entities table

**Canonical key normalization:**

1. Lowercase the display name.
2. Strip leading/trailing whitespace.
3. Collapse internal whitespace runs to single space.
4. Remove all non-alphanumeric characters except `-` and space.
5. Replace spaces with `-`.
6. Prepend the entity type in lowercase, separated by `:`.

```python
import re

def normalize_canonical_key(entity_type: str, display_name: str) -> str:
    name = display_name.lower().strip()
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"[^a-z0-9\- ]", "", name)
    name = name.replace(" ", "-")
    return f"{entity_type.lower()}:{name}"

# Examples:
# Device, "Surface Laptop 5"    → "device:surface-laptop-5"
# Component, "Battery Pack"     → "component:battery-pack"
# PartNumber, "M1287099-003"    → "partnumber:m1287099-003"
```

**entity_id derivation:**

```python
canonical_key = normalize_canonical_key(entity_type, display_name)
entity_id = make_id("entity", canonical_key)
# e.g. "entity:a3f1b2c4d5e6f7a8b9c0d1e2f3a4b5c6"
```

**Deduplication:** At write time, rows with identical `canonical_key` are merged (last-writer-wins on `description`, `aliases`, `confidence`); the `entity_id` is stable.

### 5.3 `document_element_id` — document_elements table

```python
def make_document_element_id(source_file_id: str, element_type: str,
                              page: int | None, sort_order: int | None,
                              content_hash: str) -> str:
    parts = [source_file_id, element_type,
             str(page or ""), str(sort_order or ""), content_hash[:16]]
    canonical = ":".join(parts)
    return make_id("elem", canonical)
```

### 5.4 `chunk_id` — chunks table

```python
def make_chunk_id(source_file_id: str, chunk_type: str, content_hash: str) -> str:
    canonical = f"{source_file_id}:{chunk_type}:{content_hash}"
    return make_id("chunk", canonical)
```

### 5.5 `relationship_id` — relationships table

```python
def make_relationship_id(relationship_type: str, source_entity_id: str,
                          target_entity_id: str) -> str:
    canonical = f"{relationship_type}:{source_entity_id}:{target_entity_id}"
    return make_id("rel", canonical)
```

**Note:** Multiple evidence records can support the same logical relationship. The relationship row is deduplicated by this triple; evidence links are accumulated via the `evidence_id` on the relationship row (primary evidence) or via the evidence table's FK back to the relationship.

### 5.6 `evidence_id` — evidence table

```python
def make_evidence_id(source_file_id: str, source_type: str,
                     context_key: str, text_hash: str) -> str:
    # context_key encodes page+row+col+element IDs as available
    canonical = f"{source_file_id}:{source_type}:{context_key}:{text_hash[:16]}"
    return make_id("evid", canonical)
```

### 5.7 `image_id` — visual_assets table

```python
def make_image_id(source_file_id: str, image_hash: str) -> str:
    canonical = f"{source_file_id}:{image_hash}"
    return make_id("img", canonical)
```

Identical images extracted from different source files get different `image_id` values because `source_file_id` differs. The shared `image_hash` can be used for cross-source deduplication if needed.

### 5.8 `visual_region_id` — visual_regions table

```python
def make_visual_region_id(image_id: str, region_type: str,
                           label: str | None, sort_index: int) -> str:
    canonical = f"{image_id}:{region_type}:{label or ''}:{sort_index}"
    return make_id("vr", canonical)
```

### 5.9 `source_file_id` — source_files table

```python
def make_source_file_id(canonical_path: str, content_hash: str) -> str:
    # canonical_path: forward-slash-normalized relative path from project root
    canonical = f"{canonical_path}:{content_hash}"
    return make_id("src", canonical)
```

### 5.10 Stability Guarantees

| Guarantee | Mechanism |
|---|---|
| Same content → same ID | All IDs are deterministic hashes of content |
| Cross-environment stability | No UUIDs; no runtime state in ID generation |
| Schema evolution safety | ID inputs are column values, not row positions |
| Dedup via `content_hash` | SHA-256 of relevant content fields; updated rows produce new hashes and trigger re-enrichment detection |
| Canonical key stability | Normalization rules are pinned in this spec; changes require a spec version bump |

---

## 6. CSV / TSV / XLSX Ingestion Specification

### 6.1 Row-to-Table Mapping

When a CSV, TSV, or XLSX file is ingested:

1. One row is written to `source_files` for the file itself.
2. Each data row from the file is written to `document_elements` with `element_type = "table_row"`.
3. If the source schema maps to domain entities, the enrichment stage creates `entities` and `relationships` rows.
4. Evidence rows are created for each mapped data row or cell.

```
CSV file
  → source_files (1 row: source_file_id, path, source_type="csv", content_hash, ...)
  → document_elements (1 row per data row: element_type="table_row", content=serialized row)
  → enrichment → entities, relationships, evidence
```

**Header rows:** The header row is stored as a `document_elements` row with `element_type = "table"` containing the column metadata in `content_html` as an HTML table head.

### 6.2 Schema-Profile Output (`schema-profile.json`)

The `fabric-kg inspect-source` command produces a `schema-profile.json` file in `build/enriched/`. This file is referenced in `source_files.schema_profile_path`.

```json
{
  "schema_profile_version": "1",
  "source_file_id": "src:a3f1b2c4...",
  "source_path": "examples/csv/sample.csv",
  "source_type": "csv",
  "inspected_at": "2026-06-24T18:16:49Z",
  "row_count": 42,
  "column_count": 8,
  "columns": [
    {
      "index": 0,
      "name": "part_number",
      "inferred_type": "string",
      "null_count": 0,
      "unique_count": 42,
      "sample_values": ["M1287099-003", "M2045678-001"],
      "llm_suggested_entity_type": "PartNumber",
      "llm_mapping_notes": "Looks like Microsoft hardware part numbers; maps to PartNumber entity type."
    },
    {
      "index": 1,
      "name": "component_name",
      "inferred_type": "string",
      "null_count": 0,
      "unique_count": 15,
      "sample_values": ["Battery", "Display Assembly"],
      "llm_suggested_entity_type": "Component",
      "llm_mapping_notes": "Component name. Normalize to canonical_key component:battery."
    }
  ],
  "llm_table_summary": "Parts list for a Surface Laptop 5 repair guide with part numbers, components, and quantities.",
  "warnings": []
}
```

### 6.3 Column-to-Field Mapping Conventions

| Column name pattern | Maps to entity/field | Notes |
|---|---|---|
| `*_id`, `*_number`, `*_code` | `PartNumber` or `entity_id` hint | Treated as identifier entity |
| `*_name`, `name`, `label`, `title` | `display_name` of entity | Canonical_key derived from this |
| `description`, `*_desc`, `notes` | `entities.description` | |
| `type`, `*_type`, `category` | `entity_type` hint | LLM maps to ontology type |
| `source`, `origin`, `document` | `source_file_id` reference | |
| `page`, `page_number` | `page_number` on evidence | |
| `section`, `heading`, `chapter` | `section_path` on evidence | |
| `confidence`, `score`, `prob` | `confidence` on entity/relationship | Cast to float64 |
| `alias`, `alternate_name`, `synonym` | `aliases` list on entity | Appended to list |
| `blob_url`, `image_url`, `url` | `blob_url` on visual_assets | |

LLM inference can override any mapping. The schema-profile records the final mapping used.

### 6.4 XLSX Sheet Handling

Each sheet in an XLSX file is treated as a separate logical table:

- One `source_files` row for the entire XLSX file.
- One `document_elements` row per sheet with `element_type = "table"`, `title = sheet_name`.
- Data rows within a sheet are `element_type = "table_row"`, `parent_element_id = sheet_element_id`.

---

## 7. Parquet Writer Specification

### 7.1 File Layout under `build/parquet`

```
build/
  parquet/
    source_files.parquet
    document_elements.parquet
    chunks.parquet
    entities.parquet
    relationships.parquet
    evidence.parquet
    visual_assets.parquet
    visual_regions.parquet
```

MVP uses one file per table (no partitioning). All files are written at the end of the compile-data stage.

### 7.2 Writer Configuration

```python
import pyarrow as pa
import pyarrow.parquet as pq

# Writer settings (apply to all tables)
WRITER_CONFIG = {
    "compression": "snappy",          # Good balance of speed/size for Fabric Lakehouse
    "use_dictionary": True,           # Dictionary encoding for low-cardinality string columns
    "write_statistics": True,         # Enables Parquet predicate pushdown in Fabric
    "data_page_size": 1024 * 1024,   # 1 MB data page size
    "version": "2.6",                 # Parquet format version
}
```

### 7.3 Encoding of List and JSON Columns

**`list<string>` columns** (e.g., `aliases`, `related_entity_ids`):

- Written as pyarrow `pa.list_(pa.string())` — native Parquet list encoding.
- Fabric Lakehouse reads these as arrays; Spark treats them as `ArrayType(StringType)`.
- For inspection via pandas, use `pa.array(col, type=pa.list_(pa.string()))`.

**JSON string columns** (e.g., `polygon_json`, `properties_json`):

- Written as pyarrow `pa.string()`.
- The JSON string is always valid JSON (checked at write time).
- Never store Python dict objects directly — always serialize with `json.dumps`.

```python
import json

def safe_json_str(value: dict | list | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
```

### 7.4 Null Handling

- NOT NULL columns: if a null is encountered, the writer raises `ValueError` with the column name and row index.
- NULLABLE columns: pyarrow null (Python `None`) is written as Parquet null.
- Empty string `""` is not the same as null — do not substitute empty strings for missing optional values.
- Default values for `is_placeholder` and `confidence` columns:
  - `is_placeholder` defaults to `False` at write time if not set.
  - `confidence` is left as null when not computed.

### 7.5 Schema Enforcement at Write Time

Each table has a declared pyarrow schema object. Before writing, all row batches are cast against the schema:

```python
def write_table(table_name: str, rows: list[dict], schema: pa.Schema, out_dir: Path):
    batch = pa.RecordBatch.from_pylist(rows, schema=schema)
    pq.write_table(
        pa.Table.from_batches([batch]),
        out_dir / f"{table_name}.parquet",
        **WRITER_CONFIG,
    )
```

Schema mismatch (wrong type, extra columns, missing NOT NULL columns) raises at write time — never silently coerces.

### 7.6 Partitioning Notes (Post-MVP)

For large datasets in future sprints, consider partitioning:

| Table | Candidate partition column | Notes |
|---|---|---|
| `document_elements` | `element_type` | Enables element-type filtering pushdown |
| `chunks` | `chunk_type` | Isolate text vs. image chunks |
| `entities` | `entity_type` | Enables type-scoped Lakehouse queries |
| `evidence` | `source_type` | Isolate CSV vs. visual evidence |

Do not partition in MVP. Partitioning is a post-Sprint-1 optimization.

---

## 8. Placeholder Parquet Generation (PRD §18)

### 8.1 Purpose

Placeholders reserve the canonical Parquet schema before real data is available. This keeps CI/CD artifacts structurally valid for ontology binding and model-first deployment.

### 8.2 Placeholder File Layout

```
build/
  parquet/
    entities/_placeholder.parquet
    relationships/_placeholder.parquet
    chunks/_placeholder.parquet
    document_elements/_placeholder.parquet
    visual_assets/_placeholder.parquet
    visual_regions/_placeholder.parquet
    source_files/_placeholder.parquet
    evidence/_placeholder.parquet
```

> When placeholders are used, the table file (`entities.parquet`) is replaced by the placeholder subdirectory. The full data file is `entities.parquet` (single file, no subdirectory) when real data exists.

### 8.3 Placeholder Row Specification

A placeholder file contains exactly **one row** with:

- All NOT NULL columns populated with type-valid sentinel values.
- `is_placeholder = True` on tables that have this column.
- String NOT NULL columns: empty string `""` or a descriptive sentinel like `"__placeholder__"`.
- Timestamp NOT NULL columns: the generation timestamp.
- `content_hash`: SHA-256 of `"__placeholder__"`.

```python
def write_placeholder_parquet(schema: pa.Schema, out_path: Path,
                               placeholder_row: dict):
    """Write a single-row placeholder Parquet file with the given schema."""
    batch = pa.RecordBatch.from_pylist([placeholder_row], schema=schema)
    pq.write_table(pa.Table.from_batches([batch]), out_path, **WRITER_CONFIG)
```

### 8.4 Placeholder Row Examples

**entities placeholder:**

```python
{
    "entity_id":      "entity:__placeholder__",
    "entity_type":    "__placeholder__",
    "display_name":   "__placeholder__",
    "canonical_key":  "__placeholder__:__placeholder__",
    "aliases":        [],
    "description":    None,
    "properties_json": None,
    "source_file_id": None,
    "confidence":     None,
    "is_placeholder": True,
    "content_hash":   hashlib.sha256(b"__placeholder__").hexdigest(),
    "created_at":     datetime.now(UTC),
    "updated_at":     datetime.now(UTC),
}
```

### 8.5 Placeholder Detection

At validation time, placeholder files are detected by `is_placeholder = True` in all rows. A build that deploys only placeholder data to prod should fail validation with severity `warn` (not `fail`).

---

## 9. Data-Level Validation Rules

The following rules are checked by `fabric-kg validate`. They cover the data integrity subset of PRD §21.

### 9.1 Validation Checklist

| # | Rule | Severity | Tables Checked |
|---|---|---|---|
| D-01 | `source_file_id` values in all child tables exist in `source_files` | **fail** | All tables with `source_file_id` |
| D-02 | `entity_id` values in `relationships.source_entity_id` and `target_entity_id` exist in `entities` | **fail** | relationships → entities |
| D-03 | `evidence_id` values in `relationships.evidence_id` exist in `evidence` (when non-null) | **fail** | relationships → evidence |
| D-04 | `document_element_id` FK values exist in `document_elements` (when non-null) | **fail** | chunks, evidence → document_elements |
| D-05 | `image_id` FK values exist in `visual_assets` (when non-null) | **fail** | chunks, evidence → visual_assets |
| D-06 | `visual_region_id` / `callout_id` FK values exist in `visual_regions` (when non-null) | **fail** | evidence → visual_regions |
| D-07 | `entity_id` values in `visual_regions.identified_entity_id` exist in `entities` (when non-null) | **fail** | visual_regions → entities |
| D-08 | `parent_element_id` values in `document_elements` exist in `document_elements` (when non-null) | **fail** | document_elements self-join |
| D-09 | No duplicate `entity_id` values in `entities` | **fail** | entities |
| D-10 | No duplicate `relationship_id` values in `relationships` | **fail** | relationships |
| D-11 | No duplicate `chunk_id` values in `chunks` | **fail** | chunks |
| D-12 | No duplicate `evidence_id` values in `evidence` | **fail** | evidence |
| D-13 | No duplicate `image_id` values in `visual_assets` | **fail** | visual_assets |
| D-14 | No duplicate `visual_region_id` values in `visual_regions` | **fail** | visual_regions |
| D-15 | No duplicate `source_file_id` values in `source_files` | **fail** | source_files |
| D-16 | `blob_url` is not null for `visual_assets` rows with `is_placeholder = False` (post-upload) | **fail** | visual_assets |
| D-17 | `blob_url` is not null for `evidence` rows with `source_type` in `("figure_callout", "image_region")` and `is_placeholder = False` | **fail** | evidence |
| D-18 | `entity_type` values in `entities` exist in the registered ontology model types | **fail** | entities |
| D-19 | `relationship_type` values in `relationships` exist in the registered ontology model types | **fail** | relationships |
| D-20 | Parquet column names and types match the declared canonical schemas in this spec | **fail** | All tables |
| D-21 | `canonical_key` values in `entities` are unique | **fail** | entities |
| D-22 | `content_hash` is not null on any table that declares it NOT NULL | **fail** | All tables |
| D-23 | `confidence` values, when not null, are in range [0.0, 1.0] | **warn** | entities, relationships, visual_assets, visual_regions |
| D-24 | Entities exist with no relationships (isolated nodes) | **warn** | entities, relationships |
| D-25 | Chunks exist with no linked `document_element_id` (orphan chunks) | **warn** | chunks |
| D-26 | Visual assets exist with no linked `document_element_id` (unlinked images) | **warn** | visual_assets |
| D-27 | Evidence rows exist with no null `text` AND no `blob_url` (empty evidence) | **warn** | evidence |
| D-28 | Relationships have no supporting evidence (`evidence_id` null on all rows of that type) | **warn** | relationships |
| D-29 | All-placeholder Parquet files are present for every table | **warn** | All tables (placeholder check) |
| D-30 | `schema_profile_path` references exist on disk for CSV/XLSX sources | **warn** | source_files |

**Severity definitions:**

- **fail** — Build stops; artifact must not be deployed.
- **warn** — Build continues; issue is logged and surfaced in the validation report.

---

## 10. Sample Rows

### 10.1 `entities` Sample

```json
[
  {
    "entity_id": "entity:a3f1b2c4d5e6f7a8b9c0d1e2f3a4b5c6",
    "entity_type": "Device",
    "display_name": "Surface Laptop 5",
    "canonical_key": "device:surface-laptop-5",
    "aliases": ["Surface Laptop 5 13.5\"", "SL5"],
    "description": "Microsoft Surface Laptop 5 consumer laptop device.",
    "properties_json": "{\"sku\": \"R1T-00001\", \"release_year\": 2022}",
    "source_file_id": "src:b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
    "confidence": 0.97,
    "is_placeholder": false,
    "content_hash": "e3b0c44298fc1c149afb4c8996fb92427ae41e4649b934ca495991b7852b855",
    "created_at": "2026-06-24T18:16:49Z",
    "updated_at": "2026-06-24T18:16:49Z"
  },
  {
    "entity_id": "entity:c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9",
    "entity_type": "Component",
    "display_name": "Battery",
    "canonical_key": "component:battery",
    "aliases": ["Battery Pack", "Rechargeable Battery"],
    "description": "Internal rechargeable battery assembly for Surface Laptop 5.",
    "properties_json": null,
    "source_file_id": "src:b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
    "confidence": 0.91,
    "is_placeholder": false,
    "content_hash": "d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0",
    "created_at": "2026-06-24T18:16:49Z",
    "updated_at": "2026-06-24T18:16:49Z"
  }
]
```

### 10.2 `relationships` Sample

```json
[
  {
    "relationship_id": "rel:f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0",
    "relationship_type": "has_component",
    "source_entity_id": "entity:a3f1b2c4d5e6f7a8b9c0d1e2f3a4b5c6",
    "target_entity_id": "entity:c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9",
    "evidence_id": "evid:a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
    "properties_json": null,
    "confidence": 0.88,
    "is_placeholder": false,
    "content_hash": "b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8",
    "created_at": "2026-06-24T18:16:49Z"
  }
]
```

### 10.3 `evidence` Sample

```json
[
  {
    "evidence_id": "evid:a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
    "source_file_id": "src:b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
    "source_type": "figure_callout",
    "document_element_id": "elem:d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7",
    "chunk_id": null,
    "page_number": 42,
    "section_path": "Repair Procedures/Battery Replacement",
    "table_id": null,
    "row_index": null,
    "col_index": null,
    "figure_id": "elem:d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7",
    "image_id": "img:e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8",
    "callout_id": "vr:f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9",
    "visual_region_id": "vr:f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9",
    "blob_url": "https://kgassets.blob.core.windows.net/kg-assets/figure12.png",
    "text": "Callout B identifies the battery connector on the main board.",
    "content_hash": "c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0",
    "created_at": "2026-06-24T18:16:49Z"
  }
]
```

### 10.4 `chunks` Sample

```json
[
  {
    "chunk_id": "chunk:a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1",
    "source_file_id": "src:b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
    "document_element_id": "elem:d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7",
    "chunk_type": "image_description",
    "content": "Diagram showing the battery connector location on the Surface Laptop 5 main board. Callout B marks the battery connector at the upper left.",
    "content_html": null,
    "embedding_text": "Battery connector location Surface Laptop 5 main board callout B upper left.",
    "blob_url": "https://kgassets.blob.core.windows.net/kg-assets/figure12.png",
    "page_number": 42,
    "section_path": "Repair Procedures/Battery Replacement",
    "table_id": null,
    "figure_id": "elem:d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7",
    "image_id": "img:e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8",
    "related_entity_ids": [
      "entity:a3f1b2c4d5e6f7a8b9c0d1e2f3a4b5c6",
      "entity:c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9"
    ],
    "content_hash": "b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2",
    "created_at": "2026-06-24T18:16:49Z"
  }
]
```

### 10.5 `visual_assets` Sample

```json
[
  {
    "image_id": "img:e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8",
    "source_file_id": "src:b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7",
    "document_element_id": "elem:d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7",
    "asset_type": "diagram",
    "page_number": 42,
    "section_path": "Repair Procedures/Battery Replacement",
    "caption": "Figure 12: Battery connector location",
    "alt_text": null,
    "blob_url": "https://kgassets.blob.core.windows.net/kg-assets/figure12.png",
    "image_path": "build/images/figure12.png",
    "image_hash": "sha256:c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3",
    "width": 1024,
    "height": 768,
    "description": "Exploded diagram of the Surface Laptop 5 main board showing the battery connector at upper left, labeled with callouts A through F.",
    "confidence": 0.94,
    "is_placeholder": false,
    "created_at": "2026-06-24T18:16:49Z"
  }
]
```

---

## 11. Graph-to-Search Clue Chaining (Data Model Support)

> **Trigger:** This behavior is agent/orchestration-triggered — an AI agent or orchestration layer initiates steps A→B→C in sequence. The data model must make every join deterministic and queryable without runtime graph traversal inside AI Search.

> **Research basis:** All guidance in this section is grounded in production findings from RESEARCH-001 (§2–§8). AI Search index ownership (field schema, semantic config, scoring profiles) belongs to SPEC-001/SPEC-003. This section specifies only the **canonical Parquet columns that populate those index fields** and the join determinism that makes graph-guided retrieval reliable.

### 11.1 Overview of the Flow

The system supports a three-step agent-orchestrated retrieval pattern:

```
Step A  →  GQL query over Fabric Ontology
           Discovers entities and relationships relevant to a question.
           Returns: entity_ids (opaque stable IDs), canonical_keys,
                    display_names, aliases, relationship types,
                    neighbor entity_ids.

Step B  →  AI Search hybrid query using graph results as clues
           entity_ids / canonical_keys → search.in() OData FILTER
           search_aliases             → BM25 keyword SEARCH terms
           chunk_vector               → ANN vector query
           vectorFilterMode=preFilter constrains ANN to graph-relevant docs.
           Returns: grounding text chunks, image descriptions, table HTML.

Step C  →  Combine and answer
           Merge graph facts (entity properties, relationships) from
           Lakehouse with retrieved text/visual evidence from AI Search
           to produce a grounded, accurate answer with citations.
```

**The data model supports this flow deterministically.** No runtime join inside AI Search is required. All linkage is pre-materialized in Parquet at compile-data time.

### 11.2 Bidirectional Linkage Guarantee

The model provides **four reliable traversal paths** between entities and search-indexed content:

| Direction | Path | Key Columns |
|---|---|---|
| Entity → Chunks (forward) | `entities.entity_id` → `chunks.related_entity_ids[]` | Array contains; Spark: `array_contains(related_entity_ids, entity_id)` |
| Chunk → Entities (reverse) | `chunks.related_entity_ids[]` → `entities.entity_id` | FK array, unnested in Spark or exploded for Lakehouse query |
| Entity → Evidence | `entities.entity_id` → `evidence` via `visual_regions.identified_entity_id` → `evidence.visual_region_id` | Two-hop through visual_regions |
| Entity → AI Search (graph clue — filter) | `chunks.related_entity_ids[]` → AI Search `entity_ids` filterable field | `search.in(entity_ids, '...', ',')` — opaque ID exact match |
| Entity → AI Search (graph clue — keyword) | `entities.search_aliases[]` → `chunks.entity_search_keys[]` → AI Search `entity_aliases` searchable field | Pre-materialized at compile time; BM25 keyword match |

**Every entity is reachable from its chunks and vice versa.** The canonical join keys are:

```
entities.entity_id        ←→  chunks.related_entity_ids  (array membership; feeds AI Search entity_ids FILTER)
entities.canonical_key    →   chunks.entity_search_keys  (denormalized; feeds AI Search entity_aliases KEYWORD SEARCH)
entities.search_aliases   →   chunks.entity_search_keys  (denormalized; feeds AI Search entity_aliases KEYWORD SEARCH)
```

> **ID vs alias role separation:** `entity_ids` (from `chunks.related_entity_ids`) are opaque stable strings — used **only** as filter values, never as search text. `entity_search_keys` / `entity_aliases` are human-readable — used **only** as search/keyword terms, never as filter values. Reversing these roles is the label-vs-ID anti-pattern; see §11.4.

---

### 11.3 Canonical Column → AI Search Index Field Mapping

The table below maps every AI Search chunk-document field (RESEARCH-001 §4) to its canonical Parquet source column(s) and required index attribute. Index schema ownership belongs to SPEC-001/SPEC-003; this table specifies the **Parquet data contract** that makes those fields deterministically populatable.

| AI Search Field | Index Attribute | AI Search Type | Canonical Source Column(s) | Population Timing | Notes |
|---|---|---|---|---|---|
| `entity_ids` | filterable | `Collection(Edm.String)` | `chunks.related_entity_ids` | Written at LLM enrichment | Opaque SHA-256-derived IDs; exact-match filter only via `search.in()`; never searchable |
| `canonical_key` | filterable | `Edm.String` | `entities.canonical_key` (looked up from primary entity in `related_entity_ids[0]`) | Compile-data join | Single key for the primary entity a chunk represents; stable because normalization rule is pinned (§5.2) |
| `entity_aliases` | searchable | `Collection(Edm.String)` | `chunks.entity_search_keys` (itself populated from `entities.search_aliases`; see §11.8) | Compile-data join | Fed by `entities.search_aliases` = `[canonical_key] + [display_name.lower()] + [alias.lower() for alias in aliases]`; BM25/keyword matching only — not filterable |
| `entity_types` | filterable + facetable | `Collection(Edm.String)` | `entities.entity_type` for each entity_id in `chunks.related_entity_ids` | Compile-data join | Allows faceting by entity kind (Device, Component, PartNumber, etc.) |
| `source_path` | filterable + retrievable | `Edm.String` | `source_files.path` joined via `chunks.source_file_id` | Compile-data join | Relative path to source file; enables per-document scoping |
| `graph_path` | retrievable only | `Edm.String` | Assembled at push-pipeline time from GQL traversal result; not a stored Parquet column | Push pipeline (runtime) | Serialized traversal string, e.g. `"Device --[has_component]--> Component --[identified_by]--> PartNumber"`; passed to LLM as citation path |
| `blob_url` | filterable + retrievable | `Edm.String` | `chunks.blob_url` (for `image_description`, `figure_caption` chunk types) or `visual_assets.blob_url` | Written at upload / enrichment | Required on image/figure chunks; null for text-only chunks |
| `last_modified` | filterable + retrievable | `Edm.DateTimeOffset` | `chunks.created_at` (proxy; new content → new chunk_id + new `created_at`); paired with `chunks.content_hash` for change detection | Written at chunk creation | When chunk content changes, content-hash-driven ID derivation (§5.4) produces a new chunk_id and new `created_at`, which becomes the new `last_modified` in the index document |
| `content_type` | filterable + facetable | `Edm.String` | `chunks.chunk_type` (e.g. `section_text`, `table_html`, `image_description`, `procedure_step`); `document_elements.element_type` available for parent structural context | Written at chunking | Enables result-type filtering and faceting by element kind |

**Mapping notes:**

- `entity_ids` carries `chunks.related_entity_ids` verbatim — the same opaque IDs used as Fabric Ontology node IDs. They must never appear as BM25 search text; use `entity_aliases` for that.
- `entity_aliases` is fed by `chunks.entity_search_keys`, which flattens `entities.search_aliases` for all linked entities at compile-data time. This is the **only** field AI Search should use for keyword entity matching.
- `last_modified` maps to `chunks.created_at` in the current schema. The push pipeline uses `chunks.content_hash` to decide whether to re-embed before pushing. If only entity-linkage fields changed (e.g., new alias added), the pipeline does a partial merge-push without re-embedding.
- `graph_path` is not stored in Parquet. It is injected by the push pipeline from the orchestrator's GQL traversal result at index-document assembly time.
- `content_type` / `chunk_type` encodes element-kind semantics for result-type faceting. `chunks.chunk_type` values such as `image_description` and `table_html` signal to the agent which evidence modality a retrieved chunk represents.

---

### 11.4 Filter-on-IDs / Search-on-Aliases Split and `search.in()` Requirement

**Entity IDs and canonical keys are opaque, stable identifiers — not human-readable labels.** They must always travel as OData filter expressions, never as BM25 search query text. (RESEARCH-001 §2, §7)

| Use case | Correct approach | Anti-pattern |
|---|---|---|
| Filter chunks for a specific entity or a GQL-derived list | `search.in(entity_ids, 'entity:a3f1...,entity:c4d5...', ',')` | `entity_ids/any(k: k eq '...') or entity_ids/any(k: k eq '...')` — performance cliff at >50 IDs |
| Filter by the primary canonical key | `canonical_key eq 'component:battery'` | Adding `component:battery` to search text |
| Find chunks by entity name or alternate label | Add alias to `search` text → BM25 on `entity_aliases` | Putting alias in `search.in()` filter |
| Large entity sets returned by GQL (>50 IDs) | `search.in(entity_ids, '...comma-separated...', ',')` | `or`-chained `eq` expressions — hits 16 MB POST / 8 KB GET limits |

**`search.in()` is mandatory for entity ID lists from GQL.** It delivers sub-second response for hundreds of values and avoids OData `or`-clause performance cliffs and request-size limits. (RESEARCH-001 §2, §3)

> **Anti-pattern — label-vs-ID confusion:** Entity display names and aliases change over time and are not unique across entity types. Filtering on labels produces false matches and misses entities with different display names. Always filter on immutable `entity_ids` or `canonical_key`; always search on `entity_aliases` / `entity_search_keys`. (RESEARCH-001 §7)

```python
# CORRECT — search.in() filter from GQL entity_ids (opaque IDs)
entity_ids_from_gql = ["entity:a3f1...", "entity:c4d5...", "entity:d5e6..."]
entity_ids_csv      = ",".join(entity_ids_from_gql)
filter_expr         = f"search.in(entity_ids, '{entity_ids_csv}', ',')"

# CORRECT — alias terms from GQL search_aliases as keyword boost in search text
alias_terms  = ["battery", "battery pack", "surface laptop 5", "m1287099-003"]
search_text  = f"{user_query} {' '.join(alias_terms)}"

# ANTI-PATTERN — do not or-chain ID filters; degrades at >50 IDs
filter_wrong = " or ".join([f"entity_ids/any(k: k eq '{eid}')" for eid in entity_ids_from_gql])
```

---

### 11.5 `preFilter`: Entity IDs Must Be on Every Indexed Chunk

`vectorFilterMode: preFilter` constrains HNSW ANN traversal to only documents that pass the OData filter **before** vector scoring. This eliminates false positives from semantically similar but entity-irrelevant chunks. (RESEARCH-001 §3, §5)

**For preFilter to be effective, every chunk document pushed to AI Search must carry `entity_ids` (from `chunks.related_entity_ids`).** Chunks with null `related_entity_ids` are invisible to entity-scoped `preFilter` queries — which is intentional for non-entity-linked content but is a data gap for chunks that should be entity-linked.

| Chunk state | preFilter behaviour | Action required |
|---|---|---|
| `related_entity_ids` is null or empty | Excluded from `vectorFilterMode=preFilter` entity queries — correct for generic content | None; acceptable for non-entity content |
| `related_entity_ids` populated, `entity_search_keys` null | Matches filter but misses alias keyword boost — degraded quality | Triggers D-31 warning; rerun `compile-data` |
| `related_entity_ids` populated, `entity_search_keys` populated | Full graph-guided retrieval — correct | — |

The D-31 validation rule (§11.10) catches null `entity_search_keys` on entity-linked chunks at build time before the push pipeline runs.

---

### 11.6 KG↔Index Sync: Push Pipeline Required (Not OneLake Indexer)

> **Critical data-side constraint (RESEARCH-001 §5):** The Azure AI Search OneLake indexer supports only the Lakehouse **Files** location — it does **not** read Parquet or Delta tables. All canonical entity metadata and chunk linkage fields stored as Parquet must reach AI Search through a custom push pipeline (`compile-search` stage).

| Sync approach | Freshness | Applies to | Notes |
|---|---|---|---|
| OneLake indexer (scheduled) | ~5 min | Unstructured Files in Lakehouse only | **Cannot read Parquet/Delta** — not usable for canonical tables |
| Push API (real-time merge) | Seconds | Entity alias / linkage field updates | Merge action; does not require full re-embed |
| Fabric Data Pipeline → Push API | Near real-time | All canonical Parquet data (chunks, entity-linkage fields) | **Required path**; `compile-search` stage drives this |
| Eventstream → Function → Push | Seconds | High-frequency entity updates | Optional; for near-real-time alias propagation |

**Change detection and dedup (canonical data side):**

- `chunks.content_hash` (SHA-256 of `content`) is the canonical change signal. If unchanged, the push pipeline skips re-embedding and does a merge-push only if entity-linkage fields changed.
- `chunks.created_at` maps to `last_modified` in the AI Search document (§11.3). A content change produces a new `chunk_id` + new `created_at` (content-hash-driven ID derivation, §5.4).
- `document_elements.content_hash` (SHA-256 of `content + content_html`) detects changes in parent structural elements.
- On entity alias change: re-query affected entity's linked chunks → partial-update `entity_ids` / `entity_aliases` in the index via Push API merge action. Re-embed only if `chunk.content_hash` changed.
- On entity deletion: remove the entity_id from the `entity_ids` of linked chunks — do not delete chunks unless the source document is also removed.

---

### 11.7 Embedding Coupling: 1536 Dimensions

The `chunk_vector` field in AI Search index documents is produced by embedding `chunks.embedding_text` (or `chunks.content` when `embedding_text` is null) using **`text-embedding-3-large` at `dimensions=1536`**. (RESEARCH-001 §8)

| Canonical column | Role | Note |
|---|---|---|
| `chunks.embedding_text` | Primary embedding input | Cleaned/normalized text prepared for embedding; may differ from raw `content` |
| `chunks.content` | Fallback embedding input | Used if `embedding_text` is null |
| `chunks.content_hash` | Change-detection trigger | If unchanged between runs, the push pipeline skips re-embedding |

**Coupling constraint:** The AI Search index's `chunk_vector` field must declare `dimensions: 1536` and reference model `text-embedding-3-large`. Any change to model or dimensions requires full re-indexing of all chunks. This coupling is a SPEC-001/SPEC-003 concern; SPEC-002 provides the `chunks.embedding_text` canonical column that feeds the vectorization step. Do not redefine the index field here.

---

### 11.8 Join and Lookup Path Details

#### Entity → Chunk reverse lookup (Lakehouse / Spark)

```python
# Spark SQL: find all chunks for a given entity
chunks_df.filter(
    F.array_contains(F.col("related_entity_ids"), entity_id)
)

# Or for multiple entity_ids from a GQL result:
entity_ids = ["entity:a3f1...", "entity:c4d5..."]
chunks_df.filter(
    F.arrays_overlap(F.col("related_entity_ids"), F.array(*[F.lit(e) for e in entity_ids]))
)
```

#### Entity → AI Search (graph clue path — search.in() filter + alias keyword)

```python
# After GQL returns entity_ids and search_aliases for a scoped set of entities:
entity_ids_from_gql = ["entity:a3f1...", "entity:c4d5...", "entity:d5e6..."]
alias_query_terms   = ["battery", "battery pack", "surface laptop 5", "m1287099-003"]

# AI Search filter — search.in() on entity_ids (RESEARCH-001 §2):
entity_ids_csv = ",".join(entity_ids_from_gql)
filter_expr    = f"search.in(entity_ids, '{entity_ids_csv}', ',')"

# For a small targeted set, canonical_key exact-match is also valid:
# filter_expr = "canonical_key eq 'component:battery'"

# Alias terms go into the search text (BM25 on entity_aliases):
search_text = f"{user_query} {' '.join(alias_query_terms[:5])}"
```

#### Compile-time population of `entity_search_keys` on chunks

```python
def build_entity_search_keys(related_entity_ids: list[str],
                              entities_lookup: dict[str, dict]) -> list[str]:
    """
    Flatten search_aliases for all entities in related_entity_ids.
    entities_lookup: { entity_id: {"search_aliases": [...]} }
    """
    keys = []
    for eid in (related_entity_ids or []):
        ent = entities_lookup.get(eid)
        if ent:
            keys.extend(ent.get("search_aliases") or [])
    return list(dict.fromkeys(keys))  # dedup, preserve order
```

#### Compile-time population of `search_aliases` on entities

```python
def build_search_aliases(canonical_key: str, display_name: str,
                          aliases: list[str] | None) -> list[str]:
    keys = [canonical_key, display_name.lower()]
    for a in (aliases or []):
        keys.append(a.lower())
    return list(dict.fromkeys(keys))  # dedup, preserve order
```

---

### 11.9 Worked Example

**Question:** "How do I replace the battery in a Surface Laptop 5?"

**Step A — GQL over Fabric Ontology:**

```gql
MATCH (d:Device {canonical_key: "device:surface-laptop-5"})
      -[:has_component]->(c:Component)
      -[:identified_by]->(pn:PartNumber)
RETURN d.entity_id, d.canonical_key, d.display_name,
       c.entity_id, c.canonical_key, c.display_name, c.aliases,
       pn.entity_id, pn.canonical_key, pn.display_name
```

GQL returns, among others:

| entity_id | canonical_key | display_name | search_aliases |
|---|---|---|---|
| `entity:a3f1...` | `device:surface-laptop-5` | Surface Laptop 5 | `["device:surface-laptop-5", "surface laptop 5", "sl5"]` |
| `entity:c4d5...` | `component:battery` | Battery | `["component:battery", "battery", "battery pack", "rechargeable battery"]` |
| `entity:d5e6...` | `partnumber:m1287099-003` | M1287099-003 | `["partnumber:m1287099-003", "m1287099-003"]` |

**Step B — AI Search query built from GQL results:**

The orchestrator separates IDs (→ `search.in()` filter) from aliases (→ BM25 keyword search text):

```python
# IDs → search.in() filter — exact, stable, opaque (RESEARCH-001 §2)
entity_ids_from_gql = ["entity:a3f1...", "entity:c4d5...", "entity:d5e6..."]
entity_ids_csv      = ",".join(entity_ids_from_gql)

# Top aliases → search text — human-readable, BM25 on entity_aliases field
alias_query = "battery 'battery pack' 'surface laptop 5' m1287099-003"
```

Full AI Search request (`api-version=2026-04-01`):

```jsonc
{
  "search": "how to replace battery surface laptop 5 battery 'battery pack' m1287099-003",
  "vectorQueries": [{
    "kind": "text",
    "text": "replace battery surface laptop 5",
    "fields": "chunk_vector",
    "k": 50
  }],
  "filter": "search.in(entity_ids, 'entity:a3f1...,entity:c4d5...,entity:d5e6...', ',')",
  "vectorFilterMode": "preFilter",
  "queryType": "semantic",
  "semanticConfiguration": "fabric-semantic-config",
  "scoringProfile": "entity-boost",
  "select": "chunk_id, chunk_text, source_path, blob_url, entity_ids, canonical_key, entity_aliases, graph_path, last_modified, content_type",
  "top": 5,
  "captions": "extractive",
  "answers": "extractive|count-1"
}
```

> **Key construction rules applied here (RESEARCH-001 §2, §7):**
> - `filter` uses `search.in(entity_ids, ...)` on opaque canonical entity_ids — not alias text, not display names.
> - `search` field contains alias terms from `search_aliases` for BM25 recall on the `entity_aliases` (`entity_search_keys`) searchable field.
> - `vectorFilterMode: preFilter` constrains HNSW traversal to entity-relevant docs before ANN scoring.
> - These two roles (filter vs. search text) must never be swapped — swapping is the label-vs-ID anti-pattern.

AI Search returns ranked chunks including:
- Text chunk: "Step 3: Disconnect battery connector M1287099-003 from the main board..." (`content_type=procedure_step`)
- Image description chunk: "Callout B marks the battery connector at upper left of the main board" (`content_type=image_description`, `blob_url=figure12.png`)
- Table HTML chunk listing part numbers for the Battery Replacement kit

**Step C — Combine and answer:**

The agent combines:
- Graph facts from Lakehouse (entity properties, relationship context, part number metadata)
- Retrieved text from AI Search (step-by-step procedure, figure callout text, parts table)
- Visual evidence (figure12.png blob URL for the diagram)
- `graph_path` from the index document for citation: `"Device --[has_component]--> Component --[identified_by]--> PartNumber"`

→ Produces an accurate, grounded answer with traceable citations.

---

### 11.10 Required Data Conditions for Reliable Clue Chaining

For this flow to be deterministic, the following data conditions must hold at deploy time:

| Condition | Enforced By |
|---|---|
| Every chunk linked to an entity has `entity_search_keys` populated | Compile-data stage; D-31 validation rule |
| Every entity has `search_aliases` populated | Compile-data stage; D-32 validation rule |
| `chunks.related_entity_ids` contains only IDs present in `entities` | D-02 equivalent; existing FK check |
| `visual_regions.identified_entity_id` links are valid | D-07; existing FK check |
| Every indexed chunk carries `entity_ids` (from `related_entity_ids`) as a filterable field | Required for `vectorFilterMode=preFilter` effectiveness (§11.5); SPEC-001/SPEC-003 index schema |
| `entity_ids` and `entity_aliases` are separate fields with separate index attributes (filterable vs searchable) | Confirmed by §11.3 mapping table; enforced at compile-search stage |
| `content_hash` is populated on every chunk | Required for push-pipeline change detection (§11.6) |

**Validation rules (added to §9.1):**

| # | Rule | Severity | Tables Checked |
|---|---|---|---|
| D-31 | Chunks with non-null `related_entity_ids` must have non-null `entity_search_keys` | **warn** | chunks |
| D-32 | Entities with `is_placeholder = False` must have non-null `search_aliases` | **warn** | entities |

---

### 11.11 Design Constraints

- This is **orchestration-triggered**, not automatic. The data model enables the flow; the agent decides when to execute it.
- Entity data (graph nodes, relationships) stays in Lakehouse/Fabric Ontology only. AI Search holds only text/visual content with entity linkage fields.
- `entity_search_keys` on chunks is a **denormalized field** computed once at compile time. If entity aliases change, `compile-data` must be re-run to refresh it.
- The canonical_key normalization rule (§5.2) is the stable identity anchor. Any query building on canonical_keys relies on the normalization being pinned.
- **Parquet is NOT readable by the OneLake indexer.** All canonical Parquet data must reach AI Search via the push pipeline (`compile-search` stage). (RESEARCH-001 §5)
- **Embedding dimension coupling:** `chunk_vector` dimensions (1536, `text-embedding-3-large`) are index-level concerns (SPEC-001/SPEC-003). `chunks.embedding_text` is the canonical data-level input. A model or dimension change requires full re-indexing; the data-model change is minimal.
- **`search.in()` is the only acceptable mechanism** for passing entity ID lists from GQL results to AI Search filters. `or`-chaining `eq` expressions degrades performance and hits request-size limits for sets larger than ~50 IDs. (RESEARCH-001 §2, §7)

---


---

## Appendix A: pyarrow Schema Objects

Canonical schema objects for use in the writer. Import path: `fabric_kg_builder.schemas`.

```python
import pyarrow as pa

SCHEMAS = {
    "source_files": pa.schema([
        pa.field("source_file_id",     pa.string(),                  nullable=False),
        pa.field("path",               pa.string(),                  nullable=False),
        pa.field("filename",           pa.string(),                  nullable=False),
        pa.field("source_type",        pa.string(),                  nullable=False),
        pa.field("content_hash",       pa.string(),                  nullable=False),
        pa.field("byte_size",          pa.int64(),                   nullable=True),
        pa.field("ingested_at",        pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("schema_profile_path",pa.string(),                  nullable=True),
        pa.field("row_count",          pa.int64(),                   nullable=True),
        pa.field("notes",              pa.string(),                  nullable=True),
    ]),
    "document_elements": pa.schema([
        pa.field("document_element_id",pa.string(),                  nullable=False),
        pa.field("source_file_id",     pa.string(),                  nullable=False),
        pa.field("element_type",       pa.string(),                  nullable=False),
        pa.field("parent_element_id",  pa.string(),                  nullable=True),
        pa.field("title",              pa.string(),                  nullable=True),
        pa.field("content",            pa.string(),                  nullable=True),
        pa.field("content_html",       pa.string(),                  nullable=True),
        pa.field("blob_url",           pa.string(),                  nullable=True),
        pa.field("page_number",        pa.int32(),                   nullable=True),
        pa.field("section_path",       pa.string(),                  nullable=True),
        pa.field("sort_order",         pa.int32(),                   nullable=True),
        pa.field("row_index",          pa.int32(),                   nullable=True),
        pa.field("col_index",          pa.int32(),                   nullable=True),
        pa.field("content_hash",       pa.string(),                  nullable=False),
        pa.field("extracted_at",       pa.timestamp("us", tz="UTC"), nullable=False),
    ]),
    "chunks": pa.schema([
        pa.field("chunk_id",           pa.string(),                  nullable=False),
        pa.field("source_file_id",     pa.string(),                  nullable=False),
        pa.field("document_element_id",pa.string(),                  nullable=True),
        pa.field("chunk_type",         pa.string(),                  nullable=False),
        pa.field("content",            pa.string(),                  nullable=False),
        pa.field("content_html",       pa.string(),                  nullable=True),
        pa.field("embedding_text",     pa.string(),                  nullable=True),
        pa.field("blob_url",           pa.string(),                  nullable=True),
        pa.field("page_number",        pa.int32(),                   nullable=True),
        pa.field("section_path",       pa.string(),                  nullable=True),
        pa.field("table_id",           pa.string(),                  nullable=True),
        pa.field("figure_id",          pa.string(),                  nullable=True),
        pa.field("image_id",           pa.string(),                  nullable=True),
        pa.field("related_entity_ids", pa.list_(pa.string()),        nullable=True),
        pa.field("entity_search_keys", pa.list_(pa.string()),        nullable=True),
        pa.field("content_hash",       pa.string(),                  nullable=False),
        pa.field("created_at",         pa.timestamp("us", tz="UTC"), nullable=False),
    ]),
    "entities": pa.schema([
        pa.field("entity_id",          pa.string(),                  nullable=False),
        pa.field("entity_type",        pa.string(),                  nullable=False),
        pa.field("display_name",       pa.string(),                  nullable=False),
        pa.field("canonical_key",      pa.string(),                  nullable=False),
        pa.field("aliases",            pa.list_(pa.string()),        nullable=True),
        pa.field("search_aliases",     pa.list_(pa.string()),        nullable=True),
        pa.field("description",        pa.string(),                  nullable=True),
        pa.field("properties_json",    pa.string(),                  nullable=True),
        pa.field("source_file_id",     pa.string(),                  nullable=True),
        pa.field("confidence",         pa.float64(),                 nullable=True),
        pa.field("is_placeholder",     pa.bool_(),                   nullable=False),
        pa.field("content_hash",       pa.string(),                  nullable=False),
        pa.field("created_at",         pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("updated_at",         pa.timestamp("us", tz="UTC"), nullable=False),
    ]),
    "relationships": pa.schema([
        pa.field("relationship_id",    pa.string(),                  nullable=False),
        pa.field("relationship_type",  pa.string(),                  nullable=False),
        pa.field("source_entity_id",   pa.string(),                  nullable=False),
        pa.field("target_entity_id",   pa.string(),                  nullable=False),
        pa.field("evidence_id",        pa.string(),                  nullable=True),
        pa.field("properties_json",    pa.string(),                  nullable=True),
        pa.field("confidence",         pa.float64(),                 nullable=True),
        pa.field("is_placeholder",     pa.bool_(),                   nullable=False),
        pa.field("content_hash",       pa.string(),                  nullable=False),
        pa.field("created_at",         pa.timestamp("us", tz="UTC"), nullable=False),
    ]),
    "evidence": pa.schema([
        pa.field("evidence_id",        pa.string(),                  nullable=False),
        pa.field("source_file_id",     pa.string(),                  nullable=False),
        pa.field("source_type",        pa.string(),                  nullable=False),
        pa.field("document_element_id",pa.string(),                  nullable=True),
        pa.field("chunk_id",           pa.string(),                  nullable=True),
        pa.field("page_number",        pa.int32(),                   nullable=True),
        pa.field("section_path",       pa.string(),                  nullable=True),
        pa.field("table_id",           pa.string(),                  nullable=True),
        pa.field("row_index",          pa.int32(),                   nullable=True),
        pa.field("col_index",          pa.int32(),                   nullable=True),
        pa.field("figure_id",          pa.string(),                  nullable=True),
        pa.field("image_id",           pa.string(),                  nullable=True),
        pa.field("callout_id",         pa.string(),                  nullable=True),
        pa.field("visual_region_id",   pa.string(),                  nullable=True),
        pa.field("blob_url",           pa.string(),                  nullable=True),
        pa.field("text",               pa.string(),                  nullable=True),
        pa.field("content_hash",       pa.string(),                  nullable=False),
        pa.field("created_at",         pa.timestamp("us", tz="UTC"), nullable=False),
    ]),
    "visual_assets": pa.schema([
        pa.field("image_id",           pa.string(),                  nullable=False),
        pa.field("source_file_id",     pa.string(),                  nullable=False),
        pa.field("document_element_id",pa.string(),                  nullable=True),
        pa.field("asset_type",         pa.string(),                  nullable=False),
        pa.field("page_number",        pa.int32(),                   nullable=True),
        pa.field("section_path",       pa.string(),                  nullable=True),
        pa.field("caption",            pa.string(),                  nullable=True),
        pa.field("alt_text",           pa.string(),                  nullable=True),
        pa.field("blob_url",           pa.string(),                  nullable=True),
        pa.field("image_path",         pa.string(),                  nullable=True),
        pa.field("image_hash",         pa.string(),                  nullable=False),
        pa.field("width",              pa.int32(),                   nullable=True),
        pa.field("height",             pa.int32(),                   nullable=True),
        pa.field("description",        pa.string(),                  nullable=True),
        pa.field("confidence",         pa.float64(),                 nullable=True),
        pa.field("is_placeholder",     pa.bool_(),                   nullable=False),
        pa.field("created_at",         pa.timestamp("us", tz="UTC"), nullable=False),
    ]),
    "visual_regions": pa.schema([
        pa.field("visual_region_id",          pa.string(),           nullable=False),
        pa.field("image_id",                  pa.string(),           nullable=False),
        pa.field("region_type",               pa.string(),           nullable=False),
        pa.field("label",                     pa.string(),           nullable=True),
        pa.field("text",                      pa.string(),           nullable=True),
        pa.field("polygon_json",              pa.string(),           nullable=True),
        pa.field("normalized_polygon_json",   pa.string(),           nullable=True),
        pa.field("identified_entity_id",      pa.string(),           nullable=True),
        pa.field("blob_url",                  pa.string(),           nullable=True),
        pa.field("confidence",                pa.float64(),          nullable=True),
        pa.field("created_at",               pa.timestamp("us", tz="UTC"), nullable=False),
    ]),
}
```
