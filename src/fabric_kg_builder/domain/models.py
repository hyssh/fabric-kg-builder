"""Pydantic models for versioned domain contracts and review artifacts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


DOMAIN_SCHEMA_VERSION = "1.0"


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
