"""Tests for graph/relationship.py — relationship occurrence merge (GRP-004)."""

from __future__ import annotations

import pytest

from fabric_kg_builder.graph.relationship import (
    MergedRelationship,
    _merge_key,
    merge_relationship_occurrences,
)
from fabric_kg_builder.graph.occurrence import EvidenceSpan, RelationshipOccurrence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rocc(
    text_unit_id: str = "tu1",
    rel_type: str = "supplies",
    src: str = "e1",
    tgt: str = "e2",
    confidence: float = 0.8,
    description: str = "",
    evidence_ids: list[str] | None = None,
) -> RelationshipOccurrence:
    return RelationshipOccurrence(
        text_unit_id=text_unit_id,
        relationship_type=rel_type,
        source_local_id=src,
        target_local_id=tgt,
        confidence=confidence,
        description=description,
        evidence_ids=evidence_ids or [],
    )


# ---------------------------------------------------------------------------
# MergedRelationship properties
# ---------------------------------------------------------------------------


class TestMergedRelationshipProperties:
    def test_occurrence_count_empty(self) -> None:
        merged = MergedRelationship(
            relationship_type="supplies",
            source_local_id="e1",
            target_local_id="e2",
        )
        assert merged.occurrence_count == 0

    def test_occurrence_count_with_occurrences(self) -> None:
        occ = _rocc()
        merged = MergedRelationship(
            relationship_type="supplies",
            source_local_id="e1",
            target_local_id="e2",
            occurrences=[occ],
        )
        assert merged.occurrence_count == 1

    def test_primary_description_empty(self) -> None:
        merged = MergedRelationship(
            relationship_type="supplies",
            source_local_id="e1",
            target_local_id="e2",
        )
        assert merged.primary_description == ""

    def test_primary_description_first_item(self) -> None:
        merged = MergedRelationship(
            relationship_type="supplies",
            source_local_id="e1",
            target_local_id="e2",
            descriptions=["first", "second"],
        )
        assert merged.primary_description == "first"


# ---------------------------------------------------------------------------
# _merge_key
# ---------------------------------------------------------------------------


class TestMergeKey:
    def test_key_is_triple(self) -> None:
        occ = _rocc(rel_type="supplies", src="e1", tgt="e2")
        key = _merge_key(occ)
        assert key == ("supplies", "e1", "e2")


# ---------------------------------------------------------------------------
# merge_relationship_occurrences
# ---------------------------------------------------------------------------


class TestMergeRelationshipOccurrences:
    def test_empty_list(self) -> None:
        result = merge_relationship_occurrences([])
        assert result == []

    def test_single_occurrence(self) -> None:
        occ = _rocc(confidence=0.7)
        result = merge_relationship_occurrences([occ])
        assert len(result) == 1
        assert result[0].confidence == pytest.approx(0.7, abs=1e-4)

    def test_two_occurrences_same_triple_merged(self) -> None:
        occ1 = _rocc(confidence=0.8, description="A supplies B first")
        occ2 = _rocc(confidence=0.6, description="A supplies B second")
        result = merge_relationship_occurrences([occ1, occ2])
        assert len(result) == 1
        merged = result[0]
        assert merged.occurrence_count == 2

    def test_noisy_or_confidence(self) -> None:
        # Two occurrences with confidence 0.5 each: 1 - (1-0.5)*(1-0.5) = 0.75
        occ1 = _rocc(confidence=0.5)
        occ2 = _rocc(confidence=0.5)
        result = merge_relationship_occurrences([occ1, occ2])
        assert result[0].confidence == pytest.approx(0.75, abs=1e-4)

    def test_single_occurrence_confidence_preserved(self) -> None:
        occ = _rocc(confidence=0.9)
        result = merge_relationship_occurrences([occ])
        assert result[0].confidence == pytest.approx(0.9, abs=1e-4)

    def test_descriptions_deduplicated(self) -> None:
        occ1 = _rocc(description="Alpha supplies Beta")
        occ2 = _rocc(description="Alpha supplies Beta")  # same description
        result = merge_relationship_occurrences([occ1, occ2])
        assert result[0].descriptions.count("Alpha supplies Beta") == 1

    def test_distinct_descriptions_kept(self) -> None:
        occ1 = _rocc(description="First description")
        occ2 = _rocc(description="Second description")
        result = merge_relationship_occurrences([occ1, occ2])
        assert len(result[0].descriptions) == 2

    def test_different_triples_separate(self) -> None:
        occ1 = _rocc(src="e1", tgt="e2")
        occ2 = _rocc(src="e1", tgt="e3")
        result = merge_relationship_occurrences([occ1, occ2])
        assert len(result) == 2

    def test_direction_preserved(self) -> None:
        # e1→e2 and e2→e1 are different
        forward = _rocc(src="e1", tgt="e2")
        backward = _rocc(src="e2", tgt="e1")
        result = merge_relationship_occurrences([forward, backward])
        assert len(result) == 2

    def test_occurrence_ids_populated(self) -> None:
        occ1 = _rocc()
        occ2 = _rocc()
        result = merge_relationship_occurrences([occ1, occ2])
        assert len(result[0].occurrence_ids) == 2

    def test_evidence_ids_collected(self) -> None:
        occ1 = _rocc(evidence_ids=["ev-1"])
        occ2 = _rocc(evidence_ids=["ev-2"])
        result = merge_relationship_occurrences([occ1, occ2])
        assert set(result[0].all_evidence_ids) == {"ev-1", "ev-2"}

    def test_duplicate_evidence_ids_deduplicated(self) -> None:
        occ1 = _rocc(evidence_ids=["shared"])
        occ2 = _rocc(evidence_ids=["shared", "extra"])
        result = merge_relationship_occurrences([occ1, occ2])
        assert result[0].all_evidence_ids.count("shared") == 1

    def test_zero_confidence_occurrences(self) -> None:
        occ = _rocc(confidence=0.0)
        result = merge_relationship_occurrences([occ])
        assert result[0].confidence == 0.0

    def test_recompute_empty_occurrences(self) -> None:
        merged = MergedRelationship(
            relationship_type="r",
            source_local_id="a",
            target_local_id="b",
        )
        merged._recompute()
        assert merged.confidence == 0.0

    def test_properties_json_from_first_occurrence(self) -> None:
        occ = _rocc()
        occ2 = RelationshipOccurrence(
            text_unit_id="tu1",
            relationship_type="supplies",
            source_local_id="e1",
            target_local_id="e2",
            properties_json='{"key": "val"}',
        )
        result = merge_relationship_occurrences([occ2])
        assert result[0].properties_json == '{"key": "val"}'
