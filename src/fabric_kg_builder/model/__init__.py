"""Canonical Pydantic data models for the 8 Parquet table schemas.

Tables: source_files, document_elements, chunks, entities,
relationships, evidence, visual_assets, visual_regions.
All row IDs are content-addressed SHA-256 hashes (deterministic).
"""

from .schemas import (
    TABLE_MODELS,
    ChunkRow,
    DocumentElementRow,
    EntityRow,
    EvidenceRow,
    RelationshipRow,
    SourceFileRow,
    VisualAssetRow,
    VisualRegionRow,
)
from .arrow_schemas import TABLE_SCHEMAS
from .ids import (
    content_hash,
    make_chunk_id,
    make_document_element_id,
    make_entity_id,
    make_evidence_id,
    make_id,
    make_image_id,
    make_relationship_id,
    make_source_file_id,
    make_visual_region_id,
    normalize_canonical_key,
)

__all__ = [
    # pydantic models
    "SourceFileRow",
    "DocumentElementRow",
    "ChunkRow",
    "EntityRow",
    "RelationshipRow",
    "EvidenceRow",
    "VisualAssetRow",
    "VisualRegionRow",
    "TABLE_MODELS",
    # arrow schemas
    "TABLE_SCHEMAS",
    # ID helpers
    "make_id",
    "content_hash",
    "normalize_canonical_key",
    "make_entity_id",
    "make_source_file_id",
    "make_document_element_id",
    "make_chunk_id",
    "make_relationship_id",
    "make_evidence_id",
    "make_image_id",
    "make_visual_region_id",
]
