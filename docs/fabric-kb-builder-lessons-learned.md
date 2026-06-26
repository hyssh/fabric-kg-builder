# Lessons Learned From Existing AI Search Graph Work

Date: 2026-06-24
Status: Carry-forward guidance for Fabric KB Builder

## Summary

The storage engine was not the main problem. The hard lessons came from modeling contracts, identity design, provenance, traversal expectations, and mismatches between demo questions and actual relation names.

These lessons should shape the Fabric Ontology + Parquet design.

## Lessons

| Lesson | What happened in current model | What to do differently in Fabric |
|---|---|---|
| Do not confuse retrieval rows with traversal | Early graph mode retrieved edge rows but did not truly walk connected paths. | Test actual multi-hop paths over `relationships.parquet`. |
| Model relation direction explicitly | Data stored `has_component` / `has_part`, while questions often needed `part_of` / `used_in`. | Materialize inverse relationships required by user workflows. |
| Make part numbers first-class entities | Part numbers were often buried as `Spec` or `has_spec` values. | Create `PartNumber` as a first-class entity type. |
| Avoid generic hub nodes | `Battery`, `Motherboard`, and `Screw` became high-degree hubs. | Scope entities by product/model/subsystem; link to global concepts only when useful. |
| Separate canonical identity from labels | Entity labels were used as identity, causing casing/quote/spacing fragmentation. | Use `entity_id`, `canonical_key`, `display_name`, and `aliases`. |
| Preserve document structure | Domain triples alone lost headings, tables, rows, columns, figures, and procedures. | Model document evidence entities and connect facts to them. |
| Tables require row/column semantics | Flattened table text loses the meaning of values. | Store `Table`, `TableRow`, `TableColumn`, `TableCell`, headers, row/col indexes. |
| Procedures are not just text chunks | Troubleshooting instructions need order, tools, warnings, decisions, validation. | Model `Procedure`, `Step`, `DecisionPoint`, `Warning`, `ValidationStep`. |
| Evidence must be precise | Evidence text existed, but source locations were not always machine-addressable. | Store structured provenance with page, section, table, row, col, figure, callout. |
| LLM output is not source of truth | Extraction quality varied by page and prompt. | Validate and canonicalize LLM JSON before writing Parquet. |
| Schema must be stable while extraction evolves | Taxonomy changes can break downstream retrieval. | Keep ontology model and Parquet schemas as stable contracts. |
| Broad and specific questions need different handling | Broad Surface questions needed scope selection. | Add explicit scope fields and ambiguity checks. |
| Do not rely on unsupported inference | AI Search did not infer inverses, equivalence, or hierarchy. | Materialize operational relations in Parquet/Fabric. |
| L1/L2 was useful but too coarse | L1 nodes could contain hundreds of edges. | Add intermediate tiers and split large communities by model/subsystem. |
| Query language must match data model | Demo asked for relations that did not exist. | Add tests that verify every advertised relation exists. |

## Carry-Forward Rules

1. Stable IDs first, labels second.
2. Every fact has evidence.
3. Every relationship has a direction and an inverse policy.
4. Document structure is part of the graph.
5. Parquet tables are the deployment data contract.
6. Ontology definitions are source-controlled.
7. LLM extraction is validated before storage.
8. Technician workflows matter more than nouns alone.

## Validation Checklist For The New CLI

```text
[ ] Every entity has stable entity_id
[ ] No relationship references missing source/target IDs
[ ] Every relationship has evidence_id or source location
[ ] Every table cell preserves row/col/header context
[ ] Every procedure has ordered steps
[ ] Every step has applicable product/model scope
[ ] Part numbers are typed as PartNumber, not raw strings
[ ] High-degree hub nodes are flagged
[ ] Inverse relations exist where required
[ ] Ontology IDs are stable in ids.lock.json
[ ] Parquet schemas match ontology data bindings
[ ] Dev/test/prod deployment uses the same model artifact
```

## Most Important Mistake To Avoid

Do not build the Fabric ontology as a polished schema over messy extracted text.

Build it as a contract over canonical Parquet tables:

```text
LLM extraction
  -> validated canonical entities / relationships / evidence
  -> Parquet tables
  -> Fabric ontology bindings
```

---

## Validated In Practice (2026-06-25)

What we confirmed after building and debugging the live Surface graph + Fabric
Data Agent. These predictions above proved correct; the fixes are now encoded in
the CLI.

| Observed problem | Root cause | Fix (in the CLI) |
|---|---|---|
| Ontology Explorer showed one generic `KGEntity` box | We modelled a single type; the Explorer is a **type/schema view** (one box per *type*, instances bound behind it) | `deploy-ontology --multitype` — one Fabric EntityType per real domain type + typed relationships, each bound to per-type Lakehouse tables |
| Data Agent returned "no data" for `DeviceModel → Component/Part` | **Sparse graph** — per-section extraction left device models disconnected from their own parts/procedures | `densify` source-document **hub edges** (DeviceModel→Component/Part/Procedure/Symptom) |
| "What causes a swollen battery / how resolved" returned nothing | 79 % of Symptoms had **no edges** to Cause/Resolution | `densify --link-scr` — keyword-gated Cause→Symptom→Resolution + transitive `addressed_by` |
| "List the steps for procedure X" returned nothing | Only **2 %** of Procedures had any `has_step` edge | `densify --link-steps` — reconstruct `has_step` by document **reading order** (page+sort_order). Coverage → 27 % |
| Valid GQL but 0 rows: exact-match on names | Agent used `display_name = "Surface Pro 10"`; real node is "Surface Pro 10th Edition for Business" | Generated `data-agent-instructions.md` enforces **CONTAINS** + "discover real DeviceModel names first" |
| Agent guessed wrong edge name (`supported_model_has_component`) | Hand-written grounding drifted from the live schema | Grounding is **auto-generated from the live graph** on every `deploy-ontology --multitype` (exact edge names) |
| Verbatim step text not in the graph | Graph holds short Step *labels*; full instructions live in chunks | Grounding routes verbatim-text questions to **AI Search** (`kg-*-kg-chunks`) as a second Data Agent source |
| Fear of losing edges across re-runs | — | `compile-data` **additivity guard**: fails (exit 5) if any input entity/relationship id is dropped. Densify is append-only by contract |

### Reproducibility rules added

1. **The pipeline is strictly additive.** `densify` only appends; `compile-data`
   enforces it. Old edges are always preserved to keep supporting more use cases.
2. **Grounding is generated, not hand-maintained.** It always matches the
   deployed graph — never let it drift.
3. **Iterate without re-enriching.** Enrichment is the only costly stage. Densify
   → compile → deploy reuse enriched data, so model changes are cheap.
4. **One runnable recipe.** `scripts/reproduce-surface-kg.{ps1,sh}` encodes the
   full, ordered pipeline so anyone can rebuild the graph from scratch.

### Partially addressed (2026-06-25): RCA diagnostic path

The reviewer correctly noted the ontology was a *repair* graph, not a
*troubleshooting/RCA* graph. We added `densify --link-rca`, which makes each
Symptom the hub of a root-cause-analysis answer using **real** entities already
in the corpus:

```
Cause --causes--> Symptom --diagnosed_by--> Procedure (diagnostic test: SDT, inspection, ...)
                    |  \--remediated_by--> Procedure (repair) --has_step--> Step
                    \--resolved_by--> Resolution
```

Diagnostic procedures (SDT, battery status checks, inspections, validations)
are classified by name and linked as `diagnosed_by` — so the agent grounds RCA
answers in documented diagnostics instead of LLM guesses.

### Still open (data-limited, not tooling-limited)

A *finer* RCA chain (Symptom → DiagnosticTest → **Observation** → Cause →
Resolution, with confidence scores) needs explicit observation/decision-point
data that Surface **repair manuals** mostly do not contain. Adding
`Observation` / `FailureMode` entity types helps only for corpora that include
diagnostic decision trees; synthesising them with an LLM risks the hallucinated
fallback we are avoiding. The current `diagnosed_by` / `remediated_by` edges are
the highest-fidelity RCA achievable from this source material.

