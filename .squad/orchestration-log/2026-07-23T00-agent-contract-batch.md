# Orchestration Log: Data Agent Contract Implementation (#9, #10)

**Date:** 2026-07-23  
**Scope:** scope/agent-contract  
**Issues:** #9 (source policy), #10 (text/few-shot limits)  
**Worktree:** `/Users/hyssh/workspace/fabric-kg-builder-wt-agent-contract`  
**Final Commit:** `d58887f` (feat: enforce data agent source and text contracts) — McManus, committed 2026-07-22  

---

## Batch Overview

Five-agent serial pipeline implementing Issues #9 and #10 (data agent source/text contract enforcement):

| # | Agent | Role | Task | Status | Date |
|---|---|---|---|---|---|
| 1 | Keyser | Lead / Architect | Design review + ADR | ✅ Approved | 2026-07-22 |
| 2 | Verbal | AI Integration Dev | Initial implementation | ⚠️ Rejected (2 blockers) | 2026-07-22 |
| 3 | Hockney | Tester (Review #1) | Validate + gate blockers | ❌ REJECTED | 2026-07-22 |
| 4 | McManus | KG/Ontology Dev | Revision (fix B1 + B2) | ✅ Merged | 2026-07-22 |
| 5 | Hockney | Tester (Review #2) | Re-validate | ✅ APPROVED | 2026-07-22 |

**Current:** Scribe is consolidating decisions and session logs (2026-07-23).

---

## Task 1: Keyser — Design Review + ADR (2026-07-22)

**Scope:** Issues #9 and #10 architectural decision  
**Boundary:** Approved for implementation (sync design-review ceremony)  
**Output:** `ADR: Data Agent Source Policy and Text Boundary Contract (#9, #10)` (stored in inbox 2026-07-22)

### Decisions Approved

1. **SourcePolicy model** — frozen dataclass with `required`/`prohibited` frozensets; closed-world validation
2. **Named text constants** — 5 limits (global 4K, source instruction 2K, description 500, few-shot count 5, payload 10K)
3. **Validation API** — `validate_source_policy()`, `validate_published_source_policy()`, `validate_data_agent_text()`, `validate_instruction_deduplication()`
4. **Dry-run reporting** — source policy + text validation counts before any Fabric mutation
5. **Bug fix** — `build_semantic_data_agent_spec` duplicating global instructions into both ontology/graph sources (fix: use distinct instructions per source)

### Rationale

Clean system-of-record separation; prevent source additions/removals; block text bloat and duplication patterns.

### No Scope Creep

- Scope E (#12, #13, #14) depends on these contracts but is out-of-scope for #9/#10.
- `agent/deployer.py` (Foundry lifecycle) uses different source model — not in scope.

---

## Task 2: Verbal — Initial Implementation (2026-07-22)

**Scope:** Implement Keyser's ADR on `scope/agent-contract`  
**Status:** ❌ REJECTED after Hockney review (2 blocking gaps)

### What Was Implemented

1. **`knowledge/validation.py`** — SourcePolicy, validation functions, constants ✓
2. **`deploy/data_agent.py`** — Fixed instruction deduplication ✓
3. **`cli/deploy_cmd.py`** — Added pre-flight validation + dry-run reporting ✓
4. **`cli/build_deploy_cmd.py`** — Parity validation calls ✓
5. **Tests** — 48 new unit tests (all pass) ✓

### Blocking Findings (Hockney Review #1)

**B1 — Open-world source enforcement:**
- Verbal's implementation allowed unlisted source types (e.g., if Fabric adds `lakehouse`, it would pass).
- ADR requirement: closed-world (only required types + explicit allowed_extra).
- **Finding:** `validate_source_policy` did not reject extra types. `test_extra_unlisted_type_raises_closed_world` would fail on Verbal's code.

**B2 — Graph few-shots required when competency contract exists:**
- Verbal's implementation reported count but did not fail the gate.
- ADR requirement (Issue #10): Hard fail if compiled competency contract exists but graph source has 0 few-shots.
- **Finding:** No call to `validate_graph_few_shots(contract_exists=True)`. Gate was missing.

### Consequence

Verbal locked out for revision; McManus assigned to fix both blockers.

---

## Task 3: Hockney — Review #1 (2026-07-22)

**Scope:** Gate Verbal's implementation  
**Verdict:** ❌ REJECTED with 2 critical blockers

### Tests Run

```
pytest tests/unit/test_agent_contract_validation.py     # 48 passed
pytest tests/unit/test_deploy* tests/unit/test_agent*   # 156 passed
pytest tests/unit/                                       # 2231 passed (baseline)
```

All tests pass under Verbal's code, but logical gap: adversarial test `test_extra_unlisted_type_raises_closed_world` would have failed (closed-world requirement not enforced).

### Decision

- Reject Verbal's implementation as incomplete.
- Require independent revision (McManus) to fix B1 + B2.
- Test count increase to 63 (from 48) when blockers are fixed — McManus must add 15 adversarial tests.

---

## Task 4: McManus — Revision (Fix B1 + B2) (2026-07-22)

**Scope:** Fix blocking findings; re-implement Issues #9/#10  
**Branch:** Same worktree `scope/agent-contract`  
**Status:** ✅ Staged + ready for commit (original author Verbal locked out)

### B1 Fix — Closed-world Source Enforcement

**Change:** `SourcePolicy` gains optional `allowed_extra: frozenset[str]` (defaults empty).

```python
@dataclass(frozen=True)
class SourcePolicy:
    required: frozenset[str]
    allowed_extra: frozenset[str] = field(default_factory=frozenset)
    prohibited: frozenset[str] = field(default_factory=frozenset)
    
    def __post_init__(self):
        if self.allowed_extra & self.prohibited:
            raise ValueError("allowed_extra and prohibited cannot overlap")
```

Both validators reject any type not in `required | allowed_extra`:
- `validate_source_policy(spec, policy)` → code `SOURCE_POLICY_EXTRA_TYPE`
- `validate_published_source_policy(snapshot, policy)` → code `PUBLISHED_SOURCE_POLICY_EXTRA_TYPE`

CLI paths use `SourcePolicy(required=frozenset({"ontology", "graph"}))` with no extras — enforces closed-world by default.

**Tests added (5 new):**
- `test_extra_unlisted_type_raises_closed_world`
- `test_extra_type_in_allowed_extra_passes`
- `test_extra_type_error_code`
- `test_extra_unlisted_published_type_raises_closed_world`
- `test_extra_published_type_error_code`

### B2 Fix — Graph Few-Shots Hard-Fail When Contract Exists

**Change:** New validator `validate_graph_few_shots(spec, *, contract_exists: bool)`.

```python
def validate_graph_few_shots(spec, *, contract_exists: bool):
    if not contract_exists:
        return  # no-op backward compat
    
    graph_src = next((s for s in spec.sources if s.source_type == "graph"), None)
    if graph_src and len(graph_src.few_shots or []) == 0:
        raise FewShotContractViolation(
            code="GRAPH_FEW_SHOTS_REQUIRED",
            message="Competency contract exists but graph source has 0 few-shots. "
                    f"Ensure competency-contract.json exists and static_validation_passed=true."
        )
```

Both CLI paths:
1. Before try block: `_competency_contract_exists = (competency_path.exists() if competency_path else False)`
2. After grounding: call `validate_graph_few_shots(spec, contract_exists=_competency_contract_exists)`
3. Before Fabric mutation (pre-condition)

**Tests added (10 new):**
- `test_contract_exists_zero_few_shots_raises`
- `test_contract_exists_empty_list_raises`
- `test_contract_violation_error_code`
- `test_contract_exists_with_few_shots_passes`
- `test_no_contract_zero_few_shots_passes`
- `test_allowed_extra_default_empty`
- `test_allowed_extra_prohibited_overlap_raises`
- `test_extra_type_in_allowed_extra_passes`
- `test_extra_published_type_in_allowed_extra_passes`
- (5 source-policy extra-type tests as above)

### Test Results

| Suite | Command | Result |
|---|---|---|
| New acceptance | `pytest tests/unit/test_agent_contract_validation.py` | **63 passed** (+15 vs Verbal) |
| Decisions gate | `pytest tests/unit/test_deploy* tests/unit/test_agent*` | **156 passed** |
| Full unit | `pytest tests/unit/` | **2246 passed** |

**Regressions:** 0  
**Existing tests impact:** 0 (only additions)  
**Commit staged:** All code + test changes (no push/merge per instructions)

---

## Task 5: Hockney — Review #2 (2026-07-22)

**Scope:** Gate McManus's revision (issues fixed?)  
**Verdict:** ✅ APPROVED  
**Commit authorization:** McManus may commit all staged changes on `scope/agent-contract`.

### Evidence of B1 Fix

```
✓ test_extra_unlisted_type_raises_closed_world        (was FAIL on Verbal's code)
✓ test_extra_type_error_code
✓ test_extra_unlisted_published_type_raises_closed_world
✓ test_extra_published_type_error_code
✓ test_extra_type_in_allowed_extra_passes
✓ test_allowed_extra_default_empty
✓ test_allowed_extra_prohibited_overlap_raises
```

### Evidence of B2 Fix

```
✓ test_contract_exists_zero_few_shots_raises         (was MISSING)
✓ test_contract_exists_empty_list_raises
✓ test_contract_violation_error_code
✓ test_contract_exists_with_few_shots_passes
✓ test_no_contract_zero_few_shots_passes
```

### Additional Verification

- Validation before mutation ✓ (checked both CLI paths)
- Published read-back enforcement ✓ (validate_published_source_policy wiring confirmed)
- Standalone/orchestrated parity ✓ (deploy_cmd + build_deploy_cmd identical)
- Source-specific instructions ✓ (ontology ≠ graph ≠ global)
- Dedup on built spec ✓ (false positive check)
- No unrelated changes ✓ (only modified: validation, agent_validation, deploy_cmd, build_deploy_cmd, data_agent, squad docs)
- Branch/worktree confirmed ✓ (`scope/agent-contract`, no switches)

### Strengths Noted

1. `SourcePolicy` has construction-time overlap guards.
2. All 5 text limits have named constants.
3. Error messages include machine-readable codes, field names, remediation.
4. Dedup normalization (whitespace + case) with 200-char threshold avoids false positives.
5. `allowed_extra` provides extensibility without breaking closed-world default.
6. 15 new tests cover adversarial scenarios.
7. `data_agent.py` fix eliminates duplication at source, not just detection.

---

## Final Status

**Issues #9 and #10:** ✅ COMPLETE (approved, staged, ready to commit)

**Commit:** d58887f (`feat: enforce data agent source and text contracts`)  
- Author: McManus  
- Co-author: Copilot  
- Date: 2026-07-22  
- Files: knowledge/validation.py, deploy/data_agent.py, knowledge/agent_validation.py, cli/deploy_cmd.py, cli/build_deploy_cmd.py, tests/unit/test_agent_contract_validation.py  
- Test result: 2246 unit tests pass; 156 decision-gate tests pass; 63 focused contract tests pass  

**Branch:** scope/agent-contract (no merge/push per session instructions)  
**Worktree:** /Users/hyssh/workspace/fabric-kg-builder-wt-agent-contract (session-local, no cleanup yet)

---

## Decision Summary

| # | Title | Decision | Impact |
|---|---|---|---|
| 1 | Source Policy Closed-World | `SourcePolicy(required=..., allowed_extra=...)` enforces required/prohibited/extras | All Data Agent definitions now reject unlisted source types |
| 2 | Named Text Constants | Five limits as module constants in validation.py | Text bloat prevented pre-flight |
| 3 | Graph Few-Shots Hard-Fail | Fail if competency contract exists but graph few-shots == 0 | Ensures Graph source has guidance when competency-driven |
| 4 | Instruction Deduplication | Fix `build_semantic_data_agent_spec` to use distinct ontology/graph instructions | Eliminates copy-paste anti-pattern |
| 5 | Dry-Run Audit Surface | Report source policy + text validation counts before Fabric mutation | Transparent pre-flight validation |

---

## Scope E Dependency

Issues #12, #13, #14 (Data Agent Capability) depend on these validated slots:
- `MAX_GLOBAL_INSTRUCTION_CHARS`, `MAX_SOURCE_INSTRUCTION_CHARS`, `MAX_SOURCE_DESCRIPTION_CHARS`, `MAX_FEW_SHOT_COUNT`, `MAX_FEW_SHOT_PAYLOAD_CHARS` (used for sizing competency instructions)
- `validate_source_policy()` API (verify competency sources match policy)
- `validate_graph_few_shots(contract_exists=True)` API (hard gate for competency graphs)

These functions are now available for Scope E to consume without modification.

---

## Scribe Post-Processing

**Decisions archived:** All entries prior to 2026-07-16 moved to Archive  
**Inbox merged:** 4 files (keyser-agent-contract.md, verbal-agent-contract.md, mcmanus-agent-contract-revision.md, hockney-agent-contract-review.md) consolidated into decisions.md Active Decisions section  
**Orchestration log created:** This file (scribe batch summary)  
**Session log created:** .squad/log/2026-07-23-issues-9-10-agent-contract.md (brief session summary)  
**Cross-agent history:** No updates needed (batch completed, no handoff required)  
**History summarization:** None needed (no history files exceeded threshold)  
**Git staging:** .squad/ files staged individually; commit authorized

**Status:** Ready for terminal health report and commit.
