"""Non-asserting carriers for observed collections that cannot enter C0 1.1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Sequence

from pydantic import Field, model_validator

from fabric_kg_builder.contracts.base import (
    ContractModel,
    RequiredText,
    Sha256,
    canonical_json,
    canonical_sha256,
    deterministic_contract_id,
)
from fabric_kg_builder.contracts.extraction import ExtractionAuthorityReferences

if TYPE_CHECKING:
    from .schema2_extraction import CollectionMemberFragment, ExtractionLeafResult

COLLECTION_PARTITION_VERSION = "l2-collection-partition/1.0.1"
COLLECTION_DEFERRAL_KIND = "l2.collection_deferral"
COLLECTION_DEFERRAL_VERSION = "1.0.0"


class CollectionObservation(ContractModel):
    requirement_id: RequiredText
    aggregate_entity_id: RequiredText
    member_entity_id: RequiredText
    member_candidate_id: RequiredText
    member_semantic_type_id: RequiredText
    member_role_id: RequiredText | None
    member_order: int | None = Field(ge=0)
    membership_relationship_candidate_id: RequiredText
    source_unit_id: RequiredText


def observed_order_reasons(orders: list[int | None]) -> tuple[str, ...]:
    reasons = []
    if any(order is None for order in orders):
        reasons.append("ORDER_POSITIONS_MISSING")
    present = [order for order in orders if order is not None]
    if len(present) != len(set(present)):
        reasons.append("ORDER_POSITIONS_DUPLICATE")
    if sorted(present) != list(range(len(orders))):
        reasons.append("ORDER_POSITIONS_NOT_ZERO_BASED_CONTIGUOUS")
    return tuple(sorted(reasons))


class CollectionDeferral(ContractModel):
    """Lossless observation inventory, never a RequiredMemberSetProposal."""

    contract_kind: Literal["l2.collection_deferral"] = COLLECTION_DEFERRAL_KIND
    contract_version: Literal["1.0.0"] = COLLECTION_DEFERRAL_VERSION
    collection_deferral_id: RequiredText
    authority: ExtractionAuthorityReferences
    scope_canonical_id: RequiredText
    candidate_batch_hashes: tuple[tuple[RequiredText, Sha256], ...]
    observations: tuple[CollectionObservation, ...] = Field(min_length=1)
    status: Literal["review_required"] = "review_required"
    reason_codes: tuple[str, ...]
    deferral_hash: Sha256

    @model_validator(mode="after")
    def _binding(self) -> "CollectionDeferral":
        values = self.model_dump(mode="json", exclude={"deferral_hash"})
        expected_id = deterministic_contract_id(
            "collection-deferral",
            {key: value for key, value in values.items() if key != "collection_deferral_id"},
        )
        if self.collection_deferral_id != expected_id or self.deferral_hash != canonical_sha256(values):
            raise ValueError("collection deferral ID/hash does not recompute")
        if tuple(sorted(self.candidate_batch_hashes)) != self.candidate_batch_hashes:
            raise ValueError("collection deferral batch bindings must be canonical")
        if len({item[0] for item in self.candidate_batch_hashes}) != len(self.candidate_batch_hashes):
            raise ValueError("collection deferral batch bindings must be unique")
        if not self.candidate_batch_hashes:
            raise ValueError("collection deferral requires candidate batch bindings")
        if tuple(sorted(self.observations, key=lambda item: canonical_json(item.model_dump(mode="json")))) != self.observations:
            raise ValueError("collection observations must be canonical")
        by_member: dict[str, CollectionObservation] = {}
        for item in self.observations:
            if (
                item.requirement_id != self.authority.completeness_requirement_id
                or item.aggregate_entity_id != self.scope_canonical_id
            ):
                raise ValueError("collection deferral mixes requirements or scopes")
            prior = by_member.get(item.member_entity_id)
            if prior is not None and prior != item:
                raise ValueError("collection deferral cannot hide member identity conflicts")
            by_member[item.member_entity_id] = item
        reasons = observed_order_reasons([item.member_order for item in by_member.values()])
        if not reasons or self.reason_codes != reasons:
            raise ValueError("collection deferral requires exact observed-order diagnostics")
        return self

    @classmethod
    def seal(
        cls, *,
        authority: ExtractionAuthorityReferences,
        scope_canonical_id: str,
        leaves: Sequence[ExtractionLeafResult],
        members: Sequence[CollectionMemberFragment],
    ) -> "CollectionDeferral":
        values = {
            "contract_kind": COLLECTION_DEFERRAL_KIND,
            "contract_version": COLLECTION_DEFERRAL_VERSION,
            "authority": authority.model_dump(mode="json"),
            "scope_canonical_id": scope_canonical_id,
            "candidate_batch_hashes": sorted(
                (leaf.batch.extraction_candidate_batch_id, leaf.batch.batch_hash)
                for leaf in leaves
            ),
            "observations": sorted(
                [member.__dict__ for member in members], key=canonical_json,
            ),
            "status": "review_required",
            "reason_codes": observed_order_reasons(
                list({member.member_entity_id: member.member_order for member in members}.values())
            ),
        }
        values["collection_deferral_id"] = deterministic_contract_id("collection-deferral", values)
        values["deferral_hash"] = canonical_sha256(values)
        return cls.model_validate_json(canonical_json(values))


@dataclass(frozen=True)
class DeferredCollectionOutcome:
    collection_deferral_id: str
    deferral_hash: str
    scope_canonical_id: str
    requirement_id: str
    reason_codes: tuple[str, ...]
    observed_member_ids: tuple[str, ...]
    completeness_state: Literal["unresolved"] = "unresolved"
    readiness_state: Literal["blocked"] = "blocked"
    # Verified atomic assertions remain in their own lifecycle partitions.
    verified_member_ids: tuple[str, ...] = ()
    verified_member_count: int = 0
    role_coverage: tuple[tuple[str, int], ...] = ()


def deferred_collection_outcome(deferral: CollectionDeferral) -> DeferredCollectionOutcome:
    return DeferredCollectionOutcome(
        collection_deferral_id=deferral.collection_deferral_id,
        deferral_hash=deferral.deferral_hash,
        scope_canonical_id=deferral.scope_canonical_id,
        requirement_id=deferral.authority.completeness_requirement_id,
        reason_codes=tuple(sorted((*deferral.reason_codes, "COLLECTION_REVIEW_REQUIRED"))),
        observed_member_ids=tuple(sorted({item.member_entity_id for item in deferral.observations})),
    )
