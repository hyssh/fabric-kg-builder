---
name: fabric-kg-pipeline
description: Build and deploy a Microsoft Fabric knowledge graph from documents using the fabric-kg CLI. Use when the user wants to turn PDFs/DOCX/HTML/CSV into a Fabric Lakehouse + Ontology + AI Search, run the enrich → densify → compile → deploy pipeline, densify a sparse graph, add RCA paths, or generate Data Agent grounding instructions.
---

## What this skill does

`fabric-kg` is an installed Python CLI (entry point `fabric-kg`) that converts
raw documents and CSVs into a fully deployed Microsoft Fabric knowledge graph:
canonical Parquet tables in a Fabric Lakehouse, a multi-type Ontology, and Azure
AI Search indexes for hybrid retrieval, plus grounding instructions for a Fabric
Data Agent.

Use this skill to help the user run the pipeline. Always invoke the real
`fabric-kg` CLI via the shell — do not re-implement its behavior.

## Prerequisite check (do this first)

Confirm the CLI is installed before running pipeline steps:

```bash
fabric-kg --version
```

If it is not found, tell the user to install it:

```bash
pip install fabric-kg-builder          # from PyPI when published
# or, from a clone of https://github.com/hyssh/fabric-kg-builder:
pip install -e .
```

Deploy steps additionally need Azure auth (`az login`) and per-environment
resource IDs in `ontology/environments/{env}.json` (never commit secrets — a
`.example` template is provided). Build/compile steps run fully offline.

## The pipeline (run in order)

| # | Command | Purpose |
|---|---------|---------|
| 1 | `fabric-kg set-domain` | Persist a domain brief so the LLM understands the data. `--industry` and `--business-domain` are REQUIRED; `--questions-file` is the biggest lever on ontology quality. |
| 2 | `fabric-kg inspect-source` | Profile source files before enrichment. |
| 3 | `fabric-kg enrich --input <dir>` | LLM extraction → `build/enriched/` canonical JSON. |
| 4 | `fabric-kg densify` | **RECOMMENDED** — add DeviceModel hub edges, Cause/Symptom/Resolution, Procedure→Step, and RCA paths → `build/enriched_dense/`. Strictly additive. |
| 5 | `fabric-kg compile-data --input build/enriched_dense` | Enriched JSON → 8 canonical Parquet tables (`build/parquet/`). |
| 6 | `fabric-kg compile-ontology` | `ontology/model.yaml` → Fabric Ontology definition (`build/ontology/`). |
| 7 | `fabric-kg compile-search` | Parquet → AI Search schemas + doc batches (`build/search/`). |
| 8 | `fabric-kg package` | Bundle all build artifacts → `dist/`. |
| 9 | `fabric-kg deploy-lakehouse --env dev` | Upload Parquet Delta tables to Fabric OneLake. |
| 10 | `fabric-kg deploy-ontology --env dev --multitype` | Push the Ontology (`--multitype` = rich typed graph in Explorer). |
| 11 | `fabric-kg deploy-search --env dev` | Push AI Search index schemas and documents. |
| 12 | `fabric-kg validate` | Run the VAL + BRG gate catalog against build artifacts. |
| — | `fabric-kg build-deploy` | End-to-end convenience wrapper for all stages. |

Global options apply to every subcommand: `--env [dev|test|prod]` (default
`dev`), `--config PATH` (default `./fabric-kg.yaml`), `-v/--verbose`,
`-q/--quiet`, and `--dry-run` (show the plan without making changes).

Run any subcommand with `--help` to see its options, defaults, and an example
before executing — e.g. `fabric-kg densify --help`.

## Key guidance

- **Graph quality depends on a domain-fit model.** Start from a domain template:
  define entity types (nodes) and relationships (typed edges), supply 3–5 sample
  questions via `--questions-file`, then iterate.
- **Always run `densify` between `enrich` and `compile-data`.** A sparse graph
  causes the Fabric Data Agent to fall back to generic LLM answers. Densify links
  isolated symptoms, builds Cause→Symptom→Resolution chains, rolls up umbrella
  procedure steps, and adds `diagnosed_by`/`remediated_by` RCA paths. It is
  strictly additive — it never removes existing edges.
- **Use `--multitype` when deploying the ontology** for a rich typed graph in the
  Fabric Explorer.
- **Mock vs live:** deploy commands accept `--mock`/`--no-mock` (and the global
  `--dry-run`) so the user can rehearse safely before hitting live resources.
- **Multi-type ontology deploys are async** (`202` long-running operation,
  ~1–2 min). Let the command poll; don't assume instant completion.

## Safety

- Never print or commit secrets, API keys, Azure subscription IDs, resource group
  names, or workspace/lakehouse GUIDs. These live in `.env` and
  `ontology/environments/{env}.json`, both gitignored.
- Prefer `--dry-run` or `--mock` first when the user is unsure about a deploy.

## Typical request → response

When the user says "build the knowledge graph from these docs", confirm the
input directory, then propose the ordered command sequence (steps 1–8 offline,
9–12 for deploy), run the prerequisite check, and execute step by step —
surfacing each command's output and stopping on any non-zero exit.
