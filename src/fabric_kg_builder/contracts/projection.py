"""Audit and asserted-only semantic serving projection headers."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Annotated, Any, Iterable, Literal, Mapping

from pydantic import Field, field_validator, model_validator

from .base import (
    ContractModel,
    RequiredText,
    Sha256,
    canonical_sha256,
    frozen_mapping,
    sorted_unique,
    utc_timestamp,
)
from .identity import CanonicalIdentityEnvelope
from .lifecycle import AssertionState, CandidateAccountingDisposition

NonNegativeInt = Annotated[int, Field(ge=0)]


def _sorted_ids(value: object, field_name: str) -> object:
    if isinstance(value, (list, tuple)):
        return sorted_unique(value, field_name=field_name)
    return value


def canonical_disposition_order(value: Iterable[Any]) -> tuple[Any, ...]:
    """Order candidate dispositions canonically.

    ``input_candidate_id`` is minted per extraction batch, so two batches that
    propose identical raw text share one and it is not a total order on its own.
    The retained and deduplicated candidate ids complete it.

    Producers must order dispositions through this function *before* deriving
    ``projection_hash``. ``AuditProjection`` re-derives that hash from its own
    validated - and therefore reordered - fields, so a producer that hashes some
    other order disagrees with the contract it is about to construct.
    """

    def key(item: Any) -> tuple[str, str, str]:
        if isinstance(item, Mapping):
            get = item.get
        else:
            def get(name: str, default: Any = None) -> Any:
                return getattr(item, name, default)
        return (
            str(get("input_candidate_id", "") or ""),
            str(get("retained_candidate_id", "") or ""),
            str(get("deduplicated_into_candidate_id", "") or ""),
        )

    return tuple(sorted(value, key=key))


class AuditProjection(ContractModel):
    identity: CanonicalIdentityEnvelope
    projection_id: RequiredText
    projection_version: Literal["1.0"] = "1.0"
    source_manifest_hash: Sha256
    input_candidate_count: NonNegativeInt
    retained_candidate_count: NonNegativeInt
    deduplicated_input_count: NonNegativeInt
    candidate_dispositions: tuple[CandidateAccountingDisposition, ...]
    lifecycle_state_counts: Mapping[AssertionState, NonNegativeInt]
    reason_code_counts: Mapping[str, NonNegativeInt]
    entity_assertion_ids: tuple[str, ...] = ()
    relationship_assertion_ids: tuple[str, ...] = ()
    property_assertion_ids: tuple[str, ...] = ()
    canonical_id_set_hashes: Mapping[str, Sha256]
    canonical_row_hashes: Mapping[str, Sha256]
    artifact_manifest_id: RequiredText
    projection_hash: Sha256

    @field_validator(
        "entity_assertion_ids",
        "relationship_assertion_ids",
        "property_assertion_ids",
        mode="before",
    )
    @classmethod
    def _ids(cls, value: object, info: Any) -> object:
        return _sorted_ids(value, info.field_name)

    @field_validator("candidate_dispositions", mode="before")
    @classmethod
    def _dispositions(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return canonical_disposition_order(value)
        return value

    @field_validator(
        "lifecycle_state_counts",
        "reason_code_counts",
        "canonical_id_set_hashes",
        "canonical_row_hashes",
        mode="after",
    )
    @classmethod
    def _freeze_mappings(cls, value: Mapping[Any, Any]) -> Mapping[Any, Any]:
        return frozen_mapping(value)

    @model_validator(mode="after")
    def _accounting(self) -> "AuditProjection":
        if self.identity.contract_kind != "c0.audit_projection":
            raise ValueError("invalid audit projection identity contract_kind")
        if self.input_candidate_count != (
            self.retained_candidate_count + self.deduplicated_input_count
        ):
            raise ValueError("candidate accounting partition does not reconcile")
        if len(self.candidate_dispositions) != self.input_candidate_count:
            raise ValueError("every input candidate requires exactly one disposition")
        input_ids = [
            (
                item.input_candidate_id,
                item.retained_candidate_id,
                item.deduplicated_into_candidate_id,
            )
            for item in self.candidate_dispositions
        ]
        if len(set(input_ids)) != len(input_ids):
            raise ValueError("input candidate dispositions must be unique")
        retained = [
            item for item in self.candidate_dispositions
            if item.disposition == "retained"
        ]
        deduplicated = [
            item for item in self.candidate_dispositions
            if item.disposition == "deduplicated"
        ]
        if len(retained) != self.retained_candidate_count:
            raise ValueError("retained disposition count mismatch")
        if len(deduplicated) != self.deduplicated_input_count:
            raise ValueError("deduplicated disposition count mismatch")
        retained_ids = {item.retained_candidate_id for item in retained}
        if len(retained_ids) != len(retained):
            raise ValueError("each retained candidate must appear exactly once")
        if any(
            item.deduplicated_into_candidate_id not in retained_ids
            for item in deduplicated
        ):
            raise ValueError("every deduplicated input must map to one retained candidate")
        actual_states = Counter(item.current_state for item in retained)
        declared_states = {
            state: count for state, count in self.lifecycle_state_counts.items()
            if count
        }
        if actual_states != declared_states:
            raise ValueError("retained lifecycle states do not reconcile")
        if sum(self.lifecycle_state_counts.values()) != self.retained_candidate_count:
            raise ValueError("lifecycle state counts must partition retained candidates")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"projection_hash"})
        )
        if self.projection_hash != expected:
            raise ValueError("projection_hash does not match audit projection")
        return self


class SemanticServingProjection(ContractModel):
    identity: CanonicalIdentityEnvelope
    projection_id: RequiredText
    projection_version: Literal["1.0"] = "1.0"
    audit_projection_id: RequiredText
    source_manifest_hash: Sha256
    sealed_domain_contract_hash: Sha256
    sealed_semantic_contract_hash: Sha256
    included_states: tuple[Literal[AssertionState.ASSERTED], ...] = (
        AssertionState.ASSERTED,
    )
    entity_assertion_ids: tuple[str, ...] = ()
    relationship_assertion_ids: tuple[str, ...] = ()
    property_assertion_ids: tuple[str, ...] = ()
    evidence_span_ids: tuple[str, ...] = ()
    canonical_id_set_hashes: Mapping[str, Sha256]
    canonical_row_hashes: Mapping[str, Sha256]
    artifact_manifest_id: RequiredText
    sealed_at_utc: datetime
    projection_hash: Sha256

    _utc = field_validator("sealed_at_utc")(utc_timestamp)

    @field_validator(
        "entity_assertion_ids",
        "relationship_assertion_ids",
        "property_assertion_ids",
        "evidence_span_ids",
        mode="before",
    )
    @classmethod
    def _ids(cls, value: object, info: Any) -> object:
        return _sorted_ids(value, info.field_name)

    @field_validator(
        "canonical_id_set_hashes",
        "canonical_row_hashes",
        mode="after",
    )
    @classmethod
    def _freeze_mappings(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return frozen_mapping(value)

    @model_validator(mode="after")
    def _invariants(self) -> "SemanticServingProjection":
        if self.identity.contract_kind != "c0.semantic_serving_projection":
            raise ValueError("invalid serving projection identity contract_kind")
        if self.included_states != (AssertionState.ASSERTED,):
            raise ValueError("serving projection membership is asserted-only")
        if self.identity.domain_contract_hash != self.sealed_domain_contract_hash:
            raise ValueError("sealed domain hash must equal identity domain hash")
        if self.identity.semantic_contract_hash != self.sealed_semantic_contract_hash:
            raise ValueError("sealed semantic hash must equal identity semantic hash")
        expected = canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"projection_hash", "sealed_at_utc"},
            )
        )
        if self.projection_hash != expected:
            raise ValueError("projection_hash does not match serving projection")
        return self


def validate_asserted_serving_subset(
    audit: AuditProjection,
    serving: SemanticServingProjection,
    *,
    asserted_entity_ids: set[str],
    asserted_relationship_ids: set[str],
    asserted_property_ids: set[str],
) -> None:
    """Prove serving membership is exactly the asserted audit subset."""
    if serving.audit_projection_id != audit.projection_id:
        raise ValueError("serving projection references a different audit projection")
    if serving.source_manifest_hash != audit.source_manifest_hash:
        raise ValueError("serving and audit source manifest hashes differ")
    audit_identity = audit.identity.model_dump(
        mode="json",
        exclude={"contract_kind"},
    )
    serving_identity = serving.identity.model_dump(
        mode="json",
        exclude={"contract_kind"},
    )
    if serving_identity != audit_identity:
        raise ValueError("serving and audit lineage identities differ")
    if serving.artifact_manifest_id != audit.artifact_manifest_id:
        raise ValueError("serving and audit artifact manifest IDs differ")
    checks = (
        (
            "entity",
            set(audit.entity_assertion_ids),
            set(serving.entity_assertion_ids),
            asserted_entity_ids,
        ),
        (
            "relationship",
            set(audit.relationship_assertion_ids),
            set(serving.relationship_assertion_ids),
            asserted_relationship_ids,
        ),
        (
            "property",
            set(audit.property_assertion_ids),
            set(serving.property_assertion_ids),
            asserted_property_ids,
        ),
    )
    for kind, audit_ids, serving_ids, asserted_ids in checks:
        if not asserted_ids.issubset(audit_ids):
            raise ValueError(f"asserted {kind} IDs are absent from audit projection")
        if serving_ids != asserted_ids:
            raise ValueError(
                f"serving {kind} membership must equal the exact asserted subset"
            )
    if all(audit_ids == serving_ids for _, audit_ids, serving_ids, _ in checks):
        if serving.canonical_id_set_hashes != audit.canonical_id_set_hashes:
            raise ValueError("equal projection membership requires equal ID-set hashes")
        if serving.canonical_row_hashes != audit.canonical_row_hashes:
            raise ValueError("equal projection membership requires equal row hashes")
