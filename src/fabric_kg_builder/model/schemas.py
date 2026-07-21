"""Pydantic models for canonical Parquet tables and lineage v2 contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator


SCHEMA_VERSION_V2 = "2.0"
DEFAULT_PROJECT_ID = "default"


class CommonLineageRow(BaseModel):
    """Common lineage envelope applied to derived and serving records."""

    project_id: str = DEFAULT_PROJECT_ID
    asset_id: str = ""
    asset_version_id: str = ""
    run_id: str = ""
    parent_record_id: Optional[str] = None
    source_locator_json: Optional[str] = None
    schema_version: str = SCHEMA_VERSION_V2
    domain_hash: Optional[str] = None


class SourceFileRow(CommonLineageRow):
    """source_files table — legacy contract extended with lineage v2."""

    source_file_id: str
    path: str
    filename: str
    source_type: str
    content_hash: str
    byte_size: Optional[int] = None
    ingested_at: datetime
    schema_profile_path: Optional[str] = None
    row_count: Optional[int] = None
    notes: Optional[str] = None


class DocumentElementRow(CommonLineageRow):
    """document_elements table — legacy contract extended with lineage v2."""

    document_element_id: str
    source_file_id: str
    element_type: str
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


class ChunkRow(CommonLineageRow):
    """chunks table — legacy contract extended with lineage v2."""

    chunk_id: str
    source_file_id: str
    document_element_id: Optional[str] = None
    chunk_type: str
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
    entity_search_keys: Optional[list[str]] = None
    previous_chunk_id: Optional[str] = None
    next_chunk_id: Optional[str] = None
    token_count: Optional[int] = None
    token_start: Optional[int] = None
    token_end: Optional[int] = None
    overlap_token_count: Optional[int] = None
    chunk_strategy_version: Optional[str] = None
    content_hash: str
    created_at: datetime


class EntityRow(CommonLineageRow):
    """entities table — legacy contract extended with lineage v2."""

    entity_id: str
    entity_type: str
    display_name: str
    canonical_key: str
    aliases: Optional[list[str]] = None
    search_aliases: Optional[list[str]] = None
    description: Optional[str] = None
    properties_json: Optional[str] = None
    evidence_ids: Optional[list[str]] = None
    resolution_context_key: Optional[str] = None
    cannot_link_keys: Optional[list[str]] = None
    source_file_id: Optional[str] = None
    confidence: Optional[float] = None
    is_placeholder: bool = False
    content_hash: str
    created_at: datetime
    updated_at: datetime


class RelationshipRow(CommonLineageRow):
    """relationships table — legacy contract extended with lineage v2."""

    relationship_id: str
    relationship_type: str
    source_entity_id: str
    target_entity_id: str
    evidence_id: Optional[str] = None
    evidence_ids: Optional[list[str]] = None
    source_span_ids: Optional[list[str]] = None
    semantic_relationship_id: Optional[str] = None
    assertion_state: Optional[str] = None
    direction: Optional[str] = None
    relationship_category: Optional[str] = None
    review_status: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    temporal_precision: Optional[str] = None
    description: Optional[str] = None
    properties_json: Optional[str] = None
    confidence: Optional[float] = None
    is_placeholder: bool = False
    content_hash: str
    created_at: datetime


class PropertyObservationRow(CommonLineageRow):
    """Typed, evidence-backed observation of one canonical entity property."""

    observation_id: str
    entity_id: str
    entity_type_id: str
    property_id: str
    value_json: str
    value_type: str
    normalized_value_json: str
    unit: Optional[str] = None
    confidence: float
    assertion_state: str
    evidence_ids: list[str]
    source_span_ids: list[str] = Field(default_factory=list)
    observed_at: Optional[datetime] = None
    temporal_precision: str
    semantic_lane: str
    review_status: str
    conflict_id: Optional[str] = None
    content_hash: str
    created_at: datetime


class PropertyConflictRow(CommonLineageRow):
    """Conflict group retaining disagreeing property observations."""

    conflict_id: str
    entity_id: str
    property_id: str
    observation_ids: list[str]
    resolution_state: str = "needs_review"
    content_hash: str
    created_at: datetime


class EvidenceRow(CommonLineageRow):
    """evidence table — legacy contract extended with lineage v2."""

    evidence_id: str
    source_file_id: str
    source_type: str
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


class VisualAssetRow(CommonLineageRow):
    """visual_assets table — legacy contract extended with lineage v2."""

    image_id: str
    source_file_id: str
    document_element_id: Optional[str] = None
    asset_type: str
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


class VisualRegionRow(CommonLineageRow):
    """visual_regions table — legacy contract extended with lineage v2."""

    visual_region_id: str
    image_id: str
    region_type: str
    label: Optional[str] = None
    text: Optional[str] = None
    polygon_json: Optional[str] = None
    normalized_polygon_json: Optional[str] = None
    identified_entity_id: Optional[str] = None
    blob_url: Optional[str] = None
    confidence: Optional[float] = None
    created_at: datetime


class AssetRow(BaseModel):
    """assets table — immutable logical asset registration root."""

    asset_id: str
    project_id: str = DEFAULT_PROJECT_ID
    original_name: str
    media_type: str
    source_uri: str
    classification_json: Optional[str] = None
    created_at: datetime
    created_by: str


class AssetVersionRow(BaseModel):
    """asset_versions table — immutable observed version row."""

    asset_version_id: str
    asset_id: str
    version_identity: str
    content_hash: str
    size_bytes: int
    original_name: str
    media_type: str
    source_uri: str
    blob_uri: str
    blob_version_id: Optional[str] = None
    landing_path: str
    metadata_json: Optional[str] = None
    registered_at: datetime
    landing_timestamp: datetime
    ingestion_status: str


class ProcessingRunRow(BaseModel):
    """processing_runs table — pipeline execution manifest summary."""

    run_id: str
    environment: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str
    domain_hash: Optional[str] = None
    domain_schema_version: Optional[str] = None
    pipeline_version: str
    adapter_versions_json: Optional[str] = None
    prompt_versions_json: Optional[str] = None
    model_deployments_json: Optional[str] = None
    chunk_strategy_version: Optional[str] = None
    parent_run_id: Optional[str] = None
    stage_results_json: Optional[str] = None
    manifest_path: Optional[str] = None


_VALID_CLAIM_STATUSES = frozenset({"asserted", "retracted", "disputed", "uncertain"})
_VALID_REVIEW_STATES = frozenset({"not_reviewed", "approved", "rejected", "needs_review"})
_VALID_SUPPORT_TYPES = frozenset({"supports", "refutes", "context"})


class ClaimRow(CommonLineageRow):
    """claims table — reserved by schema v2 for later milestones."""

    claim_id: str
    subject_entity_id: str
    predicate: str
    object_entity_id: Optional[str] = None
    value_json: Optional[str] = None
    status: str
    confidence: Optional[float] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    observed_at: Optional[datetime] = None
    summary: Optional[str] = None
    review_state: Optional[str] = None

    @model_validator(mode="after")
    def _validate_claim(self) -> "ClaimRow":
        if self.status not in _VALID_CLAIM_STATUSES:
            raise ValueError(
                f"status {self.status!r} must be one of {sorted(_VALID_CLAIM_STATUSES)}"
            )
        if self.review_state is not None and self.review_state not in _VALID_REVIEW_STATES:
            raise ValueError(
                f"review_state {self.review_state!r} must be one of {sorted(_VALID_REVIEW_STATES)}"
            )
        if self.valid_from is not None and self.valid_to is not None:
            if self.valid_from >= self.valid_to:
                raise ValueError(
                    f"valid_from ({self.valid_from}) must be strictly before valid_to ({self.valid_to})"
                )
        return self


class ClaimEvidenceRow(BaseModel):
    """claim_evidence table — reserved by schema v2 for later milestones."""

    claim_id: str
    evidence_id: str
    occurrence_id: Optional[str] = None
    support_type: str
    confidence: Optional[float] = None

    @model_validator(mode="after")
    def _validate_support_type(self) -> "ClaimEvidenceRow":
        if self.support_type not in _VALID_SUPPORT_TYPES:
            raise ValueError(
                f"support_type {self.support_type!r} must be one of {sorted(_VALID_SUPPORT_TYPES)}"
            )
        return self


class ClusterRow(CommonLineageRow):
    """clusters table — reserved by schema v2 for later milestones."""

    cluster_id: str
    hierarchy_version: str
    level: int
    parent_cluster_id: Optional[str] = None
    label: str
    description: Optional[str] = None
    method: str


class ClusterMembershipRow(BaseModel):
    """cluster_memberships table — reserved by schema v2 for later milestones."""

    cluster_id: str
    entity_id: Optional[str] = None
    relationship_id: Optional[str] = None
    claim_id: Optional[str] = None
    score: Optional[float] = None
    rationale: Optional[str] = None
    evidence_ids: Optional[list[str]] = None
    primary_membership: bool = False


class DeploymentRow(BaseModel):
    """deployments table — sink publication and target locator contract."""

    deployment_id: str
    run_id: str
    environment: str
    artifact_type: str
    artifact_version: Optional[str] = None
    target_resource_id: Optional[str] = None
    target_name: Optional[str] = None
    target_record_locator: Optional[str] = None
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str
    operation_id: Optional[str] = None
    error_code: Optional[str] = None
    record_ids_json: Optional[str] = None


TABLE_MODELS: dict[str, type[BaseModel]] = {
    "source_files": SourceFileRow,
    "document_elements": DocumentElementRow,
    "chunks": ChunkRow,
    "entities": EntityRow,
    "relationships": RelationshipRow,
    "property_observations": PropertyObservationRow,
    "property_conflicts": PropertyConflictRow,
    "evidence": EvidenceRow,
    "visual_assets": VisualAssetRow,
    "visual_regions": VisualRegionRow,
    "assets": AssetRow,
    "asset_versions": AssetVersionRow,
    "processing_runs": ProcessingRunRow,
    "claims": ClaimRow,
    "claim_evidence": ClaimEvidenceRow,
    "clusters": ClusterRow,
    "cluster_memberships": ClusterMembershipRow,
    "deployments": DeploymentRow,
}


# ---------------------------------------------------------------------------
# SPEC-006 §7.3: Drawing element and relationship tables (M5 graph intelligence)
# ---------------------------------------------------------------------------

_VALID_DRAWING_ELEMENT_TYPES = frozenset({
    "symbol", "callout", "dimension", "annotation", "connector", "zone", "room",
})
_VALID_REVIEW_STATE_VALUES = frozenset({"not_required", "needs_review", "reviewed"})
_VALID_PROVENANCE_ORIGINS = frozenset({"observed", "inferred"})
_VALID_DRAWING_RELATIONSHIP_TYPES = frozenset({
    "contains", "adjacent_to", "connects_to", "flows_to",
    "located_at", "references_sheet", "revision_of",
})


class DrawingElementRow(CommonLineageRow):
    """drawing_elements table — SPEC-006 §7.3 canonical drawing observation."""

    element_id: str
    source_file_id: str
    sheet_number: int
    element_type: str
    label: Optional[str] = None
    geometry_json: str
    method: str
    confidence: float
    review_state: str
    provenance_origin: str
    evidence_region_ids: Optional[list[str]] = None
    content_hash: str
    created_at: datetime

    @model_validator(mode="after")
    def _validate_drawing_element(self) -> "DrawingElementRow":
        if self.element_type not in _VALID_DRAWING_ELEMENT_TYPES:
            raise ValueError(
                f"element_type {self.element_type!r} must be one of {sorted(_VALID_DRAWING_ELEMENT_TYPES)}"
            )
        if self.review_state not in _VALID_REVIEW_STATE_VALUES:
            raise ValueError(
                f"review_state {self.review_state!r} must be one of {sorted(_VALID_REVIEW_STATE_VALUES)}"
            )
        if self.provenance_origin not in _VALID_PROVENANCE_ORIGINS:
            raise ValueError(
                f"provenance_origin {self.provenance_origin!r} must be one of {sorted(_VALID_PROVENANCE_ORIGINS)}"
            )
        return self


class DrawingRelationshipRow(CommonLineageRow):
    """drawing_relationships table — SPEC-006 §7.3 spatial/topology edges."""

    drawing_relationship_id: str
    relationship_type: str
    source_element_id: str
    target_element_id: str
    sheet_number: Optional[int] = None
    geometry_json: Optional[str] = None
    method: str
    confidence: float
    review_state: str
    provenance_origin: str
    evidence_region_ids: Optional[list[str]] = None
    content_hash: str
    created_at: datetime

    @model_validator(mode="after")
    def _validate_drawing_relationship(self) -> "DrawingRelationshipRow":
        if self.relationship_type not in _VALID_DRAWING_RELATIONSHIP_TYPES:
            raise ValueError(
                f"relationship_type {self.relationship_type!r} must be one of "
                f"{sorted(_VALID_DRAWING_RELATIONSHIP_TYPES)}"
            )
        if self.review_state not in _VALID_REVIEW_STATE_VALUES:
            raise ValueError(
                f"review_state {self.review_state!r} must be one of {sorted(_VALID_REVIEW_STATE_VALUES)}"
            )
        if self.provenance_origin not in _VALID_PROVENANCE_ORIGINS:
            raise ValueError(
                f"provenance_origin {self.provenance_origin!r} must be one of {sorted(_VALID_PROVENANCE_ORIGINS)}"
            )
        return self


DRAWING_TABLE_MODELS: dict[str, type[BaseModel]] = {
    "drawing_elements": DrawingElementRow,
    "drawing_relationships": DrawingRelationshipRow,
}
