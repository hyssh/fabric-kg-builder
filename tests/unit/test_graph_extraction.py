"""Tests for graph/persistence.py and related extraction models (GRP-003)."""

from __future__ import annotations

import json

import pytest

from fabric_kg_builder.graph.persistence import (
    GraphExtractionResult,
    MergedEntityRecord,
    MergedRelationshipRecord,
)
from fabric_kg_builder.graph.occurrence import EntityOccurrence, RelationshipOccurrence


# ---------------------------------------------------------------------------
# MergedEntityRecord
# ---------------------------------------------------------------------------


class TestMergedEntityRecord:
    def _make(self, **kwargs) -> MergedEntityRecord:
        defaults = dict(
            entity_id="e1",
            display_name="Alpha Corp",
            entity_type="org",
            description="A supplier",
            occurrence_ids=["occ1"],
            descriptions=["A supplier"],
            all_evidence_ids=["ev1"],
        )
        defaults.update(kwargs)
        return MergedEntityRecord(**defaults)

    def test_to_dict_roundtrip(self) -> None:
        record = self._make()
        d = record.to_dict()
        restored = MergedEntityRecord.from_dict(d)
        assert restored.entity_id == record.entity_id
        assert restored.display_name == record.display_name

    def test_from_dict_missing_optional_fields(self) -> None:
        d = {
            "entity_id": "e1",
            "display_name": "Alpha",
            "entity_type": "org",
        }
        record = MergedEntityRecord.from_dict(d)
        assert record.description == ""
        assert record.occurrence_ids == []
        assert record.all_evidence_ids == []
        assert record.aliases == []

    def test_aliases_preserved(self) -> None:
        record = self._make(aliases=["Alpha Corp Ltd", "ACL"])
        d = record.to_dict()
        restored = MergedEntityRecord.from_dict(d)
        assert restored.aliases == ["Alpha Corp Ltd", "ACL"]

    def test_to_dict_has_all_keys(self) -> None:
        record = self._make()
        d = record.to_dict()
        for key in ["entity_id", "display_name", "entity_type", "description",
                    "occurrence_ids", "descriptions", "all_evidence_ids", "aliases"]:
            assert key in d


# ---------------------------------------------------------------------------
# MergedRelationshipRecord
# ---------------------------------------------------------------------------


class TestMergedRelationshipRecord:
    def _make(self, **kwargs) -> MergedRelationshipRecord:
        defaults = dict(
            relationship_id="r1",
            source_entity_id="e1",
            target_entity_id="e2",
            relationship_type="supplies",
            description="A supplies B",
            occurrence_ids=["occ1"],
            descriptions=["A supplies B"],
            all_evidence_ids=["ev1"],
        )
        defaults.update(kwargs)
        return MergedRelationshipRecord(**defaults)

    def test_to_dict_roundtrip(self) -> None:
        record = self._make()
        d = record.to_dict()
        restored = MergedRelationshipRecord.from_dict(d)
        assert restored.relationship_id == record.relationship_id
        assert restored.relationship_type == record.relationship_type

    def test_from_dict_missing_optional_fields(self) -> None:
        d = {
            "relationship_id": "r1",
            "source_entity_id": "e1",
            "target_entity_id": "e2",
            "relationship_type": "supplies",
        }
        record = MergedRelationshipRecord.from_dict(d)
        assert record.description == ""
        assert record.occurrence_ids == []

    def test_to_dict_has_all_keys(self) -> None:
        record = self._make()
        d = record.to_dict()
        for key in ["relationship_id", "source_entity_id", "target_entity_id",
                    "relationship_type", "description", "occurrence_ids",
                    "descriptions", "all_evidence_ids"]:
            assert key in d


# ---------------------------------------------------------------------------
# GraphExtractionResult
# ---------------------------------------------------------------------------


def _make_entity_occ(name: str = "Alpha") -> EntityOccurrence:
    return EntityOccurrence(
        text_unit_id="tu1",
        entity_type="org",
        display_name=name,
        evidence_ids=["ev1"],
    )


def _make_rel_occ(src: str, tgt: str) -> RelationshipOccurrence:
    return RelationshipOccurrence(
        text_unit_id="tu1",
        relationship_type="supplies",
        source_local_id=src,
        target_local_id=tgt,
    )


class TestGraphExtractionResult:
    def _make_result(self, **kwargs) -> GraphExtractionResult:
        eocc = _make_entity_occ()
        defaults = dict(
            source_id="doc1",
            domain_hash="hash123",
            entity_occurrences=[eocc],
            relationship_occurrences=[],
            merged_entities=[
                MergedEntityRecord(
                    entity_id="e1",
                    display_name="Alpha",
                    entity_type="org",
                    description="A supplier",
                    occurrence_ids=["occ1"],
                    descriptions=["Alpha supplier"],
                    all_evidence_ids=["ev1"],
                )
            ],
            merged_relationships=[],
            claims=[],
            hierarchy_clusters=[],
            hierarchy_memberships=[],
        )
        defaults.update(kwargs)
        return GraphExtractionResult(**defaults)

    def test_to_json_is_valid_json(self) -> None:
        result = self._make_result()
        json_str = result.to_json()
        obj = json.loads(json_str)
        assert isinstance(obj, dict)

    def test_to_json_has_required_keys(self) -> None:
        result = self._make_result()
        obj = json.loads(result.to_json())
        for key in ["source_id", "domain_hash", "extraction_version",
                    "entity_occurrences", "relationship_occurrences",
                    "merged_entities", "merged_relationships"]:
            assert key in obj

    def test_from_json_roundtrip(self) -> None:
        result = self._make_result()
        json_str = result.to_json()
        restored = GraphExtractionResult.from_json(json_str)
        assert restored.source_id == result.source_id
        assert restored.domain_hash == result.domain_hash

    def test_from_json_restores_entity_occurrences(self) -> None:
        result = self._make_result()
        json_str = result.to_json()
        restored = GraphExtractionResult.from_json(json_str)
        assert len(restored.entity_occurrences) == 1
        assert restored.entity_occurrences[0].display_name == "Alpha"

    def test_from_json_restores_merged_entities(self) -> None:
        result = self._make_result()
        json_str = result.to_json()
        restored = GraphExtractionResult.from_json(json_str)
        assert len(restored.merged_entities) == 1
        assert restored.merged_entities[0].entity_id == "e1"

    def test_claims_preserved(self) -> None:
        result = self._make_result(claims=[{"claim_id": "c1", "predicate": "test"}])
        json_str = result.to_json()
        restored = GraphExtractionResult.from_json(json_str)
        assert len(restored.claims) == 1
        assert restored.claims[0]["claim_id"] == "c1"

    def test_hierarchy_clusters_preserved(self) -> None:
        result = self._make_result(hierarchy_clusters=[{"cluster_id": "cl1"}])
        json_str = result.to_json()
        restored = GraphExtractionResult.from_json(json_str)
        assert len(restored.hierarchy_clusters) == 1

    def test_default_extraction_version(self) -> None:
        result = self._make_result()
        assert result.extraction_version == "1.0"

    def test_from_json_missing_optional_fields(self) -> None:
        minimal = json.dumps({
            "source_id": "doc1",
        })
        restored = GraphExtractionResult.from_json(minimal)
        assert restored.source_id == "doc1"
        assert restored.merged_entities == []
        assert restored.claims == []

    def test_claim_contradictions_preserved(self) -> None:
        result = self._make_result(claim_contradictions=[("c1", "c2")])
        json_str = result.to_json()
        restored = GraphExtractionResult.from_json(json_str)
        assert len(restored.claim_contradictions) == 1

    def test_with_relationships(self) -> None:
        eocc = _make_entity_occ("Alpha")
        eocc2 = _make_entity_occ("Beta")
        rocc = RelationshipOccurrence(
            text_unit_id="tu1",
            relationship_type="supplies",
            source_local_id=eocc.local_id,
            target_local_id=eocc2.local_id,
        )
        result = self._make_result(
            entity_occurrences=[eocc, eocc2],
            relationship_occurrences=[rocc],
        )
        json_str = result.to_json()
        restored = GraphExtractionResult.from_json(json_str)
        assert len(restored.relationship_occurrences) == 1
