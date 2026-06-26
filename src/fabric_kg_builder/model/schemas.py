"""Pydantic models for all 8 canonical Parquet tables.

Exactly mirrors the column definitions in SPEC-002 §3 including the
graph→search denormalised columns from §11 (search_aliases, entity_search_keys).

All timestamps are UTC; list columns use Python list[str].
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class SourceFileRow(BaseModel):
    """source_files table — SPEC-002 §3.2."""

    source_file_id: str
    path: str
    filename: str
    source_type: str  # csv | tsv | xlsx | pdf | docx | html | markdown | image | parquet
    content_hash: str
    byte_size: Optional[int] = None
    ingested_at: datetime
    schema_profile_path: Optional[str] = None
    row_count: Optional[int] = None
    notes: Optional[str] = None


class DocumentElementRow(BaseModel):
    """document_elements table — SPEC-002 §3.3."""

    document_element_id: str
    source_file_id: str
    element_type: str  # section | page | paragraph | table | table_row | …
    parent_element_id: Optional[str] = None
    title: Optional[str] = None
    content: Optional[str] = None
    content_html: Optional[str] = None
    blob_url: Optional[str] = None
    page_number: Optional[int] = None
    section_path: Optional[str] = None
    sort_order: Optional[int] = None
    row_index: Optional[int] = None
    col_index: Optional[int] = None
    content_hash: str
    extracted_at: datetime


class ChunkRow(BaseModel):
    """chunks table — SPEC-002 §3.4 (incl. §11 entity_search_keys)."""

    chunk_id: str
    source_file_id: str
    document_element_id: Optional[str] = None
    chunk_type: str  # section_text | procedure_step | table_html | …
    content: str
    content_html: Optional[str] = None
    embedding_text: Optional[str] = None
    blob_url: Optional[str] = None
    page_number: Optional[int] = None
    section_path: Optional[str] = None
    table_id: Optional[str] = None
    figure_id: Optional[str] = None
    image_id: Optional[str] = None
    related_entity_ids: Optional[list[str]] = None
    # §11 AI Search linkage — BM25 keyword/alias matching only (not filterable)
    entity_search_keys: Optional[list[str]] = None
    content_hash: str
    created_at: datetime


class EntityRow(BaseModel):
    """entities table — SPEC-002 §3.5 (incl. §11 search_aliases)."""

    entity_id: str
    entity_type: str
    display_name: str
    canonical_key: str
    aliases: Optional[list[str]] = None
    # §11 AI Search linkage — SEARCHABLE only, never filterable
    search_aliases: Optional[list[str]] = None
    description: Optional[str] = None
    properties_json: Optional[str] = None
    source_file_id: Optional[str] = None
    confidence: Optional[float] = None
    is_placeholder: bool = False
    content_hash: str
    created_at: datetime
    updated_at: datetime


class RelationshipRow(BaseModel):
    """relationships table — SPEC-002 §3.6."""

    relationship_id: str
    relationship_type: str
    source_entity_id: str
    target_entity_id: str
    evidence_id: Optional[str] = None
    properties_json: Optional[str] = None
    confidence: Optional[float] = None
    is_placeholder: bool = False
    content_hash: str
    created_at: datetime


class EvidenceRow(BaseModel):
    """evidence table — SPEC-002 §3.7."""

    evidence_id: str
    source_file_id: str
    source_type: str  # csv_row | document_span | table_cell | figure_callout | image_region | ocr_text | chunk
    document_element_id: Optional[str] = None
    chunk_id: Optional[str] = None
    page_number: Optional[int] = None
    section_path: Optional[str] = None
    table_id: Optional[str] = None
    row_index: Optional[int] = None
    col_index: Optional[int] = None
    figure_id: Optional[str] = None
    image_id: Optional[str] = None
    callout_id: Optional[str] = None
    visual_region_id: Optional[str] = None
    blob_url: Optional[str] = None
    text: Optional[str] = None
    content_hash: str
    created_at: datetime


class VisualAssetRow(BaseModel):
    """visual_assets table — SPEC-002 §3.8."""

    image_id: str
    source_file_id: str
    document_element_id: Optional[str] = None
    asset_type: str  # figure | inline_image | screenshot | diagram | photo | chart | table_image
    page_number: Optional[int] = None
    section_path: Optional[str] = None
    caption: Optional[str] = None
    alt_text: Optional[str] = None
    blob_url: Optional[str] = None
    image_path: Optional[str] = None
    image_hash: str
    width: Optional[int] = None
    height: Optional[int] = None
    description: Optional[str] = None
    confidence: Optional[float] = None
    is_placeholder: bool = False
    created_at: datetime


class VisualRegionRow(BaseModel):
    """visual_regions table — SPEC-002 §3.9."""

    visual_region_id: str
    image_id: str
    region_type: str  # callout | ocr_text | component_region | connector_region | warning_region | table_region | detected_label
    label: Optional[str] = None
    text: Optional[str] = None
    polygon_json: Optional[str] = None
    normalized_polygon_json: Optional[str] = None
    identified_entity_id: Optional[str] = None
    blob_url: Optional[str] = None
    confidence: Optional[float] = None
    created_at: datetime


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TABLE_MODELS: dict[str, type[BaseModel]] = {
    "source_files": SourceFileRow,
    "document_elements": DocumentElementRow,
    "chunks": ChunkRow,
    "entities": EntityRow,
    "relationships": RelationshipRow,
    "evidence": EvidenceRow,
    "visual_assets": VisualAssetRow,
    "visual_regions": VisualRegionRow,
}
