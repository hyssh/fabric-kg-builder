# SPEC-004: LLM Extraction and Enrichment

**Status:** Draft  
**Date:** 2026-06-24  
**Revised:** 2026-06-24T13:24:31.077-07:00  
**Author:** Verbal (AI Integration Dev)  
**PRD refs:** §6, §10, §13, §15  

### Revision History

| Date | Author | Summary |
|------|--------|---------|
| 2026-06-24 | Verbal | Initial draft |
| 2026-06-24T11:46:10.517-07:00 | Verbal | Switch LLM SDK to Microsoft Foundry SDK (Azure AI Foundry); secrets via `.env` only; add Domain Intake & Prompt Injection (§2); Doc Intelligence as primary visual extractor (§8); AI Search second-query grounding (§12) |
| 2026-06-24T12:42:17.255-07:00 | Verbal | Part 1: Set GPT-5.5-mini as default chat/enrichment model (interim dev gpt-4.1), text-embedding-3-large @1536 as default embedding (text-embedding-3-small @1536 fallback); add `chat_deployment`, `embedding_deployment`, `embedding_dimensions` keys to §9.2; add AI Search env vars to §9.4; close Appendix B Q6/Q7. Part 2: Expand §12 with two-phase retrieval algorithm (Phase 1 GQL + Phase 2 hybrid), concrete AI Search request body (RESEARCH-001 §8), filter/query-term rule table, fallback branch, numbered-SOURCES grounded-answer prompt, confidence/anti-hallucination guardrails, multi-turn Agentic Retrieval option, structured-data boundary reaffirmation. |
| 2026-06-24T13:24:31.077-07:00 | Verbal | Canonical-naming reconciliation (coordinator-canonical-naming.md): (1) env vars → `AZURE_AI_FOUNDRY_ENDPOINT` / `AZURE_AI_FOUNDRY_API_KEY`; yaml `foundry.endpoint: ${AZURE_AI_FOUNDRY_ENDPOINT}` + `foundry.project`; model keys moved to `enrichment:` section (`enrichment.chat_deployment`, `enrichment.embedding_deployment`, `enrichment.embedding_dimensions`, `enrichment.vision_deployment`); removed `text_deployment` alias and `foundry.project_endpoint` literal. (2) Pipeline block `compile-search-index` → `compile-search`; §2.5 + §11.1 updated to canonical `enrich --domain-prompt / --domain-file` and `set-domain --prompt`. (3) Vision default = chat deployment (gpt-4.1 interim / GPT-5.5-mini target; `gpt-4o` / example-vision documented as alternative); Appendix B Q7 closed. Endpoints are `${ENV_VAR}` in yaml; literal values in `.env` only. |
| 2026-06-24T15:41:07.842-07:00 | Verbal | v5 — Enrichment default corrected to gpt-5.4-mini (deployment `gpt-5-4-mini`, 200K TPM GlobalStandard); removed gpt-5.5-mini references (model does not exist); added 200K TPM minimum requirement in §9.2; updated model defaults summary table; updated Appendix B Q6; AI Search scope corrected to IN MVP; reference REQUIREMENTS-001. |
| 2026-06-24T21:46:59.576-07:00 | McManus | Document Intelligence table approach (coordinator-tables-via-docintel.md, verified 2026-06-24): §7.3 Table Chunking rewritten — tables extracted by DI Layout (outputContentFormat=markdown), not LLM; table_row no longer produced by enrichment pipeline; §6.2 system prompt extended to ban table_row/table_cell emission; §8.6 added — DI Layout table extraction pipeline, HTML artifact flow (table_n.html), MS Learn citations (prebuilt/layout + RAG semantic chunking), validation proof (Surface PDF → 2 table_html chunks), reference implementations. |
| 2026-08-23 | Copilot | Added schema-2.0 closed-vocabulary, exact-evidence, subtype, lifecycle, and shared-K contracts. |
| 2026-08-24 | Copilot | Clarified that C0.Core contract references are owned by SPEC-006 and do not activate L2 extraction or L3 evidence validation. |
| 2026-08-25 | Copilot | Activated isolated L2 schema-constrained extraction after approved L1; separated proposed source anchors and candidate accounting from L3 evidence verification and terminal validation. |
| 2026-08-25 | Copilot | Activated the isolated local-only L3 evidence-validation successor (§12.13) with exact EvidenceSpan 1.1 minting, endpoint grounding, hierarchy/identity validation, append-only lifecycle, and generic required-member sealing; serving, publication, and CLI activation remain excluded. |
| 2026-08-25 | Copilot | Made L3 fail closed on every fact the frozen L2 carrier cannot prove (relationship direction, property owner/value, non-reproducible identity witness, design-evidenced counts), added leaf-checkpoint payload hashing with lifecycle cross-checking, proved the sealed/unresolved collection carriers partition every proposal, and scoped mutable run artifacts by input fingerprint. |
| 2026-08-25 | Copilot | Required exact required-member policy binding to the sealed `CompletenessRequirementV2`, full re-derivation of every published leaf for every candidate kind, and leaf fingerprints bound to the complete canonical `SourceUnit` artifact. |

---

## 1. Scope and Purpose

This spec defines the behavior of the LLM extraction and enrichment stage in the fabric-kg-builder pipeline. It covers the 12 required LLM tasks, the intermediate JSON contract the LLM must produce, how that contract is canonicalized, prompt architecture, chunking behavior, image/figure understanding, model configuration, reliability requirements, and the `enrich` CLI command contract.

For schema 2.0, the shared identity, SourceUnit, EvidenceSpan, lifecycle,
accounting, assertion-reference, manifest, and receipt shapes are imported from
SPEC-006. LLM output may propose candidates and evidence hints, but may not mint
verified evidence IDs, transition terminal lifecycle records, or author
cross-layer hashes. C0.Core itself adds no extraction activation.

### 1.1 Core Principle (PRD §6)

```
LLM output is INTERMEDIATE JSON only.

The LLM stage NEVER:
  - writes Parquet tables directly
  - writes Azure AI Search index documents directly
  - writes Fabric Ontology definitions directly
  - generates or resolves stable canonical IDs

The LLM stage ONLY:
  - reads source context (CSV rows, document spans, chunks, image bytes)
  - produces structured intermediate JSON (see §4)
  - attaches confidence scores and rationale hints
  - proposes id_hints that the canonicalize step resolves to stable IDs

Canonical Parquet is the data contract (PRD §6).
Fabric Ontology is the semantic layer (PRD §6).
Azure AI Search is an optional retrieval layer (PRD §6).
```

### 1.2 Position in Pipeline

```
Source files
  -> inspect-source (schema profile)
  -> domain-intake (THIS SPEC §2)
      -> user provides domain prompt
      -> LLM normalizes to domain brief
      -> domain brief stored in build/enriched/domain.json
  -> enrich (THIS SPEC)
      -> LLM passes produce intermediate JSON
      -> validation against JSON Schema
      -> canonicalize: id_hints -> stable IDs
      -> write build/enriched/*.json
      -> write build/enriched/schema-profile.json
  -> compile-data  (Fenster: Parquet writer — out of scope here)
  -> compile-ontology / compile-search / deploy-*
```

---

## 2. Domain Intake & Prompt Injection

> **Pre-enrichment step.** Before any extraction pass runs, the user supplies a **domain prompt** that narrows and constrains extraction. This section defines how that prompt is processed and how it flows into every downstream enrichment pass.

### 2.1 Purpose

Without domain context, the LLM extracts entities and relationships based on statistical priors — it may extract generic noun phrases where you want specific domain types, or map columns to wrong ontology categories. A domain brief anchors every pass to the user's intent.

Examples of domain prompts:
- `"Surface laptop service and repair documentation — extract hardware components, part numbers, procedures, and safety warnings"`
- `"Clinical trial protocol documents — focus on interventions, endpoints, adverse events, and patient cohorts"`
- `"Industrial IoT sensor network — devices, sensors, telemetry channels, and maintenance procedures"`

### 2.2 Domain Prompt Rephrase Pass

The user's raw domain text is **not** used directly. The runner first calls the LLM to normalize it into a **domain brief**:

| Step | Input | Output |
|------|-------|--------|
| User provides raw domain text | Free-form text, any length | — |
| Rephrase pass (LLM call) | Raw user text | Normalized domain brief + entity-type hint list |
| Store | — | `build/enriched/domain.json` |
| Inject into enrichment | — | All P1–P8 prompts receive domain brief in user message |

**Rephrase pass contract:**

```json
{
  "domain_brief": "<1–3 sentence normalized description of the target domain>",
  "key_entity_types": ["<string>", "..."],
  "key_relationship_types": ["<string>", "..."],
  "extraction_constraints": ["<string — e.g. 'focus only on hardware components not software features'>", "..."],
  "source_domain_text": "<the original user-provided text, preserved verbatim>"
}
```

Storage: `build/enriched/domain.json`. This file is read at the start of every enrichment pass and its `domain_brief` and `key_entity_types` are injected into the user message of each pass call (see §2.4).

### 2.3 ⚠️ Security Requirement: Domain Text in USER Message Only — Never System Prompt

**This is a hard security constraint, not a preference.**

**Rule:** The user-provided domain text, and the rephrased domain brief derived from it, must ONLY appear in the **user** message of each LLM call. They must NEVER be injected into the **system** (developer) message.

**Why this matters:**

The system/developer message is the trusted instruction layer. It defines the model's role, output contract, safety constraints, and extraction rules. It is written and controlled by the application developer — it is not user input.

If user-supplied text is placed in the system message, it gains the same trust level as developer instructions. A malicious or accidentally adversarial domain prompt could then:
- override output format constraints
- suppress safety or confidence guidelines
- instruct the model to ignore canonicalization rules
- exfiltrate prompt contents

This is a **prompt-injection / privilege-escalation** attack vector. The defense is strict separation: the system prompt stays fixed and developer-controlled; the user's domain context is clearly delimited user content.

**Required message structure for all enrichment passes:**

```
[system message]
  Role: "You are an expert knowledge extraction assistant..."
  Output contract: JSON schema enforcement
  Constraints: blob URL rules, confidence scoring rules
  ← FIXED. Written by developer. Never modified by user input.

[user message — domain context block]
  --- DOMAIN CONTEXT (user-provided, normalized) ---
  Domain: {domain_brief}
  Key entity types: {key_entity_types}
  Constraints: {extraction_constraints}
  --- END DOMAIN CONTEXT ---
  ← DELIMITED. Clearly marked as user context, not instructions.

[user message — source context]
  Source file: {source_file_id}
  ...{source-specific content}...

[user message — task]
  Extract {pass_description} from the source context above.
  ...
```

The delimiters (`--- DOMAIN CONTEXT ---` / `--- END DOMAIN CONTEXT ---`) are a defense-in-depth measure to ensure the model treats this block as contextual data, not authoritative instructions.

### 2.4 How Domain Brief Flows into Enrichment Passes

Every P1–P8 call receives the domain brief in the user message context block:

| Pass | How domain brief is used |
|------|--------------------------|
| P1 Schema inference | Narrows column→ontology type mapping to domain-relevant types |
| P2 Entity extraction | Biases extraction toward `key_entity_types`; suppresses off-domain candidates |
| P3 Relationship extraction | Uses `key_relationship_types` as preferred relationship vocabulary |
| P4 Normalization | Applies domain-specific abbreviation hints from `extraction_constraints` |
| P5 Evidence linking | No change in behavior; domain brief included for consistency |
| P6 Visual description | Guides callout and label identification toward domain objects |
| P7 Summarization | Biases search-friendly summaries toward domain terminology |
| P8 Placeholder suggestion | Uses `key_entity_types` to identify domain-relevant missing concepts |

### 2.5 Storage and CLI Coordination

**Storage:** `build/enriched/domain.json` (written by the rephrase pass before any P1–P8 run)

**CLI coordination:** The canonical CLI names (from coordinator-canonical-naming.md) are:
- `set-domain --prompt "..."` — persist a domain brief before running enrichment.
- `fabric-kg enrich --domain-prompt "..."` — supply domain inline for a single run (no persistence).
- `fabric-kg enrich --domain-file <path>` — load a pre-written domain brief file; rephrase pass is skipped and the file is loaded directly.

These are defined as the authoritative contract in Keyser's SPEC-001. This spec defines prompt usage only — do not contradict SPEC-001.

**Re-running:** If `build/enriched/domain.json` already exists and `--force` is not set, the rephrase pass is skipped. Use `--force` to re-run the rephrase and update the domain brief.

---

## 3. The 12 Required LLM Tasks

PRD §15 lists 12 required LLM tasks. This section organizes them into named pass types, maps each task to a pass, and defines the expected output per pass.

### 3.1 Pass Types

| Pass | Name | PRD §15 tasks covered |
|---|---|---|
| P1 | Schema inference | 1, 2 |
| P2 | Entity extraction | 3, 12 |
| P3 | Relationship extraction | 4, 12 |
| P4 | Normalization | 5 |
| P5 | Evidence linking | 6 |
| P6 | Visual description | 7, 8, 9 |
| P7 | Chunk and table summarization | 10 |
| P8 | Placeholder suggestion | 11 |

### 3.2 Pass Definitions

#### P1 — Schema Inference

**Trigger:** CSV/XLSX sources, or any source before entity extraction.  
**Input:** Column headers, sample rows (up to 20), document section headings.  
**Output fields:** `schema_profile` object (see §4.8).  
**Tasks:**
- Infer semantic meaning of columns or document spans (PRD §15 task 1).
- Map columns or spans to ontology types such as `Device`, `Component`, `PartNumber`, `Procedure`, `Step` (PRD §15 task 2).

#### P2 — Entity Extraction

**Trigger:** All source types.  
**Input:** Source rows, document chunks, table cells, image descriptions from P6.  
**Output fields:** `entities[]` (see §4.2).  
**Tasks:**
- Extract candidate entities with type, label, aliases, description (PRD §15 task 3).
- Assign confidence and rationale (PRD §15 task 12).

#### P3 — Relationship Extraction

**Trigger:** After P2 entities are available.  
**Input:** Source context + P2 entity id_hints.  
**Output fields:** `relationships[]` (see §4.3).  
**Tasks:**
- Extract candidate relationships between entity id_hints (PRD §15 task 4).
- Assign confidence and rationale (PRD §15 task 12).

#### P4 — Normalization

**Trigger:** After P2 entities are available.  
**Input:** Raw entity labels and aliases from P2.  
**Output fields:** Updates to `entities[].canonical_name`, `entities[].aliases`.  
**Tasks:**
- Normalize names and aliases to a consistent form (PRD §15 task 5).

> Note: P4 may be merged into P2 for simple sources. For large or noisy sources, run as a separate pass.

#### P5 — Evidence Linking

**Trigger:** After P2/P3 entities and relationships are available.  
**Input:** Source spans, table cells, row indices, page numbers, callout labels.  
**Output fields:** `evidence[]` (see §4.7).  
**Tasks:**
- Identify evidence spans, source rows, chunks, table cells, image regions, or callouts that support each entity or relationship (PRD §15 task 6).

#### P6 — Visual Description

**Trigger:** Image and figure sources (PDF figures, DOCX inline images, standalone images).  
**Input:** Image bytes (via vision model), blob_url, nearby caption text, alt text.  
**Output fields:** `visual_assets[]`, `visual_regions[]` (see §4.5, §4.6).  
**Tasks:**
- Describe figures, diagrams, screenshots, and photos (PRD §15 task 7).
- Extract visual labels, callouts, OCR text, and component candidates (PRD §15 task 8).
- Link visual regions to entity id_hints where supported (PRD §15 task 9).

#### P7 — Chunk and Table Summarization

**Trigger:** All chunk types, especially `table_html`, `table_row`, `image_description`.  
**Input:** Chunk text or HTML, table cell structure.  
**Output fields:** `chunks[]` with `summary` field (see §4.4).  
**Tasks:**
- Produce search-friendly summaries for chunks, tables, and visual assets (PRD §15 task 10).

#### P8 — Placeholder Suggestion

**Trigger:** After all other passes. Run when schema profile or entity list implies a concept exists but no instance was found.  
**Input:** Schema profile, entity list, relationship list.  
**Output fields:** `placeholder_suggestions[]` (see §4.9).  
**Tasks:**
- Suggest missing placeholders when data implies a concept exists (PRD §15 task 11).

---

## 4. Intermediate JSON Contract

The LLM produces a single intermediate JSON object per source file or batch. This contract is what the canonicalize step consumes. The LLM never writes directly to Parquet or any other storage.

### 4.1 Top-Level Structure

```json
{
  "source_file_id": "<string — passed in by the runner>",
  "pass": "<string — p1 | p2 | p3 | p4 | p5 | p6 | p7 | p8>",
  "schema_profile": { ... },
  "entities": [ ... ],
  "relationships": [ ... ],
  "chunks": [ ... ],
  "visual_assets": [ ... ],
  "visual_regions": [ ... ],
  "evidence": [ ... ],
  "placeholder_suggestions": [ ... ]
}
```

- `source_file_id` is injected by the runner; the LLM echoes it back for traceability.
- `pass` identifies which extraction pass produced this object.
- All top-level arrays are optional and may be empty `[]` if the pass does not produce that output type.

### 4.2 `entities[]`

```json
{
  "id_hint": "<string — human-readable, scoped identifier>",
  "type": "<string — ontology entity type, e.g. Device | Component | Part | PartNumber | Procedure | Step>",
  "label": "<string — display name>",
  "canonical_name": "<string | null — normalized form from P4>",
  "aliases": ["<string>"],
  "description": "<string | null>",
  "confidence": "<number — 0.0 to 1.0>",
  "rationale": "<string | null>",
  "source_spans": ["<string | null — evidence id_hints or span refs>"]
}
```

**`id_hint` semantics:**
- A human-readable scoped slug chosen by the LLM to identify this candidate entity within the current extraction run.
- Format convention: `{scope}:{type-slug}:{label-slug}`, e.g. `surface-laptop-5:component:battery`.
- `id_hint` values are NOT stable IDs. The canonicalize step (§5) resolves them to stable canonical IDs.
- `id_hint` must be unique within a single intermediate JSON output object.
- The LLM must use the same `id_hint` consistently when referencing the same entity across `entities`, `relationships`, `evidence`, and `visual_regions` within one output.

**Confidence ranges:**

| Range | Meaning |
|---|---|
| 0.90 – 1.00 | High — likely correct, minimal ambiguity |
| 0.70 – 0.89 | Medium — good candidate, may need review |
| 0.50 – 0.69 | Low — plausible but uncertain |
| 0.00 – 0.49 | Very low — flag for human review or drop |

**Required fields:** `id_hint`, `type`, `label`, `confidence`  
**Optional fields:** `canonical_name`, `aliases`, `description`, `rationale`, `source_spans`

### 4.3 `relationships[]`

```json
{
  "id_hint": "<string — unique within this output>",
  "source_id_hint": "<string — references entities[].id_hint>",
  "relation": "<string — ontology relationship type, e.g. has_component | has_part | evidenced_by>",
  "target_id_hint": "<string — references entities[].id_hint>",
  "evidence_id_hint": "<string | null — references evidence[].id_hint>",
  "confidence": "<number — 0.0 to 1.0>",
  "rationale": "<string | null>"
}
```

**Required fields:** `id_hint`, `source_id_hint`, `relation`, `target_id_hint`, `confidence`  
**Optional fields:** `evidence_id_hint`, `rationale`

### 4.4 `chunks[]`

```json
{
  "id_hint": "<string — unique within this output>",
  "chunk_type": "<string — section_text | procedure_step | table_html | table_row | figure_caption | image_description | ocr_text | warning | note | raw_page_text>",
  "content": "<string — text for retrieval>",
  "content_html": "<string | null — HTML representation for table chunks>",
  "summary": "<string | null — LLM-generated search-friendly summary from P7>",
  "embedding_text": "<string | null — text prepared for embedding (see §7.4)>",
  "blob_url": "<string | null — blob URL for visual chunks; MUST be a pre-existing URL passed in by the runner, never minted by LLM>",
  "page_number": "<integer | null>",
  "section_path": "<string | null>",
  "table_id": "<string | null>",
  "figure_id": "<string | null>",
  "image_id": "<string | null>",
  "related_entity_id_hints": ["<string>"],
  "confidence": "<number | null — 0.0 to 1.0>"
}
```

**Required fields:** `id_hint`, `chunk_type`, `content`  
**Optional fields:** all others

> **Blob URL rule:** The LLM must never generate or invent Blob URLs. If a chunk refers to a visual asset, the runner injects the pre-existing Blob URL into the prompt context, and the LLM echoes it back unchanged.

### 4.5 `visual_assets[]`

```json
{
  "id_hint": "<string — unique within this output>",
  "asset_type": "<string — figure | inline_image | screenshot | diagram | photo | chart | table_image>",
  "caption": "<string | null>",
  "alt_text": "<string | null>",
  "blob_url": "<string — MUST be the pre-existing Blob URL passed in by the runner>",
  "description": "<string | null — LLM-generated visual description from P6>",
  "page_number": "<integer | null>",
  "section_path": "<string | null>",
  "confidence": "<number — 0.0 to 1.0>"
}
```

**Required fields:** `id_hint`, `asset_type`, `blob_url`, `confidence`  
**Optional fields:** `caption`, `alt_text`, `description`, `page_number`, `section_path`

> **Blob URL rule:** Same as chunks. The runner always provides the Blob URL; the LLM must echo it unchanged.

### 4.6 `visual_regions[]`

```json
{
  "id_hint": "<string — unique within this output>",
  "image_id_hint": "<string — references visual_assets[].id_hint>",
  "region_type": "<string — callout | ocr_text | component_region | connector_region | warning_region | table_region>",
  "label": "<string | null>",
  "text": "<string | null>",
  "polygon_json": "<string | null — JSON-encoded polygon or bounding box>",
  "identified_entity_hint": "<string | null — references entities[].id_hint>",
  "blob_url": "<string | null — parent or cropped region Blob URL; runner-injected only>",
  "confidence": "<number — 0.0 to 1.0>"
}
```

**Required fields:** `id_hint`, `image_id_hint`, `region_type`, `confidence`  
**Optional fields:** all others

### 4.7 `evidence[]`

```json
{
  "id_hint": "<string — unique within this output>",
  "source_type": "<string — csv_row | document_span | table_cell | figure_callout | image_region | ocr_text | chunk>",
  "page_number": "<integer | null>",
  "section_path": "<string | null>",
  "table_id": "<string | null>",
  "row_index": "<integer | null>",
  "col_index": "<integer | null>",
  "figure_id": "<string | null>",
  "image_id": "<string | null>",
  "callout_id": "<string | null>",
  "visual_region_id_hint": "<string | null — references visual_regions[].id_hint>",
  "blob_url": "<string | null — runner-injected; never minted by LLM>",
  "text": "<string | null — supporting text or value>"
}
```

**Required fields:** `id_hint`, `source_type`  
**Optional fields:** all others

### 4.8 `schema_profile` (P1 output)

```json
{
  "inferred_domain": "<string | null — e.g. hardware-support, technical-documentation>",
  "column_mappings": [
    {
      "source_column": "<string>",
      "ontology_type": "<string | null>",
      "ontology_property": "<string | null>",
      "confidence": "<number — 0.0 to 1.0>",
      "notes": "<string | null>"
    }
  ],
  "inferred_entity_types": ["<string>"],
  "inferred_relationship_types": ["<string>"]
}
```

### 4.9 `placeholder_suggestions[]` (P8 output)

```json
{
  "concept": "<string — e.g. Device, PartNumber>",
  "reason": "<string — why the LLM infers this concept exists>",
  "example_labels": ["<string>"],
  "confidence": "<number — 0.0 to 1.0>"
}
```

---

## 5. Canonicalization Spec

The canonicalize step runs after LLM extraction and before the Parquet writer. It is a deterministic Python process — not an LLM call.

### 5.1 id_hint → Stable Canonical ID

The LLM produces `id_hint` values (scoped slugs). The canonicalize step converts these into stable canonical IDs.

**Handoff contract:**

| Step | Responsibility |
|---|---|
| LLM | Produce consistent, human-readable `id_hint` values within one output |
| Canonicalize | Hash or derive stable IDs from `id_hint` + `source_file_id` + `type` |
| Canonicalize | Cross-reference against existing `entities.parquet` to detect matches and merges |
| Parquet writer (Fenster) | Write the stable IDs to Parquet columns |

> **Deferred to Fenster's data-model spec:** The exact hashing algorithm (SHA256 prefix, UUID5 namespace, or sequential lock) is owned by Fenster's data-model spec. This spec only defines the handoff: the canonicalize step receives `id_hint` + `source_file_id` + `type` and must return a stable string ID suitable for `entity_id`, `relationship_id`, `chunk_id`, `evidence_id`, etc.

**Stable ID format (recommended):**

```
{entity_type_slug}:{sha256_prefix_of_canonical_key}
```

Example: `component:a3f2c1b9` where canonical key is `surface-laptop-5::battery`.

### 5.2 Name Normalization

Before hashing, normalize canonical keys:

1. Lowercase the label.
2. Strip leading/trailing whitespace.
3. Collapse internal whitespace to single space.
4. Remove possessives (`'s`).
5. Apply domain-specific abbreviation expansion (from config, if present).
6. Store the original label as an alias.

### 5.3 Alias Deduplication

- Merge aliases from multiple passes referencing the same canonical key.
- Remove duplicates (case-insensitive).
- Preserve original case in aliases array.

### 5.4 Confidence Thresholds

The canonicalize step applies the following thresholds to the intermediate JSON:

| Action | Threshold |
|---|---|
| Include in Parquet as active record | confidence >= 0.70 |
| Include in Parquet as flagged/low-confidence | 0.50 <= confidence < 0.70 |
| Drop from output silently | confidence < 0.50 |
| Write to `build/enriched/low-confidence.json` for review | 0.50 <= confidence < 0.70 |

> These thresholds are configurable via `fabric-kg.yaml`.

### 5.5 Schema Violation Handling

Any intermediate JSON object that fails validation against the JSON Schema (§4) must:

1. Be written to `build/enriched/violations.json` with the validation error.
2. Be excluded from Parquet output.
3. Trigger a warning (not a fatal error) unless `--strict` flag is set.

---

## 6. Prompt Architecture

### 6.1 Message Structure

All LLM calls use the **Microsoft Foundry SDK (Azure AI Foundry)** — see §9 for client setup and model configuration. The Foundry SDK is the required integration layer; do not use the raw OpenAI SDK directly.

```
[system / developer message]   — role definition, output contract, constraints
[user message — context]       — source content (rows, spans, image ref)
[user message — task]          — specific extraction instruction for this pass
```

> The `developer` role (or the Foundry SDK equivalent trusted instruction layer) is used when available to enforce output format constraints separately from role definition. Fall back to a single `system` message if the deployment does not support a separate developer-equivalent role.

### 6.2 System Message Template (all passes)

```
You are an expert knowledge extraction assistant for a document and data enrichment pipeline.

Your output must be valid JSON that conforms to the provided JSON Schema.
Do not include explanations, markdown fences, or any text outside the JSON object.
Do not invent blob URLs, canonical IDs, or file paths — echo back only what was provided.
Assign confidence scores between 0.0 and 1.0 to all extracted items.
If you are uncertain, assign a lower confidence rather than omitting the item.
Do not emit table_row or table_cell records — table structure and HTML are extracted by
Azure AI Document Intelligence Layout before the LLM pass. Your role on tables is
SEMANTICS ONLY: summarize, extract entities, and link evidence from the provided HTML.
```

### 6.3 Developer / Format Message Template

```
Respond with a JSON object matching this schema:
{response_schema_json}

Required top-level fields: source_file_id, pass, {pass_output_fields}
All other top-level array fields must be present as empty arrays [] if not used by this pass.
```

### 6.4 User Context Message

The runner injects source context as a user message. When a domain brief exists at `build/enriched/domain.json`, it is prepended as a delimited block (see §2.3 for security rationale — domain text is USER message only, never system prompt):

```
--- DOMAIN CONTEXT (user-provided, normalized) ---
Domain: {domain_brief}
Key entity types: {key_entity_types_comma_separated}
Constraints: {extraction_constraints_newline_separated}
--- END DOMAIN CONTEXT ---

Source file: {source_file_id}
Source type: {source_type}
Pass: {pass_name}

{context_block}
```

Where `{context_block}` is one of:

**CSV/XLSX:**
```
Column headers: {comma-separated headers}
Sample rows (up to 20):
{json-encoded rows array}
```

**Document span:**
```
Section: {section_path}
Page: {page_number}
Text:
{span_text}
```

**Table:**
```
Table ID: {table_id}
Page: {page_number}
Section: {section_path}
HTML:
{table_html}
```

**Image/figure (vision model):**
```
Image ID: {image_id_hint}
Asset type: {asset_type}
Blob URL: {blob_url}
Caption: {caption_or_null}
Alt text: {alt_text_or_null}
Section context: {section_path_or_null}
[image bytes or URL passed as content part]
```

> For vision passes, pass the image as a `content` array part with `type: image_url` or `type: image_bytes`. Never include secrets or SAS tokens in logged prompt skeletons.

### 6.5 User Task Message

```
Extract {pass_description} from the source context above.
Focus on: {focus_instructions_per_pass}
Use the entity id_hints from prior context if provided: {prior_entity_id_hints_or_none}
```

**Per-pass focus instructions:**

| Pass | Focus instructions |
|---|---|
| P1 | column or span semantics, ontology type mapping |
| P2 | named entities, their type, label, aliases, and description |
| P3 | relationships between the provided entity id_hints only |
| P4 | normalize labels to canonical form, expand abbreviations |
| P5 | identify the specific text spans, rows, cells, or callouts that support each entity or relationship |
| P6 | visual description, OCR text, callout labels, component candidates, region types |
| P7 | write a concise search-friendly summary (2-3 sentences) for each chunk or table |
| P8 | identify any concepts strongly implied by the data that have no extracted instance yet |

### 6.6 Structured Outputs (response_format)

Use the Foundry SDK's structured-output / JSON schema response mode. The intended contract (verify against current Foundry SDK for exact parameter names):

```python
# Verify exact API against current azure-ai-projects / azure-ai-inference SDK
response = client.complete(
    messages=messages,
    response_format={"type": "json_schema", "json_schema": { ... }},  # verify parameter name
    temperature=0.0,
    seed=42,
)
```

For deployments that do not support `json_schema` response format, fall back to `response_format={"type": "json_object"}` and validate the response manually against the JSON Schema (§4).

### 6.7 Determinism Settings

| Setting | Value | Notes |
|---|---|---|
| `temperature` | `0.0` | Deterministic output for extraction passes |
| `seed` | configured integer (e.g. `42`) | Request deterministic completions where supported |
| `max_tokens` | pass-specific limit | Prevent runaway responses |
| `top_p` | `1.0` | Consistent with temperature=0 |

> For P6 (visual description), `temperature` may be relaxed to `0.1` to allow more natural description prose while keeping structure tight.

### 6.8 Token Budget per Pass

| Pass | Approx input | Approx output | Max output tokens |
|---|---|---|---|
| P1 | 1–2K | small | 1000 |
| P2 | 2–8K | medium | 4000 |
| P3 | 2–8K + entity hints | medium | 4000 |
| P4 | 1–4K | small | 2000 |
| P5 | 2–8K + entity/rel hints | medium | 4000 |
| P6 | image + context | medium | 3000 |
| P7 | chunk text | small | 1000 |
| P8 | schema profile + entity list | small | 2000 |

---

## 7. Traditional Chunking Spec (PRD §10)

The chunking stage runs before LLM enrichment and produces chunks that are fed into P7 for summarization.

> **Note:** The `chunks.parquet` schema definition is owned by Fenster's data-model spec. This section defines chunking behavior and content only.

### 7.1 Chunk Types

```text
section_text       — text from a document section or heading block
procedure_step     — a single numbered or bulleted step in a procedure
table_html         — full HTML rendering of a structured table
table_row          — individual row from a structured table
figure_caption     — caption text associated with a figure or image
image_description  — LLM-generated description of an image or figure (P6 output)
ocr_text           — OCR-extracted text from an image region
warning            — warning block (⚠ WARNING or equivalent)
note               — informational note block
raw_page_text      — full raw text of a page (fallback)
```

### 7.2 Chunk Metadata Fields

Every chunk produced by the chunking stage must carry these fields before the LLM pass:

| Field | Required | Notes |
|---|---|---|
| `chunk_id` (id_hint at this stage) | Yes | Scoped slug; canonicalize step assigns stable ID |
| `source_file_id` | Yes | Injected by runner |
| `document_element_id` | Yes | Parent section/table/figure/image |
| `chunk_type` | Yes | From the chunk type list above |
| `content` | Yes | Text for retrieval |
| `content_html` | If table | HTML representation |
| `blob_url` | If visual | Runner-injected Blob URL only |
| `page_number` | When available | Integer |
| `section_path` | When available | Heading/TOC path string |
| `table_id` | If from table | |
| `figure_id` | If from figure | |
| `image_id` | If from image | |
| `embedding_text` | After P7 | Prepared for embedding (see §7.4) |
| `content_hash` | Yes | SHA256 of content for dedup |

### 7.3 Table Chunking (PRD §10.3)

> **⚠ DI Layout is the source of truth for tables.** Tables are extracted by **Azure AI Document Intelligence Layout** (`outputContentFormat=markdown`), not the LLM. The LLM must NOT emit `table_row` or `table_cell` records. Any such records emitted by the LLM are dropped by the `canonicalize` step.

**DI Layout table extraction pipeline:**

```
PDF/DOCX
  → Azure AI Document Intelligence Layout (outputContentFormat=markdown)
      → tables[]   — cells with row_index, col_index, kind=columnHeader, bounding_polygon
      → content    — whole-document Markdown; tables rendered as HTML <table> blocks
  → docintel_tables.extract_tables()
      → document_element  (element_type="table", content_html, page_number, section_path)
      → chunk             (chunk_type="table_html", content_html, embedding_text)
      → artifact          table_{n}.html  → Blob upload → blob_url
  → LLM P7 / P2 pass (operates over the DI-produced HTML — no structural emission)
      → embedding_text:   "{summary}\n\n{pipe-delimited plain text}"
      → entity linking, table summary
```

| Chunk form | Chunk type | Content | Source |
|---|---|---|---|
| Full table HTML | `table_html` | Complete `<table>` HTML | **DI Layout** (`docintel_tables.py`) |
| Table summary | `embedding_text` only | LLM P7 summary over the HTML | LLM (P7) |
| Structured cells | Parquet only | Written to `document_elements.parquet` | **DI Layout** |

The `table_row` chunk type remains in the schema for legacy compatibility but is **no longer produced** by the enrichment pipeline. The `canonicalize` step silently drops any LLM-emitted `table_row` chunks.

**Table chunk HTML example** (rendered by `docintel_tables.table_to_html()`):

```html
<table>
  <thead>
    <tr><th>Part</th><th>Part Number</th><th>Quantity</th></tr>
  </thead>
  <tbody>
    <tr><td>Battery</td><td>M1287099-003</td><td>1</td></tr>
  </tbody>
</table>
```

**MS Learn references:**
- [Azure AI Document Intelligence — Layout model (Markdown output)](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/layout): `outputContentFormat=markdown` returns tables as HTML `<table>` blocks; `tables[]` provides structured cells with `row_index`, `col_index`, `kind` (columnHeader), and `bounding_regions`.
- [Document Intelligence for RAG — Semantic chunking](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/retrieval-augmented-generation): Layout Markdown output is the recommended input for semantic chunking in RAG pipelines.

Evidence links from table chunks to entities are created in the evidence pass (P5).

### 7.4 `embedding_text` Preparation

`embedding_text` is the field used when computing vectors. It is prepared after P7 summarization.

**Rules:**
1. For `section_text`, `procedure_step`, `warning`, `note`: use `content` as-is.
2. For `table_html`, `table_row`: use LLM summary from P7 + raw cell text concatenated: `{summary}\n\n{plain_text_of_cells}`.
3. For `image_description`, `figure_caption`: use `content` + description from P6: `{caption}\n\n{description}`.
4. For `ocr_text`: use `content` as-is.
5. Maximum token length before truncation: 512 tokens (configurable via `fabric-kg.yaml`).
6. Strip HTML tags before embedding.

---

## 8. Image and Figure Understanding Spec (PRD §13)

### 8.1 Overview

Images and figures are first-class evidence (PRD §13). The extraction layer uses **two distinct tools** in sequence:

1. **Azure AI Document Intelligence (Layout/Read API)** — primary extractor for layout, OCR text, and bounding polygons. Runs before any LLM call.
2. **Microsoft Foundry SDK vision model (P6)** — handles semantic description, callout meaning, and label→entity linking. Consumes the Doc Intelligence output as context.

**Division of labor:**

| Concern | Tool | Output |
|---------|------|--------|
| OCR text extraction | Azure AI Document Intelligence | `text` fields in `visual_regions[]` |
| Bounding polygons / page geometry | Azure AI Document Intelligence | `polygon_json` in `visual_regions[]` |
| Callout coordinate extraction | Azure AI Document Intelligence | `polygon_json` + `region_type: callout` |
| Page layout structure | Azure AI Document Intelligence | `section_path`, `page_number` |
| Image/figure extraction from PDF/DOCX | Azure AI Document Intelligence | Raw image bytes per page/figure |
| Visual description (2–4 sentences) | Foundry vision model (P6) | `visual_assets[].description` |
| Callout semantic meaning | Foundry vision model (P6) | `visual_regions[].label` narrative |
| Label → entity id_hint linking | Foundry vision model (P6) | `visual_regions[].identified_entity_hint` |
| Component region candidates | Foundry vision model (P6) | `visual_regions[]` with `region_type: component_region` |
| Visual relationship extraction | Foundry vision model (P6) | `relationships[]` with visual relation types |

> **Infrastructure detail** (Azure AI Document Intelligence resource, endpoint, key) lives in `docs/infra` (Keyser's domain). Schema provenance for `visual_regions` is defined in SPEC-002 (Fenster). This spec defines prompt usage and the handoff contract between Doc Intelligence output and the Foundry vision model call.

The pipeline for each image/figure is:

```
Image bytes (from document)
  -> Azure AI Document Intelligence (Layout/Read)
      -> OCR text + bounding polygons
      -> page structure
  -> Upload to Blob Storage (runner)
      -> blob_url recorded
  -> Foundry vision model P6 call
      -> Doc Intelligence output injected as context
      -> blob_url injected (never minted by LLM)
      -> Returns: visual_assets[], visual_regions[], relationships[]
```

The LLM/vision model is responsible only for description and semantic analysis. It never mints Blob URLs and never performs geometric layout analysis.

### 8.2 Vision Model Tasks (P6)

| Task | Description | PRD §13 ref |
|---|---|---|
| Visual description | Describe the overall image in 2-4 sentences | §13.4 |
| OCR extraction | Extract all readable text from the image | §13.1, §13.4 |
| Callout identification | Identify callout labels, arrows, and their target regions | §13.1, §13.4 |
| Label detection | Detect component labels, part numbers, or legend entries | §13.4 |
| Region candidate extraction | Propose bounding regions for identified components or objects | §13.4 |
| Visual relationship linking | Link regions to entity id_hints where the entity was extracted in P2 | §13.5 |

### 8.3 Visual Relationship Types (PRD §13.5)

The LLM may propose these relationship types in `relationships[]` when the source or target is a visual asset or region:

```text
visually_depicts       — entity is depicted in this image
shown_in               — entity or step is shown in this figure
has_callout            — image has a callout region
callout_identifies     — callout region identifies an entity
located_in_region      — entity is located in a specific visual region
ocr_mentions           — OCR text in image mentions an entity
captioned_by           — image is captioned by a text element
image_evidences        — image provides evidence for a fact
stored_at              — visual asset is stored at a Blob URL (runner adds this; LLM echoes)
extracted_from         — visual region was extracted from a parent image
```

### 8.4 Blob URL Handling

```
Rule: The LLM must NEVER generate, invent, or modify Blob URLs.

The runner:
  1. Uploads the image/figure to Blob Storage.
  2. Records the resulting Blob URL in visual_assets.parquet (pre-enrichment).
  3. Injects the Blob URL into the P6 prompt context.

The LLM:
  1. Receives the Blob URL as part of prompt context.
  2. Echoes the Blob URL unchanged in visual_assets[].blob_url and evidence[].blob_url.
  3. Never constructs or modifies Blob URL strings.
```

### 8.5 Image Context Injected per Source Type

| Source type | What the runner injects | PRD §13 ref |
|---|---|---|
| PDF | embedded raster image bytes, page number, bounding region, nearby caption | §13.1 |
| DOCX | inline/floating image bytes, alt text, caption, heading context | §13.2 |
| HTML | `<img>` source/alt text, figure/figcaption, heading context | §13.3 |
| Standalone image | full image bytes, filename, no additional context | §13.4 |

### 8.6 Table Extraction via Document Intelligence Layout

Tables are first-class structured artifacts extracted before any LLM call. This section documents the DI-native table path and its integration with §7.3 chunking and SPEC-002 provenance.

**Division of labor — tables:**

| Concern | Tool | Output |
|---|---|---|
| Table structure extraction | Azure AI Document Intelligence Layout | `tables[]` — cells with `row_index`, `col_index`, `kind` (columnHeader), `bounding_regions` |
| Table HTML rendering | Azure AI Document Intelligence Layout | `content_html` in `document_elements` + `chunks` (via `docintel_tables.table_to_html()`) |
| Whole-document Markdown | Azure AI Document Intelligence Layout | `analyze_result.content` — tables as HTML `<table>` blocks; fed to semantic chunker for non-table content |
| Independent HTML artifact | Pipeline runner | `table_{n}.html` uploaded to Blob Storage; `blob_url` set on `document_element` |
| Table semantic summary | Foundry model P7 | `embedding_text` = `"{summary}\n\n{plain_cells}"` |
| Entity linking over table HTML | Foundry model P2/P5 | `related_entity_ids` — e.g. a PartNumber cell → `Part` entity |

**Pipeline (per document):**

```
Azure AI Document Intelligence Layout (outputContentFormat=markdown)
  → analyze_result.tables[]           structured cells per table
  → analyze_result.content            whole-document Markdown for semantic chunking

docintel_tables.extract_tables(analyze_result, source_file_id)
  → For each table[n]:
      document_element  element_type="table"
                        content_html = table_to_html(table)   [rendered <table>]
                        content      = plain tab-delimited text
                        page_number, section_path, sort_order
      chunk             chunk_type="table_html"
                        content_html = same HTML
                        embedding_text = pipe-delimited header+rows (pre-P7)
                        document_element_id = FK to above element
      artifact          html_artifacts["table_{n}.html"] = HTML string
                        → Blob upload → blob_url set on document_element + chunk

LLM passes (operate on DI HTML — no structural transcription)
  → P7: table summary injected into embedding_text
  → P2: entity extraction from HTML cells
  → P5: evidence links (entity → table chunk)
  LLM must NOT emit table_row / table_cell → canonicalize drops any that appear
```

**Validation (2026-06-24):** Real DI Layout run on a Surface PDF yielded **2 tables → 2 `table_html` chunks** (proven). Reference implementations: [`microsoft/Document-Knowledge-Mining-Solution-Accelerator`](https://github.com/microsoft/Document-Knowledge-Mining-Solution-Accelerator), [`Azure-Samples/document-intelligence-code-samples`](https://github.com/Azure-Samples/document-intelligence-code-samples).

**MS Learn references:**
- [Azure AI Document Intelligence — Layout model](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/layout): structured `tables[]` with cell `row_index`, `col_index`, `columnHeader`, `bounding_polygon`; `outputContentFormat=markdown` renders tables as HTML `<table>` blocks.
- [Document Intelligence for RAG — Semantic chunking](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/retrieval-augmented-generation): Markdown output recommended for semantic chunking in RAG pipelines.

---

## 9. Model Configuration

> **SDK decision:** All LLM and vision model calls use the **Microsoft Foundry SDK (Azure AI Foundry)**. The raw OpenAI SDK is not used. If exact Foundry API surface details differ from what is shown here, implement the intended contract and mark the code comment "verify against current Foundry SDK".

### 9.1 Foundry SDK Client Setup

The Foundry SDK client is initialized once per process using settings from `fabric-kg.yaml` (non-secret) and environment variables loaded from `.env` (secrets). Never hardcode keys or endpoints. Never put secrets in yaml files or `.squad/` files.

**Intended client initialization (verify against current azure-ai-projects / azure-ai-inference SDK):**

```python
import os
from azure.ai.projects import AIProjectClient          # verify package name
from azure.identity import DefaultAzureCredential

# Non-secret config from fabric-kg.yaml:
#   foundry.endpoint             — ${AZURE_AI_FOUNDRY_ENDPOINT} (env-interpolated)
#   foundry.project              — project name (non-secret identifier)
#   enrichment.chat_deployment   — deployment name (non-secret identifier)
#   enrichment.vision_deployment — deployment name (non-secret identifier)

# Secrets from .env (loaded via python-dotenv or equivalent):
#   AZURE_AI_FOUNDRY_API_KEY  — API key auth (alternative to managed identity)

endpoint = config["foundry"]["endpoint"]                       # from fabric-kg.yaml (interpolated from ${AZURE_AI_FOUNDRY_ENDPOINT})

# Auth option A — API key (from .env, never hardcoded)
api_key = os.environ["AZURE_AI_FOUNDRY_API_KEY"]              # set in .env
client = AIProjectClient(endpoint=endpoint, credential=api_key)  # verify constructor

# Auth option B — Managed identity / DefaultAzureCredential (preferred in Azure-hosted envs)
# client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())

# Usage (chat / enrichment model):
chat_deployment = config["enrichment"]["chat_deployment"]     # from fabric-kg.yaml
response = client.inference.get_chat_completions_client().complete(  # verify method path
    model=chat_deployment,
    messages=messages,
    temperature=0.0,
    seed=42,
)
```

**Auth options summary:**

| Option | When to use | How configured |
|--------|-------------|----------------|
| API key (`AZURE_AI_FOUNDRY_API_KEY`) | Local dev, CI without managed identity | Set in `.env`; never in yaml or committed files |
| `DefaultAzureCredential` | Azure-hosted environments, managed identity enabled | No secrets needed; uses ambient Azure identity |

> The recommended pattern is `DefaultAzureCredential` in hosted environments and API key (from `.env`) in local dev. Both are supported; neither requires secrets in config files.

### 9.2 Configuration: `fabric-kg.yaml` (non-secrets) and `.env` (secrets)

**SPEC-001 config split (Keyser):** Non-secret settings live in `fabric-kg.yaml`; secrets live in `.env` only. This section follows that split precisely.

**`fabric-kg.yaml` — non-secret Foundry settings:**

```yaml
foundry:
  # Endpoint is env-interpolated — literal value lives in .env only
  endpoint: ${AZURE_AI_FOUNDRY_ENDPOINT}
  project: "<project-name>"               # non-secret Foundry project name

enrichment:
  # --- chat / enrichment model ---
  # ⚠️ MINIMUM 200K TPM (capacity 200, GlobalStandard) required for the enrich stage.
  #    Lower capacity will cause throttling on high-volume corpus runs.
  #    See docs/REQUIREMENTS-001-cli-prerequisites.md §6 for Foundry provisioning.
  # Default: gpt-5-4-mini (gpt-5.4-mini) — deployed 2026-06-24, 200K TPM, GlobalStandard.
  # Fallback: "chat" (gpt-4.1) if gpt-5-4-mini is unavailable in your hub.
  # Note: "gpt-5.5-mini" does NOT exist in the Azure AI catalog — do not use that name.
  chat_deployment: "gpt-5-4-mini"

  # --- vision model ---
  # Default = chat deployment (multimodal: gpt-5-4-mini handles vision natively)
  # Alternative: "gpt-4o" (example-vision deployment) — set here to override
  vision_deployment: "gpt-5-4-mini"       # default matches chat_deployment; swap to "gpt-4o" if dedicated vision deployment preferred

  # --- embedding model ---
  # Primary default: text-embedding-3-large @ 1536 dimensions (deployment name "embedding" in dev)
  # Fallback: text-embedding-3-small @ 1536 dimensions
  # ⚠️  Changing embedding_dimensions requires full re-indexing of the AI Search index
  #     (the vector field is fixed at 1536 dims — see SPEC-001 / INFRA-001, Keyser).
  embedding_deployment: "embedding"       # deployment name in example-aiservices dev; update per env
  embedding_dimensions: 1536             # must match the AI Search vector field dimension

temperature: 0.0
seed: 42
max_retries: 3
retry_delay_seconds: 2

thresholds:
  min_confidence_include: 0.70
  min_confidence_flag: 0.50

chunking:
  max_embedding_tokens: 512
  table_max_rows_per_chunk: 50
```

> **Model defaults summary:**
>
> | Role | Default | Deployment name | Capacity |
> |------|---------|-----------------|----------|
> | Chat / enrichment | gpt-5.4-mini | `gpt-5-4-mini` | **200K TPM (GlobalStandard)** — minimum required |
> | Vision | Chat deployment (multimodal: gpt-5-4-mini) | `gpt-5-4-mini` | Same deployment |
> | Alternative vision | gpt-4o (example-vision) | `gpt-4o` | — |
> | Embedding (primary) | text-embedding-3-large @ 1536 dims | `embedding` | — |
> | Embedding (fallback) | text-embedding-3-small @ 1536 dims | — | — |
>
> **Note:** `gpt-5.5-mini` does not exist in the Azure AI catalog. gpt-5.4-mini (deployment `gpt-5-4-mini`) is the current default. Fallback deployment `chat` (gpt-4.1) is available if needed.
>
> Deployment names are non-secret configuration — they identify the Foundry deployment but do not grant access. Keys and endpoints that grant API access belong in `.env` only. The `embedding_dimensions: 1536` value is coupled to the AI Search index vector field; if it changes, the index must be rebuilt (coordinate with Keyser via SPEC-001/INFRA-001). See `docs/REQUIREMENTS-001-cli-prerequisites.md` §6 for Foundry provisioning prerequisites including the 200K TPM requirement.

**`.env` — secrets (names only; values never committed):**

```
# Azure AI Foundry (LLM, vision, and embedding calls)
AZURE_AI_FOUNDRY_API_KEY=<set in .env — never commit>
AZURE_AI_FOUNDRY_ENDPOINT=<set in .env — never commit>

# Azure AI Search (second-query grounding — see §12)
AZURE_SEARCH_ENDPOINT=<set in .env — never commit>
AZURE_SEARCH_KEY=<set in .env — never commit>

# Azure AI Document Intelligence
AZURE_DOCINTEL_ENDPOINT=<set in .env — never commit>
AZURE_DOCINTEL_KEY=<set in .env — never commit>
```

See `.env.example` for the full schema (safe — contains only placeholder values).

### 9.3 Auth Options

| Auth method | Config | Notes |
|-------------|--------|-------|
| API key | `AZURE_AI_FOUNDRY_API_KEY` in `.env` | Local dev; must be rotated regularly |
| `DefaultAzureCredential` | No secrets; uses ambient Azure identity | Preferred for hosted envs; uses managed identity, service principal, or `az login` token in order |
| Service Principal | `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID` in `.env` | CI/CD without managed identity |

`DefaultAzureCredential` is the recommended default for Azure-hosted environments. API key (from `.env`) is acceptable for local development.

### 9.4 Required Environment Variables

The following variable **names** are defined here. Values belong in `.env` only — never committed, never in yaml, never in `.squad/` files.

```
# Azure AI Foundry (LLM, vision, and embedding)
AZURE_AI_FOUNDRY_API_KEY          — Foundry project API key (if not using managed identity)
AZURE_AI_FOUNDRY_ENDPOINT         — Foundry project endpoint URL

# Azure AI Search (second-query grounding — see §12)
AZURE_SEARCH_ENDPOINT              — AI Search service endpoint
AZURE_SEARCH_KEY                   — AI Search admin or query key

# Azure AI Document Intelligence (layout/OCR — see §8)
AZURE_DOCINTEL_ENDPOINT            — Doc Intelligence resource endpoint
AZURE_DOCINTEL_KEY                 — Doc Intelligence API key

# Azure identity (if using DefaultAzureCredential with service principal)
AZURE_CLIENT_ID                    — Service principal app ID
AZURE_CLIENT_SECRET                — Service principal secret
AZURE_TENANT_ID                    — Azure AD tenant ID
```

See `.env.example` for format reference. All values are placeholders in that file; real values exist only in local `.env` or Azure Key Vault / CI secret stores.

---

## 10. Reliability

### 10.1 Schema Violation Handling

1. After every LLM call, validate the raw JSON response against the intermediate JSON Schema (§4).
2. If validation fails:
   - Log the error with the source file ID and pass name.
   - Write the invalid response to `build/enriched/violations.json`.
   - If `max_retries` > 0, retry the call (see §10.2).
   - If all retries fail, write the source item to `build/enriched/failed-items.json` and continue.
3. Never write invalid LLM output to the canonical pipeline.

### 10.2 Retry Policy

| Condition | Action |
|---|---|
| Schema validation failure | Retry up to `max_retries` times |
| HTTP 429 (rate limit) | Exponential backoff, up to `max_retries` |
| HTTP 500/503 (transient) | Retry up to `max_retries` times |
| HTTP 400 (bad request) | Log and skip — do not retry |
| JSON parse failure | Retry once; if still fails, skip and log |

Default: `max_retries = 3`, `retry_delay_seconds = 2` (doubles on each attempt).

### 10.3 Validation Gates

The enrichment stage must validate intermediate JSON output before writing to `build/enriched/`:

```
[ ] source_file_id matches the runner-injected value
[ ] All id_hint values within each array are unique
[ ] All relationship source_id_hint and target_id_hint reference a known entities[].id_hint
[ ] All visual_regions[].image_id_hint reference a known visual_assets[].id_hint
[ ] All evidence[].visual_region_id_hint reference a known visual_regions[].id_hint
[ ] blob_url fields in LLM output match the runner-injected values (no mutations)
[ ] confidence values are in [0.0, 1.0] range
```

Validation failures write to `build/enriched/violations.json` (see §10.1).

### 10.4 Checkpointing Large Extraction Jobs (PRD §26 Q11)

For large sources (many source files or many images), the enrich command supports checkpointing:

1. Each source file's enrichment result is written to `build/enriched/{source_file_id}.json` atomically after validation.
2. On re-run, if `build/enriched/{source_file_id}.json` already exists and is valid, that file is skipped.
3. A checkpoint manifest is maintained at `build/enriched/.checkpoint.json`:
   ```json
   {
     "started_at": "<ISO timestamp>",
     "completed_files": ["<source_file_id>"],
     "failed_files": ["<source_file_id>"],
     "in_progress_files": ["<source_file_id>"]
   }
   ```
4. Use `--force` flag on `fabric-kg enrich` to re-run all files, ignoring the checkpoint.

### 10.5 Cost Controls

| Control | Mechanism |
|---|---|
| Token budget per pass | `max_tokens` cap in API call (see §6.8) |
| Chunk batching | Batch small chunks into one call where pass allows; configurable batch size |
| Sample-only mode | `--sample N` flag: enrich only the first N source items |
| Dry run | `--dry-run` flag: validate prompts and schemas without calling the API |
| Cost estimation | `fabric-kg enrich --estimate-cost` reports estimated token usage before running |

---

## 11. CLI Integration: the `enrich` Command

> Coordinate with Keyser's CLI contract for the `fabric-kg enrich` command. Do not contradict any command-level decisions Keyser makes.

### 11.1 Command Signature

```
fabric-kg enrich
  --input          <path>           Source files or directory (required)
  --out            <path>           Output directory (default: build/enriched)
  --passes         <p1,p2,...>      Comma-separated pass list (default: all passes)
  --domain-prompt  "<text>"         Supply domain brief inline for this run (no persistence)
  --domain-file    <path>           Load domain brief from file; skips rephrase pass
  --sample         <N>              Enrich only the first N source items
  --dry-run                         Validate prompts and schemas, no API calls
  --estimate-cost                   Print estimated token count and exit
  --strict                          Treat schema violations as fatal errors
  --force                           Re-run all files, ignoring checkpoint
  --config         <path>           Path to fabric-kg.yaml (default: fabric-kg.yaml)
  --env            <dev|test|prod>  Environment (affects Blob URLs and endpoint selection)
```

> To persist a domain brief for repeated runs use: `fabric-kg set-domain --prompt "..."` (writes `build/enriched/domain.json`). The `--domain-prompt` / `--domain-file` flags on `enrich` override the persisted brief for a single run.

### 11.2 Inputs

| Input | Description |
|---|---|
| Source files | CSV, XLSX, PDF, DOCX, HTML, image files, or existing Parquet |
| `fabric-kg.yaml` | Model config, thresholds, chunking settings |
| Environment variables | API keys, Azure endpoints, deployment names (see §9.4) |
| `build/enriched/.checkpoint.json` | Existing checkpoint state (if resuming) |

### 11.3 Outputs (written to `build/enriched/`)

| File | Description |
|---|---|
| `domain.json` | Normalized domain brief from domain intake pass (§2) |
| `{source_file_id}.json` | Intermediate JSON for each source file |
| `schema-profile.json` | Merged schema profile from all P1 passes |
| `low-confidence.json` | Entities/relationships below include threshold but above flag threshold |
| `violations.json` | Schema violations from LLM output |
| `failed-items.json` | Source items that failed after all retries |
| `.checkpoint.json` | Checkpoint manifest |

### 11.4 `schema-profile.json`

The schema profile is written to `build/enriched/schema-profile.json` after the enrich run. It is used by downstream steps (`compile-data`, `compile-ontology`) to understand what types and mappings were inferred.

```json
{
  "generated_at": "<ISO timestamp>",
  "source_files": ["<source_file_id>"],
  "inferred_domain": "<string | null>",
  "column_mappings": [ ... ],
  "inferred_entity_types": ["<string>"],
  "inferred_relationship_types": ["<string>"]
}
```

### 11.5 Contract with compile-data (Fenster)

The enrich command hands off to Fenster's compile-data step:

| Handoff item | From enrich | To compile-data |
|---|---|---|
| Intermediate JSON files | `build/enriched/{source_file_id}.json` | Read by compile-data to write Parquet |
| Schema profile | `build/enriched/schema-profile.json` | Used for column mapping and type inference |
| Checkpoint manifest | `build/enriched/.checkpoint.json` | Allows compile-data to know which files are ready |

Fenster's data-model spec defines the Parquet schema. Enrich must not assume or dictate Parquet column order or types beyond what is stated in this spec's intermediate JSON contract.

---

## 12. AI Search Second-Query Grounding

> **Cited source:** [RESEARCH-001](../research/RESEARCH-001-kg-aisearch-grounding.md) — production best practices for combining the Fabric Ontology with Azure AI Search.
>
> **Hard boundary:** Structured data (entities, relationships, properties, canonical records) is always queried from the **Fabric Lakehouse** (Parquet tables via Lakehouse SQL / Fabric API) — **never** from AI Search. AI Search holds text/visual chunks only. See SPEC-002 (Fenster) for chunk schema and SPEC-003 (McManus) for ontology structure queried in Phase 1.

### 12.1 Overview: Two-Phase Retrieval

The grounded-answer flow implements the **graph-then-search** two-phase pattern (RESEARCH-001 §2, §8):

- **Phase 1 — Bounded GQL Traversal:** Run a graph query over the Fabric Ontology to identify the relevant entity set. Returns `entity_ids`, `canonical_keys`, `aliases`, and `graph_path`. Cap at ~10–20 top-ranked entities; scope by relationship type and hop limit.
- **Phase 2 — AI Search Hybrid Query:** Use Phase 1 output to build a filtered hybrid search request, retrieving text and visual chunks that reference those entities.

```
USER QUERY
   |
 AGENT (Azure AI Foundry)
   |  Phase 1: GQL traversal (Fabric Ontology / Lakehouse)
   v  -> entity_ids[]  canonical_keys[]  aliases[]  graph_path
 Fabric Lakehouse (Parquet)
   |  Phase 2: Construct AI Search hybrid query
   v  BM25 on aliases + vector on embedding_text
      + filter: search.in(entity_ids,...) + vectorFilterMode: preFilter
      + semantic reranker (k=50)
 Azure AI Search (text/visual chunks only)
   |  grounding chunks + captions + provenance
   v
 LLM (Foundry, chat_deployment) -> GROUNDED ANSWER + CITATIONS
```

### 12.2 Phase 1 — GQL Traversal Details

**Goal:** Identify the entities most relevant to the user query so Phase 2 can filter on their IDs.

**Constraints (RESEARCH-001 §7):**
- Cap results to top **~10–20** entities to avoid filter-size pressure on AI Search.
- Scope traversal by relationship type (e.g., `has_component`, `has_part`, `evidenced_by`) and a **hop cap** (e.g., max 3 hops).
- Return per entity: `entity_id`, `canonical_key`, `aliases[]`, `entity_type`, and a `graph_path` string serializing the traversal (e.g., `"PartNumber --[identifies]--> Part --[has_part]--> Component"`).

**Phase 1 output contract:**

```json
{
  "entity_ids": ["uuid-1", "uuid-2", "..."],
  "canonical_keys": ["component:battery-m1287099", "..."],
  "aliases": ["Battery", "M1287099-003", "..."],
  "graph_path": "PartNumber --[identifies]--> Part --[has_part]--> Component",
  "entity_count": 5
}
```

> **Fallback trigger:** If `entity_count` is 0, skip Phase 2 filter and run the fallback branch (§12.5).

### 12.3 Filter-vs-Query-Term Rule

How Phase 1 output maps to AI Search query parameters (RESEARCH-001 §2):

| Phase 1 output | AI Search usage | Rationale |
|----------------|-----------------|-----------|
| `entity_ids[]` | `search.in(entity_ids, '...', ',')` **filter** | Opaque IDs are not text-searchable; `search.in()` is sub-second for hundreds of values and avoids the `eq`/`or` performance cliff |
| `canonical_keys[]` | `search.in(canonical_key, '...', ',')` **filter** | Exact-match filterable field |
| `aliases[]` (top 3) | Appended to the `search` keyword param | Human-readable names benefit BM25 / keyword recall |
| Large set (>50 IDs) | `search.in()` filter only | Avoids 16 MB POST limit and `or`-clause limit |
| Partial / fuzzy names | Vector query on `chunk_vector` | Falls through to ANN; no filter needed |

### 12.4 Phase 2 — Concrete AI Search Request Body

Minimum quality bar: **hybrid (BM25 + vector) with semantic reranker** (RESEARCH-001 §3). Feed `k=50` to the reranker; use `captions` to shorten grounding context.

```jsonc
POST /indexes/fabric-kg-chunks/docs/search?api-version=2026-04-01
{
  "search": "<<user query>> <<alias_query>>",
  "vectorQueries": [
    {
      "kind": "text",
      "text": "<<user query>>",
      "fields": "chunk_vector",
      "k": 50
    }
  ],
  "filter": "search.in(entity_ids, 'uuid-1,uuid-2,uuid-3', ',')",
  "vectorFilterMode": "preFilter",
  "queryType": "semantic",
  "semanticConfiguration": "fabric-semantic-config",
  "scoringProfile": "entity-boost",
  "select": "chunk_id, chunk_text, source_path, blob_url, entity_ids, canonical_key, entity_aliases, graph_path, last_modified",
  "top": 5,
  "captions": "extractive",
  "answers": "extractive|count-1"
}
```

> `alias_query` = top 3 aliases from Phase 1, space-separated, appended to the `search` param to improve BM25 recall on human-readable names.

**Key parameter notes:**

| Parameter | Value | Why |
|-----------|-------|-----|
| `vectorFilterMode` | `preFilter` | Constrains HNSW traversal to graph-relevant docs before ANN; reduces false negatives (RESEARCH-001 §5) |
| `queryType` | `semantic` | L2 semantic reranking over the top 50 candidates |
| `k` | 50 | Feed enough candidates to the reranker |
| `captions` | `extractive` | Extracts the answering passage; reduces LLM prompt token usage |
| `answers` | `extractive|count-1` | Returns the single best direct-answer candidate |
| `select` | provenance fields | `graph_path`, `source_path`, `entity_ids` required for grounded citations |

### 12.5 Fallback Branch

When Phase 1 returns 0 entities (no graph match), run a **pure hybrid search** without entity filter (RESEARCH-001 §8 Step 4):

```jsonc
POST /indexes/fabric-kg-chunks/docs/search?api-version=2026-04-01
{
  "search": "<<user query>>",
  "vectorQueries": [
    { "kind": "text", "text": "<<user query>>", "fields": "chunk_vector", "k": 50 }
  ],
  "queryType": "semantic",
  "semanticConfiguration": "fabric-semantic-config",
  "select": "chunk_id, chunk_text, source_path, blob_url, entity_ids, canonical_key, entity_aliases, graph_path, last_modified",
  "top": 5,
  "captions": "extractive",
  "answers": "extractive|count-1"
}
```

> No `filter` or `vectorFilterMode` — falls back to full-index recall. Log the fallback event (`phase1_entity_count=0`) for observability.

### 12.6 Grounded-Answer Prompt Contract

All retrieved data is placed in the **user** message. The system prompt is fixed developer-controlled instructions. This follows the same prompt-injection security model as §2.3 — domain text and retrieved content are USER content only, never system prompt.

**Message structure:**

```
[system message — fixed, developer-controlled]
  You are a knowledge-grounded assistant. Answer only using the numbered
  SOURCES provided below. Cite each claim as [1], [2], etc. using the
  source number. If the answer cannot be derived from the provided sources,
  say "I don't have enough information to answer that." Do not invent facts.
  Do not infer relationships not stated in the sources.

[user message — grounding context and question]

  --- GRAPH CONTEXT ---
  Traversal path: {graph_path}
  Entities: {entity_label} ({entity_type}), ...
  Relationships:
    - {source_entity} --[{relation_type}]--> {target_entity}  (confidence: {conf})
    - ...
  --- END GRAPH CONTEXT ---

  --- SOURCES ---
  [1]
  source_path: {source_path}
  entity: {canonical_key}
  graph_path: {graph_path}
  caption: {extractive_caption_or_null}
  text: {chunk_text_truncated}

  [2]
  source_path: ...
  entity: ...
  graph_path: ...
  caption: ...
  text: ...

  --- END SOURCES ---

  --- USER QUESTION ---
  {user_question}
  --- END USER QUESTION ---
```

**Context assembly rules:**

| Item | Source | Placement |
|------|--------|-----------|
| Traversal path and entity nodes | Fabric Lakehouse (Phase 1) | `--- GRAPH CONTEXT ---` block |
| Relationship edges + confidence | Fabric Lakehouse (Phase 1) | `--- GRAPH CONTEXT ---` block |
| Text and visual chunks | AI Search (Phase 2) | Numbered `--- SOURCES ---` entries |
| Extractive captions | AI Search `captions` field | Inside each `[n]` source entry (prefer over raw `text` when non-null) |
| User question | User input | `--- USER QUESTION ---` (always last) |

> Prefer AI Search `captions` over raw `chunk_text` when the caption is non-null — captions extract the answering passage and cut prompt token usage.

### 12.7 Confidence and Anti-Hallucination Guardrails

Apply these checks before and after the LLM call (RESEARCH-001 §6):

| Signal | Threshold / Rule | Action |
|--------|------------------|--------|
| Reranker score (`@search.rerankerScore`) | Drop chunks with score < ~1.5 | Removes low-quality grounding context before prompt assembly |
| Dual-source corroboration | Assert a fact only if it appears in **both** a graph relationship (Phase 1) AND a text chunk (Phase 2) | Reduces overconfident claims from single-source evidence |
| Graph path length | Shorter path = higher entity relevance; order sources accordingly | Place shorter-path entities first in the SOURCES block |
| Qualifier wording | When only one source supports a claim, use "According to [1]..." rather than assertive form | Signals uncertainty to the user |
| Provenance in every answer | Each cited fact must be traceable to `source_path`, `entity`, and `graph_path` | Enables auditing and user drill-down |

Agentic Retrieval (§12.8) provides built-in citation tracking and an activity log for multi-turn flows.

### 12.8 Multi-Turn: Agentic Retrieval Option

For multi-turn agent scenarios, **Agentic Retrieval** (`api-version=2026-04-01`, GA 2026-04-01) can serve as Phase 2 (RESEARCH-001 §7):

- Creates a **knowledge base** over the AI Search index.
- Accepts **conversation history** and the current turn's user message.
- Decomposes the query into parallel subqueries, returns structured responses with citations and an activity log.
- Phase 1 (GQL traversal) still runs first; entity IDs from Phase 1 can be passed as knowledge base filters.

**Trade-offs:**

| | Agentic Retrieval | Standard hybrid query (§12.4) |
|--|---------------------|-------------------------------|
| Latency | Higher (LLM subquery decomposition) | Lower |
| Multi-turn accuracy | Higher (conversation-aware) | Lower (single-turn only) |
| Citation tracking | Built-in activity log | Manual (from `select` provenance fields) |
| Cost | Higher (additional LLM calls per turn) | Lower |
| Available since | `api-version=2026-04-01` (GA) | All tiers |

> Use standard hybrid (§12.4) for single-turn enrichment and grounding. Consider Agentic Retrieval for chat-style agents where conversation history materially affects retrieval quality. Reference: [get-started-agentic-retrieval](https://learn.microsoft.com/en-us/azure/search/search-get-started-agentic-retrieval).

### 12.9 Structured Data Boundary

> **Critical constraint (RESEARCH-001 §1):** AI Search holds text/visual chunks **only**. Structured data — entity properties, relationship facts, canonical records, part numbers — is always queried from the **Fabric Lakehouse** (Parquet tables). Never use AI Search as a source of truth for entity properties or relationship existence.

| Data type | Source | Query mechanism |
|-----------|--------|-----------------|
| Entity properties (`label`, `type`, `aliases`, `properties_json`) | Fabric Lakehouse — `entities.parquet` (SPEC-002) | Lakehouse SQL / Fabric API — Phase 1 |
| Relationship facts (`source_entity`, `relation`, `target_entity`, confidence) | Fabric Lakehouse — `relationships.parquet` (SPEC-002) | Lakehouse SQL / Fabric API — Phase 1 |
| Text chunks (`section_text`, `procedure_step`, `warning`, `note`) | Azure AI Search index | Hybrid query — Phase 2 |
| Visual chunks (`image_description`, `ocr_text`, `figure_caption`) | Azure AI Search index | Hybrid query — Phase 2 |
| Canonical part numbers, codes, identifiers | Fabric Lakehouse | Phase 1 only |

Schema for Lakehouse Parquet tables: SPEC-002 (Fenster). Ontology structure queried in Phase 1: SPEC-003 (McManus). AI Search index field definitions, vector field dimensions, scoring profiles, and semantic configs: SPEC-001/INFRA-001 (Keyser).

### 12.10 Token Budget and Truncation

| Item | Limit | Fallback |
|------|-------|---------|
| Graph context block | 1500 tokens | Truncate least-confident relationships first |
| Sources block (all chunks) | 3500 tokens | Rank by `@search.rerankerScore`; drop lowest-ranked |
| Per-source text (`chunk_text`) | 500 tokens | Prefer extractive `caption` when shorter |
| User question | No limit (typically small) | — |
| System message | ~150 tokens (fixed) | — |
| Output | 2000 tokens | Adjust via `grounding.max_output_tokens` in `fabric-kg.yaml` |

Total default context budget: **8000 tokens** (configurable via `fabric-kg.yaml` under `grounding.max_context_tokens`).

### 12.11 Linkage to Other Specs

| Spec | What it owns |
|------|-------------|
| SPEC-002 (Fenster) | `canonical_key`, `aliases`, chunk schema; `entity_ids` field on chunk documents |
| SPEC-003 (McManus) | Ontology structure queried in Phase 1 (GQL / Fabric Ontology bridge) |
| SPEC-001 / INFRA-001 (Keyser) | AI Search index schema, vector field dimensions (`embedding_dimensions: 1536`), scoring profiles, semantic configs, CLI command surface |

This spec defines Phase 1 output contract, Phase 2 request body, grounded-answer prompt contract, and confidence guardrails only. Do not redefine SPEC-002 schema, SPEC-003 ontology structure, or SPEC-001 CLI commands here.

### 12.12 Schema-2.0 Extraction Contract

L2 starts only from an intact succeeded L1 receipt, its complete
`SourceCorpusManifest`, approved `DomainContractV2` and
`DomainApprovalContext`, and exact domain, hierarchy, identity-policy, and
completeness hashes. The bounded L1 design sample remains prompt context only.
L2 rematerializes every eligible source unit from the complete corpus and
records one explicit disposition for every corpus entry, including excluded or
blocked inventory entries.

The prompt vocabulary is closed over approved entity types, relationship types,
predicates, properties, hierarchy, identity policy, completeness requirements,
N, and K. Unknown observations produce audit/discovery reasons and
`DOMAIN_REREVIEW_REQUESTED`; they never mutate those authorities. Stable entity
identity is derived from the approved hierarchy-root identity policy, independent
of classification. Relationship identity uses the approved predicate and stable
endpoint IDs. Classification is a separate immutable candidate version.

Model output contains proposed observations and untrusted SourceUnit-relative
anchors only. L2 may seal `ExtractionCandidateBatch@1.0.0`,
`RequiredMemberSetProposal@1.1.0`, proposed canonical references, mutually
exclusive retained/deduplicated accounting, and initial append-only
`None -> proposed` lifecycle events. Every input candidate receives exactly one
accounting disposition. Audit reason metrics are separate from accounting reason
codes.

L2 does **not** verify or mint `EvidenceSpan`, assert candidates, resolve
terminal endpoints or subtypes, create a `RequiredMemberManifest`, write serving
tables, or activate schema-2 product CLI behavior. Those trust-making checks are
reserved for L3 or later stages. A model-authored evidence ID is retained only
as untrusted proposal data.

The approved `max_relations_per_work_unit` is semantic policy. An over-budget
response is discarded and deterministically split at structural boundaries with
stable child IDs; candidates are never truncated. Successful leaf artifacts are
immutable and reusable by exact authority fingerprint. Corrupt or missing leaves
are rerun individually, while successful remote work is not repeated.

Structured fact-set proposals are domain-neutral and consume only sealed
`CompletenessRequirementV2`. L2 may propose observed aggregate, member,
membership, role, order, and cardinality references, but may not invent an
unspecified count, order, role, or adjacency rule. The C0 1.1 proposal carries
the exact nullable expected/minimum/maximum bounds, approved role IDs, and
ordered/unordered policy. Role and order remain null when absent; unordered
members canonicalize by stable member ID, and ordered members require observed
approved unique contiguous positions. L2 diagnostics may reference the complete
proposal but do not redefine its policy. L3 alone validates and seals those
facts as `RequiredMemberManifest@1.1.0`.

### 12.13 Schema-2.0 L3 Evidence-Validation Contract

L3 is an isolated, local-only successor to L2. It is not wired into the product
CLI, and it makes no LLM, Foundry, Document Intelligence, embedding, Search,
Fabric, or other remote call. Approved external references are consumed only
through their sealed local decision artifact and exact approved decision hash;
L3 never fetches or reinterprets external content, and every remote resource
counter stays zero.

**Entry.** `load_l3_inputs` requires a succeeded or skipped L2 aggregate
receipt, its intact output and input manifests, its bound resource metrics, the
exact L2 accepted-contract versions, every C0-owner extraction candidate batch
with its accounting dispositions, every L2 proposed-candidate partition, every
sequence-zero proposed lifecycle partition, the complete L2 SourceUnit manifest
and its persisted units, every `RequiredMemberSetProposal@1.1.0` with its merged
candidate batch and L2 view, the frozen `l1.source_corpus_manifest`, and the
approved `DomainContractV2` with exact domain, hierarchy, root identity-policy,
completeness-requirement, and approved external-reference-decision hashes.

A proposal's own hashes recompute over whatever policy it carries, so proposal
self-consistency is never accepted as authority. Every collection policy field is
re-bound at the entry gate to the sealed `CompletenessRequirementV2`: the
membership semantic relationship must equal the approved
`structured_fact_set.membership_relationship_type_id`, the ordering policy must
equal the approved `ordering_policy` (including the ordered carrier's
`member_order_encoding`), the expected/minimum/maximum cardinality must equal the
approved `cardinality` expectation exactly — including the case where the Domain
specifies a bound and the proposal omits it — and the required roles must equal
the approved `member_role_ids`. Any divergence fails closed with
`L3_COMPLETENESS_HASH_MISMATCH` before a single candidate is validated. The
bounded L1 design-sample manifest remains design context only and never proves
extraction coverage. Complete accounting is proved before any candidate is
validated. Entry fails closed with `L3_INPUT_RECEIPT_INVALID`,
`L3_INPUT_MANIFEST_INVALID`, `L3_ACCOUNTING_INCOMPLETE`,
`L3_SOURCE_UNIT_MISSING`, `L3_DOMAIN_HASH_MISMATCH`,
`L3_HIERARCHY_HASH_MISMATCH`, `L3_IDENTITY_POLICY_HASH_MISMATCH`,
`L3_COMPLETENESS_HASH_MISMATCH`,
`L3_EXTERNAL_REFERENCE_DECISION_HASH_MISMATCH`, `L3_APPROVED_CONCEPT_MISSING`,
`L3_HIERARCHY_INVALID`, `L3_CONTRACT_VERSION_UNSUPPORTED`,
`L3_EVIDENCE_PURPOSE_INVALID`, or
`L3_EVIDENCE_PURPOSE_VERSION_UNSUPPORTED`.

**Exact evidence.** Every proposed anchor is verified against the preloaded
SourceUnit partition index: Unicode code-point bounds inside the exact NFC text,
a nonempty quote equal to the exact substring, the exact SourceUnit text hash,
and the same immutable locator source coordinates. Only C0
`EvidenceSpanV1_1.mint_verified` mints the identity, always with
`purpose == "extraction_assertion"`, the approved extraction verifier name and
implementation version, and the approved purpose-policy version; the span is
then re-checked with `verify_against`. L1 `domain_design` evidence is never
accepted as extraction evidence, whether read as 1.0 or adapted to 1.1, and no
1.0 artifact can adapt to `extraction_assertion`. A
`model_authored_evidence_id` is ignored as identity, recorded as
`MODEL_EVIDENCE_ID_IGNORED`, and never becomes an evidence FK.

**Endpoints, direction, grounding, subtypes.** Local endpoint references resolve
case-insensitively only when each maps to exactly one retained entity candidate
in the same source lineage. Source and target occurrences must be grounded
inside the relationship evidence span; a unique exact label match is accepted,
repeated occurrences require one exact proposed occurrence anchor, and one span
cannot ground two distinct endpoints to the same occurrence. Endpoint
compatibility is computed only through the sealed ancestor closure with a
deterministic cycle-safe walk. Exact endpoint policy accepts only the declared
type; subtype policy records the complete child-to-allowed-parent path. A
reversed endpoint signature is rejected with `DIRECTION_MISMATCH` and is never
silently swapped.

Direction itself is a separate obligation, and it is **not** proven by endpoint
type compatibility. The frozen L2 proposed-candidate carrier folds the
model-proposed `direction` token into the relationship identity seed without
persisting the token, so a `source_to_target`, `reverse`, and `unknown` proposal
over the same endpoint pair are locally indistinguishable. L3 therefore fails
closed: a relationship candidate is never asserted as direction-proven, and a
candidate that nothing more precise already blocks transitions to `unsupported`
with `EVIDENCE_MODALITY_UNSUPPORTED` — an explicit validator-capability gap
rather than a claim of proof. A more precise blocking reason
(`DIRECTION_MISMATCH`, `ENDPOINT_UNRESOLVED`, `ENDPOINT_EVIDENCE_UNGROUNDED`,
`EVIDENCE_MISSING`, `UNKNOWN_RELATIONSHIP_TYPE`) keeps its own deterministic
state and is never overwritten. The forward-only rule that accepts a persisted
token is implemented and exhaustively tested as a pure rule so it activates
unchanged when a later carrier persists direction.

**Hierarchy, classification, identity.** One cycle-checked ancestor closure and
root-to-leaf inherited property/constraint set is compiled per sealed authority,
never per candidate. Abstract types can never be instantiated directly. A
uniquely most-specific concrete classification is retained as a versioned type
assertion; competing sibling classifications stay on the same stable ID and
transition to `unresolved` without duplicating the node. Entity identity seeds
carry no type classification, ancestor path, or hierarchy depth, and
relationship identity seeds carry no endpoint classification or ancestor path;
recomputation uses the sealed `domain.hierarchy` identity-input helpers and the
C0 `deterministic_contract_id` primitive only. Legacy `model.ids.make_entity_id`
is never called. Hierarchy depth is reported independently and is never compared
to or derived from K.

An entity may only assert when L3 reproduces its persisted stable ID exactly from
the sealed identity policy and the persisted witness. A witness that is not
persisted — no local reference, or a `business_key` policy whose normalized key
the carrier does not carry — is `unresolved` with `IDENTITY_WITNESS_UNAVAILABLE`.
A witness that fails to reproduce the persisted ID means the seed was
model-controlled rather than policy-derived, including the case where one
model-authored `stable_source_identity` collapses two distinct local references
onto one stable ID; that is `rejected` with `IDENTITY_POLICY_VIOLATION`. Every
non-reproducible witness kind is non-assertable, output reconciliation refuses
any asserted entity whose identity was not recomputed, and the identity index
records the witness kind and recomputation result per stable ID.

**Append-only lifecycle.** Exactly one current transition is appended to each
sequence-zero L2 proposed event, referencing the exact prior record, increasing
the sequence, and recording sorted reason codes, verified evidence IDs, resolved
endpoint IDs, inheritance paths, validator name/version, and transition hash.
Terminal records are never mutated. Reason codes map to one deterministic state
with the fixed precedence rejected > unsupported > discovery > unresolved >
asserted; `unsupported` is reserved for a real validator-capability gap and is
never a generic error fallback.

**Generic structured completeness.** Each `RequiredMemberSetProposal@1.1.0` is
validated entirely from full-corpus SourceUnits and locally minted spans against
its approved `CompletenessRequirementV2`: member resolution, allowed semantic
type and role, verified membership relationship and evidence, deterministic
member identity set, recomputed collection hash, source-evidenced cardinality
bounds only when the Domain contract specifies them, complete required-role
coverage, and ordinal policy conformance. Adjacency edges are required only when
the Domain contract separately approves an adjacency relationship; ordinal order
alone never invents an edge, and counts, roles, and order are never inferred or
padded.

A `source_evidence` cardinality claim is proven only by locally minted
`extraction_assertion` spans from this L3 run. Bounded L1 design-sample evidence
is design context, never extraction proof: a Domain requirement that cites a
design span for its count is not satisfied, and the collection stays `unresolved`
with `CARDINALITY_EVIDENCE_INVALID`. Design span IDs are passed to the rule as an
explicitly prohibited set, so a mistaken approval cannot silently satisfy a count.

Every proposal receives exactly one deterministic L3 outcome. A complete
collection is sealed through the C0 `RequiredMemberManifestV1_1` factory. An
incomplete collection is intentionally *not* sealed and instead remains an
audit-addressable `unresolved` `l3.required_member_outcome` that names its missing
obligations and is excluded from L4-complete membership. This is an explicit
frozen-contract constraint, not an equivalence: `c0.required_member_manifest@1.1.0`
can only carry a complete collection, so the unresolved outcome is the *only*
carrier available for an incomplete one and it is not interchangeable with a
manifest. Output reconciliation therefore proves the two carriers form a strict
partition of the proposal set — every proposal appears exactly once across the
sealed manifests and the unresolved outcomes, with no duplicate, no drop, no
proposal in both, and no manifest naming a foreign proposal.

**Outputs.** The L3 output `ArtifactManifest` references the unchanged C0-owner
candidate batches, dispositions, proposal partitions, and initial lifecycle
partitions, and adds the appended lifecycle partitions, all locally minted
`c0.evidence_span@1.1.0` partitions, versioned classification assertions with
ancestor paths, governed property-observation versions, deterministic identity,
current-state, and reason-code indexes, one required-member outcome per
proposal, and one `c0.required_member_manifest@1.1.0` per complete collection.
The manifest reconciles one current state per retained candidate, resolvable
evidence for every reference, grounded endpoints and approved IDs for every
assertion, recomputed identity for every asserted entity, unique appended
lifecycle record IDs, no duplicate stable node or edge ID, and no model-authored
ID inside the verified evidence set. Because only `CandidateLifecycleRecord` is
self-hashing, every reported current state is additionally re-derived from its own
reason codes and cross-checked against the appended transition it claims —
`to_state`, sorted reason codes, verified evidence IDs, resolved endpoint IDs, and
inheritance paths must all agree, and `classify_state` must independently return
the reported state. Any divergence fails closed with
`L3_LIFECYCLE_CHAIN_INVALID`. Because that appended transition is itself sealed
with an unkeyed hash any writer can recompute, agreement between a checkpoint and
its own records proves nothing either. Reconciliation therefore re-derives every
published leaf in full — the complete reason set and current state of every
candidate kind, plus its classifications, property observations, minted evidence,
appended transitions, and leaf fingerprint — from the sealed L2 proposal
partition, the verified `SourceUnit` artifacts, and the compiled Domain
authority, and requires byte-exact equality with what the leaf reports. A run
interrupted after checkpointing but before publication therefore cannot publish a
forged `asserted` relationship, property, entity, or classification: the rerun
either rejects the checkpoint or recomputes the correct outcome. Failures use
`L3_LIFECYCLE_CHAIN_INVALID`, `L3_EVIDENCE_ID_COLLISION`,
`L3_VALIDATION_RESULT_INCOMPLETE`, `L3_OUTPUT_MANIFEST_INVALID`, or
`L3_RESOURCE_BINDING_INVALID`. L3 emits neither `AuditProjection` nor
`SemanticServingProjection`; executing those contracts belongs to L4.

**Incremental behavior.** Validation leaves are keyed by candidate batch and
reused only when the batch, the complete canonical hash of every referenced
`SourceUnit` artifact, initial lifecycle transitions, sealed authority hashes,
validator identity, verifier identity, and scoped cross-leaf classification
context all match. The SourceUnit binding is the whole verified artifact, not its
ID and text hash: `unit_kind`, `offset_unit`, `ordinal`, locator, and identity all
bind, because a same-text unit that is reclassified into an unverifiable modality
changes every candidate outcome on it while leaving its ID and text hash
unchanged. Each leaf checkpoint is content-addressed by that leaf fingerprint and
carries its own canonical payload hash, which is recomputed before reuse; a
corrupt checkpoint fails that gate, a stale checkpoint is never reachable for
reuse because its fingerprint no longer addresses it, and a resealed checkpoint is
caught by output reconciliation rather than trusted. A corrupt or stale leaf
reruns on its own without repeating intact leaves. Reuse is a checkpoint decision
only and never a trust decision: reconciliation re-derives every leaf regardless
of whether it was reused or freshly computed, so nothing is published on the
authority of a cache.

Mutable per-run stage artifacts — the input and output manifests, receipt,
resource metrics, deterministic indexes, published per-leaf partitions,
required-member outcomes, and sealed manifests — are written under a run
directory scoped by the exact L3 input fingerprint. A changed validator, verifier,
source, authority, or candidate set therefore reruns into its own directory
instead of colliding with an earlier run, while the immutable content-addressed
collision rule is preserved unchanged inside every run directory so
nondeterminism still fails closed with `L3_OUTPUT_MANIFEST_INVALID`. Within one
fingerprint an intact replay reproduces the aggregate receipt, output manifest,
and every persisted artifact byte-identically, because the bound receipt and
resource metrics are themselves reused rather than re-measured. Recovering a run
whose resource metrics were lost re-measures them, so the semantic output manifest
still replays exactly while the receipt hash that binds those measurements
changes; no derived semantic content differs. No numeric latency, memory,
throughput, retry, token, byte, or cache threshold is selected.

**Documented constraint.** The frozen L2 proposed-candidate carrier persists
resolved endpoint IDs and stable semantic IDs but not the raw identity witness
(`identity_key`/`stable_source_identity`), the model-proposed relationship
`governed_context`/`direction` token, or property owner attribution and values.
L3 does not treat any of those as proven. It recomputes an entity ID whenever its
policy witness is derivable from persisted L2 fields, records the witness kind
and recomputation result per stable ID in the identity index, and enforces the
identity invariants that remain provable: the exact C0 minted-ID shape, exactly
one identity-root policy per stable ID, classification independence across
classification versions, and one constant predicate/endpoint triple per
relationship ID.

Where a required proof is simply absent, L3 fails closed rather than asserting a
weaker claim:

| Absent proof | L3 outcome | Reason code |
|---|---|---|
| Relationship `direction` token | `unsupported` (never asserted) | `EVIDENCE_MODALITY_UNSUPPORTED` |
| Property owner attribution and value | `unsupported` (never asserted) | `EVIDENCE_MODALITY_UNSUPPORTED` |
| Entity witness not persisted | `unresolved` | `IDENTITY_WITNESS_UNAVAILABLE` |
| Entity witness does not reproduce the ID | `rejected` | `IDENTITY_POLICY_VIOLATION` |

Consequently a relationship or property candidate can never reach `asserted`
from this carrier, and because membership is carried by a relationship candidate,
a structured collection cannot reach `complete` from it either. That is a stated
limitation of the frozen carrier, not a validation result: the deterministic
rules for direction, property owner/value validation, cardinality evidence,
identity recomputation, and adjacency are implemented and exhaustively tested as
pure rules in `enrichment/schema2_evidence.py`, and each activates unchanged the
moment a later carrier revision persists the missing field. No C0 or L2 contract
is edited to work around this, and no reason code, state, or manifest is
repurposed to make an unproven candidate look proven.

#### 12.13.1 PR #32 salvage parity for L3

This successor replaces the frozen PR #32 branch. The table records the exact
keep/change/drop decision this implementation applied. Every "keep" below means
the *rule* is preserved; where the frozen L2 carrier cannot supply the proof a
rule needs, the rule is kept and exercised as a pure rule while the stage fails
closed instead of asserting (see **Documented constraint** above).

| Frozen PR #32 file/change | L3 decision | Where it landed |
|---|---|---|
| `docs/specs/SPEC-004-llm-enrichment.md` | **Keep/change** exact evidence, endpoint grounding, subtype resolution, and lifecycle; rewrite for C0 inputs and exclude serving | §12.13 of this spec |
| `docs/specs/SPEC-005-validation-and-test-plan.md` | **Keep/change** the validation gate list; activate EXT-102/EXT-103 and add EXT-109–EXT-112 | SPEC-005 §15.4 |
| `cli/enrich_cmd.py` | **Drop**; no CLI or product activation | not present |
| `domain/guard.py` | **Drop**; L3 never enables schema-2 enrichment | not present |
| `domain/service.py` legacy brief adaptation | **Drop**; L3 consumes the sealed Domain 2.0 contract by hash | not present |
| `enrichment/orchestrator.py` | **Keep/change** local validation sequencing, leaf resume, and metrics only; no LLM or canonicalization | `enrichment/schema2_validation_stage.py`, with fingerprint-scoped run directories, content-addressed leaf checkpoints, and full leaf re-derivation before publication |
| `enrichment/output_schema.py` | **Drop** `runner_verified`, resolved IDs, states, and evidence IDs from model output; L3 owns them in C0 events and spans | `ProposedCandidateView` reads L2 proposals; trust-making fields are L3-minted |
| `enrichment/schema2_validation.py` | **Split/keep/change** grounding, deterministic reason order, endpoint/direction/subtype checks; replace `make_evidence_id`, the type-dependent entity ID seed, and canonical-row mutation with C0 evidence/identity primitives, stable entity IDs, predicate-stable relationship IDs, versioned classifications, and lifecycle events | `enrichment/schema2_evidence.py` plus `enrichment/schema2_validation_stage.py` |
| PR #32 direction check on a model-supplied `direction` field | **Keep the rule, change the trust boundary**; the field is not persisted by the frozen L2 carrier, so it is never read back as proof | `relationship_direction_reasons` keeps forward-only semantics; the stage fails closed to `unsupported` |
| PR #32 property owner/value validation | **Keep the rule, change the trust boundary**; owner attribution and value are not persisted, so inheritance and value conformance are not claimed | `validate_property_observation` plus `property_attribution_reasons`; the stage fails closed to `unsupported` |
| PR #32 count/evidence reuse across design and extraction | **Change**; L1 design-sample spans are design context and are explicitly prohibited as extraction count proof | `validate_required_member_proposal(prohibited_cardinality_evidence_ids=...)` |
| PR #32 collection policy read from the proposal | **Change**; a proposal's own hashes prove only self-consistency, so every policy field is re-bound to the sealed `CompletenessRequirementV2` at the entry gate | `_validate_required_member_policy_binding` |
| `model/schemas.py`, `model/arrow_schemas.py` | **Drop**; L4 owns canonical/Arrow integration | not present |
| `schema2_exact_relationships.json` | **Keep/change** as expected verified span/lifecycle expectations | expressed as deterministic assertions in `tests/unit/test_schema2_validation_stage.py` rather than a new fixture file |
| `test_init_domain_v2_cmd.py` | **Drop**; belongs to L1 | unchanged |
| `test_schema2_enrichment_validation.py` | **Salvage** exact span, missing evidence, model-ID distrust, quote/bounds/source mismatch, unknown/discovery, direction, unresolved endpoint, subtype, exact-only, endpoint grounding, casefold, source identity, and invariant cases | `tests/unit/test_schema2_evidence.py`, `tests/unit/test_schema2_validation_stage.py` |
| `test_schema2_enrichment_work_units.py` | **Move** the asserted-without-evidence success prohibition to L3; leave split/resume coverage in L2 | `append_current_transition` and `_reconcile` guards plus their tests |
| `graph.occurrence.EvidenceSpan` | **Drop** as a verification authority; it stays a proposal/occurrence shape | L3 validates anchors against C0 `SourceUnit` and mints only C0 `EvidenceSpan` |

---

## Appendix A: JSON Schema Reference

The full intermediate JSON Schema is maintained at `config/schemas/llm-intermediate.schema.json`. The schema file is the authoritative validation artifact used at runtime. The field descriptions in §4 are the human-readable specification that the schema file must implement.

> The schema file does not exist yet at time of this spec's writing. It must be created before the first enrich command implementation is merged.

---

## Appendix B: Open Questions from PRD §26

| Q# | Question | Impact on this spec |
|---|---|---|
| Q6 | What Foundry deployment for text enrichment? | **Closed (2026-06-24T15:41:07.842-07:00):** Default `chat_deployment: gpt-5-4-mini` = gpt-5.4-mini @ 200K TPM (GlobalStandard), deployed 2026-06-24 on `example-aiservices`. Fallback: `chat` (gpt-4.1). Note: `gpt-5.5-mini` does not exist. 200K TPM minimum required for high-volume enrich runs — see `docs/REQUIREMENTS-001-cli-prerequisites.md` §6. |
| Q7 | What Foundry deployment for vision model? | **Closed (2026-06-24T13:24:31.077-07:00):** Default `enrichment.vision_deployment` = chat deployment (multimodal: `gpt-5-4-mini`). Alternative: `gpt-4o` (example-vision deployment). Configured via `enrichment.vision_deployment` in `fabric-kg.yaml` (see §9.2). |
| Q11 | How to checkpoint large LLM extraction jobs? | Addressed in §10.4 — decision needed to finalize |

---

*End of SPEC-004*
