# fabric-kg-builder

> Turn documents, images, and tabular data into a deployable Microsoft Fabric knowledge graph — enriched by Azure OpenAI, searched via Azure AI Search, governed by a Fabric Ontology, and deployed through **fabric-cicd**.

📄 **Project site:** [hyssh.github.io/fabric-kg-builder](https://hyssh.github.io/fabric-kg-builder/) · ✉️ Questions: [https://github.com/hyssh/fabric-kg-builder/issues](mailto:https://github.com/hyssh/fabric-kg-builder/issues)

📋 **0.2.4 release proof:** [`docs/RELEASE-0.2.4-PROOF.md`](docs/RELEASE-0.2.4-PROOF.md) · **Post-merge live checklist:** [`docs/SMOKE-TEST-0.2.4.md`](docs/SMOKE-TEST-0.2.4.md)

---

## What It Does

`fabric-kg-builder` is a Python CLI for governed, evidence-backed knowledge
graphs:

1. **Propose the domain** from business goals, five to ten competency questions,
   and representative sources.
2. **Review and explicitly approve** one schema-2 contract that seals the bounded
   relationship vocabulary `N`, traversal depth `K`, source profile, prompt/model
   identity, mappings, and stable IDs.
3. **Extract and locally verify exact evidence**. Unsupported, unresolved, and
   rejected candidates remain available for audit but never enter serving tables.
4. **Compile one sealed semantic projection** into typed Lakehouse, Ontology,
   Graph, agent, and Search artifacts with count/hash equivalence gates.
5. **Package and validate** the complete authority bundle.
6. **Review a dry-run and explicitly approve live mutation**. Schema-2
   `build-deploy` requires the same run ID, unchanged plan fingerprint,
   `--resume`, and `--approve-live`.
7. **Execute bounded queries** from persisted plans that cannot exceed approved
   `K`, with exact evidence returned for relationship findings.

Schema-1 contracts remain readable through their compatibility workflow and are
not automatically migrated.

---

## Features

- **Copilot-assisted domain design** — cited proposal, deterministic YAML/JSON automation, and explicit human approval
- **Bounded semantic authority** — approved vocabulary `N`, traversal bound `K`, mappings, vocabulary, stable IDs, and hashes
- **Document Intelligence** — tables extracted as HTML; figures as images stored in Blob  
- **Exact evidence extraction** — model-authored IDs are never trusted; spans and deterministic evidence IDs are verified locally
- **Audit/serving separation** — raw lifecycle candidates are retained while only asserted, evidence-backed semantic rows publish
- **Sealed deployment** — Lakehouse, Ontology, and Graph consume the same semantic projection and receipts
- **Resume dependency graph** — source, profile, domain, model/prompt, semantic, Search, package, deploy, and validation changes invalidate exact transitive stages
- **Bounded Graph queries** — persisted plans and runtime execution enforce approved `K`
- **Azure AI Search** — vector (text-embedding-3-large, 1536 dims) + keyword indexes for grounded retrieval  
- **Generated connection guide** — packaged `ONTOLOGY_SEARCH_CONNECTION.md` explains Ontology → Graph → Search identity, source quotations, and reliable query flow
- **Reviewed live mutation** — matching dry-run and explicit approval are required for schema-2 `build-deploy`
- **Multi-environment** — `dev` / `test` / `prod` configs in `ontology/environments/`  
- **Actionable safe diagnostics** — checkpointed per-work-unit category/message/retry context without source content or secrets
- **DefaultAzureCredential** auth — `az login` for dev; Service Principal for CI/prod  

---

## Architecture Overview

```
Business goals + competency questions + representative sources
    │
    ▼  init-domain → cited proposal → domain approve
    │  seals N, K, profile, prompt/model, semantic mappings and IDs
    │
Source files (PDF / DOCX / HTML / CSV)
    │
    ▼  inspect-source → enrich
    │  exact span verification + asserted/unresolved/rejected/discovery lifecycle
    │
    ▼  compile-data
    │  ├─ raw candidate/audit surfaces
    │  └─ sealed semantic entity/relationship projection
    │
    ├─▶ compile-semantic → compile-ontology + compile-graph + compile-agent
    ├─▶ compile-search
    ▼  validate-artifacts → package → validate
    │
    ▼  build-deploy --dry-run → operator review
    ▼  build-deploy --resume --approve-live
    └─▶ Lakehouse + Ontology + Graph + Search + bounded agent/runtime
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

## Quickstart — Schema-2 Workflow

```bash
# 1. Generate draft proposal + contract from deterministic intake and sources.
fabric-kg init-domain \
  --input ./sources \
  --intake ./domain-intake.yaml \
  --non-interactive \
  --out ./domain.yaml

# 2. Review, then explicitly seal the proposal/profile/contract.
fabric-kg domain validate --file ./domain.yaml
fabric-kg domain approve \
  --file ./domain.yaml \
  --proposal ./.fkg/domain-proposal.json \
  --source-profile ./.fkg/source-profile.json \
  --approved-by "$OPERATOR"

# 3. Review the complete non-mutating plan under a stable run ID.
RUN_ID="$(python -c 'import uuid; print(uuid.uuid4())')"
fabric-kg build-deploy \
  --input ./sources \
  --domain-contract ./domain.yaml \
  --semantic-contract ./ontology/semantic-contract.yaml \
  --env dev \
  --run-id "$RUN_ID" \
  --dry-run

# 4. Only after operator review, continue the unchanged plan.
fabric-kg build-deploy \
  --input ./sources \
  --domain-contract ./domain.yaml \
  --semantic-contract ./ontology/semantic-contract.yaml \
  --env dev \
  --run-id "$RUN_ID" \
  --resume \
  --approve-live \
  --graph-preview-acknowledged
```

If source, domain/profile, model/prompt, semantic mappings/vocabulary/IDs,
configuration, or selected deployment stages change, the plan fingerprint no
longer matches and a new dry-run is required. See
[`docs/SMOKE-TEST-0.2.4.md`](docs/SMOKE-TEST-0.2.4.md) for the post-merge live
read-back sequence. This repository does not claim 0.2.4 live success until that
checklist is completed.

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

## Legacy Schema-1 Domain Template Playbook

> **Compatibility reference only.** The material in this section documents the
> older schema-1 `set-domain`, heuristic densification, generic `--multitype`,
> and Surface reproduction workflow. It must not be used to bypass schema-2
> proposal approval, closed vocabulary, exact evidence, audit/serving
> separation, sealed semantic deployment, or bounded K. New 0.2.4 projects use
> the schema-2 quickstart above. Measured Surface results below are historical
> 0.2.3 evidence, not a claim of 0.2.4 live acceptance.

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

#### Domain mappings

The defaults above preserve the Surface schema. For another schema, pass
`--densify-config path/to/densify.yaml`. The YAML may override hub source types,
their qualification (`specific`, the Surface keyword-and-version heuristic, or
`any`, which only rejects generic names), target relationship verbs, S/C/R types
and verbs, procedure/step types and verb, RCA verbs, and umbrella naming regexes:

```yaml
hub:
  source_types: [Equipment]
  qualification: any
  target_relationships:
    HVACComponent: has_component
    MaintenanceProcedure: has_maintenance_procedure
scr:
  cause_types: [FaultCause]
  symptom_types: [Fault]
  resolution_types: [CorrectiveAction]
  cause_symptom_relationship: causes_fault
  symptom_resolution_relationship: corrected_by
procedure_steps:
  procedure_types: [MaintenanceProcedure]
  step_types: [WorkStep]
  relationship: has_work_step
rca:
  symptom_types: [Fault]
  procedure_types: [MaintenanceProcedure]
  diagnosed_by_relationship: diagnosed_by
  remediated_by_relationship: remediated_by
```

Mappings are document-scoped heuristics, not semantic inference: S/C/R and RCA
still require discriminating shared name tokens, procedure-step links require
matching `document_elements` text and reading order, and umbrella rollups require
the configured naming regex and shared key nouns. Review inferred, lower-confidence
edges before relying on them for safety-critical decisions.

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
| `set-domain` | Legacy schema-1 domain brief compatibility command; new projects use `init-domain` plus `domain approve` |
| `inspect-source` | Analyse source files and report columns, structure, and detected file types |
| `enrich` | Run LLM extraction on source files; produce per-file enriched JSON (`--input`, `--out`, `--domain-file`, `--resume`, `--force`) |
| `densify` | Optional explicit, domain-configured additive rules; schema-2 output still must pass closed-vocabulary/evidence gates |
| `init-domain` | Generate a cited schema-2 proposal and draft contract; never auto-approves noninteractive output |
| `domain` | Validate, review, explicitly approve, and inspect domain authority |
| `compile-data` | Produce reconciled raw audit and sealed semantic Parquet surfaces |
| `compile-semantic` | Seal semantic contract, mappings, vocabulary, IDs, projection, and query authority |
| `compile-ontology` / `compile-graph` / `compile-agent` | Compile serving artifacts only from sealed semantic authority |
| `compile-search` | Generate Azure AI Search index schemas and document batches (`--input`, `--out`) |
| `package` | Bundle build artifacts into `dist/` with a manifest (`--out`, `--include-search`) |
| `validate-artifacts` / `validate` | Enforce exact evidence, lifecycle, identity, hash/count, and package gates |
| `build-deploy` | Fingerprinted pipeline; schema-2 live mutation requires matching dry-run, `--resume`, and `--approve-live` |
| `deploy-lakehouse` | Upload canonical Parquet tables to Fabric Lakehouse via fabric-cicd (`--env`, `--dist`) |
| `deploy-ontology` | Deploy sealed schema-2 Ontology artifacts; legacy `--multitype` remains compatibility-only |
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
│       ├── enrichment/     # Foundry client, exact evidence, lifecycle orchestrator
│       ├── parquet/        # Canonical raw/audit and semantic schema writers
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

## Deployment Manifest — Fabric Item Naming Authority

Starting with Issue #6, a `deployment.yaml` file is the **single authority** for Fabric item
display names. This replaces the previous approach where names could come from five different
sources (env JSON fields, CLI defaults, generated metadata, semantic titles, command flags).

### Why a single manifest?

Without a unified authority, a generated semantic title (e.g. `"Equipment semantic contract"`)
could silently override a configured ontology name (`"demo-ontology"`). The manifest makes this
conflict visible and actionable.

### Using the deployment manifest

1. Copy `deployment.yaml.example` to `deployment.yaml` in your project root.
2. Fill in your display names and workspace ID:
   ```yaml
   workspace: ${FABRIC_WORKSPACE_ID}
   items:
     ontology:    { display_name: my_ontology }
     lakehouse:   { display_name: my_lakehouse }
     graph_model: { display_name: My Graph Model }
     data_agent:  { display_name: my-dev-agent }
   ```
3. Pass `--manifest deployment.yaml` to any deploy command:
   ```powershell
   fabric-kg deploy-ontology --env dev --manifest deployment.yaml --no-mock
   fabric-kg deploy-graph    --env dev --manifest deployment.yaml --no-dry-run
   fabric-kg build-deploy    --env dev --manifest deployment.yaml --dry-run
   ```

### Conflict detection

When a manifest is provided and a generated metadata name or CLI `--*-name` flag differs from
the manifest, the CLI raises:
```
ERROR NAME_AUTHORITY_CONFLICT:
Generated display name "Equipment semantic contract" conflicts with
manifest display name "demo-ontology".
Update deployment.yaml items.ontology.display_name or remove the
conflicting source; names must be defined once in the manifest.
```

### Dry-run name resolution

`--dry-run` (or `--mock`) always prints the resolved name block before any Fabric mutation:
```
Resolved item:
  type: Ontology
  display name: demo-ontology
  name authority: deployment.yaml
  generated metadata: compatible

No naming conflicts detected.
```

### Migration from legacy env JSON

If `--manifest` is **not** supplied, the CLI synthesises an in-memory manifest from your
existing `ontology/environments/{env}.json` `fabric.*_display_name` fields (legacy mode).
Existing deployments continue to work without changes.

To migrate:
1. Copy display names from your env JSON into `deployment.yaml`.
2. Add `--manifest deployment.yaml` to your deploy commands.
3. When both are present and differ, a **migration warning** is emitted and the manifest wins.
4. Remove the legacy `fabric.*_display_name` fields from the env JSON once migrated.

| Source | Behaviour |
|--------|-----------|
| `deployment.yaml` (via `--manifest`) | **Authoritative** — always wins |
| Generated metadata / semantic title | Must match manifest, else `NAME_AUTHORITY_CONFLICT` |
| `--*-name` CLI flags | Must match manifest, else `NAME_AUTHORITY_CONFLICT` |
| Legacy env JSON name fields | Migration input only; migration warning if manifest differs; manifest wins |

---

## Notes & Limitations

- **Embedding dimensions are locked at 1536** (`text-embedding-3-large`). Changing the embedding model requires a full reindex and schema migration.  
- **Document text is not stored in the Lakehouse** — it is indexed in Azure AI Search for retrieval.  
- **Visual assets** (images, figures) are stored in Azure Blob Storage; only their URLs appear in Parquet / Search documents.  
- **`deploy-ontology` defaults to mock mode** — pass `--no-mock` for a live Fabric workspace deploy.  
- **0.2.4 live acceptance is pending post-merge validation** — local gates do not prove live Lakehouse/Ontology/Graph/Search read-back.
- **Windows path separators** are used in examples throughout; POSIX equivalents use forward slashes.  
- **Sensitivity labels** for Fabric items must be set to your organisation's display name in `ontology/environments/{env}.json` (`fabric.sensitivity_label`).  
- **Schema-enabled Lakehouse required** — the Fabric Lakehouse must be created with `enableSchemas=true`.
