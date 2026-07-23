# Session Log: Issues #9, #10 — Data Agent Contract Implementation

**Date:** 2026-07-23  
**Scope:** scope/agent-contract  
**Issues:** #9 (Source Policy), #10 (Text/Few-Shot Limits)  
**Session:** Agent contract implementation batch (Keyser → Verbal → Hockney → McManus → Hockney)  

---

## Summary

Five-agent serial pipeline successfully implemented Issues #9 and #10 (data agent source and text contract enforcement). Verbal's initial implementation was rejected by Hockney (2 critical blockers: open-world source enforcement, missing graph few-shot hard-fail gate). McManus fixed both blockers; Hockney approved. Commit staged and ready (no push/merge per scope rules).

---

## Outcome

| Metric | Value |
|--------|-------|
| Issues resolved | #9, #10 (both complete) |
| Commits staged | 1 (d58887f: feat: enforce data agent source and text contracts) |
| Test pass rate | 2246/2246 unit tests ✅ |
| Blockers fixed | 2/2 (B1: closed-world, B2: few-shot hard-fail) |
| Test additions | 15 new (63 total in test_agent_contract_validation.py) |
| Branch | scope/agent-contract (no merge/push per session rules) |

---

## Key Decisions

1. **SourcePolicy(required, allowed_extra, prohibited)** — Closed-world source enforcement by default
2. **Named text limit constants** — 5 limits defined; prevents scattered assertions
3. **Graph few-shots hard-fail** — Fail if competency contract exists but no few-shots in graph source
4. **Instruction dedup fix** — `build_semantic_data_agent_spec` now generates distinct per-source instructions (not global duplication)
5. **Pre-flight validation** — All checks run before any Fabric mutation; dry-run reports all validation counts

---

## Changes Made (All Staged)

- `src/fabric_kg_builder/knowledge/validation.py` — SourcePolicy, validation APIs, constants
- `src/fabric_kg_builder/deploy/data_agent.py` — Fix instruction duplication
- `src/fabric_kg_builder/knowledge/agent_validation.py` — Integrate source_policy validation
- `src/fabric_kg_builder/cli/deploy_cmd.py` — Pre-flight validation + dry-run reporting
- `src/fabric_kg_builder/cli/build_deploy_cmd.py` — Parity validation
- `tests/unit/test_agent_contract_validation.py` — 63 tests (15 new adversarial)

---

## Next Steps

- Scribe commits `.squad/` files (decisions.md, orchestration-log, session-log)
- Branch remains open for Scope E (#12, #13, #14) consumption of contracts
- No merge/push until broader team coordination

---

## Scope E Dependency Hook

The following APIs are now available for Scope E to build capability-aware agent definitions:

```python
from knowledge.validation import (
    MAX_GLOBAL_INSTRUCTION_CHARS,
    MAX_SOURCE_INSTRUCTION_CHARS,
    MAX_SOURCE_DESCRIPTION_CHARS,
    MAX_FEW_SHOT_COUNT,
    MAX_FEW_SHOT_PAYLOAD_CHARS,
    SourcePolicy,
    validate_source_policy,
    validate_published_source_policy,
    validate_data_agent_text,
    validate_graph_few_shots,
)
```

All validation happens pre-flight; Scope E can assume these slots are contract-compliant.
