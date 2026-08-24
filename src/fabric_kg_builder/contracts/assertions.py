"""Typed C0 references over existing canonical row authorities."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import field_validator, model_validator

from .base import (
    ContractModel,
    RequiredText,
    Sha256,
    canonical_sha256,
    sorted_unique,
    utc_timestamp,
)
from .identity import CanonicalIdentityEnvelope, ImmutableSourceLocator
from .lifecycle import AssertionState, assertion_state_from_authority

if TYPE_CHECKING:
    from fabric_kg_builder.model.schemas import (
        EntityRow,
        PropertyObservationRow,
        RelationshipRow,
    )

PublishedAssertionState = Literal[
    AssertionState.DISCOVERY,
    AssertionState.UNRESOLVED,
    AssertionState.REJECTED,
    AssertionState.UNSUPPORTED,
    AssertionState.ASSERTED,
]


def _assert_row_identity(row: Any, identity: CanonicalIdentityEnvelope) -> None:
    """Require equality for every lineage field repeated by a canonical row."""
    comparisons = {
        "project_id": row.project_id,
        "asset_id": row.asset_id or None,
        "asset_version_id": row.asset_version_id or None,
        "run_id": row.run_id,
        "canonical_schema_version": row.schema_version,
        "domain_contract_hash": row.domain_hash,
        "content_hash": row.content_hash,
    }
    for identity_field, row_value in comparisons.items():
        if getattr(identity, identity_field) != row_value:
            raise ValueError(
                f"canonical row {identity_field} does not equal assertion identity"
            )
    if row.parent_record_id and row.parent_record_id not in identity.parent_record_ids:
        raise ValueError(
            "canonical row parent_record_id is absent from assertion identity"
        )
    expected_locator = None
    if row.source_locator_json:
        locator_raw = json.loads(row.source_locator_json)
        locator_raw["locator_version"] = "1.0"
        locator_raw["locator_hash"] = canonical_sha256(locator_raw)
        expected_locator = ImmutableSourceLocator.model_validate(locator_raw)
    if identity.immutable_locator != expected_locator:
        raise ValueError(
            "canonical row source locator does not equal assertion identity"
        )
    metadata_value = getattr(row, "properties_json", None)
    if metadata_value:
        metadata = (
            json.loads(metadata_value)
            if isinstance(metadata_value, str)
            else metadata_value
        )
        row_semantic_hash = (
            metadata.get("semantic_contract_hash")
            if isinstance(metadata, dict)
            else None
        )
        if (
            row_semantic_hash is not None
            and identity.semantic_contract_hash != row_semantic_hash
        ):
            raise ValueError(
                "canonical row semantic contract hash does not equal assertion identity"
            )


class CanonicalEntityAssertion(ContractModel):
    identity: CanonicalIdentityEnvelope
    entity_id: RequiredText
    semantic_type_id: RequiredText
    canonical_key: RequiredText
    display_name: RequiredText
    aliases: tuple[str, ...] = ()
    description: str | None
    evidence_span_ids: tuple[str, ...] = ()
    governance_justification_id: RequiredText | None
    lifecycle_record_id: RequiredText
    assertion_state: PublishedAssertionState
    content_hash: Sha256

    @field_validator("aliases", "evidence_span_ids", mode="before")
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name=info.field_name)
        return value

    @model_validator(mode="after")
    def _kind(self) -> "CanonicalEntityAssertion":
        if self.identity.contract_kind != "c0.canonical_entity_assertion":
            raise ValueError("invalid entity assertion identity contract_kind")
        return self

    @classmethod
    def from_row(
        cls,
        row: EntityRow,
        *,
        identity: CanonicalIdentityEnvelope,
        semantic_type_id: str,
        evidence_span_ids: tuple[str, ...],
        lifecycle_record_id: str,
        assertion_state: str,
        governance_justification_id: str | None = None,
    ) -> "CanonicalEntityAssertion":
        _assert_row_identity(row, identity)
        if row.source_file_id and identity.source_file_id != row.source_file_id:
            raise ValueError(
                "entity row source_file_id does not equal assertion identity"
            )
        return cls(
            identity=identity,
            entity_id=row.entity_id,
            semantic_type_id=semantic_type_id,
            canonical_key=row.canonical_key,
            display_name=row.display_name,
            aliases=tuple(row.aliases or ()),
            description=row.description,
            evidence_span_ids=evidence_span_ids,
            governance_justification_id=governance_justification_id,
            lifecycle_record_id=lifecycle_record_id,
            assertion_state=assertion_state_from_authority(assertion_state),
            content_hash=row.content_hash,
        )


class CanonicalRelationshipAssertion(ContractModel):
    identity: CanonicalIdentityEnvelope
    relationship_id: RequiredText
    semantic_relationship_id: RequiredText
    source_entity_id: RequiredText
    target_entity_id: RequiredText
    direction: Literal["source_to_target"] = "source_to_target"
    evidence_span_ids: tuple[str, ...] = ()
    governance_justification_id: RequiredText | None
    lifecycle_record_id: RequiredText
    assertion_state: PublishedAssertionState
    valid_from: datetime | None
    valid_to: datetime | None
    content_hash: Sha256

    @field_validator("evidence_span_ids", mode="before")
    @classmethod
    def _evidence(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name="evidence_span_ids")
        return value

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return utc_timestamp(value) if value is not None else None

    @model_validator(mode="after")
    def _invariants(self) -> "CanonicalRelationshipAssertion":
        if self.identity.contract_kind != "c0.canonical_relationship_assertion":
            raise ValueError("invalid relationship assertion identity contract_kind")
        if self.valid_from and self.valid_to and self.valid_to < self.valid_from:
            raise ValueError("valid_to must not precede valid_from")
        return self

    @classmethod
    def from_row(
        cls,
        row: RelationshipRow,
        *,
        identity: CanonicalIdentityEnvelope,
        evidence_span_ids: tuple[str, ...],
        lifecycle_record_id: str,
        assertion_state: str | None = None,
        governance_justification_id: str | None = None,
    ) -> "CanonicalRelationshipAssertion":
        _assert_row_identity(row, identity)
        semantic_id = row.semantic_relationship_id
        state = assertion_state or row.assertion_state
        if not semantic_id or not state:
            raise ValueError("canonical relationship adapter requires semantic ID and state")
        return cls(
            identity=identity,
            relationship_id=row.relationship_id,
            semantic_relationship_id=semantic_id,
            source_entity_id=row.source_entity_id,
            target_entity_id=row.target_entity_id,
            evidence_span_ids=evidence_span_ids,
            governance_justification_id=governance_justification_id,
            lifecycle_record_id=lifecycle_record_id,
            assertion_state=assertion_state_from_authority(state),
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            content_hash=row.content_hash,
        )


class CanonicalPropertyAssertion(ContractModel):
    identity: CanonicalIdentityEnvelope
    property_assertion_id: RequiredText
    entity_id: RequiredText
    semantic_property_id: RequiredText
    value_json: RequiredText
    value_type: RequiredText
    normalized_value_json: RequiredText
    unit: str | None
    evidence_span_ids: tuple[str, ...] = ()
    lifecycle_record_id: RequiredText
    assertion_state: PublishedAssertionState
    observed_at: datetime | None
    content_hash: Sha256

    @field_validator("evidence_span_ids", mode="before")
    @classmethod
    def _evidence(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name="evidence_span_ids")
        return value

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return utc_timestamp(value) if value is not None else None

    @model_validator(mode="after")
    def _kind(self) -> "CanonicalPropertyAssertion":
        if self.identity.contract_kind != "c0.canonical_property_assertion":
            raise ValueError("invalid property assertion identity contract_kind")
        return self

    @classmethod
    def from_row(
        cls,
        row: PropertyObservationRow,
        *,
        identity: CanonicalIdentityEnvelope,
        evidence_span_ids: tuple[str, ...],
        lifecycle_record_id: str,
    ) -> "CanonicalPropertyAssertion":
        _assert_row_identity(row, identity)
        return cls(
            identity=identity,
            property_assertion_id=row.observation_id,
            entity_id=row.entity_id,
            semantic_property_id=row.property_id,
            value_json=row.value_json,
            value_type=row.value_type,
            normalized_value_json=row.normalized_value_json,
            unit=row.unit,
            evidence_span_ids=evidence_span_ids,
            lifecycle_record_id=lifecycle_record_id,
            assertion_state=assertion_state_from_authority(row.assertion_state),
            observed_at=row.observed_at,
            content_hash=row.content_hash,
        )
