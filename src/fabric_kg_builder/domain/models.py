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
    """Strict schema-2.0 base model with normalized string values."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
    )


V2RequiredText: TypeAlias = Annotated[
    str,
    Field(min_length=1, pattern=r"\S"),
]
ProposalEvidenceId: TypeAlias = Annotated[
    str,
    Field(pattern=r"^proposal-evidence:[a-zA-Z0-9][a-zA-Z0-9._:-]*$"),
]


class DomainSectionV2(V2StrictModel):
    """Strict domain description for schema 2.0."""

    name: V2RequiredText
    description: V2RequiredText
    subdomains: list[V2RequiredText] = Field(default_factory=list)


class BusinessSectionV2(V2StrictModel):
    """Strict users, decisions, and operating context for schema 2.0."""

    organization_context: V2RequiredText
    users: list[V2RequiredText] = Field(min_length=1)
    decisions: list[V2RequiredText] = Field(min_length=1)


class ProblemSectionV2(V2StrictModel):
    """Strict problem and scope definition for schema 2.0."""

    statement: V2RequiredText
    desired_outcomes: list[V2RequiredText] = Field(min_length=1)
    in_scope: list[V2RequiredText] = Field(min_length=1)
    out_of_scope: list[V2RequiredText] = Field(default_factory=list)


class CanonicalTermV2(V2StrictModel):
    """Strict preferred terminology for schema 2.0."""

    term: V2RequiredText
    definition: V2RequiredText
    synonyms: list[V2RequiredText] = Field(default_factory=list)


class AmbiguousTermV2(V2StrictModel):
    """Strict ambiguous terminology for schema 2.0."""

    term: V2RequiredText
    meanings: list[V2RequiredText] = Field(min_length=1)


class TerminologySectionV2(V2StrictModel):
    """Strict schema-2.0 terminology catalogue."""

    canonical_terms: list[CanonicalTermV2] = Field(default_factory=list)
    ambiguous_terms: list[AmbiguousTermV2] = Field(default_factory=list)


class ConstraintsSectionV2(V2StrictModel):
    """Strict schema-2.0 temporal, regulatory, privacy, and safety rules."""

    temporal: list[V2RequiredText] = Field(default_factory=list)
    regulatory: list[V2RequiredText] = Field(default_factory=list)
    privacy: list[V2RequiredText] = Field(default_factory=list)
    safety: list[V2RequiredText] = Field(default_factory=list)


class PositiveExampleV2(V2StrictModel):
    """Strict positive example for schema 2.0."""

    text: V2RequiredText
    expected: list[V2RequiredText] = Field(default_factory=list)


class NegativeExampleV2(V2StrictModel):
    """Strict negative example for schema 2.0."""

    text: V2RequiredText
    reason: V2RequiredText


class ExamplesSectionV2(V2StrictModel):
    """Strict representative examples for schema 2.0."""

    positive: list[PositiveExampleV2] = Field(default_factory=list)
    negative: list[NegativeExampleV2] = Field(default_factory=list)


class DomainEntityTypeV2(V2StrictModel):
    """One approved entity type in a schema-2.0 domain design."""

    id: str = Field(pattern=r"^entity-type:[a-z0-9][a-z0-9._-]*$")
    name: V2RequiredText
    parent: str | None = Field(
        default=None,
        pattern=r"^entity-type:[a-z0-9][a-z0-9._-]*$",
    )
    description: V2RequiredText
    source_evidence_ids: list[ProposalEvidenceId] = Field(default_factory=list)
    business_defined: bool = False

    @model_validator(mode="after")
    def _validate_source_support(self) -> "DomainEntityTypeV2":
        self.source_evidence_ids = list(dict.fromkeys(self.source_evidence_ids))
        if not self.business_defined and not self.source_evidence_ids:
            raise ValueError(
                "[DOM-102] Extracted entity types require proposal source "
                "evidence unless business_defined=true."
            )
        return self


class DomainRelationshipTypeV2(V2StrictModel):
    """One bounded, directed relationship type in a schema-2.0 design."""

    id: str = Field(pattern=r"^relationship-type:[a-z0-9][a-z0-9._-]*$")
    predicate: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: V2RequiredText
    source_types: list[str] = Field(min_length=1)
    target_types: list[str] = Field(min_length=1)
    direction: Literal["source_to_target"] = "source_to_target"
    endpoint_policy: Literal["allow_subtypes", "exact"] = "allow_subtypes"
    evidence_policy: Literal["exact_span_required"] = "exact_span_required"
    publication_policy: Literal["asserted_only"] = "asserted_only"
    competency_question_ids: list[str] = Field(default_factory=list)
    governance_rule: V2RequiredText | None = None
    source_evidence_ids: list[ProposalEvidenceId] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_support(self) -> "DomainRelationshipTypeV2":
        self.source_types = list(dict.fromkeys(self.source_types))
        self.target_types = list(dict.fromkeys(self.target_types))
        self.competency_question_ids = list(
            dict.fromkeys(self.competency_question_ids)
        )
        self.source_evidence_ids = list(dict.fromkeys(self.source_evidence_ids))
        if not self.competency_question_ids and not self.governance_rule:
            raise ValueError(
                "[DOM-102] A relationship type must support a competency question or "
                "declare a governance_rule."
            )
        if not self.source_evidence_ids and not self.governance_rule:
            raise ValueError(
                "[DOM-102] Relationship types require proposal source evidence "
                "or an explicit governance_rule business justification."
            )
        return self


class CandidateModelSectionV2(V2StrictModel):
    """Typed candidate model sealed by schema-2.0 approval."""

    entity_types: list[DomainEntityTypeV2] = Field(min_length=1)
    relationship_types: list[DomainRelationshipTypeV2] = Field(min_length=1)


class CompetencyQuestionV2(V2StrictModel):
    """One stable competency question used for coverage and path design."""

    id: str = Field(pattern=r"^cq:[a-z0-9][a-z0-9._-]*$")
    question: str = Field(min_length=15, pattern=r"\S")
    business_critical: bool = True


class QuestionPathStepV2(V2StrictModel):
    """One explicitly directed hop in an approved competency path."""

    from_type: str = Field(pattern=r"^entity-type:[a-z0-9][a-z0-9._-]*$")
    relationship_type: str = Field(
        pattern=r"^relationship-type:[a-z0-9][a-z0-9._-]*$"
    )
    to_type: str = Field(pattern=r"^entity-type:[a-z0-9][a-z0-9._-]*$")
    traversal: Literal["forward", "reverse"]


class QuestionPlanV2(V2StrictModel):
    """Shortest typed path or explicit unsupported result for one question."""

    question_id: str = Field(pattern=r"^cq:[a-z0-9][a-z0-9._-]*$")
    required_path: list[QuestionPathStepV2] = Field(default_factory=list)
    hop_count: int = Field(ge=0, le=4)
    covered: bool
    shortest_path: Literal[True] = True
    unsupported_reason: V2RequiredText | None = None

    @model_validator(mode="after")
    def _validate_path_shape(self) -> "QuestionPlanV2":
        if self.hop_count != len(self.required_path):
            raise ValueError("hop_count must equal the number of required_path steps.")
        if self.covered and not self.required_path:
            raise ValueError("A covered question requires at least one path step.")
        if not self.covered and self.required_path:
            raise ValueError("An unsupported question cannot declare a required path.")
        if not self.covered and not self.unsupported_reason:
            raise ValueError("An unsupported question requires unsupported_reason.")
        for left, right in zip(self.required_path, self.required_path[1:]):
            if left.to_type != right.from_type:
                raise ValueError(
                    "Consecutive path steps must connect through the same entity type."
                )
        return self


class ReasoningPolicyV2(V2StrictModel):
    """Approved N and K bounds shared by design, extraction, and querying."""

    relationship_type_count: int = Field(ge=1, le=24)
    recommended_relationship_type_range: list[int] = Field(
        default_factory=lambda: [8, 20],
        min_length=2,
        max_length=2,
    )
    max_relationship_types: Literal[24] = 24
    relationship_type_count_rationale: V2RequiredText | None = None
    max_hops: int = Field(ge=1, le=4)
    absolute_max_hops: Literal[4] = 4
    max_hops_rationale: V2RequiredText | None = None
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
            raise ValueError("[DOM-103] N must be between 1 and 24.")
        max_hops = data.get("max_hops")
        if (
            isinstance(max_hops, int)
            and not isinstance(max_hops, bool)
            and not 1 <= max_hops <= 4
        ):
            raise ValueError("[DOM-105] K must be between 1 and 4.")
        return data

    @model_validator(mode="after")
    def _validate_bounds(self) -> "ReasoningPolicyV2":
        if self.recommended_relationship_type_range != [8, 20]:
            raise ValueError(
                "recommended_relationship_type_range must remain [8, 20]."
            )
        if self.relationship_type_count > 20 and not self.relationship_type_count_rationale:
            raise ValueError(
                "[DOM-103] Relationship counts from 21 through 24 require a rationale."
            )
        if self.max_hops == 4 and not self.max_hops_rationale:
            raise ValueError(
                "[DOM-105] K=4 requires a cited max_hops_rationale."
            )
        return self


class ExtractionPolicyV2(V2StrictModel):
    """Closed-vocabulary and evidence requirements for schema 2.0."""

    vocabulary_mode: Literal["closed"] = "closed"
    exact_evidence_span_required: Literal[True] = True
    abstain_without_evidence: Literal[True] = True
    allow_subtype_endpoints: bool = True


class PublicationPolicyV2(V2StrictModel):
    """Serving lifecycle policy for schema 2.0."""

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
            raise ValueError("Schema 2.0 publishes asserted relationships only.")
        if set(self.excluded_states) != {"unresolved", "rejected"}:
            raise ValueError(
                "Schema 2.0 must exclude unresolved and rejected relationships."
            )
        self.excluded_states = ["unresolved", "rejected"]
        return self


class ApprovalMetadataV2(V2StrictModel):
    """One-summary approval metadata for a schema-2.0 domain contract."""

    status: Literal["draft", "needs_review", "approved"] = "draft"
    approved_by: str | None = None
    approved_at_utc: str | None = None
    contract_hash: str | None = None
    proposal_hash: str | None = None
    source_profile_hash: str | None = None
    prompt_hash: str | None = None
    prompt_version: str | None = None
    model_version: str | None = None
    model_hash: str | None = None
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_approved_metadata(self) -> "ApprovalMetadataV2":
        if self.status != "approved":
            return self
        required = (
            self.approved_by,
            self.approved_at_utc,
            self.contract_hash,
            self.proposal_hash,
            self.source_profile_hash,
            self.prompt_hash,
            self.prompt_version,
            self.model_version,
            self.model_hash,
        )
        if any(not value for value in required):
            raise ValueError(
                "Approved schema-2.0 contracts require approver, timestamp, "
                "contract/proposal/source-profile/prompt hashes, prompt version, "
                "model version, and model hash."
            )
        return self


class DomainContractV2(V2StrictModel):
    """New-project-only bounded domain design contract."""

    schema_version: Literal[DOMAIN_SCHEMA_V2_VERSION]
    domain: DomainSectionV2
    business: BusinessSectionV2
    problem: ProblemSectionV2
    competency_questions: list[CompetencyQuestionV2] = Field(
        min_length=5,
        max_length=10,
    )
    terminology: TerminologySectionV2
    candidate_model: CandidateModelSectionV2
    constraints: ConstraintsSectionV2
    examples: ExamplesSectionV2
    reasoning_policy: ReasoningPolicyV2
    extraction_policy: ExtractionPolicyV2 = Field(default_factory=ExtractionPolicyV2)
    publication_policy: PublicationPolicyV2 = Field(
        default_factory=PublicationPolicyV2
    )
    question_plans: list[QuestionPlanV2]
    approval: ApprovalMetadataV2 = Field(default_factory=ApprovalMetadataV2)

    @model_validator(mode="before")
    @classmethod
    def _validate_question_count(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        questions = data.get("competency_questions")
        if isinstance(questions, list) and not 5 <= len(questions) <= 10:
            raise ValueError(
                "[DOM-101] Schema 2.0 requires five to ten competency questions."
            )
        return data

    @model_validator(mode="after")
    def _validate_domain_design(self) -> "DomainContractV2":
        entities = self.candidate_model.entity_types
        relationships = self.candidate_model.relationship_types
        entity_ids = [item.id for item in entities]
        entity_names = [item.name.casefold() for item in entities]
        relationship_ids = [item.id for item in relationships]
        predicates = [item.predicate for item in relationships]
        question_ids = [item.id for item in self.competency_questions]
        plan_question_ids = [item.question_id for item in self.question_plans]

        for label, values in (
            ("entity type ID", entity_ids),
            ("entity type name", entity_names),
            ("relationship type ID", relationship_ids),
            ("relationship predicate", predicates),
            ("competency question ID", question_ids),
            ("question plan ID", plan_question_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(
                    f"[DOM-106] Duplicate {label} in schema-2.0 domain contract."
                )

        known_entities = set(entity_ids)
        parent_by_id = {item.id: item.parent for item in entities}
        for entity in entities:
            if entity.parent and entity.parent not in known_entities:
                raise ValueError(
                    f"[DOM-106] Entity type '{entity.id}' references unknown parent "
                    f"'{entity.parent}'."
                )
            seen: set[str] = set()
            cursor: str | None = entity.id
            while cursor:
                if cursor in seen:
                    raise ValueError(
                        "[DOM-106] Entity type hierarchy contains a cycle."
                    )
                seen.add(cursor)
                cursor = parent_by_id.get(cursor)

        known_questions = set(question_ids)
        relationship_by_id = {item.id: item for item in relationships}
        adjacency_by_question: dict[str, dict[str, set[str]]] = {
            question_id: {
                entity_id: set() for entity_id in known_entities
            }
            for question_id in known_questions
        }
        for relationship in relationships:
            unknown_sources = set(relationship.source_types) - known_entities
            unknown_targets = set(relationship.target_types) - known_entities
            unknown_questions = (
                set(relationship.competency_question_ids) - known_questions
            )
            if unknown_sources or unknown_targets:
                raise ValueError(
                    f"[DOM-106] Relationship '{relationship.id}' references unknown endpoint "
                    f"types: {sorted(unknown_sources | unknown_targets)}."
                )
            if unknown_questions:
                raise ValueError(
                    f"[DOM-102] Relationship '{relationship.id}' references unknown competency "
                    f"questions: {sorted(unknown_questions)}."
                )
            for question_id in relationship.competency_question_ids:
                adjacency = adjacency_by_question[question_id]
                for source_type in relationship.source_types:
                    for target_type in relationship.target_types:
                        adjacency[source_type].add(target_type)
                        adjacency[target_type].add(source_type)

        def shortest_distance(
            adjacency: dict[str, set[str]],
            start: str,
            end: str,
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
                    for neighbor in sorted(adjacency.get(node, set())):
                        if neighbor == end:
                            return depth
                        if neighbor not in visited:
                            visited.add(neighbor)
                            next_frontier.add(neighbor)
                frontier = next_frontier
            return None

        if set(plan_question_ids) != known_questions:
            raise ValueError(
                "[DOM-104] question_plans must contain exactly one plan for every competency "
                "question."
            )
        if not any(plan.covered for plan in self.question_plans):
            raise ValueError(
                "[DOM-104] At least one competency question must have a covered path."
            )
        for plan in self.question_plans:
            for step in plan.required_path:
                relationship = relationship_by_id.get(step.relationship_type)
                if relationship is None:
                    raise ValueError(
                        f"[DOM-106] Question plan '{plan.question_id}' references unknown "
                        f"relationship '{step.relationship_type}'."
                    )
                if plan.question_id not in relationship.competency_question_ids:
                    raise ValueError(
                        f"[DOM-104] Question plan '{plan.question_id}' uses "
                        f"relationship '{step.relationship_type}', but that "
                        "relationship is not approved for the competency question."
                    )
                expected_sources = (
                    relationship.source_types
                    if step.traversal == "forward"
                    else relationship.target_types
                )
                expected_targets = (
                    relationship.target_types
                    if step.traversal == "forward"
                    else relationship.source_types
                )
                if (
                    step.from_type not in expected_sources
                    or step.to_type not in expected_targets
                ):
                    raise ValueError(
                        f"[DOM-106] Question plan '{plan.question_id}' has an endpoint or "
                        f"direction mismatch for '{step.relationship_type}'."
                    )
            if plan.hop_count > self.reasoning_policy.max_hops:
                raise ValueError(
                    f"[DOM-105] Question plan '{plan.question_id}' exceeds approved K="
                    f"{self.reasoning_policy.max_hops}."
                )
            if not plan.covered:
                continue
            shortest = shortest_distance(
                adjacency_by_question[plan.question_id],
                plan.required_path[0].from_type,
                plan.required_path[-1].to_type,
            )
            if shortest != plan.hop_count:
                raise ValueError(
                    f"[DOM-105] Question plan '{plan.question_id}' declares "
                    f"{plan.hop_count} hop(s), but the approved type graph has "
                    f"a shortest path of {shortest} hop(s)."
                )

        relationship_count = len(relationships)
        if self.reasoning_policy.relationship_type_count != relationship_count:
            raise ValueError(
                "[DOM-103] reasoning_policy.relationship_type_count must equal the number "
                "of approved relationship types."
            )
        derived_k = max(
            plan.hop_count for plan in self.question_plans if plan.covered
        )
        if self.reasoning_policy.max_hops != derived_k:
            raise ValueError(
                "[DOM-105] reasoning_policy.max_hops must equal the maximum shortest "
                "covered question path."
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
