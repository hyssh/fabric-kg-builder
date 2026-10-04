"""knowledge — AGK knowledge base and Fabric Data Agent package.

Public re-exports for the most commonly used types and functions across
the AGK-001 through AGK-008 milestones.

Modules
-------
transport   AGK-001  Typed HTTP transport + FakeTransport for tests.
models      AGK-002  Capability result models + API-version selection.
search_kb   AGK-003  Idempotent CRUD for Search knowledge sources/bases.
retrieve    AGK-004  Knowledge base retrieval + citation normalisation.
data_agent  AGK-005  Fabric Data Agent lifecycle + LRO handling.
validation  AGK-006  Source validation + five-source cap enforcement.
routing     AGK-007  Domain-based routing instruction generator.
competency  AGK-008  Competency suite runner.
"""

from __future__ import annotations

from .competency import CompetencyCase, CompetencyResult, CompetencySuiteRunner, summarise_results
from .data_agent import (
    DataAgentDefinitionError,
    DataAgentSpec,
    DataAgentPublishResult,
    DataAgentStageSnapshot,
    DataAgentTargetError,
    DataAgentUpsertResult,
    DataSourceElement,
    DataSourceSpec,
    FabricDataAgentClient,
    FewShotExample,
    LROTimeoutError,
    UnsupportedDataSourceType,
    build_definition_parts,
    decode_stage_snapshot,
    stage_snapshot_from_spec,
    ELEMENT_TYPE_EDGE,
    ELEMENT_TYPE_NODE,
    ELEMENT_TYPE_PROPERTY,
)
from .agent_validation import (
    AgentPublicationError,
    PersistedAgentGrounding,
    build_agent_publication_receipt,
    build_persisted_agent_grounding,
    deploy_and_validate_data_agent,
)
from .models import (
    AgentFeature,
    CapabilityResult,
    FeatureNotAvailable,
    PreviewNotAcknowledged,
    PREVIEW_COMPLIANCE_NOTICE,
    SearchAuth,
    discover_capabilities,
)
from .retrieve import (
    Citation,
    KnowledgeBaseRetriever,
    LineageCallbackError,
    PartialRetrievalError,
    RetrievalResult,
)
from .routing import (
    RouteCategory,
    RoutingResult,
    classify_question,
    generate_routing_instructions,
    routing_hints_for_question,
)
from .search_kb import (
    FabricDataAgentKnowledgeSourceSpec,
    FabricOntologyKnowledgeSourceSpec,
    KnowledgeBaseSpec,
    KnowledgeSourceSpec,
    RemoteKnowledgeSourceSpec,
    SearchIndexKnowledgeSourceSpec,
    SearchKbClient,
    UpsertResult,
)
from .transport import (
    FakeTransport,
    HttpError,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    RequestsTransport,
)
from .validation import (
    MAX_SOURCES,
    DuplicateSourceNameError,
    InvalidSourceError,
    SourceCapError,
    SourceSpec,
    SourceTypeUnavailable,
    ValidationError,
    validate_sources,
)

__all__ = [
    # transport
    "FakeTransport",
    "HttpError",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "RequestsTransport",
    # models
    "AgentFeature",
    "CapabilityResult",
    "FeatureNotAvailable",
    "PreviewNotAcknowledged",
    "PREVIEW_COMPLIANCE_NOTICE",
    "SearchAuth",
    "discover_capabilities",
    # search_kb
    "FabricDataAgentKnowledgeSourceSpec",
    "FabricOntologyKnowledgeSourceSpec",
    "KnowledgeBaseSpec",
    "KnowledgeSourceSpec",
    "RemoteKnowledgeSourceSpec",
    "SearchIndexKnowledgeSourceSpec",
    "SearchKbClient",
    "UpsertResult",
    # retrieve
    "Citation",
    "KnowledgeBaseRetriever",
    "LineageCallbackError",
    "PartialRetrievalError",
    "RetrievalResult",
    # data_agent
    "DataAgentSpec",
    "DataAgentDefinitionError",
    "DataAgentPublishResult",
    "DataAgentStageSnapshot",
    "DataAgentTargetError",
    "DataAgentUpsertResult",
    "DataSourceElement",
    "DataSourceSpec",
    "FabricDataAgentClient",
    "FewShotExample",
    "LROTimeoutError",
    "UnsupportedDataSourceType",
    "build_definition_parts",
    "decode_stage_snapshot",
    "stage_snapshot_from_spec",
    "ELEMENT_TYPE_EDGE",
    "ELEMENT_TYPE_NODE",
    "ELEMENT_TYPE_PROPERTY",
    "AgentPublicationError",
    "PersistedAgentGrounding",
    "build_agent_publication_receipt",
    "build_persisted_agent_grounding",
    "deploy_and_validate_data_agent",
    # validation
    "MAX_SOURCES",
    "DuplicateSourceNameError",
    "InvalidSourceError",
    "SourceCapError",
    "SourceSpec",
    "SourceTypeUnavailable",
    "ValidationError",
    "validate_sources",
    # routing
    "RouteCategory",
    "RoutingResult",
    "classify_question",
    "generate_routing_instructions",
    "routing_hints_for_question",
    # competency
    "CompetencyCase",
    "CompetencyResult",
    "CompetencySuiteRunner",
    "summarise_results",
]
