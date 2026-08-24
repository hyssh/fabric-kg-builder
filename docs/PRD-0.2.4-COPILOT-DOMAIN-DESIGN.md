# PRD: Copilot-Assisted Domain and Relationship Design

Date: 2026-08-23  
Release target: `fabric-kg` 0.2.4  
Status: Draft for product approval  
Owner: Fabric KG Builder  
Applies to: New projects created with 0.2.4

## 1. Executive Summary

`fabric-kg` 0.2.4 will make domain design a guided, evidence-based product
workflow rather than a manually authored prerequisite.

The user supplies:

- the business goal and desired decisions;
- five to ten competency questions;
- representative source files;
- optional terminology, positive examples, and exclusions.

Copilot inspects these inputs and proposes:

- the domain and subdomains;
- candidate entity types;
- a bounded, closed vocabulary of relationship types;
- `N`, the approved number of relationship types;
- `K`, the maximum relationship-path depth used consistently by domain design,
  extraction validation, and Graph queries;
- a competency-question coverage plan;
- evidence and publication policies.

The user reviews one summary and either approves, requests correction, or
aborts. Approval seals these decisions into `domain.yaml`. Enrichment can then
extract only approved entity and relationship types. Every publishable
relationship must carry an exact source span and deterministic `evidence_id`.
Unsupported, unresolved, and rejected candidates remain available for audit
but are never materialized into Fabric Ontology or Graph.

The initial product defaults are:

```text
Recommended N range:       8-20 relationship types when scope requires it
Default N target:          Copilot-selected minimum covering the questions
Hard N limit:              24 relationship types per domain contract
Default K:                 3 hops
Maximum K:                 4 hops with explicit rationale
Work-unit relation budget: 25 candidates; split input rather than truncate
Publication state:         asserted only
Evidence requirement:      exact source span for every published edge
```

These are product guardrails, not claimed universal ontology limits. The
values must be observable and tunable from 0.2.4 telemetry.

## 2. Problem Statement

Version 0.2.3 can review and approve a domain contract, but the workflow still
depends heavily on heuristic source inspection and manually selected candidate
categories. It does not derive a minimal relationship vocabulary or reasoning
depth from competency questions and representative source evidence.

The Surface Technician Event Ontology smoke test exposed the resulting
pipeline risk:

- 1,849 relationship candidates were extracted.
- 137 asserted relationships had evidence.
- 1,373 candidates were rejected.
- 319 candidates were unresolved.
- 20 candidates remained in the discovery lane.
- 1,105 normalized candidates had no `evidence_id`.
- The semantic quality report correctly measured accepted relationships.
- Ontology materialization later read broader raw relationship data and
  attempted to require evidence on rejected and unresolved rows.

This made compilation appear successful while deployment rejected the same
run. The problem was not that 1,723 relationship instances were too many.
Fabric Ontology defines relationship **types** and creates relationship
**instances** by binding data rows. The contract had 14 relationship types and
1,723 canonical relationship rows. The failure was caused by publishing from
the wrong lifecycle scope.

0.2.4 must align:

```text
customer questions
  -> approved domain relationship vocabulary
  -> schema-constrained extraction
  -> exact evidence validation
  -> asserted semantic projection
  -> Ontology and Graph publication
  -> bounded K-hop retrieval
```

## 3. Product Principles

1. **Questions determine scope.** Every published entity or relationship type
   must support at least one approved competency question or an explicit
   governance requirement.
2. **Relationship types are bounded; valid instances are not arbitrarily
   capped.** `N` limits vocabulary complexity. It does not discard additional
   evidence-backed facts of an approved type.
3. **Evidence is structural data.** A relation is not publishable unless its
   exact source span is locally verified and converted to a deterministic
   `evidence_id`.
4. **Abstention is success.** If the model cannot provide exact evidence, the
   product records an auditable candidate instead of inventing a fact.
5. **One approval, not thousands.** The user approves the proposed domain
   vocabulary and reasoning policy once. The product automatically validates
   individual relationship instances.
6. **Raw candidates are not serving data.** Audit retention and Ontology
   publication use separate tables and explicit state transitions.
7. **One K contract.** The approved maximum path depth applies to domain
   coverage planning, extraction validation, and runtime Graph traversal.
8. **Copilot proposes; deterministic code enforces.** An LLM can recommend
   terms and paths, but cannot bypass schema, evidence, count, state, or
   publication gates.

## 4. Goals

1. Reduce new-project domain setup to one guided session and one summary
   approval.
2. Generate a cited domain proposal from business intent, competency questions,
   and representative source samples.
3. Recommend the smallest useful relationship vocabulary within the configured
   `N` guardrails.
4. Recommend the smallest useful maximum path depth `K` that covers the
   competency questions.
5. Seal entity types, relationship types, direction, endpoint compatibility,
   evidence policy, and publication policy into `domain.yaml`.
6. Restrict enrichment to the approved closed vocabulary.
7. Require exact source spans for asserted relationship candidates.
8. Publish only asserted, evidence-backed, schema-compatible relationships.
9. Preserve unresolved and rejected candidates in an audit surface with
   actionable reasons.
10. Make compile-time and deploy-time validation operate on the same semantic
    projection.
11. Apply the same `K` limit to generated Graph query plans and runtime
    traversal.
12. Support both an interactive CLI and a deterministic YAML/JSON
    noninteractive workflow.

## 5. Non-Goals

1. Migrating 0.2.3 domain contracts automatically.
2. Automatically approving a domain without a user action.
3. Asking the user to approve every extracted relationship instance.
4. Publishing unresolved or rejected candidates to Ontology or Graph.
5. Generating evidence from model rationale or paraphrase.
6. Treating confidence alone as evidence.
7. Capping the total number of valid relationship instances at `N`.
8. Supporting reasoning paths longer than four hops in 0.2.4.
9. External ontology discovery, licensing, or import.
10. Replacing a domain expert for high-risk legal, clinical, safety, or
    regulatory policy approval.
11. Migrating or silently rewriting existing projects.

## 6. Users and Jobs

| User | Job |
|---|---|
| Domain owner | Explain goals and approve one understandable domain summary |
| Knowledge engineer | Inspect and override proposed entity/relationship semantics |
| Data engineer | Run a deterministic source-to-Fabric pipeline |
| AI engineer | Tune proposal/extraction prompts and model configuration |
| Governance reviewer | Audit why a type or relation was proposed, accepted, or rejected |
| Agent developer | Use a bounded Graph contract for predictable retrieval |

## 7. Definitions

| Term | Meaning |
|---|---|
| Relationship type | Approved semantic predicate with source type, target type, direction, and policy |
| Relationship instance | One data edge between two entity instances |
| `N` | Number of approved relationship types in one domain contract |
| `K` | Maximum number of relationship edges in an approved reasoning path |
| Candidate | Model-proposed entity or relationship before deterministic acceptance |
| Asserted | Schema-valid candidate with exact, locally verified evidence |
| Unresolved | Potentially useful candidate lacking sufficient certainty or linkage |
| Rejected | Candidate that violates schema, type, vocabulary, span, or policy rules |
| Audit queue | Non-serving record of unresolved/rejected candidates and reasons |
| Semantic projection | Asserted, evidence-backed data eligible for Ontology and Graph |

## 8. Why N and K Are Bounded

### 8.1 Relationship instances versus relationship types

Microsoft Fabric Ontology first defines a relationship type, then binds source
data rows to create instances. Therefore, thousands or millions of relationship
rows are a data-scale concern, not a reason to define thousands of relationship
types.

The product must separately report:

```text
relationship_type_count
relationship_candidate_count
asserted_relationship_count
unresolved_relationship_count
rejected_relationship_count
published_relationship_count
```

### 8.2 N rationale

There is no published universal optimum for ontology relationship count.
Ontology engineering guidance recommends limiting scope to the properties and
relations required by the intended application rather than representing every
possible domain connection.

For 0.2.4, Copilot must solve a bounded coverage problem:

1. Identify relationship types required by each competency question.
2. Merge semantic duplicates and inverses.
3. Prefer reusable predicates over question-specific predicates.
4. Select the smallest set that covers the approved questions.
5. Use 8-20 as an advisory range, never as a reason to pad a smaller complete
   vocabulary.
6. If coverage requires 21-24, provide a complexity warning and rationale.
7. If more than 24 are required, propose domain modules or narrower scope and
   block approval until the user resolves the issue.

### 8.3 K rationale

Two-hop questions are a common baseline for explainable multi-hop QA.
Graph-based retrieval research also demonstrates stepwise traversal while
warning that indiscriminate context expansion introduces irrelevant evidence.

0.2.4 uses:

- `K=3` as the normal product default;
- a lower sealed K when every required question is covered in fewer hops;
- `K=4` only when a named competency question requires it and each step can be
  grounded;
- no path above four hops.

`K` is a maximum, not a target. Direct questions should use one hop; two-hop
questions should not be expanded to three.

Example:

```text
Symptom
  --triggered_by^-1--> DiagnosticEvent  # hop 1
  --diagnoses--------> Cause            # hop 2
  --remediates^-1----> RepairEvent      # hop 3
```

Each edge must have independent evidence. Evidence for the first edge does not
validate later edges.

## 9. User Experience

### 9.1 Interactive default

The default new-project flow is:

```bash
fabric-kg init-domain --input ./sources --interactive
```

The CLI asks only for information not inferable from supplied configuration or
source inspection:

1. What business outcome should this graph support?
2. Who will use it and what decisions will they make?
3. What is in scope and explicitly out of scope?
4. What five to ten questions must the graph answer?
5. Are any proposed predicates safety-, legal-, or policy-sensitive?

The CLI then:

1. creates a bounded source profile;
2. samples representative headings, text, tables, and visual descriptions;
3. asks Copilot for a structured domain proposal;
4. deterministically scores and validates the proposal;
5. displays one review summary;
6. accepts `approve`, `correct`, or `abort`;
7. writes an approved `domain.yaml` and cited proposal artifact.

### 9.2 One-summary approval

The approval screen must include:

- domain name, description, and scope;
- user personas, decisions, and outcomes;
- all competency questions;
- proposed entity types;
- proposed relationship types with direction and endpoints;
- `N` and why it was selected;
- `K` and which questions require each depth;
- question coverage and unsupported questions;
- exact source examples supporting proposed vocabulary;
- warnings, assumptions, and extraction risks;
- publication and abstention policy;
- model, prompt, schema, source-profile, and proposal hashes.

The user does not approve individual extracted edges.

### 9.3 Correction mode

Correction mode supports:

- add/remove/rename an entity type;
- add/remove/rename a relationship type;
- change direction or endpoint types;
- merge synonymous predicates;
- adjust a competency question;
- reduce scope;
- lower `K`;
- request regenerated recommendations.

Increasing `N` beyond 20 or setting `K=4` requires a displayed rationale.
Values above the hard limits are rejected.

### 9.4 Noninteractive mode

Automation supplies a YAML or JSON intake file:

```bash
fabric-kg init-domain \
  --input ./sources \
  --intake domain-intake.yaml \
  --proposal-out .fkg/domain-proposal.json \
  --non-interactive
```

Approval remains a separate explicit operation:

```bash
fabric-kg domain approve \
  --file domain.yaml \
  --proposal .fkg/domain-proposal.json \
  --approved-by "$OPERATOR"
```

CI cannot invent an approver. An approved intake/proposal may be reused only
when all bound hashes still match.

## 10. Copilot Proposal Algorithm

### 10.1 Inputs

Required:

- business goal;
- intended users and decisions;
- desired outcomes;
- in-scope and out-of-scope concepts;
- five to ten competency questions;
- source profile;
- bounded representative source samples.

Optional:

- canonical terminology;
- ambiguous terms;
- positive and negative examples;
- regulatory, privacy, safety, and temporal constraints.

### 10.2 Structured proposal

Copilot returns JSON matching a strict schema:

```yaml
schema_version: "2.0"
domain:
  name: string
  description: string
  subdomains: [string]
entity_types:
  - id: entity-type:<slug>
    name: string
    parent: entity-type:<slug> | null
    description: string
    source_evidence_ids: [proposal-evidence:<id>]
relationship_types:
  - id: relationship-type:<slug>
    predicate: string
    description: string
    source_types: [entity-type:<slug>]
    target_types: [entity-type:<slug>]
    direction: source_to_target
    evidence_policy: exact_span_required
    publication_policy: asserted_only
    competency_question_ids: [cq:<id>]
    source_evidence_ids: [proposal-evidence:<id>]
reasoning_policy:
  recommended_relationship_type_count: 16
  max_relationship_types: 24
  recommended_max_hops: 3
  max_hops: 4
question_plans:
  - question_id: cq:<id>
    required_path:
      - relationship_type: relationship-type:<slug>
        traversal: forward | reverse
    hop_count: 1
    covered: true
```

Unknown keys are rejected.

### 10.3 Deterministic selection

Copilot proposes candidates; local code performs final selection.

Each relationship type receives:

```text
coverage_score        competency questions covered
source_support_score  representative source evidence
reuse_score           usefulness across questions
clarity_score         endpoint and direction precision
risk_penalty          safety, ambiguity, unsupported assumptions
redundancy_penalty    synonym, inverse, or duplicate semantics
```

The selector chooses the minimal type set that:

- covers all coverable competency questions;
- respects endpoint and direction constraints;
- has representative source support or explicit business justification;
- does not exceed the approved `N`;
- yields no planned question path deeper than `K`.

Unsupported questions remain visible and block automatic approval if marked
business-critical.

### 10.4 K recommendation

For each competency question, Copilot proposes a typed path. Local code
validates the path against the proposed type graph.

The recommended `K` is:

1. the maximum shortest valid path among required questions;
2. sealed at that derived value, including values lower than three;
3. normally capped at three;
4. allowed to become four only when at least one required question has no
   valid path of three or fewer hops and the four edges have source support;
5. rejected if any required question needs more than four hops.

The proposal must suggest splitting an over-deep question into bounded
subquestions rather than increasing `K`.

## 11. Domain Contract 2.0

New 0.2.4 projects use `domain.yaml` schema version `2.0`. Existing 1.0
contracts continue to be readable under their existing workflow but are not
automatically converted.

Required additions:

```yaml
schema_version: "2.0"

candidate_model:
  entity_types: [...]
  relationship_types: [...]

reasoning_policy:
  relationship_type_count: 14
  recommended_relationship_type_range: [8, 20]
  max_relationship_types: 24
  max_hops: 3
  absolute_max_hops: 4
  max_relations_per_work_unit: 25

extraction_policy:
  vocabulary_mode: closed
  exact_evidence_span_required: true
  abstain_without_evidence: true
  allow_subtype_endpoints: true

publication_policy:
  included_states: [asserted]
  excluded_states: [unresolved, rejected]
  source_table: semantic_relationships
```

Approval seals the normalized contract, proposal, source profile, prompt
version, and model version.

## 12. Extraction Requirements

### 12.1 Closed vocabulary

Enrichment requests contain only approved:

- entity type IDs;
- relationship type IDs;
- endpoint types;
- directions;
- subtype hierarchy;
- evidence requirements.

The model must not create a new type. Unknown predicates enter a separate
discovery queue and cannot become canonical data without a new domain approval.

### 12.2 Work-unit budget

`max_relations_per_work_unit` defaults to 25.

If a source unit can contain more candidates:

- split it deterministically with overlap and stable child IDs;
- do not silently keep only the top 25;
- preserve source ordering and parent lineage;
- deduplicate after extraction.

This budget controls model output size, not domain-wide instance count.

### 12.3 Exact evidence contract

For each asserted relationship, the model must return:

```yaml
relationship_type: relationship-type:requires-tool
source_local_id: event-1
target_local_id: tool-1
evidence:
  text_unit_id: text-unit:<id>
  span_start: 410
  span_end: 487
  quote: "Use a 3IP Torx Plus driver to remove the six enclosure screws."
```

Local code must verify:

- source and target occurrences exist;
- the relationship type is approved;
- endpoints are compatible, including approved subtype inheritance;
- direction is correct;
- offsets are in range;
- `source_text[span_start:span_end] == quote`;
- the quote is nonempty and supports one source unit;
- the source locator and content hash match;
- a deterministic evidence ID can be generated.

No model-authored `evidence_id` is trusted. The CLI generates it only after
verification.

### 12.4 State transition

```text
model candidate
  -> schema/type/span checks pass
       -> asserted + deterministic evidence_id
  -> evidence missing but potentially useful
       -> unresolved + audit reason
  -> vocabulary/type/direction/span check fails
       -> rejected + audit reason
```

An asserted row without evidence is impossible by schema and must fail the
work unit before checkpoint success.

The 0.2.3 `unverified` state is not a serving state in schema 2.0. New-project
ingestion must deterministically map it to `unresolved` with an audit reason.
No `unverified` row may appear in `semantic_entities` or
`semantic_relationships`.

### 12.5 Subtype compatibility

Endpoint validation must use transitive subtype compatibility.

If:

```text
ReplacementEvent <: RepairEvent <: SupportEvent
```

then a relationship whose source is `SupportEvent` may accept a
`ReplacementEvent` instance unless the contract marks the endpoint exact-only.

The resolved endpoint type and inheritance path must be recorded for audit.

## 13. Canonical and Publication Surfaces

### 13.1 Raw audit surface

The canonical `relationships` table may retain all states:

- asserted;
- unresolved;
- rejected.

It must include `assertion_state`, `processing_status`, rejection codes,
candidate evidence hints, model metadata, and source lineage.

Raw candidate reconciliation must use explicit lifecycle accounting:

```text
input_candidate_count
  = asserted_count
  + unresolved_count
  + rejected_count
  + discovery_count
  + deduplicated_count
  + endpoint_unresolved_count
```

Every candidate must reach exactly one terminal bucket. Projection logic must
not silently drop a row.

### 13.2 Entity serving surface

`semantic_entities` contains only canonical entities that:

```text
assertion_state == asserted
identity and required properties are valid
entity_type is approved
semantic_contract_hash matches the active authority
```

Every published entity must carry at least one verified source evidence link
unless its approved type is explicitly marked `business_defined` in the domain
contract. Business-defined entities require proposal approval metadata instead
of extracted source evidence.

### 13.3 Relationship serving surface

`semantic_relationships` contains only:

```text
assertion_state == asserted
evidence_id IS NOT NULL
relationship_type is approved
endpoints are canonical and type-compatible
semantic_contract_hash matches the active authority
```

Every source and target ID in `semantic_relationships` must resolve to a row in
`semantic_entities`. An edge with an unpublished endpoint is moved to the audit
surface as `ENDPOINT_UNPUBLISHED`; it is not silently omitted.

### 13.4 Deployment invariant

Ontology and Graph materialization must read `semantic_relationships`, never
the unfiltered raw `relationships` table.

Compile-time and deploy-time code must share one projection function and one
validation implementation. A compiled projection that passes validation must
not fail deployment because deployment selected a broader lifecycle state.

## 14. Bounded Graph Querying

Generated competency plans and runtime Graph queries must:

- use only approved entity and relationship types;
- preserve edge direction;
- limit paths to the approved `K`;
- return scalar IDs and evidence IDs;
- reject unbounded variable-length traversal;
- require an explicit lower bound and upper bound;
- stop early when sufficient evidence is found;
- log actual hop count.

Example:

```gql
MATCH (s:Symptom)<-[:triggered_by]-(d:DiagnosticEvent)
      -[:diagnoses]->(c:Cause)<-[:remediates]-(r:RepairEvent)
RETURN s.entity_id, d.entity_id, c.entity_id, r.entity_id
LIMIT 100
```

This is a three-hop plan and is valid under `K=3`.

## 15. Audit Queue

The audit queue is generated without requiring immediate human action.

Each item includes:

- candidate relationship and endpoints;
- state and stable reason codes;
- missing or invalid evidence details;
- source file, page, element, and text-unit locator;
- proposed quote when available;
- confidence;
- model and prompt versions;
- retry eligibility;
- suggested resolution.

Initial reason codes:

```text
EVIDENCE_MISSING
EVIDENCE_SPAN_INVALID
EVIDENCE_QUOTE_MISMATCH
UNKNOWN_RELATIONSHIP_TYPE
SOURCE_TYPE_MISMATCH
TARGET_TYPE_MISMATCH
DIRECTION_MISMATCH
ENDPOINT_UNRESOLVED
ENDPOINT_UNPUBLISHED
OVER_MAX_HOPS
CONTRACT_HASH_MISMATCH
```

Automated retry is permitted only for transient model/schema failures. A retry
cannot broaden the approved vocabulary or weaken evidence policy.

## 16. Validation Gates

New gates:

| Gate | Requirement |
|---|---|
| DOM-101 | Five to ten competency questions are present |
| DOM-102 | Every approved relationship type supports a question or governance rule |
| DOM-103 | `N` is between 1 and 24; 21-24 includes rationale |
| DOM-104 | Every required question has a valid path or is explicitly unsupported |
| DOM-105 | No approved path exceeds `K`; `K` is at most 4 |
| DOM-106 | Relationship type names, directions, and endpoints are unique and unambiguous |
| EXT-101 | Enrichment output contains no unapproved canonical type |
| EXT-102 | Every asserted relationship has a locally verified exact span |
| EXT-103 | Subtype endpoint validation is transitive and deterministic |
| EXT-104 | Work-unit budget overflow splits input rather than truncating output |
| SEM-100 | `semantic_entities` contains asserted rows only and satisfies entity evidence policy |
| SEM-101 | `semantic_relationships` contains asserted rows only |
| SEM-102 | Every semantic relationship has a valid `evidence_id` FK |
| SEM-103 | Every semantic relationship endpoint resolves to a published semantic entity |
| SEM-104 | Candidate counts reconcile across all explicit lifecycle buckets |
| DEP-101 | Ontology and Graph deploy from the sealed semantic projection |
| DEP-102 | Compile and deploy projection row counts and hashes match |
| QRY-101 | Generated Graph paths do not exceed approved `K` |

## 17. Success Metrics

### 17.1 Product metrics

- Median user approval actions per new domain: one.
- Median interactive setup time for a representative corpus: at most 15
  minutes, excluding source parsing and model latency.
- At least 90% of proposed relationship types accepted without individual
  editing in internal evaluations.
- At least 90% of required competency questions covered by an approved path of
  three or fewer hops.
- No approved domain exceeds 24 relationship types; a complete vocabulary
  below eight is accepted without padding.

### 17.2 Quality invariants

- 100% of published relationships have exact verified evidence.
- 0 unresolved or rejected relationships appear in Ontology/Graph serving
  tables.
- 0 asserted relationships have a null `evidence_id`.
- 0 published relationships reference an unpublished entity.
- 0 `unverified` rows appear in semantic serving tables.
- 100% of published rows match the active semantic contract hash.
- Compile-time and deploy-time semantic row counts and hashes are identical.
- 100% of generated Graph plans obey the approved `K`.

### 17.3 Operational metrics

- proposal token/cost per sampled source unit;
- proposal latency;
- N proposed versus N approved;
- K proposed versus K approved;
- candidate/asserted/unresolved/rejected counts by type;
- evidence-span failure rate;
- subtype-resolution rate;
- retry rate;
- average and p95 runtime query hop count.

Metrics must contain no source text or secrets.

## 18. Release Acceptance Criteria

0.2.4 is releasable when:

1. A new project can complete interactive domain setup using business goals,
   five to ten questions, and representative source files.
2. The CLI creates a cited proposal and schema-2.0 `domain.yaml`.
3. One summary approval seals N, K, types, paths, and hashes.
4. Noninteractive proposal and explicit approval work from YAML/JSON.
5. Extraction rejects unknown types and locally verifies exact spans.
6. Asserted relations without evidence fail before checkpoint success.
7. Subtype endpoints are accepted transitively.
8. Budget overflow splits work units without losing candidates.
9. Audit artifacts retain rejected/unresolved candidates with reasons.
10. `compile-data` produces raw and semantic relationship surfaces whose counts
    reconcile.
11. Ontology and Graph deploy only the sealed semantic projection.
12. A fresh live smoke test deploys nonempty Lakehouse, Ontology, Graph, and
    Search surfaces.
13. Live read-back confirms no extra rows and no unexplained drops relative to
    the sealed projection; exact count equality is required after semantic
    endpoint integrity passes.
14. Generated Graph query plans never exceed approved K.
15. Unit, contract, integration, and release tests pass from an isolated
    installed CLI.
16. Package, plugin, CLI, and API versions all report 0.2.4.

## 19. Test Strategy

### Unit and contract

- N selector chooses the minimal covering set.
- synonymous and inverse predicates deduplicate deterministically.
- N warnings and hard limits behave correctly.
- K is the maximum shortest approved question path.
- K=4 requires a cited rationale.
- exact span, quote, hash, and source locator validation.
- unknown vocabulary abstention.
- subtype closure and exact-only endpoint behavior.
- entity assertion, evidence, and publication policy.
- legacy `unverified` to `unresolved` mapping.
- asserted-without-evidence schema rejection.
- audit reason-code coverage.
- semantic projection excludes unresolved/rejected rows.
- every published relationship endpoint resolves to a published entity.
- lifecycle counts reconcile without silent drops.
- compile/deploy use the same projection implementation.

### Golden fixtures

Include fixtures for:

1. an accepted evidence-backed relationship;
2. an unresolved candidate with no span;
3. a rejected candidate with invalid endpoint type;
4. a valid child-type endpoint;
5. an invalid quote mismatch;
6. an over-budget source unit that splits;
7. a three-hop competency question;
8. a four-hop question with rationale;
9. a five-hop question that is rejected.

### Integration

- interactive CLI with mocked Copilot;
- noninteractive intake and approval;
- representative PDF extraction with mocked LLM output;
- full compile through packaged Ontology/Graph artifacts;
- mocked deploy confirms semantic rather than raw relationship tables.

### Live smoke

Using an isolated installed CLI:

- generate and approve a new domain;
- enrich representative documents;
- verify exact evidence samples;
- compile all artifacts;
- dry-run deployment;
- obtain explicit live approval;
- deploy and read back nonempty Ontology and Graph;
- verify Search provenance and bounded query plans.

## 20. Rollout

### Phase 1: Internal preview

- new projects only;
- feature enabled by domain schema 2.0;
- collect N/K recommendation and correction telemetry;
- no automatic approval.

### Phase 2: Recommended default

- `init-domain --interactive` uses the Copilot workflow by default;
- retain deterministic intake for automation;
- publish authoring guidance and example proposals.

### Phase 3: Future consideration

- assisted migration from schema 1.0;
- learned N/K recommendations from anonymized aggregate metrics;
- external ontology discovery and mapping;
- targeted human-review queues for high-risk predicates.

## 21. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Copilot proposes a plausible but unsupported predicate | Require proposal evidence or explicit business justification |
| N is too small and misses business questions | Coverage report blocks approval for critical unsupported questions |
| N is too large and harms usability | Minimal set selection, warning above 20, hard limit 24 |
| K is too small for one complex question | Allow justified K=4 or decompose the question |
| K is too large and expands noisy context | Hard limit 4 and shortest-path planning |
| Model omits evidence | Abstain; audit; never publish |
| Model uses paraphrase instead of source quote | Exact local substring validation |
| Child types are incorrectly rejected | Transitive subtype endpoint validation |
| Raw candidates leak into serving | One shared semantic projection and deploy hash gate |
| One approval hides risky predicates | Highlight safety/legal predicates and require explicit rationale in summary |
| Prompt/model drift changes the domain | Seal model/prompt/source/proposal hashes |

## 22. Research Basis

The guardrails are product recommendations informed by the following sources:

1. Microsoft Fabric documents that a relationship type is defined first and
   bound data rows then create relationship instances:
   <https://learn.microsoft.com/fabric/iq/ontology/how-to-create-relationship-types>
2. Microsoft Fabric describes Ontology as a governed vocabulary of entity
   types, properties, relationships, and data bindings:
   <https://learn.microsoft.com/fabric/iq/ontology/overview>
3. Stanford's Ontology Development 101 recommends limiting scope to what the
   application needs and not adding every imaginable relationship:
   <https://protege.stanford.edu/publications/ontology_development/ontology101-noy-mcguinness.html>
4. W3C OWL defines ontologies as representations of things, groups, and
   relations that software can validate and reason over:
   <https://www.w3.org/OWL/>
5. HotpotQA provides explainable multi-hop questions with explicit supporting
   facts and is a useful two-hop baseline:
   <https://aclanthology.org/D18-1259/>
6. StepChain GraphRAG motivates targeted iterative retrieval instead of
   overwhelming a model with indiscriminate multi-hop context:
   <https://arxiv.org/abs/2510.02827>
7. Grounded KG extraction research supports anchor-constrained exact
   provenance and local verification:
   <https://doi.org/10.3390/computers15030178>

No source establishes a universal optimal N or K. The 0.2.4 values are
explicit product defaults chosen for usability, explainability, bounded cost,
and measurable iteration.

## 23. Decisions Confirmed for This PRD

| Decision | Selected option |
|---|---|
| Approval model | One summary approval |
| N policy | Recommended 8-20, hard maximum 24 |
| K policy | Default 3, maximum 4 |
| K enforcement | Domain design, extraction validation, and Graph queries |
| Unsupported relation handling | Exclude from Ontology; retain in audit queue |
| Proposal inputs | Goals, five to ten questions, and sample documents |
| UX | Interactive CLI default plus YAML/JSON automation |
| Migration scope | New projects only |

## 24. Required Follow-On Specifications

After PRD approval, revise or add:

1. `SPEC-001`: new CLI signatures and stage order.
2. `SPEC-002`: assertion state, audit surface, semantic projection, and
   evidence invariants.
3. `SPEC-003`: Ontology/Graph materialization exclusively from the semantic
   projection.
4. `SPEC-004`: Copilot proposal schema, N/K selection, exact-span extraction,
   abstention, and work-unit splitting.
5. `SPEC-005`: DOM/EXT/SEM/DEP/QRY validation gates and release tests.
6. Domain schema 2.0 JSON Schema and examples.
7. A 0.2.4 implementation plan with independently mergeable workstreams.
