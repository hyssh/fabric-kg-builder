---
name: surface-repro
description: Reproduce the Surface troubleshooting knowledge graph end-to-end as a worked example of fabric-kg. Use when the user wants to run the canonical Surface RCA demo, rebuild the sample graph from sample_data/Surface_Troubleshootings, see a working example of densify + RCA paths, or test the Fabric Data Agent / hybrid Foundry agent against a real graph.
---

## What this skill does

Reproduces the **Surface field-service troubleshooting** knowledge graph — the
project's canonical worked example. It rebuilds the graph from the bundled
Surface repair PDFs and encodes every graph-quality lesson (domain template,
densify passes, RCA paths, multi-type ontology, Data Agent grounding) in the
correct order.

Prefer the ready-made reproduction script over hand-typing the pipeline.

## Run it

From a clone of `https://github.com/hyssh/fabric-kg-builder` with the
`fabric-kg` CLI installed (`pip install -e .[dev]`):

**Build artifacts only (no Azure calls):**

```powershell
.\scripts\reproduce-surface-kg.ps1
```

```bash
./scripts/reproduce-surface-kg.sh
```

**Full reproduction including live deploy to `dev`:**

```powershell
az login
Copy-Item ontology\environments\dev.json.example ontology\environments\dev.json   # then edit it
.\scripts\reproduce-surface-kg.ps1 -Deploy
```

```bash
az login
cp ontology/environments/dev.json.example ontology/environments/dev.json          # then edit it
./scripts/reproduce-surface-kg.sh --deploy --env dev
```

The script runs a **preflight** that checks prerequisites and fails early with a
clear message (e.g. "create `dev.json` from the template") instead of failing
deep in the pipeline.

## What the recipe encodes

```
preflight → set-domain → enrich → densify → compile-data → compile-ontology →
compile-search → package → deploy-lakehouse → deploy-ontology --multitype →
deploy-search
```

| Step | Why it matters |
|------|----------------|
| `set-domain` | The field-service **domain template** (entity/relationship types) + sample questions — the biggest lever on graph quality. |
| `densify` | Four **additive** passes that connect the islands per-section extraction leaves behind: (1) DeviceModel→part/procedure hub edges, (2) Cause→Symptom→Resolution, (3) Procedure→Step + umbrella "X Replacement Process" step rollup, (4) RCA paths (`diagnosed_by` / `remediated_by`). |
| `compile-data` | Builds the 8 canonical Parquet tables and runs the **additivity guard** (fails if any existing edge is dropped). |
| `deploy-ontology --multitype` | One Fabric entity type per real domain type + typed relationships, and auto-writes `data-agent-instructions.md` (grounding generated from the live graph). |

## After a live deploy

1. The multi-type ontology deploy is a **202 async LRO** — allow ~1–2 min to finish in Fabric.
2. Paste `data/surface_kg/data-agent-instructions.md` into your Fabric **Data Agent**.
3. Add the AI Search index (`<env>`-prefixed `kg-chunks`) as a **second data source**.
4. For a hybrid Foundry agent that fans out over the graph then searches each item, use `docs/foundry-hybrid-agent-prompt.md`.

## Cheap iteration

Enrichment is the only slow/costly stage and is **skipped when its output
already exists** (override with `-ForceEnrich` / `--force-enrich`). Everything
after `enrich` reuses the enriched data, so refining the model and re-deploying
takes minutes, not hours.

## Example questions to test the graph

- "Give me the full root-cause analysis for battery expansion: cause, how to diagnose, the repair steps, and the resolution."
- "What components does the Surface Pro 10 for Business have?"
- "What steps are in the display replacement procedure?"
- "What can cause battery expansion and how is it resolved?"

See `sample_data/surface_questions.txt` for the full curated set.

## Safety

Never print or commit Azure subscription IDs, resource group names, workspace or
lakehouse GUIDs, or any secret. They live only in gitignored `.env` and
`ontology/environments/{env}.json` files.
