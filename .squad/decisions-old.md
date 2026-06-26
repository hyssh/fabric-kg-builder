# Squad Decisions

## Active Decisions

### PRD Kickoff Decomposition (Keyser)

**Date:** 2026-06-24  
**Status:** Proposed

The PRD defines a Python CLI — `fabric-kb` — that ingests documents/CSV, enriches via OpenAI, writes canonical Parquet, compiles Fabric Ontology definitions, and deploys to Microsoft Fabric workspaces.

**Decisions Made:**

| # | Decision | Rationale | Trade-off |
|---|----------|-----------|-----------|
| 1 | Start with CSV-only MVP (Milestone 1) before documents | Reduces unknowns; validates data contract without extraction complexity | Delays document support |
| 2 | CLI framework: `click` | Mature, composable, well-documented. Alternative `typer` adds pydantic dep chain | Slightly more verbose than typer |
| 3 | Parquet library: `pyarrow` | Industry standard, supports complex types, schema enforcement | Large install; `polars` could be lighter but less ecosystem support for Fabric |
| 4 | LLM integration: `openai` SDK with structured outputs | Direct control; structured outputs reduce JSON salvage logic | Harder to swap providers than `litellm` |
| 5 | Config format: `fabric-kb.yaml` at project root | Single config for env, model paths, LLM settings | Must document schema; TOML in pyproject.toml was considered but doesn't nest well |
| 6 | Ontology IDs: deterministic GUID from `ids.lock.json` | Ensures stability across envs per PRD mandate | Requires lock-file discipline |
| 7 | Test strategy: pytest + mocked LLM fixtures | Fast CI; no live API calls in tests | Must separately validate real LLM responses |
| 8 | Deploy via Fabric REST (not fabric-cicd initially) | Direct control, easier debugging; fabric-cicd can be added later as an alternative | More code to maintain for auth/LRO |

**Open Questions (Top 8):**

1. **Auth strategy for Fabric REST**: Service Principal vs managed identity vs user token? → Default: SPN with fallback to `az login` token.
2. **LLM model selection**: GPT-4o vs GPT-4o-mini for enrichment? → Default: GPT-4o-mini for cost; configurable.
3. **Parquet partitioning**: Single file per table or partitioned by entity_type? → Default: Single file per table for MVP.
4. **Ontology versioning**: How to handle breaking schema changes? → Default: Semver in model.yaml; lock-file tracks IDs.
5. **Evidence granularity**: How much source context per fact? → Default: Structured fields (page, section, row, col) per PRD.
6. **Inverse relationships**: Materialized or virtual? → Default: Materialized in Parquet per lessons-learned.
7. **CI/CD pipeline**: GitHub Actions vs Azure DevOps? → Default: GitHub Actions (repo is on GitHub).
8. **Document extraction library**: Azure Document Intelligence vs open-source? → Default: Defer to Milestone 5; design interface now.

---

### System Architecture Locked (Keyser) — SPEC-001

**Date:** 2026-06-24T11:16:49.430-07:00  
**Spec:** docs/specs/SPEC-001-architecture-and-cli.md

**Locked Choices:**

1. **CLI framework:** Click 8.x (not Typer). Mature, no typing-extensions conflicts, better for complex option groups.
2. **Pipeline stages (8):** ingest → enrich → compile-data → compile-ontology → compile-search → package → deploy → validate. Each maps to one or more CLI commands.
3. **Package layout:** 10 modules under `src/fabric_kg_builder/` — cli, config, sources, enrichment, model, parquet, ontology, search, deploy, validate.
4. **Entity/relationship ID generation:** Content-addressed SHA-256 hash (deterministic, collision-resistant). Ontology type IDs from `ids.lock.json`.
5. **Checkpoint strategy:** Per-source-file JSON checkpoints for LLM enrichment. `--resume` continues; `--force` restarts.
6. **Configuration:** Single `fabric-kg.yaml` with `${ENV_VAR}` interpolation. No secrets in files. Precedence: CLI flag > env var > config file > built-in default.
7. **Environment isolation:** Per-env JSON in `ontology/environments/`. Only workspace/lakehouse/blob/search IDs vary. Model and ontology definitions are stable.
8. **AI Search:** Optional and disabled by default. Entire search pipeline is no-op when disabled.

**Recommended (Pending Team Confirmation):**

- Auth: DefaultAzureCredential for all Azure services except OpenAI (uses API key).
- Deployment: fabric-cicd for ontology; direct REST for Lakehouse data + long-running op polling.
- LLM models: gpt-4o for text and vision (Azure OpenAI).
- Document extraction: Azure Document Intelligence (PDF/images), python-docx (DOCX), beautifulsoup4 (HTML).

**Open (Needs Team Input):**

- Dev workspace ID
- Blob Storage account/container per environment
- First sample CSV domain and first document with figures

---

### Canonical Data Model Schema, Type, and ID Strategy (Fenster) — SPEC-002

**Date:** 2026-06-24  
**Spec:** docs/specs/SPEC-002-canonical-data-model.md

**Decision 1: All Data Row IDs Are Deterministic SHA-256 Hashes**

Data row IDs (`entity_id`, `chunk_id`, `relationship_id`, `evidence_id`, `image_id`, `visual_region_id`, `document_element_id`, `source_file_id`) are derived as `{prefix}:{sha256(canonical_string)[:32]}`.

**Rationale:** UUIDs are unstable across runs and environments. Content-hash-derived IDs guarantee that the same input always produces the same ID, enabling cross-environment reproducibility, natural dedup, and incremental re-run detection.

**Decision 2: Entity Canonical Key Normalization is Pinned in Spec**

The `canonical_key` for entities follows the rule: lowercase → strip → collapse whitespace → remove non-alphanumeric except `-` → replace spaces with `-` → prepend `entity_type.lower():`. This rule is stable; changes require a spec version bump.

**Rationale:** The canonical_key is the dedup identity key across sources. Any change to normalization rules would invalidate existing entity_ids and require full re-extraction. Pinning the algorithm prevents silent drift.

**Decision 3: list<string> Columns Are Native Parquet Arrays, Not JSON Strings**

`aliases` (entities) and `related_entity_ids` (chunks) are written as native pyarrow `pa.list_(pa.string())`, not as JSON-encoded strings.

**Rationale:** Native Parquet arrays enable Fabric Lakehouse and Spark to read them directly as `ArrayType(StringType)` without a JSON parse step.

**Decision 4: Placeholder Parquet Uses Subdirectory Layout**

Placeholder files are written to `build/parquet/{table}/_placeholder.parquet`. When real data is produced, it is written to `build/parquet/{table}.parquet` (single file, no subdirectory). The two layouts are mutually exclusive.

**Decision 5: Validation Severity Split — 22 Fail / 8 Warn**

FK violations, duplicate primary keys, schema mismatches, and missing blob_urls (post-upload) are **fail** severity. Orphan nodes, empty evidence, and missing cross-links are **warn** severity.

**Open Questions for Team:**

1. Should `properties_json` on entities and relationships be replaced with a structured `pa.map_(pa.string(), pa.string())` in a future schema version?
2. Should `related_entity_ids` on chunks be a dedicated junction table (`chunk_entity_links`) rather than an array column?
3. Should `image_hash` uniqueness be enforced globally across source files or only per source file?
4. Should the schema-profile.json format be versioned and part of a separate SPEC?

---

### Ontology and Deployment Spec (McManus) — SPEC-003

**Date:** 2026-06-24  
**Spec:** docs/specs/SPEC-003-ontology-and-deployment.md

**Decision 1: `blob_url` is a node property, not a relationship edge**

The PRD §9 traversal shows `stored_at` as the final hop pointing to `BlobUrl`. SPEC-003 implements this as a `blob_url` property of type `blob_url` (format `uri`) on `ImageAsset`, `Figure`, and `VisualRegion` — not as a separate relationship type edge.

**Rationale:** A relationship edge to a literal URL is awkward in Fabric Ontology; properties are the correct mechanism for URI-typed scalar values.

**Decision 2: Inverse relationships are explicit and enumerated**

Every relationship type in `model.yaml` must declare `inversePolicy` as one of `none | materialize | alias`. The compiler must fail if the field is absent.

**Rationale:** The AI Search prototype lost traversal direction because inverses were implicit. Making the policy explicit and checked at compile time prevents silent regression.

**Decision 3: Environment config is injected at compile time**

The `compile-ontology --env {env}` command bakes the Lakehouse ID from `ontology/environments/{env}.json` into the data binding JSON files. The `deploy-ontology` command deploys the pre-baked artifacts without patching.

**Rationale:** Self-contained per-environment artifacts are easier to inspect, diff, and version.

**Decision 4: ID ranges are prefix-based, no per-module sub-ranges within entity types**

Entity type IDs start with `1`, relationship type IDs start with `2`. Within entity types, the hundreds column is used as a loose module grouping (1…99 = support-domain, 100…199 = document-evidence, 200…299 = retrieval), but this is a convention, not enforced by the compiler.

**Open Questions:**

1. Should the compiler support a `--dry-run` flag that validates without writing files?
2. Should `build/ontology/` be committed to source control or treated as a generated artifact only?
3. Should the Fabric sensitivity label GUID be stored in `env.json` directly, or resolved at deploy time from the display name?

---

### LLM Enrichment Spec (Verbal) — SPEC-004

**Date:** 2026-06-24  
**Spec:** docs/specs/SPEC-004-llm-enrichment.md  
**Status:** Draft

**Decision D1: LLM output is intermediate JSON only**

The enrichment pipeline is hard-constrained: LLM output never writes Parquet, AI Search, or ontology definitions directly. All LLM output is structured intermediate JSON written to `build/enriched/`. The canonicalize step (not the LLM) resolves id_hints to stable canonical IDs and applies confidence thresholds.

**Decision D2: id_hint → stable ID hashing deferred to Fenster**

The exact stable ID hashing algorithm (SHA256 prefix, UUID5, or sequential lock) is Fenster's decision. SPEC-004 defines the handoff contract: canonicalize receives id_hint + source_file_id + type and returns a stable string ID.

**Decision D3: Blob URLs are runner-injected only**

The LLM must never generate, invent, or modify Blob URLs. The runner uploads images/figures to Blob Storage before calling the LLM, then injects the resulting URL into prompt context. The LLM echoes it back unchanged.

**Decision D4: Confidence thresholds (configurable defaults)**

- Include in active Parquet records: >= 0.70
- Include as flagged/low-confidence: 0.50 – 0.69
- Drop silently: < 0.50

All thresholds are configurable via `config/enrich.yaml`.

**Decision D5: Default models are open decisions**

Text model and vision model defaults are recommended as `gpt-4o` but are not locked. Both are configured via `config/enrich.yaml` and environment variables. No model names appear in committed config files with secret values.

**Decision D6: JSON Schema validation file must be created before first merge**

`config/schemas/llm-intermediate.schema.json` must exist before the first `enrich` command implementation is merged.

**Decision D7: CLI contract coordination with Keyser**

The `fabric-kg enrich` command signature in SPEC-004 §10.1 is a proposal. Keyser owns the CLI contract. If Keyser's CLI spec contradicts any flag or output path defined here, Keyser's decision takes precedence.

**Open questions inherited from PRD §26:**

| Q# | Question | Blocked work |
|---|---|---|
| Q6 | Text enrichment model? | §8.1 default |
| Q7 | Vision model? | §8.1 default |
| Q11 | Checkpoint design for large jobs? | §9.4 (currently addressed with per-file atomic writes) |

---

### Test Plan and Validation Gate Specification (Hockney) — SPEC-005

**Date:** 2026-06-24  
**Spec:** docs/specs/SPEC-005-validation-and-test-plan.md

**Decision: VAL rule numbering starts at VAL-001 and is stable**

Rule IDs are never reused or renumbered. When a rule is retired it is marked deprecated in the spec, not deleted, so error messages in old log files remain traceable.

**Decision: Severity split: build-fail vs warn**

- 21 of the 22 rules are `build-fail`. Only VAL-022 (env drift) is `warn`, because environment drift is informational — it should alert but not block work that is otherwise valid.

**Decision: No live API calls in unit, contract, or integration tests**

OpenAI, Azure Blob Storage, and Fabric REST are always mocked. This is a hard rule, not a preference.

**Decision: Fixture locations are fixed**

All fixtures live under `tests/fixtures/`. No test may create ad-hoc data inline that is not also committed as a named fixture.

**Decision: 90% coverage gate**

The 90% line coverage floor is a merge gate. It applies to `src/fabric_kg_builder/` only. Test code is excluded.

**Decision: Smoke tests are not merge gates**

Smoke tests run post-deploy to dev. A failing smoke test generates an alert but does not roll back the deploy.

**Open questions passed to the team:**

1. Should VAL-022 (env drift) ever be promoted to `build-fail`? If so, define the allowed delta threshold explicitly.
2. What is the exact token limit for the LLM context overflow check (edge case in §10)? This number needs to come from the AI engineer's model selection.
3. The Fabric REST mock in integration tests needs a canned response shape. Whoever writes the deploy module should supply the fixture format.

## Governance

- All meaningful changes require team consensus
- Document architectural decisions here
- Keep history focused on work, decisions focused on direction
