# SPEC-003: Ontology and Fabric Deployment Specification

**Status:** Draft  
**Date:** 2026-06-24T12:42:17.255-07:00  
**Revision:** 2026-06-24T15:41:07.842-07:00 — §9 deployment mechanism reframed: fabric-cicd is REQUIRED PRIMARY tool; Fabric REST API is fallback only; fabric-cicd added as CLI prerequisite (REQUIREMENTS-001); deploy-lakehouse/deploy-ontology/deploy-search all route through fabric-cicd; deploy-search corrected to in-MVP (not optional); kg_lakehouse name and IDs captured; FabricDeployer defaults to fabric-cicd.
**Author:** McManus (KG/Ontology Dev)  
**PRD references:** §9, §13.5, §16, §17, §18, §20, §21  

---

## 1. Scope and Purpose

This specification defines:

1. The modular ontology structure for the Fabric KG Builder.
2. The `ontology/model.yaml` format as the authoritative human-authored source.
3. The `ontology/ids.lock.json` deterministic ID strategy.
4. The inverse-relationship materialization policy.
5. The ontology compiler that converts those two files into Fabric-compatible definition parts.
6. The `blob_url` property requirement on visual ontology nodes.
7. How ontology entity and relationship types bind to Fenster's canonical Parquet tables.
8. The Fabric deployment workflow including environment configuration, REST polling, and sensitivity labels.
9. Placeholder generation rules.
10. Ontology-level validation gates.
11. The `deploy-lakehouse` command and its role in loading structured Parquet tables into the Fabric Lakehouse (OneLake) as the queryable canonical store.
12. The graph-to-search bridge: relationship types and node properties that make domain entities traversable to `DocumentChunk` / `SearchIndexRecord` nodes, enabling agent-driven two-phase retrieval (graph query → AI Search grounding).
13. Azure AI Document Intelligence and Microsoft Foundry SDK awareness: ontology nodes must expose `blob_url` and region provenance properties consistent with those extraction outputs.

### 1.1 Boundaries

This spec covers the **semantic layer** only. It does not redefine the canonical Parquet table schemas (those live in PRD §12 and SPEC-002). It does not cover Azure AI Search index definitions (see future SPEC-006). It does not cover the LLM extraction pipeline (see SPEC-004).

**Canonical data home:** Structured Parquet tables are deployed to the **Fabric Lakehouse (OneLake)** via the `deploy-lakehouse` command. The Lakehouse is the queryable canonical store — it is **not** Azure AI Search. AI Search (`deploy-search`) is a separate **in-MVP** retrieval layer for text and visual retrieval artifacts only; it does not receive raw structured Parquet tables.

### 1.2 PRD Alignment

| PRD Section | What it drives in this spec |
|---|---|
| §9 | Modular ontology structure, entity/relationship types, traversal example |
| §13.5 | Visual relationship types materialized as ontology relationship types |
| §16 | Fabric definition part directory structure and REST payload format |
| §17 | `ids.lock.json` format, ID ranges, no-regeneration rule |
| §18 | Placeholder ontology parts |
| §20 | Per-environment JSON files, what varies vs. what stays stable |
| §21 | Validation gates that must fail the build |

---

## 2. Ontology Strategy

### 2.1 One Ontology, Connected Modules

The system uses a **single ontology** named `FabricKG` composed of five connected modules. Analysts should be able to traverse from domain facts all the way to source documents, visual assets, and retrieval records in one hop chain — not by switching between isolated ontologies.

```text
FabricKG
  ├─ support-domain          domain entities — devices, parts, procedures
  ├─ document-evidence       document structure — pages, tables, figures
  ├─ visual-evidence         images and visual regions
  ├─ retrieval               chunks and search records
  └─ provenance              cross-cutting relationships
```

### 2.2 Entity Types per Module

#### support-domain

| Entity Type | Purpose |
|---|---|
| `Device` | A product or device model (e.g., Surface Laptop 5) |
| `Model` | A product model or SKU variant |
| `Component` | A named functional assembly within a device |
| `Part` | A physical replaceable part |
| `PartNumber` | An OEM or SKU part number (first-class entity) |
| `Tool` | A repair or assembly tool |
| `Symptom` | An observed failure or defect description |
| `Cause` | A root cause linked to a symptom |
| `Resolution` | A resolution step or fix |
| `Procedure` | A named repair or assembly procedure |
| `Step` | A single numbered step within a procedure |

#### document-evidence

| Entity Type | Purpose |
|---|---|
| `Document` | A source document (PDF, DOCX, HTML) |
| `DocumentChunk` | A traditional chunk from document chunking |
| `Section` | A heading/section within a document |
| `Page` | A page within a document |
| `Table` | A structured table extracted from a document |
| `TableRow` | A row within a table |
| `TableColumn` | A column definition within a table |
| `TableCell` | A single cell within a table |
| `Figure` | A figure extracted from a document (has `blob_url`) |
| `Image` | An inline image within a document (has `blob_url`) |
| `Caption` | A caption text element near a figure or image |
| `Callout` | A callout label in a diagram or figure |

#### visual-evidence

| Entity Type | Purpose |
|---|---|
| `ImageAsset` | A stored visual asset in Blob Storage (has `blob_url`) |
| `Diagram` | A diagram type of ImageAsset |
| `Screenshot` | A screenshot type of ImageAsset |
| `Photo` | A photo type of ImageAsset |
| `VisualRegion` | A bounding region within an image (has `blob_url`) |
| `OCRText` | OCR text extracted from an image or region |
| `BoundingBox` | Polygon or bounding box coordinates |
| `DetectedLabel` | A label detected in an image by a vision model |

#### retrieval

| Entity Type | Purpose |
|---|---|
| `Chunk` | A retrieval chunk with text content |
| `ChunkEmbedding` | An embedding vector associated with a chunk |
| `SearchDocument` | A document stored in an AI Search index |
| `SearchIndexRecord` | A record in a specific search index |

#### provenance

Provenance is expressed through relationship types (see §2.3). It does not add new entity types; instead it provides the cross-cutting edges that connect the modules.

### 2.3 Relationship Types per Module

#### support-domain relationships

| Relationship | Source | Target | Notes |
|---|---|---|---|
| `has_component` | Device | Component | A device has zero or more components |
| `has_part` | Component | Part | A component has zero or more parts |
| `has_part_number` | Part | PartNumber | A part has one canonical part number |
| `has_variant` | Device | Model | A device has model variants |
| `causes` | Cause | Symptom | A cause produces a symptom |
| `resolves` | Resolution | Symptom | A resolution addresses a symptom |
| `uses_tool` | Procedure | Tool | A procedure requires a tool |
| `has_step` | Procedure | Step | A procedure contains steps |
| `acts_on` | Step | Component | A step acts on a component or part |

#### document-evidence relationships

| Relationship | Source | Target | Notes |
|---|---|---|---|
| `contains_section` | Document | Section | |
| `contains_table` | Document | Table | |
| `contains_figure` | Document | Figure | |
| `has_row` | Table | TableRow | |
| `has_column` | Table | TableColumn | |
| `has_cell` | TableRow | TableCell | |
| `captioned_by` | Figure | Caption | |
| `has_callout` | Figure | Callout | |
| `evidenced_by` | (any domain entity) | TableCell / DocumentChunk | Links fact to source |

#### visual-evidence relationships

| Relationship | Source | Target | Notes |
|---|---|---|---|
| `shown_in` | (any domain entity) | Figure / ImageAsset | Entity appears in a visual |
| `stored_at` | ImageAsset / Figure / VisualRegion | (blob_url property) | Resolved via property, not edge |
| `extracted_from` | VisualRegion | ImageAsset | Region extracted from parent image |
| `callout_identifies` | Callout | (any domain entity) | Callout points to a domain entity |
| `located_in_region` | Callout | VisualRegion | Callout is located in a visual region |
| `visually_depicts` | ImageAsset | (any domain entity) | Image depicts an entity |
| `ocr_mentions` | OCRText | (any domain entity) | OCR text references an entity |
| `image_evidences` | ImageAsset | (any domain entity) | Image is evidence for a claim |

#### retrieval relationships

| Relationship | Source | Target | Notes |
|---|---|---|---|
| `indexed_as` | DocumentChunk / TableCell | SearchIndexRecord | Chunk has a search index entry — **bridge hop**: yields `search_record_id` + `search_index_name` for AI Search lookup |
| `embeds_as` | Chunk | ChunkEmbedding | Chunk has an embedding |
| `applies_to` | SearchDocument | (any domain entity) | Search document references an entity |

> **Graph-to-Search Bridge note:** `evidenced_by` (document-evidence module), `shown_in` (visual-evidence module), and `indexed_as` (retrieval module) together form the traversable bridge from domain entities to AI Search records. See §12 for the full bridge specification.

#### provenance relationships

| Relationship | Source | Target | Notes |
|---|---|---|---|
| `source_section` | (any entity) | Section | Entity is mentioned in a section |
| `extracted_from` | (any entity) | Document | Entity was extracted from a document |
| `identifies` | TableCell / PartNumber | Part / Component | A cell value identifies a domain entity |

### 2.4 Traversal Example (from PRD §9)

The following traversal illustrates how a technician question — "Where in the diagram is the battery connector?" — resolves end-to-end through the ontology:

```text
PartNumber "M1287099-003"
  <- identifies -
TableCell (row=5, col=2, table="parts-list")
  <- evidenced_by -
Part "Battery"
  <- has_part -
Component "Battery Assembly"
  <- acts_on -
Step "Step 4 – Disconnect battery"
  -> shown_in ->
Figure "Fig 12"
  -> has_callout ->
Callout "B – Battery connector"
  -> located_in_region ->
VisualRegion "fig12:region:B"
  -> extracted_from ->
ImageAsset "figure12.png"
  [.blob_url = "https://…/kg-assets/figure12.png"]
```

Every node in this path is a first-class ontology entity. The `blob_url` on `ImageAsset` means both the ontology graph and the AI Search index can resolve the same visual artifact.

---

## 3. `ontology/model.yaml` Specification

`ontology/model.yaml` is the **authoritative human-authored source** for the ontology. The compiler reads it and generates all Fabric definition parts. Developers edit this file; they do not edit generated JSON directly.

### 3.1 Top-Level Structure

```yaml
ontology:
  name: string                  # Display name in Fabric
  description: string
  version: string               # Semver, e.g. "1.0.0"
  modules:                      # List of module definitions
    - <module>
  entityTypes:                  # List of entity type definitions
    - <entityType>
  relationshipTypes:            # List of relationship type definitions
    - <relationshipType>
```

### 3.2 Module Schema

```yaml
- name: string          # e.g. "support-domain"
  description: string
  entityTypeNames:      # Names of entity types belonging to this module
    - string
  relationshipTypeNames:
    - string
```

### 3.3 Entity Type Schema

```yaml
- name: string                  # e.g. "Device"
  description: string
  module: string                # Parent module name
  properties:
    - name: string
      type: string              # string | int | double | boolean | timestamp | blob_url
      required: boolean
      description: string
  dataBinding:
    table: string               # Parquet table name (without .parquet extension)
    entityIdColumn: string      # Column that maps to entity_id
    displayNameColumn: string   # Column that maps to display_name
    typeFilterColumn: string    # Optional: column used to filter rows by entity type
    typeFilterValue: string     # Optional: value to match in typeFilterColumn
    additionalColumns:          # Extra property mappings
      - property: string        # Property name in model.yaml
        column: string          # Column name in Parquet table
```

**`blob_url` type:** When a property has `type: blob_url`, the compiler emits a Fabric property definition with format `uri` and marks it as the asset location. See §7 for the list of types that require it.

### 3.4 Relationship Type Schema

```yaml
- name: string                  # e.g. "has_component"
  description: string
  module: string
  sourceType: string            # Entity type name
  targetType: string            # Entity type name
  inversePolicy: none | materialize | alias
  inverseName: string           # Required when inversePolicy is materialize or alias
  evidenceLink: boolean         # true if this relationship carries an evidence_id
  dataBinding:
    table: string               # Parquet table name
    relationshipIdColumn: string
    sourceEntityIdColumn: string
    targetEntityIdColumn: string
    typeFilterColumn: string
    typeFilterValue: string
    evidenceIdColumn: string    # Required when evidenceLink: true
    additionalColumns:
      - property: string
        column: string
```

### 3.5 Concrete `model.yaml` Snippet

The snippet below covers the required entity and relationship types. A real file includes all modules.

```yaml
ontology:
  name: "FabricKG"
  description: "Fabric Knowledge Graph for support and service guide domain"
  version: "1.0.0"

  modules:
    - name: support-domain
      description: "Domain entities for device support"
      entityTypeNames:
        - Device
        - Component
        - Part
        - PartNumber
      relationshipTypeNames:
        - has_component
        - has_part

    - name: document-evidence
      description: "Document structure evidence"
      entityTypeNames:
        - Figure
      relationshipTypeNames:
        - evidenced_by

    - name: visual-evidence
      description: "Images and visual regions"
      entityTypeNames:
        - ImageAsset
        - VisualRegion
      relationshipTypeNames:
        - shown_in
        - stored_at

  entityTypes:

    - name: Device
      description: "A product or device model"
      module: support-domain
      properties:
        - name: display_name
          type: string
          required: true
          description: "Human-readable device name"
        - name: canonical_key
          type: string
          required: true
          description: "Normalized identity key"
        - name: description
          type: string
          required: false
          description: "Device description"
      dataBinding:
        table: entities
        entityIdColumn: entity_id
        displayNameColumn: display_name
        typeFilterColumn: entity_type
        typeFilterValue: Device
        additionalColumns:
          - property: canonical_key
            column: canonical_key
          - property: description
            column: description

    - name: Component
      description: "A named functional assembly within a device"
      module: support-domain
      properties:
        - name: display_name
          type: string
          required: true
          description: "Component name"
        - name: canonical_key
          type: string
          required: true
          description: "Normalized identity key"
      dataBinding:
        table: entities
        entityIdColumn: entity_id
        displayNameColumn: display_name
        typeFilterColumn: entity_type
        typeFilterValue: Component
        additionalColumns:
          - property: canonical_key
            column: canonical_key

    - name: Part
      description: "A physical replaceable part"
      module: support-domain
      properties:
        - name: display_name
          type: string
          required: true
          description: "Part name"
        - name: canonical_key
          type: string
          required: true
          description: "Normalized identity key"
        - name: description
          type: string
          required: false
          description: "Part description"
      dataBinding:
        table: entities
        entityIdColumn: entity_id
        displayNameColumn: display_name
        typeFilterColumn: entity_type
        typeFilterValue: Part
        additionalColumns:
          - property: canonical_key
            column: canonical_key
          - property: description
            column: description

    - name: PartNumber
      description: "An OEM or SKU part number — first-class entity"
      module: support-domain
      properties:
        - name: display_name
          type: string
          required: true
          description: "The part number string"
        - name: canonical_key
          type: string
          required: true
          description: "Normalized part number key"
      dataBinding:
        table: entities
        entityIdColumn: entity_id
        displayNameColumn: display_name
        typeFilterColumn: entity_type
        typeFilterValue: PartNumber
        additionalColumns:
          - property: canonical_key
            column: canonical_key

    - name: ImageAsset
      description: "A stored visual asset in Blob Storage"
      module: visual-evidence
      properties:
        - name: display_name
          type: string
          required: true
          description: "Asset display name"
        - name: blob_url
          type: blob_url
          required: true
          description: "Blob Storage URL for the image asset"
        - name: asset_type
          type: string
          required: false
          description: "figure | inline_image | screenshot | diagram | photo | chart | table_image"
        - name: description
          type: string
          required: false
          description: "LLM-generated visual description"
        - name: caption
          type: string
          required: false
          description: "Caption or nearby title"
      dataBinding:
        table: visual_assets
        entityIdColumn: image_id
        displayNameColumn: caption
        typeFilterColumn: asset_type
        typeFilterValue: ""             # all rows; filter by asset_type if needed
        additionalColumns:
          - property: blob_url
            column: blob_url
          - property: asset_type
            column: asset_type
          - property: description
            column: description
          - property: caption
            column: caption

    - name: Figure
      description: "A figure extracted from a document"
      module: document-evidence
      properties:
        - name: display_name
          type: string
          required: true
          description: "Figure display name or caption"
        - name: blob_url
          type: blob_url
          required: true
          description: "Blob Storage URL for the figure image"
        - name: caption
          type: string
          required: false
          description: "Figure caption text"
        - name: page_number
          type: int
          required: false
          description: "Page the figure appears on"
      dataBinding:
        table: document_elements
        entityIdColumn: document_element_id
        displayNameColumn: title
        typeFilterColumn: element_type
        typeFilterValue: figure
        additionalColumns:
          - property: blob_url
            column: blob_url
          - property: caption
            column: content
          - property: page_number
            column: page_number

    - name: VisualRegion
      description: "A bounding region within an image"
      module: visual-evidence
      properties:
        - name: display_name
          type: string
          required: true
          description: "Region label"
        - name: blob_url
          type: blob_url
          required: true
          description: "Blob URL of parent or cropped image"
        - name: region_type
          type: string
          required: false
          description: "callout | ocr_text | component_region | connector_region"
        - name: label
          type: string
          required: false
          description: "Callout label or detected label text"
        - name: polygon_json
          type: string
          required: false
          description: "Region polygon or bounding box as JSON string"
      dataBinding:
        table: visual_regions
        entityIdColumn: visual_region_id
        displayNameColumn: label
        additionalColumns:
          - property: blob_url
            column: blob_url
          - property: region_type
            column: region_type
          - property: label
            column: label
          - property: polygon_json
            column: polygon_json

  relationshipTypes:

    - name: has_component
      description: "A device has a component"
      module: support-domain
      sourceType: Device
      targetType: Component
      inversePolicy: materialize
      inverseName: component_of
      evidenceLink: false
      dataBinding:
        table: relationships
        relationshipIdColumn: relationship_id
        sourceEntityIdColumn: source_entity_id
        targetEntityIdColumn: target_entity_id
        typeFilterColumn: relationship_type
        typeFilterValue: has_component

    - name: has_part
      description: "A component has a part"
      module: support-domain
      sourceType: Component
      targetType: Part
      inversePolicy: materialize
      inverseName: part_of
      evidenceLink: false
      dataBinding:
        table: relationships
        relationshipIdColumn: relationship_id
        sourceEntityIdColumn: source_entity_id
        targetEntityIdColumn: target_entity_id
        typeFilterColumn: relationship_type
        typeFilterValue: has_part

    - name: evidenced_by
      description: "A domain entity is evidenced by a document element"
      module: document-evidence
      sourceType: Part
      targetType: TableCell
      inversePolicy: none
      evidenceLink: true
      dataBinding:
        table: relationships
        relationshipIdColumn: relationship_id
        sourceEntityIdColumn: source_entity_id
        targetEntityIdColumn: target_entity_id
        typeFilterColumn: relationship_type
        typeFilterValue: evidenced_by
        evidenceIdColumn: evidence_id

    - name: shown_in
      description: "A domain entity is shown in an image or figure"
      module: visual-evidence
      sourceType: Step
      targetType: Figure
      inversePolicy: alias
      inverseName: shows
      evidenceLink: false
      dataBinding:
        table: relationships
        relationshipIdColumn: relationship_id
        sourceEntityIdColumn: source_entity_id
        targetEntityIdColumn: target_entity_id
        typeFilterColumn: relationship_type
        typeFilterValue: shown_in

    - name: stored_at
      description: "An image asset is stored at a Blob URL"
      module: visual-evidence
      sourceType: ImageAsset
      targetType: ImageAsset
      inversePolicy: none
      evidenceLink: false
      # stored_at is expressed as a blob_url property on the node, not as a separate edge.
      # This entry exists so the compiler emits a property reference, not a relationship type.
      dataBinding:
        table: visual_assets
        relationshipIdColumn: image_id
        sourceEntityIdColumn: image_id
        targetEntityIdColumn: image_id
        typeFilterColumn: asset_type
        typeFilterValue: ""
```

> **Note:** The `stored_at` relationship from PRD §9 is implemented as a `blob_url` property on visual nodes rather than a separate edge. See §7 for the rationale.

---

## 4. Deterministic Type ID Strategy

### 4.1 `ontology/ids.lock.json`

This file is **the only source of truth for numeric type IDs**. It is committed to source control and never regenerated. The compiler reads it before emitting any Fabric definition parts.

```json
{
  "entityTypes": {
    "SupportEntity":   "1000000000000000001",
    "Device":          "1000000000000000002",
    "Component":       "1000000000000000003",
    "Part":            "1000000000000000004",
    "PartNumber":      "1000000000000000005",
    "Model":           "1000000000000000006",
    "Tool":            "1000000000000000007",
    "Symptom":         "1000000000000000008",
    "Cause":           "1000000000000000009",
    "Resolution":      "1000000000000000010",
    "Procedure":       "1000000000000000011",
    "Step":            "1000000000000000012",
    "DocumentElement": "1000000000000000100",
    "DocumentChunk":   "1000000000000000101",
    "TableCell":       "1000000000000000102",
    "Figure":          "1000000000000000103",
    "ImageAsset":      "1000000000000000104",
    "VisualRegion":    "1000000000000000105",
    "Callout":         "1000000000000000106",
    "Document":        "1000000000000000107",
    "Section":         "1000000000000000108",
    "Page":            "1000000000000000109",
    "Table":           "1000000000000000110",
    "TableRow":        "1000000000000000111",
    "TableColumn":     "1000000000000000112",
    "Caption":         "1000000000000000113",
    "Image":           "1000000000000000114",
    "Diagram":         "1000000000000000115",
    "Screenshot":      "1000000000000000116",
    "Photo":           "1000000000000000117",
    "OCRText":         "1000000000000000118",
    "BoundingBox":     "1000000000000000119",
    "DetectedLabel":   "1000000000000000120",
    "Chunk":           "1000000000000000200",
    "ChunkEmbedding":  "1000000000000000201",
    "SearchDocument":  "1000000000000000202",
    "SearchIndexRecord": "1000000000000000203"
  },
  "relationshipTypes": {
    "has_component":     "2000000000000000001",
    "has_part":          "2000000000000000002",
    "has_part_number":   "2000000000000000003",
    "has_variant":       "2000000000000000004",
    "causes":            "2000000000000000005",
    "resolves":          "2000000000000000006",
    "uses_tool":         "2000000000000000007",
    "has_step":          "2000000000000000008",
    "acts_on":           "2000000000000000009",
    "evidenced_by":      "2000000000000000100",
    "shown_in":          "2000000000000000101",
    "has_callout":       "2000000000000000102",
    "callout_identifies":"2000000000000000103",
    "visually_depicts":  "2000000000000000104",
    "indexed_as":        "2000000000000000105",
    "stored_at":         "2000000000000000106",
    "extracted_from":    "2000000000000000107",
    "located_in_region": "2000000000000000108",
    "image_evidences":   "2000000000000000109",
    "ocr_mentions":      "2000000000000000110",
    "contains_section":  "2000000000000000111",
    "contains_table":    "2000000000000000112",
    "contains_figure":   "2000000000000000113",
    "has_row":           "2000000000000000114",
    "has_column":        "2000000000000000115",
    "has_cell":          "2000000000000000116",
    "captioned_by":      "2000000000000000117",
    "identifies":        "2000000000000000118",
    "source_section":    "2000000000000000119",
    "embeds_as":         "2000000000000000120",
    "applies_to":        "2000000000000000121",
    "component_of":      "2000000000000000200",
    "part_of":           "2000000000000000201",
    "shows":             "2000000000000000202"
  },
  "properties": {}
}
```

### 4.2 ID Range Conventions

| Range | Type |
|---|---|
| `1000000000000000001` – `1000000000000000099` | Core support-domain entity types |
| `1000000000000000100` – `1000000000000000199` | Document-evidence entity types |
| `1000000000000000200` – `1000000000000000299` | Retrieval entity types |
| `2000000000000000001` – `2000000000000000099` | Core support-domain relationship types |
| `2000000000000000100` – `2000000000000000199` | Evidence and visual relationship types |
| `2000000000000000200` – `2000000000000000299` | Materialized inverse relationship types |

### 4.3 Rules

1. **Never regenerate IDs across environments.** Dev, test, and prod all use the same `ids.lock.json`.
2. **New types get the next available ID in their range.** Do not recycle IDs from deleted types.
3. **Deleted types are tombstoned, not removed.** Comment them out with `// DELETED` so the ID is never reused.
4. **The compiler must fail if a type in `model.yaml` has no matching entry in `ids.lock.json`.**
5. **The compiler must fail if two types share the same ID.**

---

## 5. Inverse-Relationship Policy

Direction matters. An edge from A to B is not the same as an edge from B to A. The policy for each relationship type specifies whether the inverse is materialized as a separate row in `relationships.parquet` and a separate entry in `ids.lock.json`.

| Policy | Meaning |
|---|---|
| `none` | Inverse is not stored. Traversal goes one direction only. |
| `materialize` | Inverse is stored as a separate relationship type with its own ID. Both directions are queryable. |
| `alias` | Inverse shares the same underlying edge data but has a separate named type ID for display. Fabric can use a contextualization to show both names. |

### 5.1 Materialized Inverses

The following inverses are materialized (policy = `materialize`):

| Forward | Inverse | Reason |
|---|---|---|
| `has_component` | `component_of` | Analysts navigate from component to device |
| `has_part` | `part_of` | Analysts navigate from part to component |
| `has_step` | `step_of` | Analysts navigate from step to procedure |
| `evidenced_by` | Materialized only if analytics queries require it | Avoids join overhead |

### 5.2 Alias Inverses

The following inverses are defined as aliases (policy = `alias`):

| Forward | Alias | Reason |
|---|---|---|
| `shown_in` | `shows` | Figure shows entity; traversal in both directions useful in UX |
| `callout_identifies` | `identified_by` | Entity is identified by callout |

### 5.3 No-Inverse Relationships

Relationships with `inversePolicy: none`:

- `evidenced_by` (provenance is directional — evidence does not "have" entities)
- `stored_at` (expressed as a property, not as a separate edge)
- `indexed_as` (canonical direction is entity → search record)
- `extracted_from` (source-of-truth direction is region → asset)

> **Lesson carried forward from PRD §22:** The original AI Search prototype stored relationships only as retrieval rows and lost directionality. Explicit inverse policy prevents this regression.

---

## 6. Ontology Compiler Specification

### 6.1 Inputs

| Input | Path | Purpose |
|---|---|---|
| Model | `ontology/model.yaml` | Entity types, relationship types, properties, data bindings |
| ID lock | `ontology/ids.lock.json` | Stable numeric IDs |
| Environment config | `ontology/environments/{env}.json` | Workspace ID, Lakehouse ID, display-name suffix |

### 6.2 Output Directory Structure

The compiler writes to `build/ontology/` by default (overridable with `--out`):

```text
build/ontology/
  .platform
  definition.json
  EntityTypes/
    {ID}/
      definition.json
      DataBindings/
        {GUID}.json
  RelationshipTypes/
    {ID}/
      definition.json
      Contextualizations/
        {GUID}.json
```

Where `{ID}` is the numeric string from `ids.lock.json` (e.g., `1000000000000000002`) and `{GUID}` is a deterministic UUID v5 derived from `namespace(ontology-name) + entity-type-name + binding-table`.

### 6.3 `.platform` File

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
  "metadata": {
    "type": "Ontology",
    "displayName": "FabricKG"
  },
  "config": {
    "version": "2.0",
    "logicalId": "<deterministic-guid>"
  }
}
```

### 6.4 `definition.json` (Top-Level)

```json
{
  "parts": [
    {
      "path": ".platform",
      "payload": "<InlineBase64>",
      "payloadType": "InlineBase64"
    },
    {
      "path": "EntityTypes/1000000000000000002/definition.json",
      "payload": "<InlineBase64>",
      "payloadType": "InlineBase64"
    },
    {
      "path": "EntityTypes/1000000000000000002/DataBindings/3fa85f64-5717-4562-b3fc-2c963f66afa6.json",
      "payload": "<InlineBase64>",
      "payloadType": "InlineBase64"
    }
  ]
}
```

Every file under `build/ontology/` appears as an entry in `parts`. The `payload` value is the Base64-encoded UTF-8 content of the target file.

### 6.5 Entity Type `definition.json`

```json
{
  "$schema": "...",
  "typeId": "1000000000000000002",
  "name": "Device",
  "description": "A product or device model",
  "properties": [
    {
      "name": "display_name",
      "type": "String",
      "isRequired": true
    },
    {
      "name": "canonical_key",
      "type": "String",
      "isRequired": true
    },
    {
      "name": "blob_url",
      "type": "String",
      "format": "uri",
      "isRequired": true
    }
  ]
}
```

### 6.6 Data Binding `{GUID}.json`

```json
{
  "$schema": "...",
  "bindingId": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "displayName": "Device from entities",
  "dataSourceType": "Lakehouse",
  "lakehouseId": "<from env config>",
  "tableName": "entities",
  "entityIdColumn": "entity_id",
  "displayNameColumn": "display_name",
  "typeFilterColumn": "entity_type",
  "typeFilterValue": "Device",
  "propertyMappings": [
    { "propertyName": "canonical_key", "columnName": "canonical_key" },
    { "propertyName": "description",   "columnName": "description" }
  ]
}
```

### 6.7 Relationship Type `definition.json`

```json
{
  "$schema": "...",
  "typeId": "2000000000000000001",
  "name": "has_component",
  "description": "A device has a component",
  "sourceTypeId": "1000000000000000002",
  "targetTypeId": "1000000000000000003",
  "inverseTypeId": "2000000000000000200"
}
```

`inverseTypeId` is omitted when `inversePolicy: none`.

### 6.8 Relationship Contextualization `{GUID}.json`

```json
{
  "$schema": "...",
  "contextualizationId": "<deterministic-guid>",
  "displayName": "has_component from relationships",
  "dataSourceType": "Lakehouse",
  "lakehouseId": "<from env config>",
  "tableName": "relationships",
  "relationshipIdColumn": "relationship_id",
  "sourceEntityIdColumn": "source_entity_id",
  "targetEntityIdColumn": "target_entity_id",
  "typeFilterColumn": "relationship_type",
  "typeFilterValue": "has_component"
}
```

### 6.9 Compiler Steps

```text
1.  Read model.yaml → parse entity types, relationship types, modules
2.  Read ids.lock.json → build name→ID map
3.  Validate: every type in model.yaml has an ID; no duplicate IDs
4.  Validate: every relationship type references known entity type names
5.  Validate: every entity type with blob_url property has type: blob_url
6.  Read environment config → inject workspace ID, Lakehouse ID
7.  For each entity type:
      a. Emit EntityTypes/{ID}/definition.json
      b. Derive binding GUID (UUIDv5)
      c. Emit EntityTypes/{ID}/DataBindings/{GUID}.json
8.  For each relationship type:
      a. Emit RelationshipTypes/{ID}/definition.json
      b. Derive contextualization GUID (UUIDv5)
      c. Emit RelationshipTypes/{ID}/Contextualizations/{GUID}.json
      d. If inversePolicy=materialize, emit inverse definition.json too
9.  Emit .platform
10. Emit top-level definition.json with all parts + Base64 payloads
11. Validation gates (see §10)
```

---

## 7. `blob_url` Property Requirement

The following ontology entity types **must** carry a `blob_url` property of type `blob_url` (format `uri`). The build validation gate must fail if any of these are missing the property.

| Entity Type | Module | `blob_url` column source |
|---|---|---|
| `ImageAsset` | visual-evidence | `visual_assets.blob_url` |
| `Figure` | document-evidence | `document_elements.blob_url` |
| `VisualRegion` | visual-evidence | `visual_regions.blob_url` |
| `Image` | document-evidence | `document_elements.blob_url` |
| `Screenshot` | visual-evidence | `visual_assets.blob_url` |
| `Diagram` | visual-evidence | `visual_assets.blob_url` |
| `Photo` | visual-evidence | `visual_assets.blob_url` |

**Rationale (PRD §11.4, §16):** Blob URLs must appear in the Fabric Ontology node properties so that both graph traversal and AI Search retrieval can resolve the same visual artifact. This is not optional — it is the mechanism that links the ontology to the actual image files.

### 7.1 Azure AI Document Intelligence and Microsoft Foundry SDK (Infra Awareness)

> **Ownership note:** Visual region extraction (bounding boxes, polygon coordinates, OCR text, region provenance) is performed by the **Azure AI Document Intelligence** pipeline. LLM-generated enrichment (descriptions, visual classifications, callout labels) is produced via the **Microsoft Foundry SDK**. These are extraction and infrastructure concerns owned by Keyser and Verbal. No deep changes to ontology structure are required here.

From the ontology side, the requirement is that `VisualRegion`, `ImageAsset`, and `Figure` nodes expose the following properties so that graph traversal consumers can resolve visual evidence regardless of which extraction backend produced the data:

| Node type | Property | Type | Notes |
|---|---|---|---|
| `VisualRegion` | `blob_url` | `blob_url` | Parent or cropped image URL — populated from Document Intelligence output |
| `VisualRegion` | `polygon_json` | `string` | Bounding region coordinates from Document Intelligence |
| `VisualRegion` | `region_type` | `string` | `callout` / `ocr_text` / `component_region` / `connector_region` |
| `VisualRegion` | `label` | `string` | Detected label or callout text |
| `ImageAsset` | `blob_url` | `blob_url` | Asset URL in Blob Storage |
| `ImageAsset` | `description` | `string` | Foundry SDK–generated visual description |
| `Figure` | `blob_url` | `blob_url` | Figure image URL |
| `Figure` | `page_number` | `int` | Page provenance from Document Intelligence |
| `Figure` | `caption` | `string` | Nearby caption text |

All of these properties are already declared in `model.yaml` (§3.5). This note confirms the existing property set is sufficient for the extraction pipeline's output contract and no new ontology properties are needed.

---

## 8. Data Binding Specification

Data bindings connect ontology types to the 8 canonical Parquet tables. The schemas for those tables are defined in PRD §12 and must not be redefined here.

### 8.1 Entity Type Bindings

| Entity Type | Parquet Table | ID Column | Display Name Column | Type Filter |
|---|---|---|---|---|
| `Device` | `entities` | `entity_id` | `display_name` | `entity_type = 'Device'` |
| `Component` | `entities` | `entity_id` | `display_name` | `entity_type = 'Component'` |
| `Part` | `entities` | `entity_id` | `display_name` | `entity_type = 'Part'` |
| `PartNumber` | `entities` | `entity_id` | `display_name` | `entity_type = 'PartNumber'` |
| `Tool` | `entities` | `entity_id` | `display_name` | `entity_type = 'Tool'` |
| `Symptom` | `entities` | `entity_id` | `display_name` | `entity_type = 'Symptom'` |
| `Cause` | `entities` | `entity_id` | `display_name` | `entity_type = 'Cause'` |
| `Resolution` | `entities` | `entity_id` | `display_name` | `entity_type = 'Resolution'` |
| `Procedure` | `entities` | `entity_id` | `display_name` | `entity_type = 'Procedure'` |
| `Step` | `entities` | `entity_id` | `display_name` | `entity_type = 'Step'` |
| `ImageAsset` | `visual_assets` | `image_id` | `caption` | none |
| `Figure` | `document_elements` | `document_element_id` | `title` | `element_type = 'figure'` |
| `VisualRegion` | `visual_regions` | `visual_region_id` | `label` | none |
| `Callout` | `visual_regions` | `visual_region_id` | `label` | `region_type = 'callout'` |
| `Document` | `source_files` | `source_file_id` | `path` | none |
| `DocumentChunk` | `chunks` | `chunk_id` | `content` | none |
| `TableCell` | `document_elements` | `document_element_id` | `content` | `element_type = 'table_cell'` |
| `Chunk` | `chunks` | `chunk_id` | `content` | none |

### 8.2 Relationship Type Bindings

All relationship types bind to the `relationships` Parquet table. The type filter uses the `relationship_type` column.

| Relationship Type | `relationship_type` filter value |
|---|---|
| `has_component` | `has_component` |
| `has_part` | `has_part` |
| `has_part_number` | `has_part_number` |
| `evidenced_by` | `evidenced_by` |
| `shown_in` | `shown_in` |
| `callout_identifies` | `callout_identifies` |
| `visually_depicts` | `visually_depicts` |
| `indexed_as` | `indexed_as` |
| `stored_at` | `stored_at` |
| `extracted_from` | `extracted_from` |
| `component_of` (inverse) | `has_component` (same rows, inverse direction) |
| `part_of` (inverse) | `has_part` (same rows, inverse direction) |

### 8.3 Evidence Bindings

Relationships with `evidenceLink: true` must also emit the `evidence_id` column from the `relationships` table. This column links to `evidence.parquet` for full provenance.

---

## 9. Deployment Specification

### 9.1 Deployment Approach

The system has **three CLI deployment commands**, each with a distinct target and data type. They run in a fixed dependency order (see §9.8).

| Command | Deploys | Data destination |
|---|---|---|
| `fabric-kg deploy-lakehouse --env {env}` | Structured canonical Parquet tables (entities, relationships, chunks, evidence, visual assets) | **Fabric Lakehouse (OneLake)** — queryable canonical store |
| `fabric-kg deploy-ontology --env {env}` | Fabric Ontology definition (entity types, relationship types, data bindings bound to Lakehouse tables) | **Fabric workspace — Ontology item** |
| `fabric-kg deploy-search --env {env}` | Text chunk documents, visual chunk documents, embedding vectors | **Azure AI Search indexes** — text and visual retrieval only (**in-MVP**) |

> **Key rule:** Structured Parquet data lands in the **Fabric Lakehouse only**. AI Search receives only text and visual retrieval artifacts (chunk content, image descriptions, embedding vectors). AI Search is never the canonical store for entity or relationship data.

### 9.1.1 Deployment Mechanism: fabric-cicd (Required Primary)

**`fabric-cicd` is the REQUIRED PRIMARY deployment tool** for all three deploy commands. It is not optional. The Fabric REST API is a **fallback only** — used exclusively for item-level granularity operations that `fabric-cicd` cannot perform directly.

**Prerequisite:** Install `fabric-cicd` before running any `fabric-kg deploy-*` command. See [`docs/REQUIREMENTS-001-cli-prerequisites.md`](../REQUIREMENTS-001-cli-prerequisites.md) for the full CLI prerequisites list.

```bash
pip install fabric-cicd
```

**Deploy flow (all three commands):**

```text
1.  fabric-kg compile-* --env {env}        → packages artifacts to dist/
2.  fabric-kg deploy-* --env {env}          → invokes fabric-cicd to publish dist/ to the Fabric workspace
3.  Fabric REST API (fallback only)         → used only when fabric-cicd cannot perform a specific operation
```

| Mechanism | Role | When used |
|---|---|---|
| `fabric-cicd` | **REQUIRED PRIMARY** | All deploy commands; packages and publishes Lakehouse data, Ontology items, and AI Search artifacts to the Fabric workspace from `dist/` |
| `Fabric REST API` | **FALLBACK ONLY** | Item-level create/update when `fabric-cicd` cannot perform the specific operation (e.g., granular item-level patch, sensitivity label update) |

The `FabricDeployer` wrapper class encapsulates both mechanisms and **defaults to `fabric-cicd`**. REST calls are invoked only when `fabric-cicd` cannot satisfy the operation.

#### Dev Environment Lakehouse

| Field | Value |
|---|---|
| Lakehouse display name | `kg_lakehouse` |
| Lakehouse item ID | `44444444-4444-4444-4444-444444444444` |
| Workspace ID | `11111111-1111-1111-1111-111111111111` |

> These values are captured in `ontology/environments/dev.json` (`fabricLakehouseId`, `fabricWorkspaceId`). Do not hardcode them in source code.

### 9.2 Per-Environment JSON Format

Files live at `ontology/environments/{env}.json`. The `env` value maps to `dev`, `test`, or `prod`.

```json
{
  "env": "dev",
  "fabricWorkspaceId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "fabricLakehouseId":  "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy",
  "ontologyDisplayNameSuffix": " (Dev)",
  "sensitivityLabel": "General",
  "blobContainerUrl": "https://<account>.blob.core.windows.net/<container>",
  "blobStorageAccount": "<account>",
  "blobContainer": "<container>",
  "aiSearchServiceName": "kg-search-dev",
  "aiSearchIndexPrefix": "dev-",
  "imageArtifactPathPrefix": "dev/images"
}
```

### 9.3 What Varies vs. What Stays Stable

| Field | Varies by env | Stays stable |
|---|---|---|
| `fabricWorkspaceId` | Yes | |
| `fabricLakehouseId` | Yes | |
| `ontologyDisplayNameSuffix` | Yes | |
| `sensitivityLabel` | Yes | |
| `blobContainerUrl` | Yes | |
| `aiSearchServiceName` | Yes | |
| `aiSearchIndexPrefix` | Yes | |
| Entity type IDs (`ids.lock.json`) | | Yes — same across all envs |
| Relationship type IDs | | Yes — same across all envs |
| `model.yaml` entity/relationship definitions | | Yes — same across all envs |
| Data binding property mappings | | Yes — same across all envs |

### 9.4 Deployment Steps

```text
fabric-kg deploy-ontology --env dev
```

Internally (`fabric-cicd` is the primary mechanism; REST is fallback):

```text
1.  Read ontology/environments/{env}.json
2.  Read build/ontology/definition.json (produced by compile-ontology)
3.  Package ontology definition parts to dist/ontology/
4.  Invoke fabric-cicd to publish dist/ontology/ to the Fabric workspace
      → fabric-cicd handles workspace sync and item create/update
5.  FALLBACK (if fabric-cicd cannot perform a specific operation):
      POST to Fabric REST: items/{ontologyItemId}/updateDefinition
        Body: { "definition": { "parts": [ ... ] } }
      If response is 202 Accepted → poll long-running operation (§9.5)
6.  Inject Lakehouse ID into data binding payloads (if not already baked in)
7.  Validate deployed ontology (§10)
```

### 9.5 Long-Running Operation Polling

Fabric returns `202 Accepted` with an `Operation-Location` header for operations that run asynchronously.

```text
POST /v1/workspaces/{workspaceId}/items/{itemId}/updateDefinition
→ 202 Accepted
   Operation-Location: /v1/operations/{operationId}

Poll:
  GET /v1/operations/{operationId}
  Retry-After: <seconds from response header>

Terminal states: Succeeded | Failed | Cancelled
```

Polling rules:

1. Respect the `Retry-After` header. Default to 5 seconds if header is absent.
2. Maximum polling duration: configurable, default 10 minutes.
3. On `Failed` or `Cancelled` — print the operation `errorCode` and `message`, then fail the build.

### 9.6 Sensitivity Labels

Set the sensitivity label on the Fabric item after deployment using:

```text
PATCH /v1/workspaces/{workspaceId}/items/{itemId}
Body: { "sensitivityLabel": { "labelId": "<guid>" } }
```

The `labelId` GUID is resolved from the display name stored in `env.json` using the admin API.

### 9.7 `deploy-lakehouse` — Structured Parquet to Fabric Lakehouse

The `deploy-lakehouse` command uploads compiled Parquet tables from `build/parquet/` into the Fabric Lakehouse (`kg_lakehouse`, item ID `44444444-4444-4444-4444-444444444444`) as the queryable canonical store. **Structured canonical Parquet data is deployed here and nowhere else — it is not indexed into Azure AI Search.**

```text
fabric-kg deploy-lakehouse --env dev
```

Internally (`fabric-cicd` is the primary mechanism; REST is fallback):

```text
1.  Read ontology/environments/{env}.json → fabricWorkspaceId, fabricLakehouseId
2.  Enumerate build/parquet/ → collect all .parquet files:
      entities, relationships, chunks, evidence, source_files,
      document_elements, visual_assets, visual_regions
3.  Package Parquet files to dist/lakehouse/
4.  Invoke fabric-cicd to publish dist/lakehouse/ to the Fabric workspace
      → fabric-cicd handles Lakehouse data upload to OneLake
5.  FALLBACK (if fabric-cicd cannot perform a specific file operation):
      Upload Parquet file to OneLake via Lakehouse Files REST endpoint
      Confirm write (sync operation; no LRO for file uploads)
6.  Optional: trigger Fabric SQL endpoint refresh so Lakehouse tables appear in T-SQL
7.  Validate: confirm row counts in Lakehouse tables match build/parquet/ row counts
8.  Log deployed table names and row counts
```

**What lands in the Lakehouse:**

| Parquet table | Contents | Canonical store for |
|---|---|---|
| `entities` | All domain entities with canonical_key, aliases, properties | Devices, Components, Parts, Procedures, Steps, etc. |
| `relationships` | All typed relationship edges with evidence_id | has_component, has_part, evidenced_by, shown_in, indexed_as, etc. |
| `chunks` | Document chunks, chunk_id (AI Search key), content | DocumentChunk, Chunk, SearchIndexRecord (keyed by chunk_id) |
| `evidence` | Provenance records | Evidence facts per relationship |
| `source_files` | Ingested source file metadata | Document nodes |
| `document_elements` | Tables, table cells, figures, captions | Figure, TableCell, TableRow, etc. |
| `visual_assets` | Image asset records with blob_url | ImageAsset, Diagram, Screenshot, Photo |
| `visual_regions` | Bounding regions with polygon_json and blob_url | VisualRegion, OCRText |

### 9.8 Deployment Pipeline Ordering (CI/CD)

The three deployment commands have a fixed dependency order. Run them in this sequence in CI/CD. **All three commands invoke `fabric-cicd` as the primary publish mechanism** (Fabric REST API as fallback only).

| Step | Command | Depends on | Data destination |
|---|---|---|---|
| 1 | `fabric-kg deploy-lakehouse --env {env}` | `compile-data` complete | Fabric Lakehouse — `kg_lakehouse` (OneLake) |
| 2 | `fabric-kg deploy-ontology --env {env}` | Step 1 complete (bindings reference real tables) | Fabric workspace — Ontology item |
| 3 | `fabric-kg deploy-search --env {env}` | Step 2 complete | Azure AI Search indexes (**in-MVP**) |

**Example GitHub Actions job sequence:**

```yaml
jobs:
  deploy-lakehouse:
    runs-on: ubuntu-latest
    steps:
      - run: pip install fabric-cicd
      - run: fabric-kg compile-data --env ${{ env.DEPLOY_ENV }}
      - run: fabric-kg deploy-lakehouse --env ${{ env.DEPLOY_ENV }}

  deploy-ontology:
    needs: deploy-lakehouse
    runs-on: ubuntu-latest
    steps:
      - run: pip install fabric-cicd
      - run: fabric-kg compile-ontology --env ${{ env.DEPLOY_ENV }}
      - run: fabric-kg deploy-ontology --env ${{ env.DEPLOY_ENV }}

  deploy-search:
    needs: deploy-ontology
    runs-on: ubuntu-latest
    steps:
      - run: pip install fabric-cicd
      - run: fabric-kg compile-search --env ${{ env.DEPLOY_ENV }}
      - run: fabric-kg deploy-search --env ${{ env.DEPLOY_ENV }}
```

---

## 10. Placeholder Ontology Parts

Placeholders ensure that `compile-ontology` produces a structurally valid artifact even when source data has not yet been ingested for every entity or relationship type. The compiler emits placeholder parts for any type defined in `model.yaml` that has no matching rows in the Parquet tables.

| Placeholder path | When generated |
|---|---|
| `build/ontology/EntityTypes/{ID}/definition.json` | Always — for every entity type in model.yaml |
| `build/ontology/EntityTypes/{ID}/DataBindings/_placeholder.json` | When no Parquet binding data exists yet |
| `build/ontology/RelationshipTypes/{ID}/definition.json` | Always — for every relationship type in model.yaml |
| `build/ontology/RelationshipTypes/{ID}/Contextualizations/_placeholder.json` | When no relationship rows exist yet |

Placeholder data bindings use the schema from `model.yaml` but with an empty `rowCount: 0` annotation in a comment field. The Fabric REST API accepts them as valid definition parts.

> **Purpose (PRD §18):** Allow model-first deployment. Reserve the ontology structure before every source has been extracted.

---

## 11. Ontology-Level Validation Gates

These gates must cause `compile-ontology` and `validate` commands to fail with a non-zero exit code. They implement the checks required by PRD §21.

### 11.1 ID Validation

| Gate | Check |
|---|---|
| Missing type ID | Every entity and relationship type in `model.yaml` must have an entry in `ids.lock.json` |
| Duplicate type ID | No two types in `ids.lock.json` may share the same numeric ID string |
| ID range violation | Entity type IDs must start with `1`; relationship type IDs must start with `2` |

### 11.2 Reference Validation

| Gate | Check |
|---|---|
| Unknown entity type in relationship | `sourceType` and `targetType` in every relationship type definition must match a name in `entityTypes` |
| Missing inverse ID | If `inversePolicy: materialize` or `alias`, `inverseName` must have an entry in `ids.lock.json` |
| Unknown module reference | Every entity type's `module` field must match a declared module name |

### 11.3 `blob_url` Validation

| Gate | Check |
|---|---|
| Missing `blob_url` property | `ImageAsset`, `Figure`, `VisualRegion`, `Image`, `Screenshot`, `Diagram`, `Photo` must each declare a `blob_url` property with `type: blob_url` |
| Missing `blob_url` in binding | The data binding for each visual type must map `blob_url` to the appropriate Parquet column |

### 11.4 Binding / Schema Validation

| Gate | Check |
|---|---|
| Unknown Parquet table | The `table` in every `dataBinding` must be one of the 8 canonical table names |
| Unknown column reference | Every column referenced in `dataBinding.additionalColumns` must exist in the canonical Parquet schema for that table |
| Schema mismatch | If a Parquet file exists in `build/parquet/`, column names and types must match the binding expectations |

### 11.5 Structural Validation

| Gate | Check |
|---|---|
| Empty entity type list | `model.yaml` must define at least one entity type |
| Missing `name` on any type | Every entity type and relationship type must have a non-empty `name` |
| Duplicate type name | No two entity types, and no two relationship types, may share the same name |

### 11.6 Summary Table

| Gate | Command | Severity |
|---|---|---|
| Missing type ID | `compile-ontology` | Error — build fails |
| Duplicate type ID | `compile-ontology` | Error — build fails |
| Unknown entity type in relationship | `compile-ontology` | Error — build fails |
| Missing `blob_url` on visual type | `compile-ontology` | Error — build fails |
| Unknown Parquet table in binding | `compile-ontology` | Error — build fails |
| Schema mismatch (Parquet vs binding) | `validate` | Error — build fails |
| Missing `blob_url` in Parquet data | `validate` | Error — build fails |
| Duplicate display names within a type | `validate` | Warning |
| Missing placeholder for declared type | `compile-ontology` | Warning — placeholder auto-generated |

---

## 12. Graph-to-Search Bridge (Ontology Support)

### 12.1 Reference Pattern: Graph-then-Search (Local Search / OmniRAG)

The two-phase retrieval flow implemented by this bridge follows two established production patterns (cited: RESEARCH-001 §1, §2):

- **GraphRAG Local Search** (Microsoft Research): the graph traversal step identifies *access-point entities* that gate retrieval of raw text; AI Search then retrieves the grounding chunks via those access points. [graphrag local_search](https://microsoft.github.io/graphrag/query/local_search/)
- **CosmosAIGraph OmniRAG**: intent → KG traversal → vector/hybrid search using all data sources in their original format, minimising data movement. [CosmosAIGraph](https://github.com/AzureCosmosDB/CosmosAIGraph)

**Our advantage over GraphRAG:** GraphRAG LLM-extracts a graph from text before each query — slow and approximate. We have a **structured Fabric Ontology** with deterministic entity IDs, canonical keys, and explicit relationship types already compiled from canonical Parquet data. Phase 1 traversal is therefore faster and more precise. The ontology *is* the graph; no LLM extraction step is needed to find access points.

**Two-phase flow:**

| Phase | What happens | Mechanism |
|---|---|---|
| **Phase 1 — Ontology traversal** | GQL query over Fabric Ontology; scoped by relationship type + hop count (§12.4); returns `entity_ids`, `canonical_key`, `search_aliases`, `graph_path` | Fabric Ontology (GQL) |
| **Phase 2 — Grounding retrieval** | Hybrid (BM25 + vector) AI Search query filtered by `entity_ids` via `search.in()`; semantic reranking; returns grounding chunks + captions | Azure AI Search |

### 12.2 Bridge Relationship Types

Three relationship types form the traversable bridge from domain entities to retrieval records. All three must be declared in `model.yaml` with `inversePolicy` set (§4.3 rule). A single traversal following `evidenced_by → indexed_as` or `shown_in` produces everything needed to build a Phase 2 AI Search query.

| Relationship | Module | Source → Target | Traversal purpose | Fields yielded |
|---|---|---|---|---|
| `evidenced_by` | document-evidence | any domain entity → `DocumentChunk` | Entity → text chunk: retrieves chunk for AI Search lookup | `chunk_id`, `related_entity_ids` |
| `shown_in` | visual-evidence | any domain entity → `Figure` / `ImageAsset` | Entity → visual asset: retrieves blob URL for visual grounding | `blob_url`, `image_id` |
| `indexed_as` | retrieval | `DocumentChunk` → `SearchIndexRecord` | Chunk → search record: yields direct AI Search document key | `search_record_id` |

### 12.3 Required Node Properties for Bridge Traversal

Phase 1 must return all values Phase 2 needs in a single traversal. Properties already mandated elsewhere in this spec are cross-referenced. **Do not redefine SPEC-002 (Fenster) schemas** — align to those columns by name.

| Node type | Property | Type | Required | SPEC-002 source column | Purpose in Phase 2 |
|---|---|---|---|---|---|
| Any domain entity (`support-domain`) | `entity_id` | `string` | Yes | `entities.entity_id` | Stable opaque ID; used in `search.in()` filter — never changes once issued |
| Any domain entity (`support-domain`) | `canonical_key` | `string` | Yes | `entities.canonical_key` | Filterable identity key in AI Search (SPEC-002 normalization rules apply); also carried as a JOINed derived field on the AI Search document |
| Any domain entity (`support-domain`) | `search_aliases` | `list<string>` | Yes | `entities.search_aliases` | BM25 / keyword query terms in Phase 2 (top aliases used as query string); format = `[canonical_key] + [display_name.lower()] + [alias.lower() for alias in aliases]` |
| `DocumentChunk` | `chunk_id` | `string` | Yes | `chunks.chunk_id` | Primary key for AI Search document lookup |
| `DocumentChunk` | `related_entity_ids` | `list<string>` | Yes | `chunks.related_entity_ids` | Entity IDs linked to this chunk; feeds the AI Search `entity_ids` filterable field directly; compile-time JOIN source for `canonical_key` and `entity_aliases` derived fields |
| `DocumentChunk` | `entity_search_keys` | `list<string>` | Yes | `chunks.entity_search_keys` | Denormalized canonical_keys, display_names, and aliases for all entities in `related_entity_ids`; populated at compile-data time by JOIN to `entities.search_aliases`; feeds the AI Search `entity_aliases` searchable field |
| `SearchIndexRecord` | `search_record_id` | `string` | Yes | `chunks.chunk_id` | AI Search document key for direct retrieval (= `chunk_id` at index-build time) |
| `SearchIndexRecord` | `canonical_key` | `string` | No | `entities.canonical_key` (compile-time JOIN via `related_entity_ids[0]`) | Derived filterable field on the AI Search document; not a raw chunk column — populated at index-build from the primary linked entity |
| `ImageAsset` | `blob_url` | `blob_url` | Yes | `visual_assets.blob_url` | Already required (§7); visual grounding URL passed to vision model |
| `ImageAsset` | `entity_id` | `string` | Yes | `visual_assets.image_id` | Included in `entity_ids` array on image-aware AI Search index |
| `Figure` | `blob_url` | `blob_url` | Yes | `document_elements.blob_url` | Already required (§7); visual grounding URL passed to vision model |

> **AI Search derived fields:** `entity_ids`, `canonical_key`, `entity_aliases`, and `graph_path` fields that appear in the AI Search index are **DERIVED at index-build time** from the canonical SPEC-002 columns above — they are not raw chunk columns. `entity_ids` ← `chunks.related_entity_ids`; `canonical_key` ← JOIN to `entities.canonical_key`; `entity_aliases` ← `chunks.entity_search_keys` (itself populated from `entities.search_aliases`); `graph_path` ← serialized at traversal time (§12.5).

### 12.4 Bounded Traversal (Anti-Over-Fetch)

Sending an uncapped set of entity IDs to AI Search causes two failures: `search.in()` filter string size limits (16 MB POST body / 8 KB GET query string) and relevance degradation from noise. The graph query feeding Phase 2 **must** be bounded. (Cited: RESEARCH-001 §7 anti-pattern "Over-fetching from the graph".)

**Traversal rules:**

| Rule | Constraint | Rationale |
|---|---|---|
| Scope by relationship type | Traverse only `evidenced_by`, `shown_in`, `indexed_as` in a bridge query — not all relationship types | Prevents accidental traversal into unrelated domains |
| Cap hop count | Maximum **2 hops** from anchor entity (e.g., `Part → evidenced_by → DocumentChunk → indexed_as → SearchIndexRecord`) | Prevents combinatorial explosion |
| Cap result entities | Return at most **10–20 ranked entities** before constructing the AI Search filter | Keeps `search.in()` payload well within limits |
| Rank before filter | Score entities by traversal distance + relationship confidence; pass top-N to Phase 2 | Improves relevance; degrades gracefully |
| Fallback when result = 0 | If graph returns no access-point entities, run pure hybrid AI Search without the entity filter | Avoids zero-result failure for poorly-linked queries |

**Relationship types NOT traversed** in a bridge query (valid in the ontology for other uses):

- `has_part`, `part_of`, `variant_of`, `replaces` — structural/hierarchical; yield no retrieval records
- Any relationship whose target is in `support-domain` (entity → entity hops beyond hop-count limit)

### 12.5 graph_path Provenance

Every Phase 1 traversal emits a serialized **graph_path** string capturing the route from anchor entity to retrieval node. This string is carried on each AI Search chunk document (the `graph_path` field; `Edm.String`, retrievable, not filterable — see RESEARCH-001 §4) and passed to the LLM prompt as a citation annotation. Shorter paths rank higher (fewer hops = more direct evidence).

**Format:**

```
<EntityType> "<label>" --[<rel_type>]--> <NodeType> "<label>" --[<rel_type>]--> <NodeType> "<label>"
```

**Examples:**

```text
Part "Battery Pack" --[evidenced_by]--> DocumentChunk "chunk:a1b2c3"

PartNumber "BP-4200" --[identifies]--> Part "Battery Pack" --[evidenced_by]--> DocumentChunk "chunk:a1b2c3" --[indexed_as]--> SearchIndexRecord "dev-chunks"

Component "Cell Module" --[shown_in]--> Figure "fig:diagram-03"
```

**Rules:**
- Serialized at traversal time; not reconstructed after the fact.
- Not filterable in AI Search; passed through as a retrievable string for LLM citation.
- When multiple traversal paths reach the same chunk, keep the shortest path.

### 12.6 Sync and Deploy Separation

Ontology-linked data lives in two systems with separate deploy pipelines that must not be conflated. (Cited: RESEARCH-001 §5.)

| Data type | Storage | Deploy command | Indexer path |
|---|---|---|---|
| Structured Parquet tables (entities, chunks, relationships) | Fabric Lakehouse (OneLake) | `deploy-lakehouse` | Upload Parquet directly to OneLake Tables or Files location |
| Text / visual chunk documents for AI Search | Azure AI Search index | `deploy-search` | **Push API or Fabric Data Pipeline** — OneLake indexer does NOT read Parquet/Delta |
| Ontology definition parts | Fabric Workspace (item definitions) | `deploy-ontology` | Fabric REST API (LRO, see §9.5) |

⚠️ **Critical constraint (RESEARCH-001 §5):** The OneLake indexer supports only the Lakehouse **Files** location, not Tables / Parquet / Delta format. AI Search chunk documents must be pushed via a custom pipeline (Fabric Data Pipeline → Azure Function → Push API, or direct Push API on entity-change event). This is why `deploy-search` is a separate command from `deploy-lakehouse`.

**Sync-on-change rule (entity update):**

1. Re-query the changed entity's linked `DocumentChunk` / `SearchIndexRecord` / `ImageAsset` nodes (bounded traversal per §12.4).
2. Partial-update the derived AI Search fields on those chunk documents via Push API merge — `entity_ids` (from `chunks.related_entity_ids`), `canonical_key` (JOINed from `entities.canonical_key` via `related_entity_ids[0]`), and `entity_aliases` (from `chunks.entity_search_keys`, itself populated from `entities.search_aliases`) — no re-chunk or re-embed unless canonical text changed materially.
3. On entity deletion: remove the `entity_id` from the `entity_ids` array on linked chunks; do not delete the chunk document unless the source document is also removed.

### 12.7 `model.yaml` Additions for Bridge Nodes

Add or verify the following entries in `model.yaml` to complete bridge support. Domain entity types already include `entity_id`, `canonical_key`, and `search_aliases` (see §3.5 snippets); the entries below focus on retrieval-side nodes.

**`DocumentChunk` entity type — bridge properties:**

```yaml
- name: DocumentChunk
  description: "A text chunk from document chunking"
  module: document-evidence
  properties:
    - name: display_name
      type: string
      required: true
      description: "Chunk content preview (first 200 chars)"
    - name: entity_id
      type: string
      required: true
      description: "Stable opaque chunk ID — SHA-256-derived (SPEC-002)"
    - name: chunk_id
      type: string
      required: true
      description: "Primary key for AI Search chunk document lookup (= entity_id)"
    - name: related_entity_ids
      type: list<string>
      required: false
      description: "Entity IDs linked to this chunk (SPEC-002 chunks.related_entity_ids); feeds AI Search entity_ids filterable field; compile-time JOIN source for canonical_key and entity_aliases derived fields"
    - name: entity_search_keys
      type: list<string>
      required: false
      description: "Denormalized canonical_keys + display_names + aliases for all entities in related_entity_ids; populated at compile-data time by JOIN to entities.search_aliases (SPEC-002 chunks.entity_search_keys); feeds AI Search entity_aliases searchable field"
    - name: content
      type: string
      required: false
      description: "Full chunk text content"
  dataBinding:
    table: chunks
    entityIdColumn: chunk_id
    displayNameColumn: content
    additionalColumns:
      - property: entity_id
        column: chunk_id
      - property: chunk_id
        column: chunk_id
      - property: related_entity_ids
        column: related_entity_ids
      - property: entity_search_keys
        column: entity_search_keys
```

**`SearchIndexRecord` entity type — bridge properties:**

```yaml
- name: SearchIndexRecord
  description: "A record in a specific AI Search index — keyed by chunk_id"
  module: retrieval
  properties:
    - name: display_name
      type: string
      required: true
      description: "AI Search document key"
    - name: entity_id
      type: string
      required: true
      description: "Stable opaque record ID — SHA-256-derived (SPEC-002)"
    - name: search_record_id
      type: string
      required: true
      description: "AI Search document key for direct retrieval (= chunk_id at index-build time)"
    - name: canonical_key
      type: string
      required: false
      description: "Derived filterable field on the AI Search document; populated at index-build time by compile-data JOIN to entities.canonical_key via related_entity_ids[0] — not a raw chunks column"
    - name: entity_search_keys
      type: list<string>
      required: false
      description: "Derived searchable field on the AI Search document; populated at index-build time from chunks.entity_search_keys (itself from entities.search_aliases) — feeds AI Search entity_aliases field"
  dataBinding:
    table: chunks
    entityIdColumn: chunk_id
    displayNameColumn: chunk_id
    additionalColumns:
      - property: entity_id
        column: chunk_id
      - property: search_record_id
        column: chunk_id
      # canonical_key and entity_search_keys are DERIVED at index-build via compile-data JOINs;
      # they are not direct chunk column bindings
```

### 12.8 Traversal Example: Domain Entity → AI Search

The following illustrates a 2-hop Phase 1 traversal (within the §12.4 hop and entity caps) producing Phase 2 inputs:

```text
PartNumber "BP-4200"
  --[identifies]-->
Part "Battery Pack"  (entity_id="ent:7a3f…", canonical_key="part:battery-pack",
                      search_aliases=["part:battery-pack","battery pack","BP4200","li-ion pack"])
  --[evidenced_by]-->
DocumentChunk (chunk_id="chunk:a1b2c3",
               related_entity_ids=["ent:7a3f…"],
               entity_search_keys=["part:battery-pack","battery pack","BP4200","li-ion pack"])
  --[indexed_as]-->
SearchIndexRecord (search_record_id="chunk:a1b2c3")
  # canonical_key="part:battery-pack" and entity_aliases on the AI Search document
  # are DERIVED at index-build time from the compile-data JOINs above

graph_path: "PartNumber 'BP-4200' --[identifies]--> Part 'Battery Pack' --[evidenced_by]--> DocumentChunk 'chunk:a1b2c3' --[indexed_as]--> SearchIndexRecord 'chunk:a1b2c3'"
```

**Phase 2 — AI Search query (constructed from Phase 1 output):**

```jsonc
{
  "search": "Battery Pack BP4200 li-ion pack",
  "vectorQueries": [{ "kind": "text", "text": "Battery Pack BP-4200", "fields": "chunk_vector", "k": 50 }],
  "filter": "search.in(entity_ids, 'ent:7a3f…', ',')",
  "vectorFilterMode": "preFilter",
  "queryType": "semantic",
  "semanticConfiguration": "fabric-semantic-config",
  "select": "chunk_id, chunk_text, source_path, blob_url, entity_ids, canonical_key, entity_aliases, graph_path",
  "top": 5,
  "captions": "extractive"
}
```

For visual grounding, the `shown_in` → `Figure.blob_url` / `ImageAsset.blob_url` path follows the same pattern; `blob_url` is returned directly from Phase 1 traversal and passed to the vision model.

### 12.9 Validation Gates for Bridge Relationships

The compiler and `fabric-kg validate` enforce these gates at build time. Gates marked **Error** fail the build; **Warning** gates produce log entries without blocking. Gates BRG-001–BRG-010 are registered in SPEC-005 (Hockney) VAL rule framework.

| Gate ID | Check | Severity |
|---|---|---|
| BRG-001 | `DocumentChunk` declares `entity_id`, `chunk_id`, `related_entity_ids`, and `entity_search_keys` with correct types | Error — build fails |
| BRG-002 | `SearchIndexRecord` declares `search_record_id` with correct type; `canonical_key` and `entity_search_keys` are noted as compile-time derived (not raw column bindings) | Error — build fails |
| BRG-003 | Every entity type in `support-domain` module declares `entity_id`, `canonical_key`, and `search_aliases` | Error — build fails |
| BRG-004 | `ImageAsset` and `Figure` declare `blob_url` property (format `uri`) | Error — build fails (also §7 gate) |
| BRG-005 | `evidenced_by`, `shown_in`, `indexed_as` relationship types exist in `model.yaml` with `inversePolicy` set | Error — build fails |
| BRG-006 | All `indexed_as` relationships resolve: every `SearchIndexRecord` referenced by an `indexed_as` edge exists as a node with `search_record_id` populated | Error — build fails |
| BRG-007 | All `evidenced_by` relationships resolve: every `DocumentChunk` referenced by an `evidenced_by` edge exists as a node with `chunk_id` populated | Error — build fails |
| BRG-008 | All `shown_in` relationships resolve: every `Figure` / `ImageAsset` referenced by a `shown_in` edge has a non-empty `blob_url` | Error — build fails |
| BRG-009 | `support-domain` entities with no outbound `evidenced_by` or `shown_in` edges | Warning — logged, not blocking |
| BRG-010 | `entity_id` on any support-domain node is empty or duplicated within its entity type | Error — build fails |

### 12.10 Table Document-Element Nodes in the Bridge

`Table` document-element nodes (produced by DI Layout; `element_type="table"`, `content_html`, `blob_url`) participate in the graph-to-search bridge on equal footing with visual assets and text chunks.

**Bridge linkage for tables:**

| Relationship | Source → Target | Effect |
|---|---|---|
| `evidenced_by` | any domain entity → `DocumentChunk` (chunk_type="table_html") | Entity linked to table chunk; chunk indexed as independent AI Search doc |
| `shown_in` | any domain entity → `Table` document-element node | Entity linked to table as visual/structural evidence; `blob_url` used for direct artifact retrieval |

**Example (graph_path):**
```text
PartNumber "BP-4200" --[identifies]--> Part "Battery Pack" --[evidenced_by]--> DocumentChunk "table_html:…" (content_html=<table>…)
Part "Battery Pack" --[shown_in]--> Table "table_0" (blob_url=https://…/table_0.html)
```

Because each table is indexed as its **own AI Search document** (`chunk_type="table_html"`), Phase 2 retrieval returns the full `<table>` HTML alongside text chunks. The `blob_url` on the `Table` node allows the agent to surface the rendered HTML artifact directly. This is the key benefit of the DI Layout approach: tables become first-class, independently retrievable graph artifacts — not embedded prose.

> **Validation (2026-06-24):** Real DI Layout on a Surface PDF yielded 2 tables → 2 `table_html` chunks → 2 independent AI Search docs (coordinator-tables-via-docintel.md). Reference implementations: `microsoft/Document-Knowledge-Mining-Solution-Accelerator`, `Azure-Samples/document-intelligence-code-samples`.

## 13. Revision History

| Date | Author | Summary |
|---|---|---|
| 2026-06-24 | McManus | Initial draft — modular ontology, compiler spec, deployment approach, placeholder rules, validation gates |
| 2026-06-24T11:46:10.517-07:00 | McManus | Added `deploy-lakehouse` command; clarified structured canonical data lands in Fabric Lakehouse (not AI Search); added §9.7 deploy-lakehouse steps, §9.8 CI/CD pipeline ordering; added §12 Graph-to-Search Bridge (Ontology Support); added §7.1 Azure AI Document Intelligence / Microsoft Foundry SDK awareness note; updated §1.1 Boundaries and §2.3 retrieval relationships with bridge notes |
| 2026-06-24T12:42:17.255-07:00 | McManus | §12 Graph-to-Search Bridge rewritten to fold RESEARCH-001 findings: added graph-then-search reference pattern (GraphRAG Local Search / OmniRAG), §12.4 bounded traversal (2-hop cap, 10–20 entity cap, rel-type scoping), §12.5 graph_path provenance format, §12.6 sync/deploy separation with Parquet/OneLake indexer constraint; expanded §12.3 node properties (entity_id, aliases/search_aliases on all support-domain entities and retrieval nodes); BRG validation gates extended to BRG-001–BRG-010 with resolve-to-existing-record and required-keys/blob_url checks; model.yaml snippets updated with entity_id + aliases fields |
| 2026-06-24T13:24:31-07:00 | McManus | §12 bridge bindings reconciled to SPEC-002 canonical columns (CRITICAL finding #5): removed non-existent chunks.canonical_key, chunks.search_index_name, chunks.aliases references; replaced with chunks.related_entity_ids and chunks.entity_search_keys as the real source columns; compile-time JOINs to entities.canonical_key and entities.search_aliases made explicit throughout §12.1, §12.2, §12.3, §12.6, §12.7, §12.8; AI Search index fields (entity_ids, canonical_key, entity_aliases, graph_path) declared DERIVED at index-build time; BRG-001 updated to require related_entity_ids + entity_search_keys; BRG-002 updated to drop search_index_name; BRG-003 updated to require search_aliases |
| 2026-06-24T15:41:07.842-07:00 | McManus | §9 deployment mechanism reframed per Hyunsuk Shin decision: `fabric-cicd` is the REQUIRED PRIMARY tool for deploy-lakehouse, deploy-ontology, and deploy-search — not an optional "wrap both" choice; Fabric REST API demoted to fallback only (item-level granularity when fabric-cicd cannot perform the operation); `fabric-cicd` added as CLI prerequisite (`pip install fabric-cicd`), reference to `docs/REQUIREMENTS-001-cli-prerequisites.md`; deploy flow documented as compile → dist/ → fabric-cicd publish; FabricDeployer defaults to fabric-cicd; deploy-search corrected from *(optional)* to in-MVP throughout; dev Lakehouse name updated to `kg_lakehouse` (item ID `44444444-4444-4444-4444-444444444444`, workspace `11111111-1111-1111-1111-111111111111`); §9.1.1 mechanism table, §9.4 deploy-ontology steps, §9.7 deploy-lakehouse steps, §9.8 CI/CD pipeline updated; `if: ENABLE_AI_SEARCH` gate removed from deploy-search job |
| 2026-06-24T21:46:59.576-07:00 | McManus | §12 bridge — §12.10 added: Table document-element nodes (element_type="table", content_html, blob_url) participate in the evidenced_by / shown_in bridge, enabling graph↔table integration and AI Search indexing of tables as independent documents (coordinator-tables-via-docintel.md, verified 2026-06-24). |

---

*End of SPEC-003*
