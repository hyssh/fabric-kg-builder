"""Immutable L1 intake, profile, design, and approval context contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from fabric_kg_builder.contracts.base import (
    CONTRACT_VERSION,
    ContractModel,
    RequiredText,
    Sha256,
    canonical_sha256,
    deterministic_contract_id,
    sorted_unique,
    utc_timestamp,
)
from fabric_kg_builder.contracts.identity import CanonicalIdentityEnvelope

from .models import CompetencyQuestionV2, DomainContractV2

NonNegativeInt = Annotated[int, Field(ge=0)]


def _validate_identity(
    identity: CanonicalIdentityEnvelope,
    *,
    contract_kind: str,
    content_hash: str,
) -> None:
    if identity.contract_kind != contract_kind:
        raise ValueError(f"identity.contract_kind must be {contract_kind}")
    if identity.contract_version != CONTRACT_VERSION:
        raise ValueError("L1 identity contract version must be 1.0.0")
    if identity.content_hash != content_hash:
        raise ValueError("identity.content_hash must equal the L1 semantic hash")


class DomainTerminologyInput(ContractModel):
    term: RequiredText
    definition: RequiredText
    aliases: tuple[RequiredText, ...] = ()

    @field_validator("aliases", mode="before")
    @classmethod
    def _aliases(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name="aliases")
        return value


class StructuralCompletenessInput(ContractModel):
    requirement_id: RequiredText
    competency_question_ids: tuple[RequiredText, ...]
    required_role_labels: tuple[RequiredText, ...] = ()
    ordered_collection: bool | None = None
    expected_member_count: NonNegativeInt | None = None
    minimum_member_count: NonNegativeInt | None = None
    maximum_member_count: NonNegativeInt | None = None
    rationale: RequiredText

    @field_validator(
        "competency_question_ids", "required_role_labels", mode="before"
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name=info.field_name)
        return value

    @model_validator(mode="after")
    def _bounds(self) -> "StructuralCompletenessInput":
        if not self.competency_question_ids:
            raise ValueError("structural completeness input requires question IDs")
        if (
            self.minimum_member_count is not None
            and self.maximum_member_count is not None
            and self.minimum_member_count > self.maximum_member_count
        ):
            raise ValueError("minimum member count cannot exceed maximum")
        if self.expected_member_count is not None:
            if (
                self.minimum_member_count is not None
                and self.expected_member_count < self.minimum_member_count
            ) or (
                self.maximum_member_count is not None
                and self.expected_member_count > self.maximum_member_count
            ):
                raise ValueError("expected member count must be within bounds")
        return self


class DomainIntake(ContractModel):
    identity: CanonicalIdentityEnvelope
    contract_version: Literal["1.0.0"] = "1.0.0"
    domain_intake_id: RequiredText
    business_goal: RequiredText
    organization_context: RequiredText
    users: tuple[RequiredText, ...]
    decisions: tuple[RequiredText, ...]
    desired_outcomes: tuple[RequiredText, ...]
    in_scope: tuple[RequiredText, ...]
    out_of_scope: tuple[RequiredText, ...] = ()
    competency_questions: tuple[CompetencyQuestionV2, ...]
    terminology: tuple[DomainTerminologyInput, ...] = ()
    examples: tuple[RequiredText, ...] = ()
    temporal_constraints: tuple[RequiredText, ...] = ()
    regulatory_constraints: tuple[RequiredText, ...] = ()
    privacy_constraints: tuple[RequiredText, ...] = ()
    safety_constraints: tuple[RequiredText, ...] = ()
    structural_completeness_expectations: tuple[
        StructuralCompletenessInput, ...
    ] = ()
    intake_hash: Sha256

    @field_validator(
        "users",
        "decisions",
        "desired_outcomes",
        "in_scope",
        "out_of_scope",
        "examples",
        "temporal_constraints",
        "regulatory_constraints",
        "privacy_constraints",
        "safety_constraints",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name=info.field_name)
        return value

    @field_validator(
        "competency_questions",
        "terminology",
        "structural_completeness_expectations",
        mode="before",
    )
    @classmethod
    def _records(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "DomainIntake":
        if not 5 <= len(self.competency_questions) <= 10:
            raise ValueError("[DOM-101] intake requires five to ten questions")
        question_ids = [item.id for item in self.competency_questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("competency question IDs must be unique")
        values = self.model_dump(
            mode="json",
            exclude={"identity", "domain_intake_id", "intake_hash"},
        )
        expected_hash = canonical_sha256(values)
        if self.intake_hash != expected_hash:
            raise ValueError("intake_hash does not match intake content")
        expected_id = deterministic_contract_id(
            "domain-intake", {"intake_hash": self.intake_hash}
        )
        if self.domain_intake_id != expected_id:
            raise ValueError("domain_intake_id does not match deterministic seed")
        _validate_identity(
            self.identity,
            contract_kind="l1.domain_intake",
            content_hash=self.intake_hash,
        )
        return self


class SourceProfileWarning(ContractModel):
    warning_id: RequiredText
    warning_type: RequiredText
    source_file_id: RequiredText | None = None
    message: RequiredText


class DomainSourceProfile(ContractModel):
    identity: CanonicalIdentityEnvelope
    contract_version: Literal["1.0.0"] = "1.0.0"
    domain_source_profile_id: RequiredText
    source_corpus_manifest_id: RequiredText
    source_corpus_manifest_hash: Sha256
    design_sample_manifest_id: RequiredText
    design_sample_manifest_hash: Sha256
    budget_snapshot_hash: Sha256
    complete_source_count: NonNegativeInt
    eligible_source_count: NonNegativeInt
    excluded_source_count: NonNegativeInt
    blocked_source_count: NonNegativeInt
    observed_media_types: tuple[RequiredText, ...] = ()
    observed_schema_fields: tuple[RequiredText, ...] = ()
    inferred_suggestions: tuple[RequiredText, ...] = ()
    warnings: tuple[SourceProfileWarning, ...] = ()
    completeness_disclaimer: Literal[
        "design samples are bounded proposal support, not the complete source universe"
    ] = "design samples are bounded proposal support, not the complete source universe"
    profile_hash: Sha256

    @field_validator(
        "observed_media_types",
        "observed_schema_fields",
        "inferred_suggestions",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name=info.field_name)
        return value

    @field_validator("warnings", mode="before")
    @classmethod
    def _warnings(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(value)
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "DomainSourceProfile":
        if (
            self.eligible_source_count
            + self.excluded_source_count
            + self.blocked_source_count
            != self.complete_source_count
        ):
            raise ValueError("source profile disposition counts must reconcile")
        values = self.model_dump(
            mode="json",
            exclude={"identity", "domain_source_profile_id", "profile_hash"},
        )
        expected_hash = canonical_sha256(values)
        if self.profile_hash != expected_hash:
            raise ValueError("profile_hash does not match profile content")
        expected_id = deterministic_contract_id(
            "domain-source-profile", {"profile_hash": self.profile_hash}
        )
        if self.domain_source_profile_id != expected_id:
            raise ValueError("domain_source_profile_id does not match deterministic seed")
        _validate_identity(
            self.identity,
            contract_kind="l1.domain_source_profile",
            content_hash=self.profile_hash,
        )
        return self


class DomainDesignContext(ContractModel):
    identity: CanonicalIdentityEnvelope
    contract_version: Literal["1.0.0"] = "1.0.0"
    domain_design_context_id: RequiredText
    domain_intake_id: RequiredText
    domain_intake_hash: Sha256
    source_profile_id: RequiredText
    source_profile_hash: Sha256
    input_manifest_id: RequiredText
    input_manifest_hash: Sha256
    source_corpus_manifest_id: RequiredText
    source_corpus_manifest_hash: Sha256
    design_sample_manifest_id: RequiredText
    design_sample_manifest_hash: Sha256
    competency_question_ids: tuple[RequiredText, ...]
    completeness_requirement_ids: tuple[RequiredText, ...]
    completeness_requirement_hash: Sha256
    hierarchy_hash: Sha256
    identity_policy_hash: Sha256
    source_unit_ids: tuple[RequiredText, ...]
    evidence_span_ids: tuple[RequiredText, ...]
    prompt_version: RequiredText
    prompt_hash: Sha256
    model_version: RequiredText
    model_hash: Sha256
    selector_version: RequiredText
    selector_hash: Sha256
    scorer_version: RequiredText
    scorer_hash: Sha256
    budget_snapshot_hash: Sha256
    parent_correction_context_id: RequiredText | None = None
    design_context_hash: Sha256

    @field_validator(
        "competency_question_ids",
        "completeness_requirement_ids",
        "source_unit_ids",
        "evidence_span_ids",
        mode="before",
    )
    @classmethod
    def _sets(cls, value: object, info: Any) -> object:
        if isinstance(value, (list, tuple)):
            return sorted_unique(value, field_name=info.field_name)
        return value

    @model_validator(mode="after")
    def _invariants(self) -> "DomainDesignContext":
        if tuple(self.competency_question_ids) != tuple(
            sorted(self.competency_question_ids)
        ):
            raise ValueError("competency question IDs must be canonically ordered")
        values = self.model_dump(
            mode="json",
            exclude={
                "identity",
                "domain_design_context_id",
                "design_context_hash",
            },
        )
        expected_hash = canonical_sha256(values)
        if self.design_context_hash != expected_hash:
            raise ValueError("design_context_hash does not match context content")
        expected_id = deterministic_contract_id(
            "domain-design-context",
            {"design_context_hash": self.design_context_hash},
        )
        if self.domain_design_context_id != expected_id:
            raise ValueError("domain_design_context_id does not match deterministic seed")
        _validate_identity(
            self.identity,
            contract_kind="l1.domain_design_context",
            content_hash=self.design_context_hash,
        )
        if (
            self.identity.prompt_version != self.prompt_version
            or self.identity.prompt_hash != self.prompt_hash
            or self.identity.model_version != self.model_version
            or self.identity.model_hash != self.model_hash
        ):
            raise ValueError(
                "design context prompt/model fields must equal its C0 identity"
            )
        return self


class DomainApprovalContext(ContractModel):
    identity: CanonicalIdentityEnvelope
    contract_version: Literal["1.0.0"] = "1.0.0"
    domain_approval_context_id: RequiredText
    domain_design_context_id: RequiredText
    domain_design_context_hash: Sha256
    domain_proposal_id: RequiredText
    domain_proposal_hash: Sha256
    source_profile_id: RequiredText
    source_profile_hash: Sha256
    input_manifest_id: RequiredText
    input_manifest_hash: Sha256
    source_corpus_manifest_id: RequiredText
    source_corpus_manifest_hash: Sha256
    design_sample_manifest_id: RequiredText
    design_sample_manifest_hash: Sha256
    domain_contract_hash: Sha256
    hierarchy_hash: Sha256
    identity_policy_hash: Sha256
    completeness_requirement_hash: Sha256
    external_reference_decision_hash: Sha256
    summary_hash: Sha256
    decision: Literal["approve", "correct", "abort"]
    actor: RequiredText
    correction_text: RequiredText | None = None
    correction_hash: Sha256 | None = None
    decided_at_utc: datetime
    approval_context_hash: Sha256

    _utc = field_validator("decided_at_utc")(utc_timestamp)

    @model_validator(mode="after")
    def _invariants(self) -> "DomainApprovalContext":
        if self.decision == "correct":
            if self.correction_text is None or self.correction_hash is None:
                raise ValueError("correct decisions require correction text and hash")
            if canonical_sha256(self.correction_text) != self.correction_hash:
                raise ValueError("correction_hash does not match correction text")
        elif self.correction_text is not None or self.correction_hash is not None:
            raise ValueError("only correct decisions may contain correction fields")
        values = self.model_dump(
            mode="json",
            exclude={
                "identity",
                "domain_approval_context_id",
                "approval_context_hash",
                "decided_at_utc",
            },
        )
        expected_hash = canonical_sha256(values)
        if self.approval_context_hash != expected_hash:
            raise ValueError("approval_context_hash does not match decision content")
        expected_id = deterministic_contract_id(
            "domain-approval-context",
            {"approval_context_hash": self.approval_context_hash},
        )
        if self.domain_approval_context_id != expected_id:
            raise ValueError("domain_approval_context_id does not match deterministic seed")
        _validate_identity(
            self.identity,
            contract_kind="l1.domain_approval_context",
            content_hash=self.approval_context_hash,
        )
        if self.identity.domain_contract_hash != self.domain_contract_hash:
            raise ValueError("approval identity must bind the domain contract hash")
        return self


def l1_semantic_hash(
    model_type: type[ContractModel],
    values: dict[str, Any],
    *,
    hash_field: str,
    excluded_fields: set[str],
) -> str:
    """Compute an L1 semantic hash before constructing its frozen model."""
    del model_type, hash_field
    return canonical_sha256(
        {key: value for key, value in values.items() if key not in excluded_fields}
    )


def draft_contract_hash(contract: DomainContractV2) -> str:
    """Expose the domain authority hash without introducing another algorithm."""
    from .service import compute_contract_hash

    return compute_contract_hash(contract)
