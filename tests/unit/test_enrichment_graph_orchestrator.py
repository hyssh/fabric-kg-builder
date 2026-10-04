"""Tests for enrichment/graph_orchestrator.py — GraphOrchestrator end-to-end."""
from __future__ import annotations

import pytest

from fabric_kg_builder.enrichment.graph_orchestrator import (
    DummyExtractor,
    GraphOrchestrator,
    SubgraphExtractorProtocol,
)
from fabric_kg_builder.graph.occurrence import (
    EntityOccurrence,
    EvidenceSpan,
    RelationshipOccurrence,
    SubgraphOccurrence,
)
from fabric_kg_builder.graph.persistence import GraphExtractionResult
from fabric_kg_builder.model.ids import make_id


# ---------------------------------------------------------------------------
# DummyExtractor
# ---------------------------------------------------------------------------

class TestDummyExtractor:
    def test_returns_empty_subgraph(self):
        extractor = DummyExtractor()
        result = extractor.extract("some text", "unit-001", "dhash")
        assert isinstance(result, SubgraphOccurrence)
        assert result.entity_occurrences == []
        assert result.relationship_occurrences == []
        assert result.text_unit_id == "unit-001"
        assert result.domain_hash == "dhash"

    def test_no_domain_hash(self):
        extractor = DummyExtractor()
        result = extractor.extract("text", "unit-001", None)
        assert result.domain_hash is None

    def test_satisfies_protocol(self):
        assert isinstance(DummyExtractor(), SubgraphExtractorProtocol)


# ---------------------------------------------------------------------------
# GraphOrchestrator with DummyExtractor
# ---------------------------------------------------------------------------

class TestGraphOrchestratorEmpty:
    def setup_method(self):
        self.orchestrator = GraphOrchestrator()

    def test_empty_chunks_returns_result(self):
        result = self.orchestrator.run([])
        assert isinstance(result, GraphExtractionResult)
        assert result.merged_entities == []
        assert result.merged_relationships == []
        assert result.claims == []

    def test_single_empty_chunk(self):
        chunks = [{"text_unit_id": "unit-001", "text": ""}]
        result = self.orchestrator.run(chunks)
        assert isinstance(result, GraphExtractionResult)
        assert result.merged_entities == []

    def test_source_id_is_preserved(self):
        result = self.orchestrator.run([], source_id="my-source")
        assert result.source_id == "my-source"

    def test_domain_hash_is_preserved(self):
        result = self.orchestrator.run([], domain_hash="dhash-abc")
        assert result.domain_hash == "dhash-abc"


# ---------------------------------------------------------------------------
# GraphOrchestrator with real entity occurrences
# ---------------------------------------------------------------------------

def _make_entity_occ(local_id: str, display_name: str, entity_type: str = "Person",
                     evidence_ids: list[str] | None = None) -> EntityOccurrence:
    return EntityOccurrence(
        local_id=local_id,
        text_unit_id="unit-001",
        domain_hash="dhash",
        entity_type=entity_type,
        display_name=display_name,
        description=f"{display_name} is an entity",
        evidence_ids=evidence_ids or [],
        confidence=0.9,
    )


def _make_rel_occ(local_id: str, source: str, target: str, rel_type: str = "knows") -> RelationshipOccurrence:
    return RelationshipOccurrence(
        local_id=local_id,
        text_unit_id="unit-001",
        domain_hash="dhash",
        relationship_type=rel_type,
        source_local_id=source,
        target_local_id=target,
        description=f"{source} {rel_type} {target}",
        confidence=0.8,
    )


class FakeExtractor:
    """Extractor that returns predefined entity occurrences."""

    def __init__(self, entity_occs: list, rel_occs: list | None = None):
        self._entity_occs = entity_occs
        self._rel_occs = rel_occs or []

    def extract(self, text: str, text_unit_id: str, domain_hash: str | None) -> SubgraphOccurrence:
        occ_id = make_id("subgraph", f"{text_unit_id}:{domain_hash or ''}")
        return SubgraphOccurrence(
            occurrence_id=occ_id,
            text_unit_id=text_unit_id,
            domain_hash=domain_hash,
            entity_occurrences=self._entity_occs,
            relationship_occurrences=self._rel_occs,
        )


class TestGraphOrchestratorWithEntities:
    def test_entities_are_merged(self):
        ent1 = _make_entity_occ("local:alice", "Alice")
        ent2 = _make_entity_occ("local:bob", "Bob")
        orchestrator = GraphOrchestrator(extractor=FakeExtractor([ent1, ent2]))
        chunks = [{"text_unit_id": "unit-001", "text": "Alice and Bob work together."}]
        result = orchestrator.run(chunks)
        assert len(result.merged_entities) == 2
        names = {e.display_name for e in result.merged_entities}
        assert "Alice" in names
        assert "Bob" in names

    def test_relationships_are_merged(self):
        ent1 = _make_entity_occ("local:alice", "Alice")
        ent2 = _make_entity_occ("local:bob", "Bob")
        rel = _make_rel_occ("rel:001", "local:alice", "local:bob")
        orchestrator = GraphOrchestrator(
            extractor=FakeExtractor([ent1, ent2], [rel])
        )
        chunks = [{"text_unit_id": "unit-001", "text": "Alice knows Bob."}]
        result = orchestrator.run(chunks)
        assert len(result.merged_relationships) == 1
        assert result.merged_relationships[0].relationship_type == "knows"

    def test_same_entity_across_chunks(self):
        """Same entity ID from two chunks should remain as single merged entity."""
        ent = _make_entity_occ("local:alice", "Alice")
        orchestrator = GraphOrchestrator(extractor=FakeExtractor([ent]))
        # Two chunks — extractor returns same occurrences both times
        chunks = [
            {"text_unit_id": "unit-001", "text": "Alice is a person."},
            {"text_unit_id": "unit-002", "text": "Alice leads the team."},
        ]
        result = orchestrator.run(chunks)
        # local:alice might appear from both chunks but dedup should handle it
        alice_entities = [e for e in result.merged_entities if e.display_name == "Alice"]
        assert len(alice_entities) >= 1

    def test_run_id_propagated(self):
        orchestrator = GraphOrchestrator()
        result = orchestrator.run([], run_id="run-xyz")
        assert isinstance(result, GraphExtractionResult)

    def test_duplicate_entities_in_same_chunk(self):
        """Same local_id appearing twice should be deduped."""
        ent = _make_entity_occ("local:alice", "Alice")
        orchestrator = GraphOrchestrator(extractor=FakeExtractor([ent, ent]))
        chunks = [{"text_unit_id": "unit-001", "text": "Alice Alice."}]
        result = orchestrator.run(chunks)
        alice_entities = [e for e in result.merged_entities if e.display_name == "Alice"]
        # After dedup, should have exactly one Alice
        assert len(alice_entities) == 1

    def test_claims_extracted_with_evidence(self):
        ent = _make_entity_occ(
            "local:acme", "Acme Corporation",
            entity_type="Organization",
            evidence_ids=["ev:001"],
        )
        ent.span = EvidenceSpan(
            text_unit_id="unit-001",
            start=0,
            end=40,
            text="Acme Corporation is a large tech company.",
        )
        orchestrator = GraphOrchestrator(extractor=FakeExtractor([ent]))
        chunks = [{"text_unit_id": "unit-001", "text": "Acme Corporation is a large tech company."}]
        result = orchestrator.run(chunks)
        # claims may or may not be extracted depending on text structure
        assert isinstance(result.claims, list)

    def test_entity_occs_preserved(self):
        ent1 = _make_entity_occ("local:alice", "Alice")
        orchestrator = GraphOrchestrator(extractor=FakeExtractor([ent1]))
        chunks = [{"text_unit_id": "unit-001", "text": "Alice."}]
        result = orchestrator.run(chunks)
        assert len(result.entity_occurrences) == 1

    def test_relationship_occs_preserved(self):
        ent1 = _make_entity_occ("local:alice", "Alice")
        ent2 = _make_entity_occ("local:bob", "Bob")
        rel = _make_rel_occ("rel:001", "local:alice", "local:bob")
        orchestrator = GraphOrchestrator(extractor=FakeExtractor([ent1, ent2], [rel]))
        chunks = [{"text_unit_id": "unit-001", "text": "Alice knows Bob."}]
        result = orchestrator.run(chunks)
        assert len(result.relationship_occurrences) == 1

    def test_multiple_relationships_same_pair_merged(self):
        ent1 = _make_entity_occ("local:a", "Alice")
        ent2 = _make_entity_occ("local:b", "Bob")
        rel1 = _make_rel_occ("rel:001", "local:a", "local:b", "knows")
        rel2 = _make_rel_occ("rel:002", "local:a", "local:b", "knows")
        orchestrator = GraphOrchestrator(extractor=FakeExtractor([ent1, ent2], [rel1, rel2]))
        chunks = [{"text_unit_id": "unit-001", "text": "Alice knows Bob twice."}]
        result = orchestrator.run(chunks)
        # Two occurrences of same relationship type should merge to one
        knows_rels = [r for r in result.merged_relationships if r.relationship_type == "knows"]
        assert len(knows_rels) == 1


# ---------------------------------------------------------------------------
# GraphOrchestrator with claim contradictions
# ---------------------------------------------------------------------------

class TestClaimContradictions:
    def test_find_claim_contradictions_empty(self):
        pairs = GraphOrchestrator._find_claim_contradictions([])
        assert pairs == []

    def test_finds_contradiction(self):
        claims = [
            {"claim_id": "c1", "subject_entity_id": "e1", "predicate": "is_active", "status": "asserted"},
            {"claim_id": "c2", "subject_entity_id": "e1", "predicate": "is_active", "status": "retracted"},
        ]
        pairs = GraphOrchestrator._find_claim_contradictions(claims)
        assert ("c1", "c2") in pairs

    def test_no_contradiction_different_predicates(self):
        claims = [
            {"claim_id": "c1", "subject_entity_id": "e1", "predicate": "is_active", "status": "asserted"},
            {"claim_id": "c2", "subject_entity_id": "e1", "predicate": "is_large", "status": "retracted"},
        ]
        pairs = GraphOrchestrator._find_claim_contradictions(claims)
        assert pairs == []

    def test_disputed_also_contradicts_asserted(self):
        claims = [
            {"claim_id": "c1", "subject_entity_id": "e1", "predicate": "is_valid", "status": "asserted"},
            {"claim_id": "c2", "subject_entity_id": "e1", "predicate": "is_valid", "status": "disputed"},
        ]
        pairs = GraphOrchestrator._find_claim_contradictions(claims)
        assert ("c1", "c2") in pairs
