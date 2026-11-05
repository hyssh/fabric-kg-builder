# fabric-kg-builder

Build an evidence-backed Microsoft Fabric knowledge graph from your corpus.
Discover and design a domain-fit schema, evaluate it against your questions,
then explicitly approve extraction and publication. Use the `fabric-kg` Python
CLI directly or through the customer-facing GitHub Copilot CLI plugin.

**Development version: 0.2.8.** Install from source; this is not a claim of a
published PyPI release.

[Project site](https://hyssh.github.io/fabric-kg-builder/) ·
[Changelog](CHANGELOG.md) ·
[Questions and issues](https://github.com/hyssh/fabric-kg-builder/issues)

See a [user-supplied Data Agent example](https://hyssh.github.io/fabric-kg-builder/#example)
for Surface Go 2 tools, components, and precautions. It is illustrative, not a
benchmark or evidence of Leiden answer-quality gains. Verify the source service
guide before performing any repair.

## What changed in 0.2.8

The additive `lineage archive` and `lineage bind-publication` commands connect
historical evidence/source definitions to the exact workspace, Lakehouse,
table, and Delta version recorded by a completed create-only prototype journal.
`lineage resolve-stored` checks that binding and can explicitly read back the
pinned OneLake table and record before returning provenance. Remote reads
require `--read-remote --approve-read BINDING_HASH`; by default remote verification
is **NOT_RUN**, and a local snapshot is never a live publication version.
Existing DI, sealed evidence, native hierarchy, and publication approval remain unchanged.

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

## Experimental Graphify import review

`review-graphify-import` is an **offline, report-only interoperability check**,
not a Graphify runtime integration, extraction validator, hierarchy builder,
or performance/quality improvement. Native Leiden remains unchanged.
It reads an already-created [Graphify export](https://github.com/Graphify-Labs/graphify/blob/59b5ab1151e031fb42e39cfc9a50bf96f2d57ae0/graphify/export.py#L399-L456)
(`nodes` + `links`, not `edges`) and a project-owned binding sidecar.
It never installs, imports, or runs Graphify and never opens exported
`source_file` paths. Upstream `built_at_commit` refers to the target repository,
not proof of the Graphify producer's version.

```bash
fabric-kg review-graphify-import \
  --source .fkg/l4/approved-run \
  --input-manifest-search-root .fkg/l3 \
  --graph exports/graph.json --bindings exports/bindings.json \
  --out build/graphify-reviews/review-1.json
```

The export must be directed, have `graph.schema_version: 1` (an integer),
unique nonempty string node IDs, and a nonnegative integer `community` on every
node. Hyperedges, unknown endpoints, missing assignments, and lost/extra facts
are rejected, not repaired. Flat communities are reviewed as labels only;
no parent nodes, semantic inheritance, or publication authority are inferred.
Graphify's default undirected export is intentionally rejected: the reviewed
artifact must actually retain directed links (for example an export of a graph
built with upstream `directed=True`), not merely have its JSON flag edited.

Prepare `bindings.json` by explicitly associating **every** exported node and
link with the approved sealed facts. This is our sidecar format, **not an
upstream Graphify schema**. The following example is illustrative: replace
the hash/ID/type/evidence placeholders and add all remaining rows:

```json
{
  "schema_version": "1.0",
  "upstream_sha": "59b5ab1151e031fb42e39cfc9a50bf96f2d57ae0",
  "graphify_version": "0.9.102",
  "artifact_sha256": "<64 lowercase hex characters: SHA256 of graph.json bytes>",
  "sealed_manifest_hash": "<manifest_hash of the sealed L4 artifact manifest>",
  "entities": [
    {
      "graphify_id": "<nodes[i].id>",
      "entity_id": "<sealed entity_id>",
      "semantic_type_id": "<sealed most_specific_type_id>",
      "evidence_span_ids": ["<sealed evidence_span_id>"]
    }
  ],
  "relationships": [
    {
      "link_index": 0,
      "relationship_id": "<sealed relationship_id>",
      "semantic_relationship_id": "<sealed semantic_relationship_id>",
      "evidence_span_ids": ["<sealed evidence_span_id>"]
    }
  ]
}
```

Compute `artifact_sha256` from the exact export bytes (for example
`shasum -a 256 exports/graph.json`). Copy authority values from the sealed
L4 manifest and `semantic_asserted_entities.parquet` /
`semantic_asserted_relationships.parquet`, not Graphify labels.
`link_index` is the zero-based position in the exported `links` array; each
link's mapped source/target must match the sealed directed endpoints and its
`relation` must equal the sealed `semantic_relationship_id`. Parallel links
need separate indices and separate canonical relationship bindings; any
upstream collapse or rekey without a complete bijection fails review.
Types and nonempty, unique evidence-ID sets must match the sealed rows exactly.
Do not fabricate matches or relabel incompatible exports to pass this check.

Each input JSON is limited to 16 MiB. Malformed JSON, duplicate keys,
non-finite numbers, invalid scalar IDs, unknown sidecar fields, and mismatched
hashes fail explicitly. Outputs are create-only; sealed L4, L3 search roots,
default L1-L4 state, native derived-hierarchy, and release directories are
protected through resolved-path collision checks, including symlinks. Use
repeatable `--protected-root` for additional custom authority directories.

The review binds the export bytes, bindings bytes, sealed manifest/receipt,
input graph hash, and any sealed partial scope. A successful exit means only
that these **caller-declared associations** matched: the report always says
`shadow_review_only`, `publishable: false`, and
`producer_provenance_verified: false`. The pinned upstream SHA/version are
claims, not producer attestation. Evidence-ID agreement does not prove
Graphify's extraction or source content. DI table/cell anchors remain subject
to existing L3 checks; preserved figures/images do **not** make image-only
claims supported by L3. This report is not accepted as `--hierarchy-state`,
does not mint evidence, and cannot authorize Lakehouse/Ontology publication.

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
additional installation options. For 0.2.8, use the source-install instructions
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
