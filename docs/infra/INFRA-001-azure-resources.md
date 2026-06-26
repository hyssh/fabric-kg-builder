# INFRA-001: Azure Resource Inventory

**Date:** 2026-06-24T11:46:10.517-07:00  
**Author:** Keyser (Lead / Architect)  
**Related spec:** SPEC-001 §5 (Configuration), SPEC-002 (Canonical Data Model)

### Revision History

| Date | Author | Summary |
|---|---|---|
| 2026-06-24T11:46:10.517-07:00 | Keyser | Initial draft |
| 2026-06-24T12:42:17.255-07:00 | Keyser | v2 — Foundry expanded to two deployments (chat + embedding); dev/test environment section with verified Azure resources; auth note (DefaultAzureCredential dev / SPN CI); test-data/fixtures note; model defaults locked. |
| 2026-06-24T15:41:07.842-07:00 | Verbal | v3 — Corrected enrichment default to gpt-5.4-mini (deployment `gpt-5-4-mini`, 200K TPM GlobalStandard); added 200K TPM minimum requirement; marked GPT model action item DONE and Lakehouse action item DONE (fabrickg_lakehouse c1a44e9d-...); AI Search scope corrected to IN MVP; reference REQUIREMENTS-001. |

---

## Purpose

This document lists all Azure resources required by `fabric-kg-builder`, what configuration belongs in `fabric-kg.yaml` (non-secret, committed) vs. `.env` (secret, gitignored), and which values vary by environment.

> **Security rule:** Secrets are NEVER committed. See `.copilot/skills/secret-handling/SKILL.md`.

---

## Resource Inventory

### 1. Azure AI Foundry (LLM + Embedding + Vision Models)

Microsoft Foundry hosts **both** the chat/LLM model and the embedding model. Two deployment slots are required:

#### 1a. Chat / Enrichment Deployment

> **⚠️ 200K TPM Minimum:** The enrichment deployment must be provisioned at **≥ 200,000 TPM (capacity 200, GlobalStandard)** so the `enrich` stage can run high-volume concurrent extraction without throttling. The dev deployment `gpt-5-4-mini` satisfies this requirement. See `docs/REQUIREMENTS-001-cli-prerequisites.md` §6 for setup details.

| Attribute | Detail |
|---|---|
| **Purpose** | LLM text enrichment, vision/image analysis, domain-intake |
| **Default model** | **gpt-5.4-mini** — deployment name `gpt-5-4-mini` (GlobalStandard, **200K TPM**, deployed 2026-06-24 on `example-aiservices`) |
| **Fallback deployment** | `chat` (gpt-4.1) — retained as fallback if `gpt-5-4-mini` is unavailable |
| **Note** | `gpt-5.5-mini` does **not** exist in the Azure AI catalog; gpt-5.4-mini is the current newest mini variant. |
| **SDK** | `azure-ai-projects` (Microsoft Foundry SDK) |
| **In `fabric-kg.yaml`** | `foundry.project`, `foundry.endpoint` (as `${ENV_VAR}` ref), `enrichment.chat_deployment` |
| **In `.env`** | `AZURE_AI_FOUNDRY_ENDPOINT`, `AZURE_AI_FOUNDRY_API_KEY` |
| **Env-varying** | Endpoint (per Foundry project); `chat_deployment` name |
| **Stable across envs** | `foundry.project`, pipeline logic |

#### 1b. Embedding Deployment

| Attribute | Detail |
|---|---|
| **Purpose** | Generate vector embeddings for AI Search `chunk_vector` field (1536-dim) |
| **Default model** | **text-embedding-3-large** @ `dimensions=1536` |
| **Fallback model** | text-embedding-3-small @ `dimensions=1536` |
| **SDK** | `azure-ai-projects` (same Foundry SDK) |
| **In `fabric-kg.yaml`** | `enrichment.embedding_deployment`, `enrichment.embedding_dimensions: 1536` |
| **In `.env`** | (uses same `AZURE_AI_FOUNDRY_ENDPOINT` / `AZURE_AI_FOUNDRY_API_KEY`) |
| **Env-varying** | `embedding_deployment` name (if different model per env) |
| **Stable across envs** | `embedding_dimensions: 1536` — **coupled to AI Search vector field width; changing requires full reindex** |

> **⚠️ Dimension coupling:** The `embedding_dimensions=1536` value must match the AI Search `chunk_vector` field dimension (SPEC-002 / RESEARCH-001). Changing the embedding model or dimension requires reindexing all AI Search data.

#### Config split summary (Foundry)

```yaml
# fabric-kg.yaml (committed, non-secret)
foundry:
  project: "${AZURE_AI_FOUNDRY_PROJECT:-example-project}"
  endpoint: "${AZURE_AI_FOUNDRY_ENDPOINT}"

enrichment:
  # ⚠️ Enrichment deployment must be ≥200K TPM (GlobalStandard) — see REQUIREMENTS-001 §6
  chat_deployment: "gpt-5-4-mini"      # gpt-5.4-mini @ 200K TPM; fallback: "chat" (gpt-4.1)
  vision_deployment: "gpt-5-4-mini"    # default = chat deployment (multimodal)
  embedding_deployment: "embedding"    # text-embedding-3-large @ 1536 dims
  embedding_dimensions: 1536           # couples to AI Search vector field — reindex if changed
```

```dotenv
# .env (gitignored — secrets only)
AZURE_AI_FOUNDRY_ENDPOINT=https://<account>.services.ai.azure.com
AZURE_AI_FOUNDRY_API_KEY=<your-key>
AZURE_AI_FOUNDRY_PROJECT=example-project
```

| **Auth** | API key in `.env` or `DefaultAzureCredential` (preferred for dev — developer is `az login`'d; SPN for CI/prod) |

---

### 2. Azure AI Document Intelligence

| Attribute | Detail |
|---|---|
| **Purpose** | Extract OCR text + bounding polygons/callouts for `visual_regions` (SPEC-002). Used during ingest/extract for PDF and image source files. |
| **Status** | **REQUIRED** — this is a locked decision. Remember for later use when implementing PDF/image extraction. |
| **SDK** | `azure-ai-documentintelligence` |
| **In `fabric-kg.yaml`** | `document_intelligence.endpoint` (as `${ENV_VAR}` reference) |
| **In `.env`** | `AZURE_DOC_INTELLIGENCE_ENDPOINT`, `AZURE_DOC_INTELLIGENCE_API_KEY` |
| **Env-varying** | Endpoint (if using separate instances per env) |
| **Stable across envs** | API version, model ID (e.g., `prebuilt-layout`) |
| **Auth** | API key in `.env` or `DefaultAzureCredential` |

> **Note:** Document Intelligence provides layout analysis (text, tables, figures, bounding boxes) that feeds directly into `visual_regions.parquet` and `document_elements.parquet`. This is not optional — the visual extraction pipeline depends on it.

---

### 3. Azure Blob Storage (Visual Asset Storage)

| Attribute | Detail |
|---|---|
| **Purpose** | Store uploaded visual assets (images, figures) referenced by `blob_url` in Parquet tables |
| **SDK** | `azure-storage-blob` |
| **In `fabric-kg.yaml`** | `blob_storage.account_name`, `blob_storage.container` |
| **In `.env`** | `AZURE_STORAGE_CONNECTION_STRING` or `AZURE_STORAGE_SAS_TOKEN` |
| **Env-varying** | Container name, path prefix (configured in `ontology/environments/{env}.json`: `blob_container`, `blob_path_prefix`) |
| **Stable across envs** | Account name (typically shared), upload logic |
| **Auth** | Connection string or SAS token in `.env`; `DefaultAzureCredential` also supported |

---

### 4. Microsoft Fabric Workspace + Lakehouse (OneLake)

| Attribute | Detail |
|---|---|
| **Purpose** | Target deployment for ALL canonical structured data (Parquet tables). Data is queryable via Spark SQL / OneLake. Also hosts the Fabric Ontology definition. |
| **SDK** | Fabric REST API + `fabric-cicd` (for ontology) |
| **In `fabric-kg.yaml`** | `deploy.method` (deployment approach) |
| **In per-env JSON** (`ontology/environments/{env}.json`) | `workspace_id`, `lakehouse_item_id`, `ontology_display_name_suffix`, `sensitivity_label`, `schema_name` |
| **In `.env`** | `FABRIC_CLIENT_ID`, `FABRIC_CLIENT_SECRET`, `FABRIC_TENANT_ID` (optional — only if using Service Principal auth) |
| **Env-varying** | Workspace ID, Lakehouse item ID, display name suffix, sensitivity label — all in per-env JSON |
| **Stable across envs** | Ontology model (`model.yaml`), type IDs (`ids.lock.json`), Parquet schemas |
| **Auth** | `DefaultAzureCredential` (preferred); Service Principal credentials in `.env` as fallback |

> **Key constraint:** Structured/tabular data (entities, relationships, evidence, source_files) MUST land in the Fabric Lakehouse. It is never indexed into Azure AI Search.

---

### 5. Azure AI Search (IN MVP — Text/Visual Retrieval Only)

| Attribute | Detail |
|---|---|
| **Purpose** | Retrieval index for unstructured text and visual content (chunks, document elements, image descriptions, table HTML). **IN MVP scope** (enabled in dev — `ai_search.enabled=true` in `dev.json`). **NOT used for structured/tabular data.** |
| **SDK** | `azure-search-documents` |
| **In `fabric-kg.yaml`** | `search.enabled`, `search.service_name`, `search.index_prefix` |
| **In `.env`** | `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_API_KEY` |
| **Env-varying** | Index prefix (configured via `ontology/environments/{env}.json`: `search_index_prefix`) |
| **Stable across envs** | Index field schemas, search logic |
| **Auth** | Admin API key in `.env`; `DefaultAzureCredential` for dev (`az login`); SPN for CI/prod |
| **Disabled behavior** | When `search.enabled=false`, all compile-search and deploy-search commands are no-ops (dev has this enabled) |

> **Scope constraint:** AI Search indexes only: `chunks`, `document_elements`, `visual_assets`. Structured tables (`entities`, `relationships`, `evidence`, `source_files`) are excluded — they live in the Lakehouse.

---

## Summary: What Goes Where

| Resource | `fabric-kg.yaml` (committed) | `.env` (gitignored) | Per-env JSON |
|---|---|---|---|
| AI Foundry (chat) | Project name, `chat_deployment` | Endpoint, API key | — |
| AI Foundry (embedding) | `embedding_deployment`, `embedding_dimensions` | (same endpoint/key) | — |
| Document Intelligence | Endpoint reference (`${ENV_VAR}`) | Endpoint, API key | — |
| Blob Storage | Account name, container | Connection string or SAS | Container, path prefix |
| Fabric / Lakehouse | Deploy method | SP credentials (optional) | Workspace ID, Lakehouse ID, suffix, label |
| AI Search | Enabled flag, service name, prefix | Endpoint, API key | Index prefix |

### Authentication Strategy

| Environment | Recommended Auth | Notes |
|---|---|---|
| **Dev** (local) | `DefaultAzureCredential` (developer runs `az login`) | No secrets needed for most services; API key fallback for Foundry if needed |
| **CI / Test** | Service Principal (SPN) via env vars | `FABRIC_CLIENT_ID`, `FABRIC_CLIENT_SECRET`, `FABRIC_TENANT_ID` in CI secrets |
| **Prod** | Service Principal or Managed Identity | Managed Identity preferred when running in Azure |

---

## Dev/Test Environment (Verified)

> Resources verified via `az` CLI on 2026-06-24T12:42:17.255-07:00.

| Attribute | Value |
|---|---|
| **Subscription** | Example-Subscription (`00000000-0000-0000-0000-000000000000`) |
| **Resource Group** | `example-rg` |
| **Fabric Workspace ID** | `11111111-1111-1111-1111-111111111111` |

### Resource Mapping

| Infra Need | Azure Resource | Region | Status |
|---|---|---|---|
| Foundry (CognitiveServices) | `example-aiservices` / project `example-project` | eastus2 | ✅ Active |
| Chat deployment (default) | `gpt-5-4-mini` = gpt-5.4-mini, 200K TPM GlobalStandard | eastus2 | ✅ Deployed (2026-06-24) |
| Chat deployment (fallback) | `chat` = gpt-4.1 | eastus2 | ✅ Deployed |
| Embedding deployment | `embedding` = text-embedding-3-large @ 1536 dims | eastus2 | ✅ Deployed |
| Other Foundry models | `model-router`, `gpt-4o` | eastus2 | ✅ Available |
| Azure AI Search | `example-search` | swedencentral | ✅ Active |
| Document Intelligence | `example-docintell` | westus3 | ✅ Active (REQUIRED) |
| Vision | `example-vision` | swedencentral | ✅ Active |
| Blob Storage | `examplestorageacct` | eastus2 | ✅ Active |
| Key Vault | `example-kv` | eastus2 | ✅ Active (app secrets still via `.env`) |
| APIM | `exp-demo-apim` | — | ℹ️ Optional |

### Action Items

| # | Item | Owner | Status |
|---|---|---|---|
| 1 | Deploy enrichment model to `example-aiservices` at ≥200K TPM (GlobalStandard) | Infra / Hyunsuk | ✅ **Done** — `gpt-5-4-mini` (gpt-5.4-mini, capacity 200 = 200K TPM) deployed 2026-06-24 |
| 2 | Create `fabrickg_lakehouse` in workspace and wire into dev.json | Keyser | ✅ **Done** — item ID `44444444-4444-4444-4444-444444444444`, workspace `11111111-1111-1111-1111-111111111111` (2026-06-24) |

> **Setup reference:** See `docs/REQUIREMENTS-001-cli-prerequisites.md` for the full engineer onboarding guide (Azure CLI install, RBAC, `.env`, Foundry capacity check, Fabric access, verify checklist, Sprint-1 quickstart).

---

## Test Data / Fixtures

| Path | Purpose | Status |
|---|---|---|
| `sample_data\Surface_Troubleshootings\*.pdf` | Reserved for future e2e tests and grounding fixtures (Sprint 2+) | **Not processed now** — forward ref to SPEC-005 |

> These PDF files are sample Surface troubleshooting documents reserved for integration tests, e2e grounding validation, and fixture generation. They are NOT ingested during Sprint 1. See SPEC-005 for the test plan that will consume them.
