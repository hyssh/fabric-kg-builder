# Fenster — History

## Core Context

- **Project:** A Python CLI tool that builds and deploys knowledge graphs and Fabric ontologies from documents/CSV using OpenAI enrichment and canonical Parquet.
- **Role:** Data Engineer
- **Joined:** 2026-06-24T17:38:25.159Z

## Learnings

<!-- Append learnings below -->

### 2026-06-24T16:01:37-07:00 — Sprint 2 Search & Inspect: linkage, push, inspect-source, parquet e2e

- **`entity_ids` (filter) vs `entity_aliases` (search) split is a hard SPEC-002 §11.4 constraint.** Entity IDs are opaque SHA-256 hashes — usable only in `search.in()` filter expressions, never BM25-indexed. Aliases (canonical_name + synonyms) are human text — BM25-indexed but never filterable. Mixing the two columns breaks AI Search scoring and filter correctness simultaneously.
- **`chunks.entity_search_keys` should be populated at compile-data time, not derived at push time.** At push time the canonical record set may not be in memory. The compile-data pass already has the entity lookup in scope, so populating `entity_search_keys` there (§11.8) and reading it in `derive_chunk_doc` is the right seam. `derive_chunk_search_docs` falls back to on-the-fly derivation only for backward compat / tests.
- **`build_entity_lookup` must accept both plain dicts and Pydantic model instances.** The enrichment orchestrator produces `EntityRow` Pydantic objects, not dicts. Adding `ent.model_dump() if not isinstance(ent, dict) else ent` in the lookup builder keeps callers from needing to serialize before passing.
- **Change detection via `content_hash` is simpler than ETags for blob-sourced documents.** AI Search ETags only exist once a document is indexed; for new or updated chunks you need a client-side hash. SHA-256 of `(chunk_id, text_content)` is stable and cheap; comparing against `existing_hashes: dict[str, str]` in `push_chunk_docs` makes skip logic one line per doc.
- **`PushResult.skipped` is backward compatible when added as `skipped: int = 0`.** Existing callers that unpack only `(uploaded, failed)` continue to work; `__str__` can surface the skipped count for visibility.
- **VAL-008..012 are FK-style checks, not just dup checks.** VAL-010 (visual_regions → visual_assets), VAL-011 (evidence → visual_assets), VAL-012 (evidence → visual_regions) must all run after dup checks, since a missing FK target that is itself a dup would cause confusing double errors. Running them in gate order (dup first, FK second) gives cleaner diagnostics.
- **`inspect-source` on a 22-PDF, ~137 MB corpus takes ~13 minutes.** All PDFs are processed sequentially via `pdfplumber`; for directories with many large PDFs a progress bar or async extraction would improve UX. The combined summary (1,425 pages, 14,948 elements across 23 files) confirms the routing is correct.
- **`.md` files in the corpus are routed as HTML (via `html_extractor`), not as doc or CSV.** Markdown is valid HTML-adjacent text; `html_extractor` extracts minimal paragraphs. This is acceptable for inventory but produces low element counts (1 element for a 2 KB README). A dedicated Markdown extractor could improve element granularity if needed downstream.

### 2026-06-24T17:41:34-07:00 — Sprint 2 Extractors: pdfplumber, DOCX, HTML, chunking

- **`pdfplumber` line reconstruction is easiest from `extract_words(extra_attrs=["size"])`, not raw `chars`.** Grouping words by `top` with a small tolerance yields stable line text plus average font size, which is enough for heading detection without reimplementing PDF layout.
- **PDF heading heuristics need layered fallbacks.** The most reliable order is: larger-than-body font size ratio, numbered heading regex, ALL CAPS short lines, then colon-suffixed labels. Real manuals mix all four patterns across pages.
- **`python-docx` does not preserve paragraph/table order through `document.paragraphs` and `document.tables`.** Iterating `document.element.body.iterchildren()` is required to keep headings, paragraphs, and tables in document order.
- **HTML extraction should skip nested paragraph/heading tags inside `<table>` elements.** Otherwise the same content appears twice: once as a paragraph and again as a table-derived chunk/cell.
- **Chunk IDs stay deterministic only if the chunk content string is normalized before hashing.** For table chunks, plain-text extraction from HTML must feed both `content` and `embedding_text`, while `content_html` stays as the original structured payload.
- **Cell-level table decomposition works best as a second-pass normalizer.** Keep first-pass extractors responsible for section/page/table structure, then let `TableExtractor` derive `table_cell` rows from stored `content_html`.

### 2026-06-24T17:26:19-07:00 — Sprint 1 Implementation: compile-data command and data-integrity gates

- **`compile_data_cmd` is a thin orchestrator — it delegates all heavy lifting.** The command itself is ~230 lines; it calls `_load_enriched_json`, `run_gates`, and `write_all_tables`. Keeping I/O, validation, and writes as separate function calls makes each unit testable in isolation and avoids a god-function.
- **Datetime coercion is mandatory when loading from JSON.** The enrichment orchestrator serializes datetimes with `json.dumps(..., default=str)`, producing ISO-8601 strings like `"2026-06-24T12:00:00+00:00"`. PyArrow's `pa.timestamp("us", tz="UTC")` schema refuses bare strings — every timestamp column must be converted back to an aware `datetime` object before calling `write_table`. The `_DATETIME_FIELDS` registry + `_parse_dt` pattern handles this cleanly.
- **Windows cp1252 breaks any non-ASCII in `click.echo` output.** Characters like `→` (U+2192) and `—` (U+2014) are outside Windows-1252. Always use ASCII fallbacks (`->`, `-`) in CLI output or set `PYTHONUTF8=1` explicitly. The test suite runs fine because CliRunner captures output to a buffer, masking the error.
- **`sys.exit(5)` inside a Click command works correctly with CliRunner.** `result.exit_code` captures it as 5. This is the SPEC-001 §7 exit code for validation failure, distinct from 1 (I/O error) and 0 (success).
- **`CliRunner(mix_stderr=True)` vs `runner.invoke(..., mix_stderr=False)` — use the constructor form.** Passing `mix_stderr=False` to `invoke` raises `ValueError: stderr not separately captured` in Click 8.x unless the `CliRunner` was also constructed with `mix_stderr=False`. For tests that just check the combined output, use the default `mix_stderr=True` on the runner constructor and check `result.output` only.
- **Validation gates should run on raw merged data, not after silent dedup.** Running VAL-001..004 (duplicate ID checks) before any deduplication means a corrupt input (two entities with the same entity_id but different content) is caught and reported. Real pipeline flows (same entity in multiple enriched JSON files) are handled upstream by the orchestrator's per-batch canonical_key dedup — compile-data's gate is the safety net.
- **Empty Parquet tables (zero rows) are valid and necessary.** Writing all 8 tables even when enrichment only populates 4 (entities, relationships, chunks, evidence) ensures downstream consumers (compile-search, deploy-lakehouse) always find all 8 files. PyArrow writes empty tables cleanly; `write_table(name, [], out_dir)` with the NOT NULL check is a no-op on an empty list.
- **`run_gates` as a pure function returning `list[Violation]` is the cleanest interface.** The compile-data command owns the exit logic; `data_gates` only identifies problems. This keeps the gate module testable without CLI machinery and reusable by `validate-cmd` in a later sprint.
- **`.checkpoint.json` and `domain.json` must be explicitly skipped when globbing enriched JSONs.** Both files live in `build/enriched/` but are not batch data files. The `_SKIP_NAMES` set prevents a `json.JSONDecodeError` or wrong-structure error on checkpoint files.



- **utf-8-sig encoding is the right default for CSV reads.** Python's `utf-8-sig` codec transparently strips the UTF-8 BOM (`\xef\xbb\xbf`) that Excel and many Windows tools emit. Using it costs nothing and avoids the `\ufeffname` header-name bug.
- **`csv.Sniffer` is good enough for delimiter detection at Sprint-1 scale.** Pass `delimiters=",\t|;"` and a 4 KB sample. It correctly classifies the common cases. Fall back to comma on `csv.Error` — don't propagate the exception.
- **`pa.Table.from_pylist(rows, schema=schema)` is the safest PyArrow write path.** It performs strict schema coercion at Python dict → Arrow conversion time, surfacing type errors with useful messages. Avoid `from_pydict` with manual column arrays for row-oriented data — from_pylist keeps row integrity visible.
- **`list<string>` columns round-trip naturally via from_pylist.** Python `list[str]` (or `None`) maps directly to `pa.list_(pa.string())` when the schema is declared. No manual `pa.array(col, type=pa.list_(pa.string()))` wrapping needed at write time; that wrapping is only needed for inspection/read transforms.
- **NOT NULL enforcement belongs before Arrow conversion, not after.** Arrow itself may silently coerce `None` in some contexts; explicit pre-validation produces a clear error message at the right abstraction level.
- **Placeholder rows must use `_NOW_SENTINEL = datetime(2000, 1, 1, tzinfo=UTC)` — not `datetime.now()`.** A sentinel fixed in the year 2000 makes placeholder detection trivially visible in any data inspector and avoids non-deterministic timestamps in build artifacts.
- **SPEC-002 §8.2 says placeholders go in `<table>/_placeholder.parquet` subdirs, not flat files.** The subdirectory structure reserves the flat `<table>.parquet` name exclusively for real data. Important for CI artifact validation.
- **Pre-existing test failure in `test_foundry_client.py` (temperature/seed assertion) is unrelated** — confirmed by `git stash` round-trip. Do not touch it in this sprint.
- **`examples/csv/sample.csv` and `tests/fixtures/csv/sample.csv` are different files with different purposes.** The `examples/` file is the public demo (6 rows, Surface Laptop 5 + Pro 9 BOM). The `tests/fixtures/` file is the minimal test stub (3 rows, used by `sample_csv_path` fixture in conftest). Keep them in sync on schema/column names but they can differ in row count.

### 2026-06-24T12:42:17.255-07:00 — SPEC-002 v1.3: Reconcile consistency-review MINOR #1 (filter-on-IDs / search-on-aliases)

- **§3.4 `entity_search_keys` note was stale:** It said "GQL returns entity canonical_keys → AI Search filter on `entity_search_keys`." That is wrong. `entity_search_keys` feeds only the `entity_aliases` SEARCHABLE field (BM25/keyword matching). Filtering is done exclusively on `entity_ids` / `canonical_key` (stable IDs). Corrected the Key Notes column accordingly.
- **§3.5 `search_aliases` note had "filterable/searchable":** `search_aliases` is exclusively a SEARCHABLE (not filterable) source. Removed "filterable/" to match §11.4's filter-on-IDs / search-on-aliases split.
- **The canonical split in one sentence:** `entity_search_keys` → `entity_aliases` SEARCHABLE (BM25); `related_entity_ids` → `entity_ids` FILTERABLE (`search.in()`). Never invert these roles.

### 2026-06-24T12:42:17.255-07:00 — SPEC-002 v1.2: RESEARCH-001 Fold into §11

- **search.in() is mandatory for entity ID lists from GQL — never or-chain `eq`.** At >50 IDs the or-chain hits OData clause limits and the 16 MB POST / 8 KB GET size limits. `search.in(entity_ids, '...csv...', ',')` delivers sub-second response for hundreds of values.
- **ID vs alias role separation is the most critical architectural constraint in the graph-to-search path.** `entity_ids` (from `chunks.related_entity_ids`) are opaque SHA-256-derived strings — filter only, never searchable. `entity_search_keys` / `entity_aliases` (from `entities.search_aliases`) are human-readable — searchable only, never filterable. Swapping them is the label-vs-ID anti-pattern and causes false matches and missed entities.
- **The canonical → AI Search field mapping has nine fields with distinct source columns and timing.** Key couplings: `entity_ids` ← `chunks.related_entity_ids` (enrichment), `entity_aliases` ← `chunks.entity_search_keys` ← `entities.search_aliases` (compile-data), `last_modified` ← `chunks.created_at` (content-hash-driven chunk refresh), `graph_path` ← runtime GQL traversal string (not a Parquet column — injected by push pipeline).
- **Parquet is NOT readable by the OneLake indexer.** OneLake indexer supports only the Files location. All canonical Parquet data must reach AI Search through the push pipeline (`compile-search` stage). This is a hard constraint from RESEARCH-001 §5 that was missing from the prior §11.
- **`vectorFilterMode=preFilter` requires `entity_ids` on every entity-linked chunk document.** Chunks with null `related_entity_ids` are invisible to preFilter entity queries. D-31 validation at build time prevents silent gaps.
- **Embedding coupling: `chunks.embedding_text` → `text-embedding-3-large` @ `dimensions=1536`.** Any change to model or dimensions requires full re-indexing. The data model only supplies `embedding_text`; the index field definition is SPEC-001/SPEC-003 territory.
- **`graph_path` is not a Parquet column.** It is assembled at push-pipeline time from the GQL traversal result and injected into the AI Search index document. Do not expect it in Parquet — only in index documents and LLM citation context.
- **content_hash drives push-pipeline change detection.** If `chunks.content_hash` is unchanged, the pipeline skips re-embedding and does a merge-push only for changed entity-linkage fields. This is the dedup + freshness mechanism for the incremental sync loop.

### 2026-06-24T11:46:10.517-07:00 — SPEC-002 v1.1: Graph-to-Search, Doc Intelligence, Lakehouse Boundary

- **Graph-to-Search Clue Chaining requires two new denormalized fields:** `entities.search_aliases` (list<string>: canonical_key + display_name + aliases lowercased) and `chunks.entity_search_keys` (list<string>: flattened search_aliases from all related entities). These are computed at compile-data time — not by the LLM. They make the entity→AI Search path deterministic without runtime joins inside AI Search.
- **The bidirectional link entity↔chunk uses two complementary columns:** `chunks.related_entity_ids` for exact entity_id lookups (Lakehouse/Spark joins), and `chunks.entity_search_keys` for AI Search filter/query (OData array-contains). Do not conflate them — one is the FK anchor, the other is the search surface.
- **Document Intelligence is the source of truth for polygon/OCR, vision LLM for semantics.** `polygon_json`, `normalized_polygon_json`, and `text` on visual_regions come from Document Intelligence Layout/Read. `label` and `identified_entity_id` come from vision LLM. `region_type` is split: `ocr_text`/`table_region` = Doc Intelligence; `callout`/`component_region` etc. = vision LLM. The provenance table in §3.9.1 is the authoritative source.
- **Structured Parquet tables (entities, relationships, evidence, source_files, document_elements, visual_regions) are Lakehouse-only.** Only chunks and visual_assets feed AI Search, and only their text/description/caption/blob_url content — not the raw Parquet records. The §2.1 boundary table is the canonical reference.
- **Spec section ordering matters:** §11 (new major feature section) should come before Appendix A (code reference). Reordering required file-level surgery with PowerShell because the edit tool inserts at end-of-file relative to the replaced block.
- **New validation rules D-31 and D-32** (warn severity) enforce that `entity_search_keys` and `search_aliases` are populated on non-placeholder rows. This prevents silent failures in the graph-to-search path.

### 2026-06-24 — SPEC-002 Canonical Data Model

- **8 Parquet tables form the full data contract:** source_files, document_elements, chunks, entities, relationships, evidence, visual_assets, visual_regions. All downstream consumers (Fabric, AI Search) read only from these tables, never from LLM JSON.
- **ID strategy is content-hash-derived, not UUID-based.** All data row IDs use `sha256(canonical_string)[:32]` with a typed prefix (e.g. `entity:`, `chunk:`, `img:`). This guarantees cross-environment stability and natural dedup via `content_hash`.
- **canonical_key normalization** for entities follows a strict rule: lowercase → strip → collapse whitespace → remove non-alphanumeric except dash → replace spaces with dash → prepend `type:`. This is the dedup key for entities across sources.
- **Fabric ontology type IDs** (numeric IDs in `ids.lock.json`) are McManus's domain. SPEC-002 only defines data-row IDs (strings in Parquet). Do not mix these two concerns.
- **list<string> columns** (aliases, related_entity_ids) are native pyarrow `pa.list_(pa.string())` — not JSON strings. Only bespoke JSON objects (polygon, properties) use `pa.string()` JSON columns.
- **Placeholder Parquet** = one row per table with `is_placeholder=True` and typed sentinel values. They live in subdirectories (e.g. `entities/_placeholder.parquet`) and are replaced by the single full-data file (`entities.parquet`) once real data exists.
- **CSV ingestion** produces one `source_files` row + N `document_elements` rows (`element_type="table_row"`). Schema-profile.json records column mapping decisions from LLM inspection and is referenced in `source_files.schema_profile_path`.
- **Validation severity:** 22 fail-level rules (FK integrity, duplicates, schema mismatch, missing blob_urls post-upload) and 8 warn-level rules (orphan nodes, empty evidence, isolated entities). Build stops only on fail.
