"""Strict cross-layer carriers for L2 extraction and L3 member sealing."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Iterable, Literal

from pydantic import Field, field_validator, model_validator

from .base import (
    ContractModel,
    RequiredText,
    Sha256,
    canonical_sha256,
    sorted_unique,
    utc_timestamp,
)
from .evidence import EvidenceSpan
from .identity import CanonicalIdentityEnvelope
from .lifecycle import CandidateAccountingDisposition, CandidateLifecycleRecord

NonNegativeInt = Annotated[int, Field(ge=0)]
EvidenceSpanId = Annotated[
    str,
    Field(pattern=r"^evidence-span:[0-9a-f]{32}$"),
]


class ExtractionCandidateReference(ContractModel):
    """Opaque references to one retained C0.Core candidate version."""

    candidate_id: RequiredText
    candidate_version_id: RequiredText
    candidate_kind: Literal["entity", "relationship", "property"]
    semantic_type_id: RequiredText
    lifecycle_record_id: RequiredText
    evidence_span_ids: tuple[EvidenceSpanId, ...] = ()

    @field_validator("evidence_span_ids", mode="before")
    @classmethod
    def _evidence_ids(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name="evidence_span_ids")
        return value


class RequiredMemberReference(ContractModel):
    """Domain-neutral member facts copied from sealed L1 authority."""

    member_canonical_id: RequiredText
    member_semantic_type_id: RequiredText
    member_role_id: RequiredText
    member_order: NonNegativeInt
    minimum_cardinality: NonNegativeInt
    maximum_cardinality: NonNegativeInt | None
    candidate_id: RequiredText
    supporting_evidence_span_ids: tuple[EvidenceSpanId, ...] = ()

    @field_validator("supporting_evidence_span_ids", mode="before")
    @classmethod
    def _evidence_ids(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(
                value,
                field_name="supporting_evidence_span_ids",
            )
        return value

    @model_validator(mode="after")
    def _cardinality_bounds(self) -> "RequiredMemberReference":
        if (
            self.maximum_cardinality is not None
            and self.maximum_cardinality < self.minimum_cardinality
        ):
            raise ValueError(
                "maximum_cardinality must be greater than or equal to minimum_cardinality"
            )
        return self


class ExtractionAuthorityReferences(ContractModel):
    """Hashes and IDs owned by L1 and source-manifest authorities."""

    source_corpus_manifest_id: RequiredText
    source_corpus_manifest_hash: Sha256
    source_unit_manifest_id: RequiredText
    source_unit_manifest_hash: Sha256
    domain_contract_hash: Sha256
    completeness_requirement_id: RequiredText
    completeness_requirement_hash: Sha256
    hierarchy_hash: Sha256
    identity_policy_hash: Sha256


_AUTHORITY_FIELDS = tuple(ExtractionAuthorityReferences.model_fields)


def _authority_from_carrier(
    carrier: ExtractionAuthorityReferences,
) -> ExtractionAuthorityReferences:
    return ExtractionAuthorityReferences(
        **{field: getattr(carrier, field) for field in _AUTHORITY_FIELDS}
    )


def _expand_authority(values: dict[str, Any]) -> None:
    authority = values.pop("authority", None)
    if authority is None:
        return
    if not isinstance(authority, ExtractionAuthorityReferences):
        authority = ExtractionAuthorityReferences.model_validate(authority)
    values.update(authority.model_dump(mode="python"))


def _validate_identity(
    identity: CanonicalIdentityEnvelope,
    *,
    expected_kind: str,
    domain_contract_hash: str,
) -> None:
    if identity.contract_kind != expected_kind:
        raise ValueError(f"invalid {expected_kind} identity contract_kind")
    if identity.domain_contract_hash != domain_contract_hash:
        raise ValueError("domain_contract_hash must equal identity authority")


def _collection_payload(
    *,
    authority: ExtractionAuthorityReferences,
    scope_canonical_id: str,
    membership_semantic_relationship_id: str,
    members: tuple[RequiredMemberReference, ...],
) -> dict[str, Any]:
    return {
        **authority.model_dump(mode="json"),
        "scope_canonical_id": scope_canonical_id,
        "membership_semantic_relationship_id": membership_semantic_relationship_id,
        "members": [member.model_dump(mode="json") for member in members],
    }


def authoritative_collection_hash(
    *,
    authority: ExtractionAuthorityReferences,
    scope_canonical_id: str,
    membership_semantic_relationship_id: str,
    members: tuple[RequiredMemberReference, ...],
) -> str:
    """Hash ordered membership plus every sealed authority reference."""

    return canonical_sha256(
        _collection_payload(
            authority=authority,
            scope_canonical_id=scope_canonical_id,
            membership_semantic_relationship_id=membership_semantic_relationship_id,
            members=members,
        )
    )


def _validate_members(members: tuple[RequiredMemberReference, ...]) -> None:
    if not members:
        raise ValueError("required member collection must not be empty")
    member_ids = [member.member_canonical_id for member in members]
    if len(member_ids) != len(set(member_ids)):
        raise ValueError("member_canonical_id values must be unique")
    candidate_ids = [member.candidate_id for member in members]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("member candidate_id values must be unique")
    orders = [member.member_order for member in members]
    if len(orders) != len(set(orders)):
        raise ValueError("member_order values must be unique")
    if orders != sorted(orders):
        raise ValueError("members must be stored in ascending member_order")


class ExtractionCandidateBatch(ExtractionAuthorityReferences):
    """L2 carrier for candidates and C0.Core accounting dispositions."""

    identity: CanonicalIdentityEnvelope
    extraction_candidate_batch_id: RequiredText
    input_candidate_count: NonNegativeInt
    retained_candidate_count: NonNegativeInt
    deduplicated_input_count: NonNegativeInt
    candidates: tuple[ExtractionCandidateReference, ...]
    candidate_dispositions: tuple[CandidateAccountingDisposition, ...]
    candidate_id_set_hash: Sha256
    batch_hash: Sha256

    @field_validator("candidates", mode="before")
    @classmethod
    def _candidates(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (
                        item.candidate_id
                        if isinstance(item, ExtractionCandidateReference)
                        else str(item.get("candidate_id", ""))
                    ),
                )
            )
        return value

    @field_validator("candidate_dispositions", mode="before")
    @classmethod
    def _dispositions(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                sorted(
                    value,
                    key=lambda item: (
                        item.input_candidate_id
                        if isinstance(item, CandidateAccountingDisposition)
                        else str(item.get("input_candidate_id", ""))
                    ),
                )
            )
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "ExtractionCandidateBatch":
        _validate_identity(
            self.identity,
            expected_kind="c0.extraction_candidate_batch",
            domain_contract_hash=self.domain_contract_hash,
        )
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate IDs must be unique")
        version_ids = [candidate.candidate_version_id for candidate in self.candidates]
        if len(version_ids) != len(set(version_ids)):
            raise ValueError("candidate version IDs must be unique")
        lifecycle_ids = [candidate.lifecycle_record_id for candidate in self.candidates]
        if len(lifecycle_ids) != len(set(lifecycle_ids)):
            raise ValueError("candidate lifecycle record IDs must be unique")

        input_ids = [
            disposition.input_candidate_id
            for disposition in self.candidate_dispositions
        ]
        if any(
            disposition.identity.domain_contract_hash
            != self.domain_contract_hash
            for disposition in self.candidate_dispositions
        ):
            raise ValueError("candidate accounting domain authority differs")
        if any(
            (
                disposition.identity.project_id,
                disposition.identity.run_id,
            )
            != (self.identity.project_id, self.identity.run_id)
            for disposition in self.candidate_dispositions
        ):
            raise ValueError("candidate accounting identity scope differs")
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("each input candidate requires exactly one disposition")
        retained_ids = {
            disposition.retained_candidate_id
            for disposition in self.candidate_dispositions
            if disposition.disposition == "retained"
        }
        deduplicated = [
            disposition
            for disposition in self.candidate_dispositions
            if disposition.disposition == "deduplicated"
        ]
        if retained_ids != set(candidate_ids):
            raise ValueError(
                "retained accounting references must equal candidate references"
            )
        if any(
            disposition.deduplicated_into_candidate_id not in retained_ids
            for disposition in deduplicated
        ):
            raise ValueError("deduplicated accounting must target one retained candidate")
        if self.input_candidate_count != len(input_ids):
            raise ValueError("input_candidate_count must equal disposition count")
        if self.retained_candidate_count != len(candidate_ids):
            raise ValueError("retained_candidate_count must equal candidate count")
        if self.deduplicated_input_count != len(deduplicated):
            raise ValueError("deduplicated_input_count must equal deduplicated dispositions")
        if (
            self.input_candidate_count
            != self.retained_candidate_count + self.deduplicated_input_count
        ):
            raise ValueError("candidate accounting counts do not reconcile")

        expected_set_hash = canonical_sha256(sorted(candidate_ids))
        if self.candidate_id_set_hash != expected_set_hash:
            raise ValueError("candidate_id_set_hash does not match retained candidate IDs")
        expected_batch_hash = canonical_sha256(
            self.model_dump(mode="json", exclude={"batch_hash"})
        )
        if self.batch_hash != expected_batch_hash:
            raise ValueError("batch_hash does not match extraction candidate batch")
        return self

    @classmethod
    def seal(cls, **values: Any) -> "ExtractionCandidateBatch":
        _expand_authority(values)
        candidates = tuple(
            sorted(values["candidates"], key=lambda item: item.candidate_id)
        )
        dispositions = tuple(
            sorted(
                values["candidate_dispositions"],
                key=lambda item: item.input_candidate_id,
            )
        )
        values["candidates"] = candidates
        values["candidate_dispositions"] = dispositions
        values["candidate_id_set_hash"] = canonical_sha256(
            sorted(candidate.candidate_id for candidate in candidates)
        )
        values["batch_hash"] = canonical_sha256(values)
        return cls.model_validate(values)

    def validate_core_references(
        self,
        *,
        lifecycle_records: Iterable[CandidateLifecycleRecord],
        evidence_spans: Iterable[EvidenceSpan],
    ) -> None:
        """Require references to equal supplied C0.Core records exactly."""

        lifecycle_by_id = {
            record.lifecycle_record_id: record for record in lifecycle_records
        }
        evidence_by_id = {
            evidence.evidence_span_id: evidence for evidence in evidence_spans
        }
        retained_dispositions = {
            disposition.retained_candidate_id: disposition
            for disposition in self.candidate_dispositions
            if disposition.disposition == "retained"
        }
        for candidate in self.candidates:
            try:
                lifecycle = lifecycle_by_id[candidate.lifecycle_record_id]
            except KeyError as exc:
                raise ValueError("candidate lifecycle reference does not resolve") from exc
            if (
                lifecycle.candidate_id != candidate.candidate_id
                or lifecycle.candidate_version_id != candidate.candidate_version_id
                or lifecycle.candidate_kind != candidate.candidate_kind
            ):
                raise ValueError("candidate reference differs from C0.Core lifecycle")
            if tuple(lifecycle.evidence_span_ids) != candidate.evidence_span_ids:
                raise ValueError("candidate evidence differs from C0.Core lifecycle")
            if (
                retained_dispositions[candidate.candidate_id].current_state
                != lifecycle.to_state
            ):
                raise ValueError("candidate accounting state differs from lifecycle")
            if lifecycle.identity.domain_contract_hash != self.domain_contract_hash:
                raise ValueError("candidate lifecycle domain authority differs")
            if (
                lifecycle.identity.project_id,
                lifecycle.identity.run_id,
            ) != (self.identity.project_id, self.identity.run_id):
                raise ValueError("candidate lifecycle identity scope differs")
            for evidence_id in candidate.evidence_span_ids:
                try:
                    evidence = evidence_by_id[evidence_id]
                except KeyError as exc:
                    raise ValueError("candidate evidence reference does not resolve") from exc
                if evidence.identity.domain_contract_hash != self.domain_contract_hash:
                    raise ValueError("candidate evidence domain authority differs")
                if (
                    evidence.identity.project_id,
                    evidence.identity.run_id,
                ) != (self.identity.project_id, self.identity.run_id):
                    raise ValueError("candidate evidence identity scope differs")

    @property
    def authority(self) -> ExtractionAuthorityReferences:
        return _authority_from_carrier(self)


class RequiredMemberSetProposal(ExtractionAuthorityReferences):
    """L2 carrier proposing scope membership under sealed L1 authority."""

    identity: CanonicalIdentityEnvelope
    required_member_set_proposal_id: RequiredText
    extraction_candidate_batch_id: RequiredText
    extraction_candidate_batch_hash: Sha256
    scope_canonical_id: RequiredText
    membership_semantic_relationship_id: RequiredText
    members: tuple[RequiredMemberReference, ...]
    proposal_hash: Sha256

    @model_validator(mode="after")
    def _invariants(self) -> "RequiredMemberSetProposal":
        _validate_identity(
            self.identity,
            expected_kind="c0.required_member_set_proposal",
            domain_contract_hash=self.domain_contract_hash,
        )
        _validate_members(self.members)
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"proposal_hash"})
        )
        if self.proposal_hash != expected:
            raise ValueError("proposal_hash does not match required member set proposal")
        return self

    @classmethod
    def seal(cls, **values: Any) -> "RequiredMemberSetProposal":
        _expand_authority(values)
        values["proposal_hash"] = canonical_sha256(values)
        return cls.model_validate(values)

    def validate_against_batch(self, batch: ExtractionCandidateBatch) -> None:
        if (
            self.extraction_candidate_batch_id
            != batch.extraction_candidate_batch_id
            or self.extraction_candidate_batch_hash != batch.batch_hash
        ):
            raise ValueError("proposal extraction candidate batch reference differs")
        if self.authority != batch.authority:
            raise ValueError("proposal authority references differ from candidate batch")
        if (
            self.identity.project_id,
            self.identity.run_id,
        ) != (batch.identity.project_id, batch.identity.run_id):
            raise ValueError("proposal identity scope differs from candidate batch")
        candidates = {candidate.candidate_id: candidate for candidate in batch.candidates}
        for member in self.members:
            try:
                candidate = candidates[member.candidate_id]
            except KeyError as exc:
                raise ValueError("member candidate does not resolve in batch") from exc
            if member.member_semantic_type_id != candidate.semantic_type_id:
                raise ValueError("member semantic type differs from candidate")
            if not set(member.supporting_evidence_span_ids).issubset(
                candidate.evidence_span_ids
            ):
                raise ValueError("member evidence is not carried by its candidate")

    @property
    def authority(self) -> ExtractionAuthorityReferences:
        return _authority_from_carrier(self)


class RequiredMemberManifest(ExtractionAuthorityReferences):
    """L3 deterministic seal and sole L4-L6 scope-membership carrier."""

    identity: CanonicalIdentityEnvelope
    required_member_manifest_id: RequiredText
    required_member_set_proposal_id: RequiredText
    required_member_set_proposal_hash: Sha256
    extraction_candidate_batch_id: RequiredText
    extraction_candidate_batch_hash: Sha256
    scope_canonical_id: RequiredText
    membership_semantic_relationship_id: RequiredText
    members: tuple[RequiredMemberReference, ...]
    authoritative_collection_hash: Sha256
    validator_name: RequiredText
    validator_version: RequiredText
    sealed_at_utc: datetime
    manifest_hash: Sha256

    _utc = field_validator("sealed_at_utc")(utc_timestamp)

    @model_validator(mode="after")
    def _invariants(self) -> "RequiredMemberManifest":
        _validate_identity(
            self.identity,
            expected_kind="c0.required_member_manifest",
            domain_contract_hash=self.domain_contract_hash,
        )
        _validate_members(self.members)
        expected_collection_hash = authoritative_collection_hash(
            authority=self.authority,
            scope_canonical_id=self.scope_canonical_id,
            membership_semantic_relationship_id=(
                self.membership_semantic_relationship_id
            ),
            members=self.members,
        )
        if self.authoritative_collection_hash != expected_collection_hash:
            raise ValueError(
                "authoritative_collection_hash does not match ordered membership"
            )
        expected_manifest_hash = canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"manifest_hash", "sealed_at_utc"},
            )
        )
        if self.manifest_hash != expected_manifest_hash:
            raise ValueError("manifest_hash does not match required member manifest")
        return self

    @classmethod
    def seal_from_proposal(
        cls,
        proposal: RequiredMemberSetProposal,
        *,
        identity: CanonicalIdentityEnvelope,
        required_member_manifest_id: str,
        validator_name: str,
        validator_version: str,
        sealed_at_utc: datetime,
    ) -> "RequiredMemberManifest":
        if (
            identity.project_id,
            identity.run_id,
        ) != (proposal.identity.project_id, proposal.identity.run_id):
            raise ValueError("manifest identity scope differs from proposal")
        values = {
            "identity": identity,
            "required_member_manifest_id": required_member_manifest_id,
            "required_member_set_proposal_id": (
                proposal.required_member_set_proposal_id
            ),
            "required_member_set_proposal_hash": proposal.proposal_hash,
            "extraction_candidate_batch_id": proposal.extraction_candidate_batch_id,
            "extraction_candidate_batch_hash": proposal.extraction_candidate_batch_hash,
            **proposal.authority.model_dump(mode="python"),
            "scope_canonical_id": proposal.scope_canonical_id,
            "membership_semantic_relationship_id": (
                proposal.membership_semantic_relationship_id
            ),
            "members": proposal.members,
            "validator_name": validator_name,
            "validator_version": validator_version,
            "sealed_at_utc": sealed_at_utc,
        }
        values["authoritative_collection_hash"] = authoritative_collection_hash(
            authority=proposal.authority,
            scope_canonical_id=proposal.scope_canonical_id,
            membership_semantic_relationship_id=(
                proposal.membership_semantic_relationship_id
            ),
            members=proposal.members,
        )
        semantic_values = dict(values)
        semantic_values.pop("sealed_at_utc")
        values["manifest_hash"] = canonical_sha256(semantic_values)
        return cls.model_validate(values)

    def validate_against_proposal(
        self,
        proposal: RequiredMemberSetProposal,
    ) -> None:
        if (
            self.required_member_set_proposal_id
            != proposal.required_member_set_proposal_id
            or self.required_member_set_proposal_hash != proposal.proposal_hash
        ):
            raise ValueError("manifest proposal reference differs")
        if (
            self.identity.project_id,
            self.identity.run_id,
        ) != (proposal.identity.project_id, proposal.identity.run_id):
            raise ValueError("manifest identity scope differs from proposal")
        repeated = (
            "extraction_candidate_batch_id",
            "extraction_candidate_batch_hash",
            "authority",
            "scope_canonical_id",
            "membership_semantic_relationship_id",
            "members",
        )
        if any(getattr(self, field) != getattr(proposal, field) for field in repeated):
            raise ValueError("manifest reinterprets proposal membership or authority")

    @property
    def authority(self) -> ExtractionAuthorityReferences:
        return _authority_from_carrier(self)
