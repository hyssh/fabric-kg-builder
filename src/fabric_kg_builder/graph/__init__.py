"""Graph intelligence package — M5 GRP-001 through GRP-018.

Public API re-exports for convenient single-import access.
"""

from fabric_kg_builder.graph.blocking import block_candidates, CandidateBlock, InvalidEntityPropertiesError
from fabric_kg_builder.graph.claims import (
    ClaimExtractionResult,
    DeterministicClaimExtractor,
    extract_claims,
)
from fabric_kg_builder.graph.community import (
    CommunityHierarchyResult,
    InsufficientCorpusResult,
    build_community_hierarchy,
    HIERARCHY_VERSION,
    INSUFFICIENT_HIERARCHY_EVIDENCE,
)
from fabric_kg_builder.graph.dedup import (
    dedup_overlapping_occurrences,
    dedup_entity_occurrences,
    dedup_relationship_occurrences,
)
from fabric_kg_builder.graph.drawing_enricher import (
    DrawingValidationResult,
    observations_to_drawing_elements,
    enrich_spatial_containment,
    enrich_spatial_adjacency,
    enrich_located_at,
    enrich_topology,
    enrich_cross_sheet_references,
    enrich_revision_lineage,
    validate_drawing_graph,
)
from fabric_kg_builder.graph.extraction import (
    LLMExtractionClient,
    SubgraphExtractionRequest,
)
from fabric_kg_builder.graph.labeler import label_communities
from fabric_kg_builder.graph.metrics import (
    QualityMetrics,
    compute_quality_metrics,
    DomainEvaluationContract,
    EvaluationMetrics,
    GoldEntity,
    GoldRelationship,
    GoldClaim,
    evaluate_against_gold,
    SUPPLY_CHAIN_CONTRACT,
    LEGAL_CONTRACT,
    FACILITIES_CONTRACT,
)
from fabric_kg_builder.graph.occurrence import (
    EntityOccurrence,
    RelationshipOccurrence,
    SubgraphOccurrence,
    OccurrenceContext,
    EvidenceSpan,
    SUBGRAPH_SCHEMA_VERSION,
)
from fabric_kg_builder.graph.persistence import (
    GraphExtractionResult,
    MergedEntityRecord,
    MergedRelationshipRecord,
)
from fabric_kg_builder.graph.relationship import (
    MergedRelationship,
    merge_relationship_occurrences,
)
from fabric_kg_builder.graph.resolution import (
    ResolutionDecision,
    ScopeCompatibilityMap,
    resolve_candidates,
)
from fabric_kg_builder.graph.review import (
    ResolutionDecision as ReviewResolutionDecision,
    ReviewExport,
    ReplayResult,
    export_review,
    replay_decisions,
)
from fabric_kg_builder.graph.summarizer import (
    DeterministicSummarizer,
    LLMSummarizer,
    SummarizerProtocol,
    SummaryConsolidationResult,
    SummaryVerifier,
    consolidate_description,
    consolidate_description_typed,
)
from fabric_kg_builder.graph.validation import (
    GraphValidationResult,
    HierarchyValidationResult,
    validate_graph,
    validate_hierarchy,
    VAL_038,
)

__all__ = [
    # blocking
    "block_candidates",
    "CandidateBlock",
    "InvalidEntityPropertiesError",
    # claims
    "ClaimExtractionResult",
    "DeterministicClaimExtractor",
    "extract_claims",
    # community
    "CommunityHierarchyResult",
    "InsufficientCorpusResult",
    "build_community_hierarchy",
    "HIERARCHY_VERSION",
    "INSUFFICIENT_HIERARCHY_EVIDENCE",
    # dedup
    "dedup_overlapping_occurrences",
    "dedup_entity_occurrences",
    "dedup_relationship_occurrences",
    # drawing
    "DrawingValidationResult",
    "observations_to_drawing_elements",
    "enrich_spatial_containment",
    "enrich_spatial_adjacency",
    "enrich_located_at",
    "enrich_topology",
    "enrich_cross_sheet_references",
    "enrich_revision_lineage",
    "validate_drawing_graph",
    # extraction
    "LLMExtractionClient",
    "SubgraphExtractionRequest",
    # labeler
    "label_communities",
    # metrics
    "QualityMetrics",
    "compute_quality_metrics",
    "DomainEvaluationContract",
    "EvaluationMetrics",
    "GoldEntity",
    "GoldRelationship",
    "GoldClaim",
    "evaluate_against_gold",
    "SUPPLY_CHAIN_CONTRACT",
    "LEGAL_CONTRACT",
    "FACILITIES_CONTRACT",
    # occurrence
    "EntityOccurrence",
    "RelationshipOccurrence",
    "SubgraphOccurrence",
    "OccurrenceContext",
    "EvidenceSpan",
    "SUBGRAPH_SCHEMA_VERSION",
    # persistence
    "GraphExtractionResult",
    "MergedEntityRecord",
    "MergedRelationshipRecord",
    # relationship
    "MergedRelationship",
    "merge_relationship_occurrences",
    # resolution
    "ResolutionDecision",
    "ScopeCompatibilityMap",
    "resolve_candidates",
    # review
    "ReviewResolutionDecision",
    "ReviewExport",
    "ReplayResult",
    "export_review",
    "replay_decisions",
    # summarizer
    "DeterministicSummarizer",
    "LLMSummarizer",
    "SummarizerProtocol",
    "SummaryConsolidationResult",
    "SummaryVerifier",
    "consolidate_description",
    "consolidate_description_typed",
    # validation
    "GraphValidationResult",
    "HierarchyValidationResult",
    "validate_graph",
    "validate_hierarchy",
    "VAL_038",
]
