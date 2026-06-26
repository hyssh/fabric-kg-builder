# scripts

Reproducible, runnable recipes for the project.

## `reproduce-surface-kg.ps1` / `reproduce-surface-kg.sh`

The **canonical end-to-end recipe** that rebuilds the Surface troubleshooting
knowledge graph from `sample_data/Surface_Troubleshootings`. It encodes every
graph-quality step learned during development, in the right order:

```
preflight → set-domain → enrich → densify → compile-data → compile-ontology →
compile-search → package → deploy-lakehouse → deploy-ontology --multitype →
deploy-search
```

Why each step matters:

| Step | What it captures |
|------|------------------|
| `set-domain` | The field-service **domain template** (entity/relationship types) + sample questions — the biggest lever on graph quality. |
| `densify` | Four **additive** passes that connect the islands per-section extraction leaves behind: (1) DeviceModel→part/procedure **hub edges**, (2) **Cause→Symptom→Resolution**, (3) **Procedure→Step** + umbrella "X Replacement Process" step rollup, (4) **RCA paths** (`diagnosed_by` / `remediated_by`). |
| `compile-data` | Builds the 8 canonical Parquet tables and runs the **additivity guard** (fails if any existing edge is dropped). |
| `deploy-ontology --multitype` | One Fabric entity type per real domain type + typed relationships, and auto-writes `data-agent-instructions.md` (grounding generated from the live graph). |

### Prerequisites

| For | You need |
|-----|----------|
| Build only (no `-Deploy`) | `pip install -e .[dev]` (the `fabric-kg` CLI), the sample PDFs (included). |
| Live deploy (`-Deploy`) | All of the above, **plus**: `az login`; an env config at `ontology/environments/<env>.json` (this file is gitignored — copy it from the committed `<env>.json.example` and fill in your Azure resource IDs); a Fabric workspace with a schema-enabled Lakehouse, Azure OpenAI/Foundry, AI Search, Document Intelligence, and Blob storage. |

The scripts run a **preflight** that checks these and fails early with a clear
message (e.g. "create `dev.json` from the template") rather than deep in the
pipeline.

### Usage

Build artifacts only (no Azure calls):

```powershell
.\scripts\reproduce-surface-kg.ps1
```

Full reproduction including live deploy to `dev`:

```powershell
az login
Copy-Item ontology\environments\dev.json.example ontology\environments\dev.json   # then edit it
.\scripts\reproduce-surface-kg.ps1 -Deploy
```

POSIX:

```bash
./scripts/reproduce-surface-kg.sh            # build only
az login
cp ontology/environments/dev.json.example ontology/environments/dev.json   # then edit it
./scripts/reproduce-surface-kg.sh --deploy --env dev
```

### After a live deploy

1. The multi-type ontology deploy is a **202 async LRO** — allow ~1–2 min to
   finish processing in Fabric.
2. Paste `data/surface_kg/data-agent-instructions.md` into your Fabric **Data Agent**.
3. Add the AI Search index (`<env>`-prefixed `kg-chunks`) as a **second data source**.
4. For a hybrid Foundry agent that fans out over the graph then searches each
   item, use [`docs/foundry-hybrid-agent-prompt.md`](../docs/foundry-hybrid-agent-prompt.md).

### Cheap iteration

Enrichment is the only slow/costly stage and is **skipped when its output
already exists** (override with `-ForceEnrich` / `--force-enrich`). Everything
after `enrich` reuses the enriched data — so refining the model (densify
options, ontology, grounding) and re-deploying takes minutes, not hours.
