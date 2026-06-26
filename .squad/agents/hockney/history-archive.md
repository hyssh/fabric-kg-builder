# Hockney — History Archive

## Core Context

- **Project:** Python CLI tool (knowledge graph + Fabric ontology builder)
- **Role:** Test Engineer
- **Joined:** 2026-06-24T17:38:25.166Z

---

## Key Learnings (Summarized)

### Sprint 1 CI Scaffold (2026-06-24)
- pytest-cov requires `pip install -e .[dev]` before running (document in CONTRIBUTING)
- `--cov-fail-under` belongs in CI workflow, NOT in `pyproject.toml` addopts (kills iteration)
- Mock factories (`make_foundry_client()`, etc.) > just fixtures for reusability
- Parametrize `--help` tests per-command for pinpoint regression detection
- Coverage 31% on scaffold is expected (0% modules driven by other agents)

### Test Tier Strategy (2026-06-24)
- `addopts = "-m 'not slow and not integration'"` in pyproject.toml → fast by default
- Dual-mark real-PDF tests with `@pytest.mark.integration` AND `@pytest.mark.slow` (complementary)
- Golden fixture (~2s e2e) covers compile-data + data-gates without large PDFs
- CI merge gate excludes `slow`/`integration`; integration job is separate `workflow_dispatch`
- Use `pytest.skip()` in test body (not skipif decorator) for portability
- **Baseline:** 684 passed, 4 deselected, ~19s

### SPEC-005 Validation & Test Plan (2026-06-24)
- **18 bullets → VAL-001 through VAL-022** (VAL-022 = env drift warn only)
- **3-table trace** (entity → relationship → evidence → visual_region → visual_asset) = critical integration test
- Mocking discipline: patch at constructor level (not function level) to avoid real calls
- `ids.lock.json` validator must check duplicate numeric values across BOTH entityTypes + relationshipTypes
- Placeholder generation tied to source-type detection (not static list)

### API & Search Gate Patterns (2026-06-24)
- `search.in(entity_ids, 'id1,id2', ',')` is only safe ID-list filter (not `any(id eq ...)`)
- Aliases → keyword `search` param; IDs → `filter` with `search.in()`
- `vectorFilterMode: preFilter` mandatory for entity-scoped retrieval
- Provenance selects: `source_path`, `canonical_key`, `entity_aliases`, `graph_path` required
- BRG gates belong in SPEC-005 catalog (not just SPEC-003) for traceability

### Implementation Patterns (2026-06-24)
- Two Violation types coexist: `data_gates.Violation` (D-XX internal) + `ValidationViolation` (SPEC-005 IDs)
- `chat_deployment` under `enrichment`, not `foundry`; VAL-027 must check both sections
- `validate_all(skip_env_check=True)` for unit/integration tests (decouples structural from credential checks)
- Unicode breaks on Windows cp1252: use ASCII (`FAIL`, `WARN`, `PASS:`) in CLI output
- E2E trace tests fastest as pure-dict + JSON fixtures (zero Parquet I/O)
- VAL-023 structural check: `frozenset(entity_fields) & set(doc.keys())` catches leakage

### Command & Security Patterns (2026-06-24)
- CLI command rename touches: acceptance matrix, smoke tests, integration file names, CI examples, decisions log
- Domain-intake security: sentinel-value inject + scan `call_args_list` for wrong roles
- Document Intelligence + vision LLM: separate mocks, assert polygon_json stable across LLM pass
- VAL-026 secret scanner: regex for base64-like (40+ chars); acceptable false positive risk
- Bidirectional linkage tests (entity→chunks, chunk→entities) > e2e agent loop
- Patch `fabric_kg_builder.enrichment.foundry_client` (module singleton) > constructor path

---

## Sprint Deliverables

1. ✅ CI scaffold with pytest + coverage
2. ✅ Test tiers (fast/integration/smoke)
3. ✅ SPEC-005 validation gates (VAL-001..022)
4. ✅ Implementation sprint 1 + 2 (validation gates, validate cmd, e2e trace)
5. ✅ 745 unit tests passing (47 new table tests + 6 enrich tests + 10 golden tests)

---

## Next Steps for Hockney

- Monitor test tier adoption in daily dev
- Track integration job success rate (workflow_dispatch)
- Extend golden fixture coverage as schema evolves

