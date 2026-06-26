# Fenster — History

## Core Context

- **Project:** A Python CLI tool that builds and deploys knowledge graphs and Fabric ontologies from documents/CSV using OpenAI enrichment and canonical Parquet.
- **Role:** Data Engineer
- **Joined:** 2026-06-24T17:38:25.159Z

## Summary (Session: 2026-06-24 → 2026-06-25)

Completed 6 sprints of implementation (Sprint 1 baseline + Sprints 3–6):
- **Sprint 3:** Azure AI Search schema REST sanitization (prioritizedFields, vectorizers fixed; 789 tests)
- **Sprint 4:** Live deployment wiring — OneLake Delta writer + AI Search batch API (779 tests; 34 new tests)
- **Sprint 5:** Lakehouse lean projection — graph/ontology only, text→AI Search separation (832 tests; 30 new tests)
- **Sprint 6:** deploy-ontology live wiring — Fabric items REST API (845 tests; 14 new tests)

**Key patterns mastered:** Pre-Arrow validation, deterministic SHA-256 IDs, defensive column select, deltalake/requests patching, token_provider injection, concurrent-agent regression detection.

**Total test growth:** 682 (baseline) → 845 (after all sprints). All tests green. No failures.

## Detailed Session Artifacts

**Search Schema Sanitization** — Fixed REST API 2024-07-01 compliance: renamed prioritizedContentFields, removed incomplete vectorizers entry. Deployer defensively sanitizes even legacy schema files.

**Live Deploy Wiring** — Replaced mock stubs:
- `onelake_writer.py`: uses deltalake 1.6 with fabric-enabled abfss paths, Bearer token auth
- `search_deployer.py`: uses REST API 2024-07-01, batch upload (1k docs), vector search injection safety net
- Both default to live (--no-mock); --mock for testing only

**Lakehouse Lean Projection** — Implemented graph/ontology-only Lakehouse:
- `LAKEHOUSE_TABLE_PROJECTION` dict (single source of truth): 7 tables, no chunks
- document_elements kept lean (12 cols): structural IDs only, dropped content/content_html/row_index/col_index
- Defensive column select: silently ignores projection columns absent from parquet

**Ontology Item Creation** — Fabric REST items API (previously unimplemented):
- POST /workspaces/{ws}/items (Ontology type) — idempotent GET-first, reuse or create
- Handles 201 sync + 202 LRO; returns item_id or lro:{location} placeholder
- Definition API limitation always noted (updateDefinition wiring is separate task for coordinator/mcmanus)

## Key Learnings

1. **Concurrent agents can corrupt shared files** — Another agent's insertion deleted a function def header in enrich_cmd.py. Always check `git diff` on unexpected import failures.
2. **__pycache__ masks SyntaxErrors** — Cached .pyc files hide errors until fresh import or cache clear.
3. **Patch targets must be canonical imports** — Patching `requests.get` works because helper imports lazily; patching local name fails.
4. **Defensive select for projections** — `[c for c in keep_cols if c in arrow_table.schema.names]` prevents crash when projection lists optional future columns.
5. **Token provider injection** — Enables clean unit testing without network/credential setup and consistent exit code 6 on auth failure across all deploy modules.

For full context and code diffs, see session decisions merged into `.squad/decisions.md` (Fenster: Lakehouse Lean Projection, McManus: Real Fabric Ontology Format).

