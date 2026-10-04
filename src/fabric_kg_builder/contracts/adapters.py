"""Explicit equality adapters to existing repository authorities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field, field_validator

from .assertions import (
    CanonicalEntityAssertion,
    CanonicalPropertyAssertion,
    CanonicalRelationshipAssertion,
)
from .base import (
    CONTRACT_VERSION,
    ContractModel,
    EvidencePurposeAmbiguousError,
    EvidencePurposePromotionError,
    RequiredText,
    Sha256,
    sorted_unique,
)
from .evidence import EvidencePurpose, EvidenceSpanV1_0, EvidenceSpanV1_1, SourceUnit
from .identity import CanonicalIdentityEnvelope, ImmutableSourceLocator

if TYPE_CHECKING:
    from fabric_kg_builder.domain.models import AnyDomainContract
    from fabric_kg_builder.model.schemas import (
        CommonLineageRow,
        EntityRow,
        PropertyObservationRow,
        RelationshipRow,
    )

DESIGN_VERIFIER_NAME = "fabric-kg.local-evidence-verifier/domain_design"


class TrustedL1DesignEvidenceManifestContext(ContractModel):
    """Explicit trust boundary for an already validated L1 design manifest."""

    manifest_contract_kind: Literal["l1.design_sample_manifest"]
    manifest_contract_version: Literal["1.0.0"]
    design_sample_manifest_id: RequiredText
    design_sample_manifest_hash: Sha256
    evidence_span_ids: Annotated[tuple[RequiredText, ...], Field(min_length=1)]

    @field_validator("evidence_span_ids", mode="before")
    @classmethod
    def _evidence_ids(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name="evidence_span_ids")
        return value


def adapt_evidence_span_v1_0_to_v1_1(
    span: EvidenceSpanV1_0,
    *,
    source_unit: SourceUnit,
    trusted_manifest: TrustedL1DesignEvidenceManifestContext,
    purpose: EvidencePurpose,
    verifier_purpose_version: str,
) -> EvidenceSpanV1_1:
    """Promote only intact, manifest-listed L1 design evidence."""
    if span.identity.contract_version != "1.0.0":
        raise ValueError("evidence adapter input must be EvidenceSpan 1.0.0")
    if purpose != "domain_design":
        raise EvidencePurposePromotionError(
            "EvidenceSpan 1.0 can only adapt to domain_design"
        )
    if span.evidence_span_id not in trusted_manifest.evidence_span_ids:
        raise EvidencePurposeAmbiguousError(
            "legacy span is not listed by the trusted L1 design manifest"
        )
    if span.verifier_name != DESIGN_VERIFIER_NAME:
        raise EvidencePurposeAmbiguousError(
            "legacy verifier_name does not unambiguously prove domain_design"
        )
    span.verify_against(source_unit)
    adapted = EvidenceSpanV1_1.mint_verified(
        source_unit=source_unit,
        span_start=span.span_start,
        span_end=span.span_end,
        verifier_name=span.verifier_name,
        verifier_version=span.verifier_version,
        purpose="domain_design",
        verifier_purpose_version=verifier_purpose_version,
        verified_at_utc=span.verified_at_utc,
    )
    return adapted


def assert_domain_hash_authority(
    contract: "AnyDomainContract",
    expected_domain_contract_hash: str,
) -> None:
    """Prove the C0 identity hash equals ``domain.service`` authority."""
    from fabric_kg_builder.domain.service import compute_contract_hash

    actual = compute_contract_hash(contract)
    if actual != expected_domain_contract_hash:
        raise ValueError(
            "domain_contract_hash does not equal domain.service.compute_contract_hash"
        )


def identity_from_common_lineage(
    row: "CommonLineageRow",
    *,
    contract_kind: str,
    domain_schema_version: Literal["1.0", "2.0"],
    canonical_schema_version: str,
    content_hash: str | None,
    source_file_id: str | None = None,
    source_unit_id: str | None = None,
    semantic_contract_hash: str | None = None,
) -> CanonicalIdentityEnvelope:
    """Preserve CommonLineageRow fields and map domain_hash by equality."""
    return CanonicalIdentityEnvelope.from_common_lineage(
        row,
        contract_kind=contract_kind,
        contract_version=CONTRACT_VERSION,
        domain_schema_version=domain_schema_version,
        canonical_schema_version=canonical_schema_version,
        content_hash=content_hash,
        source_file_id=source_file_id,
        source_unit_id=source_unit_id,
        semantic_contract_hash=semantic_contract_hash,
    )


def locator_from_authority(**kwargs: Any) -> ImmutableSourceLocator:
    """Adapt and seal the exact ``build_source_locator`` output vocabulary."""
    return ImmutableSourceLocator.from_authority(**kwargs)


def checkpoint_fingerprint_from_authority(
    *,
    content_hash: str,
    adapter_name: str,
    adapter_version: str,
    options: dict[str, Any] | None = None,
) -> str:
    """Use the existing checkpoint primitive without defining a second seed."""
    from fabric_kg_builder.sources.checkpoint import compute_checkpoint_fingerprint

    return compute_checkpoint_fingerprint(
        content_hash,
        adapter_name,
        adapter_version,
        options,
    )


def entity_assertion_from_row(
    row: "EntityRow",
    **kwargs: Any,
) -> CanonicalEntityAssertion:
    """Preserve the canonical row's entity ID, key, and content hash."""
    return CanonicalEntityAssertion.from_row(row, **kwargs)


def relationship_assertion_from_row(
    row: "RelationshipRow",
    **kwargs: Any,
) -> CanonicalRelationshipAssertion:
    """Preserve the canonical row's relationship/endpoints/content hash."""
    return CanonicalRelationshipAssertion.from_row(row, **kwargs)


def property_assertion_from_row(
    row: "PropertyObservationRow",
    **kwargs: Any,
) -> CanonicalPropertyAssertion:
    """Preserve observation/property/value/content identity from the row."""
    return CanonicalPropertyAssertion.from_row(row, **kwargs)


def semantic_projection_header_ids(
    projection: dict[str, list[dict[str, Any]]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Read IDs from the existing semantic projection without minting new IDs."""
    entity_ids = tuple(
        sorted(
            {
                str(row["entity_id"])
                for row in projection.get("semantic_entities", [])
                if row.get("entity_id")
            }
        )
    )
    relationship_ids = tuple(
        sorted(
            {
                str(row["relationship_id"])
                for row in projection.get("semantic_relationships", [])
                if row.get("relationship_id")
                and row.get("assertion_status") == "asserted"
            }
        )
    )
    return entity_ids, relationship_ids
