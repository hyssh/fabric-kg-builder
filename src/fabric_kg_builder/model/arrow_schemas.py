"""PyArrow schemas for all 8 canonical Parquet tables.

Exactly mirrors SPEC-002 §3 types, nullability, and §11 graph→search columns.
All schemas use pa.schema([...]) and are importable individually or via the
TABLE_SCHEMAS registry.
"""

from __future__ import annotations

import pyarrow as pa

_TS = pa.timestamp("us", tz="UTC")
_STR = pa.string()
_INT32 = pa.int32()
_INT64 = pa.int64()
_FLOAT64 = pa.float64()
_BOOL = pa.bool_()
_LIST_STR = pa.list_(pa.string())


# ---------------------------------------------------------------------------
# 3.2  source_files
# ---------------------------------------------------------------------------

SOURCE_FILES_SCHEMA = pa.schema(
    [
        pa.field("source_file_id", _STR, nullable=False),
        pa.field("path", _STR, nullable=False),
        pa.field("filename", _STR, nullable=False),
        pa.field("source_type", _STR, nullable=False),
        pa.field("content_hash", _STR, nullable=False),
        pa.field("byte_size", _INT64, nullable=True),
        pa.field("ingested_at", _TS, nullable=False),
        pa.field("schema_profile_path", _STR, nullable=True),
        pa.field("row_count", _INT64, nullable=True),
        pa.field("notes", _STR, nullable=True),
    ]
)

# ---------------------------------------------------------------------------
# 3.3  document_elements
# ---------------------------------------------------------------------------

DOCUMENT_ELEMENTS_SCHEMA = pa.schema(
    [
        pa.field("document_element_id", _STR, nullable=False),
        pa.field("source_file_id", _STR, nullable=False),
        pa.field("element_type", _STR, nullable=False),
        pa.field("parent_element_id", _STR, nullable=True),
        pa.field("title", _STR, nullable=True),
        pa.field("content", _STR, nullable=True),
        pa.field("content_html", _STR, nullable=True),
        pa.field("blob_url", _STR, nullable=True),
        pa.field("page_number", _INT32, nullable=True),
        pa.field("section_path", _STR, nullable=True),
        pa.field("sort_order", _INT32, nullable=True),
        pa.field("row_index", _INT32, nullable=True),
        pa.field("col_index", _INT32, nullable=True),
        pa.field("content_hash", _STR, nullable=False),
        pa.field("extracted_at", _TS, nullable=False),
    ]
)

# ---------------------------------------------------------------------------
# 3.4  chunks  (incl. §11 entity_search_keys)
# ---------------------------------------------------------------------------

CHUNKS_SCHEMA = pa.schema(
    [
        pa.field("chunk_id", _STR, nullable=False),
        pa.field("source_file_id", _STR, nullable=False),
        pa.field("document_element_id", _STR, nullable=True),
        pa.field("chunk_type", _STR, nullable=False),
        pa.field("content", _STR, nullable=False),
        pa.field("content_html", _STR, nullable=True),
        pa.field("embedding_text", _STR, nullable=True),
        pa.field("blob_url", _STR, nullable=True),
        pa.field("page_number", _INT32, nullable=True),
        pa.field("section_path", _STR, nullable=True),
        pa.field("table_id", _STR, nullable=True),
        pa.field("figure_id", _STR, nullable=True),
        pa.field("image_id", _STR, nullable=True),
        pa.field("related_entity_ids", _LIST_STR, nullable=True),
        pa.field("entity_search_keys", _LIST_STR, nullable=True),  # §11 AI Search
        pa.field("content_hash", _STR, nullable=False),
        pa.field("created_at", _TS, nullable=False),
    ]
)

# ---------------------------------------------------------------------------
# 3.5  entities  (incl. §11 search_aliases)
# ---------------------------------------------------------------------------

ENTITIES_SCHEMA = pa.schema(
    [
        pa.field("entity_id", _STR, nullable=False),
        pa.field("entity_type", _STR, nullable=False),
        pa.field("display_name", _STR, nullable=False),
        pa.field("canonical_key", _STR, nullable=False),
        pa.field("aliases", _LIST_STR, nullable=True),
        pa.field("search_aliases", _LIST_STR, nullable=True),  # §11 AI Search
        pa.field("description", _STR, nullable=True),
        pa.field("properties_json", _STR, nullable=True),
        pa.field("source_file_id", _STR, nullable=True),
        pa.field("confidence", _FLOAT64, nullable=True),
        pa.field("is_placeholder", _BOOL, nullable=False),
        pa.field("content_hash", _STR, nullable=False),
        pa.field("created_at", _TS, nullable=False),
        pa.field("updated_at", _TS, nullable=False),
    ]
)

# ---------------------------------------------------------------------------
# 3.6  relationships
# ---------------------------------------------------------------------------

RELATIONSHIPS_SCHEMA = pa.schema(
    [
        pa.field("relationship_id", _STR, nullable=False),
        pa.field("relationship_type", _STR, nullable=False),
        pa.field("source_entity_id", _STR, nullable=False),
        pa.field("target_entity_id", _STR, nullable=False),
        pa.field("evidence_id", _STR, nullable=True),
        pa.field("properties_json", _STR, nullable=True),
        pa.field("confidence", _FLOAT64, nullable=True),
        pa.field("is_placeholder", _BOOL, nullable=False),
        pa.field("content_hash", _STR, nullable=False),
        pa.field("created_at", _TS, nullable=False),
    ]
)

# ---------------------------------------------------------------------------
# 3.7  evidence
# ---------------------------------------------------------------------------

EVIDENCE_SCHEMA = pa.schema(
    [
        pa.field("evidence_id", _STR, nullable=False),
        pa.field("source_file_id", _STR, nullable=False),
        pa.field("source_type", _STR, nullable=False),
        pa.field("document_element_id", _STR, nullable=True),
        pa.field("chunk_id", _STR, nullable=True),
        pa.field("page_number", _INT32, nullable=True),
        pa.field("section_path", _STR, nullable=True),
        pa.field("table_id", _STR, nullable=True),
        pa.field("row_index", _INT32, nullable=True),
        pa.field("col_index", _INT32, nullable=True),
        pa.field("figure_id", _STR, nullable=True),
        pa.field("image_id", _STR, nullable=True),
        pa.field("callout_id", _STR, nullable=True),
        pa.field("visual_region_id", _STR, nullable=True),
        pa.field("blob_url", _STR, nullable=True),
        pa.field("text", _STR, nullable=True),
        pa.field("content_hash", _STR, nullable=False),
        pa.field("created_at", _TS, nullable=False),
    ]
)

# ---------------------------------------------------------------------------
# 3.8  visual_assets
# ---------------------------------------------------------------------------

VISUAL_ASSETS_SCHEMA = pa.schema(
    [
        pa.field("image_id", _STR, nullable=False),
        pa.field("source_file_id", _STR, nullable=False),
        pa.field("document_element_id", _STR, nullable=True),
        pa.field("asset_type", _STR, nullable=False),
        pa.field("page_number", _INT32, nullable=True),
        pa.field("section_path", _STR, nullable=True),
        pa.field("caption", _STR, nullable=True),
        pa.field("alt_text", _STR, nullable=True),
        pa.field("blob_url", _STR, nullable=True),
        pa.field("image_path", _STR, nullable=True),
        pa.field("image_hash", _STR, nullable=False),
        pa.field("width", _INT32, nullable=True),
        pa.field("height", _INT32, nullable=True),
        pa.field("description", _STR, nullable=True),
        pa.field("confidence", _FLOAT64, nullable=True),
        pa.field("is_placeholder", _BOOL, nullable=False),
        pa.field("created_at", _TS, nullable=False),
    ]
)

# ---------------------------------------------------------------------------
# 3.9  visual_regions
# ---------------------------------------------------------------------------

VISUAL_REGIONS_SCHEMA = pa.schema(
    [
        pa.field("visual_region_id", _STR, nullable=False),
        pa.field("image_id", _STR, nullable=False),
        pa.field("region_type", _STR, nullable=False),
        pa.field("label", _STR, nullable=True),
        pa.field("text", _STR, nullable=True),
        pa.field("polygon_json", _STR, nullable=True),
        pa.field("normalized_polygon_json", _STR, nullable=True),
        pa.field("identified_entity_id", _STR, nullable=True),
        pa.field("blob_url", _STR, nullable=True),
        pa.field("confidence", _FLOAT64, nullable=True),
        pa.field("created_at", _TS, nullable=False),
    ]
)

# ---------------------------------------------------------------------------
# Registry  — table-name → pyarrow schema
# ---------------------------------------------------------------------------

TABLE_SCHEMAS: dict[str, pa.Schema] = {
    "source_files": SOURCE_FILES_SCHEMA,
    "document_elements": DOCUMENT_ELEMENTS_SCHEMA,
    "chunks": CHUNKS_SCHEMA,
    "entities": ENTITIES_SCHEMA,
    "relationships": RELATIONSHIPS_SCHEMA,
    "evidence": EVIDENCE_SCHEMA,
    "visual_assets": VISUAL_ASSETS_SCHEMA,
    "visual_regions": VISUAL_REGIONS_SCHEMA,
}
