# fabric-kg — Copilot CLI plugin

Use the [`fabric-kg`](https://github.com/hyssh/fabric-kg-builder) knowledge-graph
builder directly from [GitHub Copilot CLI](https://docs.github.com/copilot/how-tos/use-copilot-agents/use-copilot-cli).
The plugin guides Copilot through corpus-first design, approved extraction,
evidence validation, and explicitly approved Fabric prototype publication.
The current structured path is Lakehouse + Ontology, followed by a Data Agent
using Ontology v1 as its sole structured source. Azure AI Search is a separate
capability, not an automatically attached Agent source.

**Development version: 0.2.7.** Install the Python CLI from source.

## What's inside

| Component | Type | Purpose |
|-----------|------|---------|
| `fabric-kg-pipeline` | skill | Schema-2 proposal, document assessment, reviewed revision, approval, extraction and local projection; deployment is separately capability-gated. |
| `surface-repro` | skill | Optional Surface service-guide example using user-downloaded documents and the current approved pipeline. |
| `kg-builder` | agent | A guided assistant that separates design, model spending, approval, extraction and deployment. |

## Prerequisite

The plugin orchestrates the **`fabric-kg` CLI**, which must be installed
separately:

```bash
git clone https://github.com/hyssh/fabric-kg-builder.git
cd fabric-kg-builder
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell.
Verify with `fabric-kg --version`. Do not assume a published PyPI package.
For optional native Hierarchical Leiden, use Python 3.10–3.13 and install
`python -m pip install -e '.[leiden]'`; Python 3.14 is not supported by that
provider. The plugin itself does not install the Python CLI.

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

- "Discover a schema from the PDFs in my local corpus and evaluate these questions."
- "Assess these documents against the draft ontology and prepare a reviewed revision."
- "Prepare a Surface service-guide example; show the plan before model calls or publication."

Or select the agent with `/agent` and choose `kg-builder`.

Check command help in the installed build before constructing arguments.
Model spending, schema approval, extraction, hierarchy derivation, and cloud
publication are separate decisions. Local projection is not live deployment;
the explicitly approved create-only publisher is nontransactional and retains
partial resources without automatic rollback.

The Surface example requires service guides downloaded by the user. No PDF
corpus, pre-extracted graph, or historical reproduction script is bundled.
See the [source setup guide](../../sample_data/Surface_Troubleshootings/README.md).

## Safety

Do not print or commit credentials, customer documents, or resource-specific
configuration. Keep these in local, uncommitted files and review scopes,
budgets, and exact publication plans. Plugin instructions are not a guarantee
of injection immunity, complete extraction, or correct answers.
