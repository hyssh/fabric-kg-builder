# Development roadmap

Updated: 2026-09-09.

This document separates decisions, implemented capabilities and future work.
Research conclusions do not authorize deployment or imply live acceptance.
Development stays local with task-labelled commits; no push or PR is implied.

## Current baseline

- Local CLI: 0.2.6; current full-corpus compilation fix `da14b0f`.
- Design-first generation, separate evaluation and strict compilation/approval
  are implemented. See [the specification](specs/SPEC-0.2.6-DESIGN-FIRST.md).
- Full 22-document discovery and reviewed domain approval have completed.
  Real zero-call replay through L3/L4 produced 3,680 entities but zero relations
  and one property; vocabulary alignment and detail-evidence issues remain.
  This is not a usable deployed Data Agent yet.
- Track A's local question-routing/context flow is implemented. This does not
  mean that physical SQL bindings or analytical execution are available.

## C. Active priority: corpus-first discovery and reuse

Status: local pipeline implementation complete, authorized 2026-09-09.
See [Corpus-first pipeline](specs/SPEC-CORPUS-FIRST-PIPELINE.md).

Move full source preparation, chunk-level open candidate discovery and
document/corpus consolidation before ontology design. After approval, reuse the
retained sources/candidates and perform only explicitly needed additional model
work. The sample-only path becomes explicit compatibility mode.

Required sequence: shared source preparation -> all-chunk discovery -> document
and corpus consolidation -> design/routing -> approval -> candidate reuse and
evidence validation -> serving.

Completion is based on auditable file/chunk coverage, safe resume and measured
candidate reuse, not on the number of files inventoried or the absence of errors.
Partial budgets and unsupported content must remain visible. This work does not
authorize multi-ontology deployment or treat discovery as asserted knowledge.

Current evidence: all 22 PDFs prepared as 1,425 cached-OCR SourceUnits/chunks with
zero model calls; a separate two-document, 33-page live smoke run completed
discovery/consolidation and discovery-bound design. It retained 403 raw
observations, 165 grounded candidates and 238 quarantined candidates. Revalidation
used no repeated chunk calls, consolidation used nine additional calls, and a
completed replay used zero calls.

Full-corpus follow-up: all 1,425 chunks were processed. The remaining summary
gaps were explicitly accepted for the prototype, without clearing grounding
quarantine. A full-corpus domain was approved and reused through real local
L2/L3/L4 without model calls. Its low relation/property yield motivates track E.
Grounding-quality improvement and Fabric/SQL business acceptance remain open.

## E. Current priority: windowed working schema and mapping

Status: local implementation and full-corpus deterministic replay complete;
semantic model-assisted alignment and Fabric business acceptance remain open.
See [window operation contracts](specs/SPEC-WINDOW-SCHEMA-OPERATIONS.md).

Process saved discovery observations in deterministic windows. Every worker
within a batch uses one frozen working schema. After gathering changes, evaluate
and commit the next version with local snapshots and before/after change logs.
Preserve aliases, stable concept IDs, owner/endpoint constraints and unresolved
choices through the last declared chunk. Review the final mappings before
approved replay; do not mutate the already approved domain or raw observations.

Implement the state/control/mapping flow in the CLI. Keep detailed owner/value
retrieval and Search index extension as
[Foundry Agent integration guidance](FOUNDRY-SEARCH-ORCHESTRATION.md), not a new
CLI search engine. Ontology holds core structure and keys; Search supplies
scoped original detail, and SQL handles analytical questions. Optional detail
need not become an ontology property, but asserted facts still require proof.

Implementation order: freeze snapshot/transition/mapping contracts -> parallel
core and CLI/replay work -> helper guidance -> targeted regression and public
CLI checks on existing saved candidates. No full raw-corpus recollection.

Actual public-CLI result over the retained 22-document corpus: 1,425/1,425 chunks
processed in 23 windows of up to 64 chunks, with versions 0 through 23 and a final
17-chunk window. All operations used zero model calls. Completed resume preserves
the authoritative run bytes/hash and accepted mapping-review binding.

Reviewed formatting mappings, replayed into fresh L2/L3/L4 state, changed
asserted entities from 3,680 to 3,882 and properties from 1 to 15; relationships
remain zero. All 11,951 grounded candidates reached the audit layer, and the
original 6,676 quarantined candidates were not promoted. Processing completion
does not mean semantic coverage: 233 grounded relationship candidates still need
semantic/endpoint alignment, and evidence and identity requirements still apply.
This result does not justify Fabric publication or claim all six questions work.

## D. Active release acceptance: full corpus and a user-facing Data Agent

Status: execution/development authorized and started 2026-09-09.

Two parallel workstreams:

1. Resume the prepared 22-PDF, 1,425-chunk corpus through the public CLI. Complete
   candidate collection and document/corpus consolidation, preserve partial work
   and grounding diagnostics, then generate/review the full-corpus design and
   approve only the exact acceptable proposal.
2. Complete a Schema-2-native Data Agent publication handoff from the owned
   structured-publication journal, sealed source authority and readback. Do not
   fabricate the legacy projection receipt expected by the compatibility command.

Join the workstreams only after approved data is ready: reuse candidates, verify
evidence, publish structured data, then create and validate the Data Agent through
the CLI. The user-facing deliverable is an actual Fabric Data Agent URL with
declared source/readiness status, not a plan file or an old unrelated agent.

Publication remains create-only in the user-selected workspace and prefix, with
dry-run planning, exact approval and retained partial resources. Ontology and
Lakehouse SQL source bindings must reference the actual new items; Search is
included only when a real index and authorized connection are available.
No existing shared item is adopted, deleted or replaced to make acceptance pass.

Required acceptance distinguishes Graph/ontology retrieval, SQL analysis and
source-evidence retrieval. Agent creation alone does not establish correct
answers to all six technician questions. Full-corpus discovery is finished;
completion, approval, deployment and user acceptance must be recorded
as separate milestones.

User decision at 2026-09-09 15:34: for this prototype, at least 99% processed
chunk coverage may be accepted explicitly to proceed with design. Retain
unprocessed/error IDs, grounding quarantine and summary gaps; do not mark the
original run complete or waive source/fact checks. Record actor, rationale,
actual numerator/denominator and the exact accepted discovery hash. This avoids
repeating remaining requests solely to reach 100%, while keeping downstream
limitations visible. Implementation is in progress.

## A. Next priority: query-time numeric analysis

Status: local context-flow implementation complete; SQL execution remains separate. See
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

### Implemented scope

1. Shared generation guidance routes analytical questions to Lakehouse SQL,
   rather than forcing ontology quantity fields or graph paths for them.
2. Typed per-question routing retains rationale, scope/grain, filter/time/source
   requirements and unresolved binding status. Pending requirements remain
   distinct, with versioned canonical hashes.
3. Explicit intake decisions are honored; model-proposed decisions are reviewed.
   Original question text and criticality survive compilation and approval.
4. Additional user background survives in derived approved business context,
   without rewriting the original intake.
5. Read-only `domain question-context` exports and existing sealed domain
   authority preserve this context through L2, serving/L5a and agent consumers.
6. Numeric facts and structural identity/order metadata remain valid. No SQL
   engine, numerical result, table binding or ontology metric is fabricated.

The current L6 guard recognizes exact registered SQL-question wording and stops
before Graph/Search when SQL is unresolved. General paraphrase classification,
actual SQL binding/execution and full business-answer acceptance are not delivered
by this context-flow change. Generated ontology designs still require review.

### Completion criteria

The public CLI conveys this policy to generation and evaluation; helper guidance
does not contradict it; counting does not create quantity entities merely to
pass question coverage; analytical results are not presented as stored ontology
facts. Existing approved artifacts are not silently rewritten.

## B. Deferred: multiple ontologies over one Lakehouse

Status: investigation complete; implementation and live proof deferred.
Resume only when requested; active track C takes priority over this investigation.

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
