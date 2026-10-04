# fabric-kg-builder

Build an evidence-backed Microsoft Fabric knowledge graph from your corpus.
Discover and design a domain-fit schema, evaluate it against your questions,
then explicitly approve extraction and publication. Use the `fabric-kg` Python
CLI directly or through the customer-facing GitHub Copilot CLI plugin.

**Development version: 0.2.7.** Install from source; this is not a claim of a
published PyPI release.

[Project site](https://hyssh.github.io/fabric-kg-builder/) ·
[Changelog](CHANGELOG.md) ·
[Questions and issues](https://github.com/hyssh/fabric-kg-builder/issues)

See a [user-supplied Data Agent example](https://hyssh.github.io/fabric-kg-builder/#example)
for Surface Go 2 tools, components, and precautions. It is illustrative, not a
benchmark or evidence of Leiden answer-quality gains. Verify the source service
guide before performing any repair.

## What changed in 0.2.7

| Area | Change | What it helps with |
| --- | --- | --- |
| New: native Hierarchical Leiden | Opt-in community derivation from sealed, validated L4; immutable, separately receipted output. | Organizes existing graph facts without changing approved inputs. |
| New: grouping and conversion | Optional abstract community bases in Ontology; offline conversion of emitted parts to v2 TMDL. | Explore display grouping and a conversion path while keeping default publication on v1. |
| Fix: relationship fidelity | Serialized direction/context witness 1.3.0 fails closed, with historical compatibility. | Keeps relationship direction and context accountable through serialization. |
| Improvement: heading context | Bounded ancestor headings in default extraction; cached layout headings carry across pages. | Gives extraction more context without treating headings as evidence. |
| Fix: endpoint and identity checks | Reject cross-leaf endpoints and duplicate local IDs; correct re-extraction member fragment keys. | Prevents invalid links and last-wins loss when an entity has different approved types per leaf. |
| Fix: discovery resume | Discard stale prepared cache on resume. | Avoids reusing outdated discovery inputs. |
| Improvement: schema capacity | Configurable relationship-type range, still 8–20 by default, with compiler capacity support. | Fits the schema to the corpus rather than a fixed range. |
| Improvement: publication values | Safe native date mapping; scoped, deterministic property variant coalescing; opt-in evidence-grounded label fallback for missing required non-key strings. | Reduces avoidable publication failures without inventing unsupported values. |

New headings apply to the default extraction path, not historical approved
re-extraction, donor continuation, or discovery-reuse paths. Label fallback is
explicitly opt-in and does not supply missing keys.

## Install from source

Use a fresh clone of the new public repository:

```bash
git clone https://github.com/hyssh/fabric-kg-builder.git
cd fabric-kg-builder
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
fabric-kg --version
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell.
The project requires Python **3.10+**. For native Hierarchical Leiden, use
**Python 3.10–3.13** and install the optional extra:

```bash
python -m pip install -e '.[leiden]'
```

This pins `graspologic-native==1.2.5`. Python 3.14 is not supported by the
native provider; selecting the extra does not make it available.

### Prerequisites depend on the task

- **Local hierarchy and offline conversion:** installed CLI and the required
  existing artifacts. No Azure credentials, `.env`, or remote calls are needed.
- **OCR and enrichment:** configured services, authentication, and explicit
  spending budget/approval. Use `.env.example` as configuration guidance and
  keep credentials in a local, uncommitted `.env`.
- **Fabric publication:** an authorized workspace and the required Azure
  credentials, plus an explicitly reviewed publication plan.
- **Copilot orchestration:** GitHub Copilot CLI and the separately installed
  plugin. The plugin does not install the Python CLI.

## Corpus-first workflow

The pipeline is domain-agnostic: the corpus and approved questions shape the
schema, rather than a fixed set of entity types.

```text
Corpus → discover → design → evaluate → explicit approval
                                            ↓
                                    L2 extraction
                                            ↓
                                    L3 evidence validation
                                            ↓
                                    sealed L4 serving projection
                                            ├→ optional derived hierarchy
                                            └→ approved prototype publication
```

Discovery collects reusable observations; design proposes the schema.
Evaluate question gaps and revise before approval. Extraction produces L2,
evidence validation checks L3, and serving projection seals L4 for downstream
use. Compilation and publication remain separate from schema approval.

Use command help for the input contracts and approval flags of your installed
version:

```bash
fabric-kg domain discover --help
fabric-kg domain design --help
fabric-kg enrich --help
fabric-kg validate-evidence --help
fabric-kg project-serving --help
```

## Native Hierarchical Leiden

Derive a hierarchy **only after** validating evidence and sealing L4.
The following example assumes `.fkg/l4/approved-run` already exists, its
validated input manifests are under `.fkg/l3`, and the output directory is new.
Replace those paths with your artifacts; this command does not build the source.

```bash
fabric-kg derive-hierarchy \
  --source .fkg/l4/approved-run \
  --input-manifest-search-root .fkg/l3 \
  --out .fkg/derived-hierarchy/communities-1 \
  --run-id hier-run-1 --policy-id default \
  --seed 3735928559 --max-cluster-size 10
```

### What it guarantees

- Uses a sorted, undirected partition projection while preserving original
  directed/typed relationship rows, IDs, and evidence.
- Covers all connected components and isolates. Isolates become singleton
  communities; duplicate IDs and unknown endpoints are rejected.
- Validates exactly-once terminal membership and deterministic community
  identity, with ancestor evidence and SHA256 graph/evidence/config/seed binding.
- Writes a separately receipted, immutable **three-Parquet derived artifact**.
  Sealed inputs are never mutated and the operation makes no remote calls.
- Reports explicit `empty`, `insufficient`, `native_unavailable`, or `failed`
  states when appropriate. There is no fallback algorithm.

`--max-cluster-size` is a **split trigger, not a hard cap**: oversized terminal
communities are retained and flagged. `COMPLETE` means structural validity,
not semantic usefulness.

### What it does not establish

Leiden organizes **existing facts**. It does not restore missing relationships,
discover procedural order, prove semantic inheritance, or establish gains in
answer accuracy. A/B answer-quality evaluation is deferred.

Optional `--ontology-inheritance` projects communities as abstract
`group_<dominant type>` bases for display grouping. These are not assertions of
true “is-a” meaning; do not interpret topology as domain semantics.

## Fabric publication and v2 conversion

The current default structured publisher creates a **Lakehouse + Ontology**,
not a standalone GraphModel. The platform-managed Ontology companion graph
still exists.

The prototype Fabric Data Agent uses **Ontology v1 as its sole structured
source**. Its deployed GQL instructions use `FILTER`, not `WHERE`, entity and
relationship type label names, backtick-quoted `label`, and explicit guidance
not to assert unsupported facts. There is no Lakehouse SQL source in this
default Agent configuration.

Azure AI Search is a separate capability, not the primary structured pipeline
and not automatically attached to the ontology-only Agent.

```bash
fabric-kg app publish-structured --help
fabric-kg app publish-prototype-agent --help
fabric-kg app convert-ontology-v2 --help
```

Publication is an explicitly approved, **create-only prototype**. Review the
default dry-run plan before a live call; live publication requires the exact
plan hash via `--approve-live`. Optional hierarchy publication uses
`--hierarchy-state`; grouping additionally requires `--ontology-inheritance`.

Publication is **nontransactional**: partial resources are retained, with no
automatic rollback. A draft Agent is not a completed answer-quality evaluation.
General transactional, fully automated production deployment is not verified.

`convert-ontology-v2` converts emitted Ontology parts to TMDL offline,
preserving inheritance and bindings. It is not full default v2 deployment or
v2 Data Agent support.

## Use with GitHub Copilot CLI

Install the CLI above first, then add the customer-facing plugin:

```bash
copilot plugin marketplace add hyssh/fabric-kg-builder
copilot plugin install fabric-kg@fabric-kg-builder
```

In a Copilot CLI session, check `/plugin list` and `/skills list`, or use
`/agent` to select `kg-builder`. Ask it to discover a schema from your corpus,
evaluate question gaps, and prepare a plan for review. Keep model spending,
schema approval, extraction, and publication as distinct decisions.

See [plugin installation and commands](plugins/fabric-kg/README.md) for
additional installation options. For 0.2.7, use the source-install instructions
above rather than assuming a PyPI package is available.

## Safety and limits

Evidence grounding and fail-closed validation do not guarantee complete
extraction, injection immunity, or correct business answers. Review unsupported
facts and gaps, test representative questions, and assess answer quality
separately before relying on a deployed Agent.

Keep secrets and customer data out of commits. Review scopes, budgets, plans,
and retained partial resources before remote work. Local derivation is not
authorization for cloud publication.

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest
```

The default suite excludes slow and integration tests. Select those deliberately
when their data and environment prerequisites are available; use
`python -m pytest --help` for runner options.
