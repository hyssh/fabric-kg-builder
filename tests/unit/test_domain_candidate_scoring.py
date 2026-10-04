from __future__ import annotations

import math

import pytest

from fabric_kg_builder.domain.scoring import (
    CandidateScoreInputsV2,
    CandidateScoreV2,
    score_candidate,
)


def _inputs(**updates) -> CandidateScoreInputsV2:
    values = {
        "accepted_evidence_span_count": 2,
        "required_evidence_span_count": 2,
        "covered_competency_question_count": 2,
        "total_relevant_competency_question_count": 2,
        "ambiguity_conflict_count": 0,
        "classification_fit": "exact",
        "ip_governance_status": "eligible",
    }
    values.update(updates)
    return CandidateScoreInputsV2(**values)


def test_candidate_score_is_deterministic_and_component_based() -> None:
    score = score_candidate(_inputs())

    assert score == score_candidate(_inputs())
    assert score.evidence_quality_score == 100
    assert score.cq_coverage_score == 100
    assert score.total_score == 85


def test_ip_governance_is_a_non_compensating_gate() -> None:
    score = score_candidate(_inputs(ip_governance_status="license_unclear"))

    assert score.ip_governance_eligible is False
    assert score.total_score == -1
    assert "license_unclear" in score.gate_reason


def test_nonfinite_scores_are_rejected() -> None:
    with pytest.raises(ValueError, match="finite"):
        CandidateScoreV2(
            evidence_quality_score=100,
            cq_coverage_score=100,
            ambiguity_conflict_penalty=0,
            common_domain_fit_score=100,
            ip_governance_eligible=True,
            total_score=math.inf,
        )
