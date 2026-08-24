# SPEC-007: L1 Domain Design and Approval

**Status:** Implemented  
**Version:** 1.0.0  
**Date:** 2026-08-25  
**Owner:** L1 Domain Contract Owner  
**Depends on:** Bootstrap PR #30, C0.Core PR #34, SPEC-001, SPEC-005,
SPEC-006  
**Plan revision:** `c49bc6d...`

## 1. Scope

L1 is the schema-2 new-project domain-design and approval stage. It inventories
the complete local source corpus, selects a bounded representative design
sample, verifies exact proposal evidence through C0.Core, deterministically
normalizes model-proposed candidates, and seals one immutable
`DomainContractV2` after one explicit user decision.

L1 does not extract canonical entities or relationships, publish an ontology,
mutate a running schema, import external ontology content, or activate schema-2
enrichment. L2 and later layers remain fail-closed until their required receipt
integration is implemented. Schema-1 behavior is unchanged and is selected with
`--legacy-schema-1`.

## 2. Inputs and immutable outputs

The required human input is the business goal, intended users and decisions,
scope and exclusions, terminology and constraints, and five to ten competency
questions. Examples are fixtures or proposal inputs only; no entity type,
predicate, relationship count, or domain term is built into L1 code.

L1 emits these versioned artifacts:

| Contract kind | Purpose |
|---|---|
| `l1.domain_intake` | Normalized user intent and competency questions |
| `l1.source_corpus_manifest` | Every discovered source identity and original-byte hash, with `eligible`, `excluded`, or `blocked` disposition |
| `l1.design_sample_manifest` | The distinct bounded subset used for design |
| `c0.source_unit` / `c0.evidence_span` | Sample-only proposal evidence minted by the local `domain_design` verifier |
| `l1.domain_source_profile` | Deterministic corpus/sample summary |
| `l1.domain_design_context` | Sealed intake, corpus, sample, prompt, model, and proposal inputs |
| `l1.domain_proposal` | Draft contract plus complete candidate audit |
| `l1.domain_approval_context` | Explicit decision and all approval bindings |
| `domain.yaml` | Approved `DomainContractV2` |
| `c0.artifact_manifest` | Input and output artifact bindings |
| `c0.stage_resource_metrics` | Observed counters only; no release threshold |
| `c0.stage_receipt` | `succeeded`, `blocked`, `failed`, or binding-safe `skipped` outcome |

Design SourceUnits and EvidenceSpans cover only the design sample. They cannot
stand in for complete extraction evidence. L2 must materialize complete
SourceUnits from the corpus manifest.

## 3. Domain authority

`DomainContractV2` is the only L1 domain authority. C0.Core retains authority
over canonical serialization, IDs, hashes, identities, locators, evidence,
manifests, metrics, and receipts.

The domain contract exclusively owns:

- globally unique stable semantic entity type IDs, aliases, optional single
  parent, abstract status, deterministic ancestor/descendant closure, and the
  hierarchy hash;
- identity roots and keys, inherited constraints and relationship endpoints,
  and a type-independent instance identity policy;
- approved relationship predicates and endpoint compatibility;
- generic `CompletenessRequirementV2` aggregate/scope, membership relationship,
  allowed member types and roles, ordering, source-supported cardinality,
  required roles, collection identity/hash policy, and evidence/CQ coverage;
- N/K, publication, evidence, ambiguity, drift, and external-reference policy.

Entity IDs exclude mutable labels and type assertions. Reclassification creates
a versioned type assertion and does not change the entity ID. Relationship
identity uses the approved predicate plus stable endpoint identities and context;
it likewise survives endpoint reclassification.

Hierarchy cycles, multiple parents, incompatible inheritance, invalid
root-owned keys, and incomplete closure/hash bindings fail closed. Hierarchy
depth is reported independently and never substituted for K.

## 4. Evidence, scoring, and governance

The local verifier mints C0.Core EvidenceSpans with purpose `domain_design`.
Each span preserves source file, asset/version, immutable locator, source-unit
text hash, exact quote, and Unicode code-point offsets. Model-authored evidence
IDs are rejected even when attached to an unselected candidate.

Candidate evidence coverage, competency-question coverage, ambiguity, semantic
fit, and IP/governance status are recomputed locally. IP/legal/provenance
ineligibility is a non-compensating rejection gate.

External ontology references are optional metadata. They require an immutable
source URI/version/hash, provenance, classified license, explicit allowed-use
decision, reviewer, and approval reference. L1 bundles no third-party ontology
content.

Unknown or ambiguous observations remain in proposal audit/discovery. They may
produce `DOMAIN_REREVIEW_REQUESTED`; they never mutate the approved runtime
schema.

## 5. N, K, and completeness

- N counts approved relationship types, including required role relationships
  even when they are not on a selected CQ path.
- 8-20 is advisory. Smaller complete vocabularies are not padded.
- 21-24 requires a recorded complexity rationale. More than 24 blocks approval.
- K is the maximum shortest approved competency-question path.
- K values 1-3 are normal. K=4 requires an exact cited rationale. K above four
  is invalid.
- Completeness cardinality has no inferred default. Expected, minimum, or
  maximum counts are allowed only when supported by reviewed source or
  competency-question evidence.

## 6. CLI and state transitions

Interactive new-project design is the default:

```bash
fabric-kg init-domain --input ./sources --intake intake.yaml --interactive
```

Automation validates YAML or JSON inputs but cannot silently approve a proposal:

```bash
fabric-kg init-domain \
  --input ./sources \
  --intake intake.yaml \
  --candidates candidates.json \
  --non-interactive

fabric-kg domain approve \
  --file domain.yaml \
  --proposal .fkg/l1/domain-proposal.json \
  --design-context .fkg/l1/domain-design-context.json \
  --source-profile .fkg/l1/domain-source-profile.json \
  --source-corpus-manifest .fkg/l1/source-corpus-manifest.json \
  --design-sample-manifest .fkg/l1/design-sample-manifest.json
```

The one-summary decision is `approve`, `correct`, or `abort`. Corrections create
a new proposal linked to the prior proposal; approval never mutates a proposal.
`--dry-run` performs local inventory and validation without writes or remote
calls. `--resume` emits `skipped` only after input, corpus, sample, proposal,
approval, domain, hierarchy, identity, completeness, and output artifact
bindings all match an intact prior succeeded run.

## 7. PR #31 successor parity matrix

Frozen PR #31 and `archive/pr-31-e6b4ad14` are read-only historical inputs. This
table is the exact behavior-level salvage map for this successor.

| PR #31 behavior or shape | Successor decision | L1 implementation |
|---|---|---|
| Recursive deterministic source discovery | **Keep** | Complete sorted corpus inventory in `sources/corpus.py` |
| Deterministic bounded representative sampling | **Keep** | Separate `DesignSampleManifest`; no sample entry substitutes for corpus completeness |
| Media-aware source inspection | **Keep** | Existing source adapters plus bounded L1 profile path |
| User intent and competency questions as proposal inputs | **Keep** | Strict `DomainIntake` with five to ten questions |
| Domain text remains untrusted model input | **Keep** | Trusted prompt boundary separates system instructions from serialized user context |
| Deterministic candidate score recomputation | **Keep** | `domain/scoring.py`; model scores are not trusted |
| Duplicate/inverse relationship merge | **Keep** | Deterministic semantic merge in `domain/selection.py` |
| Minimum relationship union covering approved CQ paths | **Keep** | Shortest question-scoped path union plus mandatory relationships |
| N advisory range, no padding, rationale/hard cap | **Keep** | Contract and selection invariants enforce 8-20 advisory, 21-24 rationale, hard 24 |
| K from maximum shortest approved path | **Keep** | Directed CQ paths, normal maximum 3, cited K=4 exception |
| Required role relationships count toward N | **Keep** | Mandatory relationship selection includes completeness role requirements |
| One rendered summary and approve/correct/abort | **Keep** | `render_approval_summary` and CLI decision loop |
| Noninteractive proposal generation | **Keep, change approval** | Automation writes a blocked draft; explicit `domain approve` is required |
| Local duplicate evidence model and locally invented evidence IDs | **Drop** | C0.Core `SourceUnit`/`EvidenceSpan.mint_verified` adapter only |
| Evidence derived from all source files during design | **Change** | Design evidence is sample-only; complete corpus remains a separate immutable manifest |
| Mutable source/profile approval flags | **Drop** | Immutable `DomainDesignContext` and `DomainApprovalContext` |
| Ad hoc JSON/hash helpers | **Drop** | C0 canonical JSON/SHA and deterministic identity helpers |
| Domain/profile hashes stored without manifest/receipt chain | **Change** | C0 input/output manifests, metrics, and stage receipts bind every authority hash |
| Domain-specific completeness groups and built-in counts | **Drop** | Generic `CompletenessRequirementV2`; cardinality requires reviewed support |
| Entity identity includes mutable type or display label | **Drop** | Root/key identity is type-independent; reclassification preserves entity ID |
| Relationship identity depends on endpoint type labels | **Drop** | Predicate plus stable endpoints/context survives reclassification |
| External ontology references without legal approval gate | **Drop** | Metadata-only references with license/legal/provenance approval |
| Unknown observations can expand the working schema | **Drop** | Audit/discovery plus optional rereview signal; no runtime mutation |
| Approved schema 2 enables enrichment immediately | **Drop** | L1 chain validates, but downstream remains fail-closed pending L2 receipts |
| Existing schema-1 workflow is implicitly replaced | **Drop** | Schema 1 remains unchanged behind `--legacy-schema-1` |

## 8. Compatibility and downstream gate

Schema discrimination remains strict. Schema 1 loads, reviews, approves, hashes,
and guards exactly as before. For schema 2, `domain status` can validate the
approved L1 artifact chain, while `ready_for_enrichment` remains false until the
required successor receipt integration is implemented by L2. No enrichment,
publication, deployment, or runtime behavior is part of L1.

## 9. Validation

The merge gate covers:

- unrelated representative domains and absence of built-in domain terms;
- complete corpus inventory versus bounded sample separation and reconciliation;
- exact Unicode-code-point evidence and rejection of invented evidence IDs;
- hierarchy cycles, inherited constraints/endpoints, closure/hash stability, and
  reclassification-stable identity;
- generic completeness, ordering, supported cardinality, and required roles;
- deterministic scoring, duplicate/inverse merge, candidate audit, N, and K;
- one-summary approval/correction/abort and YAML/JSON automation;
- dry-run no-write behavior and content-verified resume/skip;
- C0 manifest/metrics/receipt bindings;
- unchanged schema-1 behavior; and
- approved schema-2 downstream fail-closed behavior.
