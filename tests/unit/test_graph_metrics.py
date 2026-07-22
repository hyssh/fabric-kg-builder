"""Tests for graph/metrics.py — quality metrics and gold evaluation (GRP-014)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from fabric_kg_builder.graph.metrics import (
    FACILITIES_CONTRACT,
    LEGAL_CONTRACT,
    SUPPLY_CHAIN_CONTRACT,
    DomainEvaluationContract,
    EvaluationMetrics,
    GoldClaim,
    GoldEntity,
    GoldRelationship,
    QualityMetrics,
    _f1,
    _normalize_name,
    compute_quality_metrics,
    evaluate_against_gold,
)


# ---------------------------------------------------------------------------
# Helpers — minimal entity/relationship/claim stubs for eval tests
# ---------------------------------------------------------------------------


@dataclass
class _Entity:
    entity_id: str
    display_name: str
    entity_type: str
    description: str = ""
    confidence: float = 1.0


@dataclass
class _Relationship:
    source_entity_id: str
    target_entity_id: str
    relationship_type: str


@dataclass
class _Claim:
    claim_id: str
    predicate: str
    status: str = "asserted"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestF1:
    def test_both_zero(self) -> None:
        assert _f1(0.0, 0.0) == 0.0

    def test_perfect_score(self) -> None:
        assert _f1(1.0, 1.0) == pytest.approx(1.0)

    def test_harmonic_mean(self) -> None:
        assert _f1(0.5, 1.0) == pytest.approx(2 / 3, abs=1e-6)

    def test_zero_precision(self) -> None:
        assert _f1(0.0, 1.0) == 0.0

    def test_zero_recall(self) -> None:
        assert _f1(1.0, 0.0) == 0.0


class TestNormalizeName:
    def test_strips_whitespace(self) -> None:
        assert _normalize_name("  Acme  ") == "acme"

    def test_lowercases(self) -> None:
        assert _normalize_name("ALPHA") == "alpha"

    def test_empty(self) -> None:
        assert _normalize_name("") == ""


# ---------------------------------------------------------------------------
# QualityMetrics / compute_quality_metrics
# ---------------------------------------------------------------------------


class TestComputeQualityMetrics:
    def test_empty_inputs(self) -> None:
        result = compute_quality_metrics([], [], [])
        assert result.total_entities == 0
        assert result.total_relationships == 0
        assert result.total_claims == 0
        # avg_confidence is 0 (no entities) which fails threshold check
        assert not result.passed

    def test_good_entities_pass(self) -> None:
        entities = [_Entity("e1", "Alpha", "org", "A supplier", 0.9)]
        result = compute_quality_metrics(entities, [], [])
        assert result.avg_confidence == pytest.approx(0.9)
        assert result.entities_without_descriptions == 0
        assert result.passed

    def test_low_confidence_fails(self) -> None:
        entities = [_Entity("e1", "Alpha", "org", "desc", 0.1)]
        result = compute_quality_metrics(entities, [], [], min_avg_confidence=0.5)
        assert not result.passed
        assert len(result.threshold_failures) > 0

    def test_missing_descriptions_failure(self) -> None:
        entities = [
            _Entity("e1", "A", "org", ""),  # no description
            _Entity("e2", "B", "org", ""),  # no description
            _Entity("e3", "C", "org", ""),  # no description
            _Entity("e4", "D", "org", ""),  # no description
        ]
        result = compute_quality_metrics(entities, [], [], max_missing_descriptions=0.1)
        assert not result.passed
        assert result.entities_without_descriptions == 4

    def test_claims_invalid_status_counted(self) -> None:
        claims = [_Claim("c1", "Some claim", "invalid_status")]
        result = compute_quality_metrics([], [], claims)
        assert result.claims_with_invalid_status == 1

    def test_valid_claim_statuses(self) -> None:
        claims = [
            _Claim("c1", "claim1", "asserted"),
            _Claim("c2", "claim2", "retracted"),
            _Claim("c3", "claim3", "disputed"),
        ]
        result = compute_quality_metrics([], [], claims)
        assert result.claims_with_invalid_status == 0

    def test_entity_without_confidence_uses_default(self) -> None:
        @dataclass
        class _NoConf:
            entity_id: str
            display_name: str
            description: str = "desc"
            # no confidence attribute

        entities = [_NoConf("e1", "Alpha")]
        result = compute_quality_metrics(entities, [], [])
        assert result.avg_confidence == pytest.approx(1.0)

    def test_relationships_counted(self) -> None:
        entities = [_Entity("e1", "A", "org", "desc")]
        rels = [_Relationship("e1", "e1", "self")]
        result = compute_quality_metrics(entities, rels, [])
        assert result.total_relationships == 1


# ---------------------------------------------------------------------------
# Gold evaluation contracts — evaluate_against_gold
# ---------------------------------------------------------------------------


class TestEvaluateAgainstGold:
    def _make_contract(self, min_entity_f1: float = 0.5) -> DomainEvaluationContract:
        return DomainEvaluationContract(
            domain_name="test",
            gold_entities=[
                GoldEntity("Alpha", "org"),
                GoldEntity("Beta", "org"),
            ],
            gold_relationships=[
                GoldRelationship("Alpha", "knows", "Beta"),
            ],
            min_entity_f1=min_entity_f1,
            min_relationship_f1=0.3,
            min_groundedness=0.5,
            min_coherence=0.5,
        )

    def test_empty_predictions_low_scores(self) -> None:
        contract = self._make_contract()
        result = evaluate_against_gold(contract, [], [], [])
        assert result.entity_f1 == 0.0
        assert result.relationship_f1 == 0.0

    def test_perfect_entity_match(self) -> None:
        contract = self._make_contract(min_entity_f1=0.0)
        entities = [
            _Entity("e1", "Alpha", "org", "desc"),
            _Entity("e2", "Beta", "org", "desc"),
        ]
        result = evaluate_against_gold(contract, entities, [])
        assert result.entity_precision == pytest.approx(1.0)
        assert result.entity_recall == pytest.approx(1.0)

    def test_partial_entity_match(self) -> None:
        contract = self._make_contract(min_entity_f1=0.0)
        entities = [_Entity("e1", "Alpha", "org", "desc")]
        result = evaluate_against_gold(contract, entities, [])
        assert result.entity_recall == pytest.approx(0.5)

    def test_groundedness_score(self) -> None:
        contract = self._make_contract(min_entity_f1=0.0)
        entities = [
            _Entity("e1", "Alpha", "org", "Has description"),
            _Entity("e2", "Beta", "org", ""),  # no description
        ]
        result = evaluate_against_gold(contract, entities, [])
        assert result.groundedness_score == pytest.approx(0.5)

    def test_coherence_score(self) -> None:
        contract = self._make_contract(min_entity_f1=0.0)
        entities = [
            _Entity("e1", "Alpha", "org", "desc"),
            _Entity("e2", "Beta", "org", "desc"),
        ]
        rels = [_Relationship("e1", "e2", "knows")]
        result = evaluate_against_gold(contract, entities, rels)
        assert result.coherence_score == pytest.approx(1.0)

    def test_coherence_incoherent_relationship(self) -> None:
        contract = self._make_contract(min_entity_f1=0.0)
        entities = [_Entity("e1", "Alpha", "org", "desc")]
        rels = [_Relationship("e1", "missing-id", "knows")]
        result = evaluate_against_gold(contract, entities, rels)
        assert result.coherence_score == pytest.approx(0.0)

    def test_threshold_failures_collected(self) -> None:
        contract = self._make_contract(min_entity_f1=0.99)
        entities = [_Entity("e1", "Alpha", "org", "desc")]
        result = evaluate_against_gold(contract, entities, [])
        assert not result.passed
        assert any("entity_f1" in f for f in result.threshold_failures)

    def test_passes_when_all_thresholds_met(self) -> None:
        contract = DomainEvaluationContract(
            domain_name="easy",
            gold_entities=[GoldEntity("Alpha", "org")],
            gold_relationships=[],
            min_entity_f1=0.0,
            min_relationship_f1=0.0,
            min_groundedness=0.0,
            min_coherence=0.0,
        )
        entities = [_Entity("e1", "Alpha", "org", "desc")]
        result = evaluate_against_gold(contract, entities, [])
        assert result.passed

    def test_claims_evaluation(self) -> None:
        contract = DomainEvaluationContract(
            domain_name="test",
            gold_entities=[],
            gold_relationships=[],
            gold_claims=[GoldClaim("Alpha claim")],
            min_entity_f1=0.0,
            min_relationship_f1=0.0,
            min_groundedness=0.0,
            min_coherence=0.0,
        )
        claims = [_Claim("c1", "Alpha claim")]
        result = evaluate_against_gold(contract, [], [], claims)
        assert result.claims_precision == pytest.approx(1.0)
        assert result.claims_recall == pytest.approx(1.0)

    def test_no_claims_when_none_provided(self) -> None:
        contract = DomainEvaluationContract(
            domain_name="test",
            gold_entities=[],
            gold_relationships=[],
            gold_claims=[GoldClaim("a claim")],
            min_entity_f1=0.0,
            min_relationship_f1=0.0,
            min_groundedness=0.0,
            min_coherence=0.0,
        )
        # predicted_claims=None → claims scores stay 0
        result = evaluate_against_gold(contract, [], [], None)
        assert result.claims_precision == 0.0


# ---------------------------------------------------------------------------
# Canonical domain contracts
# ---------------------------------------------------------------------------


class TestCanonicalContracts:
    def test_supply_chain_contract_defined(self) -> None:
        assert SUPPLY_CHAIN_CONTRACT.domain_name == "supply_chain"
        assert len(SUPPLY_CHAIN_CONTRACT.gold_entities) > 0
        assert len(SUPPLY_CHAIN_CONTRACT.gold_relationships) > 0

    def test_legal_contract_defined(self) -> None:
        assert LEGAL_CONTRACT.domain_name == "legal_contracts"
        assert len(LEGAL_CONTRACT.gold_entities) > 0

    def test_facilities_contract_defined(self) -> None:
        assert FACILITIES_CONTRACT.domain_name == "facilities"
        assert len(FACILITIES_CONTRACT.gold_entities) > 0

    def test_supply_chain_empty_predictions_fail(self) -> None:
        result = evaluate_against_gold(SUPPLY_CHAIN_CONTRACT, [], [])
        assert not result.passed

    def test_legal_empty_predictions_fail(self) -> None:
        result = evaluate_against_gold(LEGAL_CONTRACT, [], [])
        assert not result.passed
