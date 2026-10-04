"""Tests for graph/labeler.py — community label generation (GRP-009)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock

import pytest

from fabric_kg_builder.graph.labeler import (
    _collect_evidence_ids,
    _descendant_entity_ids,
    _member_entity_ids,
    label_communities,
)
from fabric_kg_builder.model.schemas import ClusterMembershipRow, ClusterRow, EntityRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity(entity_id: str, entity_type: str = "org", display_name: Optional[str] = None) -> EntityRow:
    name = display_name or entity_id
    key = hashlib.sha1(entity_id.encode()).hexdigest()[:16]
    return EntityRow(
        entity_id=entity_id,
        entity_type=entity_type,
        display_name=name,
        canonical_key=key,
        content_hash=key,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        description=f"Description of {name}",
    )


def _cluster(cluster_id: str, level: int = 0, parent_id: Optional[str] = None) -> ClusterRow:
    return ClusterRow(
        cluster_id=cluster_id,
        hierarchy_version="1.0",
        level=level,
        parent_cluster_id=parent_id,
        label="",
        description="",
        method="test",
    )


def _membership(
    cluster_id: str,
    entity_id: str,
    evidence_ids: Optional[list[str]] = None,
) -> ClusterMembershipRow:
    return ClusterMembershipRow(
        cluster_id=cluster_id,
        entity_id=entity_id,
        relationship_id=None,
        claim_id=None,
        score=1.0,
        rationale="member",
        evidence_ids=evidence_ids or [],
    )


# ---------------------------------------------------------------------------
# _member_entity_ids
# ---------------------------------------------------------------------------


class TestMemberEntityIds:
    def test_returns_ids_for_cluster(self) -> None:
        memberships = [
            _membership("c1", "e1"),
            _membership("c1", "e2"),
            _membership("c2", "e3"),
        ]
        result = _member_entity_ids("c1", memberships)
        assert set(result) == {"e1", "e2"}

    def test_empty_memberships(self) -> None:
        assert _member_entity_ids("c1", []) == []

    def test_no_match(self) -> None:
        memberships = [_membership("c2", "e3")]
        assert _member_entity_ids("c1", memberships) == []


# ---------------------------------------------------------------------------
# _descendant_entity_ids
# ---------------------------------------------------------------------------


class TestDescendantEntityIds:
    def test_leaf_cluster_no_children(self) -> None:
        clusters = {
            "c1": _cluster("c1"),
        }
        memberships = [_membership("c1", "e1")]
        result = _descendant_entity_ids("c1", clusters, memberships)
        assert "e1" in result

    def test_parent_includes_child_entities(self) -> None:
        c_parent = _cluster("c_parent", level=1)
        c_child = _cluster("c_child", level=0, parent_id="c_parent")
        clusters = {"c_parent": c_parent, "c_child": c_child}
        memberships = [
            _membership("c_parent", "e1"),
            _membership("c_child", "e2"),
        ]
        result = _descendant_entity_ids("c_parent", clusters, memberships)
        assert "e1" in result
        assert "e2" in result


# ---------------------------------------------------------------------------
# _collect_evidence_ids
# ---------------------------------------------------------------------------


class TestCollectEvidenceIds:
    def test_collects_from_matching_memberships(self) -> None:
        memberships = [
            _membership("c1", "e1", evidence_ids=["ev-a", "ev-b"]),
            _membership("c1", "e2", evidence_ids=["ev-c"]),
        ]
        result = _collect_evidence_ids(["e1", "e2"], memberships, "c1")
        assert set(result) == {"ev-a", "ev-b", "ev-c"}

    def test_deduplicates_evidence_ids(self) -> None:
        memberships = [
            _membership("c1", "e1", evidence_ids=["shared"]),
            _membership("c1", "e2", evidence_ids=["shared"]),
        ]
        result = _collect_evidence_ids(["e1", "e2"], memberships, "c1")
        assert result.count("shared") == 1

    def test_empty_entity_ids(self) -> None:
        memberships = [_membership("c1", "e1", evidence_ids=["ev-a"])]
        result = _collect_evidence_ids([], memberships, "c1")
        assert result == []


# ---------------------------------------------------------------------------
# label_communities
# ---------------------------------------------------------------------------


class TestLabelCommunities:
    def test_empty_inputs(self) -> None:
        result = label_communities([], [], [])
        assert result == []

    def test_clusters_without_members_pass_through(self) -> None:
        clusters = [_cluster("c1")]
        result = label_communities(clusters, [], [])
        assert len(result) == 1
        # Label should remain empty (no members)
        assert result[0].cluster_id == "c1"

    def test_label_generated_from_entity_names(self) -> None:
        entities = [_entity("e1", "org", "Alpha Corp"), _entity("e2", "org", "Beta Ltd")]
        cluster = _cluster("c1")
        memberships = [_membership("c1", "e1"), _membership("c1", "e2")]
        result = label_communities([cluster], entities, memberships)
        assert len(result) == 1
        updated = result[0]
        # Label should contain at least one entity name
        assert "Alpha Corp" in updated.label or "Beta Ltd" in updated.label

    def test_type_annotation_in_label_for_single_type(self) -> None:
        entities = [_entity("e1", "org", "Acme")]
        cluster = _cluster("c1")
        memberships = [_membership("c1", "e1")]
        result = label_communities([cluster], entities, memberships)
        assert len(result) == 1
        # Single type → label should include type annotation
        assert "org" in result[0].label

    def test_truncation_with_many_entities(self) -> None:
        entities = [_entity(f"e{i}", "org", f"Entity{i:03d}") for i in range(10)]
        cluster = _cluster("c1")
        memberships = [_membership("c1", e.entity_id) for e in entities]
        result = label_communities([cluster], entities, memberships)
        assert "…" in result[0].label

    def test_description_generated(self) -> None:
        entities = [_entity("e1", "org", "Alpha Corp")]
        cluster = _cluster("c1")
        memberships = [_membership("c1", "e1")]
        result = label_communities([cluster], entities, memberships)
        assert result[0].description  # should be non-empty

    def test_level_0_uses_direct_members(self) -> None:
        entities = [_entity("e1"), _entity("e2")]
        leaf = _cluster("leaf", level=0)
        memberships = [_membership("leaf", "e1")]
        result = label_communities([leaf], entities, memberships)
        assert len(result) == 1

    def test_level_1_uses_descendants(self) -> None:
        entities = [_entity("e1"), _entity("e2")]
        parent = _cluster("parent", level=1)
        child = _cluster("child", level=0, parent_id="parent")
        memberships = [
            _membership("child", "e1"),
            _membership("child", "e2"),
        ]
        result = label_communities([parent, child], entities, memberships)
        assert len(result) == 2

    def test_with_summarizer(self) -> None:
        mock_summarizer = MagicMock()
        mock_summarizer.summarize.return_value = "AI-generated summary"
        # Also mock consolidate since that's what consolidate_description checks first
        mock_summarizer.consolidate.return_value = MagicMock(summary="AI-generated summary")
        entities = [_entity("e1", "org", "Alpha")]
        cluster = _cluster("c1")
        memberships = [_membership("c1", "e1")]
        result = label_communities(
            [cluster], entities, memberships, summarizer=mock_summarizer
        )
        assert len(result) == 1
        # Description should be non-empty
        assert result[0].description
