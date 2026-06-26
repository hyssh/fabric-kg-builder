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

## Learnings

- **README authored** (`README.md` at repo root) — canonical onboarding document covering goal, prerequisites, installation, configuration layers (`.env` / `fabric-kg.yaml` / `ontology/environments/{env}.json`), full end-to-end quickstart against `sample_data\Surface_Troubleshootings`, and a command reference table for all 13 subcommands.
- **Documented command surface:** `init`, `set-domain`, `inspect-source`, `enrich`, `compile-data`, `compile-ontology`, `compile-search`, `package`, `validate`, `build-deploy`, `deploy-lakehouse`, `deploy-ontology`, `deploy-search`. Pipeline stage order confirmed from SPEC-001 note in PRD.md.
- `build-deploy` is registered but not yet fully implemented — noted as a limitation in the README.
- **CLI made fully self-documenting (2026-06-25):** Added `epilog=` to the top-level `@click.group` and all 12 subcommands. Each epilog includes a realistic Windows-path `Example:` section and a contact line (`Questions? hyssh@microsoft.com`). The group epilog additionally shows the numbered 12-stage pipeline overview. Top-level group docstring expanded to describe the end-to-end transformation (documents/CSV → Parquet + Ontology + AI Search). All Click options audited: added `show_default=True` where missing, improved `help=` strings to clarify input types, defaults, and behavior. `context_settings={"max_content_width": 120, "help_option_names": ["-h", "--help"]}` added to the group so `-h` works and long lines render cleanly. **918 tests passed** after the changes — zero functional regressions (all changes were help-text/epilog only). Key lesson: Click's `\b` marker only suppresses re-wrapping for the paragraph immediately following it (before the next blank line); multi-paragraph epilogs need each example block on a contiguous line or in its own `\b` section.
- **Domain Template Playbook documented (2026-06-25):** Added "Domain Template Playbook" section to README.md covering the domain-fit model concept, the Surface/field-service 12-type template (entity types table + relationship table), full step-by-step build with `densify` in the correct position, sample questions with tips, Data Agent grounding pointer, why-densify numbers (3,715→32,118 relationships; 327→8 isolated symptoms), the iteration loop, and industry adaptation examples. Strengthened `cli` docstring + `_GROUP_EPILOG` in main.py (densify inserted as step 4, deploy-ontology notes --multitype). Strengthened `set_domain_cmd` docstring and epilog with 4-point template guidance and a full Surface worked example.
 The original Quickstart used custom `data\surface_kg\...` output paths but chained the commands incorrectly, causing a silent stale-data bug in production (Lakehouse showed only 2 entities). Root cause: `package` reads from `--build-dir` (NOT from `--out`), so passing a custom compile output dir without `--build-dir` silently bundled stale `build\` artifacts; and `deploy-lakehouse --dist X` expects `X\fabric-kg-package\parquet` (the packaged bundle), not a raw parquet dir, falling back silently to `build\parquet` otherwise. **Fix:** Quickstart now uses all-default paths (`build\enriched` → `build\parquet` → `build\ontology` → `build\search` → `dist\fabric-kg-package\`); a ⚠️ "Custom output paths" callout was added explaining the `package --build-dir` / `deploy-lakehouse --dist` gotcha with a concrete `data\surface_kg` example.
