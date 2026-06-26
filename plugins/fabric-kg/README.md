# fabric-kg — Copilot CLI plugin

Use the [`fabric-kg`](https://github.com/hyssh/fabric-kg-builder) knowledge-graph
builder directly from [GitHub Copilot CLI](https://docs.github.com/copilot/how-tos/use-copilot-agents/use-copilot-cli).
The plugin teaches Copilot how to drive the installed `fabric-kg` CLI to turn
documents into a deployed Microsoft Fabric knowledge graph (Lakehouse +
multi-type Ontology + Azure AI Search + Data Agent grounding).

## What's inside

| Component | Type | Purpose |
|-----------|------|---------|
| `fabric-kg-pipeline` | skill | The `enrich → densify → compile → deploy` pipeline, with guidance on domain-fit modelling, densify/RCA passes, and safe deploys. |
| `surface-repro` | skill | Reproduce the canonical Surface troubleshooting RCA graph end-to-end via `scripts/reproduce-surface-kg.*`. |
| `kg-builder` | agent | A guided assistant that walks through building and deploying a graph. |

## Prerequisite

The plugin orchestrates the **`fabric-kg` CLI**, which must be installed
separately:

```bash
pip install fabric-kg-builder          # from PyPI when published
# or, from a clone of the repo:
pip install -e .
```

Verify: `fabric-kg --version`.

## Install the plugin

**From the marketplace** (recommended):

```shell
copilot plugin marketplace add hyssh/fabric-kg-builder
copilot plugin install fabric-kg@fabric-kg-builder
```

**Directly from the repo subdirectory:**

```shell
copilot plugin install hyssh/fabric-kg-builder:plugins/fabric-kg
```

**From a local clone (for development):**

```shell
copilot plugin install ./plugins/fabric-kg
```

> When developing locally, re-run `copilot plugin install ./plugins/fabric-kg`
> after edits — installed plugin components are cached.

## Verify it loaded

In a Copilot CLI session:

```
/plugin list
/skills list
/agent
```

You should see the `fabric-kg` plugin, the two skills, and the `kg-builder`
agent.

## Use it

Just ask Copilot, e.g.:

- "Build a Fabric knowledge graph from the PDFs in `./docs`."
- "Densify the graph and add RCA paths, then validate."
- "Reproduce the Surface troubleshooting graph and deploy to dev."

Or select the agent with `/agent` and choose `kg-builder`.

## Safety

The skills and agent never print or commit secrets, API keys, Azure subscription
IDs, resource group names, or workspace/lakehouse GUIDs. Those live only in
gitignored `.env` and `ontology/environments/{env}.json` files (copy from the
committed `.example` templates).
