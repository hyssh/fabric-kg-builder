# Frozen PR #32 to L2 parity

Authority: frozen PR #32 / `archive/pr-32-8f3b5eaf` is read-only. No commit from
that PR is cherry-picked, rebased, modified, or closed by this successor.

## File parity

| Frozen file/change | L2 decision | Successor location or rationale |
|---|---|---|
| `docs/specs/SPEC-004-llm-enrichment.md` | Keep/change | Closed vocabulary, bounded work, split-not-truncate, and checkpoint concepts are rewritten for complete-corpus C0 input and proposed-only output. |
| `cli/enrich_cmd.py` | Drop | Schema-2 product CLI activation is explicitly excluded. |
| `domain/guard.py` | Drop | L2 does not enable schema-2 enrichment through legacy guards. |
| `domain/service.py` legacy brief adaptation | Drop/change | L2 consumes the sealed L1 Domain 2 handoff in `enrichment/schema2_sources.py`. |
| `enrichment/orchestrator.py` | Keep/change | Work identities, deterministic split, leaf resume, malformed-response failure, and metrics move to isolated `schema2_work_units.py` and `schema2_stage.py`. |
| `enrichment/output_schema.py` | Change dependency | C0 owns carrier schemas. L2 uses local strict proposal models and edits no contracts. |
| `enrichment/schema2_validation.py` | Split/change | L2 keeps vocabulary/context compilation and identity-safe proposal mapping in `schema2_extraction.py`; evidence, endpoint, direction, and subtype trust remain for L3. |
| `model/schemas.py`, `model/arrow_schemas.py` | Drop | Canonical and Arrow integration belongs to L4. |
| `schema2_exact_relationships.json` | Keep/change | Replaced by proposed-candidate and domain-neutral collection fixtures with untrusted SourceUnit-relative anchors. |
| `test_init_domain_v2_cmd.py` | Drop | Domain initialization belongs to L1. |
| `test_schema2_enrichment_validation.py` | Split | L2 keeps closed-vocabulary, model-ID distrust, stable identity, and malformed-response cases. Exact evidence and terminal validation remain L3 work. |
| `test_schema2_enrichment_work_units.py` | Keep/change | Stable split, no-loss overflow, leaf reuse, corrupt-leaf rerun, atomic overflow, and schema-1 compatibility are L2. Asserted-without-evidence behavior remains L3. |

## Behavior parity

| Frozen behavior | Decision | L2 parity |
|---|---|---|
| Closed approved vocabulary | Keep | Unknown terms produce audit and rereview reasons without schema mutation. |
| Deterministic work identity and structural splitting | Keep/change | IDs also bind complete SourceUnit content and all semantic authorities; over-budget parent output is discarded, never truncated. |
| Successful leaf resume | Keep/change | Exact authority fingerprints permit reuse; corrupt or missing leaf artifacts rerun independently. |
| Malformed candidate failure | Keep | The complete response is parsed atomically with unknown fields forbidden. |
| Exact span verification and evidence ID minting | Move to L3 | L2 stores only proposed offsets, quotes, and untrusted model-authored IDs. |
| Endpoint/direction/subtype validation | Move to L3 | L2 records proposed references but makes no terminal trust decision. |
| Type-dependent entity identity | Replace | Stable entity identity uses the approved hierarchy-root identity policy; classification is separately versioned. |
| Relationship identity tied to endpoint classification | Replace | Stable relationship identity uses approved predicate plus stable endpoint IDs and governed context. |
| Canonical row mutation and Arrow writes | Drop | L2 emits C0 references, accounting, lifecycle proposals, manifests, metrics, and a receipt only. |
| Schema-2 CLI activation | Drop | Isolated Python stage wiring provides dry-run and execution without product activation. |
| Schema-1 compatibility | Keep | Legacy enrichment, output schema, checkpoint, CLI, and canonical tables are unchanged. |
