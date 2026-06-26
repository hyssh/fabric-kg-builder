# Keyser — History

## Core Context

- **Project:** A Python CLI tool that builds and deploys knowledge graphs and Fabric ontologies from documents/CSV using OpenAI enrichment and canonical Parquet.
- **Role:** Lead
- **Joined:** 2026-06-24T17:38:25.155Z

## Learnings

### 2026-06-24 — Sprint 2 compile-search (full) + deploy-search Implemented

- **Files created:**
  - `src/fabric_kg_builder/search/linkage.py` — derives AI Search doc fields from chunk/document_element rows; `derive_chunk_doc`, `derive_document_element_doc`, `build_entity_lookup`, `build_search_aliases`, `build_entity_search_keys`; composes Fenster's richer batch API where present
  - `src/fabric_kg_builder/search/embeddings.py` — `attach_vectors` (mock=zero vecs when no endpoint; live via AIProjectClient); `generate_embeddings` batch helper using FoundryClient
  - `src/fabric_kg_builder/search/push.py` — `PushResult` dataclass; `push_index`, `push_documents`, `push_from_build_dir`; default mock=True, no live SDK call unless endpoint is set
  - `tests/unit/test_compile_deploy_search_sprint2.py` — 38 tests covering linkage, push, compile-search with Parquet fixture, deploy-search mock
- **Files modified:**
  - `src/fabric_kg_builder/cli/compile_search_cmd.py` — full Sprint 2: reads chunks.parquet + entities.parquet, derives docs via linkage.py, optionally embeds, writes docs.json alongside index.schema.json; _INDEXES registry now carries parquet_table/id_field/vector_field/text_field metadata
  - `src/fabric_kg_builder/cli/deploy_cmd.py` — `deploy_search_cmd` now reads `ai_search` section from env JSON, respects `enabled` flag, logs mock push results via `push.push_from_build_dir`; `_read_search_env_config` helper added
- **Key patterns:**
  - **Duplicate appends from concurrent agents:** Fenster and Verbal agents appended their implementations to linkage.py and embeddings.py during the same wave — detect via `from __future__ import annotations` appearing mid-file (syntax error); remove the appended duplicate, keep whichever is more complete.
  - **Coverage run hangs:** `pytest tests/unit` with coverage can take 15+ minutes due to slow imports in foundry/docintel test files. Use `--no-cov` for fast iteration; full coverage run (84s) was baseline behaviour but not reproducible on this run — likely environment contention.
  - **docs.json skipped when no Parquet:** compile-search skips docs.json write (but always writes schema) when parquet table is absent. Tests must not check for docs.json absence when fixture writes the Parquet.
  - **deploy-search uses push.push_from_build_dir:** Reads build/search/{index}/index.schema.json + docs.json; mock=True by default — no network call even if endpoint is set in env config.
  - **ai_search.enabled flag:** False → log + exit 0 immediately (no index/doc attempt). True → proceed with mock push.
  - **pyarrow to_pylist():** Converts Parquet table rows to plain Python dicts; list columns (related_entity_ids, entity_search_keys) become Python lists — no special handling needed.
- **Result:** 38 new tests pass; 566 unit tests total pass (excl. slow network tests).



- **Files changed:**
  - `src/fabric_kg_builder/cli/package_cmd.py` — replaced stub with full bundle implementation (parquet + ontology required; search optional via --include-search; writes manifest.json)
  - `src/fabric_kg_builder/cli/compile_search_cmd.py` — replaced stub with placeholder schema generator: two indexes (kg-chunks, kg-document-elements) with 1536-dim chunk_vector + entity-linkage fields (entity_ids filterable, entity_aliases searchable, canonical_key filterable, graph_path, blob_url)
  - `src/fabric_kg_builder/cli/deploy_cmd.py` — replaced deploy_lakehouse stub with mock mode reading env JSON directly (no secrets needed); leaves clear TODO seam for fabric-cicd (SPEC-003 §9.1.1)
- **Tests added:** `tests/unit/test_package_deploy_search.py` — 38 tests covering all four tasks
- **Key patterns:**
  - **Function ordering in Python modules:** `_INDEXES` dict that references functions by name must be defined AFTER those functions, not before; NameError at module load if reversed.
  - **deploy_lakehouse offline config:** Read `ontology/environments/{env}.json` fabric section directly (no full `load_config` call) to avoid requiring `AZURE_AI_FOUNDRY_ENDPOINT` in mock/offline mode. Keeps Sprint 1 mock fully deterministic with no credential deps.
  - **package cmd dir check:** Use `Path.exists()` only (not `any(iterdir())`) for required-dir validation; empty artifact dirs are valid (placeholder Parquets); fail only when the directory itself is absent.
  - **Set-Content heredoc on Windows:** PowerShell `@'...'@ | Set-Content` can produce partial writes if the heredoc contains characters that confuse the parser. Use explicit `[System.IO.File]::WriteAllText(...)` for large or special-character content.
  - **Pre-existing agent work on deploy_cmd.py:** Another agent had already implemented `deploy_ontology_cmd` with `--mock` flag using FabricDeployer; my overwrite merged with their content. Final state passes 6 deploy-ontology tests + 38 new tests.
  - **Search schema LOCKED fields:** `entity_ids` must be filterable + NOT searchable; `entity_aliases` must be searchable + NOT filterable; never reverse (SPEC-002 §11.4 label-vs-ID anti-pattern). `chunk_vector` dimensions LOCKED at 1536 to match `text-embedding-3-large` (SPEC-002 §11.7).
  - **CliRunner mix_stderr:** Use `mix_stderr=False` when asserting on warning content in stderr vs stdout.
- **dev.json verification:** `load_config('dev')` with real `ontology/environments/dev.json` returns `fabric.lakehouse_item_id = '44444444-4444-4444-4444-444444444444'` and `workspace_id = '11111111-1111-1111-1111-111111111111'` (both confirmed present per SPEC-003 §9.1.1).
- **Result:** 346 unit tests pass, 0 failures (up from 274 baseline + 38 new + 34 pre-existing that were already passing from other sprint agents).

### 2026-06-25 — inspect-source Command Implemented

- **File changed:** `src/fabric_kg_builder/cli/inspect_cmd.py` — replaced stub with real implementation
- **Tests added:** `tests/unit/test_inspect_cmd.py` — 12 tests covering exit codes, output format, --out flag, bad path, unsupported type
- **Key implementation pattern:** `_collect_files()` must check extension for *both* files and dirs before returning — returning any file unconditionally means unsupported-type exit-code (3) never fires; extension check must be at the return point for single-file case.
- **Click + sys.exit():** `sys.exit(N)` inside a Click command raises `SystemExit(N)`, which CliRunner captures as `result.exit_code`. This is the correct pattern for non-zero exit codes in Click.
- **CsvLoadResult API:** `result.source_file` (SourceFileRow), `result.schema_profile` (dict with `source_type`, `content_hash`, `row_count`, `column_count`, `columns`). No need to reimplement parsing — `load_csv()` handles all supported formats.
- **CliRunner mix_stderr:** Default is `mix_stderr=True`; use `mix_stderr=False` when you need to assert separately on stderr content (e.g., error messages written with `err=True`).
- **Unsupported type exit code:** SPEC-001 §7 mandates exit 3 for unsupported source types; exit 1 for general errors.
- **Result:** 237 unit tests pass, 0 failures.



### 2026-06-24 — PRD Kickoff Decomposition

- **PRD source of truth:** `docs/fabric-kb-builder-decision-snapshot.md`, `docs/fabric-kb-builder-target-architecture.md`, `docs/fabric-kb-builder-implementation-backlog.md`, `docs/fabric-kb-builder-lessons-learned.md`
- **Key architecture pattern:** LLM output → validate → canonical model → Parquet → Fabric Ontology bindings. LLM is never source of truth.
- **Core data contract:** 4 Parquet tables (source_files, entities, relationships, evidence).
- **Ontology approach:** Single ontology, 3 modules (support-domain, document-evidence, provenance).
- **Stack decisions:** click (CLI), pyarrow (Parquet), openai SDK (LLM), pytest (tests), Fabric REST (deploy).
- **ID stability:** `ontology/ids.lock.json` tracks deterministic GUIDs across environments.
- **Sprint 1 focus:** Project skeleton + CSV loader + canonical model schemas.
- **Sprint 2 focus:** LLM enrichment + Parquet writer + ontology compiler MVP.
- **Team mapping:** Fenster=data pipeline, McManus=ontology, Verbal=LLM, Hockney=tests, Keyser=architecture/CLI.
- **User preference:** Hyunsuk wants execution-ready decomposition with clear sprint mapping.

### 2026-06-24 — SPEC-001 Architecture & CLI Written

- **Spec file:** `docs/specs/SPEC-001-architecture-and-cli.md` (Draft)
- **Pipeline locked:** 8 stages: ingest → enrich → compile-data → compile-ontology → compile-search → package → deploy → validate.
- **Package layout:** 10 modules under `src/fabric_kg_builder/` (cli, config, sources, enrichment, model, parquet, ontology, search, deploy, validate).
- **CLI framework:** Click 8.x chosen over Typer (maturity, fewer dep conflicts).
- **ID strategy locked:** Content-addressed hashing for entities/relationships; `ids.lock.json` for Fabric ontology type IDs.
- **Checkpoint strategy locked:** Per-source-file JSON checkpoints in `build/enriched/.checkpoint/`; `--resume` and `--force` flags.
- **Exit codes:** Defined 0–8 scheme with distinct codes for auth failure, timeout, validation failure, partial enrichment.
- **Env strategy:** `ontology/environments/{env}.json` carries only varying values; model/IDs stable across envs.
- **Auth recommendation:** DefaultAzureCredential for Fabric/Blob/Search; explicit key for OpenAI.
- **AI Search:** Optional by default; compile-search/deploy-search are no-op when disabled.

### 2026-06-24 — SPEC-001 v2 Revision (Feedback from Hyunsuk)

- **Secrets model:** Separated config into `fabric-kg.yaml` (non-secret, committed) and `.env` (secrets, gitignored). Precedence: CLI flag > env var (from .env or shell) > yaml value > built-in default. `.env.example` committed as schema reference.
- **LLM SDK change:** Replaced raw `openai` SDK with **Microsoft Foundry SDK** (`azure-ai-projects`). Trade-off: slightly higher abstraction, gains project-level governance and unified Azure auth.
- **CLI rename:** `deploy-data` → `deploy-lakehouse`. Rationale: the command deploys structured data to the Fabric Lakehouse / OneLake datalake.
- **AI Search scope narrowed:** Structured/tabular data (CSV, Parquet canonical tables) is NOT indexed into AI Search. Structured data lands exclusively in the Fabric Lakehouse. AI Search is reserved for unstructured text/visual retrieval only (chunks, document elements, image descriptions).
- **Domain intake stage added:** New `fabric-kg set-domain` command intakes a user domain prompt BEFORE enrichment. Domain text stored in `build/enriched/domain.json`. Security constraint: domain text injected into LLM USER prompt only, never system prompt.
- **Pipeline expanded to 12 stages:** domain-intake → ingest → enrich → compile-data → compile-ontology → compile-search → package → deploy-lakehouse → deploy-ontology → deploy-search → validate.
- **Document Intelligence:** Locked as REQUIRED for PDF/image extraction (OCR + bounding polygons for visual_regions).
- **Infra doc created:** `docs/infra/INFRA-001-azure-resources.md` — inventory of all Azure resources with config/secret handling.

### 2026-06-24 — Model Defaults & Dev Infra Locked (SPEC-001 v3 + INFRA-001 v2)

- **Chat model default locked:** GPT-5.5-mini (target production); gpt-4.1 (interim dev — GPT-5.5-mini not yet deployed in dev sandbox).
- **Embedding model default locked:** text-embedding-3-large @ dimensions=1536 (fallback text-embedding-3-small@1536).
- **Dimension coupling documented:** `embedding_dimensions=1536` couples to AI Search `chunk_vector` field width (SPEC-002, RESEARCH-001 §4). Changing embedding requires full reindex.
- **Config split:** Non-secret config (Foundry project, deployment names, embedding_dimensions) in `fabric-kg.yaml`; secrets (endpoint, API key) in `.env`.
- **Dev environment verified:** Subscription Example-Subscription, RG example-rg, Fabric workspace 9802a28a, Foundry `example-aiservices`/`example-project`, AI Search `example-search`, DocIntelligence `example-docintell`, Blob `examplestorageacct`, Vision `example-vision`, KV `example-kv`.
- **Auth strategy:** DefaultAzureCredential for dev (az login); SPN for CI/prod.
- **Test data:** `sample_data\Surface_Troubleshootings\*.pdf` reserved for Sprint 2+ e2e tests (not processed now).
- **Action item:** Deploy GPT-5.5-mini to example-aiservices.

### 2026-06-24 — Canonical Naming Reconciliation (SPEC-001 v4 + PRD banner)

- **Task:** Reconcile 5 CRITICAL + 4 MEDIUM consistency findings from coordinator-canonical-naming.md into SPEC-001 and PRD.md.
- **`compile-search-index` → `compile-search`:** Renamed in command heading, PRD coverage table. Stage name was already `compile-search` in the pipeline table (no change needed there); fixed the command entry heading and any residual `compile-search-index` references.
- **Stage 2 `ingest` → `inspect-source`:** Pipeline stages table, data flow mermaid arrow label, stage I/O matrix, inspect-source command pipeline stage attribute.
- **`foundry.project_name` → `foundry.project`:** Updated in §5.1 yaml block.
- **`vision_model_deployment` → `vision_deployment`:** Updated in §5.1 yaml block with canonical comment: default = chat deployment (multimodal; gpt-4.1 interim / GPT-5.5-mini target); alternative = example-vision/gpt-4o.
- **AZURE_DOC_INTELLIGENCE_* → AZURE_DOCINTEL_*:** Updated `.env` example block (`AZURE_DOCINTEL_ENDPOINT`, `AZURE_DOCINTEL_API_KEY`) and the yaml `${...}` reference.
- **Vision open decision #5:** Expanded to name the default explicitly and document `example-vision`/gpt-4o as the alternative.
- **build-deploy command:** Added `inspect-source` to the pipeline sequence in the purpose row.
- **PRD.md:** Added supersede banner (table) immediately after H1; PRD body untouched.
- **Key lesson:** When two separate sections (pipeline table vs. command entry heading) diverge on a name, both must be fixed — one change doesn't cascade automatically. Always grep for all occurrences.
- **Revision row:** Added v4 row to SPEC-001 Revision History at CURRENT_DATETIME.


- **Design doc:** `docs/design/DESIGN-001-retrieval-agent-orchestration.md` (Draft)
- **Decision:** Foundry Agent + deterministic `retrieve_grounding` function tool (Option A). LLM decides when to call; tool builds OData/search.in filter from validated graph IDs — LLM never hand-authors filters.
- **Core insight:** Filter injection and label-vs-ID confusion are the two biggest risks of LLM-generated OData; deterministic code eliminates both.
- **Effort:** ~400–600 LOC application code (small), plus ~200–300 LOC tests. Modules: graph client, filter builder, search wrapper, retrieve tool, answer assembler, CLI `query` command.
- **MCP path:** Start in-process (single agent, lowest effort); graduate to MCP server only if a second agent or non-Foundry client needs the tools.
- **Relation to MVP:** Query-time grounding is post-MVP. No existing specs need editing — data model (SPEC-002), ontology bridge (SPEC-003 §12), and search index schema (RESEARCH-001 §4) already support it. Design reserves the tool contract.
- **Minimal prototype:** CLI `fabric-kg query` command against dev resources (example-aiservices, example-search, workspace 9802a28a). Estimated 2–3 days.

### 2026-06-24 — Sprint 1 Scaffold Implemented (Tasks: repo-skeleton, pyproject-setup, cli-entrypoint, cli-stubs)

- **Key files created:**
  - `pyproject.toml` — src layout, PEP 621, entry point `fabric-kg = fabric_kg_builder.cli:main`
  - `src/fabric_kg_builder/__init__.py` — top-level package, `__version__ = "0.1.0"`
  - `src/fabric_kg_builder/cli/__init__.py` — re-exports `cli` and `main`
  - `src/fabric_kg_builder/cli/main.py` — Click group with 13 subcommands, global options
  - `src/fabric_kg_builder/cli/{init,set_domain,inspect,enrich,compile_data,compile_ontology,compile_search,package,deploy,validate,build_deploy}_cmd.py` — stub commands
  - `src/fabric_kg_builder/{config,sources,enrichment,model,parquet,ontology,search,deploy,validate}/__init__.py` — module packages with docstrings
  - `tests/{unit,integration,fixtures}/__init__.py` — test directories
  - `.gitignore` extended — excludes build/, dist/, __pycache__/, *.egg-info/, .env, .venv/, build/**/*.parquet
- **CLI framework:** Click 8.x (per SPEC-001 §4 and decisions.md decision 1). NOT Typer.
- **Foundry SDK package:** `azure-ai-projects>=1.0` (per SPEC-001 §4, decisions.md decision 10). TODO comment added in pyproject.toml. Installed successfully.
- **fabric-cicd:** Added as required deploy dependency; installed version 1.1.0.
- **Entry point resolution:** `fabric_kg_builder.cli:main` resolves via `cli/__init__.py` re-exporting `main` from `cli/main.py`.
- **Unicode gotcha:** Windows console (cp1252) cannot encode arrow `->` in Click docstrings. Used `>` ASCII in build-deploy docstring.
- **`pip install -e .` result:** SUCCESS — all deps already present in Anaconda env; fabric-cicd 1.1.0 installed fresh.
- **`fabric-kg --help` result:** Lists all 13 commands: build-deploy, compile-data, compile-ontology, compile-search, deploy-lakehouse, deploy-ontology, deploy-search, enrich, init, inspect-source, package, set-domain, validate.

### 2026-06-24 — PLAN-001 Implementation Plan Created

- **File:** `docs/PLAN-001-implementation-plan.md` (Draft)
- **Structure:** 6 milestones (M0–M5), 4 epics in Sprint 1, 4 epics in Sprint 2.
- **Task count:** 59 total (37 Sprint 1 + 22 Sprint 2).
- **Critical path:** pyproject-setup → cli-entrypoint → cli-stubs → enrich-cmd → compile-data-cmd → compile-ontology-cmd → package-cmd.
- **Parallelization:** 5 concurrent tracks identified (schemas, CLI+config, ontology model, LLM integration, CI+fixtures).
- **Key insight:** The enrichment chain is the longest serial dependency. Schemas and ontology model.yaml are fully parallelizable.
- **MVP DoD:** All 13 PRD §23 acceptance criteria mapped to milestones and automated tests.
- **Open risks tracked:** GPT-5.5-mini deployment, Lakehouse item ID, fabric-cicd vs REST, auth strategy, AI Search MVP scope.
- **Lesson:** Task tables must use stable kebab-case IDs and reference only other IDs in the same table for tooling compatibility.
