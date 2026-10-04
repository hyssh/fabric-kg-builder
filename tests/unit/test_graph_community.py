"""Tests for graph/community.py — semantic community hierarchy (GRP-008)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from fabric_kg_builder.graph.community import (
    INSUFFICIENT_HIERARCHY_EVIDENCE,
    InsufficientCorpusResult,
    CommunityHierarchyResult,
    _MIN_ENTITIES_FOR_3_LEVELS,
    _TYPE_FAMILIES,
    _adjacency,
    _connected_components,
    _type_family,
    build_community_hierarchy,
)
from fabric_kg_builder.model.schemas import EntityRow, RelationshipRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entity(
    entity_id: str,
    entity_type: str = "org",
    display_name: str | None = None,
    description: str = "",
) -> EntityRow:
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
        description=description,
    )


def _make_relationship(
    source_id: str,
    target_id: str,
    rel_type: str = "knows",
    rel_id: str | None = None,
) -> RelationshipRow:
    from fabric_kg_builder.model.schemas import RelationshipRow
    rid = rel_id or f"rel:{source_id}:{target_id}:{rel_type}"
    key = hashlib.sha1(rid.encode()).hexdigest()[:16]
    return RelationshipRow(
        relationship_id=rid,
        relationship_type=rel_type,
        source_entity_id=source_id,
        target_entity_id=target_id,
        canonical_key=key,
        content_hash=key,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _make_entities(n: int, entity_type: str = "org") -> list[EntityRow]:
    return [_make_entity(f"e{i}", entity_type, f"Entity {i}") for i in range(n)]


# ---------------------------------------------------------------------------
# _type_family
# ---------------------------------------------------------------------------


class TestTypeFamily:
    def test_org_family(self) -> None:
        assert _type_family("org") == "organization"
        assert _type_family("company") == "organization"
        assert _type_family("vendor") == "organization"

    def test_location_family(self) -> None:
        assert _type_family("location") == "location"
        assert _type_family("zone") == "location"
        assert _type_family("region") == "location"

    def test_product_family(self) -> None:
        assert _type_family("product") == "product"
        assert _type_family("equipment") == "product"

    def test_process_family(self) -> None:
        assert _type_family("process") == "process"
        assert _type_family("workflow") == "process"

    def test_document_family(self) -> None:
        assert _type_family("document") == "document"
        assert _type_family("contract") == "document"

    def test_concept_family(self) -> None:
        assert _type_family("concept") == "concept"
        assert _type_family("claim") == "concept"

    def test_person_family(self) -> None:
        assert _type_family("person") == "person"
        assert _type_family("employee") == "person"

    def test_unknown_type_returns_other(self) -> None:
        assert _type_family("unknown_entity_type_xyz") == "other"


# ---------------------------------------------------------------------------
# _adjacency
# ---------------------------------------------------------------------------


class TestAdjacency:
    def test_empty(self) -> None:
        adj = _adjacency([], [])
        assert adj == {}

    def test_two_entities_no_rels(self) -> None:
        entities = _make_entities(2)
        adj = _adjacency(entities, [])
        assert len(adj) == 2
        for neighbors in adj.values():
            assert len(neighbors) == 0

    def test_bidirectional_edge(self) -> None:
        entities = _make_entities(2)
        rel = _make_relationship("e0", "e1")
        adj = _adjacency(entities, [rel])
        assert "e1" in adj["e0"]
        assert "e0" in adj["e1"]

    def test_external_entity_not_in_adj(self) -> None:
        entities = _make_entities(2)
        rel = _make_relationship("e0", "external-entity")
        adj = _adjacency(entities, [rel])
        # external-entity is not in the entity set → not added to adj
        assert "external-entity" not in adj


# ---------------------------------------------------------------------------
# _connected_components
# ---------------------------------------------------------------------------


class TestConnectedComponents:
    def test_single_node(self) -> None:
        adj = {"e0": set()}
        comps = _connected_components(["e0"], adj)
        assert len(comps) == 1
        assert comps[0] == ["e0"]

    def test_two_disconnected_nodes(self) -> None:
        adj = {"e0": set(), "e1": set()}
        comps = _connected_components(["e0", "e1"], adj)
        assert len(comps) == 2

    def test_connected_component(self) -> None:
        adj = {"e0": {"e1"}, "e1": {"e0"}, "e2": set()}
        comps = _connected_components(["e0", "e1", "e2"], adj)
        # e0 and e1 are connected; e2 is alone
        sizes = sorted(len(c) for c in comps)
        assert sizes == [1, 2]


# ---------------------------------------------------------------------------
# build_community_hierarchy
# ---------------------------------------------------------------------------


class TestBuildCommunityHierarchy:
    def test_insufficient_entities_returns_insufficient_result(self) -> None:
        entities = _make_entities(3)
        result = build_community_hierarchy(entities, [])
        assert isinstance(result, InsufficientCorpusResult)
        assert result.entity_count == 3
        assert result.levels_built == 0

    def test_insufficient_entity_count_threshold(self) -> None:
        entities = _make_entities(_MIN_ENTITIES_FOR_3_LEVELS - 1)
        result = build_community_hierarchy(entities, [])
        assert isinstance(result, InsufficientCorpusResult)
        assert INSUFFICIENT_HIERARCHY_EVIDENCE in result.reason

    def test_sufficient_entities_returns_hierarchy(self) -> None:
        entities = _make_entities(_MIN_ENTITIES_FOR_3_LEVELS)
        result = build_community_hierarchy(entities, [])
        assert isinstance(result, CommunityHierarchyResult)
        assert result.levels == 3

    def test_three_levels_produced(self) -> None:
        entities = _make_entities(20)
        result = build_community_hierarchy(entities, [])
        assert isinstance(result, CommunityHierarchyResult)
        levels_present = {c.level for c in result.clusters}
        assert 0 in levels_present  # leaf
        assert 1 in levels_present  # topic
        assert 2 in levels_present  # broad

    def test_all_entities_covered_in_memberships(self) -> None:
        entities = _make_entities(15)
        result = build_community_hierarchy(entities, [])
        assert isinstance(result, CommunityHierarchyResult)
        member_entity_ids = {m.entity_id for m in result.memberships if m.entity_id}
        entity_ids = {e.entity_id for e in entities}
        assert entity_ids <= member_entity_ids

    def test_deterministic_with_seed(self) -> None:
        entities = _make_entities(15)
        r1 = build_community_hierarchy(entities, [], seed=42)
        r2 = build_community_hierarchy(entities, [], seed=42)
        assert isinstance(r1, CommunityHierarchyResult)
        assert isinstance(r2, CommunityHierarchyResult)
        ids1 = sorted(c.cluster_id for c in r1.clusters)
        ids2 = sorted(c.cluster_id for c in r2.clusters)
        assert ids1 == ids2

    def test_domain_hash_propagated(self) -> None:
        entities = _make_entities(15)
        result = build_community_hierarchy(entities, [], domain_hash="hash123")
        assert isinstance(result, CommunityHierarchyResult)
        # At least some clusters should have domain_hash
        clusters_with_hash = [c for c in result.clusters if c.domain_hash == "hash123"]
        assert len(clusters_with_hash) > 0

    def test_relationships_affect_components(self) -> None:
        # Create 12 entities of same type; without rels they're all isolated
        entities = _make_entities(15, entity_type="org")
        rel = _make_relationship("e0", "e1")
        result = build_community_hierarchy(entities, [rel])
        assert isinstance(result, CommunityHierarchyResult)

    def test_parent_cluster_ids_set(self) -> None:
        entities = _make_entities(15)
        result = build_community_hierarchy(entities, [])
        assert isinstance(result, CommunityHierarchyResult)
        # Level 0 and 1 clusters should have parent IDs
        level0 = [c for c in result.clusters if c.level == 0]
        assert all(c.parent_cluster_id is not None for c in level0)
        level1 = [c for c in result.clusters if c.level == 1]
        assert all(c.parent_cluster_id is not None for c in level1)

    def test_no_all_null_membership_rows(self) -> None:
        entities = _make_entities(15)
        result = build_community_hierarchy(entities, [])
        assert isinstance(result, CommunityHierarchyResult)
        for m in result.memberships:
            # Each membership must have at least an entity_id
            assert m.entity_id is not None

    def test_mixed_entity_types(self) -> None:
        entities = (
            _make_entities(5, "org") +
            _make_entities(5, "location") +
            _make_entities(5, "product")
        )
        # Reassign unique IDs
        for i, e in enumerate(entities):
            entities[i] = EntityRow(
                entity_id=f"mixed-{i}",
                entity_type=e.entity_type,
                display_name=f"Entity {i}",
                canonical_key=f"key-{i}",
                content_hash=f"hash-{i}",
                created_at=e.created_at,
                updated_at=e.updated_at,
            )
        result = build_community_hierarchy(entities, [])
        assert isinstance(result, CommunityHierarchyResult)
        # Should have org, location, product families at level 2
        level2 = [c for c in result.clusters if c.level == 2]
        assert len(level2) >= 3  # at least 3 family clusters
