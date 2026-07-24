# Keyser — History

## Core Context

- **Project:** A Python CLI tool that builds and deploys knowledge graphs and Fabric ontologies from documents/CSV using OpenAI enrichment and canonical Parquet.
- **Role:** Lead / Architect
- **Joined:** 2026-06-24T17:38:25.155Z

## Summary

Sprint 1: Repository scaffold, pyproject.toml, Click CLI framework with 13 subcommands, 11-stage pipeline defined. Sprint 2: package command (dist/ layout), deploy-lakehouse mock (offline env JSON read), compile-search schema generation with locked dimensions (1536). inspect-source command routing files by extension, collecting metadata. Key patterns: function ordering (dict refs), offline config reads avoid credential deps, set-content heredoc on Windows.

**Key decisions:** Entity IDs filterable-only, entity_aliases searchable-only (SPEC-002 §11.4 anti-pattern prevention). Chunk_vector dimensions LOCKED at 1536 (text-embedding-3-large coupling).

**Verification:** dev.json endpoints confirmed. **Tests:** 38 new package/deploy/search tests, 12 inspect tests. **Total:** 346 unit tests passing.

Full history and details in history-archive.md.

## Sprint 3: Ontology Identity Integrity (Issues #7 + #8)

- **Design review completed (2026-07-22):** Issues #7 (relationship key validation) and #8 (partial dates) combined into one branch `scope/ontology-integrity`. Key architecture: new `ontology/identity_validation.py` module with gate IDs `OKV-001` (identity column mismatch) and `OKV-002` (date type incompatibility). `event_date` stays as String, not DateTime — partial dates preserved by design. Post-deployment validation uses structural definition read-back via existing `get_ontology_definition()`, not graph-level row counts (Fabric API limitation).
- **Validation layering pattern confirmed:** `compiler.py._validate()` = "can we build parts" (type-name existence, ID uniqueness). `bridge_validation.py` = "will retrieval traversal work" (BRG gates). New `identity_validation.py` = "will the graph connect at the column level" (OKV gates). `validate/data_gates.py` = "is the data internally consistent" (VAL gates on rows). Four layers, distinct concerns, all composable.

## Learnings

- **README authored** (`README.md` at repo root) — canonical onboarding document covering goal, prerequisites, installation, configuration layers (`.env` / `fabric-kg.yaml` / `ontology/environments/{env}.json`), full end-to-end quickstart against `sample_data\Surface_Troubleshootings`, and a command reference table for all 13 subcommands.
- **Documented command surface:** `init`, `set-domain`, `inspect-source`, `enrich`, `compile-data`, `compile-ontology`, `compile-search`, `package`, `validate`, `build-deploy`, `deploy-lakehouse`, `deploy-ontology`, `deploy-search`. Pipeline stage order confirmed from SPEC-001 note in PRD.md.
- `build-deploy` is registered but not yet fully implemented — noted as a limitation in the README.
- **CLI made fully self-documenting (2026-06-25):** Added `epilog=` to the top-level `@click.group` and all 12 subcommands. Each epilog includes a realistic Windows-path `Example:` section and a contact line (`Questions? hyssh@microsoft.com`). The group epilog additionally shows the numbered 12-stage pipeline overview. Top-level group docstring expanded to describe the end-to-end transformation (documents/CSV → Parquet + Ontology + AI Search). All Click options audited: added `show_default=True` where missing, improved `help=` strings to clarify input types, defaults, and behavior. `context_settings={"max_content_width": 120, "help_option_names": ["-h", "--help"]}` added to the group so `-h` works and long lines render cleanly. **918 tests passed** after the changes — zero functional regressions (all changes were help-text/epilog only). Key lesson: Click's `\b` marker only suppresses re-wrapping for the paragraph immediately following it (before the next blank line); multi-paragraph epilogs need each example block on a contiguous line or in its own `\b` section.
- **Domain Template Playbook documented (2026-06-25):** Added "Domain Template Playbook" section to README.md covering the domain-fit model concept, the Surface/field-service 12-type template (entity types table + relationship table), full step-by-step build with `densify` in the correct position, sample questions with tips, Data Agent grounding pointer, why-densify numbers (3,715→32,118 relationships; 327→8 isolated symptoms), the iteration loop, and industry adaptation examples. Strengthened `cli` docstring + `_GROUP_EPILOG` in main.py (densify inserted as step 4, deploy-ontology notes --multitype). Strengthened `set_domain_cmd` docstring and epilog with 4-point template guidance and a full Surface worked example.
 The original Quickstart used custom `data\surface_kg\...` output paths but chained the commands incorrectly, causing a silent stale-data bug in production (Lakehouse showed only 2 entities). Root cause: `package` reads from `--build-dir` (NOT from `--out`), so passing a custom compile output dir without `--build-dir` silently bundled stale `build\` artifacts; and `deploy-lakehouse --dist X` expects `X\fabric-kg-package\parquet` (the packaged bundle), not a raw parquet dir, falling back silently to `build\parquet` otherwise. **Fix:** Quickstart now uses all-default paths (`build\enriched` → `build\parquet` → `build\ontology` → `build\search` → `dist\fabric-kg-package\`); a ⚠️ "Custom output paths" callout was added explaining the `package --build-dir` / `deploy-lakehouse --dist` gotcha with a concrete `data\surface_kg` example.

## Scope E Pre-Work Design Review (2026-07-23) — Issues #12/#13/#14

- **Facilitated capability-aware Data Agent design review** on branch scope/agent-capability (inherits scope/agent-contract). ADR written to `.squad/decisions/inbox/keyser-agent-capability-plan.md`.
- **Confirmed the three bugs each have existing hooks** (extend, do not fork):
  - **#14 property strip:** `knowledge/agent_validation.py::build_public_graph_source_projection` (L55) sets `children=None`, dropping all selected graph properties. `build_agent_publication_receipt` (L534) compares published vs an already-stripped `expected` (both 0 ⇒ PASS), and copies `property_child_coverage` (always 1.0, computed pre-strip at L404) verbatim into the receipt. Fix: project properties into the Fabric-supported child shape (id/type=`graph.property`/is_selected/data_type/index_state) and validate against `grounding.expected_property_child_count`.
  - **#13 example gating:** `knowledge/data_agent.py::graph_few_shots_from_competency_contract` (L285) gates only on `static_validation_passed`, never observed rows. `semantic/persisted_projection.py::validate_graph_query_readiness` (L749) already computes `counts_by_semantic_id` but discards per-relationship counts. `runtime/acceptance.py::_required_relationships`/`_route_required` already parse case→required-relationship — reuse.
  - **#12 over-claim:** `semantic/instructions.py::build_graph_source_description` hardcodes "warranty, installation, replacement" regardless of availability.
- **Module ownership decided:** immutable pydantic receipts/counts → `semantic/schemas.py`; `ValidationError` subtypes + pure validators → `knowledge/validation.py`; grounding/projection/receipt → `knowledge/agent_validation.py`; capability text → `semantic/instructions.py`; observed-row capture → `persisted_projection.py`.
- **Key constraints:** `AgentPublicationReceipt` is `extra="forbid"` and invariant-checks `property_child_coverage==1.0` — add fields with defaults, do NOT relax the invariant (fix real preservation instead). `DataAvailabilityStatus` is persisted — derive #13's four states via a helper, never widen the enum. Un-stripping graph children changes selection/element hashes ⇒ golden-hash fixtures must be regenerated.
- **New error codes:** `DATA_AGENT_PROPERTY_OMITTED`, `DATA_AGENT_REQUIRED_EXAMPLE_EMPTY`, `DATA_AGENT_UNAVAILABLE_RELATIONSHIP_CLAIMED`.
- **Sequence:** schemas/vocab → #14 → #13 → #12 → CLI wiring (both `deploy_cmd.py::deploy_data_agent_cmd` and `build_deploy_cmd.py::_deploy_knowledge` in parity). Verbal owns all `src/**` (single-author on hot files avoids intra-file collisions); Hockney authors independent tests + reviews; rejection ⇒ McManus revises (lockout).
- **Gate:** targeted 5-file pytest for fast loop; full `pytest` (unit+contract default) green before sign-off.
- **Lesson:** the scope/agent-contract spine (pre-flight validation, publication receipt, property_child_count field, DataAvailability) was deliberately shaped so Scope E only *populates the capability dimension* — every #12/#13/#14 requirement maps onto an already-present slot. Serial B→E→F waves paid off exactly as the wave plan predicted.

## Issue #5 Review — init-domain source inspection (2026-07-23) — VERDICT: REJECT

- Reviewed Fenster's uncommitted impl: `cli/init_domain_cmd.py`, `sources/inspector.py`, `cli/main.py` (registration), tests `unit/test_init_domain_cmd.py`, `contract/test_source_profile_contract.py`.
- **BLOCKER 1 — downstream reuse missing.** Issue explicitly requires "persist the approved summary so later commands use the same context." Only `init_domain_cmd.py` references the profile; `grep` across `cli/enrich_cmd.py`, `compile_data_cmd.py`, `compile_*` shows zero consumers of `.fkg/source-profile.json` / `load_source_profile`. The contract test file's header claims to verify "structural contracts that downstream commands depend on" but never invokes any downstream command — reuse is unimplemented AND unverified. source_hash staleness (Hockney R2) is inert with no consumer.
- **BLOCKER 2 — approve-OR-correct missing.** Issue requires "allow the user to approve or correct the source summary." `_approve_interactively` (init_domain_cmd.py ~L112-148) only accepts y (approve) or n→`sys.exit(4)` (abort). No in-place correction of categories/entities/questions/dates; no inspect→summarize→approve re-loop on rejection. Fenster self-admits. Hockney gates [CORRECTION_EDITING] and [REJECTION_RERUNS] fail.
- Secondary: inferred items (entity_candidates→candidate_model.entity_categories; document_categories→subdomains) copied wholesale into generated domain.yaml on one blanket "y" — weakens provenance ([NO_ASSUMPTION_LEAKAGE]). Contract test mis-titled ("downstream") without exercising a consumer.
- Positives: observed/inferred cleanly separated in `SourceProfile` (deterministic, no LLM); schema_version/source_hash/approver metadata persisted; noninteractive `--approve`/non-TTY path safe; `domain init`/`inspect-source` untouched (compatible); legacy domain.json warning + --force overwrite guard present.
- Live test run BLOCKED by environment (Defender EDR real-time scanning; load avg 8.5+, `python3` import hangs) — not a code fault. Verdict rests on static gaps not covered by any test.
- **Revision owner: Verbal** (CLI/domain workflow: wire enrich/compile-data to load+honor persisted profile w/ staleness check; implement correction/re-loop in init-domain). Hockney to extend tests with real downstream-consumer + edit-then-approve contracts afterward. **Fenster locked out** of these artifacts this cycle.

## Issue #5 — init-domain source inspection (2026-07-23) — REVISION VERDICT: APPROVED

- **Revised implementation owner: Verbal** — authorized independent revision (Fenster locked out per protocol).

- **Blocker 1 — downstream reuse FIXED** ✅
  - `_load_source_profile_for_enrich(source_path, profile_path)` public helper wired into `cli/enrich_cmd.py`
  - Staleness check: `check_source_profile_staleness()` recomputes source_hash from current files, compares to stored hash
  - Soft check (warning on mismatch, enrich still proceeds); profile-less legacy path returns `(None, None)` safely
  - Extraction risks logged pre-enrichment, giving operators visibility into known problem files
  - `--source-profile` CLI option (default: `.fkg/source-profile.json`) for CI/CD override

- **Blocker 2 — approve-OR-correct FIXED** ✅
  - Three-choice prompt: `Approve [y], Correct [c], Abort [n]`
  - Editable fields: document categories, entity candidates, extraction risks, observed date range
  - Correction loop: updates profile in-place, re-renders, re-asks approval (multiple corrections allowed)
  - `user_corrected: bool` field tracks whether inferred items should be promoted to domain.yaml
  - Provenance rule: inferred entities/categories → domain.yaml **only when `user_corrected=True`** (prevents heuristic→fact leakage)
  - `--approve` flag + non-TTY stdin bypass corrections (deterministic, CI/CD safe)

- **Test validation by Hockney:**
  - Targeted: 147/147 pass (123 Fenster baseline + 24 Verbal corrections)
  - Full: 2,567 pass, 4 deselected, 0 failed
  - Zero regressions from baseline through revisions

- **Code review notes:**
  - SourceProfile design sound (observed/inferred enforced at model level)
  - No LLM in inspector (determinism preserved)
  - Profile staleness tracking robust (source_hash recomputed on-demand)
  - Legacy compatibility maintained (no profile → zero behavior change)
  - Correction UX validation future-iteration (not blocker)

- **Decision:** APPROVED. Product commit `a0f658669d695b5933d4064791166b969ed2b7eb` created by Verbal, ready for integration.
