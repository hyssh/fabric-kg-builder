# Fabric KB Builder: Decision Snapshot

Date: 2026-06-24
Status: Handoff for new project
Source repo: `starbuck-siot-kb`

## Goal

Build a reusable CLI that ingests documents or tabular files, uses OpenAI for extraction and enrichment, writes canonical Parquet tables, generates a Microsoft Fabric Ontology definition, and deploys both data and ontology through Fabric CI/CD / Fabric REST APIs.

The current repo remains a reference implementation for Azure AI Search RAG and graph extraction lessons. The new project should be clean, Fabric-first, and source-agnostic.

## Product Shape

```text
Source files
  -> document or CSV loading
  -> OpenAI extraction / enrichment
  -> validated canonical model
  -> canonical Parquet tables
  -> Fabric Lakehouse data
  -> Fabric Ontology definition
  -> fabric-cicd / Fabric REST deployment
```

## Key Decisions

| Decision | Rationale |
|---|---|
| Start a new repo/project | The current repo is Surface + Azure AI Search demo oriented; the new tool is a generic Fabric ingestion and ontology CLI. |
| Use one ontology, not two isolated ontologies | Technician-style questions cross domain facts and document evidence; one ontology with modules keeps traversal natural. |
| Model as connected modules | Use `support-domain`, `document-evidence`, and `provenance` modules inside one ontology. |
| Treat canonical Parquet as the data contract | Fabric Ontology should bind to stable Lakehouse tables, not raw LLM output. |
| Treat LLM output as intermediate | LLM output must be validated, canonicalized, and converted into deterministic tables before deployment. |
| Support documents and CSV | Documents require extraction; CSV skips structural extraction but still needs LLM enrichment and ontology mapping. |
| Generate placeholders | The CLI should create placeholder folders/files/tables/bindings when a model expects artifacts that are not filled yet. |
| Use deterministic Fabric ontology IDs | Fabric entity type, relationship type, and property IDs must be stable across dev/test/prod via `ids.lock.json`. |
| Deploy model and data together | CI/CD should package Parquet data plus ontology definitions and deploy/promote the same artifact. |

## Recommended New Repo Name

Preferred:

```text
fabric-kb-builder
```

Other acceptable names:

```text
fabric-ontology-ingestion-cli
fabric-ontology-builder
```

## First MVP

Start with CSV before documents.

```text
Given a CSV with device/component/part rows,
the CLI produces:
  - source_files.parquet
  - entities.parquet
  - relationships.parquet
  - evidence.parquet
  - a minimal Fabric Ontology definition package
```

Do not begin with PDF/DOCX complexity or Fabric deployment debugging. First prove the local data contract and ontology compiler.

## Immediate Build Order

1. Create new repo `fabric-kb-builder`.
2. Add this decision snapshot to `docs/decision-snapshot.md`.
3. Create Python CLI skeleton.
4. Add `ontology/model.yaml`.
5. Add `ontology/ids.lock.json`.
6. Implement CSV loader.
7. Implement LLM enrichment contract.
8. Implement canonical model validation.
9. Implement Parquet writer.
10. Implement ontology compiler.
11. Package Fabric ontology definition parts.
12. Add Fabric REST / fabric-cicd deployment.
13. Add document extraction.

## Core Rule

```text
Canonical Parquet is the data contract.
Fabric Ontology is the semantic layer.
LLM output is only an intermediate.
CI/CD deploys versioned data and ontology artifacts.
```
