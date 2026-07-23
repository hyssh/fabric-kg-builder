# Hockney — History

## Core Context

- **Project:** A Python CLI tool that builds and deploys knowledge graphs and Fabric ontologies from documents/CSV using OpenAI enrichment and canonical Parquet.
- **Role:** Tester
- **Joined:** 2026-06-24T17:38:25.166Z

## Current Sprint

- **2026-06-24 (Tables + Enrichment Hardening):** Completed test tier strategy (fast-by-default, integration opt-in); 10 golden fixture tests added; 745 unit tests passing. See history-archive.md for detailed learnings.

- **2026-07-22 (Ontology Integrity — Issues #7 + #8):** Authored 70 comprehensive unit tests in `tests/unit/test_identity_validation.py` for the planned `ontology/identity_validation.py` module (McManus). Tests cover OKV-001 (relationship key mismatch: identity map resolution, FK alias validity, source/target endpoint mismatch, missing binding, dry-run helpers) and OKV-002 (date precision: YEAR/YEAR_MONTH/FULL_DATE/TIMESTAMP detection, coarsest-wins, event_date as string passes, event_date as timestamp triggers PARTIAL_DATE_INCOMPATIBLE) and post-deploy structural read-back (zero entity/relationship type fails, zero-contextualization fails). All 70 tests fail with ImportError (module absent) — this is the expected pre-implementation gap state. Decision note written to `.squad/decisions/inbox/hockney-ontology-integrity-tests.md`. No final approval yet; McManus must implement the module.

- **2026-07-23 (Final Re-Review — APPROVED):** Keyser's revision fixes all four defects. Code probes confirmed:
  D1: `validate_identity()` now wired into `compiler._validate()` step 7 — OntologyCompiler raises OntologyCompilerError on cross-table domain mismatch; valid FK alias (source_entity_id → entity_id) still passes.
  D2: OKV-002 now fires without datePrecision annotation (name-heuristic via identity_validation); true non-date timestamps unaffected.
  D3: `_validate_parquet_date_precision` now reports `rejected_count` + `affected_entity_names` count in error messages.
  D4: Broad `except Exception` replaced with `except ImportError` + hard `sys.exit(1)` when `total_nodes/total_edges < 0`.
  Full suite: 2264 passed, 0 failed. Targeted suite: 263 passed. APPROVED.
  1. **D1 (Critical):** `identity_validation.validate_identity()` never called in the pipeline — compiler.py `validate_relationship_keys()` is SILENT for cross-table domain mismatches (probe confirmed: 0 errors for DocumentChunk/chunk_id vs source_entity_id). The identity_validation module is dead code relative to the enforcement chain.
  2. **D2 (Critical):** `validate_date_types()` in compiler.py is SILENT unless model has explicit `datePrecision` annotation (probe confirmed: 0 errors for `event_date: timestamp` without annotation). OKV-002 in identity_validation.py correctly fires on name heuristic but is not wired in.
  3. **D3 (Significant):** `_validate_parquet_date_precision` reports sample values `[:3]` only — NOT rejected-value counts or affected entity counts as required by acceptance contract.
  4. **D4 (Moderate):** Broad `except Exception` at deploy_cmd.py L1228 silently skips zero-edge validation when `read_graph_counts` raises — prohibited by contract ("no success-shaped fallbacks or silent skips").
  REJECTED. Revision assigned to **Keyser** (McManus locked out per reviewer-protocol lockout rules).

## 2026-07-23 (Agent Capability Tests — Issues #12, #13, #14)

**Sprint:** Pre-implementation test authoring for scope/agent-capability branch.
**Task:** Write contract-focused tests for three GH issues while Verbal concurrently implemented production code.

### What was done
Authored/revised 5 authorized test files totalling ~370 new lines of tests. Final state: **182 passed, 2 legitimately RED** (both production gaps, not test errors).

**Files modified:**
- `tests/unit/test_agent_contract_validation.py` — classify_relationship_availability (7 tests, all PASS), new error codes (3 tests, all PASS), CompetencyExampleReceipt (3 tests, all PASS), gate_competency_examples four-state gating (9 tests, all PASS), QueryReadiness.observed_relationship_rows (2 tests, all PASS), AgentPublicationReceipt property fields (3 tests, all PASS), property-omission anti-self-referential (3 tests, all PASS)
- `tests/unit/test_agent_instructions.py` — capability-aware build_graph_source_description (5 tests, all PASS), global instruction boundary (3 tests, all PASS)
- `tests/unit/test_data_agent_grounding.py` — property children in public projection (3 tests, PASS), text char counts (3 tests, PASS)
- `tests/unit/test_knowledge_data_agent_helpers.py` — graph_few_shots row-gating (5 tests, 3 PASS / 2 RED), DataSourceElement children (3 tests, PASS)
- `tests/unit/test_deploy_data_agent.py` — AgentPublicationReceipt new char-count fields (4 tests, all PASS)

### Key implementation divergences vs ADR

| ADR plan | Verbal's actual implementation |
|----------|-------------------------------|
| `gate_competency_examples` takes `list[DataAvailability]` | Takes `dict[str, DataAvailability]` keyed by semantic_id (also accepts list via `_normalize_availability`) |
| Per-relationship `requirement` field controls required/optional | `routes.direct_graph: "optional"/"required"` controls case-level required/optional |
| `AgentPublicationReceipt` has `instruction_chars: dict` and `description_chars: dict` | Individual fields: `graph_instruction_chars`, `ontology_instruction_chars`, `graph_description_chars`, `ontology_description_chars` |
| `DataAgentPropertyOmitted(property_id, stage)` | `DataAgentPropertyOmitted(property_id, stage, required_count, actual_count, remediation="")` |
| `DataAgentUnavailableRelationshipClaimed(relationship_id, context)` | `DataAgentUnavailableRelationshipClaimed(relationship_id, availability_class, context="")` |
| `CompetencyExampleReceipt` status: `"published"/"omitted"` | Status: `"pass"/"blocked"/"skipped"/"omitted"` (all valid) |
| `build_public_graph_source_projection` strips children (bug) | Already fixed: children preserved |
| `gate_competency_examples(None, ...)` should return `[]` | Handled (isinstance guard at top) |
| `graph_few_shots_from_competency_contract` filters optional-absent | NOT YET: calls gate for side-effect only, does not filter by receipt |

### Lessons learned

1. **`_required_relationships` uses `expected.relationship_types`, not `probes.direct_graph`** — `gate_competency_examples` extracts relationship IDs from both sources but only `routes.direct_graph` determines case_required.

2. **Case-level vs relationship-level optional**: Production uses case-level `routes.direct_graph: "optional"` to classify required/optional. Test helpers must include this key for optional cases to behave correctly.

3. **Availability format**: `gate_competency_examples` and `graph_few_shots_from_competency_contract` both expect `dict[str, DataAvailability]`. The internal normalization of list→dict exists but callers should pass dicts.

4. **graph_few_shots_from_competency_contract gap**: Calls `gate_competency_examples` purely for the raise-on-required-absent side effect. Does NOT use receipts to filter optional-absent cases from example extraction. This is the production gap causing 2 RED tests.

### Remaining production gaps (2 RED tests)

**`TestGraphFewShotsObservedRowGating::test_optional_case_with_zero_rows_is_silently_omitted`**
**`TestGraphFewShotsObservedRowGating::test_unrelated_required_case_unaffected_by_unavailable_optional`**

Root cause: `graph_few_shots_from_competency_contract` iterates all cases with `probes.direct_graph.static_validation_passed=True` regardless of gating receipts. Optional-absent cases are not filtered out.

Fix required: After `gate_competency_examples`, collect receipts and skip cases where `receipt.published=False`. OR: check `routes.direct_graph` + availability directly during extraction loop.
