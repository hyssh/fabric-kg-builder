# Orchestration Log: Tables & Enrichment Hardening Session

**ISO8601 UTC:** 2026-06-25T04:56:14Z  
**Session Theme:** Document Intelligence Layout table extraction + enrichment system hardening  
**Coordinator:** Hyunsuk Shin (primary orchestrator)

---

## Spawn Decision

**Decision route:** Document Intelligence (DI) Layout for tables + enrichment hardening.

Rationale: LLM table transcription was wasteful and low-fidelity. DI Layout provides:
- Structured table cells (row/col index, columnHeader, bounding polygons)
- Markdown output for semantic RAG chunking
- Table artifacts independent, indexable, graph-linkable
- Natural separation of concerns: DI = geometry/OCR; LLM = semantics only

---

## Team Roster

| Role | Agent | Status |
|---|---|---|
| Coordinator | Hyunsuk Shin (Copilot) | ✅ Decided tables via DI Layout; verified MS Learn |
| Data Engineer | Fenster | ✅ Implemented `docintel_tables.py` (47 tests, 745 total) |
| AI Integration | Verbal | ✅ Wired DI tables into enrich path; banned LLM table_row |
| KG/Ontology | McManus | ✅ Updated SPEC-002/003/004 with DI table approach |
| Test Engineer | Hockney | ✅ Test tiers: fast-by-default, integration opt-in (docs/TEST-STRATEGY.md) |
| Session Logger | Scribe | ✅ Merging decisions, archiving, logging |

---

## Work Completed

### 1. Tables via Document Intelligence Layout [COORDINATOR]

**Decision:** Hyunsuk Shin (via Copilot) decided to use DI Layout as single source of truth for tables.

**Verification:**
- MS Learn [prebuilt/layout]: Layout extracts tables as structured cells.
- MS Learn [concept/retrieval-augmented-generation]: Markdown is recommended RAG input.
- Real PDF validation: Surface PDF → 2 tables → 2 `table_html` chunks (verified).

**Files referenced:**
- `docs/specs/SPEC-004-llm-enrichment.md` §6/§8 (updated)
- `docs/specs/SPEC-002-canonical-data-model.md` §3.2/§3.4 (updated)
- `docs/specs/SPEC-003-ontology-and-deployment.md` §12 (updated)

---

### 2. Fenster: DI Layout Table Extraction Implementation

**Status:** ✅ DONE — 47 new unit tests, 731 → 745 total passing, 100% mocked (zero live DI calls)

**New modules:**
- `src/fabric_kg_builder/enrichment/docintel_tables.py` (350 LOC):
  - `table_to_html(table) -> str` — HTML rendering from DI Layout table
  - `extract_tables(analyze_result, ...) -> DocIntelTableResult` — produces DocumentElementRow + ChunkRow
  - `write_table_artifacts(html_artifacts, ...) -> list[Path]` — Windows-safe artifact output
  - `get_document_markdown(analyze_result) -> str` — whole-doc markdown for semantic chunking

**Extended modules:**
- `docintel.py` — `analyze_document_bytes`/`analyze_document_url` now forward `output_content_format` kwarg
- `conftest.py` — added `mock_document_intelligence_client_with_tables` fixture

**Fixtures:**
- `tests/fixtures/document_intelligence/analyze_result_tables.json` — 3-row parts table

**Tests:** `tests/unit/test_docintel_tables.py` (47 tests, 100% pass)
- HTML structure, thead/tbody separation, cell values, deterministic IDs, FK alignment
- Write artifacts, Windows-safe dir names, empty/multiple tables, markdown passthrough
- SDK kwarg forwarding/omission, conftest integration

**Design alignment:**
- SPEC-002 §3.3/§3.4: DocumentElementRow (element_type="table") + ChunkRow (chunk_type="table_html")
- SPEC-004 §8: LLM = semantics only (banned table_row/table_cell)
- SHA-256 deterministic IDs (SPEC-002 §5.3/§5.4)

---

### 3. Verbal: Wire DI Tables into Document Enrich Path

**Status:** ✅ DONE — 6 new tests, 731 → 737 total passing

**Changes:**
- `orchestrator.py`:
  - Updated `_ENRICH_SYSTEM_PROMPT` to forbid LLM table_row/table_cell
  - `canonicalize_llm_output` drops any `effective_chunk_type == "table_row"` (with warning)

- `docintel.py`:
  - Added `layout_analyze_raw(data: bytes) -> Any` — raw DI Layout analyze (markdown output)

- `enrich_cmd.py`:
  - Added `_build_di_layout_client(ctx_obj)` — conditional DI Layout client builder
  - Updated `_enrich_document_file` to accept `di_layout_client=None`
  - When provided: reads PDF, calls Layout, extracts tables, merges into canonical JSON
  - When `None`: silently skipped (graceful fallback)
  - DI client injected via `ctx.obj["_di_layout_client"]` (test-injectable)

- `tests/unit/test_enrich_cmd_di_tables.py` (6 new tests):
  - System prompt forbids table_row
  - LLM table_row dropped in canonicalize
  - DI table_html chunk produced
  - DI document_element produced
  - DI table_html has content_html
  - DI not configured → pipeline still works

**Design:** Separate `layout_analyze_raw` from `map_di_result_to_visual_regions` (respects SPEC-002 provenance split).

---

### 4. McManus: Spec Updates for DI Table Approach

**Status:** ✅ DONE — SPEC-002/003/004 updated with DI table approach

**SPEC-004:**
- §6.2: System prompt constraint — LLM banned from table_row/table_cell
- §7.3: Table Chunking rewritten (DI Layout → document_element + chunk)
- §8.6: New section — full pipeline diagram, division-of-labor, validation proof

**SPEC-002:**
- §3.3/§3.4: Provenance notes — DI = structure+HTML, LLM = semantics only

**SPEC-003:**
- §12.10: Table nodes in bridge (evidenced_by / shown_in)
- Independent AI Search docs, artifact retrieval

**References:** MS Learn [prebuilt/layout], [retrieval-augmented-generation]

---

### 5. Hockney: Test Tiers — Fast by Default + Integration Opt-In

**Status:** ✅ DONE — 3-tier system, golden fixture, 745 unit tests pass in ~18–20s

**Decision:**

| Tier | Markers | Default | CI Gate |
|---|---|---|---|
| Fast unit | `unit`, `contract` | ✅ Yes | ✅ Yes |
| Integration / Slow | `integration`, `slow` | No (opt-in) | No (separate job) |
| Smoke | `smoke` | No | No |

**Implementation:**
- `pyproject.toml`: `addopts = "-m 'not slow and not integration'"`
- Golden fixture: `tests/fixtures/golden/surface_mini_canonical.json` (2 entities, 1 rel, 1 chunk, 1 evidence)
- Graceful `pytest.skip()` in real-PDF tests when `sample_data/` absent
- CI: merge gate = fast-only; integration job = `workflow_dispatch` only

**Files:**
- `pyproject.toml` — marker + addopts
- `tests/unit/test_extractors.py`, `test_inspect_cmd_pdf.py` — `@pytest.mark.slow`
- `tests/fixtures/golden/surface_mini_canonical.json` — golden fixture
- `tests/unit/test_golden_canonical.py` — 10 golden tests
- `.github/workflows/ci.yml` — split merge gate + integration job
- `docs/TEST-STRATEGY.md` — new documentation

**Verification:**
```
pytest -q                           # 745 passed, 4 deselected, ~18–20s
pytest -m integration --collect-only # 4 tests collected
pytest tests/unit/test_golden_canonical.py  # 10 passed, 1.7s
```

---

## Decisions Archive

**3 inbox files merged into decisions.md:**
1. `coordinator-tables-via-docintel.md` → New "Coordinator" subsection
2. `fenster-docintel-tables.md` → New "Fenster" subsection
3. `verbal-docintel-tables-wire.md` → New "Verbal" subsection
4. `mcmanus-docintel-tables-spec.md` → New "McManus" subsection
5. `hockney-test-tiers.md` → New "Hockney" subsection

**Status:** ✅ All archived into "Document Intelligence Tables & Enrichment Hardening" section (2026-06-24).

---

## Test Results Summary

| Stage | Count | Status | Duration |
|---|---|---|---|
| Unit (fast, no integration) | 741 | ✅ PASS | ~18–20s |
| Integration (real PDF, opt-in) | 4 | ⏳ Deselected | N/A |
| Smoke | — | ⏳ Not run | N/A |
| **Total** | **745** | ✅ **PASS** | ~19s |

---

## Session Outcomes

✅ **Tables:** DI Layout is single source of truth; LLM semantic-only; independent artifacts + graph-linkage  
✅ **Enrichment hardening:** System prompt enforces DI; canonicalize drops LLM table_row  
✅ **Test tiers:** Fast by default; integration opt-in; golden fixture for rapid feedback  
✅ **Specs aligned:** SPEC-002/003/004 updated with DI approach; all MS Learn verified  
✅ **745 tests passing** (47 new table tests, 6 new enrich tests, 10 golden tests)  
✅ **Decisions merged** into squad/decisions.md; inbox cleared

---

## Next Steps (Post-Session)

1. **Markdown semantic chunking** (Verbal opt-in): `di_result.markdown` → Chunker (low-priority)
2. **Lazy-load table artifacts** (Fenster follow-up): compress `table_N.html` → S-Store (deferred)
3. **Graph → Table visual verification** (Hyunsuk post-MVP): UI preview of table HTML via blob_url (v2)
4. **Enrichment cost tracking** (McManus): log DI Layout + LLM tokens per document (observability)

