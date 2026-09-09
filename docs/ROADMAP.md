# Development roadmap

Updated: 2026-09-09.

This document separates decisions, implemented capabilities and future work.
Research conclusions do not authorize deployment or imply live acceptance.
Development stays local with task-labelled commits; no push or PR is implied.

## Current baseline

- Local CLI: 0.2.6, implementation milestone `a4df579`.
- Design-first generation, separate evaluation and strict compilation/approval
  are implemented. See [the specification](specs/SPEC-0.2.6-DESIGN-FIRST.md).
- Real design generation succeeded, but the generated design was not ready for
  approval or Fabric publication. Bounded sampling was dominated by one file.
- Track A is being implemented under the refined question-routing scope below.
  Previous passing regression results do not validate this new context flow.

## A. Next priority: query-time numeric analysis

Status: implementation in progress. See
[Question execution context](specs/SPEC-QUESTION-ROUTING-CONTEXT.md).

### Decision

Classify numerical/count/trend questions as Lakehouse SQL work when collecting
the domain, expected questions and background. Retain the backend decision and
relevant source/analysis requirements as context through the pipeline. Do not
create ontology quantity/metric/trend entities or properties merely to answer
those questions.

The user's 2026-09-09 clarification supersedes interpreting this as a blanket
numeric-property prohibition. This change carries context; it does not implement
an analytical SQL engine or claim that physical SQL bindings already exist.

For part-count questions, count the relevant connected records at a defined
grain. Distinguish distinct part/SKU counts from physical part occurrences;
do not report one as the other. Retain original source evidence and values in
the appropriate source/data layer. Do not erase safety conditions from source
text or treat unknown multiplicity as a known quantity.

This is a product design policy, not a restriction of Microsoft Fabric.
It is not an instruction to delete existing approved data or migrate deployed
ontologies without a separate plan.

### Work remaining

1. Remove conflicting instructions that currently ask the model to add quantity
   and unit properties in `domain/design.py`, `domain/compact.py` and
   `domain/proposal.py`. Keep generation modes consistent.
2. Record the policy in machine-readable CLI context and human-readable helper
   guidance, including scope, identity, deduplication and counting grain.
3. Separate SQL analytical requirements from missing ontology fields in design
   evaluation. A SQL count question must not require a stored count property or
   fake graph path. Missing source data or an unsupported computation remains visible.
4. Preserve structural identity/order metadata and evidence guarantees. Do not
   implement a blanket rejection of every identifier or source string containing
   digits.
5. Update CLI help, the pipeline skill, agent instructions and design
   specifications together.
6. Add focused tests for routed question/context preservation from intake through
   approval, extraction and serving/agent handoff, correct grain and unchanged
   source-evidence boundaries.

### Completion criteria

The public CLI conveys this policy to generation and evaluation; helper guidance
does not contradict it; counting does not create quantity entities merely to
pass question coverage; analytical results are not presented as stored ontology
facts. Existing approved artifacts are not silently rewritten.

## B. Deferred: multiple ontologies over one Lakehouse

Status: investigation complete; implementation and live proof deferred.
Resume after track A unless the user changes priority.

### Recommended design

Use one logical parent domain with versioned common definitions and separate
subdomain ontology projections:

| Layer | Responsibility |
|---|---|
| Parent domain package | Shared vocabulary, identity policy, source/evidence rules and subdomain membership |
| Product/parts catalogue ontology | Device models, configurations and explicitly sourced parts-list associations |
| Repair ontology | Repair jobs, procedures, steps, safety and required resources |
| Shared Lakehouse | Common canonical entity tables plus catalogue and repair relationship tables |

The parent package need not be deployed as a third Ontology item. Separate
question paths do not inherently require separate Ontology items; the proposed
split is for domain boundaries, ownership, reuse and independent evolution.

### Feasibility and evidence limits

Fabric Ontology data bindings identify source Lakehouse tables per entity type.
This supports the proposed architecture of independently defined Ontology items
using a shared Lakehouse. We have **not** live-tested the exact two-item binding
combination in the selected workspace.

No confirmed native cross-item inheritance or single-query traversal across
independent Ontology items was established by this investigation. Do not confuse
federation across sources inside one ontology with federation across ontology
items. Manage shared definitions in the CLI and join scoped results through
canonical source IDs or a separately validated shared-data query.

Shared Lakehouse storage does not mean shared graph materialization, automatic
synchronized refresh, identical opaque graph node IDs or identical permissions.

### Current repository gaps

- `deploy/schema2_prototype.py` creates one new Lakehouse, one Ontology and one
  independent Graph per run. Running it twice does not create the proposed
  shared-Lakehouse topology.
- Its journal uses kind-level keys such as `create:ontology`. Multiple ontology
  targets need independent logical target IDs and journal entries.
- `deploy/fabric_ontology_definition.py` already accepts a Lakehouse ID and table
  names, so its native binding compiler can be reused.
- L5a target mappings, release authority and L6 question scopes need explicit
  subdomain/target membership, not an unvalidated loop around current commands.
- Common identities need one governed definition and version. Independently
  generated names or SourceUnit-local occurrence IDs do not automatically
  reconcile the same entity across domains.

### Ordered implementation path

1. Define a domain-package manifest: parent identity/version, common definitions,
   subdomain IDs, questions, included types/relationships and storage bindings.
2. Freeze shared canonical identity and evidence contracts. Preserve occurrence
   provenance separately from reconciled entity identity.
3. Build subdomain projections from shared ingestion and verified facts, without
   repeating OCR/extraction solely because there are two ontology targets.
4. Separate Lakehouse/table preparation from ontology item publication. Existing
   Lakehouse reuse requires explicit ID, authority and schema checks.
5. Produce one reviewed group plan with per-ontology names, definitions, bindings,
   journal state and refresh/readback outcomes. Partial failure must not delete
   the shared Lakehouse or modify unrelated items.
6. Add question routing and scoped cross-domain result composition. Keep numeric
   analysis policy A shared across both ontologies; use Search for source detail.
7. Run an isolated live acceptance using one Lakehouse and two ontology items.

Steps 3 and subdomain-specific definition generation can be developed in parallel
only after shared identity and package contracts are fixed.

### Live acceptance criteria

- Exactly one explicitly owned/reused Lakehouse and two separately identified
  Ontology items; both bindings point to the intended Lakehouse/tables.
- Common entities use the same governed canonical IDs, without assuming that
  Fabric-generated graph IDs are interchangeable.
- Catalogue lookup works independently of repair-job relationships.
- Repair queries use the repair ontology and retain original evidence.
- A combined question composes source-verified results with explicit scope and
  matching IDs; no guessed joins or claims of native cross-item traversal.
- Refresh and partial failure are tracked per item without deleting shared data.
- Counts execute over complete scoped records, not a truncated chat response.

### Platform constraints to recheck before implementation

- Ontology remains preview in the referenced documentation.
- Bindings require supported managed Lakehouse tables. Current documentation
  excludes OneLake-security-enabled sources and Delta column mapping. Do not
  disable security automatically to make a binding work.
- Each entity type has one static data binding; prepare a suitable table rather
  than assuming multiple static sources will be unioned inside the entity.
- Fabric documents a maximum of ten Graph models per workspace. Ontology child
  graphs and any separately created diagnostic graphs affect the plan; do not
  assume the old workspace inventory is still current.
- Data Agent supports up to five sources in any combination. Two ontologies,
  a Lakehouse and Search are a candidate four-source arrangement, not proof of
  reliable cross-source joining or complete conversational result delivery.

### Official references consulted

- [Ontology overview](https://learn.microsoft.com/fabric/iq/ontology/overview)
- [Data binding and restrictions](https://learn.microsoft.com/fabric/iq/ontology/how-to-bind-data)
- [Ontology item-scoped MCP endpoints](https://learn.microsoft.com/fabric/iq/ontology/how-to-use-ontology-mcp-server)
- [Data Agent source combinations](https://learn.microsoft.com/fabric/data-science/data-agent-add-datasources)
- [Graph limitations](https://learn.microsoft.com/fabric/graph/limitations)
- [Ontology refresh and troubleshooting](https://learn.microsoft.com/fabric/iq/ontology/resources-troubleshooting)
