"""Azure AI Search index compiler and document builder.

Generates index schemas and document batches from the text/visual
Parquet tables only: chunks, document_elements, visual_assets.
Structured/tabular tables (entities, relationships, evidence,
source_files) are NOT indexed here — they live in the Fabric Lakehouse.
"""
