# Hockney — History

## Core Context

- **Project:** A Python CLI tool that builds and deploys knowledge graphs and Fabric ontologies from documents/CSV using OpenAI enrichment and canonical Parquet.
- **Role:** Tester
- **Joined:** 2026-06-24T17:38:25.166Z

## Current Sprint

- **2026-06-24 (Tables + Enrichment Hardening):** Completed test tier strategy (fast-by-default, integration opt-in); 10 golden fixture tests added; 745 unit tests passing. See history-archive.md for detailed learnings.

- **2026-07-22 (Ontology Integrity — Issues #7 + #8):** Authored 70 comprehensive unit tests in `tests/unit/test_identity_validation.py` for the planned `ontology/identity_validation.py` module (McManus). Tests cover OKV-001 (relationship key mismatch: identity map resolution, FK alias validity, source/target endpoint mismatch, missing binding, dry-run helpers) and OKV-002 (date precision: YEAR/YEAR_MONTH/FULL_DATE/TIMESTAMP detection, coarsest-wins, event_date as string passes, event_date as timestamp triggers PARTIAL_DATE_INCOMPATIBLE) and post-deploy structural read-back (zero entity/relationship type fails, zero-contextualization fails). All 70 tests fail with ImportError (module absent) — this is the expected pre-implementation gap state. Decision note written to `.squad/decisions/inbox/hockney-ontology-integrity-tests.md`. No final approval yet; McManus must implement the module.

- **2026-07-23 (Final Re-Review — APPROVED):** Keyser's revision fixes all four defects. Code probes confirmed:
  D1: `validate_identity()` now wired into `compiler._validate()` step 7 — OntologyCompiler raises OntologyCompilerError on cross-table domain mismatch; valid FK alias (source_entity_id → entity_id) still passes.
  D2: OKV-002 now fires without datePrecision annotation (name-heuristic via identity_validation); true non-date timestamps unaffected.
  D3: `_validate_parquet_date_precision` now reports `rejected_count` + `affected_entity_names` count in error messages.
  D4: Broad `except Exception` replaced with `except ImportError` + hard `sys.exit(1)` when `total_nodes/total_edges < 0`.
  Full suite: 2264 passed, 0 failed. Targeted suite: 263 passed. APPROVED.
  1. **D1 (Critical):** `identity_validation.validate_identity()` never called in the pipeline — compiler.py `validate_relationship_keys()` is SILENT for cross-table domain mismatches (probe confirmed: 0 errors for DocumentChunk/chunk_id vs source_entity_id). The identity_validation module is dead code relative to the enforcement chain.
  2. **D2 (Critical):** `validate_date_types()` in compiler.py is SILENT unless model has explicit `datePrecision` annotation (probe confirmed: 0 errors for `event_date: timestamp` without annotation). OKV-002 in identity_validation.py correctly fires on name heuristic but is not wired in.
  3. **D3 (Significant):** `_validate_parquet_date_precision` reports sample values `[:3]` only — NOT rejected-value counts or affected entity counts as required by acceptance contract.
  4. **D4 (Moderate):** Broad `except Exception` at deploy_cmd.py L1228 silently skips zero-edge validation when `read_graph_counts` raises — prohibited by contract ("no success-shaped fallbacks or silent skips").
  REJECTED. Revision assigned to **Keyser** (McManus locked out per reviewer-protocol lockout rules).
