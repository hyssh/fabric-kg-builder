"""Tests for graph/occurrence.py — versioned occurrence schemas (GRP-001)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fabric_kg_builder.graph.occurrence import (
    SUBGRAPH_SCHEMA_VERSION,
    EntityOccurrence,
    EvidenceSpan,
    OccurrenceContext,
    RelationshipOccurrence,
    SubgraphOccurrence,
    _content_hash,
    _local_id,
    _norm,
)


# ---------------------------------------------------------------------------
# Helpers / normalisation
# ---------------------------------------------------------------------------


class TestNorm:
    def test_basic_lowercase(self) -> None:
        assert _norm("Hello World") == "hello world"

    def test_strips_accents(self) -> None:
        assert _norm("Café") == "cafe"

    def test_collapses_whitespace(self) -> None:
        assert _norm("  foo   bar  ") == "foo bar"

    def test_empty_string(self) -> None:
        assert _norm("") == ""


class TestContentHash:
    def test_deterministic(self) -> None:
        assert _content_hash("abc") == _content_hash("abc")

    def test_different_inputs_differ(self) -> None:
        assert _content_hash("abc") != _content_hash("def")

    def test_is_sha256_hex(self) -> None:
        h = _content_hash("hello")
        assert len(h) == 64
        int(h, 16)  # should not raise


class TestLocalId:
    def test_prefix_included(self) -> None:
        lid = _local_id("ent", "a", "b")
        assert lid.startswith("ent:")

    def test_deterministic(self) -> None:
        assert _local_id("pfx", "x", "y") == _local_id("pfx", "x", "y")

    def test_different_parts_differ(self) -> None:
        assert _local_id("pfx", "a") != _local_id("pfx", "b")


# ---------------------------------------------------------------------------
# EvidenceSpan
# ---------------------------------------------------------------------------


class TestEvidenceSpan:
    def test_valid_span(self) -> None:
        span = EvidenceSpan(text_unit_id="tu1", start=0, end=10, text="hello")
        assert span.start == 0
        assert span.end == 10

    def test_end_must_be_greater_than_start(self) -> None:
        with pytest.raises(ValidationError, match="span end"):
            EvidenceSpan(text_unit_id="tu1", start=10, end=5, text="bad")

    def test_end_equal_to_start_invalid(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceSpan(text_unit_id="tu1", start=5, end=5, text="bad")

    def test_start_must_be_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            EvidenceSpan(text_unit_id="tu1", start=-1, end=5, text="bad")


# ---------------------------------------------------------------------------
# OccurrenceContext
# ---------------------------------------------------------------------------


class TestOccurrenceContext:
    def test_local_id_auto_generated(self) -> None:
        ctx = OccurrenceContext(text_unit_id="tu1")
        assert ctx.local_id.startswith("occ:")

    def test_semantic_key_defaults_to_text_unit_id(self) -> None:
        ctx = OccurrenceContext(text_unit_id="tu42")
        assert ctx.semantic_key == "tu42"

    def test_custom_semantic_key(self) -> None:
        ctx = OccurrenceContext(text_unit_id="tu1", semantic_key="custom_key")
        assert ctx.semantic_key == "custom_key"

    def test_confidence_range(self) -> None:
        with pytest.raises(ValidationError):
            OccurrenceContext(text_unit_id="tu1", confidence=-0.1)
        with pytest.raises(ValidationError):
            OccurrenceContext(text_unit_id="tu1", confidence=1.1)

    def test_schema_version_default(self) -> None:
        ctx = OccurrenceContext(text_unit_id="tu1")
        assert ctx.schema_version == SUBGRAPH_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# EntityOccurrence
# ---------------------------------------------------------------------------


class TestEntityOccurrence:
    def _make(self, **kwargs) -> EntityOccurrence:
        defaults = dict(
            text_unit_id="tu1",
            entity_type="org",
            display_name="Acme Corp",
            evidence_ids=["ev-001"],
        )
        defaults.update(kwargs)
        return EntityOccurrence(**defaults)

    def test_local_id_auto_generated(self) -> None:
        occ = self._make()
        assert occ.local_id.startswith("eocc:")

    def test_description_defaults_to_display_name(self) -> None:
        occ = self._make(description="")
        assert occ.description == "Acme Corp"

    def test_explicit_description_retained(self) -> None:
        occ = self._make(description="Global supplier")
        assert occ.description == "Global supplier"

    def test_with_span(self) -> None:
        span = EvidenceSpan(text_unit_id="tu1", start=0, end=9, text="Acme Corp")
        occ = self._make(span=span)
        assert occ.span == span

    def test_with_evidence_ids(self) -> None:
        occ = self._make(evidence_ids=["ev-a", "ev-b"])
        assert occ.evidence_ids == ["ev-a", "ev-b"]

    def test_custom_local_id_preserved(self) -> None:
        occ = self._make(local_id="eocc:custom123")
        assert occ.local_id == "eocc:custom123"

    def test_different_entities_have_different_local_ids(self) -> None:
        occ1 = self._make(display_name="Alpha")
        occ2 = self._make(display_name="Beta")
        assert occ1.local_id != occ2.local_id

    def test_confidence_defaults_to_one(self) -> None:
        occ = self._make()
        assert occ.confidence == 1.0

    def test_aliases_default_empty(self) -> None:
        occ = self._make()
        assert occ.aliases == []


# ---------------------------------------------------------------------------
# RelationshipOccurrence
# ---------------------------------------------------------------------------


class TestRelationshipOccurrence:
    def _make_entities(self):
        e1 = EntityOccurrence(
            text_unit_id="tu1", entity_type="org", display_name="A",
            evidence_ids=["ev1"],
        )
        e2 = EntityOccurrence(
            text_unit_id="tu1", entity_type="org", display_name="B",
            evidence_ids=["ev2"],
        )
        return e1, e2

    def test_local_id_auto_generated(self) -> None:
        e1, e2 = self._make_entities()
        rel = RelationshipOccurrence(
            text_unit_id="tu1",
            relationship_type="supplies",
            source_local_id=e1.local_id,
            target_local_id=e2.local_id,
        )
        assert rel.local_id.startswith("rocc:")

    def test_description_auto_generated_when_empty(self) -> None:
        e1, e2 = self._make_entities()
        rel = RelationshipOccurrence(
            text_unit_id="tu1",
            relationship_type="supplies",
            source_local_id=e1.local_id,
            target_local_id=e2.local_id,
        )
        assert "supplies" in rel.description

    def test_explicit_description_retained(self) -> None:
        e1, e2 = self._make_entities()
        rel = RelationshipOccurrence(
            text_unit_id="tu1",
            relationship_type="supplies",
            source_local_id=e1.local_id,
            target_local_id=e2.local_id,
            description="A supplies B",
        )
        assert rel.description == "A supplies B"

    def test_with_span(self) -> None:
        e1, e2 = self._make_entities()
        span = EvidenceSpan(text_unit_id="tu1", start=0, end=20, text="A supplies B here")
        rel = RelationshipOccurrence(
            text_unit_id="tu1",
            relationship_type="supplies",
            source_local_id=e1.local_id,
            target_local_id=e2.local_id,
            span=span,
        )
        assert rel.span is not None


# ---------------------------------------------------------------------------
# SubgraphOccurrence
# ---------------------------------------------------------------------------


class TestSubgraphOccurrence:
    def _make_entities(self, n: int = 2) -> list[EntityOccurrence]:
        return [
            EntityOccurrence(
                text_unit_id="tu1",
                entity_type="org",
                display_name=f"Entity{i}",
                evidence_ids=[f"ev-{i}"],
            )
            for i in range(n)
        ]

    def test_make_factory(self) -> None:
        entities = self._make_entities(2)
        subgraph = SubgraphOccurrence.make(
            text_unit_id="tu1",
            entity_occurrences=entities,
            relationship_occurrences=[],
        )
        assert subgraph.occurrence_id.startswith("subgraph:")
        assert len(subgraph.entity_occurrences) == 2

    def test_relationship_reference_validation(self) -> None:
        entities = self._make_entities(2)
        rel = RelationshipOccurrence(
            text_unit_id="tu1",
            relationship_type="knows",
            source_local_id=entities[0].local_id,
            target_local_id="bad-id-not-in-entities",
        )
        with pytest.raises(ValidationError, match="not found in entity_occurrences"):
            SubgraphOccurrence.make(
                text_unit_id="tu1",
                entity_occurrences=entities,
                relationship_occurrences=[rel],
            )

    def test_valid_with_relationship(self) -> None:
        entities = self._make_entities(2)
        rel = RelationshipOccurrence(
            text_unit_id="tu1",
            relationship_type="knows",
            source_local_id=entities[0].local_id,
            target_local_id=entities[1].local_id,
        )
        subgraph = SubgraphOccurrence.make(
            text_unit_id="tu1",
            entity_occurrences=entities,
            relationship_occurrences=[rel],
        )
        assert len(subgraph.relationship_occurrences) == 1

    def test_empty_subgraph(self) -> None:
        subgraph = SubgraphOccurrence.make(
            text_unit_id="tu1",
            entity_occurrences=[],
            relationship_occurrences=[],
        )
        assert subgraph.occurrence_id.startswith("subgraph:")

    def test_domain_hash_propagated(self) -> None:
        entities = self._make_entities(1)
        subgraph = SubgraphOccurrence.make(
            text_unit_id="tu1",
            entity_occurrences=entities,
            relationship_occurrences=[],
            domain_hash="abc123",
        )
        assert subgraph.domain_hash == "abc123"

    def test_different_entities_produce_different_occurrence_id(self) -> None:
        entities_a = self._make_entities(1)
        entities_b = [
            EntityOccurrence(
                text_unit_id="tu1",
                entity_type="org",
                display_name="DifferentEntity",
                evidence_ids=["ev-99"],
            )
        ]
        sub_a = SubgraphOccurrence.make(
            text_unit_id="tu1",
            entity_occurrences=entities_a,
            relationship_occurrences=[],
        )
        sub_b = SubgraphOccurrence.make(
            text_unit_id="tu1",
            entity_occurrences=entities_b,
            relationship_occurrences=[],
        )
        assert sub_a.occurrence_id != sub_b.occurrence_id
