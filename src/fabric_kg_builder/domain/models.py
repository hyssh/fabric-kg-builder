"""Pydantic models for versioned domain contracts and review artifacts."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DOMAIN_SCHEMA_VERSION = "1.0"
DOMAIN_SCHEMA_V2_VERSION = "2.0"


def _coerce_string_list(value: object) -> object:
    """Accept either a scalar string or a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, list):
        cleaned: list[str] = []
        for item in value:
            if not isinstance(item, str):
                return value
            stripped = item.strip()
            if stripped:
                cleaned.append(stripped)
        return cleaned
    return value


class StrictModel(BaseModel):
    """Base model that rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")


class DomainSection(StrictModel):
    """Top-level domain description."""

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    subdomains: list[str] = Field(default_factory=list)

    _coerce_subdomains = field_validator("subdomains", mode="before")(
        _coerce_string_list
    )


class BusinessSection(StrictModel):
    """Business and organizational context."""

    organization_context: str = Field(min_length=1)
    users: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)

    _coerce_users = field_validator("users", mode="before")(_coerce_string_list)
    _coerce_decisions = field_validator("decisions", mode="before")(
        _coerce_string_list
    )


class ProblemSection(StrictModel):
    """Problem statement and scoping."""

    statement: str = Field(min_length=1)
    desired_outcomes: list[str] = Field(default_factory=list)
    in_scope: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)

    _coerce_desired_outcomes = field_validator("desired_outcomes", mode="before")(
        _coerce_string_list
    )
    _coerce_in_scope = field_validator("in_scope", mode="before")(
        _coerce_string_list
    )
    _coerce_out_of_scope = field_validator("out_of_scope", mode="before")(
        _coerce_string_list
    )


class CanonicalTerm(StrictModel):
    """Preferred business terminology."""

    term: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    synonyms: list[str] = Field(default_factory=list)

    _coerce_synonyms = field_validator("synonyms", mode="before")(
        _coerce_string_list
    )


class AmbiguousTerm(StrictModel):
    """Ambiguous or overloaded term."""

    term: str = Field(min_length=1)
    meanings: list[str] = Field(default_factory=list)

    _coerce_meanings = field_validator("meanings", mode="before")(
        _coerce_string_list
    )


class TerminologySection(StrictModel):
    """Domain terminology catalogue."""

    canonical_terms: list[CanonicalTerm] = Field(default_factory=list)
    ambiguous_terms: list[AmbiguousTerm] = Field(default_factory=list)


class CandidateModelSection(StrictModel):
    """Candidate entity and relationship categories."""

    entity_categories: list[str] = Field(default_factory=list)
    relationship_categories: list[str] = Field(default_factory=list)

    _coerce_entity_categories = field_validator(
        "entity_categories", mode="before"
    )(_coerce_string_list)
    _coerce_relationship_categories = field_validator(
        "relationship_categories", mode="before"
    )(_coerce_string_list)


class ConstraintsSection(StrictModel):
    """Temporal, regulatory, privacy, and safety constraints."""

    temporal: list[str] = Field(default_factory=list)
    regulatory: list[str] = Field(default_factory=list)
    privacy: list[str] = Field(default_factory=list)
    safety: list[str] = Field(default_factory=list)

    _coerce_temporal = field_validator("temporal", mode="before")(
        _coerce_string_list
    )
    _coerce_regulatory = field_validator("regulatory", mode="before")(
        _coerce_string_list
    )
    _coerce_privacy = field_validator("privacy", mode="before")(
        _coerce_string_list
    )
    _coerce_safety = field_validator("safety", mode="before")(_coerce_string_list)


class PositiveExample(StrictModel):
    """Representative positive example."""

    text: str = Field(min_length=1)
    expected: list[str] = Field(default_factory=list)

    _coerce_expected = field_validator("expected", mode="before")(_coerce_string_list)


class NegativeExample(StrictModel):
    """Representative negative example."""

    text: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ExamplesSection(StrictModel):
    """Positive and negative examples."""

    positive: list[PositiveExample] = Field(default_factory=list)
    negative: list[NegativeExample] = Field(default_factory=list)


class ApprovalMetadata(StrictModel):
    """Approval state stored with the domain contract."""

    status: Literal["draft", "needs_review", "approved"] = "draft"
    approved_by: str | None = None
    approved_at_utc: str | None = None
    contract_hash: str | None = None
    schema_version: str | None = None
    prompt_version: str | None = None
    model_version: str | None = None
    notes: list[str] = Field(default_factory=list)

    _coerce_notes = field_validator("notes", mode="before")(_coerce_string_list)


class DomainContract(StrictModel):
    """Approved YAML source-of-truth for domain intent."""

    schema_version: Literal[DOMAIN_SCHEMA_VERSION] = DOMAIN_SCHEMA_VERSION
    domain: DomainSection
    business: BusinessSection
    problem: ProblemSection
    competency_questions: list[str] = Field(default_factory=list)
    terminology: TerminologySection
    candidate_model: CandidateModelSection
    constraints: ConstraintsSection
    examples: ExamplesSection
    approval: ApprovalMetadata = Field(default_factory=ApprovalMetadata)

    _coerce_competency_questions = field_validator(
        "competency_questions", mode="before"
    )(_coerce_string_list)


class V2StrictModel(BaseModel):
    """Strict immutable schema-2.0 model."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


V2RequiredText: TypeAlias = Annotated[str, Field(min_length=1, pattern=r"\S")]
Sha256Text: TypeAlias = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SemanticTypeId: TypeAlias = Annotated[
    str, Field(pattern=r"^semantic-type:[a-z0-9][a-z0-9._:-]*$")
]
RelationshipTypeId: TypeAlias = Annotated[
    str, Field(pattern=r"^relationship-type:[a-z0-9][a-z0-9._:-]*$")
]
EvidenceSpanId: TypeAlias = Annotated[
    str, Field(pattern=r"^evidence-span:[0-9a-f]{32}$")
]
CompetencyQuestionId: TypeAlias = Annotated[
    str, Field(pattern=r"^cq:[a-z0-9][a-z0-9._:-]*$")
]


def _sorted_unique_strings(value: object) -> object:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sorted(set(value))
    return value


class DomainSectionV2(V2StrictModel):
    name: V2RequiredText
    description: V2RequiredText
    subdomains: list[V2RequiredText] = Field(default_factory=list)


class BusinessSectionV2(V2StrictModel):
    organization_context: V2RequiredText
    users: list[V2RequiredText] = Field(min_length=1)
    decisions: list[V2RequiredText] = Field(min_length=1)


class ProblemSectionV2(V2StrictModel):
    statement: V2RequiredText
    desired_outcomes: list[V2RequiredText] = Field(min_length=1)
    in_scope: list[V2RequiredText] = Field(min_length=1)
    out_of_scope: list[V2RequiredText] = Field(default_factory=list)


class CanonicalTermV2(V2StrictModel):
    term: V2RequiredText
    definition: V2RequiredText
    synonyms: list[V2RequiredText] = Field(default_factory=list)


class AmbiguousTermV2(V2StrictModel):
    term: V2RequiredText
    meanings: list[V2RequiredText] = Field(min_length=1)


class TerminologySectionV2(V2StrictModel):
    canonical_terms: list[CanonicalTermV2] = Field(default_factory=list)
    ambiguous_terms: list[AmbiguousTermV2] = Field(default_factory=list)


class ConstraintsSectionV2(V2StrictModel):
    temporal: list[V2RequiredText] = Field(default_factory=list)
    regulatory: list[V2RequiredText] = Field(default_factory=list)
    privacy: list[V2RequiredText] = Field(default_factory=list)
    safety: list[V2RequiredText] = Field(default_factory=list)


class PositiveExampleV2(V2StrictModel):
    text: V2RequiredText
    expected: list[V2RequiredText] = Field(default_factory=list)


class NegativeExampleV2(V2StrictModel):
    text: V2RequiredText
    reason: V2RequiredText


class ExamplesSectionV2(V2StrictModel):
    positive: list[PositiveExampleV2] = Field(default_factory=list)
    negative: list[NegativeExampleV2] = Field(default_factory=list)


class IdentityKeyPolicyV2(V2StrictModel):
    authority: V2RequiredText
    namespace: V2RequiredText
    key_mode: Literal["business_key", "stable_source_identity"]
    business_key_fields: list[V2RequiredText] = Field(default_factory=list)
    normalization_version: V2RequiredText
    collision_behavior: Literal["block", "unresolved"]
    missing_key_behavior: Literal["block", "unresolved"]
    type_independent: Literal[True] = True

    _sort_fields = field_validator("business_key_fields", mode="before")(
        _sorted_unique_strings
    )

    @model_validator(mode="after")
    def _validate_key_mode(self) -> "IdentityKeyPolicyV2":
        if self.key_mode == "business_key" and not self.business_key_fields:
            raise ValueError("business_key identity requires business_key_fields")
        if self.key_mode == "stable_source_identity" and self.business_key_fields:
            raise ValueError(
                "stable_source_identity cannot declare business_key_fields"
            )
        return self


class DomainPropertyV2(V2StrictModel):
    property_id: str = Field(pattern=r"^property:[a-z0-9][a-z0-9._:-]*$")
    display_name: V2RequiredText
    value_type: Literal["string", "integer", "number", "boolean", "date", "datetime"]
    required: bool = False


class DomainConstraintV2(V2StrictModel):
    constraint_id: str = Field(pattern=r"^constraint:[a-z0-9][a-z0-9._:-]*$")
    expression: V2RequiredText
    severity: Literal["error", "warning"] = "error"


class GeneralizationBasisV2(V2StrictModel):
    competency_question_ids: list[CompetencyQuestionId] = Field(default_factory=list)
    evidence_span_ids: list[EvidenceSpanId] = Field(default_factory=list)
    governance_rationale: V2RequiredText | None = None

    _sort_cqs = field_validator("competency_question_ids", mode="before")(
        _sorted_unique_strings
    )
    _sort_evidence = field_validator("evidence_span_ids", mode="before")(
        _sorted_unique_strings
    )

    @model_validator(mode="after")
    def _require_basis(self) -> "GeneralizationBasisV2":
        if (
            not self.competency_question_ids
            and not self.evidence_span_ids
            and self.governance_rationale is None
        ):
            raise ValueError(
                "generalization requires competency, exact evidence, or governance support"
            )
        return self


class SiblingClassificationPolicyV2(V2StrictModel):
    mode: Literal["exclusive", "overlap_allowed", "discriminator", "unresolved"]
    discriminator_property_id: str | None = Field(
        default=None,
        pattern=r"^property:[a-z0-9][a-z0-9._:-]*$",
    )
    rationale: V2RequiredText

    @model_validator(mode="after")
    def _validate_discriminator(self) -> "SiblingClassificationPolicyV2":
        if (self.mode == "discriminator") != (
            self.discriminator_property_id is not None
        ):
            raise ValueError(
                "discriminator mode and discriminator_property_id must be paired"
            )
        return self


class DomainEntityTypeV2(V2StrictModel):
    """Stable semantic type independent of labels and physical projections."""

    type_id: SemanticTypeId
    semantic_key: V2RequiredText
    display_name: V2RequiredText
    description: V2RequiredText
    aliases: list[V2RequiredText] = Field(default_factory=list)
    classification: Literal["common", "domain", "domain_specialization"]
    parent_type_id: SemanticTypeId | None = None
    abstract: bool = False
    identity_root_type_id: SemanticTypeId
    identity_key_policy: IdentityKeyPolicyV2 | None = None
    declared_properties: list[DomainPropertyV2] = Field(default_factory=list)
    declared_constraints: list[DomainConstraintV2] = Field(default_factory=list)
    sibling_classification_policy: SiblingClassificationPolicyV2
    generalization_basis: GeneralizationBasisV2 | None = None
    evidence_span_ids: list[EvidenceSpanId] = Field(default_factory=list)
    competency_question_ids: list[CompetencyQuestionId] = Field(default_factory=list)
    governance_rationale: V2RequiredText | None = None
    tombstoned: bool = False

    _sort_aliases = field_validator("aliases", mode="before")(_sorted_unique_strings)
    _sort_evidence = field_validator("evidence_span_ids", mode="before")(
        _sorted_unique_strings
    )
    _sort_cqs = field_validator("competency_question_ids", mode="before")(
        _sorted_unique_strings
    )

    @model_validator(mode="after")
    def _validate_support(self) -> "DomainEntityTypeV2":
        if self.parent_type_id is None and self.generalization_basis is not None:
            raise ValueError("root types cannot declare a generalization basis")
        if self.parent_type_id is not None and self.generalization_basis is None:
            raise ValueError("child types require a reviewed generalization basis")
        if (
            not self.evidence_span_ids
            and not self.competency_question_ids
            and self.governance_rationale is None
        ):
            raise ValueError(
                "[DOM-102] Semantic types require evidence, competency, or governance support"
            )
        return self


class RelationshipIdentityPolicyV2(V2StrictModel):
    seed_fields: list[
        Literal[
            "predicate_id",
            "source_entity_id",
            "target_entity_id",
            "governed_context",
        ]
    ] = Field(
        default_factory=lambda: [
            "predicate_id",
            "source_entity_id",
            "target_entity_id",
            "governed_context",
        ]
    )
    excludes_display_labels: Literal[True] = True
    excludes_endpoint_type_labels: Literal[True] = True
    context_policy: V2RequiredText

    @model_validator(mode="after")
    def _fixed_seed(self) -> "RelationshipIdentityPolicyV2":
        required = {
            "predicate_id",
            "source_entity_id",
            "target_entity_id",
            "governed_context",
        }
        if set(self.seed_fields) != required or len(self.seed_fields) != len(required):
            raise ValueError("relationship identity seed fields are fixed")
        return self


class DomainRelationshipTypeV2(V2StrictModel):
    relationship_type_id: RelationshipTypeId
    predicate_id: str = Field(pattern=r"^predicate:[a-z0-9][a-z0-9._:-]*$")
    display_name: V2RequiredText
    description: V2RequiredText
    source_type_ids: list[SemanticTypeId] = Field(min_length=1)
    target_type_ids: list[SemanticTypeId] = Field(min_length=1)
    direction: Literal["source_to_target"] = "source_to_target"
    endpoint_policy: Literal["allow_subtypes", "exact"] = "allow_subtypes"
    evidence_policy: Literal["exact_span_required"] = "exact_span_required"
    publication_policy: Literal["asserted_only"] = "asserted_only"
    identity_policy: RelationshipIdentityPolicyV2
    competency_question_ids: list[CompetencyQuestionId] = Field(default_factory=list)
    governance_rationale: V2RequiredText | None = None
    evidence_span_ids: list[EvidenceSpanId] = Field(default_factory=list)

    _sort_sources = field_validator("source_type_ids", mode="before")(
        _sorted_unique_strings
    )
    _sort_targets = field_validator("target_type_ids", mode="before")(
        _sorted_unique_strings
    )
    _sort_cqs = field_validator("competency_question_ids", mode="before")(
        _sorted_unique_strings
    )
    _sort_evidence = field_validator("evidence_span_ids", mode="before")(
        _sorted_unique_strings
    )

    @model_validator(mode="after")
    def _validate_support(self) -> "DomainRelationshipTypeV2":
        if not self.competency_question_ids and self.governance_rationale is None:
            raise ValueError(
                "[DOM-102] Relationship types require competency or governance support"
            )
        if not self.evidence_span_ids and self.governance_rationale is None:
            raise ValueError(
                "[DOM-102] Relationship types require exact evidence or governance support"
            )
        return self


class CandidateModelSectionV2(V2StrictModel):
    entity_types: list[DomainEntityTypeV2] = Field(min_length=1)
    relationship_types: list[DomainRelationshipTypeV2] = Field(min_length=1)


class CompetencyQuestionV2(V2StrictModel):
    id: CompetencyQuestionId
    question: str = Field(min_length=15, pattern=r"\S")
    business_critical: bool = True


class QuestionPathStepV2(V2StrictModel):
    from_type_id: SemanticTypeId
    relationship_type_id: RelationshipTypeId
    to_type_id: SemanticTypeId
    traversal: Literal["forward", "reverse"]
    evidence_span_ids: list[EvidenceSpanId] = Field(default_factory=list)

    _sort_evidence = field_validator("evidence_span_ids", mode="before")(
        _sorted_unique_strings
    )


class QuestionPlanV2(V2StrictModel):
    question_id: CompetencyQuestionId
    required_path: list[QuestionPathStepV2] = Field(default_factory=list)
    hop_count: int = Field(ge=0, le=4)
    covered: bool
    shortest_path: Literal[True] = True
    unsupported_reason: V2RequiredText | None = None

    @model_validator(mode="after")
    def _validate_path_shape(self) -> "QuestionPlanV2":
        if self.hop_count != len(self.required_path):
            raise ValueError("hop_count must equal required_path length")
        if self.covered != bool(self.required_path):
            raise ValueError("covered questions require a path; unsupported questions cannot")
        if not self.covered and self.unsupported_reason is None:
            raise ValueError("unsupported questions require unsupported_reason")
        for left, right in zip(self.required_path, self.required_path[1:]):
            if left.to_type_id != right.from_type_id:
                raise ValueError("consecutive path steps must connect")
        return self


class K4RationaleV2(V2StrictModel):
    question_id: CompetencyQuestionId
    hop_relationship_type_ids: list[RelationshipTypeId] = Field(
        min_length=4, max_length=4
    )
    evidence_span_ids: list[EvidenceSpanId] = Field(min_length=4)
    rationale: V2RequiredText


class ReasoningPolicyV2(V2StrictModel):
    relationship_type_count: int = Field(ge=1, le=24)
    recommended_relationship_type_range: list[int] = Field(
        default_factory=lambda: [8, 20],
        min_length=2,
        max_length=2,
    )
    max_relationship_types: Literal[24] = 24
    retained_type_rationales: dict[RelationshipTypeId, list[V2RequiredText]] = Field(
        default_factory=dict
    )
    max_hops: int = Field(ge=1, le=4)
    absolute_max_hops: Literal[4] = 4
    k4_rationales: list[K4RationaleV2] = Field(default_factory=list)
    max_relations_per_work_unit: int = Field(default=25, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _validate_raw_limits(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        relationship_count = data.get("relationship_type_count")
        if (
            isinstance(relationship_count, int)
            and not isinstance(relationship_count, bool)
            and not 1 <= relationship_count <= 24
        ):
            raise ValueError("[DOM-103] N must be between 1 and 24")
        max_hops = data.get("max_hops")
        if (
            isinstance(max_hops, int)
            and not isinstance(max_hops, bool)
            and not 1 <= max_hops <= 4
        ):
            raise ValueError("[DOM-105] K must be between 1 and 4")
        return data

    @model_validator(mode="after")
    def _validate_bounds(self) -> "ReasoningPolicyV2":
        if self.recommended_relationship_type_range != [8, 20]:
            raise ValueError("recommended relationship range must remain [8, 20]")
        if self.relationship_type_count <= 20 and self.retained_type_rationales:
            raise ValueError("retained_type_rationales are only used for N=21..24")
        if self.relationship_type_count > 20 and not self.retained_type_rationales:
            raise ValueError("[DOM-103] N=21..24 requires per-type rationales")
        if self.max_hops == 4 and not self.k4_rationales:
            raise ValueError("[DOM-105] K=4 requires exact cited rationale")
        if self.max_hops < 4 and self.k4_rationales:
            raise ValueError("K4 rationales are invalid when K is below four")
        return self


class RequiredRoleV2(V2StrictModel):
    role_id: str = Field(pattern=r"^role:[a-z0-9][a-z0-9._:-]*$")
    relationship_type_id: RelationshipTypeId
    allowed_target_type_ids: list[SemanticTypeId] = Field(min_length=1)
    satisfaction: Literal["one_allowed_type", "all_allowed_types"]

    _sort_types = field_validator("allowed_target_type_ids", mode="before")(
        _sorted_unique_strings
    )


class RequiredRoleCoverageV2(V2StrictModel):
    mode: Literal["all_of"] = "all_of"
    roles: list[RequiredRoleV2] = Field(min_length=1)


class OrderingPolicyV2(V2StrictModel):
    mode: Literal["unordered", "ordered"]
    ordinal_property_id: str | None = Field(
        default=None, pattern=r"^property:[a-z0-9][a-z0-9._:-]*$"
    )
    ordinal_value_type: Literal["integer"] | None = None
    direction: Literal["ascending", "descending"] | None = None
    unique_ordinals: bool | None = None
    contiguous: bool | None = None

    @model_validator(mode="after")
    def _ordered_fields(self) -> "OrderingPolicyV2":
        values = (
            self.ordinal_property_id,
            self.ordinal_value_type,
            self.direction,
            self.unique_ordinals,
        )
        if self.mode == "ordered" and any(value is None for value in values):
            raise ValueError("ordered collections require complete ordinal semantics")
        if self.mode == "unordered" and any(
            value is not None for value in values + (self.contiguous,)
        ):
            raise ValueError("unordered collections cannot declare ordinal semantics")
        return self


class CardinalityExpectationV2(V2StrictModel):
    expected_count: int | None = Field(default=None, ge=0)
    minimum_count: int | None = Field(default=None, ge=0)
    maximum_count: int | None = Field(default=None, ge=0)
    count_basis: Literal["distinct_members_per_aggregate"] = (
        "distinct_members_per_aggregate"
    )
    source_kind: Literal[
        "competency_question", "source_evidence", "governance_rule"
    ]
    source_question_ids: list[CompetencyQuestionId] = Field(default_factory=list)
    source_evidence_span_ids: list[EvidenceSpanId] = Field(default_factory=list)
    reviewed_rationale: V2RequiredText | None = None

    @model_validator(mode="after")
    def _validate_bounds_and_source(self) -> "CardinalityExpectationV2":
        if (
            self.expected_count is None
            and self.minimum_count is None
            and self.maximum_count is None
        ):
            raise ValueError("cardinality expectation must declare a supported bound")
        if (
            self.minimum_count is not None
            and self.maximum_count is not None
            and self.minimum_count > self.maximum_count
        ):
            raise ValueError("minimum_count cannot exceed maximum_count")
        if self.expected_count is not None:
            if (
                self.minimum_count is not None
                and self.expected_count < self.minimum_count
            ) or (
                self.maximum_count is not None
                and self.expected_count > self.maximum_count
            ):
                raise ValueError("expected_count must be inside min/max bounds")
        if self.source_kind == "source_evidence" and not self.source_evidence_span_ids:
            raise ValueError("source-supported cardinality requires exact evidence")
        if self.source_kind == "competency_question" and not self.source_question_ids:
            raise ValueError("question-supported cardinality requires question IDs")
        if self.source_kind != "source_evidence" and self.reviewed_rationale is None:
            raise ValueError("business/governance cardinality requires reviewed rationale")
        return self


class CollectionIdentityPolicyV2(V2StrictModel):
    aggregate_identity_included: Literal[True] = True
    membership_relationship_included: Literal[True] = True
    member_identities_included: Literal[True] = True
    member_roles_included: bool
    ordinals_included: bool
    preserve_member_order: bool
    hash_algorithm: Literal["sha256"] = "sha256"


class StructuredFactSetV2(V2StrictModel):
    aggregate_type_id: SemanticTypeId
    membership_relationship_type_id: RelationshipTypeId
    allowed_member_type_ids: list[SemanticTypeId] = Field(min_length=1)
    member_role_ids: list[str] = Field(default_factory=list)
    ordering_policy: OrderingPolicyV2
    cardinality: CardinalityExpectationV2 | None = None
    collection_identity_policy: CollectionIdentityPolicyV2
    membership_source_kind: Literal[
        "competency_question", "source_evidence", "governance_rule"
    ]
    membership_evidence_span_ids: list[EvidenceSpanId] = Field(default_factory=list)
    membership_rationale: V2RequiredText

    _sort_types = field_validator("allowed_member_type_ids", mode="before")(
        _sorted_unique_strings
    )
    _sort_roles = field_validator("member_role_ids", mode="before")(
        _sorted_unique_strings
    )

    @model_validator(mode="after")
    def _identity_matches_ordering(self) -> "StructuredFactSetV2":
        ordered = self.ordering_policy.mode == "ordered"
        policy = self.collection_identity_policy
        if policy.ordinals_included != ordered or policy.preserve_member_order != ordered:
            raise ValueError("collection identity must agree with ordering semantics")
        if policy.member_roles_included != bool(self.member_role_ids):
            raise ValueError("collection identity must agree with member roles")
        if (
            self.membership_source_kind == "source_evidence"
            and not self.membership_evidence_span_ids
        ):
            raise ValueError("source-supported membership requires exact evidence")
        return self


class CompletenessRequirementV2(V2StrictModel):
    requirement_id: str = Field(
        pattern=r"^completeness-requirement:[a-z0-9][a-z0-9._:-]*$"
    )
    competency_question_ids: list[CompetencyQuestionId] = Field(min_length=1)
    requirement_kind: Literal["required_role_set", "structured_fact_set"]
    scope_type_id: SemanticTypeId
    scoped_subtype_id: SemanticTypeId | None = None
    scoped_filter: V2RequiredText | None = None
    rationale: V2RequiredText
    source_kind: Literal[
        "competency_question", "source_evidence", "governance_rule"
    ]
    source_question_ids: list[CompetencyQuestionId] = Field(default_factory=list)
    governance_references: list[V2RequiredText] = Field(default_factory=list)
    evidence_span_ids: list[EvidenceSpanId] = Field(default_factory=list)
    coverage_status: Literal["covered", "unsupported"]
    unsupported_reason: V2RequiredText | None = None
    required_roles: RequiredRoleCoverageV2 | None = None
    structured_fact_set: StructuredFactSetV2 | None = None

    @model_validator(mode="after")
    def _validate_kind_and_coverage(self) -> "CompletenessRequirementV2":
        if (self.requirement_kind == "required_role_set") != (
            self.required_roles is not None
        ):
            raise ValueError("required_role_set requires required_roles only")
        if (self.requirement_kind == "structured_fact_set") != (
            self.structured_fact_set is not None
        ):
            raise ValueError("structured_fact_set requires structured_fact_set only")
        if (self.coverage_status == "unsupported") != (
            self.unsupported_reason is not None
        ):
            raise ValueError("unsupported coverage requires exactly one reason")
        if self.source_kind == "source_evidence" and not self.evidence_span_ids:
            raise ValueError("source-supported requirement requires exact evidence")
        return self


class CompletenessQuestionCoverageV2(V2StrictModel):
    question_id: CompetencyQuestionId
    requirement_ids: list[str] = Field(default_factory=list)
    covered_role_ids: list[str] = Field(default_factory=list)
    missing_role_ids: list[str] = Field(default_factory=list)
    coverage_status: Literal["covered", "unsupported"]
    unsupported_reason: V2RequiredText | None = None

    @model_validator(mode="after")
    def _coverage_reason(self) -> "CompletenessQuestionCoverageV2":
        if (self.coverage_status == "unsupported") != (
            self.unsupported_reason is not None
        ):
            raise ValueError("unsupported completeness coverage requires a reason")
        if self.coverage_status == "covered" and self.missing_role_ids:
            raise ValueError("covered completeness cannot have missing roles")
        return self


class TypeHierarchyClosureV2(V2StrictModel):
    direct_parent_by_type: dict[SemanticTypeId, SemanticTypeId | None]
    ancestors_by_type: dict[SemanticTypeId, list[SemanticTypeId]]
    descendants_by_type: dict[SemanticTypeId, list[SemanticTypeId]]
    effective_property_ids_by_type: dict[SemanticTypeId, list[str]]
    effective_constraint_ids_by_type: dict[SemanticTypeId, list[str]]
    compatible_source_type_ids_by_relationship: dict[
        RelationshipTypeId, list[SemanticTypeId]
    ]
    compatible_target_type_ids_by_relationship: dict[
        RelationshipTypeId, list[SemanticTypeId]
    ]
    hierarchy_hash: Sha256Text


class ApprovedExternalSemanticReferenceV2(V2StrictModel):
    reference_id: str = Field(
        pattern=r"^external-reference:[a-z0-9][a-z0-9._:-]*$"
    )
    source_uri: V2RequiredText
    version: V2RequiredText
    content_hash: Sha256Text
    retrieved_at_utc: V2RequiredText
    provenance: V2RequiredText
    license_classification: V2RequiredText
    allowed_use_decision: Literal["approved"]
    reviewer: V2RequiredText
    approval_reference: V2RequiredText
    semantic_target_ids: list[V2RequiredText] = Field(min_length=1)
    evidence_span_ids: list[EvidenceSpanId] = Field(default_factory=list)
    rationale: V2RequiredText


class ExtractionPolicyV2(V2StrictModel):
    vocabulary_mode: Literal["closed"] = "closed"
    exact_evidence_span_required: Literal[True] = True
    abstain_without_evidence: Literal[True] = True
    allow_subtype_endpoints: bool = True
    unknown_observation_policy: Literal["audit_and_request_rereview"] = (
        "audit_and_request_rereview"
    )


class PublicationPolicyV2(V2StrictModel):
    included_states: list[Literal["asserted"]] = Field(
        default_factory=lambda: ["asserted"]
    )
    excluded_states: list[Literal["unresolved", "rejected"]] = Field(
        default_factory=lambda: ["unresolved", "rejected"]
    )
    source_table: Literal["semantic_relationships"] = "semantic_relationships"

    @model_validator(mode="after")
    def _validate_states(self) -> "PublicationPolicyV2":
        if self.included_states != ["asserted"]:
            raise ValueError("schema 2.0 publishes asserted relationships only")
        if self.excluded_states != ["unresolved", "rejected"]:
            raise ValueError("excluded states must be canonically ordered")
        return self


class DomainDriftPolicyV2(V2StrictModel):
    triggers: list[
        Literal[
            "corpus_manifest_changed",
            "sustained_unresolved_semantic_observation",
            "competency_question_coverage_failed",
            "identity_collision",
            "cardinality_or_order_failed",
            "external_reference_version_changed",
            "governance_changed",
        ]
    ] = Field(min_length=1)
    signal: Literal["DOMAIN_REREVIEW_REQUESTED"] = "DOMAIN_REREVIEW_REQUESTED"
    automatic_schema_mutation: Literal[False] = False


class ApprovalMetadataV2(V2StrictModel):
    status: Literal["draft", "needs_review", "approved"] = "draft"
    approved_by: V2RequiredText | None = None
    approved_at_utc: V2RequiredText | None = None
    contract_hash: Sha256Text | None = None
    domain_approval_context_id: V2RequiredText | None = None
    domain_approval_context_hash: Sha256Text | None = None
    notes: list[V2RequiredText] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_approved_metadata(self) -> "ApprovalMetadataV2":
        values = (
            self.approved_by,
            self.approved_at_utc,
            self.contract_hash,
            self.domain_approval_context_id,
            self.domain_approval_context_hash,
        )
        if self.status == "approved" and any(value is None for value in values):
            raise ValueError(
                "approved schema-2.0 metadata requires approval-context bindings"
            )
        if self.status != "approved" and any(value is not None for value in values):
            raise ValueError("draft schema-2.0 metadata cannot carry approval bindings")
        return self


class DomainContractV2(V2StrictModel):
    """New-project-only domain authority sealed by L1 approval."""

    schema_version: Literal[DOMAIN_SCHEMA_V2_VERSION]
    domain: DomainSectionV2
    business: BusinessSectionV2
    problem: ProblemSectionV2
    competency_questions: list[CompetencyQuestionV2] = Field(
        min_length=5, max_length=10
    )
    terminology: TerminologySectionV2
    candidate_model: CandidateModelSectionV2
    constraints: ConstraintsSectionV2
    examples: ExamplesSectionV2
    completeness_requirements: list[CompletenessRequirementV2] = Field(
        default_factory=list
    )
    completeness_question_coverage: list[CompletenessQuestionCoverageV2]
    hierarchy_closure: TypeHierarchyClosureV2
    identity_policy_hash: Sha256Text
    completeness_requirement_hash: Sha256Text
    approved_external_references: list[ApprovedExternalSemanticReferenceV2] = Field(
        default_factory=list
    )
    external_reference_decision_hash: Sha256Text
    reasoning_policy: ReasoningPolicyV2
    extraction_policy: ExtractionPolicyV2 = Field(default_factory=ExtractionPolicyV2)
    publication_policy: PublicationPolicyV2 = Field(
        default_factory=PublicationPolicyV2
    )
    question_plans: list[QuestionPlanV2]
    drift_policy: DomainDriftPolicyV2
    approval: ApprovalMetadataV2 = Field(default_factory=ApprovalMetadataV2)

    @model_validator(mode="before")
    @classmethod
    def _validate_question_count(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        questions = data.get("competency_questions")
        if isinstance(questions, list) and not 5 <= len(questions) <= 10:
            raise ValueError(
                "[DOM-101] Schema 2.0 requires five to ten competency questions"
            )
        return data

    @model_validator(mode="after")
    def _validate_domain_design(self) -> "DomainContractV2":
        from fabric_kg_builder.contracts.base import canonical_sha256
        from fabric_kg_builder.domain.hierarchy import (
            build_type_hierarchy_closure,
            validate_relationship_endpoint_compatibility,
        )

        entities = self.candidate_model.entity_types
        relationships = self.candidate_model.relationship_types
        entity_ids = [item.type_id for item in entities]
        semantic_keys = [item.semantic_key for item in entities]
        relationship_ids = [item.relationship_type_id for item in relationships]
        predicate_ids = [item.predicate_id for item in relationships]
        question_ids = [item.id for item in self.competency_questions]
        plan_question_ids = [item.question_id for item in self.question_plans]
        coverage_question_ids = [
            item.question_id for item in self.completeness_question_coverage
        ]

        for label, values in (
            ("semantic type ID", entity_ids),
            ("semantic key", semantic_keys),
            ("relationship type ID", relationship_ids),
            ("predicate ID", predicate_ids),
            ("competency question ID", question_ids),
            ("question plan ID", plan_question_ids),
            ("completeness coverage question ID", coverage_question_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"[DOM-106] Duplicate {label}")

        known_entities = set(entity_ids)
        known_questions = set(question_ids)
        known_relationships = set(relationship_ids)
        for relationship in relationships:
            unknown_endpoints = (
                set(relationship.source_type_ids)
                | set(relationship.target_type_ids)
            ) - known_entities
            if unknown_endpoints:
                raise ValueError(
                    f"[DOM-106] Unknown relationship endpoints: {sorted(unknown_endpoints)}"
                )
            unknown_questions = (
                set(relationship.competency_question_ids) - known_questions
            )
            if unknown_questions:
                raise ValueError(
                    f"[DOM-102] Unknown relationship questions: {sorted(unknown_questions)}"
                )

        expected_closure = build_type_hierarchy_closure(entities, relationships)
        if self.hierarchy_closure != expected_closure:
            raise ValueError("[DOM-106] hierarchy closure/hash is stale or incorrect")
        validate_relationship_endpoint_compatibility(
            relationships, self.hierarchy_closure
        )

        roots = [item for item in entities if item.parent_type_id is None]
        for root in roots:
            if root.identity_root_type_id != root.type_id:
                raise ValueError("root type must identify itself as identity root")
            if root.identity_key_policy is None:
                raise ValueError("every hierarchy root requires one identity key policy")
        for entity in entities:
            if entity.parent_type_id is not None and entity.identity_key_policy is not None:
                raise ValueError("descendants inherit and cannot override root identity")
            if (
                entity.identity_root_type_id
                not in self.hierarchy_closure.ancestors_by_type[entity.type_id]
                and entity.identity_root_type_id != entity.type_id
            ):
                raise ValueError("identity_root_type_id must resolve to transitive root")

        expected_identity_hash = canonical_sha256(
            {
                item.type_id: item.identity_key_policy.model_dump(mode="json")
                for item in roots
            }
        )
        if self.identity_policy_hash != expected_identity_hash:
            raise ValueError("identity_policy_hash is stale or incorrect")

        expected_completeness_hash = canonical_sha256(
            [item.model_dump(mode="json") for item in self.completeness_requirements]
        )
        if self.completeness_requirement_hash != expected_completeness_hash:
            raise ValueError("completeness_requirement_hash is stale or incorrect")
        expected_external_hash = canonical_sha256(
            [item.model_dump(mode="json") for item in self.approved_external_references]
        )
        if self.external_reference_decision_hash != expected_external_hash:
            raise ValueError("external_reference_decision_hash is stale or incorrect")

        relationship_by_id = {
            item.relationship_type_id: item for item in relationships
        }
        adjacency_by_question = {
            question_id: {entity_id: set() for entity_id in known_entities}
            for question_id in known_questions
        }
        for relationship in relationships:
            for question_id in relationship.competency_question_ids:
                adjacency = adjacency_by_question[question_id]
                for source in relationship.source_type_ids:
                    for target in relationship.target_type_ids:
                        adjacency[source].add(target)
                        adjacency[target].add(source)

        def shortest_distance(
            adjacency: dict[str, set[str]], start: str, end: str
        ) -> int | None:
            if start == end:
                return 0
            visited = {start}
            frontier = {start}
            depth = 0
            while frontier:
                depth += 1
                next_frontier: set[str] = set()
                for node in sorted(frontier):
                    for neighbor in sorted(adjacency[node]):
                        if neighbor == end:
                            return depth
                        if neighbor not in visited:
                            visited.add(neighbor)
                            next_frontier.add(neighbor)
                frontier = next_frontier
            return None

        if set(plan_question_ids) != known_questions:
            raise ValueError("[DOM-104] exactly one path plan is required per question")
        if set(coverage_question_ids) != known_questions:
            raise ValueError(
                "[DOM-104] exactly one completeness coverage record is required per question"
            )
        if not any(plan.covered for plan in self.question_plans):
            raise ValueError("[DOM-104] at least one question must be covered")
        for plan in self.question_plans:
            for step in plan.required_path:
                relationship = relationship_by_id.get(step.relationship_type_id)
                if relationship is None:
                    raise ValueError("[DOM-106] question path references unknown relationship")
                if plan.question_id not in relationship.competency_question_ids:
                    raise ValueError(
                        "[DOM-104] path relationship is not approved for its question"
                    )
                expected_sources = (
                    relationship.source_type_ids
                    if step.traversal == "forward"
                    else relationship.target_type_ids
                )
                expected_targets = (
                    relationship.target_type_ids
                    if step.traversal == "forward"
                    else relationship.source_type_ids
                )
                if (
                    step.from_type_id not in expected_sources
                    or step.to_type_id not in expected_targets
                ):
                    raise ValueError("[DOM-106] path endpoint or direction mismatch")
            if plan.hop_count > self.reasoning_policy.max_hops:
                raise ValueError("[DOM-105] question path exceeds approved K")
            if plan.covered:
                shortest = shortest_distance(
                    adjacency_by_question[plan.question_id],
                    plan.required_path[0].from_type_id,
                    plan.required_path[-1].to_type_id,
                )
                if shortest != plan.hop_count:
                    raise ValueError("[DOM-105] question plan is not shortest")

        if self.reasoning_policy.relationship_type_count != len(relationships):
            raise ValueError("[DOM-103] N must equal approved relationship type count")
        derived_k = max(plan.hop_count for plan in self.question_plans if plan.covered)
        if self.reasoning_policy.max_hops != derived_k:
            raise ValueError("[DOM-105] K must equal maximum shortest covered path")
        if len(relationships) > 20 and set(
            self.reasoning_policy.retained_type_rationales
        ) != known_relationships:
            raise ValueError("[DOM-103] N=21..24 requires rationale for every type")
        if derived_k == 4:
            rationale_by_question = {
                item.question_id: item for item in self.reasoning_policy.k4_rationales
            }
            for plan in self.question_plans:
                if plan.hop_count != 4:
                    continue
                rationale = rationale_by_question.get(plan.question_id)
                if rationale is None:
                    raise ValueError("[DOM-105] every K=4 path requires a rationale")
                hop_ids = [
                    step.relationship_type_id for step in plan.required_path
                ]
                if rationale.hop_relationship_type_ids != hop_ids:
                    raise ValueError("[DOM-105] K=4 rationale hop IDs are stale")
                for step in plan.required_path:
                    if not step.evidence_span_ids:
                        raise ValueError("[DOM-105] every K=4 hop requires exact evidence")

        requirement_by_id = {
            item.requirement_id: item for item in self.completeness_requirements
        }
        if len(requirement_by_id) != len(self.completeness_requirements):
            raise ValueError("duplicate completeness requirement ID")
        for requirement in self.completeness_requirements:
            if requirement.scope_type_id not in known_entities:
                raise ValueError("completeness requirement references unknown scope type")
            if not set(requirement.competency_question_ids) <= known_questions:
                raise ValueError("completeness requirement references unknown question")
            if not set(requirement.source_question_ids) <= known_questions:
                raise ValueError(
                    "completeness source references an unknown question"
                )
            if requirement.required_roles is not None:
                for role in requirement.required_roles.roles:
                    relationship = relationship_by_id.get(role.relationship_type_id)
                    if relationship is None:
                        raise ValueError("required role references unknown relationship")
                    if not set(role.allowed_target_type_ids) <= known_entities:
                        raise ValueError("required role references unknown target type")
                    if requirement.scope_type_id not in relationship.source_type_ids:
                        raise ValueError("required role scope endpoint mismatch")
                    if not set(role.allowed_target_type_ids) <= set(
                        relationship.target_type_ids
                    ):
                        raise ValueError("required role target endpoint mismatch")
                    if (
                        not set(requirement.competency_question_ids)
                        & set(relationship.competency_question_ids)
                        and relationship.governance_rationale is None
                    ):
                        raise ValueError(
                            "required role relationship lacks CQ or governance support"
                        )
            if requirement.structured_fact_set is not None:
                fact_set = requirement.structured_fact_set
                membership = relationship_by_id.get(
                    fact_set.membership_relationship_type_id
                )
                if membership is None:
                    raise ValueError("membership relationship is unknown")
                if fact_set.aggregate_type_id not in membership.source_type_ids:
                    raise ValueError("membership aggregate endpoint mismatch")
                if not set(fact_set.allowed_member_type_ids) <= set(
                    membership.target_type_ids
                ):
                    raise ValueError("membership member endpoint mismatch")
                if (
                    not set(requirement.competency_question_ids)
                    & set(membership.competency_question_ids)
                    and membership.governance_rationale is None
                ):
                    raise ValueError(
                        "membership relationship lacks CQ or governance support"
                    )
                ordering = fact_set.ordering_policy
                if ordering.mode == "ordered":
                    ordinal_property_id = ordering.ordinal_property_id
                    if ordinal_property_id is None or any(
                        ordinal_property_id
                        not in self.hierarchy_closure.effective_property_ids_by_type[
                            type_id
                        ]
                        for type_id in fact_set.allowed_member_type_ids
                    ):
                        raise ValueError(
                            "ordered collection ordinal property is unavailable "
                            "on every allowed member type"
                        )
                if fact_set.cardinality is not None:
                    cardinality = fact_set.cardinality
                    if not set(cardinality.source_question_ids) <= known_questions:
                        raise ValueError(
                            "cardinality references an unknown competency question"
                        )

        questions_by_id = {item.id: item for item in self.competency_questions}
        plans_by_id = {item.question_id: item for item in self.question_plans}
        coverage_by_id = {
            item.question_id: item for item in self.completeness_question_coverage
        }
        for question_id, coverage in coverage_by_id.items():
            if not set(coverage.requirement_ids) <= set(requirement_by_id):
                raise ValueError("completeness coverage references unknown requirement")
            if (
                questions_by_id[question_id].business_critical
                and (
                    not plans_by_id[question_id].covered
                    or coverage.coverage_status != "covered"
                )
            ):
                raise ValueError(
                    "business-critical questions require path and completeness coverage"
                )
        return self


AnyDomainContract: TypeAlias = Annotated[
    DomainContract | DomainContractV2,
    Field(discriminator="schema_version"),
]


class DomainReviewFinding(StrictModel):
    """One structured review finding."""

    severity: Literal["error", "warning", "suggestion"]
    path: str = Field(min_length=1)
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    proposed_value: Any | None = None


class CompetencyQuestionCoverage(StrictModel):
    """Structured competency-question coverage summary."""

    question: str = Field(min_length=1)
    supported: bool
    required_concepts: list[str] = Field(default_factory=list)
    notes: str | None = None

    _coerce_required_concepts = field_validator(
        "required_concepts", mode="before"
    )(_coerce_string_list)


class DomainReviewPayload(StrictModel):
    """LLM-returned review payload before metadata is attached."""

    schema_version: Literal[DOMAIN_SCHEMA_VERSION] = DOMAIN_SCHEMA_VERSION
    quality_score: float = Field(ge=0.0, le=1.0)
    findings: list[DomainReviewFinding] = Field(default_factory=list)
    competency_question_coverage: list[CompetencyQuestionCoverage] = Field(
        default_factory=list
    )
    proposed_contract: DomainContract | None = None


class DomainReview(DomainReviewPayload):
    """Persisted review artifact with contract and prompt metadata."""

    contract_hash: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    reviewed_at_utc: str = Field(min_length=1)
