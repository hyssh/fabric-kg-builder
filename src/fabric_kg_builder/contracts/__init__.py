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
    UnknownContractKindError,
    UnknownContractMajorError,
    canonical_json,
    canonical_sha256,
)
from .evidence import EvidenceSpan, SourceUnit
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
    "EvidenceSpan",
    "ImmutableSourceLocator",
    "REGISTERED_CONTRACTS",
    "SemanticServingProjection",
    "SourceUnit",
    "StageReceipt",
    "StageResourceMetrics",
    "UnknownContractKindError",
    "UnknownContractMajorError",
    "canonical_json",
    "canonical_sha256",
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
