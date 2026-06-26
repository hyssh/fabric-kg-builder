# fabric-kg-builder

> Turn documents, images, and tabular data into a deployable Microsoft Fabric knowledge graph — enriched by Azure OpenAI, searched via Azure AI Search, governed by a Fabric Ontology, and deployed through **fabric-cicd**.

📄 **Project site:** [hyssh.github.io/fabric-kg-builder](https://hyssh.github.io/fabric-kg-builder/) · ✉️ Questions: [hyssh@microsoft.com](mailto:hyssh@microsoft.com)

---

## What It Does

`fabric-kg-builder` is a Python CLI that runs a structured pipeline from raw source files to a fully deployed Fabric knowledge graph:

1. **Ingest** PDFs, Word documents, HTML pages, and CSVs  
2. **Extract** text, tables (as structured cells and HTML), images, and figures — using Azure AI Document Intelligence (Layout model)  
3. **Upload** visual assets (images, figures) to Azure Blob Storage  
4. **Enrich** with LLM entity/relationship extraction via Microsoft Foundry (`azure-ai-projects`) / Azure OpenAI  
5. **Compile** enriched JSON into 8 canonical Parquet tables (entities, relationships, chunks, document elements, evidence, …)  
6. **Compile** a Fabric Ontology definition over those Parquet tables  
7. **Compile** Azure AI Search index schemas and document batches (vector + keyword retrieval)  
8. **Package** all build artifacts into a versioned `dist/` bundle  
9. **Deploy** Parquet tables → Fabric Lakehouse, Ontology definition → Fabric workspace, Search documents → Azure AI Search — all via **fabric-cicd**

The tool is a **reusable framework**, not a demo. Every domain (hardware support, legal docs, product manuals, etc.) is modelled via a domain brief, and the pipeline is repeatable across dev / test / prod environments.

---

## Features

- **End-to-end CLI** — single command (`build-deploy`) or fine-grained stage-by-stage control  
- **Document Intelligence** — tables extracted as HTML; figures as images stored in Blob  
- **LLM enrichment** — entity / relationship / evidence extraction via Microsoft Foundry SDK  
- **8-table canonical Parquet schema** — durable data contract; source-controlled and versionable  
- **Fabric Ontology** — generates Ontology definition parts deployable to any Fabric workspace  
- **Azure AI Search** — vector (text-embedding-3-large, 1536 dims) + keyword indexes for grounded retrieval  
- **fabric-cicd deployment** — Lakehouse, Ontology, and Search deployed deterministically  
- **Multi-environment** — `dev` / `test` / `prod` configs in `ontology/environments/`  
- **Resume-safe enrichment** — `--resume` skips already-processed files  
- **DefaultAzureCredential** auth — `az login` for dev; Service Principal for CI/prod  

---

## Architecture Overview

```
Source files (PDF / DOCX / HTML / CSV)
    │
    ▼  inspect-source
    │  Document Intelligence (Layout) — tables as HTML, figures as images → Blob Storage
    │
    ▼  enrich
    │  Microsoft Foundry / Azure OpenAI — entity, relationship, evidence extraction
    │  → enriched JSON (per-file, resume-safe)
    │
    ▼  compile-data
    │  → 8 canonical Parquet tables  (entities, relationships, chunks,
    │                                  document_elements, evidence, …)
    │
    ├─▶ compile-ontology → Fabric Ontology definition parts
    │
    ├─▶ compile-search   → AI Search index schemas + document batches
    │
    ▼  package  → dist/ bundle (manifest + all artifacts)
    │
    ├─▶ deploy-lakehouse  → Fabric Lakehouse (OneLake Tables) via fabric-cicd
    ├─▶ deploy-ontology   → Fabric workspace Ontology via fabric-cicd
    └─▶ deploy-search     → Azure AI Search indexes + documents
```

> Structured graph/ontology data lives in the Lakehouse. Document text and vector embeddings are searched via Azure AI Search — they are **not** stored in the Lakehouse.

---

## Prerequisites

| Requirement | Details |
|---|---|
| Python | ≥ 3.10 |
| Azure subscription | Required for all Azure services |
| Azure CLI (`az`) | `az login` for DefaultAzureCredential in dev |
| Azure AI Foundry project | Chat model (`gpt-5-4-mini`) + embedding model (`text-embedding-3-large`) |
| Azure AI Document Intelligence | Layout model — PDF/image table and figure extraction |
| Azure AI Search | Standard tier recommended; index prefix configured per-env |
| Azure Blob Storage | Container for visual assets (images, figures) |
| Microsoft Fabric workspace | Schema-enabled Lakehouse (`enableSchemas=true`) + Fabric Ontology |

> **Auth:** dev uses `az login` (DefaultAzureCredential). CI/prod uses a Service Principal — set `FABRIC_CLIENT_ID`, `FABRIC_CLIENT_SECRET`, `FABRIC_TENANT_ID` in `.env`.

---

## Installation

```bash
# Editable install (recommended for development)
pip install -e .

# With dev/test extras (pytest, coverage)
pip install -e .[dev]
```

Verify:

```bash
fabric-kg --version
fabric-kg --help
```

---

## Configuration

The tool uses three layers of configuration:

### 1. `.env` — secrets (never committed)

```bash
cp .env.example .env
# Edit .env and fill in your endpoint URLs and (optionally) API keys
```

Key variables (see `.env.example` for the full list):

| Variable | Purpose |
|---|---|
| `AZURE_AI_FOUNDRY_ENDPOINT` | Foundry project endpoint (`services.ai.azure.com/…`) |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint (`openai.azure.com`) |
| `AZURE_DOCINTEL_ENDPOINT` | Document Intelligence endpoint |
| `AZURE_SEARCH_ENDPOINT` | AI Search service endpoint |
| `AZURE_STORAGE_CONNECTION_STRING` | Blob Storage (visual assets) |
| `FABRIC_CLIENT_ID` / `_SECRET` / `_TENANT_ID` | Service Principal (CI/prod only) |

> With `az login` (dev), API keys may be omitted — DefaultAzureCredential uses your Azure AD session.

### 2. `fabric-kg.yaml` — non-secret config

Controls model deployments, embedding dimensions, blob container, and search index prefix. `${ENV_VAR}` references are interpolated from `.env` at runtime. **Secrets are never stored here.**

### 3. `ontology/environments/{env}.json` — per-environment resource IDs

Each file (`dev.json`, `test.json`, `prod.json`) contains workspace IDs, lakehouse IDs, OneLake paths, AI Search index names, Foundry project references, and Blob Storage account details for that environment. The **shape** of each file is:

```json
{
  "env": "dev",
  "auth_strategy": "DefaultAzureCredential",
  "azure":     { "subscription_id": "...", "resource_group": "..." },
  "fabric":    { "workspace_id": "...", "lakehouse_item_id": "...", "onelake_tables_path": "..." },
  "blob_storage": { "account_name": "...", "container": "...", "path_prefix": "dev/" },
  "ai_search": { "enabled": true, "endpoint": "...", "index_prefix": "kg-dev-" },
  "foundry":   { "endpoint": "...", "chat_deployment": "...", "embedding_deployment": "..." },
  "document_intelligence": { "endpoint": "..." }
}
```

> **Do not commit real resource IDs, tenant IDs, or secrets into these files.** The `dev.json` in the repo contains example/placeholder values for illustration only.

---

## Quickstart — End-to-End Example

The `sample_data\Surface_Troubleshootings` directory contains 22 Surface service-guide PDFs. This walkthrough runs the full pipeline against that dataset.

### Step 1 — Authenticate

```bash
az login
```

### Step 2 — Configure secrets

```bash
cp .env.example .env
# Open .env and fill in your Azure endpoint URLs
```

### Step 3 — Set the domain brief

```bash
fabric-kg set-domain \
  --prompt "Microsoft Surface hardware troubleshooting guides covering repair procedures, components, error codes, and replacement parts."
```

Writes `build\enriched\domain.json` (default `--out build\enriched`).  
Or supply a file: `--domain-file docs\surface_domain.txt`

### Step 4 — Enrich source documents

```bash
fabric-kg enrich \
  --input sample_data\Surface_Troubleshootings \
  --resume
```

Outputs to `build\enriched\` (default `--out build\enriched`).  
`domain.json` is picked up automatically from `build\enriched\domain.json`.  
`--resume` skips files already processed — safe to re-run after interruption.

### Step 5 — Compile canonical Parquet tables

```bash
fabric-kg compile-data
```

Reads `build\enriched\` → writes 8 Parquet tables to `build\parquet\` (default `--input build\enriched`, `--out build\parquet`).

### Step 6 — Compile Fabric Ontology

```bash
fabric-kg compile-ontology
```

Writes ontology definition parts to `build\ontology\` (default `--out build\ontology`).

### Step 7 — Compile AI Search schemas

```bash
fabric-kg compile-search
```

Reads `build\parquet\` → writes index schemas and document batches to `build\search\` (default `--input build\parquet`, `--out build\search`).

### Step 8 — Package build artifacts

```bash
fabric-kg package --include-search
```

Reads `build\` (parquet + ontology + search) → writes `dist\fabric-kg-package\` with a `manifest.json` (default `--build-dir build`, `--out dist`).

### Step 9 — Deploy to Fabric Lakehouse

```bash
fabric-kg deploy-lakehouse --env dev --dist dist
```

Reads Parquet tables from `dist\fabric-kg-package\parquet` (default `--dist dist`).

### Step 10 — Deploy AI Search indexes

```bash
fabric-kg deploy-search --env dev --dist build\search
```

Reads directly from the `build\search\` directory produced by `compile-search` (default `--dist build\search`).

### Step 11 — Deploy Fabric Ontology

```bash
fabric-kg deploy-ontology --env dev --no-mock
```

`deploy-ontology` defaults to `--mock` (safe dry-run); pass `--no-mock` for a live Fabric workspace deploy.

By default the ontology models all entities as a single generic `KGEntity` type (one box in the Fabric Ontology Explorer). For a **rich, multi-type graph** — one node type per real domain type (Device, DeviceModel, Component, Part, PartNumber, Procedure, Step, Tool, Symptom, Cause, Resolution, Section) plus typed relationships (`has_step`, `uses_tool`, `has_part`, `has_part_number`, `causes`, `resolved_by`, …) — use `--multitype`:

```bash
fabric-kg deploy-ontology --env dev --multitype --parquet-dir data\surface_kg\parquet --no-mock
```

This plans the types/relationships from your data, materializes one Lakehouse table per type (`entities_<type>`) and per relationship pair (`rel_<src>_<tgt>`), then pushes the ontology definition. Tune `--min-pair-count N` to control how many edges a `(source → target)` pair needs before it becomes a typed relationship (default 10). The Ontology Explorer is a *schema* view — it shows one box per entity **type**, with all instances bound behind it from the Lakehouse tables.

> **One-shot alternative:** `fabric-kg build-deploy --input sample_data\Surface_Troubleshootings --env dev` runs all stages in sequence (in development — see Notes).

---

### ⚠️ Custom output paths

If you override `--out` on any compile step, you **must** align the downstream commands manually — the defaults no longer apply.

**The two non-obvious rules:**

1. **`package --build-dir`** — `package` reads artifacts from `--build-dir` (default `build`), *not* from `--out`. If you compiled to a custom directory, you must pass `--build-dir <thatdir>` to `package`, otherwise it silently bundles stale or empty data from `build\`.

2. **`deploy-lakehouse --dist`** — `deploy-lakehouse` looks for Parquet under `<dist>\fabric-kg-package\parquet` (the packaged bundle). If the path is wrong it silently falls back to `build\parquet`. Always point `--dist` at the directory that *contains* the `fabric-kg-package\` subfolder.

**Example with `data\surface_kg` as a custom root:**

```bash
fabric-kg enrich          --input sample_data\Surface_Troubleshootings --out data\surface_kg\enriched --resume
fabric-kg compile-data    --input data\surface_kg\enriched --out data\surface_kg\parquet
fabric-kg compile-ontology --out data\surface_kg\ontology
fabric-kg compile-search  --input data\surface_kg\parquet --out data\surface_kg\search
fabric-kg package         --build-dir data\surface_kg --out data\surface_kg\dist --include-search
fabric-kg deploy-lakehouse --env dev --dist data\surface_kg\dist
fabric-kg deploy-search   --env dev --dist data\surface_kg\search
fabric-kg deploy-ontology --env dev --no-mock
```

Note `--build-dir data\surface_kg` (not `--build-dir data\surface_kg\parquet`) and `--dist data\surface_kg\dist` (the dir containing `fabric-kg-package\`, not `data\surface_kg` itself).

---

---

## Domain Template Playbook

### Concept — Domain-Fit Model

Graph retrieval quality is directly tied to how well the **ontology model matches your domain**. A generic one-size-fits-all graph (one `KGEntity` node type, unlabeled edges) retrieves poorly because queries have no typed path to follow. A **domain-fit model** defines:

- **Entity types** — the node types that represent real objects in your domain (e.g. `Device`, `Procedure`, `Symptom`).  Each type becomes a distinct box in the Fabric Ontology Explorer.
- **Typed relationships** — named, directed edges between entity types (e.g. `has_step`, `causes`, `resolved_by`).

> **Ontology Explorer is a TYPE/schema view.** It shows one box per entity *type*, not one box per instance.  All real instances (e.g. every Surface Pro model, every procedure) are bound behind their type from the Lakehouse tables.

Inspiration from the ecosystem: Microsoft **GraphRAG** uses `graphrag prompt-tune --domain ... --discover-entity-types` to adapt extraction to the user's domain and a data sample. **Neo4j LLM Graph Builder** lets users configure the node/relationship schema up-front for higher-quality extraction. The same principle drives this tool — specify your schema in `set-domain` before enriching.

---

### The Surface (Field-Service) Template

The `sample_data\Surface_Troubleshootings` corpus models a **hardware troubleshooting / field-service** domain.  Use this as a copyable starting point for any hardware support, repair manual, or field-service dataset.

#### Entity types (12)

| Entity Type  | What it represents |
|---|---|
| `Device`       | Product family (e.g. Surface Pro) |
| `DeviceModel`  | Specific SKU (e.g. "Surface Pro 10 for Business") |
| `Component`    | Major sub-assembly (e.g. Display Assembly) |
| `Part`         | Replaceable part (e.g. Back Cover) |
| `PartNumber`   | Manufacturer part number |
| `Procedure`    | Named repair or replacement procedure |
| `Step`         | Individual numbered step within a procedure |
| `Tool`         | Required tool (e.g. Torx T3 screwdriver) |
| `Symptom`      | Observed failure (e.g. "No display", "Battery swelling") |
| `Cause`        | Root cause of a symptom |
| `Resolution`   | Corrective action for a cause/symptom |
| `Section`      | Document section — groups steps or procedures |

#### Main relationships

| Relationship | Source → Target |
|---|---|
| `has_component`  | Device / DeviceModel → Component |
| `has_part`       | Component → Part |
| `has_part_number`| Part → PartNumber |
| `has_step`       | Procedure / Section → Step |
| `uses_tool`      | Procedure / Step → Tool |
| `causes`         | Cause → Symptom |
| `resolved_by`    | Symptom → Resolution |
| `addressed_by`   | Cause → Resolution |
| `applies_to`     | Procedure → DeviceModel |
| `compatible_with`| Part → DeviceModel |

---

### Step-by-Step Build (default paths)

> **One-command reproduction.** The entire sequence below is encoded in a
> runnable script — `scripts/reproduce-surface-kg.ps1` (PowerShell) and
> `scripts/reproduce-surface-kg.sh` (POSIX). Run it for build artifacts only, or
> with `-Deploy` / `--deploy` (after `az login`) for a full live rebuild. See
> [`scripts/README.md`](scripts/README.md). The manual steps below show what it does.

```bash
# 1. Set domain — declare industry + business domain, name entity types and
#    relationships, and pass sample questions (--industry and --business-domain
#    are REQUIRED; --questions-file is the biggest lever on ontology quality).
fabric-kg set-domain \
  --industry manufacturing --business-domain field-service \
  --questions-file data\surface_questions.txt \
  --prompt \
  "Field-service hardware troubleshooting for Microsoft Surface devices. \
Entity types: Device, DeviceModel, Component, Part, PartNumber, Procedure, \
Step, Tool, Symptom, Cause, Resolution. Key relationships: has_component, \
has_part, has_part_number, has_step, uses_tool, causes, resolved_by, \
addressed_by."

# data\surface_questions.txt (one question per line), e.g.:
#   What components does the Surface Pro 10 for Business have?
#   What steps are in the display replacement procedure?
#   What can cause battery expansion and how is it resolved?

# 2. Enrich (LLM extraction, per-document)
fabric-kg enrich --input sample_data\Surface_Troubleshootings --resume

# 3. Densify — RECOMMENDED: link DeviceModel hub edges, Cause/Symptom/Resolution
#    triples, AND Procedure→Step edges (by document reading order). Strictly
#    additive — only adds edges, never removes existing ones (enforced in step 4).
#    Toggle parts with --no-link-scr / --no-link-steps if needed.
fabric-kg densify --input build\enriched --out build\enriched_dense

# 4. Compile canonical Parquet tables (from densified output).
#    Runs the ADDITIVITY GUARD: compile fails (exit 5) if any entity or
#    relationship present in the input is missing from the output.
fabric-kg compile-data --input build\enriched_dense

# 5. Compile Fabric Ontology definition
fabric-kg compile-ontology

# 6. Compile AI Search schemas
fabric-kg compile-search

# 7. Package artifacts
fabric-kg package --include-search

# 8. Deploy Lakehouse tables
fabric-kg deploy-lakehouse --env dev --no-mock

# 9. Deploy Ontology — use --multitype for a rich typed graph
#    (202 async LRO — takes ~1-2 min to finish after command returns)
fabric-kg deploy-ontology --env dev --multitype --parquet-dir build\parquet --no-mock

# 10. Deploy AI Search
fabric-kg deploy-search --env dev --dist build\search
```

> **`--multitype`** materialises one Lakehouse table per entity type (`entities_Device`, `entities_Procedure`, …) and per relationship pair, then pushes a rich ontology definition.  The Fabric Ontology Explorer will show one distinct box per type.

---

### Sample Questions to Validate the Surface Graph

After deployment, test with a **Fabric Data Agent** connected to the ontology (see next section).  These questions worked in our testing against the 22-PDF Surface corpus:

| # | Sample question | Key types traversed |
|---|---|---|
| 1 | What components does the Surface Pro 10 for Business have? | DeviceModel → Component |
| 2 | List the parts of the Display Assembly. | Component → Part |
| 3 | What part number is the Surflink Screw? | Part → PartNumber |
| 4 | What steps are in the Audio Jack Replacement procedure? | Procedure → Step |
| 5 | What tools does the display replacement procedure need? | Procedure → uses_tool → Tool |
| 6 | What can cause battery expansion and how is it resolved? | Cause → Symptom → Resolution |
| 7 | What causes battery overheating? | Cause → Symptom |
| 8 | How is "no display" resolved? | Symptom → resolved_by → Resolution |

> **Tip:** Use `CONTAINS` (not exact match) in GQL queries — real DeviceModel names include SKU suffixes.  E.g. `CONTAINS(n.name, "Surface Pro 10")` matches `"Surface Pro 10 for Business"`.

---

### Connect a Fabric Data Agent

A **Fabric Data Agent** over the ontology translates natural language to GQL queries.  For reliable NL→GQL:

1. Create a Data Agent in your Fabric workspace pointed at the deployed ontology.
2. **Use the auto-generated grounding file.** When you run `deploy-ontology --multitype`, the CLI writes **`data-agent-instructions.md`** next to your `--parquet-dir` by default (toggle with `--no-create-data-agent-instruction`). It is generated from the **live deployed graph** — the actual entity types with instance counts, the exact relationship edge names with direction, and example queries seeded from your `set-domain` sample questions. Because it is regenerated on every deploy, it always matches what is live.
   - Override the path with `--agent-instruction-out <path>`, and enrich it with your domain brief via `--domain-file build\enriched\domain.json`.
   - A hand-curated reference for the Surface corpus also lives at **`docs/data-agent-grounding.md`**.
3. Paste its three sections into the Data Agent: **Additional instructions**, per-entity **descriptions**, and **example queries**. The instructions force `CONTAINS` (not exact match) and short single-hop queries.

Without grounding, the agent may generate valid GQL that returns 0 rows due to exact-match name mismatches (e.g. `"Surface Pro 10"` vs. `"Surface Pro 10 for Business"`).

---

### Why Densify Matters

The LLM enrichment pipeline runs **per document section** — it extracts entities and relationships from each section independently, with no awareness of adjacent sections.  This produces a **sparse, fragmented graph**: device models are disconnected from their own parts; troubleshooting symptoms have no path to their causes or resolutions.

`fabric-kg densify` repairs this in four passes:

1. **DeviceModel hub edges** — for each document, links the specific device model(s) it covers to every Component, Part, Procedure, and Symptom in that same document (`has_component`, `has_part`, `has_procedure`, `has_symptom`).
2. **Cause → Symptom → Resolution triples** (`--link-scr`, default on) — connects isolated troubleshooting entities within each document using keyword overlap (`causes`, `resolved_by`, `addressed_by` edges, confidence 0.45).
3. **Procedure → Step edges** (`--link-steps`, default on) — reconstructs `has_step` links by document reading order (mapping Procedure/Step entities to their position via `document_elements`), so "list the steps for procedure X" works even when extraction missed them.
4. **RCA diagnostic-path edges** (`--link-rca`, default on) — links each Symptom to its diagnostic procedures (`diagnosed_by` → SDT / check / inspect / validate procedures) and repair procedures (`remediated_by`), so a Symptom becomes the hub of a complete root-cause-analysis answer.

**Measured impact on the Surface corpus (22 PDFs):**
- Total relationships: **3,715 → 35,445** (+854 %)
- Isolated symptoms (no edges): **327 → 8** (−98 %)
- Procedures with steps: **2 % → 27 %**
- RCA edges added: **142 `diagnosed_by` + 826 `remediated_by`**

#### The RCA chain you can now traverse

A single Symptom node connects to the full troubleshooting story:

```
Cause ──causes──▶ Symptom ──diagnosed_by──▶ Procedure (diagnostic test)
                    │  └────remediated_by──▶ Procedure (repair) ──has_step──▶ Step
                    └──────resolved_by─────▶ Resolution
```

Example — *"Battery expansion"* resolves to **28 causes**, **1 diagnostic test** (Lithium-ion battery inspection), **19 remediation procedures → 35 actionable steps**, and **28 resolutions**. The `diagnosed_by` procedures are real diagnostic entities already in the corpus (SDT, battery status checks, inspections) — not synthesised, so the agent never has to fall back to generic LLM guesses.

Densify is deterministic, idempotent, and **strictly additive** — input files are never modified and existing edges are never removed; it only appends. `compile-data` enforces this with an **additivity guard** that fails the build if any entity or relationship present in the input is missing from the output. Run densify every time between `enrich` and `compile-data`.

---

### The Iteration Loop

Building a high-quality domain graph is an iterative process.  The key insight: **re-deploying the ontology reuses existing enriched data — no re-enrichment needed.**  Iteration is fast and cheap.

```
1. Design template    — pick entity types + relationships for your industry
2. set-domain         — --industry, --business-domain, --questions-file, entity types, relationships
3. enrich             — LLM extraction (run once; --resume for incremental)
4. densify            — add hub edges + S/C/R triples
5. compile-data       — from densified output
6. deploy-ontology    — --multitype --no-mock  (reuses Parquet; ~1-2 min LRO)
                         └─ also writes data-agent-instructions.md from the live graph
7. connect Data Agent — paste data-agent-instructions.md (auto-generated)
8. TEST               — run your sample questions against the Data Agent
9. inspect failures   — missing edges? name mismatch? zero rows?
10. refine            — tune densify params, improve grounding, re-deploy ontology
    └─ GOTO 4         — no re-enrichment required
```

**Common failure modes and fixes:**

| Symptom | Likely cause | Fix |
|---|---|---|
| Zero rows for device queries | Sparse graph (no hub edges) | Run `densify` |
| Zero rows despite hub edges | Exact-match name mismatch | Use CONTAINS in GQL / update grounding |
| Missing entity type in Explorer | Not enough instances (below min-pair-count) | Lower `--min-pair-count` on `deploy-ontology` |
| Symptom/Cause not linked | SCR linking didn't fire | Check `--link-scr` flag; inspect keyword overlap |

---

### Adapting to Other Industries

Define your own domain template by naming the entity types and relationships that fit your industry.  Three starting points:

| Industry | Example entity types |
|---|---|
| **Healthcare** | Patient, Condition, Symptom, Treatment, Medication, Provider, Facility |
| **Legal** | Contract, Party, Clause, Obligation, Term, Jurisdiction, Amendment |
| **Finance** | Account, Transaction, Counterparty, Instrument, Risk, Portfolio, Regulation |

For each industry: pass `--industry` and `--business-domain`, name 4-6 entity types and 3-5 relationships in `--prompt`, and supply 3-5 sample questions via `--questions-file`, then follow the build sequence above. External inspiration: Microsoft GraphRAG *auto prompt-tune* (adapts extraction to your domain + a data sample) and the Neo4j *LLM Graph Builder* (configure the node/relationship schema up front).

---



All commands accept the global options `--config PATH`, `--env [dev|test|prod]`, `-v`/`--verbose`, `-q`/`--quiet`, and `--dry-run`. Run `fabric-kg <command> --help` for full option details.

| Command | Description |
|---|---|
| `init` | Scaffold a new project — creates `fabric-kg.yaml`, ontology model stub, and directory layout |
| `set-domain` | Persist a domain brief to `build/enriched/domain.json` (`--industry`, `--business-domain` **required**; `--prompt` or `--domain-file`; `--questions-file` for sample questions) |
| `inspect-source` | Analyse source files and report columns, structure, and detected file types |
| `enrich` | Run LLM extraction on source files; produce per-file enriched JSON (`--input`, `--out`, `--domain-file`, `--resume`, `--force`) |
| `densify` | Add DeviceModel hub edges + Cause/Symptom/Resolution links to enriched JSON (`--input`, `--out`, `--link-scr`) |
| `compile-data` | Convert enriched JSON into the 8 canonical Parquet tables (`--input`, `--out`) |
| `compile-ontology` | Generate Fabric Ontology definition parts from Parquet schema (`--out`) |
| `compile-search` | Generate Azure AI Search index schemas and document batches (`--input`, `--out`) |
| `package` | Bundle build artifacts into `dist/` with a manifest (`--out`, `--include-search`) |
| `validate` | Validate build artifacts, ontology shape, and AI Search schemas |
| `build-deploy` | Run the full pipeline end-to-end (`--input`, `--env`, `--resume`, `--force`, `--skip-search`) |
| `deploy-lakehouse` | Upload canonical Parquet tables to Fabric Lakehouse via fabric-cicd (`--env`, `--dist`) |
| `deploy-ontology` | Deploy Fabric Ontology definition (`--env`, `--no-mock`; `--multitype` for a rich typed graph; `--create-data-agent-instruction` writes Data Agent grounding) |
| `deploy-search` | Upload AI Search index schemas and document batches (`--env`, `--dist`) |

---

## Use from GitHub Copilot CLI (plugin)

You can drive `fabric-kg` directly from [GitHub Copilot CLI](https://docs.github.com/copilot/how-tos/use-copilot-agents/use-copilot-cli)
via an installable plugin in [`plugins/fabric-kg/`](plugins/fabric-kg/). The
plugin bundles two skills (the build/deploy pipeline and the Surface RCA
reproduction) and a guided `kg-builder` agent. It orchestrates the **installed
`fabric-kg` CLI** — so `pip install fabric-kg-builder` (or `pip install -e .`)
is still a prerequisite.

Install it from the repo's plugin marketplace:

```shell
copilot plugin marketplace add hyssh/fabric-kg-builder
copilot plugin install fabric-kg@fabric-kg-builder
```

Then in a Copilot CLI session, verify with `/plugin list`, `/skills list`, and
`/agent`, and just ask — e.g. *"Build a Fabric knowledge graph from the PDFs in
./docs, densify it, and validate"* or *"Reproduce the Surface troubleshooting
graph and deploy to dev"*. See [`plugins/fabric-kg/README.md`](plugins/fabric-kg/README.md)
for all install options (marketplace, repo subdirectory, local path).

---

## Project Layout

```
fabric-kg-builder/
├── src/
│   └── fabric_kg_builder/
│       ├── cli/            # Click commands (one file per command)
│       ├── config/         # Config loader (fabric-kg.yaml + env JSON)
│       ├── sources/        # Document router, PDF/DOCX/HTML/CSV loaders, chunker
│       ├── enrichment/     # Foundry client, domain brief, LLM orchestrator
│       ├── parquet/        # Canonical schema writers (8 tables)
│       ├── ontology/       # Fabric Ontology definition builder
│       ├── search/         # AI Search schema + batch generators
│       ├── deploy/         # Lakehouse, Ontology, Search deployers (fabric-cicd)
│       ├── model/          # Pydantic data models
│       └── validate/       # Artifact and schema validators
├── plugins/
│   └── fabric-kg/          # GitHub Copilot CLI plugin (skills + agent)
├── tests/
│   ├── unit/               # Pure-function tests, no I/O
│   ├── contract/           # Schema-conformance tests, fixture data only
│   └── integration/        # Full-pipeline tests against real sample_data (opt-in)
├── docs/                   # PRD, specs (SPEC-001 … SPEC-005), infra docs
├── ontology/
│   └── environments/       # dev.json, test.json, prod.json — per-env resource config
├── sample_data/
│   └── Surface_Troubleshootings/   # 22 Surface service-guide PDFs
├── data/                   # Build outputs (gitignored)
├── dist/                   # Packaged artifacts (gitignored)
├── fabric-kg.yaml          # Non-secret project config
└── .env.example            # Secret variable template (copy to .env)
```

---

## Testing

```bash
# Fast default: unit + contract tests (no network, no real files)
pytest

# Integration tests — reads real files from sample_data/
pytest -m integration

# Slow tests (large fixtures, real PDFs) — implies integration
pytest -m slow

# All tests
pytest -m ""

# With coverage report
pytest --cov=fabric_kg_builder --cov-report=html
```

**Markers:**

| Marker | Scope |
|---|---|
| `unit` | Pure-function tests; no I/O, no network |
| `contract` | Schema-conformance tests; fixture data only |
| `integration` | Full-pipeline tests; reads real `sample_data/` files (opt-in) |
| `slow` | Tests > 2 s — real PDFs, large fixtures (opt-in) |
| `smoke` | Post-deploy live-environment checks (not part of merge gate) |

CI enforces ≥ 80 % coverage on core modules (`--cov-fail-under=80`).

---

## Notes & Limitations

- **`build-deploy` is not yet fully implemented** — use the step-by-step commands for now.  
- **Embedding dimensions are locked at 1536** (`text-embedding-3-large`). Changing the embedding model requires a full reindex and schema migration.  
- **Document text is not stored in the Lakehouse** — it is indexed in Azure AI Search for retrieval.  
- **Visual assets** (images, figures) are stored in Azure Blob Storage; only their URLs appear in Parquet / Search documents.  
- **`deploy-ontology` defaults to mock mode** — pass `--no-mock` for a live Fabric workspace deploy.  
- **Windows path separators** are used in examples throughout; POSIX equivalents use forward slashes.  
- **Sensitivity labels** for Fabric items must be set to your organisation's display name in `ontology/environments/{env}.json` (`fabric.sensitivity_label`).  
- **Schema-enabled Lakehouse required** — the Fabric Lakehouse must be created with `enableSchemas=true`.
