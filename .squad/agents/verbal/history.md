# Verbal — History

## Core Context

- **Project:** A Python CLI tool that builds and deploys knowledge graphs and Fabric ontologies from documents/CSV using OpenAI enrichment and canonical Parquet.
- **Role:** AI Integration Dev
- **Joined:** 2026-06-24T17:38:25.163Z

## Summary

Sprint 1: Foundry client + LLM enrichment pipeline, token mocking, checkpoints. Sprint 2: PDF/DOCX routing, multi-section batching with independent checkpoints, resilience (optional fields with leniency, per-item drop on validation failure). Live hardening: AzureOpenAI SDK replacement (verified DefaultAzureCredential, gpt-5-4-mini, 1536d embeddings). UTF-8 console fix (Windows cp1252 crash on arrows). Entity merge on partial failures, field aliases (name→label, type→relation), dedup.

**Sprint 3 — DI table wire (2026-06-24):** Wired Document Intelligence Layout as source of truth for tables. Stopped LLM from transcribing table_row/table_cell chunks (updated `_ENRICH_SYSTEM_PROMPT` + `canonicalize_llm_output` drops `table_row`). Added `DocIntelClient.layout_analyze_raw()` returning raw AnalyzeResult for `extract_tables()`. Added `_build_di_layout_client()` in enrich_cmd; `_enrich_document_file` now accepts `di_layout_client`, calls DI Layout, merges `table_html` chunks + table document_elements into canonical JSON. Graceful fallback when DI not configured (None client = skip DI, no crash). 6 new tests.

**Sprint 4 — Real visual extraction (2026-06-24):** Implemented real figure/image extraction so `visual_assets` and `visual_regions` tables are populated. Key changes:
- **image_extractor.py**: Added `_polygon_to_rect` (DI polygon inches→PyMuPDF points ×72), `_render_figure_crop` (page.get_pixmap with zoom 200/72), `extract_figures_from_di` (iterates DI `.figures`, renders crops, deduplicates by hash, returns `VisualAssetCandidate` list), `make_visual_regions_for_figure` (produces one `VisualRegionRow` per figure with polygon_json, normalized_polygon_json, FK to image_id). Added `polygon` field to `VisualAssetCandidate`. `_fitz_open` kwarg injection for clean unit testing without patching.
- **enrich_cmd.py**: Added `_build_blob_uploader` (returns None when account_name empty — graceful). Refactored DI block to expose `di_analyze_result` for reuse. Added figure extraction block (after table extraction, before records write): calls `extract_figures_from_di` → uploads via blob_uploader → appends to canonical JSON as `visual_assets` + `visual_regions`. Added `_blob_uploader` injection via `ctx.obj`. Canonical JSON now always has `visual_assets` and `visual_regions` keys (empty lists when DI/blob not configured).
- **tests/unit/test_enrich_cmd_visual.py** (new): 20 tests covering unit + integration path with mocked fitz, mocked DI, mocked blob.

**Key insights (Sprint 4):**
- DI polygon units are **inches** → multiply ×72 for PyMuPDF points. Reference: `starbuck-siot-kb/ingestion/images.py`.
- `fitz.open` injectable via kwarg (`_fitz_open`) for unit tests; `patch("fitz.open")` for integration tests (fitz is really installed, module-level patch works reliably).
- `di_analyze_result` shared between table extraction and figure extraction — single DI call per document.
- Graceful fallback when blob=None OR di=None: figure extraction skipped, pipeline continues, exit 0.
- Existing tests unaffected: `blob_uploader=None` is the default, figure extraction is additive.

**Tests:** 20 new visual extraction tests. **Total:** 832 unit tests passing (was 737 + 6 DI table + remaining = 832 total).

Full history and details in history-archive.md.
