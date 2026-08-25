"""C0.Core shared contract foundation.

This package owns cross-layer primitives only. Domain, canonical row, lineage,
semantic, checkpoint, release, and runtime packages remain their existing
field-semantic authorities.
"""

from .assertions import (
    CanonicalEntityAssertion,
    CanonicalPropertyAssertion,
    CanonicalRelationshipAssertion,
)
from .adapters import (
    TrustedL1DesignEvidenceManifestContext,
    adapt_evidence_span_v1_0_to_v1_1,
    assert_domain_hash_authority,
    checkpoint_fingerprint_from_authority,
    identity_from_common_lineage,
    locator_from_authority,
    semantic_projection_header_ids,
)
from .base import (
    CONTRACT_VERSION,
    ContractError,
    ContractModel,
    EvidencePurposeAmbiguousError,
    EvidencePurposePromotionError,
    UnknownContractKindError,
    UnknownContractMajorError,
    canonical_json,
    canonical_sha256,
)
from .evidence import (
    EvidencePurpose,
    EvidenceSpan,
    EvidenceSpanV1_0,
    EvidenceSpanV1_1,
    SourceUnit,
)
from .extraction import (
    ExtractionAuthorityReferences,
    ExtractionCandidateBatch,
    ExtractionCandidateReference,
    RequiredMemberManifest,
    RequiredMemberManifestV1_1,
    RequiredMemberMigrationError,
    RequiredMemberOrderingPolicyV1_1,
    RequiredMemberReference,
    RequiredMemberReferenceV1_1,
    RequiredMemberSetProposal,
    RequiredMemberSetProposalV1_1,
    TrustedRequiredMemberPolicyContextV1_1,
    adapt_required_member_manifest_v1_0_to_v1_1,
    adapt_required_member_set_proposal_v1_0_to_v1_1,
    authoritative_collection_hash,
    authoritative_collection_hash_v1_1,
)
from .identity import CanonicalIdentityEnvelope, ImmutableSourceLocator
from .lifecycle import (
    AssertionState,
    CandidateAccountingDisposition,
    CandidateLifecycleRecord,
)
from .projection import (
    AuditProjection,
    SemanticServingProjection,
    validate_asserted_serving_subset,
)
from .receipts import (
    ArtifactEntry,
    ArtifactManifest,
    StageReceipt,
    validate_skip_preconditions,
)
from .registry import (
    REGISTERED_CONTRACTS,
    REGISTERED_CONTRACT_VERSIONS,
    SUPPORTED_VERSIONS,
    negotiate_contract,
    parse_contract,
    write_registered_schemas,
)
from .resources import StageResourceMetrics, validate_receipt_resources

__all__ = [
    "CONTRACT_VERSION",
    "AssertionState",
    "ArtifactEntry",
    "ArtifactManifest",
    "AuditProjection",
    "CandidateAccountingDisposition",
    "CandidateLifecycleRecord",
    "CanonicalEntityAssertion",
    "CanonicalIdentityEnvelope",
    "CanonicalPropertyAssertion",
    "CanonicalRelationshipAssertion",
    "ContractError",
    "ContractModel",
    "EvidencePurpose",
    "EvidencePurposeAmbiguousError",
    "EvidencePurposePromotionError",
    "EvidenceSpan",
    "EvidenceSpanV1_0",
    "EvidenceSpanV1_1",
    "ExtractionAuthorityReferences",
    "ExtractionCandidateBatch",
    "ExtractionCandidateReference",
    "ImmutableSourceLocator",
    "REGISTERED_CONTRACTS",
    "REGISTERED_CONTRACT_VERSIONS",
    "RequiredMemberManifest",
    "RequiredMemberManifestV1_1",
    "RequiredMemberMigrationError",
    "RequiredMemberOrderingPolicyV1_1",
    "RequiredMemberReference",
    "RequiredMemberReferenceV1_1",
    "RequiredMemberSetProposal",
    "RequiredMemberSetProposalV1_1",
    "SemanticServingProjection",
    "SourceUnit",
    "StageReceipt",
    "StageResourceMetrics",
    "SUPPORTED_VERSIONS",
    "TrustedL1DesignEvidenceManifestContext",
    "TrustedRequiredMemberPolicyContextV1_1",
    "UnknownContractKindError",
    "UnknownContractMajorError",
    "canonical_json",
    "canonical_sha256",
    "adapt_evidence_span_v1_0_to_v1_1",
    "adapt_required_member_manifest_v1_0_to_v1_1",
    "adapt_required_member_set_proposal_v1_0_to_v1_1",
    "authoritative_collection_hash",
    "authoritative_collection_hash_v1_1",
    "assert_domain_hash_authority",
    "checkpoint_fingerprint_from_authority",
    "identity_from_common_lineage",
    "locator_from_authority",
    "negotiate_contract",
    "parse_contract",
    "validate_asserted_serving_subset",
    "validate_receipt_resources",
    "validate_skip_preconditions",
    "write_registered_schemas",
    "semantic_projection_header_ids",
]
