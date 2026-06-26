# Fabric KB Builder: Implementation Backlog

Date: 2026-06-24
Status: Starting backlog for new project

## Milestone 0: Project Setup

Goal: create the new repo and basic package skeleton.

Tasks:

```text
[ ] Create repo/folder fabric-kb-builder
[ ] Add pyproject.toml
[ ] Add README.md
[ ] Add docs/decision-snapshot.md
[ ] Add src/fabric_kb_builder package
[ ] Add CLI entry point fabric-kb
[ ] Add basic config loader
[ ] Add tests folder
```

Done when:

```powershell
fabric-kb --help
```

prints command help from the installed package.

## Milestone 1: CSV MVP

Goal: produce canonical JSON and Parquet from CSV input.

Tasks:

```text
[ ] Implement csv_loader.py
[ ] Implement inspect-source command
[ ] Add source_files canonical schema
[ ] Add entities canonical schema
[ ] Add relationships canonical schema
[ ] Add evidence canonical schema
[ ] Implement canonical JSON writer
[ ] Implement Parquet writer
[ ] Add sample CSV
[ ] Add tests for CSV load -> Parquet output
```

Done when:

```powershell
fabric-kb inspect-source --input examples/csv/sample.csv
fabric-kb compile-data --input examples/csv/sample.csv --out build/parquet
```

produces:

```text
build/parquet/source_files.parquet
build/parquet/entities.parquet
build/parquet/relationships.parquet
build/parquet/evidence.parquet
```

## Milestone 2: LLM Enrichment Contract

Goal: use OpenAI to map source rows/columns into the canonical model.

Tasks:

```text
[ ] Define LLM JSON response schema
[ ] Implement llm_enrich.py
[ ] Add validation for LLM response
[ ] Add canonicalization pass
[ ] Add confidence and evidence fields
[ ] Add retry and JSON salvage handling
[ ] Add fixture-based tests with mocked LLM output
```

LLM output should be intermediate only:

```text
LLM JSON -> validate -> canonical model -> Parquet
```

Done when CSV rows can be enriched into typed `Device`, `Component`, `Part`, and `PartNumber` entities.

## Milestone 3: Ontology Compiler

Goal: compile `ontology/model.yaml` + `ids.lock.json` into Fabric ontology definition parts.

Tasks:

```text
[ ] Add ontology/model.yaml
[ ] Add ontology/ids.lock.json
[ ] Implement model YAML parser
[ ] Implement Fabric entity type definition generator
[ ] Implement Fabric relationship type definition generator
[ ] Implement base64 part packager
[ ] Add .platform and definition.json generation
[ ] Add validation for missing IDs and dangling relationship ends
```

Done when:

```powershell
fabric-kb compile-ontology --out build/ontology
fabric-kb package --out dist
```

produces a Fabric REST-compatible ontology definition payload.

## Milestone 4: Fabric Deployment

Goal: deploy data and ontology to Fabric dev workspace.

Tasks:

```text
[ ] Add environment config dev/test/prod
[ ] Add Fabric auth strategy
[ ] Add Fabric REST client
[ ] Add LRO polling with Retry-After support
[ ] Add create ontology path
[ ] Add update ontology path or versioned fallback
[ ] Add Lakehouse data deployment strategy
[ ] Add smoke validation
```

Preflight requirements:

```text
[ ] Workspace on supported Fabric capacity
[ ] CI identity has Contributor workspace role
[ ] CI identity has Item.ReadWrite.All
[ ] Service principal Fabric API tenant setting enabled, if using SPN
```

Done when dev deployment creates/updates the ontology item and deployed data is queryable/bindable.

## Milestone 5: Document Ingestion

Goal: support PDF/DOCX/HTML/Markdown sources after CSV path is stable.

Tasks:

```text
[ ] Add document_loader.py
[ ] Extract document text and page structure
[ ] Extract sections / TOC
[ ] Extract tables with rows/cols/cells
[ ] Extract figures and captions
[ ] Add LLM extraction for document facts
[ ] Add evidence links to document elements
[ ] Add document-specific Parquet tables when needed
```

Document-specific entities to add:

```text
Document
Section
Page
Table
TableRow
TableColumn
TableCell
Figure
Callout
VisualRegion
Procedure
Step
Warning
DecisionPoint
Condition
```

## Future Capabilities

```text
[ ] Plugin source adapters
[ ] Prompt versioning
[ ] Schema migration support
[ ] Drift detection across environments
[ ] Relation cardinality checks
[ ] Hub node detection
[ ] Sample Fabric GQL validation queries
[ ] Power BI resource links
[ ] Ontology overview widgets
```
