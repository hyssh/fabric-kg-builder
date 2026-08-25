"""Strict cross-layer carriers for L2 extraction and L3 member sealing."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Iterable, Literal

from pydantic import Field, field_validator, model_validator

from .base import (
    ContractError,
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


RequiredRoleIdV1_1 = Annotated[
    str,
    Field(pattern=r"^role:[a-z0-9][a-z0-9._:-]*$"),
]

_AMBIGUOUS_ROLE_IDS = frozenset(
    {
        "role:default",
        "role:none",
        "role:placeholder",
        "role:unknown",
        "role:unspecified",
    }
)


def _is_ambiguous_role_id(role_id: str) -> bool:
    return (
        role_id in _AMBIGUOUS_ROLE_IDS
        or role_id.rsplit(":", maxsplit=1)[-1]
        in {"default", "none", "placeholder", "unknown", "unspecified"}
    )


class RequiredMemberMigrationError(ContractError):
    """Raised when a 1.0 carrier cannot be promoted without reinterpretation."""

    code = "C0_REQUIRED_MEMBER_1_0_AMBIGUOUS"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class RequiredMemberOrderingPolicyV1_1(ContractModel):
    """Exact ordered/unordered metadata copied from sealed DomainContractV2."""

    mode: Literal["unordered", "ordered"]
    ordinal_property_id: RequiredText | None = None
    ordinal_value_type: Literal["integer"] | None = None
    direction: Literal["ascending", "descending"] | None = None
    unique_ordinals: bool | None = None
    contiguous: bool | None = None
    member_order_encoding: Literal["zero_based_contiguous"] | None = None

    @model_validator(mode="after")
    def _policy_fields(self) -> "RequiredMemberOrderingPolicyV1_1":
        ordinal_fields = (
            self.ordinal_property_id,
            self.ordinal_value_type,
            self.direction,
            self.unique_ordinals,
            self.contiguous,
            self.member_order_encoding,
        )
        if self.mode == "unordered":
            if any(value is not None for value in ordinal_fields):
                raise ValueError("unordered policy cannot declare ordinal metadata")
            return self
        if any(value is None for value in ordinal_fields):
            raise ValueError("ordered policy requires complete ordinal metadata")
        if self.unique_ordinals is not True or self.contiguous is not True:
            raise ValueError(
                "ordered carrier requires unique contiguous Domain ordinals"
            )
        return self


class RequiredMemberReferenceV1_1(ContractModel):
    """One member without duplicated collection policy."""

    member_canonical_id: RequiredText
    member_semantic_type_id: RequiredText
    member_role_id: RequiredRoleIdV1_1 | None = None
    member_order: NonNegativeInt | None = None
    candidate_id: RequiredText
    supporting_evidence_span_ids: tuple[EvidenceSpanId, ...] = ()
    member_hash: Sha256

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
    def _member_invariants(self) -> "RequiredMemberReferenceV1_1":
        if self.member_role_id is not None and _is_ambiguous_role_id(
            self.member_role_id
        ):
            raise ValueError("sentinel member_role_id is prohibited")
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"member_hash"})
        )
        if self.member_hash != expected:
            raise ValueError("member_hash does not match required member")
        return self

    @classmethod
    def seal(cls, **values: Any) -> "RequiredMemberReferenceV1_1":
        values.setdefault("member_role_id", None)
        values.setdefault("member_order", None)
        values.setdefault("supporting_evidence_span_ids", ())
        values["supporting_evidence_span_ids"] = sorted_unique(
            values["supporting_evidence_span_ids"],
            field_name="supporting_evidence_span_ids",
        )
        values["member_hash"] = canonical_sha256(values)
        return cls.model_validate(values)


def _canonical_members_v1_1(
    members: Iterable[RequiredMemberReferenceV1_1],
    *,
    ordering_mode: Literal["unordered", "ordered"],
) -> tuple[RequiredMemberReferenceV1_1, ...]:
    if ordering_mode == "unordered":
        return tuple(sorted(members, key=lambda item: item.member_canonical_id))
    return tuple(
        sorted(
            members,
            key=lambda item: (
                item.member_order if item.member_order is not None else -1
            ),
        )
    )


def _member_hashes_v1_1(
    members: tuple[RequiredMemberReferenceV1_1, ...],
    *,
    ordering_mode: Literal["unordered", "ordered"],
) -> tuple[str, str | None]:
    member_set_hash = canonical_sha256(
        sorted(
            (
                member.member_canonical_id,
                member.member_hash,
            )
            for member in members
        )
    )
    ordered_member_tuple_hash = (
        canonical_sha256(
            [
                (
                    member.member_order,
                    member.member_canonical_id,
                    member.member_hash,
                )
                for member in members
            ]
        )
        if ordering_mode == "ordered"
        else None
    )
    return member_set_hash, ordered_member_tuple_hash


def _validate_cardinality_v1_1(
    *,
    expected_cardinality: int | None,
    minimum_cardinality: int | None,
    maximum_cardinality: int | None,
) -> None:
    if (
        minimum_cardinality is not None
        and maximum_cardinality is not None
        and minimum_cardinality > maximum_cardinality
    ):
        raise ValueError("minimum_cardinality cannot exceed maximum_cardinality")
    if expected_cardinality is not None and (
        (
            minimum_cardinality is not None
            and expected_cardinality < minimum_cardinality
        )
        or (
            maximum_cardinality is not None
            and expected_cardinality > maximum_cardinality
        )
    ):
        raise ValueError("expected_cardinality must be inside min/max bounds")


def _validate_members_v1_1(
    *,
    members: tuple[RequiredMemberReferenceV1_1, ...],
    ordering_policy: RequiredMemberOrderingPolicyV1_1,
    required_role_ids: tuple[str, ...],
) -> None:
    member_ids = [member.member_canonical_id for member in members]
    if len(member_ids) != len(set(member_ids)):
        raise ValueError("member_canonical_id values must be unique")
    candidate_ids = [member.candidate_id for member in members]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("member candidate_id values must be unique")
    if any(_is_ambiguous_role_id(role_id) for role_id in required_role_ids):
        raise ValueError("sentinel required role ID is prohibited")

    roles = [member.member_role_id for member in members]
    orders = [member.member_order for member in members]
    if ordering_policy.mode == "unordered":
        if any(order is not None for order in orders):
            raise ValueError("unordered members cannot declare member_order")
        if member_ids != sorted(member_ids):
            raise ValueError("unordered members must be canonicalized by member ID")
    else:
        if any(order is None for order in orders):
            raise ValueError("ordered members require member_order")
        if orders != list(range(len(members))):
            raise ValueError(
                "member_order must be unique contiguous zero-based ordinals"
            )

    if not required_role_ids:
        if any(role is not None for role in roles):
            raise ValueError("roleless policy cannot declare member roles")
    else:
        if any(role is None for role in roles):
            raise ValueError("role-bearing policy requires a role for every member")
        if not set(roles) <= set(required_role_ids):
            raise ValueError("member_role_id is not approved by Domain authority")
        if set(roles) != set(required_role_ids):
            raise ValueError("every required Domain role must be represented")


def _collection_payload_v1_1(
    *,
    authority: ExtractionAuthorityReferences,
    scope_canonical_id: str,
    membership_semantic_relationship_id: str,
    ordering_policy: RequiredMemberOrderingPolicyV1_1,
    expected_cardinality: int | None,
    minimum_cardinality: int | None,
    maximum_cardinality: int | None,
    required_role_ids: tuple[str, ...],
    members: tuple[RequiredMemberReferenceV1_1, ...],
) -> dict[str, Any]:
    return {
        **authority.model_dump(mode="json"),
        "scope_canonical_id": scope_canonical_id,
        "membership_semantic_relationship_id": membership_semantic_relationship_id,
        "ordering_policy": ordering_policy.model_dump(mode="json"),
        "expected_cardinality": expected_cardinality,
        "minimum_cardinality": minimum_cardinality,
        "maximum_cardinality": maximum_cardinality,
        "required_role_ids": list(required_role_ids),
        "members": [member.model_dump(mode="json") for member in members],
    }


def authoritative_collection_hash_v1_1(
    *,
    authority: ExtractionAuthorityReferences,
    scope_canonical_id: str,
    membership_semantic_relationship_id: str,
    ordering_policy: RequiredMemberOrderingPolicyV1_1,
    expected_cardinality: int | None,
    minimum_cardinality: int | None,
    maximum_cardinality: int | None,
    required_role_ids: tuple[str, ...],
    members: tuple[RequiredMemberReferenceV1_1, ...],
) -> str:
    """Hash exact collection policy, canonical members, and sealed authority."""

    return canonical_sha256(
        _collection_payload_v1_1(
            authority=authority,
            scope_canonical_id=scope_canonical_id,
            membership_semantic_relationship_id=(
                membership_semantic_relationship_id
            ),
            ordering_policy=ordering_policy,
            expected_cardinality=expected_cardinality,
            minimum_cardinality=minimum_cardinality,
            maximum_cardinality=maximum_cardinality,
            required_role_ids=required_role_ids,
            members=members,
        )
    )


class _RequiredMemberCollectionV1_1(ExtractionAuthorityReferences):
    extraction_candidate_batch_id: RequiredText
    extraction_candidate_batch_hash: Sha256
    scope_canonical_id: RequiredText
    membership_semantic_relationship_id: RequiredText
    ordering_policy: RequiredMemberOrderingPolicyV1_1
    expected_cardinality: NonNegativeInt | None = None
    minimum_cardinality: NonNegativeInt | None = None
    maximum_cardinality: NonNegativeInt | None = None
    required_role_ids: tuple[RequiredRoleIdV1_1, ...] = ()
    members: tuple[RequiredMemberReferenceV1_1, ...]
    member_set_hash: Sha256
    ordered_member_tuple_hash: Sha256 | None
    authoritative_collection_hash: Sha256

    @field_validator("required_role_ids", mode="before")
    @classmethod
    def _role_ids(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name="required_role_ids")
        return value

    @model_validator(mode="before")
    @classmethod
    def _canonicalize_members(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        policy = value.get("ordering_policy")
        mode = (
            policy.mode
            if isinstance(policy, RequiredMemberOrderingPolicyV1_1)
            else policy.get("mode")
            if isinstance(policy, dict)
            else None
        )
        raw_members = value.get("members")
        if mode not in {"unordered", "ordered"} or not isinstance(
            raw_members, (list, tuple)
        ):
            return value
        members = tuple(
            item
            if isinstance(item, RequiredMemberReferenceV1_1)
            else RequiredMemberReferenceV1_1.model_validate(item)
            for item in raw_members
        )
        canonical = dict(value)
        canonical["members"] = _canonical_members_v1_1(
            members,
            ordering_mode=mode,
        )
        return canonical

    def _validate_collection(self) -> None:
        _validate_cardinality_v1_1(
            expected_cardinality=self.expected_cardinality,
            minimum_cardinality=self.minimum_cardinality,
            maximum_cardinality=self.maximum_cardinality,
        )
        _validate_members_v1_1(
            members=self.members,
            ordering_policy=self.ordering_policy,
            required_role_ids=self.required_role_ids,
        )
        member_set_hash, ordered_tuple_hash = _member_hashes_v1_1(
            self.members,
            ordering_mode=self.ordering_policy.mode,
        )
        if self.member_set_hash != member_set_hash:
            raise ValueError("member_set_hash does not match canonical members")
        if self.ordered_member_tuple_hash != ordered_tuple_hash:
            raise ValueError(
                "ordered_member_tuple_hash does not match ordered members"
            )
        expected_collection_hash = authoritative_collection_hash_v1_1(
            authority=self.authority,
            scope_canonical_id=self.scope_canonical_id,
            membership_semantic_relationship_id=(
                self.membership_semantic_relationship_id
            ),
            ordering_policy=self.ordering_policy,
            expected_cardinality=self.expected_cardinality,
            minimum_cardinality=self.minimum_cardinality,
            maximum_cardinality=self.maximum_cardinality,
            required_role_ids=self.required_role_ids,
            members=self.members,
        )
        if self.authoritative_collection_hash != expected_collection_hash:
            raise ValueError(
                "authoritative_collection_hash does not match policy, "
                "members, and authority"
            )

    @property
    def authority(self) -> ExtractionAuthorityReferences:
        return _authority_from_carrier(self)


class RequiredMemberSetProposalIdentityV1_1(CanonicalIdentityEnvelope):
    """Identity revision scoped to the additive proposal carrier."""

    contract_kind: Literal["c0.required_member_set_proposal"] = (
        "c0.required_member_set_proposal"
    )
    contract_version: Literal["1.1.0"] = "1.1.0"
    domain_schema_version: Literal["2.0"]


class RequiredMemberManifestIdentityV1_1(CanonicalIdentityEnvelope):
    """Identity revision scoped to the additive manifest carrier."""

    contract_kind: Literal["c0.required_member_manifest"] = (
        "c0.required_member_manifest"
    )
    contract_version: Literal["1.1.0"] = "1.1.0"
    domain_schema_version: Literal["2.0"]


class RequiredMemberSetProposalV1_1(_RequiredMemberCollectionV1_1):
    """L2 collection proposal preserving exact sealed Domain policy."""

    identity: RequiredMemberSetProposalIdentityV1_1
    required_member_set_proposal_id: RequiredText
    proposal_hash: Sha256

    @model_validator(mode="after")
    def _invariants(self) -> "RequiredMemberSetProposalV1_1":
        _validate_identity(
            self.identity,
            expected_kind="c0.required_member_set_proposal",
            domain_contract_hash=self.domain_contract_hash,
        )
        self._validate_collection()
        expected = canonical_sha256(
            self.model_dump(mode="json", exclude={"proposal_hash"})
        )
        if self.proposal_hash != expected:
            raise ValueError("proposal_hash does not match required member set proposal")
        return self

    @classmethod
    def seal(cls, **values: Any) -> "RequiredMemberSetProposalV1_1":
        _expand_authority(values)
        policy = values["ordering_policy"]
        if not isinstance(policy, RequiredMemberOrderingPolicyV1_1):
            policy = RequiredMemberOrderingPolicyV1_1.model_validate(policy)
        values["ordering_policy"] = policy
        values.setdefault("expected_cardinality", None)
        values.setdefault("minimum_cardinality", None)
        values.setdefault("maximum_cardinality", None)
        values["required_role_ids"] = sorted_unique(
            values.get("required_role_ids", ()),
            field_name="required_role_ids",
        )
        values["members"] = _canonical_members_v1_1(
            values["members"],
            ordering_mode=policy.mode,
        )
        member_set_hash, ordered_tuple_hash = _member_hashes_v1_1(
            values["members"],
            ordering_mode=policy.mode,
        )
        values["member_set_hash"] = member_set_hash
        values["ordered_member_tuple_hash"] = ordered_tuple_hash
        authority = ExtractionAuthorityReferences.model_validate(
            {field: values[field] for field in _AUTHORITY_FIELDS}
        )
        values["authoritative_collection_hash"] = (
            authoritative_collection_hash_v1_1(
                authority=authority,
                scope_canonical_id=values["scope_canonical_id"],
                membership_semantic_relationship_id=(
                    values["membership_semantic_relationship_id"]
                ),
                ordering_policy=policy,
                expected_cardinality=values.get("expected_cardinality"),
                minimum_cardinality=values.get("minimum_cardinality"),
                maximum_cardinality=values.get("maximum_cardinality"),
                required_role_ids=values["required_role_ids"],
                members=values["members"],
            )
        )
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
        if batch.identity.domain_schema_version != self.identity.domain_schema_version:
            raise ValueError(
                "proposal Domain schema authority differs from candidate batch"
            )
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


class RequiredMemberManifestV1_1(_RequiredMemberCollectionV1_1):
    """L3 seal that repeats one 1.1 proposal without reinterpretation."""

    identity: RequiredMemberManifestIdentityV1_1
    required_member_manifest_id: RequiredText
    required_member_set_proposal_id: RequiredText
    required_member_set_proposal_hash: Sha256
    validator_name: RequiredText
    validator_version: RequiredText
    sealed_at_utc: datetime
    manifest_hash: Sha256

    @field_validator("sealed_at_utc", mode="before")
    @classmethod
    def _parse_utc(cls, value: object) -> object:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return utc_timestamp(value)

    @model_validator(mode="after")
    def _invariants(self) -> "RequiredMemberManifestV1_1":
        _validate_identity(
            self.identity,
            expected_kind="c0.required_member_manifest",
            domain_contract_hash=self.domain_contract_hash,
        )
        self._validate_collection()
        count = len(self.members)
        if (
            self.expected_cardinality is not None
            and count != self.expected_cardinality
        ):
            raise ValueError("member count does not equal expected_cardinality")
        if self.minimum_cardinality is not None and count < self.minimum_cardinality:
            raise ValueError("member count is below minimum_cardinality")
        if self.maximum_cardinality is not None and count > self.maximum_cardinality:
            raise ValueError("member count exceeds maximum_cardinality")
        expected = canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"manifest_hash", "sealed_at_utc"},
            )
        )
        if self.manifest_hash != expected:
            raise ValueError("manifest_hash does not match required member manifest")
        return self

    @classmethod
    def seal_from_proposal(
        cls,
        proposal: RequiredMemberSetProposalV1_1,
        *,
        identity: CanonicalIdentityEnvelope,
        required_member_manifest_id: str,
        validator_name: str,
        validator_version: str,
        sealed_at_utc: datetime,
    ) -> "RequiredMemberManifestV1_1":
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
            "ordering_policy": proposal.ordering_policy,
            "expected_cardinality": proposal.expected_cardinality,
            "minimum_cardinality": proposal.minimum_cardinality,
            "maximum_cardinality": proposal.maximum_cardinality,
            "required_role_ids": proposal.required_role_ids,
            "members": proposal.members,
            "member_set_hash": proposal.member_set_hash,
            "ordered_member_tuple_hash": proposal.ordered_member_tuple_hash,
            "authoritative_collection_hash": (
                proposal.authoritative_collection_hash
            ),
            "validator_name": validator_name,
            "validator_version": validator_version,
            "sealed_at_utc": sealed_at_utc,
        }
        semantic_values = dict(values)
        semantic_values.pop("sealed_at_utc")
        values["manifest_hash"] = canonical_sha256(semantic_values)
        return cls.model_validate(values)

    def validate_against_proposal(
        self,
        proposal: RequiredMemberSetProposalV1_1,
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
            "ordering_policy",
            "expected_cardinality",
            "minimum_cardinality",
            "maximum_cardinality",
            "required_role_ids",
            "members",
            "member_set_hash",
            "ordered_member_tuple_hash",
            "authoritative_collection_hash",
        )
        if any(getattr(self, field) != getattr(proposal, field) for field in repeated):
            raise ValueError("manifest reinterprets proposal membership or authority")


class TrustedRequiredMemberPolicyContextV1_1(ContractModel):
    """Explicit Domain authority needed for controlled legacy adaptation."""

    domain_contract_hash: Sha256
    completeness_requirement_id: RequiredText
    completeness_requirement_hash: Sha256
    hierarchy_hash: Sha256
    identity_policy_hash: Sha256
    ordering_policy: RequiredMemberOrderingPolicyV1_1
    expected_cardinality: NonNegativeInt | None = None
    minimum_cardinality: NonNegativeInt | None = None
    maximum_cardinality: NonNegativeInt | None = None
    required_role_ids: tuple[RequiredRoleIdV1_1, ...]

    @field_validator("required_role_ids", mode="before")
    @classmethod
    def _role_ids(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name="required_role_ids")
        return value

    @model_validator(mode="after")
    def _safe_legacy_subset(self) -> "TrustedRequiredMemberPolicyContextV1_1":
        _validate_cardinality_v1_1(
            expected_cardinality=self.expected_cardinality,
            minimum_cardinality=self.minimum_cardinality,
            maximum_cardinality=self.maximum_cardinality,
        )
        if self.ordering_policy.mode != "ordered":
            raise ValueError("legacy adaptation supports ordered policy only")
        if not self.required_role_ids:
            raise ValueError("legacy adaptation requires approved role IDs")
        if any(_is_ambiguous_role_id(role_id) for role_id in self.required_role_ids):
            raise ValueError("legacy adaptation prohibits sentinel role IDs")
        return self


def adapt_required_member_set_proposal_v1_0_to_v1_1(
    proposal: RequiredMemberSetProposal,
    *,
    trusted_policy: TrustedRequiredMemberPolicyContextV1_1,
) -> RequiredMemberSetProposalV1_1:
    """Promote only legacy ordered, role-bearing, lossless collection facts."""

    if proposal.identity.contract_version != "1.0.0":
        raise RequiredMemberMigrationError("proposal input must be 1.0.0")
    if proposal.identity.domain_schema_version != "2.0":
        raise RequiredMemberMigrationError(
            "legacy proposal is not bound to DomainContractV2"
        )
    authority_pairs = (
        ("domain_contract_hash", trusted_policy.domain_contract_hash),
        (
            "completeness_requirement_id",
            trusted_policy.completeness_requirement_id,
        ),
        (
            "completeness_requirement_hash",
            trusted_policy.completeness_requirement_hash,
        ),
        ("hierarchy_hash", trusted_policy.hierarchy_hash),
        ("identity_policy_hash", trusted_policy.identity_policy_hash),
    )
    if any(getattr(proposal, field) != expected for field, expected in authority_pairs):
        raise RequiredMemberMigrationError(
            "trusted policy does not match legacy authority references"
        )
    roles = tuple(member.member_role_id for member in proposal.members)
    if any(_is_ambiguous_role_id(role_id) for role_id in roles):
        raise RequiredMemberMigrationError("legacy sentinel role is ambiguous")
    if not set(roles) <= set(trusted_policy.required_role_ids) or set(roles) != set(
        trusted_policy.required_role_ids
    ):
        raise RequiredMemberMigrationError(
            "legacy member roles do not equal approved Domain roles"
        )
    orders = tuple(member.member_order for member in proposal.members)
    if orders != tuple(range(len(proposal.members))):
        raise RequiredMemberMigrationError(
            "legacy member order is not contiguous zero-based"
        )
    legacy_bounds = {
        (
            member.minimum_cardinality,
            member.maximum_cardinality,
        )
        for member in proposal.members
    }
    if len(legacy_bounds) != 1:
        raise RequiredMemberMigrationError(
            "legacy members repeat inconsistent collection cardinality"
        )
    legacy_minimum, legacy_maximum = next(iter(legacy_bounds))
    if (
        trusted_policy.minimum_cardinality is None
        or legacy_minimum != trusted_policy.minimum_cardinality
        or legacy_maximum != trusted_policy.maximum_cardinality
    ):
        raise RequiredMemberMigrationError(
            "legacy cardinality does not exactly match trusted Domain bounds"
        )
    if trusted_policy.expected_cardinality is not None and not (
        legacy_minimum
        == legacy_maximum
        == trusted_policy.expected_cardinality
    ):
        raise RequiredMemberMigrationError(
            "legacy fields cannot prove the trusted expected cardinality"
        )
    identity_values = proposal.identity.model_dump(mode="python")
    identity_values["contract_version"] = "1.1.0"
    identity = RequiredMemberSetProposalIdentityV1_1.model_validate(identity_values)
    members = tuple(
        RequiredMemberReferenceV1_1.seal(
            member_canonical_id=member.member_canonical_id,
            member_semantic_type_id=member.member_semantic_type_id,
            member_role_id=member.member_role_id,
            member_order=member.member_order,
            candidate_id=member.candidate_id,
            supporting_evidence_span_ids=member.supporting_evidence_span_ids,
        )
        for member in proposal.members
    )
    return RequiredMemberSetProposalV1_1.seal(
        identity=identity,
        required_member_set_proposal_id=proposal.required_member_set_proposal_id,
        extraction_candidate_batch_id=proposal.extraction_candidate_batch_id,
        extraction_candidate_batch_hash=proposal.extraction_candidate_batch_hash,
        authority=proposal.authority,
        scope_canonical_id=proposal.scope_canonical_id,
        membership_semantic_relationship_id=(
            proposal.membership_semantic_relationship_id
        ),
        ordering_policy=trusted_policy.ordering_policy,
        expected_cardinality=trusted_policy.expected_cardinality,
        minimum_cardinality=trusted_policy.minimum_cardinality,
        maximum_cardinality=trusted_policy.maximum_cardinality,
        required_role_ids=trusted_policy.required_role_ids,
        members=members,
    )


def adapt_required_member_manifest_v1_0_to_v1_1(
    manifest: RequiredMemberManifest,
    *,
    legacy_proposal: RequiredMemberSetProposal,
    trusted_policy: TrustedRequiredMemberPolicyContextV1_1,
) -> RequiredMemberManifestV1_1:
    """Seal a legacy manifest only after its proposal adapted safely."""

    if manifest.identity.contract_version != "1.0.0":
        raise RequiredMemberMigrationError("manifest input must be 1.0.0")
    try:
        manifest.validate_against_proposal(legacy_proposal)
    except ValueError as exc:
        raise RequiredMemberMigrationError(
            "legacy manifest does not exactly seal its legacy proposal"
        ) from exc
    adapted_proposal = adapt_required_member_set_proposal_v1_0_to_v1_1(
        legacy_proposal,
        trusted_policy=trusted_policy,
    )
    identity_values = manifest.identity.model_dump(mode="python")
    identity_values["contract_version"] = "1.1.0"
    identity = RequiredMemberManifestIdentityV1_1.model_validate(identity_values)
    return RequiredMemberManifestV1_1.seal_from_proposal(
        adapted_proposal,
        identity=identity,
        required_member_manifest_id=manifest.required_member_manifest_id,
        validator_name=manifest.validator_name,
        validator_version=manifest.validator_version,
        sealed_at_utc=manifest.sealed_at_utc,
    )
