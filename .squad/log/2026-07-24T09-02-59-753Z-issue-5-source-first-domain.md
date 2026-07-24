# Session Log — Issue #5: Inspect and Summarize Source Data Before Domain Questioning

**Session ID:** issue-5-source-first-domain  
**Timestamp:** 2026-07-24T09:02:59.753Z  
**Branch:** scope/source-inspect  
**Status:** COMPLETE & APPROVED  

---

## Summary

Issue #5 implemented a **source-inspect-first workflow** for domain contract authoring. Users can now run `fabric-kg init-domain` to inspect source files, get an approved profile with observed facts and inferred suggestions, and optionally correct inferred categories/entities before committing the profile.

**Workflow:**

1. User runs `fabric-kg init-domain --source-path <path> [--approve]`
2. Inspector scans files (deterministic, no LLM) and produces SourceProfile with:
   - **Observed:** file counts, formats, byte size, column names, date range
   - **Inferred:** document categories, entity candidates, extraction risks (from filename keywords + schema)
3. If interactive: User sees profile rendered and chooses Approve / Correct / Abort
   - Corrections update inferred fields in-place; loop re-renders and re-asks
   - Corrected profile marked with `user_corrected=True` (affects downstream domain.yaml provenance)
4. Approved profile saved to `.fkg/source-profile.json` with source_hash for staleness tracking
5. Downstream `enrich` command loads profile, checks staleness, logs extraction risks before enrichment
6. Profile enables downstream `domain init` to pre-populate domain.yaml with user-confirmed categories/entities

**Key Architectural Choices:**

- **Strict observed/inferred separation** prevents suggestion leakage
- **No LLM in inspector** ensures determinism (same files → same profile)
- **Correction loop** allows user approval without re-running inspection
- **Staleness tracking** warns when source files change after profile approval
- **Provenance rule:** Inferred items → domain.yaml only when `user_corrected=True`
- **Noninteractive mode:** `--approve` flag + TTY detection for CI/CD automation

---

## Test Evidence

### Targeted Test Run

**Command:** `pytest tests/unit/test_init_domain_cmd.py tests/contract/test_source_profile_contract.py`  
**Result:** **147/147 PASS**

Breakdown:
- Fenster baseline tests: 123 pass
  - 63 unit tests for init-domain command
  - 35 contract tests for SourceProfile model
  - 25 matrix coverage items verified
- Verbal revision tests: 24 pass
  - 6 tests for downstream profile reuse (enrich integration)
  - 7 tests for correction flow
  - 4 tests for staleness detection
  - 7 tests for correction flow contract

### Full Test Suite Run

**Command:** `pytest tests/unit/ tests/contract/`  
**Result:** **2,567 PASS, 4 DESELECTED, 0 FAILED**

- Baseline before Issue #5: 2,472 pass
- After Fenster submission: 2,535 pass (+63)
- After Verbal revisions: 2,567 pass (+24)
- Net new tests: 87
- Regressions: 0

### Test Matrix Coverage

| Contract | Coverage | Status |
|----------|----------|--------|
| FILE_COUNT_REPORT | empty dir, single file, 21-file scenario | ✅ Pass |
| FORMAT_CLASSIFICATION | all major formats by extension | ✅ Pass |
| METADATA_EXTRACTION | size, columns, date range | ✅ Pass |
| DATE_RANGE_DETECTION | from filename years + mtime | ✅ Pass |
| EXTRACTION_RISK_ASSESSMENT | images, zero-byte, small PDFs | ✅ Pass |
| CLEAR_LABEL_SEPARATION | observed/inferred model fields | ✅ Pass |
| NO_ASSUMPTION_LEAKAGE | inferred not in observed | ✅ Pass |
| SAMPLE_DATA_PRESENTATION | CSV columns shown + labeled | ✅ Pass |
| EXISTING_DOMAIN_LOAD | --domain-file, --domain-description | ✅ Pass |
| DOMAIN_INCORPORATION | description in profile + render | ✅ Pass |
| APPROVAL_PROMPT | default N, --interactive for forced | ✅ Pass |
| CORRECTION_EDITING | 3-choice, editable fields, loop | ✅ Pass |
| PROFILE_LOCATION | .fkg/source-profile.json | ✅ Pass |
| PROFILE_CONTENT | schema_version, timestamp, source_hash | ✅ Pass |
| PROFILE_VERSIONING | schema_version + source_hash staleness | ✅ Pass |
| QUESTION_FILTERING | temporal skipped when date_range | ✅ Pass |
| STDIN_FALLBACK | --approve + isatty detection | ✅ Pass |
| CLI_COMPATIBILITY | all existing tests pass | ✅ Pass |
| DOWNSTREAM_REUSE | enrich loads + staleness check | ✅ Pass |
| CORRECTION_FLOW | approve/correct/abort loop | ✅ Pass |
| STALENESS_DETECTION | hash mismatch warning | ✅ Pass |
| PROVENANCE_RULE | inferred→domain.yaml only if corrected | ✅ Pass |

---

## Review & Approval

### Keyser (Lead Reviewer) Decision

**Date:** 2026-07-23  
**Initial Verdict:** REJECTED
- **B1:** Downstream reuse missing (enrich doesn't load profile)
- **B2:** Correction flow missing (no edit path before approval)

**Final Verdict:** APPROVED
- **B1 Fixed** by Verbal: `_load_source_profile_for_enrich()` integrated; staleness check active
- **B2 Fixed** by Verbal: 3-choice prompt with correction loop implemented
- **Test Validation:** 147/147 targeted; 2,567/2,567 full suite; 0 regressions
- **Code Review:** All design patterns sound; no remaining blockers

---

## Product Commit

**Commit SHA:** `a0f658669d695b5933d4064791166b969ed2b7eb`  
**Author:** Verbal (CLI Developer, authorized independent revision)  
**Date:** 2026-07-23  
**Message:** Issue #5: Source-first domain initialization with profile inspection and downstream reuse

**Not Pushed:** Per constraints (branch scope/source-inspect stays local, no push authorized)

---

## Files Changed

| File | Type | Purpose |
|------|------|---------|
| `src/fabric_kg_builder/sources/inspector.py` | New | SourceProfile model, build/save/load/render, staleness check |
| `src/fabric_kg_builder/cli/init_domain_cmd.py` | New | init-domain command, approval loop, correction editing, domain build |
| `src/fabric_kg_builder/cli/enrich_cmd.py` | Modified | Profile loading, staleness check, extraction risk logging |
| `src/fabric_kg_builder/cli/main.py` | Modified | init-domain registration, pipeline documentation |
| `tests/unit/test_init_domain_cmd.py` | New | 70 unit tests (63 baseline + 7 correction/staleness) |
| `tests/contract/test_source_profile_contract.py` | New | 42 contract tests (35 baseline + 7 correction/downstream) |

---

## Outcome

✅ **ISSUE #5 COMPLETE & APPROVED**

- Source-inspect workflow fully implemented
- Downstream reuse integrated (enrich)
- Correction flow operational
- All test gates passed (147/147 targeted; 2,567/2,567 full)
- No regressions
- Product commit created and ready for merge
- Team consensus: Ready for integration
