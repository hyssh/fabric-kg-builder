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

**Sprint — Agent Capability (#12 / #13 / #14) — 2026-07-23:**

Implemented issues #12 (capability-aware descriptions), #13 (few-shot example gating on observed row counts), and #14 (property child preservation) per Keyser's ADR. All changes are in `src/fabric_kg_builder/**` production only; Hockney owns tests independently.

**Changes delivered:**

- `semantic/schemas.py`: Added `observed_relationship_rows: dict[str,int]` to `QueryReadiness`. Added `CompetencyExampleStatus` Literal (`"pass"`, `"published"`, `"blocked"`, `"skipped"`, `"omitted"`), `RelationshipAvailabilityClass` Literal, and `CompetencyExampleReceipt` model. Added `required_property_count`, `compiled_property_count`, `draft_property_count`, `published_property_count`, `compiled_property_selection_hash`, `published_property_selection_hash`, `global_instruction_chars`, `instruction_chars: dict[str,int]`, `description_chars: dict[str,int]` to `AgentPublicationReceipt`. All new fields default-safe for Pydantic `extra="forbid"` backward compat.

- `semantic/__init__.py`: Exported `CompetencyExampleReceipt`, `CompetencyExampleStatus`, `RelationshipAvailabilityClass`.

- `knowledge/validation.py`: Added `DataAgentPropertyOmitted` (required_count/actual_count optional, defaults 0), `DataAgentRequiredExampleEmpty` (competency_id, relationship_id, observed_rows, expected_minimum, stage, remediation), `DataAgentUnavailableRelationshipClaimed` (relationship_id, availability_class optional, context optional). Added `classify_relationship_availability()` pure helper (four-state: schema_supported_unobserved / optional_absent / required_absent / executable_nonempty). Added `gate_competency_examples()`: accepts list[DataAvailability] OR dict[str, DataAvailability]; reads case requirement from `routes.direct_graph` or `expected.relationship_types` (both formats); raises on required-absent; silently omits optional-absent.

- `semantic/persisted_projection.py`: `validate_graph_query_readiness` now populates `observed_relationship_rows` filtered to relationship semantic IDs.

- `knowledge/agent_validation.py`: Fixed `build_public_graph_source_projection` — removed `replace(element, children=None)` stripping bug (#14). Updated `build_agent_publication_receipt` with three-way property count check, `compiled_property_selection_hash`/`published_property_selection_hash`, and new receipt fields. Updated `deploy_and_validate_data_agent` to accept char count params.

- `knowledge/data_agent.py`: `graph_few_shots_from_competency_contract` now retains receipts from `gate_competency_examples`, builds `published_case_ids` set, and skips non-published cases. Optional-absent cases are silently omitted; required-absent raises before mutation.

- `semantic/instructions.py`: `build_graph_source_description`, `build_graph_source_instructions`, `build_ontology_source_description` all accept optional `availability` (list or dict). `_normalize_availability()` handles both. Description omits unavailable relationship labels; mentions "some paths have no verified published data" without naming them.

- `cli/deploy_cmd.py` + `cli/build_deploy_cmd.py`: Added availability dict from materialization plan, `DataAgentRequiredExampleEmpty` import, capability reporting block, dict char counts to `deploy_and_validate_data_agent`.

**Key learnings:**

- `gate_competency_examples` must handle TWO case structures: compiled contract format (`routes.direct_graph` + `probes.direct_graph.required_relationship_ids`) and runtime format (`expected.relationship_types` via `_required_relationships`). Merge both.
- After gating, receipts must be *retained* and used to filter the case loop. Discarding them and iterating all cases lets optional-absent cases leak through (#13 root cause).
- `DataAgentPropertyOmitted`: `required_count`/`actual_count` should default to 0 so tests can construct minimal instances.
- `DataAgentUnavailableRelationshipClaimed`: `availability_class` and `context` should be optional kwargs.
- `CompetencyExampleStatus` needs "published" and "omitted" as valid members — tests use both.
- Descriptions must OMIT unavailable relationship labels (not just qualify them with "UNAVAILABLE") — test uses `upper().replace("UNAVAILABLE","").replace("NO VERIFIED","")` so any bare label mention fails.
- Three-way property invariant holds because the raise happens before receipt construction, keeping `property_child_coverage == 1.0` validator clean.

**Result:** 184 targeted tests passing (was 128 baseline); 2391 full-suite passing, 0 failures.

**Issue #6 — Deployment Manifest (Single Fabric Item Naming Authority) — 2026-07-23:**

Implemented the deployment manifest as the single authority for all Fabric item display names, prefixes, configured IDs, target workspace, and dependencies per Keyser's ADR.

**Files created:**
- `src/fabric_kg_builder/deploy/manifest.py`: `DeploymentManifest` Pydantic v2 schema + `load_deployment_manifest()` loader with `${ENV_VAR}` interpolation (same pattern as `infra/manifest.py`). Post-validation enforces non-empty `workspace` for file-loaded manifests. `ManifestItemSpec` model-validator enforces `display_name` required when `configured_id` is set.
- `src/fabric_kg_builder/deploy/name_authority.py`: `ResolvedName` dataclass, `NameAuthorityConflict` exception (structured with `NAME_AUTHORITY_CONFLICT` error code), `resolve_item_name()`, `validate_readback_name()`, `render_name_resolution()`, `manifest_from_env_config()`. Supports both CamelCase (`"Ontology"`) and snake_case (`"ontology"`) item type keys for CLI and test compatibility.
- `deployment.yaml.example`: Fully annotated sample manifest with migration guidance.

**Files modified:**
- `cli/deploy_cmd.py`: `--manifest` added to all 6 commands (lakehouse, ontology, graph, data-agent, search, serving). `deploy-ontology` also gets `--display-name` with early conflict detection. Helpers `_load_or_synthesize_manifest`, `_warn_manifest_vs_env` added.
- `cli/build_deploy_cmd.py`: `--manifest` (`deploy_manifest_path`) added; `--semantic-contract` made optional (required=False) and `--semantic-mappings`/`--semantic-vocabulary` have `exists=True` removed so dry-run works before infrastructure is provisioned. Name plan emitted early in dry-run before infra manifest load.
- `semantic/compiler.py`: Guarded `ontology_name if ontology_name is not None else contract_name`.
- `README.md`: Added `## Deployment Manifest — Fabric Item Naming Authority` section.

**Key learnings:**

1. **Variable name collision**: The parameter name `manifest_path` collided with the local variable `manifest_path = infra_root / "environments" / f"{env}.yaml"` in `build_deploy_cmd`. Renamed to `deploy_manifest_path`.

2. **Early vs deferred conflict detection**: `deploy-ontology --display-name <name>` conflict must be detected BEFORE `_read_fabric_env_config()` so the error is always emitted (even when env config file is missing). Pattern: eager conflict check early, full resolution after env config.

3. **`ManifestItemSpec` validator scope**: The `configured_id requires display_name` validator is correct for file-loaded manifests but must NOT fire for synthesized manifests. Fix: in `manifest_from_env_config`, only set `configured_id` when `display_name` is also non-empty.

4. **Click exists=True on defaults**: Click validates defaults through `exists=True` when Click 8.2+. Removing `exists=True` from `--semantic-mappings`, `--semantic-vocabulary`, `--semantic-contract` is required for `--dry-run` to work before infrastructure is provisioned.

5. **Item type duality**: Hockney's tests use snake_case (`"ontology"`, `"graph_model"`); CLI code uses CamelCase (`"Ontology"`, `"GraphModel"`). Both must be accepted by `_ITEM_TYPE_FIELD` dict.

6. **`manifest_from_env_config` accepts full or extracted env JSON**: Tests pass the full env dict with `{"fabric": {...}}` nesting. Added detection to handle both the full dict and the extracted fabric section.

7. **Test runner**: `uv run --extra dev pytest` required throughout; bare `python3` on this machine recurses.

**Result:** 2525 unit+contract tests passing, 0 failures.

Implemented end-to-end live Graph example validation + Data Agent semantic parity for deploy flows (`deploy-data-agent`, `build-deploy`):
- Normalize generated GQL to Fabric syntax (strip fences, single-quote literal normalization).
- Validate each candidate probe against persisted query schema (`validate_physical_query`) before execution.
- Execute candidates against deployed Graph; enforce required non-empty rows and evidence coverage.
- Publish only passed examples (max 7), block required failures, omit optional failures.
- Run post-publish Data Agent execution for published cases and compare semantic outcomes to direct Graph results.
- Persist per-example receipts in `AgentPublicationReceipt.competency_examples` with query hashes, row counts, coverage, result categories, and request IDs.

**Key learnings (Issue #11):**
- Fabric Graph success statuses are app-level prefixes (`"00".."03"`), not HTTP `"200"`; tests and gates must key off status-code prefixes.
- Max-example enforcement should happen after per-candidate validation/execution to keep “execute every candidate” semantics while still enforcing the ≤7 publication cap.
- This workstation’s `python3` points to a recursive shell wrapper; use `uv run --extra dev pytest ...` for reliable test execution.
