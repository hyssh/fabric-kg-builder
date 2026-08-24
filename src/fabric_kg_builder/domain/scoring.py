"""Deterministic evidence/CQ/ambiguity/IP-governance candidate scoring."""

from __future__ import annotations

from math import fsum, isfinite
from typing import Literal

from pydantic import Field, model_validator

from fabric_kg_builder.contracts.base import ContractModel, RequiredText, Sha256, canonical_sha256


SCORER_VERSION = "l1-domain-candidate-scorer/1.0.0"
SCORER_WEIGHTS = {
    "evidence_quality": 0.35,
    "cq_coverage": 0.30,
    "common_domain_fit": 0.20,
    "ambiguity_penalty": -0.15,
}
SCORER_HASH = canonical_sha256(
    {
        "version": SCORER_VERSION,
        "weights": SCORER_WEIGHTS,
        "tie_break": "descending-total-then-lexicographic-candidate-id",
        "ip_governance": "fail_closed_gate",
    }
)


class CandidateScoreInputsV2(ContractModel):
    accepted_evidence_span_count: int = Field(ge=0)
    required_evidence_span_count: int = Field(ge=0)
    covered_competency_question_count: int = Field(ge=0)
    total_relevant_competency_question_count: int = Field(ge=0)
    ambiguity_conflict_count: int = Field(ge=0)
    classification_fit: Literal["exact", "plausible", "mismatch"]
    ip_governance_status: Literal[
        "eligible", "license_unclear", "legal_unapproved", "provenance_missing", "rejected"
    ]


class CandidateScoreV2(ContractModel):
    scorer_version: Literal["l1-domain-candidate-scorer/1.0.0"] = SCORER_VERSION
    scorer_hash: Sha256 = SCORER_HASH
    evidence_quality_score: float = Field(ge=0.0, le=100.0)
    cq_coverage_score: float = Field(ge=0.0, le=100.0)
    ambiguity_conflict_penalty: float = Field(ge=0.0, le=100.0)
    common_domain_fit_score: float = Field(ge=0.0, le=100.0)
    ip_governance_eligible: bool
    total_score: float
    gate_reason: RequiredText | None = None

    @model_validator(mode="after")
    def _finite_and_gated(self) -> "CandidateScoreV2":
        numeric = (
            self.evidence_quality_score,
            self.cq_coverage_score,
            self.ambiguity_conflict_penalty,
            self.common_domain_fit_score,
            self.total_score,
        )
        if not all(isfinite(value) for value in numeric):
            raise ValueError("candidate score components must be finite")
        if self.ip_governance_eligible == (self.gate_reason is not None):
            raise ValueError("ineligible candidates require exactly one gate reason")
        return self


def score_candidate(inputs: CandidateScoreInputsV2) -> CandidateScoreV2:
    """Score from verified counts only; legal/IP status is a non-compensating gate."""
    if inputs.required_evidence_span_count:
        evidence_score = min(
            100.0,
            100.0
            * inputs.accepted_evidence_span_count
            / inputs.required_evidence_span_count,
        )
    else:
        evidence_score = 100.0
    if inputs.total_relevant_competency_question_count:
        cq_score = min(
            100.0,
            100.0
            * inputs.covered_competency_question_count
            / inputs.total_relevant_competency_question_count,
        )
    else:
        cq_score = 0.0
    ambiguity_penalty = min(100.0, float(inputs.ambiguity_conflict_count * 25))
    fit_score = {"exact": 100.0, "plausible": 50.0, "mismatch": 0.0}[
        inputs.classification_fit
    ]
    eligible = inputs.ip_governance_status == "eligible"
    weighted = fsum(
        (
            evidence_score * SCORER_WEIGHTS["evidence_quality"],
            cq_score * SCORER_WEIGHTS["cq_coverage"],
            fit_score * SCORER_WEIGHTS["common_domain_fit"],
            ambiguity_penalty * SCORER_WEIGHTS["ambiguity_penalty"],
        )
    )
    return CandidateScoreV2(
        evidence_quality_score=evidence_score,
        cq_coverage_score=cq_score,
        ambiguity_conflict_penalty=ambiguity_penalty,
        common_domain_fit_score=fit_score,
        ip_governance_eligible=eligible,
        total_score=weighted if eligible else float("-1"),
        gate_reason=(
            None
            if eligible
            else f"IP/governance status is {inputs.ip_governance_status}"
        ),
    )
