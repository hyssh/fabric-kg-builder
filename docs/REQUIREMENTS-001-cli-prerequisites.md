# REQUIREMENTS-001: CLI Prerequisites — Setting Up to Run fabric-kg

**Status:** Draft  
**Date:** 2026-06-24T15:41:07.842-07:00  
**Author:** Verbal (AI Integration Dev)  
**Related specs:** SPEC-001-architecture-and-cli.md §5 (config), §7 (CLI); SPEC-004-llm-enrichment.md §9 (model config); INFRA-001-azure-resources.md

### Revision History

| Date | Author | Summary |
|---|---|---|
| 2026-06-24T15:41:07.842-07:00 | Verbal | Initial draft — prerequisites, RBAC table, .env guide, Foundry 200K TPM requirement, Fabric prerequisites, verify checklist, Sprint-1 quickstart. |
| 2026-06-24T15:41:07.842-07:00 | Verbal | Added fabric-cicd as required deploy tool (§4); renamed Lakehouse `fabrickg_lakehouse` → `kg_lakehouse`; added required-tools table to overview; updated RBAC, Fabric prereqs, verify checklist. |

---

## 1. Overview

Before you can run any `fabric-kg` CLI command, you need:

- **Azure CLI** installed and authenticated (`az login`)
- **Python 3.10+** with `fabric-cicd` installed (required for all deploy commands — see §4)
- **Access** to each Azure resource the pipeline touches (Foundry, Fabric, Blob, AI Search, Document Intelligence)
- A **`.env` file** at the project root that holds endpoint URLs (and optionally API keys for non-DefaultAzureCredential flows)
- **Foundry model deployments** provisioned with sufficient capacity — specifically, the enrichment deployment must be **≥ 200,000 TPM (200K TPM)** to run the enrich stage without throttling at scale
- **Fabric workspace + Lakehouse** access (the `kg_lakehouse` item is already provisioned for dev; Tables/Files paths and SQL endpoint are captured in `ontology/environments/dev.json`)

### Required tools at a glance

| Tool | Purpose | Install / Config |
|---|---|---|
| **Azure CLI** | `DefaultAzureCredential` / `az login` identity | §2–§3 |
| **Python 3.10+** | Runtime for `fabric-kg` CLI | §4 |
| **fabric-cicd** | Primary deploy mechanism (`deploy-lakehouse`, `deploy-ontology`, `deploy-search`) | §4 |
| **Fabric workspace + `kg_lakehouse`** | Target for Lakehouse deployments | §8 |
| **Azure AI Foundry ≥ 200K TPM** | LLM enrichment at scale | §7 |
| **Azure AI Search** | Search index (IN MVP scope) | §5, INFRA-001 |
| **Azure Document Intelligence** | PDF/image OCR extraction | §5, INFRA-001 |

> **Auth note:** `fabric-cicd`, `DefaultAzureCredential`, and `az login` all share the same identity session. No separate keys are required in dev.

This guide walks through each requirement. **Secrets (API keys, SPN credentials) must never be committed to source control.** See `.copilot/skills/secret-handling/SKILL.md` for the team's hard rules.

---

## 2. Install Azure CLI (Windows)

The Azure CLI is required so `DefaultAzureCredential` can pick up your identity via `az login`. It is the only credential you need in dev — no keys in code.

### Option A — winget (recommended)

```powershell
winget install --id Microsoft.AzureCLI --source winget
```

### Option B — MSI installer

Download from: <https://aka.ms/installazurecliwindows>  
Run the downloaded `.msi` and follow the installer prompts.

### Verify installation

```powershell
az version
```

Expected output (versions may differ):

```json
{
  "azure-cli": "2.x.x",
  "azure-cli-core": "...",
  "azure-cli-telemetry": "..."
}
```

---

## 3. Authenticate with Azure CLI

### Log in

```powershell
az login
```

A browser window opens. Sign in with the account that has access to the `Example-Subscription` subscription.

### Set the correct subscription

```powershell
az account set --subscription "Example-Subscription"
```

Verify you are on the right subscription:

```powershell
az account show --query "{Name:name, ID:id, State:state}" -o table
```

### How DefaultAzureCredential uses this login

`fabric-kg` uses `DefaultAzureCredential` (from `azure-identity`) for all Azure calls. In the dev flow, `DefaultAzureCredential` automatically picks up the token from `az login` — **no API keys are required in your `.env` for dev**. The credential chain tries (in order):

1. Environment variables (`AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID`) — used by CI/prod Service Principals
2. Managed Identity — used when running inside Azure
3. Azure CLI (`az login`) — **the dev path** — no secrets needed

As long as you are `az login`'d to the correct account and subscription, `DefaultAzureCredential` resolves transparently.

---

## 4. Install Python and fabric-cicd

### Python 3.10+

`fabric-kg` requires **Python 3.10 or later**. Install from [python.org](https://www.python.org/downloads/) or via winget:

```powershell
winget install --id Python.Python.3 --source winget
```

Verify:

```powershell
python --version
```

### Install fabric-cicd (REQUIRED for deploy commands)

`fabric-cicd` is the **primary deployment mechanism** for all `fabric-kg` deploy stages. It is **required** — not optional — to run:

| Deploy command | Role of fabric-cicd |
|---|---|
| `deploy-lakehouse` | ✅ Primary mechanism |
| `deploy-ontology` | ✅ Primary mechanism |
| `deploy-search` | ✅ Primary mechanism |

> The Fabric REST API is used only as a fallback for item-level operations that `fabric-cicd` cannot handle. All Git-driven, reproducible deployments go through `fabric-cicd`.

Install into your virtual environment:

```powershell
pip install fabric-cicd
```

Verify:

```powershell
python -c "import fabric_cicd; print(fabric_cicd.__version__)"
```

### Authentication — no extra keys needed

`fabric-cicd` uses the same `DefaultAzureCredential` / `az login` session as the rest of the pipeline. Once you have run `az login` (§3), `fabric-cicd` picks up your token automatically. **No separate API keys or service principal credentials are required in dev.**

---

## 5. Required Azure Access (RBAC)

The table below lists the minimum role assignment each signed-in user (or Service Principal) needs to run the full `fabric-kg` pipeline.

| Azure Resource | Required Role | Why |
|---|---|---|
| **Azure AI Foundry** (`example-aiservices`) | `Cognitive Services OpenAI User` | Call the gpt-5-4-mini (gpt-5.4-mini) and embedding Foundry deployments |
| **Fabric workspace** (`9802a28a-...`) | `Contributor` or `Member` | Read workspace metadata, deploy ontology, upload data to Lakehouse |
| **Fabric Lakehouse** (`kg_lakehouse`) | `Contributor` | Write Parquet tables to the Lakehouse via fabric-cicd (Fabric REST API as fallback) |
| **Azure Blob Storage** (`examplestorageacct`) | `Storage Blob Data Contributor` | Upload visual assets (images, figures) referenced by `blob_url` in Parquet |
| **Azure AI Search** (`example-search`) | `Search Index Data Contributor` + `Search Service Contributor` | Create/update search indexes and upload document batches |
| **Azure AI Document Intelligence** (`example-docintell`) | `Cognitive Services User` | Run layout/OCR extraction on PDF and image source files |

> **Note:** These roles apply to the `Example-Subscription` subscription, resource group `example-rg`. For CI/prod, replace the signed-in user with the Service Principal; same roles apply.

---

## 6. The `.env` File

`fabric-kg` reads endpoint URLs and — when not using `DefaultAzureCredential` — API keys from a `.env` file at the project root. **This file is gitignored and must never be committed.**

### What goes in `.env`

- **Endpoint URLs** (environment-specific, not secrets per se, but excluded from committed yaml by convention — see SPEC-001 §5.2)
- **API keys** (only if you are not using `DefaultAzureCredential` — keys are optional in dev)
- **Service Principal credentials** (for CI/prod only)

### `.env.example` — safe reference schema (no real values)

```dotenv
# .env — project root (GITIGNORED — never commit real values)
# Copy this file to .env and fill in your values.
# In DEV: DefaultAzureCredential (az login) covers most auth.
#   API keys are optional if you are az-login'd to the correct account.

# ── Azure AI Foundry (LLM + vision + embedding) ──────────────────────────────
AZURE_AI_FOUNDRY_ENDPOINT=https://<account>.services.ai.azure.com
# API key — optional in dev (DefaultAzureCredential preferred)
AZURE_AI_FOUNDRY_API_KEY=<your-foundry-api-key-or-leave-blank-for-dac>
AZURE_AI_FOUNDRY_PROJECT=example-project

# ── Azure AI Document Intelligence ───────────────────────────────────────────
AZURE_DOCINTEL_ENDPOINT=https://<resource>.cognitiveservices.azure.com
# API key — optional in dev
AZURE_DOCINTEL_KEY=<your-doc-intel-key-or-leave-blank-for-dac>

# ── Azure Blob Storage ────────────────────────────────────────────────────────
# Use connection string OR SAS token (one of the two)
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;AccountName=<account>;...
# AZURE_STORAGE_SAS_TOKEN=<sas-token>

# ── Azure AI Search (required — IN MVP scope) ─────────────────────────────────
AZURE_SEARCH_ENDPOINT=https://<service>.search.windows.net
# API key — optional in dev (DefaultAzureCredential preferred)
AZURE_SEARCH_KEY=<your-search-admin-key-or-leave-blank-for-dac>

# ── Fabric / Service Principal (CI/prod only — omit in dev) ──────────────────
# FABRIC_CLIENT_ID=<service-principal-client-id>
# FABRIC_CLIENT_SECRET=<service-principal-secret>
# FABRIC_TENANT_ID=<azure-ad-tenant-id>
```

> **DefaultAzureCredential is preferred.** In dev, once you have run `az login`, you can leave the API key variables blank or omit them entirely. Keys in `.env` are a fallback for environments where managed identity / az-login is not available.

### `.gitignore` must include

```
.env
.env.*
!.env.example
```

---

## 7. Foundry Deployment Prerequisites

### ⚠️ 200K TPM Minimum Requirement

The `enrich` pipeline stage sends high-volume concurrent requests to the Foundry chat/enrichment deployment. **The deployment must be provisioned at ≥ 200,000 TPM (tokens per minute)** to avoid rate-limit throttling during a full corpus run.

> The dev deployment `gpt-5-4-mini` is provisioned at **capacity 200 = 200K TPM** (GlobalStandard) on `example-aiservices`. This satisfies the requirement. **Do not lower this capacity.**

### Required deployments

| Deployment name | Model | Capacity | Tier | Purpose |
|---|---|---|---|---|
| `gpt-5-4-mini` | **gpt-5.4-mini** | **200K TPM (capacity 200)** | GlobalStandard | Chat / enrichment — **default** |
| `chat` | gpt-4.1 | — | — | Fallback chat deployment |
| `embedding` | text-embedding-3-large @ dimensions=1536 | — | — | Vector embeddings for AI Search |

### Provisioning example — gpt-5-4-mini at 200K TPM

This is how the dev deployment was created (shown for reference; already done for `example-aiservices`):

```bash
az cognitiveservices account deployment create \
  --resource-group example-rg \
  --name example-aiservices \
  --deployment-name gpt-5-4-mini \
  --model-name gpt-5.4-mini \
  --model-version 2026-03-17 \
  --model-format OpenAI \
  --sku-name GlobalStandard \
  --sku-capacity 200
```

`--sku-capacity 200` = 200K TPM.

### Verify deployments are present

```bash
az cognitiveservices account deployment list \
  --resource-group example-rg \
  --name example-aiservices \
  --query "[].{name:name, model:properties.model.name, capacity:sku.capacity, state:properties.provisioningState}" \
  -o table
```

Expected output (verify `gpt-5-4-mini` is present at capacity 200 and `Succeeded`):

```
Name           Model            Capacity    State
-------------  ---------------  ----------  ---------
gpt-5-4-mini   gpt-5.4-mini     200         Succeeded
chat           gpt-4.1          ...         Succeeded
embedding      text-embedding-3-large  ...  Succeeded
```

### Embedding deployment

The embedding deployment must use **text-embedding-3-large** with `dimensions=1536`. This value is **locked** — it is coupled to the `chunk_vector` field width in the AI Search index. Changing it requires a full reindex of all AI Search data.

---

## 8. Microsoft Fabric Prerequisites

### Workspace access

You (or the SPN) must have **Contributor** or **Member** access in the Fabric workspace:

- **Workspace ID:** `11111111-1111-1111-1111-111111111111`

Request access from your workspace admin if you are not a member.

### Lakehouse

The `kg_lakehouse` item is already created for dev:

| Attribute | Value |
|---|---|
| **Display name** | `kg_lakehouse` |
| **Item ID** | `44444444-4444-4444-4444-444444444444` |
| **Workspace ID** | `11111111-1111-1111-1111-111111111111` |

These values are already wired into `ontology/environments/dev.json`. No manual configuration needed for the dev environment.

### OneLake access

`deploy-lakehouse` uploads Parquet tables to the Lakehouse via **fabric-cicd** (primary) or the Fabric REST API (fallback) using `DefaultAzureCredential`. Ensure your account has access to write to OneLake for the workspace above. OneLake Tables/Files paths and the SQL Analytics Endpoint are available in `ontology/environments/dev.json`.

---

## 9. Verify-Your-Setup Checklist

Run these checks before your first `fabric-kg` command. All should succeed.

### 9.1 Confirm subscription context

```powershell
az account show --query "{Name:name, ID:id, State:state}" -o table
```

Expected: `Example-Subscription` is active.

### 9.2 Verify Fabric API access

Get a token for the Fabric API:

```powershell
az account get-access-token --resource https://api.fabric.microsoft.com `
  --query "{token:accessToken, expires:expiresOn}" -o table
```

Expected: a JWT token with a future expiry.

### 9.3 List Foundry deployments

```powershell
az cognitiveservices account deployment list `
  --resource-group example-rg `
  --name example-aiservices `
  --query "[].{name:name, capacity:sku.capacity, state:properties.provisioningState}" `
  -o table
```

Expected: `gpt-5-4-mini` at capacity 200 and state `Succeeded`.

### 9.4 Verify AI Search service reachable

```powershell
az search service show `
  --resource-group example-rg `
  --name example-search `
  --query "{name:name, status:properties.status, sku:sku.name}" `
  -o table
```

Expected: status `running`.

### 9.5 Verify Document Intelligence service reachable

```powershell
az cognitiveservices account show `
  --resource-group example-rg `
  --name example-docintell `
  --query "{name:name, state:properties.provisioningState}" `
  -o table
```

Expected: state `Succeeded`.

### 9.6 Confirm `.env` file exists and is not committed

```powershell
Test-Path .env
git check-ignore -v .env
```

Expected: `True` for `Test-Path`; `git check-ignore` should print the `.gitignore` rule that covers `.env`.

### 9.7 Verify fabric-cicd is installed

```powershell
python -c "import fabric_cicd; print(fabric_cicd.__version__)"
```

Expected: a version string (e.g., `0.1.x`). If you see `ModuleNotFoundError`, run `pip install fabric-cicd`.

---

## 10. Quickstart — Sprint-1 Demo Command Sequence

The Sprint-1 demo runs the core pipeline: domain intake → source inspection → enrichment → data compilation → ontology compilation → package. These are the canonical command names from SPEC-001 §7.

> **Prerequisite:** Complete all steps in §2–§9 above. Activate your Python virtual environment and install the CLI (`pip install -e .`).

### Step 0 — Set your domain

```powershell
fabric-kg set-domain `
  --prompt "Surface laptop service documentation — extract hardware components, part numbers, procedures, and safety warnings" `
  --env dev
```

This writes `build/enriched/domain.json` to anchor all LLM extraction.

### Step 1 — Inspect source files

```powershell
fabric-kg inspect-source `
  --input ./sources `
  --format table `
  --env dev
```

Review the schema profile. Fix any unsupported file types before proceeding.

### Step 2 — Enrich (LLM extraction)

```powershell
fabric-kg enrich `
  --input ./sources `
  --env dev
```

> **Note:** This stage calls `gpt-5-4-mini` (gpt-5.4-mini @ 200K TPM) on Foundry. Requires `az login` and Cognitive Services OpenAI User role on `example-aiservices`. Use `--resume` to continue after interruption; `--sample N` to test on a small subset first.

### Step 3 — Compile data (Parquet)

```powershell
fabric-kg compile-data `
  --env dev
```

Converts `build/enriched/*.json` to 8 canonical Parquet tables in `build/parquet/`.

### Step 4 — Compile ontology

```powershell
fabric-kg compile-ontology `
  --env dev
```

Generates Fabric Ontology definition parts in `build/ontology/` from `ontology/model.yaml` + `ontology/ids.lock.json`.

### Step 5 — Package

```powershell
fabric-kg package `
  --env dev
```

Bundles all build artifacts into `dist/` ready for deployment.

### Optional: full build-deploy in one command

```powershell
fabric-kg build-deploy `
  --input ./sources `
  --env dev `
  --skip-search
```

> Use `--skip-search` if you are not deploying to AI Search in this run. Remove the flag to include AI Search index deployment (AI Search is IN MVP scope — see INFRA-001).

---

*End of REQUIREMENTS-001*
